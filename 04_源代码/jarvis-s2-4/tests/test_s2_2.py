"""S2-2 测试：真实调用测试——usage 日志与审计。

测试 1：UsageLog 写 JSONL 信封（time/event/result/detail，与事件日志
       同一口径）；detail 只含数字与标识，不含提示词与响应原文。
测试 2：审计通过——Ollama 形状的 usage 记录（prompt_eval_count /
       eval_count 与 token 数一致，费用 $0）。
测试 3：审计通过——Gemini 形状的 usage 记录（usageMetadata 的
       promptTokenCount / candidatesTokenCount 与 token 数一致）。
测试 4：审计抓到 token 不一致（tokens_in 与 prompt_eval_count 差 1）。
测试 5：审计抓到费用非零（cost_usd 0.01 即失败）。
测试 6：审计抓到 backend_usage 缺失（空 dict 即失败）。
测试 7：审计对缺失的日志文件报 log_missing（不是“没有调用所以通过”）。
测试 8：Router 只记真实适配器调用（本地桩服务扮 Ollama）；
       stub 模式调用不写 usage 日志。
测试 9：Gemini 形状桩服务：Router 走 gemini-flash（本地桩，不联网），
       usage 记录带 usageMetadata，审计通过。
测试 10：Ollama 实调用（仅当本地服务可达且模型已拉取；否则 skip，
        不谎报通过）：chat + embedding 各一次，usage 日志 2 条，
        usage-audit CLI 通过。
测试 11：usage-audit CLI 在无日志时 exit 1 并报 log_missing。
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
from router import Router  # noqa: E402
from usage import UsageLog, audit_usage_log  # noqa: E402


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
    """清空运行数据后 boot 重建（S2-1 清单 + usage.jsonl）。"""
    data_dir = S2_DIR / "data"
    for name in ("jarvis.db", "improvements.db", "events.jsonl",
                 "events.checkpoint.json", "usage.jsonl"):
        p = data_dir / name
        if p.exists():
            p.unlink()
    backups = data_dir / "backups"
    if backups.exists():
        shutil.rmtree(backups)
    cli_json(["boot"])


class FakeLog:
    """内存事件日志：只记 append 的调用，供 in-process Router 测试用。"""

    def __init__(self):
        self.events: list[dict] = []

    def append(self, event: str, result: str, detail: str = "") -> dict:
        rec = {"event": event, "result": result, "detail": detail}
        self.events.append(rec)
        return rec


def ollama_record(tokens_in=35, tokens_out=3, cost=0.0):
    return {
        "logical": "local",
        "provider": "ollama-local",
        "model": "ollama-local",
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "backend_usage": {"prompt_eval_count": tokens_in,
                          "eval_count": tokens_out},
        "latency_ms": 1745,
        "cost_usd": cost,
        "result_hash": "abc",
    }


def gemini_record(tokens_in=8, tokens_out=23, cost=0.0):
    return {
        "logical": "fast",
        "provider": "gemini-flash",
        "model": "gemini-flash",
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "backend_usage": {"promptTokenCount": tokens_in,
                          "candidatesTokenCount": tokens_out,
                          "totalTokenCount": tokens_in + tokens_out},
        "latency_ms": 25872,
        "cost_usd": cost,
        "result_hash": "def",
    }


def write_usage(tmp: Path, records: list[dict]) -> Path:
    path = tmp / "usage.jsonl"
    log = UsageLog(path)
    for rec in records:
        log.append(rec)
    return path


def live_config(ollama_host: str, gemini_base_url: str | None = None) -> dict:
    gemini_cfg: dict = {"type": "gemini", "model": "gemini-3.6-flash",
                        "timeout_s": 10, "retry_max": 0}
    if gemini_base_url:
        gemini_cfg["base_url"] = gemini_base_url
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
                "gemini-flash": gemini_cfg,
                "ollama-local": {"type": "ollama", "model": "qwen2.5:0.5b",
                                 "host": ollama_host, "timeout_s": 5,
                                 "retry_max": 0},
                "ollama-embedding": {"type": "ollama",
                                     "model": "nomic-embed-text",
                                     "host": ollama_host, "timeout_s": 5,
                                     "retry_max": 0},
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
    """复制 s2-2 到临时目录并改成 live 配置（CLI 端到端测试用）。"""
    tmp = Path(tempfile.mkdtemp())
    dest = tmp / "jarvis-s2-2-live"
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
                            "host: http://127.0.0.1:9")
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


class TestS22(unittest.TestCase):
    def setUp(self):
        self._saved_key = os.environ.pop("GEMINI_API_KEY", None)
        self._tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        if self._saved_key is not None:
            os.environ["GEMINI_API_KEY"] = self._saved_key
        else:
            os.environ.pop("GEMINI_API_KEY", None)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_1_usage_log_envelope_no_prompt_or_content(self):
        path = self._tmp / "usage.jsonl"
        log = UsageLog(path)
        rec = ollama_record()
        log.append(rec)
        raw = path.read_text(encoding="utf-8")
        # 信封口径与事件日志一致；detail 里只有数字与标识。
        envelope = json.loads(raw.strip())
        self.assertEqual(envelope["event"], "usage")
        self.assertEqual(envelope["result"], "ok")
        self.assertIn("time", envelope)
        detail = json.loads(envelope["detail"])
        self.assertEqual(detail, rec)
        self.assertNotIn("content", detail)
        self.assertNotIn("prompt", detail)
        self.assertEqual(log.read_all()[0]["event"], "usage")

    def test_2_audit_passes_ollama_record(self):
        path = write_usage(self._tmp, [ollama_record()])
        result = audit_usage_log(path)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["mismatches"], [])

    def test_3_audit_passes_gemini_record(self):
        path = write_usage(self._tmp, [gemini_record()])
        result = audit_usage_log(path)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["total"], 1)

    def test_4_audit_catches_token_mismatch(self):
        rec = ollama_record(tokens_in=35)
        rec["backend_usage"] = {"prompt_eval_count": 34, "eval_count": 3}
        path = write_usage(self._tmp, [rec])
        result = audit_usage_log(path)
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["mismatches"]), 1)
        self.assertTrue(any("token_mismatch" in i
                            for i in result["mismatches"][0]["issues"]))
        self.assertTrue(any("token_mismatch" in i for i in result["issues"]))

    def test_5_audit_catches_nonzero_cost(self):
        path = write_usage(self._tmp, [ollama_record(cost=0.01)])
        result = audit_usage_log(path)
        self.assertFalse(result["ok"])
        self.assertTrue(any("nonzero_cost" in i for i in result["issues"]))

    def test_6_audit_missing_backend_usage(self):
        rec = ollama_record()
        rec["backend_usage"] = {}
        path = write_usage(self._tmp, [rec])
        result = audit_usage_log(path)
        self.assertFalse(result["ok"])
        self.assertTrue(any("missing_backend_usage" in i
                            for i in result["issues"]))

    def test_7_audit_missing_log_file(self):
        result = audit_usage_log(self._tmp / "no-such-usage.jsonl")
        self.assertFalse(result["ok"])
        self.assertEqual(result["issues"], ["log_missing"])
        self.assertEqual(result["total"], 0)

    def test_8_router_logs_real_adapter_only(self):
        # 本地桩服务扮 Ollama（/api/generate 返回固定 token 数）。
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
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
            usage_path = self._tmp / "usage.jsonl"
            usage_log = UsageLog(usage_path)
            router = Router(
                live_config(f"http://127.0.0.1:{port}"), FakeLog(),
                usage_log=usage_log)
            r = router.complete("local", "hi")
            self.assertEqual(r["model"], "ollama-local")
            self.assertFalse(r["degraded"])
            records = usage_log.read_all()
            self.assertEqual(len(records), 1)
            detail = json.loads(records[0]["detail"])
            self.assertEqual(detail["provider"], "ollama-local")
            self.assertEqual(detail["tokens_in"], 3)
            self.assertEqual(detail["tokens_out"], 1)
            self.assertEqual(detail["backend_usage"],
                             {"prompt_eval_count": 3, "eval_count": 1})
            self.assertEqual(detail["cost_usd"], 0.0)
            self.assertTrue(audit_usage_log(usage_path)["ok"])
            # stub 模式调用不写 usage 日志。
            stub_router = Router(stub_config(), FakeLog(),
                                 usage_log=usage_log)
            stub_router.complete("fast", "hi")
            self.assertEqual(len(usage_log.read_all()), 1)
        finally:
            server.shutdown()

    def test_9_gemini_shape_stub_audit(self):
        # 本地桩服务返回 Gemini 形状 JSON（含 usageMetadata）。
        # 全程不联网、不耗额度；Key 只用测试假值。
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                req = json.loads(body)
                assert "contents" in req
                resp = json.dumps({
                    "candidates": [{"content": {"parts": [
                        {"text": "mock 回答"}]}}],
                    "usageMetadata": {"promptTokenCount": 7,
                                      "candidatesTokenCount": 4,
                                      "totalTokenCount": 11}}).encode()
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
            usage_path = self._tmp / "usage.jsonl"
            usage_log = UsageLog(usage_path)
            router = Router(
                live_config("http://127.0.0.1:9",
                            gemini_base_url=f"http://127.0.0.1:{port}"),
                FakeLog(), usage_log=usage_log)
            r = router.complete("fast", "你好")
            self.assertEqual(r["model"], "gemini-flash")
            self.assertEqual(r["content"], "mock 回答")
            self.assertEqual(r["tokens_in"], 7)
            self.assertEqual(r["tokens_out"], 4)
            records = usage_log.read_all()
            self.assertEqual(len(records), 1)
            detail = json.loads(records[0]["detail"])
            self.assertEqual(detail["provider"], "gemini-flash")
            self.assertEqual(detail["backend_usage"]["promptTokenCount"], 7)
            self.assertEqual(detail["backend_usage"]["candidatesTokenCount"], 4)
            self.assertTrue(audit_usage_log(usage_path)["ok"])
        finally:
            server.shutdown()
            os.environ.pop("GEMINI_API_KEY", None)

    def test_10_ollama_real_call_usage_audit(self):
        reachable, models = ollama_reachable()
        if not reachable:
            self.skipTest("Ollama 服务未运行，跳过实调用（未验证，不谎报通过）")
        present = {m.split(":")[0] for m in models}
        if "qwen2.5" not in present:
            self.skipTest("qwen2.5:0.5b 未拉取，跳过实调用（未验证）")
        dest = make_live_copy(dead_ollama=False)
        cli_json(["boot"], cwd=dest, timeout=30)
        chat = cli_json(["router-call", "--logical", "local",
                         "--prompt", "只回两个字：你好"], cwd=dest, timeout=180)
        self.assertEqual(chat["model"], "ollama-local")
        self.assertFalse(chat["degraded"])
        self.assertEqual(chat["cost_usd"], 0.0)
        self.assertGreater(chat["latency_ms"], 0)
        if "nomic-embed-text" not in present:
            self.skipTest("nomic-embed-text 未拉取，跳过 embedding 实调用")
        emb = cli_json(["router-call", "--logical", "embedding",
                        "--prompt", "向量测试"], cwd=dest, timeout=120)
        self.assertEqual(emb["model"], "ollama-embedding")
        self.assertEqual(emb["cost_usd"], 0.0)
        usage_path = dest / "data" / "usage.jsonl"
        records = UsageLog(usage_path).read_all()
        self.assertEqual(len(records), 2)
        details = [json.loads(r["detail"]) for r in records]
        self.assertEqual(details[0]["provider"], "ollama-local")
        self.assertGreater(details[0]["tokens_in"], 0)
        self.assertGreater(details[0]["tokens_out"], 0)
        self.assertEqual(details[1]["provider"], "ollama-embedding")
        self.assertGreater(details[1]["tokens_in"], 0)
        self.assertEqual(details[1]["tokens_out"], 0)
        audit = cli_json(["usage-audit"], cwd=dest, timeout=30)
        self.assertTrue(audit["ok"], audit)
        self.assertEqual(audit["total"], 2)

    def test_11_usage_audit_cli_no_log(self):
        dest = make_live_copy()
        cli_json(["boot"], cwd=dest, timeout=30)
        r = run_cli(["usage-audit"], cwd=dest, timeout=30)
        self.assertNotEqual(r.returncode, 0)
        out = json.loads(r.stdout)
        self.assertFalse(out["ok"])
        self.assertEqual(out["issues"], ["log_missing"])


if __name__ == "__main__":
    unittest.main()
