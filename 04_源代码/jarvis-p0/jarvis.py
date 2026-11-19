#!/usr/bin/env python3
"""Jarvis P0 CLI：boot / health / list-tasks

用法（在 jarvis-p0/ 目录下）：
  python3 jarvis.py boot
  python3 jarvis.py health
  python3 jarvis.py list-tasks
  python3 jarvis.py list-tasks --id task-001

不调用任何模型，不联网，只用标准库。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import Core  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis", description="Jarvis P0 — 离线启动原型")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("boot", help="按固定顺序启动核心")
    sub.add_parser("health", help="健康检查")
    list_p = sub.add_parser("list-tasks", help="列出本地任务")
    list_p.add_argument("--id", dest="task_id", default=None, help="只显示指定任务")

    args = parser.parse_args(argv)
    base_dir = Path(__file__).resolve().parent
    core = Core(base_dir)

    if args.command == "boot":
        result = core.boot()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "health":
        result = core.health()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "ok" else 1
    if args.command == "list-tasks":
        # list-tasks 先保证库已初始化（幂等），再读
        core.store.init()
        if args.task_id:
            task = core.store.get_task(args.task_id)
            print(json.dumps(task, ensure_ascii=False, indent=2) if task else "null")
            return 0 if task else 1
        tasks = core.store.list_tasks()
        print(json.dumps(tasks, ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
