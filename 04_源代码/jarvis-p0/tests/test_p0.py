"""P0 测试：4 项，全部用 subprocess 跑真实 CLI，不 mock 核心路径。

测试 1：正常启动
测试 2：无 Key / 无网启动（清空环境变量中的 API Key，断开模型配置已在 config 中）
测试 3：重启幂等（boot 两次，任务数仍为 5，事件日志不丢）
测试 4：复制目录导出启动（复制整个 jarvis-p0 到临时目录，仍能 boot + list-tasks）
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

P0_DIR = Path(__file__).resolve().parent.parent
PY = sys.executable


def run_cli(args, cwd=P0_DIR, env=None):
    merged = dict(os.environ)
    # 测试 2 的关键：显式清空可能存在的 API Key，证明启动不依赖它们
    for key in list(merged):
        if "API_KEY" in key or "OPENAI" in key or "ANTHROPIC" in key:
            del merged[key]
    if env:
        merged.update(env)
    result = subprocess.run(
        [PY, "jarvis.py", *args],
        cwd=cwd,
        env=merged,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result


class TestP0(unittest.TestCase):
    def test_1_normal_boot(self):
        r = run_cli(["boot"])
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(
            data["steps"], ["config", "database", "policy", "audit", "scheduler"]
        )
        self.assertEqual(data["tasks"], 5)
        self.assertEqual(data["forbidden_imports_loaded"], [])

        r = run_cli(["health"])
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertEqual(data["status"], "ok")
        self.assertTrue(all(data["checks"].values()), data["checks"])

    def test_2_no_key_no_network_boot_and_list(self):
        # 无 Key：环境已清空；无网：P0 本来就不联网，config 明确禁止
        r = run_cli(["boot"])
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run_cli(["list-tasks"])
        self.assertEqual(r.returncode, 0, r.stderr)
        tasks = json.loads(r.stdout)
        self.assertEqual(len(tasks), 5)
        self.assertEqual([t["id"] for t in tasks], [f"task-00{i}" for i in range(1, 6)])

    def test_3_restart_idempotent(self):
        run_cli(["boot"])
        before = json.loads(run_cli(["list-tasks"]).stdout)
        events_before = (P0_DIR / "data" / "events.jsonl").read_text(encoding="utf-8")
        run_cli(["boot"])
        after = json.loads(run_cli(["list-tasks"]).stdout)
        self.assertEqual(len(before), 5)
        self.assertEqual(len(after), 5)
        self.assertEqual(before, after)
        events_after = (P0_DIR / "data" / "events.jsonl").read_text(encoding="utf-8")
        # 事件日志只追加，不丢旧记录
        self.assertTrue(events_after.startswith(events_before))
        self.assertGreater(len(events_after), len(events_before))

    def test_4_copy_dir_still_boots(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "jarvis-p0-copy"
            shutil.copytree(P0_DIR, dest, ignore=shutil.ignore_patterns("__pycache__"))
            # 删掉复制过去的运行数据，模拟干净环境
            shutil.rmtree(dest / "data", ignore_errors=True)
            (dest / "data").mkdir(exist_ok=True)
            r = run_cli(["boot"], cwd=dest)
            self.assertEqual(r.returncode, 0, r.stderr)
            data = json.loads(r.stdout)
            self.assertEqual(data["status"], "ok")
            self.assertEqual(data["tasks"], 5)
            r = run_cli(["list-tasks"], cwd=dest)
            self.assertEqual(len(json.loads(r.stdout)), 5)


if __name__ == "__main__":
    unittest.main()
