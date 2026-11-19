"""Jarvis P1 — 长期记忆治理（Memory 最小版）

原则（来自用户需求基线 A3-1 与可行性报告）：
- 记忆不是存聊天记录：提取、分类、去重、关联、检索、更新、纠错，区分事实/观点/推断/待验证。
- 写时 gate：新事实写入前做一致性检查，低于阈值进隔离区，不直接入库。
- 版本化、不删除旧版：修正不是覆盖旧事实，而是旧事实标 superseded，新事实另起一条，Correction 记录保留历史。
- P1 先用规则 + 模板提取，不调 LLM，证明流程本身成立；LLM 提取留给 P2 路由就绪后。
- 可信等级语义（P6-11，见三方质疑裁决记录）：
  verified = 已通过写时 gate 且置信度 >=0.8 且证据等级属于
  (user_stated, verified, multiple_occurrences) 的当前事实；只有用户
  本人（actor=user）或 gate 的 accepted 判定能产生 verified。
  provisional = 待核实的当前事实（单次证据、高置信单次、外部导入等）。
  外部导入、规划器模板等非用户 actor 一律不得自授 verified：导入的
  verified 一律降为 provisional；网关对 actor != "user" 的 memory-add
  强制 evidence_grade=single_occurrence（从而状态上限 provisional）。
  superseded / expired / disputed 不是当前事实，不召回。
- 修正只改当前事实：correct_fact 只允许修正 verified/provisional，
  新事实继承旧事实的置信度与证据等级（不自授 0.9/verified），
  注入文本进隔离区、旧事实不动；回滚只允许链最新项，按原状态恢复。

只用标准库 + SQLite。PyYAML 只在 core.py 读配置时用，memory.py 本身不依赖它。
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "memory-facts-v1"

FACT_TYPES = ("fact", "viewpoint", "inference", "to_verify")
STATUSES = ("verified", "provisional", "superseded", "disputed", "expired")
EVIDENCE_GRADES = (
    "user_stated",
    "single_occurrence",
    "multiple_occurrences",
    "verified",
)

# 写时 gate：注入 / 恶意指令模式（中英文，子串匹配，确定性规则）
INJECTION_PATTERNS = (
    "忽略之前",
    "忽略以上",
    "ignore previous",
    "ignore all previous",
    "删除所有",
    "delete all",
    "rm -rf",
    "drop table",
    "删库",
    "以管理员身份",
    "as admin",
    "你是管理员",
    "system prompt",
    "泄露密码",
    "泄露密钥",
    "password",
    "api key",
    "密钥",
    "假装你是",
    "pretend you are",
)

OVERGENERALIZATION_MARKERS = ("所有人", "永远", "100%")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def today_str() -> str:
    return date.today().isoformat()


class WriteGate:
    """写时 gate：确定性规则，无模型。"""

    @staticmethod
    def check(text: str, fact_type: str, confidence: float,
              evidence_grade: str, source: str) -> tuple[str, str]:
        """返回 (decision, reason)，decision ∈ accepted / needs_review / quarantined。"""
        if not text or len(text.strip()) < 4:
            return "quarantined", "empty_or_too_short"
        if fact_type not in FACT_TYPES:
            return "quarantined", "invalid_type"
        if not (0.0 <= confidence <= 1.0):
            return "quarantined", "confidence_out_of_range"
        if evidence_grade not in EVIDENCE_GRADES:
            return "quarantined", "invalid_evidence_grade"
        if not source or not source.strip():
            return "quarantined", "missing_source"
        lowered = text.lower()
        for pat in INJECTION_PATTERNS:
            if pat.lower() in lowered:
                return "quarantined", f"injection_pattern:{pat}"
        # 过度泛化：单次证据 + 绝对化表述 + 低置信度 -> 隔离
        if (
            evidence_grade == "single_occurrence"
            and confidence < 0.8
            and any(m in text for m in OVERGENERALIZATION_MARKERS)
        ):
            return "quarantined", "overgeneralization"
        if confidence < 0.3:
            return "quarantined", "low_confidence"
        # 高置信度但证据只有单次：可写入，但只能是 provisional，需复核
        if confidence >= 0.8 and evidence_grade == "single_occurrence":
            return "needs_review", "high_confidence_single_occurrence"
        return "accepted", "ok"


# 规则提取模板：只匹配明确句式，不匹配就不提取，不猜。
# 每条：(正则, 事实模板, 类型, 置信度, 证据等级)
EXTRACT_PATTERNS: list[tuple[str, str, str, float, str]] = [
    (r"我喜欢(.+)", "用户喜欢{0}", "viewpoint", 0.6, "single_occurrence"),
    (r"我不喜欢(.+)", "用户不喜欢{0}", "viewpoint", 0.6, "single_occurrence"),
    (r"我偏好(.+)", "用户偏好{0}", "viewpoint", 0.6, "single_occurrence"),
    (r"我的目标是(.+)", "用户目标是{0}", "fact", 0.6, "single_occurrence"),
    (r"我住在(.+)", "用户住在{0}", "fact", 0.6, "single_occurrence"),
    (r"我认为(.+)", "用户认为{0}", "viewpoint", 0.5, "single_occurrence"),
    (r"我记得(.+)", "用户记得{0}", "fact", 0.5, "single_occurrence"),
]


def extract_facts(dialogue: str, source: str) -> list[dict]:
    """从一句话对话中按模板提取事实；不匹配返回空列表，不猜。"""
    text = dialogue.strip()
    for pattern, template, fact_type, confidence, grade in EXTRACT_PATTERNS:
        m = re.search(pattern, text)
        if m:
            content = m.group(1).strip().rstrip("。！!,.，")
            if not content:
                continue
            return [{
                "text": template.format(content),
                "fact_type": fact_type,
                "confidence": confidence,
                "evidence_grade": grade,
                "source": source,
            }]
    return []


class MemoryStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def init(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_facts (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    text TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    evidence_grade TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    valid_from TEXT,
                    valid_until TEXT,
                    status TEXT NOT NULL,
                    superseded_by TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS quarantine (
                    id TEXT PRIMARY KEY,
                    text TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS corrections (
                    id TEXT PRIMARY KEY,
                    fact_id TEXT NOT NULL,
                    old_text TEXT NOT NULL,
                    new_text TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    new_fact_id TEXT,
                    old_status TEXT
                )
                """
            )
            # P6-10：旧库补链追踪列。new_fact_id = 本次修正产生的新事实 id，
            # old_status = 被修正事实在修正前的状态（回滚时按原状态恢复）。
            corr_cols = {row[1] for row in
                         conn.execute("PRAGMA table_info(corrections)")}
            if "new_fact_id" not in corr_cols:
                conn.execute(
                    "ALTER TABLE corrections ADD COLUMN new_fact_id TEXT")
            if "old_status" not in corr_cols:
                conn.execute(
                    "ALTER TABLE corrections ADD COLUMN old_status TEXT")
            conn.commit()

    def _insert_fact(self, conn, fact_id, fact_type, text, confidence,
                     evidence_grade, source, valid_from, valid_until, status):
        conn.execute(
            "INSERT INTO memory_facts"
            " (id, type, text, confidence, evidence_grade, source,"
            "  created_at, valid_from, valid_until, status, superseded_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (fact_id, fact_type, text, confidence, evidence_grade, source,
             now_iso(), valid_from, valid_until, status),
        )

    def add_fact(self, text: str, fact_type: str, confidence: float,
                 evidence_grade: str, source: str,
                 valid_from: str | None = None,
                 valid_until: str | None = None,
                 fact_id: str | None = None) -> dict:
        """经写时 gate 写入。返回 decision / id / reason。"""
        self.init()
        decision, reason = WriteGate.check(
            text, fact_type, confidence, evidence_grade, source
        )
        fid = fact_id or f"mem-{uuid.uuid4().hex[:12]}"
        # 幂等：固定 id 已存在时直接返回，不重复写入（P4 任务重跑用）。
        if fact_id is not None:
            existing = self.get_fact(fact_id)
            if existing is not None:
                return {"decision": "already_exists", "id": fact_id,
                        "reason": "fact_id already exists",
                        "status": existing["status"]}
        with sqlite3.connect(self.db_path) as conn:
            if decision == "quarantined":
                conn.execute(
                    "INSERT INTO quarantine (id, text, reason, source, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (fid, text, reason, source, now_iso()),
                )
                conn.commit()
                return {"decision": "quarantined", "id": fid, "reason": reason}
            status = "verified" if decision == "accepted" and confidence >= 0.8 \
                and evidence_grade in ("user_stated", "verified", "multiple_occurrences") \
                else "provisional"
            # needs_review 也落为 provisional，需人工复核后才能升 verified
            self._insert_fact(conn, fid, fact_type, text, confidence,
                              evidence_grade, source,
                              valid_from or today_str(), valid_until, status)
            conn.commit()
            return {"decision": decision, "id": fid, "reason": reason, "status": status}

    def _expire_check(self, conn) -> None:
        """过期不是删除：valid_until 已过且状态为 verified/provisional 的，标 expired。"""
        conn.execute(
            "UPDATE memory_facts SET status='expired'"
            " WHERE valid_until IS NOT NULL AND valid_until < ?"
            " AND status IN ('verified', 'provisional')",
            (today_str(),),
        )

    def get_fact(self, fact_id: str) -> dict | None:
        self.init()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            self._expire_check(conn)
            conn.commit()
            row = conn.execute(
                "SELECT * FROM memory_facts WHERE id = ?", (fact_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_current(self, query: str | None = None) -> list[dict]:
        """只召回当前事实：verified / provisional；superseded / expired / disputed 不召回。"""
        self.init()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            self._expire_check(conn)
            conn.commit()
            if query:
                rows = conn.execute(
                    "SELECT * FROM memory_facts"
                    " WHERE status IN ('verified', 'provisional') AND text LIKE ?"
                    " ORDER BY created_at",
                    (f"%{query}%",),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM memory_facts"
                    " WHERE status IN ('verified', 'provisional')"
                    " ORDER BY created_at"
                ).fetchall()
        return [dict(r) for r in rows]

    def list_quarantine(self) -> list[dict]:
        self.init()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM quarantine ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def correct_fact(self, fact_id: str, new_text: str, evidence: str,
                     actor: str = "user") -> dict:
        """修正：旧事实标 superseded（保留历史），新事实另起一条，写 Correction 记录。

        P6-5（M-a 的修复）：修正前先过 WriteGate，不通过进隔离区、
        旧事实不动；只允许修正 verified/provisional 的当前事实
        （已取代的事实不得再修正，否则会产生两条“当前”事实，M-c）；
        新事实继承旧事实的置信度与证据等级与状态上限，
        不再硬编码 0.9/verified（旧代码的自授高可信是 M-a 的根因）。
        旧事实的置信度/证据等级若已损坏（非数值、越界、非法等级，
        多为旧版未校验导入的遗留行），则以 (0.6, single_occurrence)
        兜底并降为 provisional，不抛异常。
        """
        self.init()
        old = self.get_fact(fact_id)
        if old is None:
            return {"decision": "not_found", "id": fact_id}
        if old["status"] not in ("verified", "provisional"):
            return {"decision": "rejected", "id": fact_id,
                    "reason": f"cannot_correct_status:{old['status']}"}
        try:
            old_conf = float(old["confidence"])
        except (TypeError, ValueError):
            old_conf = None
        trust_reset = False
        if (old_conf is None or isinstance(old["confidence"], bool)
                or not (0.0 <= old_conf <= 1.0)
                or old["evidence_grade"] not in EVIDENCE_GRADES):
            old_conf, old_grade, trust_reset = 0.6, "single_occurrence", True
        else:
            old_grade = old["evidence_grade"]
        decision, reason = WriteGate.check(
            new_text, old["type"], old_conf, old_grade,
            f"correction:{evidence}")
        if decision == "quarantined":
            qid = f"q-{uuid.uuid4().hex[:12]}"
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO quarantine (id, text, reason, source, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (qid, new_text, reason, f"correction:{evidence}", now_iso()),
                )
                conn.commit()
            return {"decision": "quarantined", "id": fact_id,
                    "reason": reason, "quarantine_id": qid}
        if (not trust_reset and decision == "accepted"
                and old["status"] == "verified" and old_conf >= 0.8
                and old_grade in ("user_stated", "verified",
                                  "multiple_occurrences")):
            new_status = "verified"
        else:
            new_status = "provisional"
        new_id = f"mem-{uuid.uuid4().hex[:12]}"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE memory_facts SET status='superseded', superseded_by=?"
                " WHERE id = ?",
                (new_id, fact_id),
            )
            self._insert_fact(conn, new_id, old["type"], new_text, old_conf,
                              old_grade, f"correction:{evidence}",
                              today_str(), None, new_status)
            conn.execute(
                "INSERT INTO corrections (id, fact_id, old_text, new_text,"
                " evidence, actor, created_at, new_fact_id, old_status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"corr-{uuid.uuid4().hex[:8]}", fact_id, old["text"],
                 new_text, evidence, actor, now_iso(), new_id,
                 old["status"]),
            )
            conn.commit()
        return {"decision": "corrected", "old_id": fact_id, "new_id": new_id,
                "status": new_status, "trust_reset": trust_reset}

    def list_corrections(self) -> list[dict]:
        self.init()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM corrections ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def rollback_correction(self, correction_id: str, actor: str = "user") -> dict:
        """一键改回：按 Correction 记录回滚一次修正。

        P6-10（M-c/M-d 的修复）：只允许回滚链最新项——被修正事实的
        superseded_by 必须仍指向本次修正产生的新事实，否则说明之后
        还有更新的修正，回滚会与之冲突，直接拒绝（M-d 的根因：旧代码
        按 superseded_by 找新事实，链式修正后它已指向更晚的事实）；
        新事实必须是当前事实（verified/provisional），已取代的不回滚；
        旧事实按修正前记录的原状态恢复，不再无条件恢复成 verified；
        缺链追踪字段的老记录（new_fact_id 为空）无法核验链位置，拒绝。
        历史全保留：再写一条反向 Correction 记录。
        """
        self.init()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            corr = conn.execute(
                "SELECT * FROM corrections WHERE id = ?", (correction_id,)
            ).fetchone()
            if corr is None:
                return {"decision": "not_found", "id": correction_id}
            corr = dict(corr)
            new_fact_id = corr.get("new_fact_id")
            old_status = corr.get("old_status")
            if not new_fact_id or not old_status:
                return {"decision": "rejected", "id": correction_id,
                        "reason": "correction_chain_unknown"}
            old = conn.execute(
                "SELECT * FROM memory_facts WHERE id = ?", (corr["fact_id"],)
            ).fetchone()
            if old is None:
                return {"decision": "not_found", "id": corr["fact_id"]}
            old = dict(old)
            new = conn.execute(
                "SELECT * FROM memory_facts WHERE id = ?", (new_fact_id,)
            ).fetchone()
            if new is None:
                return {"decision": "not_found", "id": new_fact_id}
            new = dict(new)
            # 链最新检查：旧事实的 superseded_by 必须仍指向本次修正的新事实
            if old.get("superseded_by") != new_fact_id:
                return {"decision": "rejected", "id": correction_id,
                        "reason": "not_latest_correction"}
            if new["status"] not in ("verified", "provisional"):
                return {"decision": "rejected", "id": correction_id,
                        "reason": f"cannot_rollback_status:{new['status']}"}
            # 旧事实按原状态恢复，新事实标 superseded
            conn.execute(
                "UPDATE memory_facts SET status=?, superseded_by=NULL"
                " WHERE id = ?",
                (old_status, corr["fact_id"]),
            )
            conn.execute(
                "UPDATE memory_facts SET status='superseded', superseded_by=?"
                " WHERE id = ?",
                (corr["fact_id"], new_fact_id),
            )
            conn.execute(
                "INSERT INTO corrections (id, fact_id, old_text, new_text,"
                " evidence, actor, created_at, new_fact_id, old_status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"corr-{uuid.uuid4().hex[:8]}", new_fact_id, corr["new_text"],
                 corr["old_text"], f"rollback of {correction_id}", actor,
                 now_iso(), corr["fact_id"], new["status"]),
            )
            conn.commit()
        return {"decision": "rolled_back", "correction_id": correction_id,
                "restored_id": corr["fact_id"], "superseded_id": new_fact_id,
                "restored_status": old_status}

    def count_by_status(self) -> dict:
        self.init()
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM memory_facts GROUP BY status"
            ).fetchall()
        return {status: count for status, count in rows}

    def export_jsonl(self, out_path: Path) -> dict:
        """导出：JSONL，每行带 schema_version；向量可重算，文本与来源是 ground truth。"""
        self.init()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM memory_facts ORDER BY created_at"
            ).fetchall()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            for row in rows:
                record = dict(row)
                record["schema_version"] = SCHEMA_VERSION
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return {"schema_version": SCHEMA_VERSION, "count": len(rows), "path": str(out_path)}

    def import_jsonl(self, in_path: Path) -> dict:
        """导入导出的 JSONL：逐条校验，逐条隔离，返回四分计数。

        P6-6（M-b 的修复）：每行逐条校验 status/type/confidence/
        evidence_grade/必填字段，再过 WriteGate；不合格计 rejected，
        缺字段逐条计 rejected、不整批回滚；重复 id 计 ignored_existing
        （旧代码 INSERT OR IGNORE 静默丢行不计数，2+1≠4）；schema_version
        不符计 skipped；四数之和恒等于行数。外部数据不得自授 verified：
        文件里标 verified 的行一律降为 provisional 入库（P6-11）。
        每行独立提交，一行的坏数据不影响其他行。
        """
        self.init()
        imported = 0
        ignored_existing = 0
        rejected = 0
        skipped = 0
        with open(in_path, encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
        required = ("id", "type", "text", "confidence", "evidence_grade",
                    "source", "created_at", "status")
        for line in lines:
            try:
                record = json.loads(line)
            except Exception:
                rejected += 1
                continue
            if not isinstance(record, dict):
                rejected += 1
                continue
            if record.get("schema_version") != SCHEMA_VERSION:
                skipped += 1
                continue
            if any(k not in record for k in required):
                rejected += 1
                continue
            if (record["type"] not in FACT_TYPES
                    or record["status"] not in STATUSES
                    or record["evidence_grade"] not in EVIDENCE_GRADES
                    or not isinstance(record["text"], str)
                    or not record["text"].strip()
                    or not isinstance(record["id"], str)
                    or not record["id"].strip()
                    or not isinstance(record["source"], str)
                    or not record["source"].strip()
                    or not isinstance(record["created_at"], str)):
                rejected += 1
                continue
            try:
                conf = float(record["confidence"])
            except (TypeError, ValueError):
                rejected += 1
                continue
            if (isinstance(record["confidence"], bool)
                    or not (0.0 <= conf <= 1.0)):
                rejected += 1
                continue
            if record.get("superseded_by") is not None and not isinstance(
                    record.get("superseded_by"), str):
                rejected += 1
                continue
            if self.get_fact(record["id"]) is not None:
                ignored_existing += 1
                continue
            decision, _reason = WriteGate.check(
                record["text"], record["type"], conf,
                record["evidence_grade"], record["source"])
            if decision == "quarantined":
                rejected += 1
                continue
            # 外部数据不得自授 verified：降为 provisional（P6-11）
            status = ("provisional" if record["status"] == "verified"
                      else record["status"])
            try:
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute(
                        "INSERT INTO memory_facts"
                        " (id, type, text, confidence, evidence_grade, source,"
                        "  created_at, valid_from, valid_until, status, superseded_by)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (record["id"], record["type"], record["text"],
                         conf, record["evidence_grade"],
                         record["source"], record["created_at"],
                         record.get("valid_from"), record.get("valid_until"),
                         status, record.get("superseded_by")),
                    )
                    conn.commit()
            except Exception:
                rejected += 1
                continue
            imported += 1
        return {"schema_version": SCHEMA_VERSION, "imported": imported,
                "ignored_existing": ignored_existing, "rejected": rejected,
                "skipped": skipped, "lines": len(lines)}
