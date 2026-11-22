#!/usr/bin/env python3
"""Jarvis 聊天服务：把 S2-2 的 Router（live 模式）包成 HTTP 接口，供聊天窗口调用。

只用标准库 + PyYAML（S2-2 本身就用它读配置），不新增依赖。
Key 只从环境变量读（GEMINI_API_KEY），绝不写进代码、配置或日志。
诚实原则：只返回真实模型的回复；若回落到 Stub 或降级，一律报 503，
不拿假回复冒充。聊天只走本地 Ollama（logical=local），不回落云端。

用法：
  python3 server.py            # 监听 127.0.0.1:8000
  curl -s -X POST localhost:8000/chat -H 'Content-Type: application/json' \
    -d '{"message": "你好"}'
"""

from __future__ import annotations

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
S2_2_DIR = HERE.parent / "jarvis-s2-2"
sys.path.insert(0, str(S2_2_DIR))

import yaml  # noqa: E402

from router import Router  # noqa: E402

HOST = "127.0.0.1"
PORT = 8000
DATA_DIR = HERE / "data"


class JsonlEventLog:
    """最小追加写事件日志（Router 审计用），与 S2-2 的事件日志同形。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event_type: str, status: str, detail: str) -> None:
        rec = {"ts": time.time(), "event": event_type,
               "status": status, "detail": detail}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def build_router() -> Router:
    """读 S2-2 配置，强制 live 模式，建 Router（logical=local 走 Ollama）。"""
    cfg_path = S2_2_DIR / "config.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["model"]["router_mode"] = "live"
    event_log = JsonlEventLog(DATA_DIR / "events.jsonl")
    return Router(cfg, event_log, usage_log=None)


ROUTER = build_router()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 不打访问日志到 stdout
        pass

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            probe = ROUTER.probe_all()
            ollama = probe.get("ollama-local", {})
            self._send(200, {
                "ok": True,
                "router_mode": ROUTER.router_mode,
                "ollama_local": ollama,
                "gemini_key_set": bool(os.environ.get("GEMINI_API_KEY")),
            })
            return
        self._send(404, {"error": "not_found"})

    def do_POST(self):
        if self.path != "/chat":
            self._send(404, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8")
            data = json.loads(raw) if raw else {}
        except (ValueError, UnicodeDecodeError):
            self._send(400, {"error": "bad_json"})
            return
        message = str(data.get("message") or "").strip()
        if not message:
            self._send(400, {"error": "empty_message"})
            return
        try:
            result = ROUTER.complete("local", message)
        except Exception as exc:  # Router 本身不抛，但兜底
            self._send(500, {"error": "router_error", "detail": str(exc)})
            return
        model = str(result.get("model") or "")
        # 诚实关：Stub 假回复与降级模板一律不返回，报 503。
        if result.get("degraded") or "stub" in model:
            self._send(503, {
                "error": "model_unavailable",
                "model": model,
                "degraded": bool(result.get("degraded")),
                "detail": "Ollama 本地模型不可用，未返回假回复",
            })
            return
        self._send(200, {
            "reply": str(result.get("content") or ""),
            "model": model,
            "tokens_in": int(result.get("tokens_in") or 0),
            "tokens_out": int(result.get("tokens_out") or 0),
            "latency_ms": int(result.get("latency_ms") or 0),
            "cost_usd": 0.0,
        })


def main() -> int:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"jarvis-chat listening on http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
