"""P2 测试：路由切换 + P1 遗留（回滚、导入）。

测试 1：配置切换 — 只改 config.yaml 的映射，模型就变，输出格式不变。
测试 2：故障切换 — 首选失败时切备选，审计里有 router_fallback。
测试 3：全失败降级 — 三家全失败时返回降级回答，不抛异常，degraded=true。
测试 4：审计记录 — 每次调用记模型、token、成本、耗时、结果 hash，hash 与内容一致。
测试 5：无 Key 降级 — 环境清空 API Key，router-call 仍成功，成本 0。
测试 6：一键改回 — 修正后再 rollback，召回恢复旧判断。
测试 7：导入验证 — 导出后清空库再导入，条数一致，重复导入不重复。
测试 8：P0/P1 回归 — boot/health/list-tasks/记忆召回仍通过。
"""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

P2_DIR = Path(__file__).resolve().parent.parent
PY = sys.executable


def run_cli(args, cwd=P2_DIR, env=None):
    merged = dict(os.environ)
    for key in list(merged):
        if "API_KEY" in key or "OPENAI" in key or "ANTHROPIC" in key:
            del merged[key]
    if env:
        merged.update(env)
    return subprocess.run(
        [PY, "jarvis.py", *args], cwd=cwd, env=merged,
        capture_output=True, text=True, timeout=30,
    )


def cli_json(args, cwd=P2_DIR):
    r = run_cli(args, cwd=cwd)
    assert r.returncode == 0, f"{args} failed: {r.stderr}"
    return json.loads(r.stdout)


