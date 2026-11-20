"""Jarvis S2-1 — 确定性核心（Agent Core 最小版 + 模型接入层）

设计红线（来自总体架构）：
- 这是纯确定性代码，不导入任何模型供应商 SDK，不调用任何 LLM，不联网。
- 启动顺序固定：config -> database -> policy -> audit -> scheduler。
- 事件日志是唯一事实源：追加写 JSONL，重启不丢。
- 任务写入幂等：种子任务用固定 id + INSERT OR IGNORE，重复启动不重复。
- P1 变化：config 改用 PyYAML 真解析（requirements.txt: pyyaml==6.0.1），
  解决 P0“最小文本检查”的遗留问题；boot 时同时初始化记忆表。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError as exc:  # 明确报错，不静默降级
    raise ImportError(
        "P1 需要 PyYAML，请先 pip install -r requirements.txt"
    ) from exc

from memory import MemoryStore

# P0 显式禁止的 import：任何模型供应商 SDK 都不得出现。
# health 检查用 sys.modules 验证已加载模块（P6-14 更正：旧注释说的
# “grep + sys.modules 双重验证”只有后者，全文无 grep）。
FORBIDDEN_MODULE_PREFIXES = (
    "openai",
    "anthropic",
    "google.generativeai",
    "google.genai",
    "litellm",
    "langchain",
    "langgraph",
    "ollama",
)

SEED_TASKS = [
    # 合成数据，绝不用真实对话/文件。id 固定，保证幂等。
    ("task-001", "检查本地备份是否完整", "pending", "2026-11-19"),
    ("task-002", "整理本周合成测试数据", "pending", "2026-11-19"),
    ("task-003", "验证事件日志追加写", "pending", "2026-11-19"),
    ("task-004", "演练一次回滚：恢复上一版数据快照", "pending", "2026-11-19"),
    ("task-005", "写 P0 测试记录", "pending", "2026-11-19"),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventLog:
    """追加写事件日志（P6-18：哈希链）。

    每条记录带 prev_hash（上一条的 entry_hash）与 entry_hash
    （本条内容的 sha256），并在同目录写检查点文件
    events.checkpoint.json（条数 + 末尾哈希）。verify() 逐条核对链
    与检查点：截断表现为条数少于检查点，篡改表现为哈希不匹配。
    局限（已记录在案）：检查点在追加时被覆盖，截断须在追加前用
    verify() 发现；检查点本身被删则无法确认完整性。完整方案须把
    检查点放独立信任域，Phase 2。旧日志（无 entry_hash 的记录）
    逐条标 legacy_entry，不可验证。
    """

    GENESIS = "GENESIS"

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _checkpoint_path(self) -> Path:
        return self.path.parent / (self.path.stem + ".checkpoint.json")

    def _read_checkpoint(self) -> tuple[int, str]:
        """读检查点；缺失或损坏返回 (0, GENESIS)。"""
        try:
            data = json.loads(
                self._checkpoint_path().read_text(encoding="utf-8"))
            return int(data["count"]), str(data["last_hash"])
        except Exception:
            return 0, self.GENESIS

    def _last_hash(self) -> str:
        """上一条的 entry_hash：优先检查点；无检查点时从日志末尾取；
        旧记录无 entry_hash 时用行哈希锚定（LEGACY: 前缀）。"""
        count, last_hash = self._read_checkpoint()
        if count > 0:
            return last_hash
        if not self.path.exists():
            return self.GENESIS
        last_line = None
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    last_line = line
        if last_line is None:
            return self.GENESIS
        try:
            rec = json.loads(last_line)
            if rec.get("entry_hash"):
                return rec["entry_hash"]
        except Exception:
            pass
        return "LEGACY:" + hashlib.sha256(
            last_line.encode("utf-8")).hexdigest()

    @staticmethod
    def _entry_hash(record: dict) -> str:
        payload = "|".join([
            record.get("prev_hash", ""),
            record.get("time", ""),
            record.get("event", ""),
            record.get("result", ""),
            record.get("detail", ""),
        ])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _write_checkpoint(self, count: int, last_hash: str) -> None:
        tmp = self._checkpoint_path().with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"count": count, "last_hash": last_hash},
                       ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._checkpoint_path())

    def append(self, event: str, result: str, detail: str = "") -> dict:
        record = {
            "time": now_iso(),
            "event": event,
            "result": result,
            "detail": detail,
            "prev_hash": self._last_hash(),
        }
        record["entry_hash"] = self._entry_hash(record)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        try:
            with open(self.path, encoding="utf-8") as f:
                actual = sum(1 for line in f if line.strip())
        except Exception:
            actual = self._read_checkpoint()[0] + 1
        self._write_checkpoint(actual, record["entry_hash"])
        return record

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        records = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records

    def verify(self) -> dict:
        """校验审计链。返回 {"ok": bool, "issues": [...], "count": int}。

        issues 可能的值：legacy_entry_at:<i>（旧记录，无哈希，不可验证）、
        chain_break_at:<i>（prev_hash 与上一条不符）、
        hash_mismatch_at:<i>（本条内容哈希不符）、
        truncated:log=N,checkpoint=M（条数少于检查点，尾部丢失）、
        checkpoint_stale:log=N,checkpoint=M（日志比检查点多，检查点滞后）、
        last_hash_mismatch（末尾哈希与检查点不符）、
        checkpoint_missing（检查点丢失，无法确认完整性）、
        log_unreadable:<err>（日志无法解析）。
        """
        issues: list[str] = []
        if not self.path.exists():
            return {"ok": False, "issues": ["log_missing"], "count": 0}
        try:
            with open(self.path, encoding="utf-8") as f:
                raw_lines = [line for line in f if line.strip()]
            records = [json.loads(line) for line in raw_lines]
        except Exception as exc:
            return {"ok": False, "issues": [f"log_unreadable:{exc}"], "count": 0}
        prev = self.GENESIS
        for idx, rec in enumerate(records):
            if not rec.get("entry_hash"):
                issues.append(f"legacy_entry_at:{idx}")
                prev = "LEGACY:" + hashlib.sha256(
                    raw_lines[idx].encode("utf-8")).hexdigest()
                continue
            if rec.get("prev_hash") != prev:
                issues.append(f"chain_break_at:{idx}")
            if rec.get("entry_hash") != self._entry_hash(rec):
                issues.append(f"hash_mismatch_at:{idx}")
            prev = rec["entry_hash"]
        cp_path = self._checkpoint_path()
        if not cp_path.exists():
            if records:
                issues.append("checkpoint_missing")
        else:
            try:
                data = json.loads(cp_path.read_text(encoding="utf-8"))
                cp_count, cp_last = int(data["count"]), str(data["last_hash"])
            except Exception as exc:
                issues.append(f"checkpoint_unreadable:{exc}")
                cp_count, cp_last = -1, ""
            if cp_count >= 0:
                if len(records) > cp_count:
                    issues.append(
                        f"checkpoint_stale:log={len(records)},checkpoint={cp_count}")
                elif len(records) < cp_count:
                    issues.append(
                        f"truncated:log={len(records)},checkpoint={cp_count}")
                elif records and records[-1].get("entry_hash") != cp_last:
                    issues.append("last_hash_mismatch")
        return {"ok": not issues, "issues": issues, "count": len(records)}


class TaskStore:
    """SQLite 任务库。"""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def init(self) -> int:
        """建表 + 种子写入（幂等）。返回当前任务总数。"""
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_date TEXT NOT NULL
                )
                """
            )
            conn.executemany(
                "INSERT OR IGNORE INTO tasks (id, title, status, created_date)"
                " VALUES (?, ?, ?, ?)",
                SEED_TASKS,
            )
            conn.commit()
            count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        return count

    def list_tasks(self) -> list[dict]:
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, title, status, created_date FROM tasks ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_task(self, task_id: str) -> dict | None:
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT id, title, status, created_date FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        return dict(row) if row else None


