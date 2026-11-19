#!/usr/bin/env python3
"""Jarvis P2 CLI：boot / health / list-tasks / memory-* / router-*

用法（在 jarvis-p2/ 目录下）：
  python3 jarvis.py boot
  python3 jarvis.py router-call --logical fast --prompt "你好"
  python3 jarvis.py router-call --logical fast --prompt "你好" --fail stub-fast
  python3 jarvis.py router-list
  python3 jarvis.py memory-rollback --correction-id corr-xxx
  python3 jarvis.py memory-import --in data/memory-export.jsonl

P2 只用 Stub，不接真实 API，不调用任何模型供应商，不联网。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import Core  # noqa: E402
from gateway import Gateway  # noqa: E402
from improvement import ImprovementStore  # noqa: E402
from memory import MemoryStore, extract_facts  # noqa: E402
from planner import TEMPLATES, Planner  # noqa: E402
from router import Router  # noqa: E402
from sandbox import sandbox_check  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis", description="Jarvis P2 — 路由切换原型")
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

    rb_p = sub.add_parser("memory-rollback", help="一键改回：按 Correction 记录回滚一次修正")
    rb_p.add_argument("--correction-id", required=True)

    imp_p = sub.add_parser("memory-import", help="导入导出的 JSONL，校验 schema_version，幂等写入")
    imp_p.add_argument("--in", dest="in_path", required=True)

    rc_p = sub.add_parser("router-call", help="经 Model Router 调用逻辑模型（P2 为 Stub）")
    rc_p.add_argument("--logical", required=True,
                      choices=["fast", "capable", "reasoning", "local", "embedding"])
    rc_p.add_argument("--prompt", required=True)
    rc_p.add_argument("--fail", default="",
                      help="模拟失败的供应商，逗号分隔，如 stub-fast,stub-capable")

    sub.add_parser("router-list", help="显示逻辑模型名映射与切换链")

    tc_p = sub.add_parser("tool-call", help="经 Tool Gateway 调用工具（权限检查在执行前）")
    tc_p.add_argument("--tool", required=True)
    tc_p.add_argument("--args-json", default="{}",
                      help="工具参数，JSON 对象，如 '{\"id\": \"task-001\"}'")

    sub.add_parser("tool-list", help="列出已注册工具与是否允许")

    sub.add_parser("sandbox-check", help="真断网演练：在 unshare 命名空间内跑 boot + router-call")

    pc_p = sub.add_parser("plan-create", help="建任务：目标 + 有序步骤（每步绑一个已注册工具）")
    pc_p.add_argument("--goal", required=True)
    pc_p.add_argument("--steps-json", default=None,
                      help="步骤 JSON，如 '[{\"tool\": \"list_tasks\", \"args\": {}}]'")
    pc_p.add_argument("--template", default=None,
                      choices=["morning-brief", "learn-and-record"])
    pc_p.add_argument("--id", dest="plan_id", default=None)

    pr_p = sub.add_parser("plan-run", help="按顺序执行任务，失败一步即停")
    pr_p.add_argument("--id", required=True)

    ps_p = sub.add_parser("plan-status", help="查看任务与每步状态")
    ps_p.add_argument("--id", required=True)

    sub.add_parser("plan-list", help="列出全部任务")

    ip_p = sub.add_parser("improve-propose", help="提案：改动以 (目标文件, 旧文本, 新文本) 记入")
    ip_p.add_argument("--title", required=True)
    ip_p.add_argument("--target", required=True)
    ip_p.add_argument("--old-text", required=True)
    ip_p.add_argument("--new-text", required=True)
    ip_p.add_argument("--id", dest="proposal_id", default=None)

    for name, help_text in [
        ("improve-test", "测试：在临时复制目录里应用改动并跑回归门"),
        ("improve-approve", "批准：只有评价 pass 的提案才能批（用户显式动作）"),
        ("improve-deploy", "部署：只有 approved 的提案能部署，先备份"),
        ("improve-rollback", "回滚：用部署备份恢复原文件"),
        ("improve-monitor", "监控：部署后跑 boot + health + 注入测试"),
    ]:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--id", required=True)

    is_p = sub.add_parser("improve-status", help="查看提案、评价与部署记录")
    is_p.add_argument("--id", required=True)

    sub.add_parser("improve-list", help="列出全部提案")

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
    if args.command == "memory-rollback":
        result = memory.rollback_correction(args.correction_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["decision"] == "rolled_back" else 1
    if args.command == "memory-import":
        result = memory.import_jsonl(Path(args.in_path))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "router-call":
        config = core.load_config()
        router = Router(config, core.event_log)
        fail = tuple(p.strip() for p in args.fail.split(",") if p.strip())
        result = router.complete(args.logical, args.prompt, fail_providers=fail)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "router-list":
        config = core.load_config()
        router = Router(config, core.event_log)
        chains = {logical: router.chain_for(logical) for logical in router.mapping}
        print(json.dumps({"mapping": router.mapping, "chains": chains},
                         ensure_ascii=False, indent=2))
        return 0
    if args.command == "tool-call":
        config = core.load_config()
        gateway = Gateway(config, core, memory, core.event_log)
        try:
            tool_args = json.loads(args.args_json)
        except json.JSONDecodeError as exc:
            print(json.dumps({"decision": "denied", "reason": f"bad_args_json:{exc}"},
                             ensure_ascii=False, indent=2))
            return 1
        result = gateway.execute(args.tool, tool_args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["decision"] == "allowed" else 1
    if args.command == "tool-list":
        config = core.load_config()
        gateway = Gateway(config, core, memory, core.event_log)
        print(json.dumps(gateway.list_tools(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "sandbox-check":
        result = sandbox_check(base_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    if args.command == "plan-create":
        config = core.load_config()
        gateway = Gateway(config, core, memory, core.event_log)
        planner = Planner(base_dir / "data" / "jarvis.db", gateway)
        if args.template:
            steps = TEMPLATES[args.template]
        elif args.steps_json:
            try:
                steps = json.loads(args.steps_json)
            except json.JSONDecodeError as exc:
                print(json.dumps({"decision": "rejected", "reason": f"bad_steps_json:{exc}"},
                                 ensure_ascii=False, indent=2))
                return 1
        else:
            print(json.dumps({"decision": "rejected",
                              "reason": "need --steps-json or --template"},
                             ensure_ascii=False, indent=2))
            return 1
        result = planner.create(args.goal, steps, plan_id=args.plan_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["decision"] == "created" else 1
    if args.command == "plan-run":
        config = core.load_config()
        gateway = Gateway(config, core, memory, core.event_log)
        planner = Planner(base_dir / "data" / "jarvis.db", gateway)
        result = planner.run(args.id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["decision"] == "done" else 1
    if args.command == "plan-status":
        config = core.load_config()
        gateway = Gateway(config, core, memory, core.event_log)
        planner = Planner(base_dir / "data" / "jarvis.db", gateway)
        result = planner.status(args.id)
        if result is None:
            print("null")
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "plan-list":
        config = core.load_config()
        gateway = Gateway(config, core, memory, core.event_log)
        planner = Planner(base_dir / "data" / "jarvis.db", gateway)
        print(json.dumps(planner.list_plans(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "improve-propose":
        store = ImprovementStore(base_dir, base_dir / "data" / "improvements.db", core.event_log)
        result = store.propose(args.title, args.target, args.old_text,
                               args.new_text, proposal_id=args.proposal_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["decision"] == "proposed" else 1
    if args.command in ("improve-test", "improve-approve", "improve-deploy",
                        "improve-rollback", "improve-monitor"):
        store = ImprovementStore(base_dir, base_dir / "data" / "improvements.db", core.event_log)
        method = {"improve-test": store.test, "improve-approve": store.approve,
                  "improve-deploy": store.deploy, "improve-rollback": store.rollback,
                  "improve-monitor": store.monitor}[args.command]
        result = method(args.id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        ok_decisions = {"evaluated", "approved", "deployed", "rolled_back", "healthy"}
        return 0 if result["decision"] in ok_decisions else 1
    if args.command == "improve-status":
        store = ImprovementStore(base_dir, base_dir / "data" / "improvements.db", core.event_log)
        result = store.status(args.id)
        if result is None:
            print("null")
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "improve-list":
        store = ImprovementStore(base_dir, base_dir / "data" / "improvements.db", core.event_log)
        print(json.dumps(store.list_proposals(), ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