def fresh_db():
    """每组测试前清空运行数据：jarvis.db、improvements.db、events.jsonl、
    data/backups/* 全部删除（P6：fresh_db 漏删 improvements.db 是 34/38 的根因），
    再 boot 重建。"""
    data_dir = P2_DIR / "data"
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
    path = P2_DIR / "data" / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestP2(unittest.TestCase):
    def test_1_config_switch_only(self):
        fresh_db()
        # 默认映射：fast -> stub-fast
        r1 = cli_json(["router-call", "--logical", "fast", "--prompt", "你好"])
        self.assertEqual(r1["model"], "stub-fast")
        self.assertEqual(set(r1.keys()) >= {"model", "content", "tokens_in", "tokens_out",
                                            "cost_usd", "latency_ms", "result_hash", "degraded"}, True)
        # 只改配置：复制目录，改 config.yaml 里 fast 的映射为 stub-capable
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "jarvis-p2-copy"
            shutil.copytree(P2_DIR, dest, ignore=shutil.ignore_patterns("__pycache__"))
            shutil.rmtree(dest / "data", ignore_errors=True)
            (dest / "data").mkdir(exist_ok=True)
            cfg = dest / "config.yaml"
            text = cfg.read_text(encoding="utf-8")
            text = text.replace("fast: stub-fast", "fast: stub-capable")
            cfg.write_text(text, encoding="utf-8")
            r2 = cli_json(["router-call", "--logical", "fast", "--prompt", "你好"], cwd=dest)
            self.assertEqual(r2["model"], "stub-capable")
            # 输出格式不变：同一组键
            self.assertEqual(set(r1.keys()), set(r2.keys()))

    def test_2_failure_fallback(self):
        fresh_db()
        r = cli_json(["router-call", "--logical", "fast", "--prompt", "你好",
                      "--fail", "stub-fast"])
        # 首选失败，应切到备选 stub-capable（不是 stub-fast），且不降级
        self.assertEqual(r["model"], "stub-capable")
        self.assertFalse(r["degraded"])
        self.assertEqual(r["attempted"], ["stub-fast", "stub-capable"])
        events = read_events()
        fallbacks = [e for e in events if e["event"] == "router_fallback"]
        self.assertEqual(len(fallbacks), 1)
        self.assertIn("stub-fast", fallbacks[0]["detail"])

    def test_3_all_fail_degraded(self):
        fresh_db()
        r = cli_json(["router-call", "--logical", "fast", "--prompt", "你好",
                      "--fail", "stub-fast,stub-capable,stub-local"])
        self.assertTrue(r["degraded"])
        self.assertEqual(r["model"], "local-rule")
        self.assertEqual(r["attempted"], ["stub-fast", "stub-capable", "stub-local"])
        self.assertIn("降级回答", r["content"])

    def test_4_audit_record(self):
        fresh_db()
        r = cli_json(["router-call", "--logical", "capable", "--prompt", "测试审计"])
        self.assertEqual(r["cost_usd"], 0.0)
        self.assertIn("tokens_in", r)
        self.assertIn("latency_ms", r)
        expected_hash = hashlib.sha256(r["content"].encode("utf-8")).hexdigest()
        self.assertEqual(r["result_hash"], expected_hash)
        events = read_events()
        calls = [e for e in events if e["event"] == "router_call"]
        self.assertEqual(len(calls), 1)
        detail = json.loads(calls[0]["detail"])
        self.assertEqual(detail["model"], "stub-capable")
        self.assertEqual(detail["result_hash"], expected_hash)

    def test_5_no_key_still_works(self):
        fresh_db()
        # 环境已清空 API Key（run_cli 负责），Stub 不需要 Key
        r = cli_json(["router-call", "--logical", "local", "--prompt", "无 Key 测试"])
        self.assertEqual(r["model"], "stub-local")
        self.assertEqual(r["cost_usd"], 0.0)
        self.assertFalse(r["degraded"])

    def test_6_rollback(self):
        fresh_db()
        cli_json(["memory-add", "--text", "用户喜欢吃辣", "--type", "inference",
                  "--confidence", "0.5", "--evidence-grade", "single_occurrence",
                  "--source", "推断：合成对话001", "--id", "mem-rb-001"])
        cli_json(["memory-correct", "--id", "mem-rb-001",
                  "--new-text", "用户不吃辣",
                  "--evidence", "用户在3次合成对话中明确说不吃辣"])
        # 修正后召回只有新判断
        self.assertEqual([f["text"] for f in cli_json(["memory-recall"])], ["用户不吃辣"])
        # 取 Correction id，一键改回
        with sqlite3.connect(P2_DIR / "data" / "jarvis.db") as conn:
            corr_id = conn.execute("SELECT id FROM corrections").fetchone()[0]
        result = cli_json(["memory-rollback", "--correction-id", corr_id])
        self.assertEqual(result["decision"], "rolled_back")
        # 改回后召回恢复旧判断
        self.assertEqual([f["text"] for f in cli_json(["memory-recall"])], ["用户喜欢吃辣"])

    def test_7_import_verification(self):
        fresh_db()
        cli_json(["memory-add", "--text", "用户目标是学会跑步", "--type", "fact",
                  "--confidence", "0.9", "--evidence-grade", "user_stated",
                  "--source", "user:合成对话001", "--id", "mem-imp-001"])
        cli_json(["memory-add", "--text", "用户喜欢游泳", "--type", "viewpoint",
                  "--confidence", "0.6", "--evidence-grade", "single_occurrence",
                  "--source", "user:合成对话002", "--id", "mem-imp-002"])
        out = P2_DIR / "data" / "memory-export.jsonl"
        exp = cli_json(["memory-export", "--out", str(out)])
        self.assertEqual(exp["count"], 2)
        # 清空库再导入
        fresh_db()
        self.assertEqual(cli_json(["memory-recall"]), [])
        imp = cli_json(["memory-import", "--in", str(out)])
        self.assertEqual(imp["imported"], 2)
        self.assertEqual(imp["lines"], 2)
        self.assertEqual(len(cli_json(["memory-recall"])), 2)
        # 重复导入不重复
        imp2 = cli_json(["memory-import", "--in", str(out)])
        self.assertEqual(imp2["imported"], 0)
        self.assertEqual(len(cli_json(["memory-recall"])), 2)

    def test_8_p0_p1_regression(self):
        fresh_db()
        boot = cli_json(["boot"])
        self.assertEqual(boot["status"], "ok")
        health = cli_json(["health"])
        self.assertEqual(health["status"], "ok")
        self.assertEqual(len(cli_json(["list-tasks"])), 5)
        cli_json(["memory-extract", "--dialogue", "我喜欢跑步",
                  "--source", "user:合成对话001"])
        self.assertEqual(len(cli_json(["memory-recall"])), 1)


if __name__ == "__main__":
    unittest.main()