class Core:
    """确定性编排器：按固定顺序启动，不调用模型。"""

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.config_path = base_dir / "config.yaml"
        self.config: dict = {}
        self.db_path = base_dir / "data" / "jarvis.db"
        self.event_log = EventLog(base_dir / "data" / "events.jsonl")
        self.store = TaskStore(self.db_path)
        self.memory = MemoryStore(self.db_path)
        self.boot_steps: list[str] = []

    def load_config(self) -> dict:
        if not self.config_path.exists():
            raise FileNotFoundError(f"config not found: {self.config_path}")
        with open(self.config_path, encoding="utf-8") as f:
            self.config = yaml.safe_load(f) or {}
        return self.config

    def _check_forbidden_imports(self) -> list[str]:
        # P6-14：按完整点分前缀匹配（m == p 或 m 以 p + "." 开头）。
        # 旧代码用 m.split(".")[0] 查表，google.generativeai 的首段是
        # google，永远匹配不上，google 系供应商完全漏检。
        # 注：光 import google（命名空间包）仍不命中，可接受。
        loaded = [m for m in sys.modules
                  for p in FORBIDDEN_MODULE_PREFIXES
                  if m == p or m.startswith(p + ".")]
        return loaded

    def boot(self) -> dict:
        """按 config -> database -> policy -> audit -> scheduler 启动。"""
        # 1. config（P1：真 YAML 解析）
        config = self.load_config()
        # S2-1：启动约束须与 router_mode 一致。stub 模式沿用第一阶段的
        # 离线不变量（模型关闭、不联网）；live 模式必须显式声明模型启用
        # 与联网许可，否则拒绝启动——配置与模式不一致是配置错误，
        # 不静默放行。出厂默认 config 为 stub，两项均为 false。
        router_mode = (config.get("model", {}) or {}).get("router_mode", "stub")
        if router_mode not in ("stub", "live"):
            raise ValueError(f"unknown router_mode: {router_mode}")
        model_enabled = config.get("core", {}).get("model_enabled")
        network_allowed = config.get("policy", {}).get("network_calls_allowed")
        if router_mode == "stub":
            if model_enabled is not False:
                raise ValueError("stub mode requires core.model_enabled: false")
            if network_allowed is not False:
                raise ValueError(
                    "stub mode requires policy.network_calls_allowed: false")
        else:  # live
            if model_enabled is not True:
                raise ValueError("live mode requires core.model_enabled: true")
            if network_allowed is not True:
                raise ValueError(
                    "live mode requires policy.network_calls_allowed: true")
        self.boot_steps.append("config")

        # 2. database（建表 + 幂等种子）+ 记忆表初始化
        task_count = self.store.init()
        self.memory.init()
        self.boot_steps.append("database")

        # 3. policy（P1：只读本地任务，不启用写工具）
        if "list_tasks" not in config.get("policy", {}).get("allowed_tools", []):
            raise ValueError("policy must allow list_tasks in P1")
        self.boot_steps.append("policy")

        # 4. audit（事件日志可写）
        self.event_log.append("boot", "ok", f"steps={','.join(self.boot_steps)} tasks={task_count}")
        self.boot_steps.append("audit")

        # 5. scheduler（P0：只记录已就绪，不启动任何后台模型任务）
        self.boot_steps.append("scheduler")
        self.event_log.append("boot_complete", "ok", f"tasks={task_count}")

        forbidden = self._check_forbidden_imports()
        return {
            "status": "ok",
            "steps": self.boot_steps,
            "tasks": task_count,
            "memory_facts": self.memory.count_by_status(),
            "db": str(self.db_path),
            "event_log": str(self.event_log.path),
            "forbidden_imports_loaded": forbidden,
        }

    def health(self) -> dict:
        """健康检查：配置、数据库、记忆表、事件日志、策略、禁 import。

        P6-14（init/health 分离）：纯只读检查，不调会播种的 store.init()
        （旧代码调 init() 会复活被删的种子任务）；任务数只要求
        >= 种子数（合法增长不误报，旧代码 == 判等，多一条任务就 fail）；
        事件日志只读解析，不写 health 事件。
        """
        checks: dict[str, bool] = {}
        try:
            config = self.load_config()
            checks["config_exists"] = True
            # S2-1：模型/网络开关须与 router_mode 一致
            # （stub=全关，live=全开），不一致即 fail。
            router_mode = (config.get("model", {}) or {}).get("router_mode", "stub")
            model_enabled = config.get("core", {}).get("model_enabled")
            network_allowed = config.get("policy", {}).get("network_calls_allowed")
            checks["model_mode_consistent"] = (
                (router_mode == "stub" and model_enabled is False)
                or (router_mode == "live" and model_enabled is True))
            checks["network_mode_consistent"] = (
                (router_mode == "stub" and network_allowed is False)
                or (router_mode == "live" and network_allowed is True))
        except Exception:
            checks["config_exists"] = False
            checks["model_mode_consistent"] = False
            checks["network_mode_consistent"] = False
        try:
            # 只读连接：库文件不存在时直接失败，不创建文件、不建表、不播种
            uri = self.db_path.resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True) as conn:
                count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
                conn.execute("SELECT COUNT(*) FROM memory_facts").fetchone()
            checks["database_readable"] = count >= len(SEED_TASKS)
            checks["memory_tables_ok"] = True
        except Exception:
            checks["database_readable"] = False
            checks["memory_tables_ok"] = False
        try:
            self.event_log.read_all()
            checks["event_log_readable"] = True
        except Exception:
            checks["event_log_readable"] = False
        checks["no_forbidden_imports"] = len(self._check_forbidden_imports()) == 0
        ok = all(checks.values())
        return {"status": "ok" if ok else "fail", "checks": checks}
