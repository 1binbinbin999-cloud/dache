"""Jarvis P4 — Planner（最小版，确定性任务分解）

原则（来自总体架构）：
- 任务 = 目标 + 有序步骤，每步绑定一个已注册工具 + 参数。
- P4 不用 LLM 做分解：步骤由用户（或模板）给定，Planner 只负责校验、
  按顺序执行、追踪状态。分解质量的模型化留给 P5 之后。
- 步骤全部经 Tool Gateway 执行，actor="planner"，不绕过网关。
- 失败一步即停：denied / failed 的步骤之后不再执行，任务状态如实记为
  denied / failed，不谎报完成。
- 任务可重跑：重跑时重置步骤状态，从第一步重新执行；确定性步骤
  （Stub 回答、只读查询、带固定 id 的记忆写入）重跑结果一致。
- 任务与步骤存在 SQLite 里，与记忆库同库不同表，导出时一起走。

不联网，不 import 任何模型供应商 SDK。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from gateway import TOOL_REGISTRY

# 确定性模板：步骤固定，不用 LLM 生成。
TEMPLATES = {
    "morning-brief": [
        {"tool": "memory-recall", "args": {}},
        {"tool": "router-call", "args": {"logical": "fast", "prompt": "早上好，今天有什么值得注意的事？"}},
        {"tool": "list_tasks", "args": {}},
    ],
    "learn-and-record": [
        {"tool": "memory-recall", "args": {}},
        {"tool": "router-call", "args": {"logical": "reasoning", "prompt": "解释一个概念"}},
        # P6-11：模板是规划器 actor，不是用户本人，不得自报 user_stated 0.9
        #（旧模板写死 0.9/user_stated，跑完即 verified，M-e）。模板只报
        # 单次证据 0.6，网关对非用户 actor 还会再强制降级，状态上限 provisional。
        {"tool": "memory-add", "args": {"text": "用户目标是学会跑步", "type": "fact",
                                        "confidence": 0.6, "evidence_grade": "single_occurrence",
                                        "source": "planner:learn-and-record",
                                        "id": "mem-plan-learn-001"}},
    ],
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Planner:
    def __init__(self, db_path, gateway):
        self.db_path = db_path
        self.gateway = gateway

    def init(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS plans ("
                " id TEXT PRIMARY KEY, goal TEXT, status TEXT,"
                " created_at TEXT, updated_at TEXT)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS plan_steps ("
                " id TEXT PRIMARY KEY, plan_id TEXT, seq INTEGER,"
                " tool TEXT, args_json TEXT, status TEXT,"
                " result_hash TEXT, created_at TEXT)"
            )
            conn.commit()

    def create(self, goal: str, steps: list[dict], plan_id: str | None = None) -> dict:
        """建任务：校验每步的工具都在注册表里，否则拒绝建任务。"""
        self.init()
        if not goal.strip():
            return {"decision": "rejected", "reason": "empty_goal"}
        if not steps:
            return {"decision": "rejected", "reason": "empty_steps"}
        for step in steps:
            if step.get("tool") not in TOOL_REGISTRY:
                return {"decision": "rejected",
                        "reason": f"unknown_tool:{step.get('tool')}"}
            if not isinstance(step.get("args", {}), dict):
                return {"decision": "rejected", "reason": "args_must_be_object"}
        plan_id = plan_id or f"plan-{uuid.uuid4().hex[:12]}"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO plans (id, goal, status, created_at, updated_at)"
                " VALUES (?, ?, 'pending', ?, ?)",
                (plan_id, goal, now_iso(), now_iso()),
            )
            for seq, step in enumerate(steps):
                conn.execute(
                    "INSERT INTO plan_steps (id, plan_id, seq, tool, args_json,"
                    " status, result_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (f"{plan_id}-step-{seq+1}", plan_id, seq + 1,
                     step["tool"], json.dumps(step.get("args", {}), ensure_ascii=False),
                     "pending", None, now_iso()),
                )
            conn.commit()
        return {"decision": "created", "id": plan_id, "steps": len(steps)}

    def _get_steps(self, plan_id: str) -> list[dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM plan_steps WHERE plan_id = ? ORDER BY seq",
                (plan_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def status(self, plan_id: str) -> dict | None:
        self.init()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            plan = conn.execute(
                "SELECT * FROM plans WHERE id = ?", (plan_id,)
            ).fetchone()
            if plan is None:
                return None
            plan = dict(plan)
        plan["steps"] = self._get_steps(plan_id)
        return plan

    def list_plans(self) -> list[dict]:
        self.init()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM plans ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    def run(self, plan_id: str) -> dict:
        """按顺序执行：失败一步即停；重跑时重置步骤状态。"""
        self.init()
        plan = self.status(plan_id)
        if plan is None:
            return {"decision": "not_found", "id": plan_id}
        steps = plan["steps"]
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE plan_steps SET status='pending', result_hash=NULL"
                         " WHERE plan_id = ?", (plan_id,))
            conn.execute("UPDATE plans SET status='running', updated_at=?"
                         " WHERE id = ?", (now_iso(), plan_id))
            conn.commit()
        final_status = "done"
        for step in steps:
            step_id = step["id"]
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("UPDATE plan_steps SET status='running' WHERE id = ?",
                             (step_id,))
                conn.commit()
            args = json.loads(step["args_json"])
            try:
                result = self.gateway.execute(step["tool"], args, actor="planner")
            except Exception as exc:  # 网关之外的异常也记为 failed，不继续
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute("UPDATE plan_steps SET status='failed' WHERE id = ?",
                                 (step_id,))
                    conn.execute("UPDATE plans SET status='failed', updated_at=?"
                                 " WHERE id = ?", (now_iso(), plan_id))
                    conn.commit()
                return {"decision": "failed", "id": plan_id, "failed_step": step_id,
                        "error": str(exc)}
            if result["decision"] == "denied":
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute("UPDATE plan_steps SET status='denied' WHERE id = ?",
                                 (step_id,))
                    conn.execute("UPDATE plans SET status='denied', updated_at=?"
                                 " WHERE id = ?", (now_iso(), plan_id))
                    conn.commit()
                return {"decision": "denied", "id": plan_id, "denied_step": step_id,
                        "reason": result.get("reason")}
            if result["decision"] == "error":
                # P6-7：网关执行异常（已记 tool_error 审计）按失败处理，
                # 不再当成 done（旧代码只有 denied 分支，error 会被误标 done）。
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute("UPDATE plan_steps SET status='failed' WHERE id = ?",
                                 (step_id,))
                    conn.execute("UPDATE plans SET status='failed', updated_at=?"
                                 " WHERE id = ?", (now_iso(), plan_id))
                    conn.commit()
                return {"decision": "failed", "id": plan_id, "failed_step": step_id,
                        "error": result.get("reason")}
            # 执行成功：记结果 hash；记忆写入被 gate 隔离时，步骤仍算 done
            #（网关放行了，隔离是记忆层的事，见 P3 测试 5），但在结果里标出来
            from gateway import _hash
            result_hash = _hash(result.get("result", {}))
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("UPDATE plan_steps SET status='done', result_hash=?"
                             " WHERE id = ?", (result_hash, step_id))
                conn.commit()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE plans SET status='done', updated_at=?"
                         " WHERE id = ?", (now_iso(), plan_id))
            conn.commit()
        return {"decision": final_status, "id": plan_id, "steps": len(steps)}
