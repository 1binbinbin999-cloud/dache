"""P5 测试：自我改进流程。

测试 1：完整六步 — 提案（gate 加 "sudo rm"）→ 测试 → 评价 pass → 批准 → 部署 → 监控 healthy；部署后新注入被拦截；最后回滚恢复。
测试 2：未批准部署被拒 — 文件不变，审计有 improvement_denied。
测试 3：未通过评价不批准 — 破坏性提案测试 fail，批准被拒。
测试 4：不可变规则拒提案 — 改 policy 允许 delete_file，提案阶段直接拒绝。
测试 5：回滚 — 部署后回滚，文件恢复原样，状态 rolled_back。
测试 6：P0–P4 回归 — boot/health/路由/网关/规划器仍通过。
"""

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

P5_DIR = Path(__file__).resolve().parent.parent
PY = sys.executable

GATE_OLD = '"rm -rf",'
GATE_NEW = '"rm -rf", "sudo rm",'


def run_cli(args, cwd=P5_DIR, env=None):
    merged = dict(os.environ)
    for key in list(merged):
        if "API_KEY" in key or "OPENAI" in key or "ANTHROPIC" in key:
            del merged[key]
    if env:
        merged.update(env)
    return subprocess.run(
        [PY, "jarvis.py", *args], cwd=cwd, env=merged,
        capture_output=True, text=True, timeout=180,
    )


def cli_json(args, cwd=P5_DIR):
    r = run_cli(args, cwd=cwd)
    assert r.returncode == 0, f"{args} failed: {r.stderr}"
    return json.loads(r.stdout)


def fresh_db():
    """每组测试前清空运行数据：jarvis.db、improvements.db、events.jsonl、
    data/backups/* 全部删除（P6：fresh_db 漏删 improvements.db 是 34/38 的根因），
    再 boot 重建。"""
    data_dir = P5_DIR / "data"
    for name in ("jarvis.db", "improvements.db", "events.jsonl",
                 "events.checkpoint.json"):
        p = data_dir / name
        if p.exists():
            p.unlink()
    backups = data_dir / "backups"
    if backups.exists():
        shutil.rmtree(backups)
    cli_json(["boot"])


def read_events():
    path = P5_DIR / "data" / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def memory_has(pattern: str) -> bool:
    return pattern in (P5_DIR / "memory.py").read_text(encoding="utf-8")


def propose_gate(pid: str) -> dict:
    return cli_json(["improve-propose", "--title", "gate 增加 sudo rm",
                     "--target", "memory.py", "--old-text", GATE_OLD,
                     "--new-text", GATE_NEW, "--id", pid])


