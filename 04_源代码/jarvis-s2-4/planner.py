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
- S2-4（JIS-017）：失败任务可从失败步骤续跑（retry / skip /
  retry-args），已 done 的步骤不重跑，续跑记 plan_resumed 审计。
- S2-4（JIS-018）：每步记忆写入的 gate 判定记进 gate_result，
  任务报告含 gate 汇总；被隔离的步骤不被记为“已写入”。
- S2-4（JIS-016）：decompose() 用真实模型（经 Router）把自然语言
  目标拆成步骤，解析校验后返回；拆分质量须人工评价，本模块不自评。
- S2-4（JIS-013）：步骤可带 approval_id，destructive 步骤持批准执行。
- 任务与步骤存在 SQLite 里，与记忆库同库不同表，导出时一起走。

不联网，不 import 任何模型供应商 SDK。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from gateway import TOOL_REGISTRY

# 分解白名单（JIS-016）：模型拆出的步骤只能用这 5 个安全工具。
# 与 decompose() 提示词里的工具列表一致；delete_file 等破坏性工具
# 不得由模型拆分产出（破坏性操作只能由用户显式建步骤 + 持批准执行）。
DECOMPOSE_ALLOWED_TOOLS = frozenset(
    {"list_tasks", "read_task", "memory-recall", "memory-add", "router-call"})

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
            # S2-4：gate_result（JIS-018）= 每步记忆写入的 gate 判定
            # （accepted/needs_review/quarantined/already_exists；非
            # memory-add 步骤为空）；approval_id（JIS-013）= 该步
            # destructive 调用所持的工具批准 id（无则为空）。
            step_cols = {row[1] for row in
                         conn.execute("PRAGMA table_info(plan_steps)")}
            if "gate_result" not in step_cols:
                conn.execute(
                    "ALTER TABLE plan_steps ADD COLUMN gate_result TEXT")
            if "approval_id" not in step_cols:
                conn.execute(
                    "ALTER TABLE plan_steps ADD COLUMN approval_id TEXT")
            conn.commit()

    def _gate_summary(self, steps: list[dict]) -> dict:
        """任务的记忆写入汇总（JIS-018）：每步 gate 结果分桶；
        被隔离的步骤列出来，不被记为“已写入”。"""
        summary: dict[str, list[str]] = {
            "written": [], "quarantined": [], "needs_review": [],
            "already_exists": [],
        }
        for step in steps:
            gate = step.get("gate_result")
            if not gate:
                continue
            if gate in ("accepted",):
                summary["written"].append(step["id"])
            elif gate in summary:
                summary[gate].append(step["id"])
            else:
                summary.setdefault(gate, []).append(step["id"])
        return summary

    def create(self, goal: str, steps: list[dict], plan_id: str | None = None) -> dict:
        """建任务：校验每步的工具都在注册表里，否则拒绝建任务。
        每步可带可选 approval_id（字符串）：destructive 步骤执行时
        持该批准放行（JIS-013）；无批准的 destructive 步骤执行时被拒。"""
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
            if ("approval_id" in step and step["approval_id"] is not None
                    and not isinstance(step["approval_id"], str)):
                return {"decision": "rejected",
                        "reason": "approval_id_must_be_string"}
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
                    " status, result_hash, created_at, gate_result, approval_id)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                    (f"{plan_id}-step-{seq+1}", plan_id, seq + 1,
                     step["tool"], json.dumps(step.get("args", {}), ensure_ascii=False),
                     "pending", None, now_iso(), step.get("approval_id")),
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
        # JIS-018：任务报告含每步 gate 结果汇总；被隔离的步骤不算“已写入”。
        plan["gate_summary"] = self._gate_summary(plan["steps"])
        return plan

    def list_plans(self) -> list[dict]:
        self.init()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM plans ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    def _run_steps(self, plan_id: str, steps: list[dict],
                   start_index: int) -> dict:
        """从 steps[start_index] 起按顺序执行（run 与 resume 共用）。

        已 done 的步骤不重跑；失败一步即停，任务状态如实记为
        denied / failed。memory-add 步骤的 gate 判定记进 gate_result
        （JIS-018），供任务报告汇总；被隔离的步骤仍算“已执行”
        （网关放行了），但 gate_summary 里标为 quarantined，
        不被记为“已写入”。
        """
        for step in steps[start_index:]:
            step_id = step["id"]
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("UPDATE plan_steps SET status='running' WHERE id = ?",
                             (step_id,))
                conn.commit()
            args = json.loads(step["args_json"])
            try:
                result = self.gateway.execute(
                    step["tool"], args, actor="planner",
                    approval_id=step.get("approval_id"))
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
            # 执行成功：记结果 hash；memory-add 时记 gate 判定（JIS-018）。
            from gateway import _hash
            result_hash = _hash(result.get("result", {}))
            gate_result = None
            if step["tool"] == "memory-add":
                inner = result.get("result") or {}
                if isinstance(inner, dict) and inner.get("decision"):
                    gate_result = str(inner["decision"])
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("UPDATE plan_steps SET status='done', result_hash=?,"
                             " gate_result=? WHERE id = ?",
                             (result_hash, gate_result, step_id))
                conn.commit()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE plans SET status='done', updated_at=?"
                         " WHERE id = ?", (now_iso(), plan_id))
            conn.commit()
        return {"decision": "done", "id": plan_id, "steps": len(steps)}

    def run(self, plan_id: str) -> dict:
        """按顺序执行：失败一步即停；重跑时重置步骤状态。"""
        self.init()
        plan = self.status(plan_id)
        if plan is None:
            return {"decision": "not_found", "id": plan_id}
        steps = plan["steps"]
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE plan_steps SET status='pending', result_hash=NULL,"
                         " gate_result=NULL WHERE plan_id = ?", (plan_id,))
            conn.execute("UPDATE plans SET status='running', updated_at=?"
                         " WHERE id = ?", (now_iso(), plan_id))
            conn.commit()
        out = self._run_steps(plan_id, steps, 0)
        out["gate_summary"] = self._gate_summary(self._get_steps(plan_id))
        return out

    def resume(self, plan_id: str, mode: str = "retry",
               new_args: dict | None = None) -> dict:
        """从失败那步续跑（JIS-017），不整个重跑。

        mode=retry：从第一个 failed/denied 的步骤重跑（其余 done
        的步骤不重跑，结果 hash 不变）；mode=skip：把失败步骤标为
        skipped，从下一步继续；mode=retry-args：先把失败步骤的参数
        换成 new_args 再重跑（改参数续跑）。续跑记 plan_resumed
        审计，状态与审计连续。
        """
        self.init()
        if mode not in ("retry", "skip", "retry-args"):
            return {"decision": "rejected", "reason": f"unknown_mode:{mode}"}
        plan = self.status(plan_id)
        if plan is None:
            return {"decision": "not_found", "id": plan_id}
        steps = plan["steps"]
        failed_index = next(
            (i for i, s in enumerate(steps)
             if s["status"] in ("failed", "denied")), None)
        if failed_index is None:
            return {"decision": "rejected", "id": plan_id,
                    "reason": "nothing_to_resume"}
        failed = steps[failed_index]
        start_index = failed_index
        if mode == "skip":
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("UPDATE plan_steps SET status='skipped' WHERE id = ?",
                             (failed["id"],))
                conn.commit()
            start_index = failed_index + 1
            if start_index >= len(steps):
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute("UPDATE plans SET status='done', updated_at=?"
                                 " WHERE id = ?", (now_iso(), plan_id))
                    conn.commit()
                self.gateway.event_log.append(
                    "plan_resumed", "ok",
                    json.dumps({"plan_id": plan_id, "mode": "skip",
                                "skipped_step": failed["id"]},
                               ensure_ascii=False))
                return {"decision": "done", "id": plan_id, "mode": "skip",
                        "skipped_step": failed["id"],
                        "gate_summary": self._gate_summary(self._get_steps(plan_id))}
        elif mode == "retry-args":
            if not isinstance(new_args, dict):
                return {"decision": "rejected", "id": plan_id,
                        "reason": "retry-args requires new_args object"}
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("UPDATE plan_steps SET args_json=?, status='pending',"
                             " result_hash=NULL, gate_result=NULL WHERE id = ?",
                             (json.dumps(new_args, ensure_ascii=False),
                              failed["id"]))
                conn.commit()
            steps[failed_index]["args_json"] = json.dumps(
                new_args, ensure_ascii=False)
        else:  # retry：失败步骤重置为 pending，其余 done 步骤不动
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("UPDATE plan_steps SET status='pending',"
                             " result_hash=NULL, gate_result=NULL WHERE id = ?",
                             (failed["id"],))
                conn.commit()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE plans SET status='running', updated_at=?"
                         " WHERE id = ?", (now_iso(), plan_id))
            conn.commit()
        self.gateway.event_log.append(
            "plan_resumed", "ok",
            json.dumps({"plan_id": plan_id, "mode": mode,
                        "from_step": failed["id"],
                        "from_seq": failed["seq"]}, ensure_ascii=False))
        out = self._run_steps(plan_id, steps, start_index)
        out["mode"] = mode
        out["resumed_from_step"] = failed["id"]
        out["gate_summary"] = self._gate_summary(self._get_steps(plan_id))
        return out

    def decompose(self, goal: str, router, logical: str = "reasoning") -> dict:
        """用真实模型把自然语言目标拆成步骤（JIS-016）。

        固定提示词要求模型只输出 JSON：{"steps": [{"tool": ..., "args": {...}}]}；
        解析后逐项校验（工具在分解白名单里、args 是对象），不合格即失败，
        不猜、不补。模型不可用或降级（degraded）时直接失败，不编步骤。
        返回不含任何“已验证通过”的评价——拆分质量须人工评价，见 S2-4 记录。
        """
        if not goal.strip():
            return {"decision": "rejected", "reason": "empty_goal"}
        prompt = (
            "把以下目标拆成按顺序执行的步骤。只输出一个 JSON 对象，"
            "格式：{\"steps\": [{\"tool\": \"工具名\", \"args\": {参数}}]}。"
            "工具名只能是以下之一：list_tasks, read_task, memory-recall, "
            "memory-add, router-call。args 必须是 JSON 对象。不要输出"
            "任何解释文字。\n目标：" + goal
        )
        try:
            result = router.complete(logical, prompt)
        except Exception as exc:
            return {"decision": "failed", "reason": f"router_error:{exc}"}
        if result.get("degraded"):
            return {"decision": "failed", "reason": "model_degraded",
                    "attempted": result.get("attempted")}
        content = str(result.get("content", "") or "")
        steps = self._parse_decomposition(content)
        if steps is None:
            return {"decision": "failed", "reason": "unparsable_output",
                    "model": result.get("model"), "raw": content[:500]}
        for step in steps:
            if step.get("tool") not in DECOMPOSE_ALLOWED_TOOLS:
                return {"decision": "failed",
                        "reason": f"unknown_tool:{step.get('tool')}",
                        "model": result.get("model"), "raw": content[:500]}
            if not isinstance(step.get("args", {}), dict):
                return {"decision": "failed", "reason": "args_must_be_object",
                        "model": result.get("model"), "raw": content[:500]}
        return {"decision": "decomposed", "goal": goal,
                "model": result.get("model"), "steps": steps,
                "raw": content[:500]}

    @staticmethod
    def _parse_decomposition(content: str):
        """解析模型输出的步骤 JSON：先整段解析，再在文本里找第一个
        {...} 或 [...]；解析失败返回 None（不猜）。"""
        text = content.strip()
        for candidate in (text,):
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict) and isinstance(parsed.get("steps"), list):
                    return parsed["steps"]
                if isinstance(parsed, list):
                    return parsed
            except Exception:
                pass
        import re as _re
        match = _re.search(r"\{[\s\S]*\}", text)
        if match:
            try:
                parsed = json.loads(match.group(0))
                if isinstance(parsed, dict) and isinstance(parsed.get("steps"), list):
                    return parsed["steps"]
            except Exception:
                pass
        return None
