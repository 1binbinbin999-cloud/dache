"""Jarvis P3 — Tool Gateway + Permission/Policy（最小版）

原则（来自总体架构）：
- 工具注册表：每个工具声明副作用等级 read / write / destructive / external。
- 权限检查在工具执行前，不在工具内部：拒绝的调用不执行，只记审计。
- 拒绝的依据按顺序：不在 allowed_tools 里 -> 在 denied_tools 里 -> destructive
  一律拒绝（P3 还没有审批机制）-> external 且 network_calls_allowed 为 false。
  S2-4（JIS-013）：destructive 工具改走批准放行——调用方先经
  tool-approval-request 申请、tool-approval-approve 显式批准，
  再以 approval_id 调 execute；网关核对批准（已批准、工具一致、
  参数哈希一致、未消费过）才执行，一次批准只放行一次调用；
  无批准或批准无效仍拒绝，只记审计。批准记录进 improvements.db
  的 approvals 表（见 improvement.py）。
- 每次调用都记审计：允许的记 tool_call，拒绝的记 tool_denied，执行异常记
  tool_error，含工具名、参数 hash、结果 hash、耗时。参数原文不进审计
  （避免把敏感内容写进日志）。
- 异常分类（P6-7，先定义再使用）：invalid_input = 参数不是 dict 或参数值
  类型非法（调用方传错了参数，工具未执行）；call_error = 工具执行本身
  抛异常。审计写入失败不掩盖工具结果，在返回里以 audit_write_failed 标记。
- 网关只调度，不自己做业务：读任务、记忆、路由分别调 Core / MemoryStore / Router。

不联网，不 import 任何模型供应商 SDK。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

# 工具注册表：副作用等级。注册表在代码里，但“允许哪个”在 config.yaml 的 policy 里。
TOOL_REGISTRY = {
    "list_tasks": {"side_effect": "read", "description": "列出本地任务"},
    "read_task": {"side_effect": "read", "description": "读一条本地任务"},
    "memory-recall": {"side_effect": "read", "description": "召回当前记忆"},
    "memory-add": {"side_effect": "write", "description": "经写时 gate 写入一条记忆"},
    "router-call": {"side_effect": "external", "description": "经 Model Router 调用逻辑模型"},
    "write_file": {"side_effect": "write", "description": "写文件（P3 默认拒绝）"},
    "delete_file": {"side_effect": "destructive", "description": "删文件（P3 一律拒绝）"},
    "send_email": {"side_effect": "external", "description": "发邮件（P3 默认拒绝）"},
    "external_api_call": {"side_effect": "external", "description": "外部 API 调用（P3 默认拒绝）"},
}


def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _args_summary(args) -> dict:
    """审计脱敏摘要（P3 待定项，P4 定案）：只记参数键名，不记值。
    既能事后排查“当时传了哪些参数”，又不把敏感内容写进日志。
    非 dict 参数返回空键名，不抛异常（P6-7：旧代码对 list 调 .keys()
    抛 AttributeError 且无审计，G1）。"""
    if not isinstance(args, dict):
        return {"keys": []}
    return {"keys": sorted(args.keys())}


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

    def _audit_event(self, event: str, result: str, detail: dict) -> bool:
        """写一条审计事件；写入失败返回 False（调用方在结果里标记，
        不抛异常、不掩盖工具结果，P6-7）。"""
        try:
            self.event_log.append(
                event, result, json.dumps(detail, ensure_ascii=False))
            return True
        except Exception:
            return False

    def _dispatch_error(self, tool: str, args: dict, actor: str,
                        start: float, error_class: str, exc: Exception) -> dict:
        """分发异常：记 tool_error 审计，返回 error 结果（不抛异常；
        调用方按 decision=error 处理，见 planner.run 的 P6-7 分支）。"""
        latency_ms = int((time.monotonic() - start) * 1000)
        detail = {"tool": tool, "error_class": error_class,
                  "error": f"{type(exc).__name__}: {exc}",
                  "actor": actor, "args_hash": _hash(args),
                  "args_summary": _args_summary(args),
                  "latency_ms": latency_ms}
        audit_ok = self._audit_event("tool_error", "error", detail)
        out = {"tool": tool, "decision": "error",
               "reason": f"{error_class}:{exc}", "executed": False}
        if not audit_ok:
            out["audit_write_failed"] = True
        return out

    def _approvals_store(self):
        """批准记录库（JIS-013）：懒加载，复用治理库 improvements.db。"""
        from improvement import ImprovementStore
        base_dir = Path(self.core.base_dir)
        return ImprovementStore(base_dir, base_dir / "data" / "improvements.db",
                                self.event_log)

    def execute(self, tool: str, args: dict | None = None, actor: str = "user",
                approval_id: str | None = None) -> dict:
        args = args or {}
        start = time.monotonic()
        # P6-7：args 非 dict 直接 denied 并记 tool_error（invalid_input）。
        # 旧代码在 _args_summary 里对 list 调 .keys() 抛 AttributeError 且无审计（G1）。
        if not isinstance(args, dict):
            detail = {"tool": tool, "error_class": "invalid_input",
                      "reason": "args_not_object", "actor": actor,
                      "latency_ms": int((time.monotonic() - start) * 1000)}
            audit_ok = self._audit_event("tool_error", "error", detail)
            out = {"tool": tool, "decision": "denied",
                   "reason": "args_must_be_object", "executed": False}
            if not audit_ok:
                out["audit_write_failed"] = True
            return out
        # S2-4（JIS-013）：destructive 工具只走批准放行。无批准
        # 或批准无效 -> 拒绝（只记审计，不执行）；批准有效 -> 消费
        # 后执行（一次批准只放行一次调用，且只放行申请时那套参数）。
        approval_record = None
        if tool in TOOL_REGISTRY and TOOL_REGISTRY[tool]["side_effect"] == "destructive":
            if not approval_id:
                latency_ms = int((time.monotonic() - start) * 1000)
                audit_ok = self._audit_event(
                    "tool_denied", "denied",
                    {"tool": tool, "reason": "destructive_requires_approval",
                     "actor": actor, "args_hash": _hash(args),
                     "args_summary": _args_summary(args),
                     "latency_ms": latency_ms})
                out = {"tool": tool, "decision": "denied",
                       "reason": "destructive_requires_approval",
                       "executed": False}
                if not audit_ok:
                    out["audit_write_failed"] = True
                return out
            ok, reason, approval_record = self._approvals_store().consume_tool_approval(
                approval_id, tool, args)
            if not ok:
                latency_ms = int((time.monotonic() - start) * 1000)
                audit_ok = self._audit_event(
                    "tool_denied", "denied",
                    {"tool": tool, "reason": f"invalid_approval:{reason}",
                     "actor": actor, "args_hash": _hash(args),
                     "args_summary": _args_summary(args),
                     "latency_ms": latency_ms})
                out = {"tool": tool, "decision": "denied",
                       "reason": f"invalid_approval:{reason}",
                       "executed": False}
                if not audit_ok:
                    out["audit_write_failed"] = True
                return out
            # 批准有效：跳过 check() 里的 destructive 一律拒绝，
            # 直接分发执行（其余检查——未知工具等——已在上游完成）。
            return self._execute_approved(tool, args, actor, start,
                                          approval_id, approval_record)
        allowed, reason = self.check(tool)
        latency_ms = int((time.monotonic() - start) * 1000)
        if not allowed:
            audit_ok = self._audit_event(
                "tool_denied", "denied",
                {"tool": tool, "reason": reason, "actor": actor,
                 "args_hash": _hash(args),
                 "args_summary": _args_summary(args),
                 "latency_ms": latency_ms})
            out = {"tool": tool, "decision": "denied", "reason": reason,
                   "executed": False}
            if not audit_ok:
                out["audit_write_failed"] = True
            return out
        try:
            result = self._dispatch(tool, args, actor)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            # 输入非法类异常：参数值类型错误（如 confidence="abc" 的 float 转换，G1）
            return self._dispatch_error(tool, args, actor, start,
                                        "invalid_input", exc)
        except Exception as exc:
            # 调用异常：工具执行本身失败
            return self._dispatch_error(tool, args, actor, start,
                                        "call_error", exc)
        latency_ms = int((time.monotonic() - start) * 1000)
        audit_ok = self._audit_event(
            "tool_call", "ok",
            {"tool": tool, "actor": actor, "args_hash": _hash(args),
             "args_summary": _args_summary(args),
             "result_hash": _hash(result), "latency_ms": latency_ms})
        out = {"tool": tool, "decision": "allowed", "executed": True, "result": result}
        if not audit_ok:
            out["audit_write_failed"] = True
        return out

    def _execute_approved(self, tool: str, args: dict, actor: str,
                          start: float, approval_id: str,
                          approval_record: dict | None) -> dict:
        """已批准的 destructive 调用：分发执行，审计里记批准 id 与操作者。"""
        try:
            result = self._dispatch(tool, args, actor)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            return self._dispatch_error(tool, args, actor, start,
                                        "invalid_input", exc)
        except Exception as exc:
            return self._dispatch_error(tool, args, actor, start,
                                        "call_error", exc)
        latency_ms = int((time.monotonic() - start) * 1000)
        detail = {"tool": tool, "actor": actor, "args_hash": _hash(args),
                  "args_summary": _args_summary(args),
                  "result_hash": _hash(result), "latency_ms": latency_ms,
                  "approval_id": approval_id}
        if approval_record and approval_record.get("operator"):
            detail["approved_by"] = approval_record["operator"]
        audit_ok = self._audit_event("tool_call", "ok", detail)
        out = {"tool": tool, "decision": "allowed", "executed": True,
               "result": result, "approval_id": approval_id}
        if not audit_ok:
            out["audit_write_failed"] = True
        return out

    def _dispatch(self, tool: str, args: dict, actor: str = "user") -> dict:
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
            # P6-11：证据等级不得由调用方自报。非用户 actor（规划器等）
            # 一律强制 evidence_grade=single_occurrence；经 add_fact 的
            # 状态规则，状态上限自动为 provisional，不得自授 verified。
            if actor != "user":
                args = dict(args)
                args["evidence_grade"] = "single_occurrence"
            return self.memory.add_fact(
                args.get("text", ""), args.get("type", "fact"),
                float(args.get("confidence", 0.6)),
                args.get("evidence_grade", "single_occurrence"),
                args.get("source", "gateway"),
                valid_until=args.get("valid_until"),
                fact_id=args.get("id"),
            )
        if tool == "router-call":
            from router import Router
            router = Router(self.config, self.event_log)
            return router.complete(
                args.get("logical", "fast"), args.get("prompt", ""),
                fail_providers=tuple(args.get("fail", []) or []),
            )
        if tool == "delete_file":
            # S2-4（JIS-013）：只有持有有效批准的调用能走到这里
            # （execute 里的批准核对已通过）。路径必须落在工程目录内，
            # 只删文件不删目录；不存在、越界、目录一律报 ValueError
            # （调用方按 decision=error 处理，不静默成功）。
            raw_path = str(args.get("path", "") or "")
            if not raw_path:
                raise ValueError("delete_file requires path")
            base = Path(self.core.base_dir).resolve()
            target = (base / raw_path).resolve() if not Path(raw_path).is_absolute() \
                else Path(raw_path).resolve()
            if target == base or base not in target.parents:
                raise ValueError("delete path outside base dir")
            if not target.exists():
                raise ValueError(f"delete target not found: {raw_path}")
            if not target.is_file():
                raise ValueError("delete target is not a file")
            target.unlink()
            return {"deleted": str(target)}
        raise ValueError(f"no handler for allowed tool: {tool}")

    def list_tools(self) -> list[dict]:
        return [
            {"name": name, "side_effect": info["side_effect"],
             "description": info["description"],
             "allowed": self.check(name)[0]}
            for name, info in TOOL_REGISTRY.items()
        ]