class TestP5(unittest.TestCase):
    def setUp(self):
        # P6：测试失败兜底。部署类测试直接改真实源码（memory.py 等）；
        # 任何中途失败都必须把源码恢复原样，否则失败会污染源码树
        #（Gemini 指出的第二条泄漏链，已复现）。快照只覆盖白名单目标文件。
        self._snapshot = {}
        for name in ("memory.py", "router.py", "planner.py"):
            p = P5_DIR / name
            self._snapshot[name] = p.read_bytes()

    def tearDown(self):
        for name, content in self._snapshot.items():
            p = P5_DIR / name
            if p.read_bytes() != content:
                p.write_bytes(content)

    def test_1_full_six_steps(self):
        fresh_db()
        self.assertFalse(memory_has('"sudo rm"'))
        proposed = propose_gate("imp-t1")
        self.assertEqual(proposed["decision"], "proposed")
        tested = cli_json(["improve-test", "--id", "imp-t1"])
        self.assertEqual(tested["verdict"], "pass")
        approved = cli_json(["improve-approve", "--id", "imp-t1"])
        self.assertEqual(approved["decision"], "approved")
        deployed = cli_json(["improve-deploy", "--id", "imp-t1"])
        self.assertEqual(deployed["decision"], "deployed")
        self.assertTrue(memory_has('"sudo rm"'))
        # 部署后新注入被拦截
        r = cli_json(["memory-add", "--text", "sudo rm -rf /", "--type", "fact",
                      "--confidence", "0.9", "--evidence-grade", "single_occurrence",
                      "--source", "test:P5"])
        self.assertEqual(r["decision"], "quarantined")
        monitored = cli_json(["improve-monitor", "--id", "imp-t1"])
        self.assertEqual(monitored["decision"], "healthy")
        status = cli_json(["improve-status", "--id", "imp-t1"])
        self.assertEqual(status["status"], "deployed")
        self.assertEqual(len(status["evaluations"]), 1)
        self.assertEqual(len(status["deployments"]), 1)
        # 收尾：回滚，恢复干净状态供后续测试
        rolled = cli_json(["improve-rollback", "--id", "imp-t1"])
        self.assertEqual(rolled["decision"], "rolled_back")
        self.assertFalse(memory_has('"sudo rm"'))

    def test_2_deploy_without_approval_denied(self):
        fresh_db()
        propose_gate("imp-t2")
        r = run_cli(["improve-deploy", "--id", "imp-t2"])
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stdout)["decision"], "denied")
        self.assertFalse(memory_has('"sudo rm"'))
        events = read_events()
        self.assertTrue(any(e["event"] == "improvement_denied" for e in events))

    def test_3_approve_without_pass_denied(self):
        fresh_db()
        # 破坏性提案：把 "用户喜欢" 加进注入模式，100 条合成对话会被误拦
        cli_json(["improve-propose", "--title", "破坏性提案",
                  "--target", "memory.py", "--old-text", GATE_OLD,
                  "--new-text", '"rm -rf", "用户喜欢",', "--id", "imp-t3"])
        tested = cli_json(["improve-test", "--id", "imp-t3"])
        self.assertEqual(tested["verdict"], "fail")
        r = run_cli(["improve-approve", "--id", "imp-t3"])
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stdout)["reason"], "evaluation_not_pass")
        self.assertFalse(memory_has('"用户喜欢",'))

    def test_4_immutable_rule_rejected(self):
        fresh_db()
        r = run_cli(["improve-propose", "--title", "放开 delete_file",
                     "--target", "config.yaml",
                     "--old-text", "  allowed_tools:",
                     "--new-text", "  allowed_tools:\n    - delete_file",
                     "--id", "imp-t4"])
        self.assertNotEqual(r.returncode, 0)
        result = json.loads(r.stdout)
        self.assertEqual(result["decision"], "rejected")
        self.assertIn("immutable_rule", result["reason"])

    def test_5_rollback(self):
        fresh_db()
        propose_gate("imp-t5")
        cli_json(["improve-test", "--id", "imp-t5"])
        cli_json(["improve-approve", "--id", "imp-t5"])
        cli_json(["improve-deploy", "--id", "imp-t5"])
        self.assertTrue(memory_has('"sudo rm"'))
        rolled = cli_json(["improve-rollback", "--id", "imp-t5"])
        self.assertEqual(rolled["decision"], "rolled_back")
        self.assertFalse(memory_has('"sudo rm"'))
        status = cli_json(["improve-status", "--id", "imp-t5"])
        self.assertEqual(status["status"], "rolled_back")
        # 二次回滚被拒
        r = run_cli(["improve-rollback", "--id", "imp-t5"])
        self.assertNotEqual(r.returncode, 0)

    def test_6_p0_p1_p2_p3_p4_regression(self):
        fresh_db()
        self.assertEqual(cli_json(["boot"])["status"], "ok")
        self.assertEqual(cli_json(["health"])["status"], "ok")
        self.assertEqual(cli_json(["router-call", "--logical", "fast",
                                   "--prompt", "回归"])["model"], "stub-fast")
        self.assertEqual(cli_json(["tool-call", "--tool", "list_tasks"])["decision"], "allowed")
        cli_json(["plan-create", "--goal", "早上简报", "--template", "morning-brief",
                  "--id", "plan-p5"])
        self.assertEqual(cli_json(["plan-run", "--id", "plan-p5"])["decision"], "done")


if __name__ == "__main__":
    unittest.main()
