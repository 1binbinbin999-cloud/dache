"""P4 测试：规划器。

测试 1：三步任务全程经网关，审计链完整。
测试 2：拒绝一步即停 — 第二步 delete_file 被拒，任务 denied，第三步不执行。
测试 3：未知工具拒建 — 建任务时就拒绝，不等到运行时。
测试 4：重跑一致 — 只读任务跑两次，每步结果 hash 相同。
测试 5：带固定 id 的记忆写入重跑不重复。
测试 6：actor 区分 — 规划器调用审计 actor=planner，用户直接调 actor=user。
测试 7：P0/P1/P2/P3 回归 — boot/health/router-call/网关/真断网仍通过。
"""

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

P4_DIR = Path(__file__).resolve().parent.parent
PY = sys.executable


def run_cli(args, cwd=P4_DIR, env=None):
    merged = dict(os.environ)
    for key in list(merged):
        if "API_KEY" in key or "OPENAI" in key or "ANTHROPIC" in key:
            del merged[key]
    if env:
        merged.update(env)
    return subprocess.run(
        [PY, "jarvis.py", *args], cwd=cwd, env=merged,
        capture_output=True, text=True, timeout=60,
    )


def cli_json(args, cwd=P4_DIR):
    r = run_cli(args, cwd=cwd)
    assert r.returncode == 0, f"{args} failed: {r.stderr}"
    return json.loads(r.stdout)


def fresh_db():
    """每组测试前清空运行数据：jarvis.db、improvements.db、events.jsonl、
    data/backups/* 全部删除（P6：fresh_db 漏删 improvements.db 是 34/38 的根因），
    再 boot 重建。"""
    data_dir = P4_DIR / "data"
    for name in ("jarvis.db", "improvements.db", "events.jsonl"):
        p = data_dir / name
        if p.exists():
            p.unlink()
    backups = data_dir / "backups"
    if backups.exists():
        shutil.rmtree(backups)
    cli_json(["boot"])


def read_events():
    path = P4_DIR / "data" / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


THREE_STEPS = json.dumps([
    {"tool": "memory-recall", "args": {}},
    {"tool": "router-call", "args": {"logical": "fast", "prompt": "你好"}},
    {"tool": "list_tasks", "args": {}},
], ensure_ascii=False)


class TestP4(unittest.TestCase):
    def test_1_three_step_plan(self):
        fresh_db()
        created = cli_json(["plan-create", "--goal", "早上简报",
                            "--steps-json", THREE_STEPS, "--id", "plan-t1"])
        self.assertEqual(created["decision"], "created")
        self.assertEqual(created["steps"], 3)
        result = cli_json(["plan-run", "--id", "plan-t1"])
        self.assertEqual(result["decision"], "done")
        status = cli_json(["plan-status", "--id", "plan-t1"])
        self.assertEqual(status["status"], "done")
        self.assertEqual([s["status"] for s in status["steps"]],
                         ["done", "done", "done"])
        events = read_events()
        self.assertEqual(len([e for e in events if e["event"] == "tool_call"]), 3)
        self.assertEqual(len([e for e in events if e["event"] == "router_call"]), 1)

    def test_2_denied_step_stops_plan(self):
        fresh_db()
        steps = json.dumps([
            {"tool": "list_tasks", "args": {}},
            {"tool": "delete_file", "args": {"path": "data/jarvis.db"}},
            {"tool": "list_tasks", "args": {}},
        ], ensure_ascii=False)
        cli_json(["plan-create", "--goal", "含拒绝步骤的任务",
                  "--steps-json", steps, "--id", "plan-t2"])
        r = run_cli(["plan-run", "--id", "plan-t2"])
        self.assertNotEqual(r.returncode, 0)
        result = json.loads(r.stdout)
        self.assertEqual(result["decision"], "denied")
        status = cli_json(["plan-status", "--id", "plan-t2"])
        self.assertEqual(status["status"], "denied")
        self.assertEqual([s["status"] for s in status["steps"]],
                         ["done", "denied", "pending"])
        events = read_events()
        self.assertTrue(any(e["event"] == "tool_denied" for e in events))
        self.assertTrue((P4_DIR / "data" / "jarvis.db").exists())

    def test_3_unknown_tool_rejected_at_creation(self):
        fresh_db()
        steps = json.dumps([{"tool": "fly_to_moon", "args": {}}], ensure_ascii=False)
        r = run_cli(["plan-create", "--goal", "未知工具",
                     "--steps-json", steps, "--id", "plan-t3"])
        self.assertNotEqual(r.returncode, 0)
        result = json.loads(r.stdout)
        self.assertEqual(result["decision"], "rejected")
        self.assertIn("unknown_tool", result["reason"])
        # 任务没建出来
        r2 = run_cli(["plan-status", "--id", "plan-t3"])
        self.assertNotEqual(r2.returncode, 0)

    def test_4_rerun_consistent(self):
        fresh_db()
        cli_json(["plan-create", "--goal", "只读重跑",
                  "--steps-json", THREE_STEPS, "--id", "plan-t4"])
        cli_json(["plan-run", "--id", "plan-t4"])
        first = cli_json(["plan-status", "--id", "plan-t4"])
        first_hashes = [s["result_hash"] for s in first["steps"]]
        cli_json(["plan-run", "--id", "plan-t4"])
        second = cli_json(["plan-status", "--id", "plan-t4"])
        self.assertEqual(second["status"], "done")
        self.assertEqual([s["result_hash"] for s in second["steps"]], first_hashes)

    def test_5_fixed_id_memory_add_rerun_no_duplicate(self):
        fresh_db()
        cli_json(["plan-create", "--goal", "学习并记录",
                  "--template", "learn-and-record", "--id", "plan-t5"])
        cli_json(["plan-run", "--id", "plan-t5"])
        self.assertEqual(len(cli_json(["memory-recall"])), 1)
        cli_json(["plan-run", "--id", "plan-t5"])
        self.assertEqual(len(cli_json(["memory-recall"])), 1)
        status = cli_json(["plan-status", "--id", "plan-t5"])
        self.assertEqual(status["status"], "done")

    def test_6_actor_distinction(self):
        fresh_db()
        cli_json(["tool-call", "--tool", "list_tasks"])
        cli_json(["plan-create", "--goal", "早上简报",
                  "--steps-json", THREE_STEPS, "--id", "plan-t6"])
        cli_json(["plan-run", "--id", "plan-t6"])
        events = read_events()
        tool_calls = [json.loads(e["detail"]) for e in events if e["event"] == "tool_call"]
        actors = [d["actor"] for d in tool_calls]
        self.assertIn("user", actors)
        self.assertIn("planner", actors)
        # 脱敏摘要：只记键名，不记值
        for d in tool_calls:
            self.assertIn("args_summary", d)
            self.assertNotIn("你好", json.dumps(d, ensure_ascii=False))

    def test_7_p0_p1_p2_p3_regression(self):
        fresh_db()
        self.assertEqual(cli_json(["boot"])["status"], "ok")
        self.assertEqual(cli_json(["health"])["status"], "ok")
        self.assertEqual(cli_json(["router-call", "--logical", "fast",
                                   "--prompt", "回归"])["model"], "stub-fast")
        self.assertEqual(cli_json(["tool-call", "--tool", "list_tasks"])["decision"], "allowed")
        sandbox = cli_json(["sandbox-check"])
        self.assertTrue(sandbox["ok"])


if __name__ == "__main__":
    unittest.main()
