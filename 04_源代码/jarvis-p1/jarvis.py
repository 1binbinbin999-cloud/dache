#!/usr/bin/env python3
"""Jarvis P1 CLI：boot / health / list-tasks / memory-*

用法（在 jarvis-p1/ 目录下）：
  python3 jarvis.py boot
  python3 jarvis.py health
  python3 jarvis.py list-tasks
  python3 jarvis.py memory-add --text "用户喜欢跑步" --type viewpoint --confidence 0.6 --evidence-grade single_occurrence --source "user:合成对话001"
  python3 jarvis.py memory-extract --dialogue "我喜欢跑步" --source "user:合成对话001"
  python3 jarvis.py memory-recall [--query 跑步]
  python3 jarvis.py memory-correct --id mem-xxx --new-text "用户不喜欢跑步" --evidence "用户在3次对话中明确说不喜欢跑步"
  python3 jarvis.py memory-export --out data/memory-export.jsonl

不调用任何模型，不联网。提取只用规则模板。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import Core  # noqa: E402
from memory import MemoryStore, extract_facts  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis", description="Jarvis P1 — 记忆治理原型")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("boot", help="按固定顺序启动核心")
    sub.add_parser("health", help="健康检查")
    list_p = sub.add_parser("list-tasks", help="列出本地任务")
    list_p.add_argument("--id", dest="task_id", default=None)

    add_p = sub.add_parser("memory-add", help="经写时 gate 写入一条记忆")
    add_p.add_argument("--text", required=True)
    add_p.add_argument("--type", required=True, choices=["fact", "viewpoint", "inference", "to_verify"])
    add_p.add_argument("--confidence", required=True, type=float)
    add_p.add_argument("--evidence-grade", required=True,
                       choices=["user_stated", "single_occurrence", "multiple_occurrences", "verified"])
    add_p.add_argument("--source", required=True)
    add_p.add_argument("--valid-until", default=None)
    add_p.add_argument("--id", dest="fact_id", default=None)

    ext_p = sub.add_parser("memory-extract", help="用规则模板从对话提取事实并写入")
    ext_p.add_argument("--dialogue", required=True)
    ext_p.add_argument("--source", required=True)

    rec_p = sub.add_parser("memory-recall", help="只召回当前事实（verified/provisional）")
    rec_p.add_argument("--query", default=None)

    cor_p = sub.add_parser("memory-correct", help="修正一条记忆：旧标 superseded，新另起一条")
    cor_p.add_argument("--id", required=True)
    cor_p.add_argument("--new-text", required=True)
    cor_p.add_argument("--evidence", required=True)

    exp_p = sub.add_parser("memory-export", help="导出记忆为 JSONL（含 schema_version）")
    exp_p.add_argument("--out", required=True)

    args = parser.parse_args(argv)
    base_dir = Path(__file__).resolve().parent
    core = Core(base_dir)
    memory = MemoryStore(base_dir / "data" / "jarvis.db")

    if args.command == "boot":
        print(json.dumps(core.boot(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "health":
        result = core.health()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "ok" else 1
    if args.command == "list-tasks":
        core.store.init()
        if args.task_id:
            task = core.store.get_task(args.task_id)
            print(json.dumps(task, ensure_ascii=False, indent=2) if task else "null")
            return 0 if task else 1
        print(json.dumps(core.store.list_tasks(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "memory-add":
        result = memory.add_fact(
            args.text, args.type, args.confidence, args.evidence_grade,
            args.source, valid_until=args.valid_until, fact_id=args.fact_id,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "memory-extract":
        facts = extract_facts(args.dialogue, args.source)
        results = [memory.add_fact(**f) for f in facts]
        print(json.dumps({"extracted": facts, "results": results}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "memory-recall":
        print(json.dumps(memory.list_current(args.query), ensure_ascii=False, indent=2))
        return 0
    if args.command == "memory-correct":
        result = memory.correct_fact(args.id, args.new_text, args.evidence)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["decision"] == "corrected" else 1
    if args.command == "memory-export":
        result = memory.export_jsonl(Path(args.out))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
