"""Jarvis P1 — 确定性核心（Agent Core 最小版）

设计红线（来自总体架构）：
- 这是纯确定性代码，不导入任何模型供应商 SDK，不调用任何 LLM，不联网。
- 启动顺序固定：config -> database -> policy -> audit -> scheduler。
- 事件日志是唯一事实源：追加写 JSONL，重启不丢。
- 任务写入幂等：种子任务用固定 id + INSERT OR IGNORE，重复启动不重复。
- P1 变化：config 改用 PyYAML 真解析（requirements.txt: pyyaml==6.0.1），
  解决 P0“最小文本检查”的遗留问题；boot 时同时初始化记忆表。
"""

from __future__ import annotations

import json
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
# health 检查会用 grep + sys.modules 双重验证。
FORBIDDEN_MODULE_PREFIXES = (
    "openai",
    "anthropic",
    "google.generativeai",
    "google-genai",
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
    """追加写事件日志。"""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: str, result: str, detail: str = "") -> dict:
        record = {
            "time": now_iso(),
            "event": event,
            "result": result,
            "detail": detail,
        }
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
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
        loaded = [m for m in sys.modules if m.split(".")[0] in FORBIDDEN_MODULE_PREFIXES]
        return loaded

    def boot(self) -> dict:
        """按 config -> database -> policy -> audit -> scheduler 启动。"""
        # 1. config（P1：真 YAML 解析）
        config = self.load_config()
        if not config.get("core", {}).get("model_enabled") is False:
            raise ValueError("config must set core.model_enabled: false in P1")
        if config.get("policy", {}).get("network_calls_allowed") is not False:
            raise ValueError("config must set policy.network_calls_allowed: false in P1")
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
        """健康检查：配置、数据库、记忆表、事件日志、策略、禁 import。"""
        checks: dict[str, bool] = {}
        try:
            config = self.load_config()
            checks["config_exists"] = True
            checks["model_disabled"] = config.get("core", {}).get("model_enabled") is False
        except Exception:
            checks["config_exists"] = False
            checks["model_disabled"] = False
        try:
            count = self.store.init()
            self.memory.init()
            checks["database_readable"] = count == len(SEED_TASKS)
            checks["memory_tables_ok"] = True
        except Exception:
            checks["database_readable"] = False
            checks["memory_tables_ok"] = False
        try:
            self.event_log.append("health", "ok", "")
            checks["event_log_writable"] = True
        except Exception:
            checks["event_log_writable"] = False
        checks["no_forbidden_imports"] = len(self._check_forbidden_imports()) == 0
        ok = all(checks.values())
        return {"status": "ok" if ok else "fail", "checks": checks}
