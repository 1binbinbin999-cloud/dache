"""S2-3 测试：真实失败演练——拔 Key、断代理、超时与切换行为。

全部 $0，不发真实 Gemini 请求。真实机制与模拟后端分开标：
- 真实机制：缺 Key（适配器真实路径）、连接拒绝（真实 socket
  失败）、真实 socket 超时（桩服务只负责“慢”，超时由真实
  urllib 超时触发）、代理断开（真实代理连接失败）、Ollama
  服务停掉（手动演练，见 S2-3 记录；套件内用死端口等价测
  连接拒绝路径）。
- 模拟后端：桩服务返回的 401 / 403 / 429 / 500（Gemini 官方
  错误形状）；Google 真实返回的 401/403/5xx 正文从未观测到，
  见 S2-3 记录诚实清单。

测试 1：缺 GEMINI_API_KEY → missing_key，不静默降级，按链回落
       到 stub-local（Ollama 死端口时），attempted 全列。
测试 2：缺 Key → 回落到真实 Ollama（仅当服务可达；否则 skip）。
测试 3：假 Key + 桩服务 401 → auth_error，只发 1 次请求（4xx
       不重试），回落。
测试 4：假 Key + 桩服务 403 → auth_error，只发 1 次请求，回落。
测试 5：桩服务 500 → server_error，重试 1 次（共 2 次请求），
       耗尽后回落。
测试 6：桩服务 429 一次后 200 → 重试 1 次成功，model 仍为
       gemini-flash。
测试 7：桩服务一直 429 → rate_limited，重试耗尽（共 2 次），回落。
测试 8：重试退避是真实等待（backoff 2s，实测耗时 ≥1.9s）。
测试 9：超时机制——桩服务慢响应 + timeout_s=2 → 真实 socket
       超时，只发 1 次请求（超时不重试），回落。
测试 10：连接拒绝（闭端口）→ unavailable，快速失败，回落。
测试 11：Ollama 死端口 → unavailable，logical=local 回落到
        stub-local。
测试 12：probe_all 在缺 Key 时报 gemini missing_key，不发请求。
测试 13：CLI 端到端——临时 live 副本，缺 Key + Ollama 死端口，
        router-call --logical fast 走到 stub-local，费用 $0。
测试 14：CLI 端到端——临时 live 副本，假 Key + 代理指向死端口
        （断代理），router-call 不挂不崩，走到底。
测试 15：失败尝试不写 usage 日志；无日志时审计报 log_missing
       （不是空口通过）。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

S2_DIR = Path(__file__).resolve().parent.parent
PY = sys.executable

sys.path.insert(0, str(S2_DIR))
from router import Router  # noqa: E402
from usage import UsageLog, audit_usage_log  # noqa: E402

FAKE_KEY = "test-key-not-real"
DEAD_PORT_URL = "http://127.0.0.1:9"


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


def cli_json(args, cwd=S2_DIR, env=None, timeout=60):
    r = run_cli(args, cwd=cwd, env=env, timeout=timeout)
    assert r.returncode == 0, f"{args} failed: {r.stderr}"
    return json.loads(r.stdout)


class FakeLog:
    """内存事件日志：只记 append 的调用，供 in-process Router 测试用."""

    def __init__(self):
        self.events: list[dict] = []

    def append(self, event: str, result: str, detail: str = "") -> dict:
        rec = {"event": event, "result": result, "detail": detail}
        self.events.append(rec)
        return rec


def fallback_classes(log: FakeLog) -> list[str]:
    out = []
    for ev in log.events:
        if ev["event"] == "router_fallback":
            detail = json.loads(ev["detail"])
            out.append(detail.get("error_class", ""))
    return out


def fallback_latencies(log: FakeLog) -> list[int]:
    out = []
    for ev in log.events:
        if ev["event"] == "router_fallback":
            detail = json.loads(ev["detail"])
            out.append(int(detail.get("latency_ms", -1)))
    return out


def live_config(ollama_host: str, gemini_base_url: str | None = None,
                gemini_timeout: float = 10, gemini_retry: int = 1,
                gemini_backoff: float = 0, ollama_timeout: float = 5) -> dict:
    gemini_cfg: dict = {"type": "gemini", "model": "gemini-3.6-flash",
                        "timeout_s": gemini_timeout, "retry_max": gemini_retry,
                        "retry_backoff_s": gemini_backoff}
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
                                 "host": ollama_host,
                                 "timeout_s": ollama_timeout,
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


def make_live_copy(dead_ollama: bool = True) -> Path:
    """复制 s2-3 到临时目录并改成 live 配置（CLI 端到端测试用）。"""
    tmp = Path(tempfile.mkdtemp())
    dest = tmp / "jarvis-s2-3-live"
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


def start_server(handler_cls) -> tuple[ThreadingHTTPServer, int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def gemini_error_handler(status: int, body: dict, calls: dict,
                         sleep_s: float = 0.0):
    """生成一个固定返回 Gemini 形状错误 JSON 的 handler 类。"""
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls["n"] += 1
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            if sleep_s:
                time.sleep(sleep_s)
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if status == 429:
                self.send_header("Retry-After", "2")
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    return Handler


def gemini_success_handler(calls: dict):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls["n"] += 1
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
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

    return Handler


class TestS23(unittest.TestCase):
    def setUp(self):
        self._saved_key = os.environ.pop("GEMINI_API_KEY", None)
        self._tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        if self._saved_key is not None:
            os.environ["GEMINI_API_KEY"] = self._saved_key
        else:
            os.environ.pop("GEMINI_API_KEY", None)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_1_missing_key_falls_back_to_stub(self):
        log = FakeLog()
        router = Router(live_config(DEAD_PORT_URL), log)
        r = router.complete("fast", "你好")
        self.assertEqual(r["model"], "stub-local")
        self.assertFalse(r["degraded"])
        self.assertEqual(r["attempted"],
                         ["gemini-flash", "ollama-local", "stub-local"])
        self.assertEqual(fallback_classes(log), ["missing_key", "unavailable"])
        self.assertEqual(r["cost_usd"], 0.0)
        # 缺 Key 在发请求前就报错：gemini 尝试耗时应 < 1s。
        self.assertLess(fallback_latencies(log)[0], 1000)

    def test_2_missing_key_falls_back_to_real_ollama(self):
        reachable, models = ollama_reachable()
        if not reachable:
            self.skipTest("Ollama 服务未运行，跳过实调用（未验证，不谎报通过）")
        present = {m.split(":")[0] for m in models}
        if "qwen2.5" not in present:
            self.skipTest("qwen2.5:0.5b 未拉取，跳过实调用（未验证）")
        log = FakeLog()
        router = Router(live_config("http://localhost:11434"), log)
        r = router.complete("fast", "只回两个字：你好")
        self.assertEqual(r["model"], "ollama-local")
        self.assertFalse(r["degraded"])
        self.assertEqual(r["attempted"], ["gemini-flash", "ollama-local"])
        self.assertEqual(fallback_classes(log), ["missing_key"])
        self.assertEqual(r["cost_usd"], 0.0)

    def test_3_fake_key_401_no_retry(self):
        calls = {"n": 0}
        handler = gemini_error_handler(401, {
            "error": {"code": 401,
                      "message": "API key not valid. Please pass a valid API key.",
                      "status": "UNAUTHENTICATED"}}, calls)
        server, port = start_server(handler)
        try:
            os.environ["GEMINI_API_KEY"] = FAKE_KEY
            log = FakeLog()
            router = Router(live_config(
                DEAD_PORT_URL,
                gemini_base_url=f"http://127.0.0.1:{port}"), log)
            r = router.complete("fast", "你好")
            self.assertEqual(calls["n"], 1)  # 4xx 不重试
            self.assertEqual(r["model"], "stub-local")
            self.assertEqual(fallback_classes(log),
                             ["auth_error", "unavailable"])
        finally:
            server.shutdown()

    def test_4_fake_key_403_no_retry(self):
        calls = {"n": 0}
        handler = gemini_error_handler(403, {
            "error": {"code": 403,
                      "message": "The caller does not have permission.",
                      "status": "PERMISSION_DENIED"}}, calls)
        server, port = start_server(handler)
        try:
            os.environ["GEMINI_API_KEY"] = FAKE_KEY
            log = FakeLog()
            router = Router(live_config(
                DEAD_PORT_URL,
                gemini_base_url=f"http://127.0.0.1:{port}"), log)
            r = router.complete("fast", "你好")
            self.assertEqual(calls["n"], 1)  # 4xx 不重试
            self.assertEqual(r["model"], "stub-local")
            self.assertEqual(fallback_classes(log),
                             ["auth_error", "unavailable"])
        finally:
            server.shutdown()

    def test_5_server_error_500_retries_once(self):
        calls = {"n": 0}
        handler = gemini_error_handler(500, {
            "error": {"code": 500, "message": "Internal error encountered.",
                      "status": "INTERNAL"}}, calls)
        server, port = start_server(handler)
        try:
            os.environ["GEMINI_API_KEY"] = FAKE_KEY
            log = FakeLog()
            router = Router(live_config(
                DEAD_PORT_URL,
                gemini_base_url=f"http://127.0.0.1:{port}",
                gemini_retry=1, gemini_backoff=0), log)
            r = router.complete("fast", "你好")
            self.assertEqual(calls["n"], 2)  # 首次 + 重试 1 次
            self.assertEqual(r["model"], "stub-local")
            self.assertEqual(fallback_classes(log),
                             ["server_error", "unavailable"])
        finally:
            server.shutdown()

    def test_6_429_retry_then_success(self):
        calls = {"n": 0}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                calls["n"] += 1
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                if calls["n"] == 1:
                    raw = json.dumps({"error": {
                        "code": 429,
                        "message": "Resource has been exhausted.",
                        "status": "RESOURCE_EXHAUSTED"}}).encode()
                    self.send_response(429)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Retry-After", "2")
                    self.end_headers()
                    self.wfile.write(raw)
                    return
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

        server, port = start_server(Handler)
        try:
            os.environ["GEMINI_API_KEY"] = FAKE_KEY
            log = FakeLog()
            router = Router(live_config(
                DEAD_PORT_URL,
                gemini_base_url=f"http://127.0.0.1:{port}",
                gemini_retry=1, gemini_backoff=0), log)
            r = router.complete("fast", "你好")
            self.assertEqual(calls["n"], 2)
            self.assertEqual(r["model"], "gemini-flash")
            self.assertFalse(r["degraded"])
            self.assertEqual(r["tokens_in"], 7)
            self.assertEqual(r["tokens_out"], 4)
            self.assertEqual(fallback_classes(log), [])
        finally:
            server.shutdown()

    def test_7_429_always_exhausts(self):
        calls = {"n": 0}
        handler = gemini_error_handler(429, {
            "error": {"code": 429,
                      "message": "Resource has been exhausted.",
                      "status": "RESOURCE_EXHAUSTED"}}, calls)
        server, port = start_server(handler)
        try:
            os.environ["GEMINI_API_KEY"] = FAKE_KEY
            log = FakeLog()
            router = Router(live_config(
                DEAD_PORT_URL,
                gemini_base_url=f"http://127.0.0.1:{port}",
                gemini_retry=1, gemini_backoff=0), log)
            r = router.complete("fast", "你好")
            self.assertEqual(calls["n"], 2)  # 首次 + 重试 1 次
            self.assertEqual(r["model"], "stub-local")
            self.assertEqual(fallback_classes(log),
                             ["rate_limited", "unavailable"])
        finally:
            server.shutdown()

    def test_8_retry_backoff_is_real_wait(self):
        calls = {"n": 0}
        handler = gemini_error_handler(429, {
            "error": {"code": 429,
                      "message": "Resource has been exhausted.",
                      "status": "RESOURCE_EXHAUSTED"}}, calls)
        server, port = start_server(handler)
        try:
            os.environ["GEMINI_API_KEY"] = FAKE_KEY
            log = FakeLog()
            router = Router(live_config(
                DEAD_PORT_URL,
                gemini_base_url=f"http://127.0.0.1:{port}",
                gemini_retry=1, gemini_backoff=2), log)
            start = time.monotonic()
            r = router.complete("fast", "你好")
            elapsed = time.monotonic() - start
            self.assertEqual(calls["n"], 2)
            self.assertEqual(r["model"], "stub-local")
            # backoff 2s * 1 次：总耗时必须 ≥1.9s，证明退避不是跳过的。
            self.assertGreaterEqual(elapsed, 1.9)
        finally:
            server.shutdown()

    def test_9_timeout_mechanism_short(self):
        calls = {"n": 0}
        handler = gemini_error_handler(200, {
            "candidates": [{"content": {"parts": [
                {"text": "too late"}]}}],
            "usageMetadata": {"promptTokenCount": 1,
                              "candidatesTokenCount": 1,
                              "totalTokenCount": 2}}, calls, sleep_s=5)
        server, port = start_server(handler)
        try:
            os.environ["GEMINI_API_KEY"] = FAKE_KEY
            log = FakeLog()
            router = Router(live_config(
                DEAD_PORT_URL,
                gemini_base_url=f"http://127.0.0.1:{port}",
                gemini_timeout=2, gemini_retry=1, gemini_backoff=0), log)
            start = time.monotonic()
            r = router.complete("fast", "你好")
            elapsed = time.monotonic() - start
            self.assertEqual(calls["n"], 1)  # 超时不重试
            self.assertEqual(r["model"], "stub-local")
            self.assertEqual(fallback_classes(log),
                             ["timeout", "unavailable"])
            # 真实 socket 超时约 2s（不是立即失败，也不是等满 5s）。
            self.assertGreaterEqual(elapsed, 1.8)
            self.assertLess(elapsed, 4.5)
        finally:
            server.shutdown()

    def test_10_connection_refused_is_unavailable(self):
        log = FakeLog()
        router = Router(live_config(
            DEAD_PORT_URL,
            gemini_base_url=DEAD_PORT_URL,
            gemini_timeout=5, gemini_retry=1), log)
        os.environ["GEMINI_API_KEY"] = FAKE_KEY
        start = time.monotonic()
        r = router.complete("fast", "你好")
        elapsed = time.monotonic() - start
        self.assertEqual(r["model"], "stub-local")
        self.assertEqual(fallback_classes(log),
                         ["unavailable", "unavailable"])
        # 连接拒绝是快速失败，不应等到超时。
        self.assertLess(elapsed, 2.0)

    def test_11_ollama_dead_port_local_falls_to_stub(self):
        log = FakeLog()
        router = Router(live_config(DEAD_PORT_URL), log)
        r = router.complete("local", "你好")
        self.assertEqual(r["model"], "stub-local")
        self.assertFalse(r["degraded"])
        self.assertEqual(r["attempted"], ["ollama-local", "stub-local"])
        self.assertEqual(fallback_classes(log), ["unavailable"])
        self.assertEqual(r["cost_usd"], 0.0)

    def test_12_probe_reports_missing_key(self):
        log = FakeLog()
        router = Router(live_config(DEAD_PORT_URL), log)
        probes = router.probe_all()
        self.assertEqual(probes["gemini-flash"]["ok"], False)
        self.assertEqual(probes["gemini-flash"]["reason"], "missing_key")

    def test_13_cli_missing_key_dead_ollama(self):
        dest = make_live_copy(dead_ollama=True)
        cli_json(["boot"], cwd=dest, timeout=30)
        r = cli_json(["router-call", "--logical", "fast",
                      "--prompt", "你好"], cwd=dest, timeout=60)
        self.assertEqual(r["model"], "stub-local")
        self.assertFalse(r["degraded"])
        self.assertEqual(r["attempted"],
                         ["gemini-flash", "ollama-local", "stub-local"])
        self.assertEqual(r["cost_usd"], 0.0)

    def test_14_cli_bogus_proxy_survives(self):
        dest = make_live_copy(dead_ollama=True)
        cli_json(["boot"], cwd=dest, timeout=30)
        env = {"GEMINI_API_KEY": FAKE_KEY,
               "http_proxy": DEAD_PORT_URL,
               "https_proxy": DEAD_PORT_URL,
               "HTTP_PROXY": DEAD_PORT_URL,
               "HTTPS_PROXY": DEAD_PORT_URL}
        r = cli_json(["router-call", "--logical", "fast",
                      "--prompt", "你好"], cwd=dest, env=env, timeout=60)
        # 代理断开：gemini 尝试失败，链路走到底，不挂不崩。
        self.assertEqual(r["model"], "stub-local")
        self.assertFalse(r["degraded"])
        self.assertEqual(r["attempted"],
                         ["gemini-flash", "ollama-local", "stub-local"])
        self.assertEqual(r["cost_usd"], 0.0)

    def test_15_failed_attempts_write_no_usage(self):
        usage_path = self._tmp / "usage.jsonl"
        usage_log = UsageLog(usage_path)
        log = FakeLog()
        router = Router(live_config(DEAD_PORT_URL), log,
                        usage_log=usage_log)
        r = router.complete("fast", "你好")
        self.assertEqual(r["model"], "stub-local")
        # 失败尝试不产生 usage 记录：文件根本没被创建。
        self.assertEqual(usage_log.read_all(), [])
        self.assertFalse(usage_path.exists())
        # 无日志时审计必须报 log_missing，而不是空口通过。
        result = audit_usage_log(usage_path)
        self.assertFalse(result["ok"])
        self.assertEqual(result["issues"], ["log_missing"])
        self.assertEqual(result["total"], 0)


if __name__ == "__main__":
    unittest.main()
