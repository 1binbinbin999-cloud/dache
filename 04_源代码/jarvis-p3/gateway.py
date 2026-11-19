"""Jarvis P3 — Tool Gateway + Permission/Policy（最小版）

原则（来自总体架构）：
- 工具注册表：每个工具声明副作用等级 read / write / destructive / external。
- 权限检查在工具执行前，不在工具内部：拒绝的调用不执行，只记审计。
- 拒绝的依据按顺序：不在 allowed_tools 里 -> 在 denied_tools 里 -> destructive
  一律拒绝（P3 还没有审批机制）-> external 且 network_calls_allowed 为 false。
- 每次调用都记审计：允许的记 tool_call，拒绝的记 tool_denied，含工具名、
  参数 hash、结果 hash、耗时。参数原文不进审计（避免把敏感内容写进日志）。
- 网关只调度，不自己做业务：读任务、记忆、路由分别调 Core / MemoryStore / Router。

不联网，不 import 任何模型供应商 SDK。
"""

from __future__ import annotations

import hashlib
import json
import time

# 工具注册表：副作用等级。注册表在代码里，但“允许哪个”在 config.yaml 的 policy 里。
TOOL_REGISTRY = {
    "list_tasks": {"side_effect": "read", "description": "列出本地任务"},
    "read_task": {"side_effect": "read", "description": "读一条本地任务"},
    "memory-recall": {"side_effect": "read", "description": "召回当前记忆"},
    "memory-add": {"side_effect": "write", "description": "经写时 gate 写入一条记忆"},
    "router-call": {"side_effect": "external", "description": "经 Model Router 调用逻辑模型（P3 为 Stub）"},
    "write_file": {"side_effect": "write", "description": "写文件（P3 默认拒绝）"},
    "delete_file": {"side_effect": "destructive", "description": "删文件（P3 一律拒绝）"},
    "send_email": {"side_effect": "external", "description": "发邮件（P3 默认拒绝）"},
    "external_api_call": {"side_effect": "external", "description": "外部 API 调用（P3 默认拒绝）"},
}


def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


class Gateway:
    def __init__(self, config: dict, core, memory, event_log):
        self.config = config
        self.core = core
        self.memory = memory
        self.event_log = event_log
        policy = config.get("policy", {}) or {}
        self.allowed = set(policy.get("allowed_tools", []))
        self.denied = set(policy.get("denied_tools", []))
        self.network_allowed = policy.get("network_calls_allowed") is True

    def check(self, tool: str) -> tuple[bool, str]:
        """权限检查：返回 (允许, 原因)。不执行任何工具。"""
        if tool not in TOOL_REGISTRY:
            return False, f"unknown_tool:{tool}"
        if tool in self.denied:
            return False, "denied_by_policy"
        if tool not in self.allowed:
            return False, "not_in_allowed_tools"
        side_effect = TOOL_REGISTRY[tool]["side_effect"]
        if side_effect == "destructive":
            return False, "destructive_always_denied"
        if side_effect == "external" and not self.network_allowed:
            # 例外：router-call 在 Stub 模式下不产生真实外部调用，config 里 router_mode=stub 时放行
            if tool == "router-call" and self.config.get("model", {}).get("router_mode") == "stub":
                return True, "ok_stub_no_real_network"
            return False, "external_requires_network"
        return True, "ok"

    def execute(self, tool: str, args: dict | None = None, actor: str = "user") -> dict:
        args = args or {}
        start = time.monotonic()
        allowed, reason = self.check(tool)
        latency_ms = int((time.monotonic() - start) * 1000)
        if not allowed:
            self.event_log.append(
                "tool_denied", "denied",
                json.dumps({"tool": tool, "reason": reason, "actor": actor,
                            "args_hash": _hash(args), "latency_ms": latency_ms},
                           ensure_ascii=False),
            )
            return {"tool": tool, "decision": "denied", "reason": reason,
                    "executed": False}
        result = self._dispatch(tool, args)
        latency_ms = int((time.monotonic() - start) * 1000)
        self.event_log.append(
            "tool_call", "ok",
            json.dumps({"tool": tool, "actor": actor, "args_hash": _hash(args),
                        "result_hash": _hash(result), "latency_ms": latency_ms},
                       ensure_ascii=False),
        )
        return {"tool": tool, "decision": "allowed", "executed": True, "result": result}

    def _dispatch(self, tool: str, args: dict) -> dict:
        if tool == "list_tasks":
            self.core.store.init()
            return {"tasks": self.core.store.list_tasks()}
        if tool == "read_task":
            self.core.store.init()
            task = self.core.store.get_task(args.get("id", ""))
            return {"task": task}
        if tool == "memory-recall":
            return {"facts": self.memory.list_current(args.get("query"))}
        if tool == "memory-add":
            return self.memory.add_fact(
                args.get("text", ""), args.get("type", "fact"),
                float(args.get("confidence", 0.6)),
                args.get("evidence_grade", "single_occurrence"),
                args.get("source", "gateway"),
                valid_until=args.get("valid_until"),
            )
        if tool == "router-call":
            from router import Router
            router = Router(self.config, self.event_log)
            return router.complete(
                args.get("logical", "fast"), args.get("prompt", ""),
                fail_providers=tuple(args.get("fail", []) or []),
            )
        raise ValueError(f"no handler for allowed tool: {tool}")

    def list_tools(self) -> list[dict]:
        return [
            {"name": name, "side_effect": info["side_effect"],
             "description": info["description"],
             "allowed": self.check(name)[0]}
            for name, info in TOOL_REGISTRY.items()
        ]
