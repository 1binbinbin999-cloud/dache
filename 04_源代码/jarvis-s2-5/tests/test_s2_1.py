"""S2-1 测试：模型接入层——直接适配器（Ollama 本地 + Gemini 免费层）。

测试 1：live 切换链形状（Gemini -> Ollama 本地 -> Stub；embedding 只在同类内）。
测试 2：stub 模式行为与 P2–P6 完全一致（默认 config 回归）。
测试 3：Gemini 缺 Key 明确报 missing_key，链继续切到本地再到 Stub，不静默降级。
测试 4：Groq / OpenRouter 桩位报 not_implemented，切链继续，不产生调用。
测试 5：429 识别与重试（本地 HTTP 桩服务，不联网）：429 一次后 200 则成功；
       一直 429 则报 rate_limited。
测试 6：live 启动要求配置一致：只改 router_mode 不改开关则 boot 拒绝；
       三项一致则 boot + health 通过。
测试 7：live 模式 CLI 端到端（Ollama 指向死端口、Gemini 无 Key）：
       local 与 fast 均一路切到 stub-local，全程无真实调用。
测试 8：Ollama 实调用（仅当本地服务可达且模型已拉取；否则 skip，
       不谎报通过）：local 真实生成 + embedding 真实向量。
测试 9：Gemini 缺 Key 时适配器直接报 missing_key（不返回任何降级结果）。
测试 10：provider-status CLI：各供应商状态齐全，Key 缺失明确报 missing_key。
测试 11：live embedding 全失败仍按 P6-15 返回 embedding-unavailable（空内容）。
测试 12：未知供应商 type 是配置错误，建适配器时当场报 ValueError。
测试 13：Gemini mock 响应解析（本地桩服务，不联网）：candidates 与
       usageMetadata 解析正确；429 一次后 200 则重试成功。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

S2_DIR = Path(__file__).resolve().parent.parent
PY = sys.executable

sys.path.insert(0, str(S2_DIR))
from adapters import AdapterError, GeminiAdapter, OllamaAdapter, build_adapter  # noqa: E402
from router import Router  # noqa: E402


def run_cli(args, cwd=S2_DIR, env=None, timeout=60):
    merged = dict(os.environ)
    for key in list(merged):
        if "API_KEY" in key or "OPENAI" in key or "ANTHROPIC" in key:
            del merged[key]
    if env:
        merged.update(env)
    return subprocess.run(
        [PY, "jarvis.py", *args], cwd=cwd, env=merged,
        capture_output=True, text=True, timeout=timeout,
    )


def cli_json(args, cwd=S2_DIR, timeout=60):
    r = run_cli(args, cwd=cwd, timeout=timeout)
    assert r.returncode == 0, f"{args} failed: {r.stderr}"
    return json.loads(r.stdout)


def fresh_db():
    """清空运行数据后 boot 重建（沿用 P6 的 fresh_db 清单）。"""
    data_dir = S2_DIR / "data"
    for name in ("jarvis.db", "improvements.db", "events.jsonl",
                 "events.checkpoint.json"):
        p = data_dir / name
        if p.exists():
            p.unlink()
    backups = data_dir / "backups"
    if backups.exists():
        shutil.rmtree(backups)
    cli_json(["boot"])


def read_events(cwd=S2_DIR):
    path = Path(cwd) / "data" / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class FakeLog:
    """内存事件日志：只记 append 的调用，供 in-process Router 测试用。"""

    def __init__(self):
        self.events: list[dict] = []

    def append(self, event: str, result: str, detail: str = "") -> dict:
        rec = {"event": event, "result": result, "detail": detail}
        self.events.append(rec)
        return rec


DEAD_HOST = "http://127.0.0.1:9"  # 必然拒绝连接的端口，用于确定性失败测试


def live_config(dead_ollama: bool = True) -> dict:
    """in-process 测试用的 live 配置（不读磁盘 config）。"""
    host = DEAD_HOST if dead_ollama else "http://localhost:11434"
    return {
        "model": {
            "router_mode": "live",
            "logical_models": {
                "fast": "stub-fast", "capable": "stub-capable",
                "reasoning": "stub-reasoning", "local": "stub-local",
                "embedding": "stub-embedding",
            },
            "live_logical_models": {
                "fast": "gemini-flash", "capable": "gemini-flash",
                "reasoning": "gemini-flash", "local": "ollama-local",
                "embedding": "ollama-embedding",
            },
            "fallback_chain": ["gemini-flash", "ollama-local", "stub-local"],
            "embedding_fallback_chain": ["ollama-embedding", "stub-embedding"],
            "providers": {
                "gemini-flash": {"type": "gemini", "model": "gemini-3.6-flash",
                                 "timeout_s": 10, "retry_max": 0},
                "ollama-local": {"type": "ollama", "model": "qwen2.5:0.5b",
                                 "host": host, "timeout_s": 5, "retry_max": 0},
                "ollama-embedding": {"type": "ollama", "model": "nomic-embed-text",
                                     "host": host, "timeout_s": 5, "retry_max": 0},
                "groq-gpt-oss": {"type": "groq", "status": "not_implemented"},
                "openrouter-free": {"type": "openrouter", "status": "not_implemented"},
            },
        }
    }


def stub_config() -> dict:
    return {
        "model": {
            "router_mode": "stub",
            "logical_models": {
                "fast": "stub-fast", "capable": "stub-capable",
                "reasoning": "stub-reasoning", "local": "stub-local",
                "embedding": "stub-embedding",
            },
        }
    }


def make_live_copy(dead_ollama: bool = True) -> Path:
    """复制 s2-1 到临时目录并改成 live 配置（CLI 端到端测试用）。"""
    tmp = Path(tempfile.mkdtemp())
    dest = tmp / "jarvis-s2-1-live"
    shutil.copytree(S2_DIR, dest, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.rmtree(dest / "data", ignore_errors=True)
    (dest / "data").mkdir(exist_ok=True)
    cfg = dest / "config.yaml"
    text = cfg.read_text(encoding="utf-8")
    text = text.replace("router_mode: stub", "router_mode: live")
    text = text.replace("model_enabled: false", "model_enabled: true")
    text = text.replace("network_calls_allowed: false", "network_calls_allowed: true")
    if dead_ollama:
        text = text.replace("host: http://localhost:11434",
                            f"host: {DEAD_HOST}")
    cfg.write_text(text, encoding="utf-8")
    return dest


def ollama_reachable() -> tuple[bool, list[str]]:
    """Ollama 服务是否可达、已拉取哪些模型（探针，不推理）。"""
    try:
        from adapters import _get_json
        parsed = _get_json("http://localhost:11434/api/tags", timeout_s=5)
        models = [m.get("name") for m in parsed.get("models", [])
                  if isinstance(m, dict) and m.get("name")]
        return True, models
    except Exception:
        return False, []


class TestS21(unittest.TestCase):
    def setUp(self):
        self._saved_key = os.environ.pop("GEMINI_API_KEY", None)

    def tearDown(self):
        if self._saved_key is not None:
            os.environ["GEMINI_API_KEY"] = self._saved_key
        else:
            os.environ.pop("GEMINI_API_KEY", None)

    def test_1_live_chain_shape(self):
        router = Router(live_config(), FakeLog())
        self.assertEqual(router.chain_for("fast"),
                         ["gemini-flash", "ollama-local", "stub-local"])
        self.assertEqual(router.chain_for("capable"),
                         ["gemini-flash", "ollama-local", "stub-local"])
        self.assertEqual(router.chain_for("local"),
                         ["ollama-local", "stub-local"])
        self.assertEqual(router.chain_for("embedding"),
                         ["ollama-embedding", "stub-embedding"])

    def test_2_stub_mode_unchanged(self):
        router = Router(stub_config(), FakeLog())
        self.assertEqual(router.chain_for("fast"),
                         ["stub-fast", "stub-capable", "stub-local"])
        self.assertEqual(router.chain_for("local"), ["stub-local"])
        self.assertEqual(router.chain_for("embedding"), ["stub-embedding"])
        r = router.complete("fast", "你好")
        self.assertEqual(r["model"], "stub-fast")
        self.assertFalse(r["degraded"])

    def test_3_gemini_missing_key_falls_back(self):
        log = FakeLog()
        router = Router(live_config(), log)
        r = router.complete("fast", "你好")
        self.assertEqual(r["model"], "stub-local")
        self.assertFalse(r["degraded"])
        self.assertEqual(r["attempted"],
                         ["gemini-flash", "ollama-local", "stub-local"])
        fallbacks = [e for e in log.events if e["event"] == "router_fallback"]
        self.assertEqual(len(fallbacks), 2)
        first = json.loads(fallbacks[0]["detail"])
        self.assertEqual(first["provider"], "gemini-flash")
        self.assertEqual(first["error_class"], "missing_key")
        second = json.loads(fallbacks[1]["detail"])
        self.assertEqual(second["provider"], "ollama-local")
        self.assertEqual(second["error_class"], "unavailable")

    def test_4_groq_openrouter_stubs_not_implemented(self):
        cfg = live_config()
        cfg["model"]["live_logical_models"]["fast"] = "groq-gpt-oss"
        cfg["model"]["fallback_chain"] = ["groq-gpt-oss", "openrouter-free",
                                          "stub-local"]
        log = FakeLog()
        router = Router(cfg, log)
        r = router.complete("fast", "你好")
        self.assertEqual(r["model"], "stub-local")
        self.assertFalse(r["degraded"])
        fallbacks = [json.loads(e["detail"]) for e in log.events
                     if e["event"] == "router_fallback"]
        self.assertEqual([f["provider"] for f in fallbacks],
                         ["groq-gpt-oss", "openrouter-free"])
        self.assertTrue(all(f["error_class"] == "not_implemented"
                            for f in fallbacks))
        probes = router.probe_all()
        self.assertEqual(probes["groq-gpt-oss"]["reason"], "not_implemented")
        self.assertEqual(probes["openrouter-free"]["reason"], "not_implemented")

    def test_5_rate_limit_recognized_and_retried(self):
        # 本地桩服务：第一次 429，第二次 200（Ollama 形状的响应）。
        calls = {"n": 0}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                calls["n"] += 1
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                if calls["n"] == 1:
                    self.send_response(429)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error": {"message": "rate limited"}}')
                    return
                body = json.dumps({"model": "qwen2.5:0.5b", "done": True,
                                   "response": "ok",
                                   "prompt_eval_count": 3,
                                   "eval_count": 1}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            adapter = OllamaAdapter("ollama-test", {
                "model": "qwen2.5:0.5b",
                "host": f"http://127.0.0.1:{port}",
                "timeout_s": 5, "retry_max": 1, "retry_backoff_s": 0})
            result = adapter.complete("hi")
            self.assertEqual(result["content"], "ok")
            self.assertEqual(calls["n"], 2)  # 429 后重试一次成功

            # 一直 429：重试耗尽后报 rate_limited
            calls["n"] = 0

            class Always429(BaseHTTPRequestHandler):
                def do_POST(self):
                    calls["n"] += 1
                    length = int(self.headers.get("Content-Length", 0))
                    self.rfile.read(length)
                    self.send_response(429)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error": "slow down"}')

                def log_message(self, *args):
                    pass

            server2 = ThreadingHTTPServer(("127.0.0.1", 0), Always429)
            port2 = server2.server_address[1]
            t2 = threading.Thread(target=server2.serve_forever, daemon=True)
            t2.start()
            try:
                adapter2 = OllamaAdapter("ollama-test2", {
                    "model": "qwen2.5:0.5b",
                    "host": f"http://127.0.0.1:{port2}",
                    "timeout_s": 5, "retry_max": 1, "retry_backoff_s": 0})
                with self.assertRaises(AdapterError) as cm:
                    adapter2.complete("hi")
                self.assertEqual(cm.exception.error_class, "rate_limited")
                self.assertEqual(calls["n"], 2)  # 首次 + 1 次重试
            finally:
                server2.shutdown()
        finally:
            server.shutdown()

    def test_6_live_boot_requires_consistent_config(self):
        dest = make_live_copy()
        # 只改 router_mode 不改开关：boot 必须拒绝
        cfg = dest / "config.yaml"
        text = cfg.read_text(encoding="utf-8")
        text = text.replace("model_enabled: true", "model_enabled: false")
        text = text.replace("network_calls_allowed: true",
                            "network_calls_allowed: false")
        cfg.write_text(text, encoding="utf-8")
        r = run_cli(["boot"], cwd=dest, timeout=30)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("live mode requires", r.stderr)
        # 三项一致：boot + health 通过
        text = cfg.read_text(encoding="utf-8")
        text = text.replace("model_enabled: false", "model_enabled: true")
        text = text.replace("network_calls_allowed: false",
                            "network_calls_allowed: true")
        cfg.write_text(text, encoding="utf-8")
        boot = cli_json(["boot"], cwd=dest, timeout=30)
        self.assertEqual(boot["status"], "ok")
        health = cli_json(["health"], cwd=dest, timeout=30)
        self.assertEqual(health["status"], "ok")

    def test_7_live_cli_falls_back_without_servers(self):
        dest = make_live_copy()
        cli_json(["boot"], cwd=dest, timeout=30)
        local = cli_json(["router-call", "--logical", "local",
                          "--prompt", "本地测试"], cwd=dest, timeout=60)
        self.assertEqual(local["model"], "stub-local")
        self.assertFalse(local["degraded"])
        fast = cli_json(["router-call", "--logical", "fast",
                         "--prompt", "云端测试"], cwd=dest, timeout=60)
        self.assertEqual(fast["model"], "stub-local")
        self.assertEqual(fast["attempted"],
                         ["gemini-flash", "ollama-local", "stub-local"])
        events = read_events(cwd=dest)
        fallbacks = [json.loads(e["detail"]) for e in events
                     if e["event"] == "router_fallback"]
        classes = {(f["provider"], f["error_class"]) for f in fallbacks}
        self.assertIn(("gemini-flash", "missing_key"), classes)
        self.assertIn(("ollama-local", "unavailable"), classes)

    def test_8_ollama_real_call(self):
        reachable, models = ollama_reachable()
        if not reachable:
            self.skipTest("Ollama 服务未运行，跳过实调用（未验证，不谎报通过）")
        # Ollama 会把无标签名存为 name:latest；比较时按“名”归一化。
        present = {m.split(":")[0] for m in models}
        if "qwen2.5" not in present:
            self.skipTest("qwen2.5:0.5b 未拉取，跳过实调用（未验证）")
        dest = make_live_copy(dead_ollama=False)
        cli_json(["boot"], cwd=dest, timeout=30)
        r = cli_json(["router-call", "--logical", "local",
                      "--prompt", "只回两个字：你好"], cwd=dest, timeout=180)
        self.assertEqual(r["model"], "ollama-local")
        self.assertFalse(r["degraded"])
        self.assertTrue(r["content"].strip())
        self.assertEqual(r["cost_usd"], 0.0)
        self.assertGreater(r["latency_ms"], 0)
        # embedding：模型已拉取则须为真向量，否则跳过本段
        if "nomic-embed-text" not in present:
            self.skipTest("nomic-embed-text 未拉取，跳过 embedding 实调用")
        emb = cli_json(["router-call", "--logical", "embedding",
                        "--prompt", "向量测试"], cwd=dest, timeout=120)
        self.assertEqual(emb["model"], "ollama-embedding")
        self.assertIn("embeddings", emb)
        self.assertEqual(emb["dimensions"], len(emb["embeddings"][0]))
        self.assertGreater(emb["dimensions"], 0)
        self.assertEqual(emb["content"], "")

    def test_9_gemini_missing_key_raises(self):
        adapter = GeminiAdapter("gemini-flash", {
            "model": "gemini-3.6-flash", "timeout_s": 10, "retry_max": 0})
        with self.assertRaises(AdapterError) as cm:
            adapter.complete("你好")
        self.assertEqual(cm.exception.error_class, "missing_key")
        probe = adapter.probe()
        self.assertFalse(probe["ok"])
        self.assertEqual(probe["reason"], "missing_key")

    def test_10_provider_status_cli(self):
        fresh_db()
        status = cli_json(["provider-status"], timeout=30)
        self.assertEqual(status["router_mode"], "stub")
        providers = status["providers"]
        self.assertEqual(providers["gemini-flash"]["reason"], "missing_key")
        self.assertEqual(providers["groq-gpt-oss"]["reason"], "not_implemented")
        self.assertEqual(providers["openrouter-free"]["reason"], "not_implemented")
        for stub in ("stub-fast", "stub-capable", "stub-local", "stub-embedding"):
            self.assertTrue(providers[stub]["ok"])
        self.assertIn("reason", providers["ollama-local"])

    def test_11_live_embedding_all_fail_unavailable(self):
        dest = make_live_copy()
        cli_json(["boot"], cwd=dest, timeout=30)
        failed = cli_json(["router-call", "--logical", "embedding",
                           "--prompt", "向量测试",
                           "--fail", "stub-embedding"], cwd=dest, timeout=60)
        self.assertEqual(failed["model"], "embedding-unavailable")
        self.assertEqual(failed["content"], "")
        self.assertTrue(failed["degraded"])
        self.assertNotIn("stub-capable", failed.get("attempted", []))
        self.assertNotIn("stub-local", failed.get("attempted", []))

    def test_12_unknown_provider_type_fails_fast(self):
        with self.assertRaises(ValueError) as cm:
            build_adapter("weird", {"type": "nope"})
        self.assertIn("unknown provider type", str(cm.exception))

    def test_13_gemini_mock_response_parsing(self):
        # 本地桩服务返回 Gemini 形状的响应（含 usageMetadata）：
        # 适配器须解析出文本与 token 数；429 一次后 200 则重试成功。
        # 全程不联网、不耗额度；base_url 只在测试配置里指向桩服务。
        calls = {"n": 0}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                calls["n"] += 1
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                req = json.loads(body)
                # 请求体须含 contents，且 Key 在 URL 查询里（不在 body 里）
                assert "contents" in req
                if calls["n"] == 1:
                    self.send_response(429)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error": {"message": "quota"}}')
                    return
                resp = json.dumps({
                    "candidates": [{"content": {"parts": [
                        {"text": "mock 回答"}]}}],
                    "usageMetadata": {"promptTokenCount": 7,
                                      "candidatesTokenCount": 4}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(resp)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            os.environ["GEMINI_API_KEY"] = "test-key-not-real"
            adapter = GeminiAdapter("gemini-flash", {
                "model": "gemini-3.6-flash",
                "base_url": f"http://127.0.0.1:{port}",
                "timeout_s": 5, "retry_max": 1, "retry_backoff_s": 0})
            result = adapter.complete("你好")
            self.assertEqual(result["content"], "mock 回答")
            self.assertEqual(result["tokens_in"], 7)
            self.assertEqual(result["tokens_out"], 4)
            self.assertEqual(result["cost_usd"], 0.0)
            self.assertEqual(calls["n"], 2)  # 429 后重试一次成功
        finally:
            server.shutdown()
            os.environ.pop("GEMINI_API_KEY", None)


if __name__ == "__main__":
    unittest.main()
