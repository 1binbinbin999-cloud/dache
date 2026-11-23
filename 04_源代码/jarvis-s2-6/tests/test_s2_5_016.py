"""S2-5 测试：JIS-016 拆分提示词改进（候选 a）+ 探查原文校验。

S2-4 实测：qwen2.5:0.5b 严格 1/10（仅 #4）、宽松 4/10，用户
2026-11-21 13:20 判不通过。失败模式：读写方向搞反、参数 schema
全是模型自造、复合目标压扁成单步透传、次要动作缺失、外部工具误用。

本轮候选 (a)：只改 planner.py::decompose() 的提示词——写明每工具
的真实参数契约（与 gateway.py::_dispatch 一致）、读写方向规则、
复合目标全覆盖规则。校验逻辑（白名单 + args 是对象）未动。

探查结果（人工评价，见 S2-5 汇报；阈值用户未定，本文件不宣判
通过/不通过）：
- (a) 新提示词 + 0.5b：严格 1/10（仅 #2 全对）、宽松 6/10
  （#2、#3、#4、#6、#7、#10）。自造字段 0 次；新主导失败模式为
  无意义的 memory-recall 步骤（8/10 目标出现）与写动作丢失/反向
  （#1、#5、#9）。
- (b) 7b 结果见本文件 test_09 与 S2-5 汇报：严格 4/10
  （#2、#4、#5、#6）、宽松 8/10（#1、#2、#3、#4、#5、#6、#9、#10）；
  #7、#8 因模型输出缺最后的 } 而 unparsable（两次重跑均复现）。
- (c) Gemini 结果见本文件 test_10 与 S2-5 汇报 §五：严格 8/10
  （#1-#8）、宽松 8/10；#9、#10 因模型输出截断而
  unparsable_output（与 (b) #7、#8 同类）；8 条已分解的均无方向
  反转、无自造字段、无编造的具体内容。真实 Gemini 调用 11 次
  （#10 限流重试 1 次），$0。

本文件只用假模型与已存档的探查原文，不依赖 Ollama、不发真实
请求；（c) 探查原文是 2026-11-23 实测所得。
"""

import json
import unittest
from pathlib import Path

S2_DIR = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(S2_DIR))

from gateway import Gateway  # noqa: E402
from planner import DECOMPOSE_ALLOWED_TOOLS, Planner  # noqa: E402
from core import Core  # noqa: E402


class FakeLog:
    def append(self, *args, **kwargs):
        return True


# 各工具真实参数契约（gateway.py::_dispatch）；提示词必须与之一致，
# 探查原文里出现的 args 键也必须落在这个集合里（自造字段 = 违规）。
REAL_ARG_KEYS = {
    "list_tasks": set(),
    "read_task": {"id"},
    "memory-recall": {"query"},
    "memory-add": {"text", "type", "confidence", "evidence_grade",
                   "source", "id", "valid_until"},
    "router-call": {"logical", "prompt", "fail"},
}


def _planner():
    core = Core(S2_DIR)
    config = core.load_config()
    gateway = Gateway(config, core, None, FakeLog())
    return Planner(S2_DIR / "data" / "jarvis.db", gateway)


def _canned_router(content=None, degraded=False):
    class R:
        def __init__(self):
            self.captured_prompt = None

        def complete(self, logical, prompt):
            self.captured_prompt = prompt
            return {"model": "canned", "content": content,
                    "tokens_in": 1, "tokens_out": 1, "cost_usd": 0.0,
                    "latency_ms": 1, "degraded": degraded,
                    "attempted": ["canned"] if degraded else []}
    return R()


class TestS25PromptSchemas(unittest.TestCase):
    """新提示词必须写明真实工具契约与方向规则。"""

    def test_01_prompt_contains_real_schemas(self):
        router = _canned_router('{"steps": []}')
        _planner().decompose("查一下跑步的记忆", router)
        prompt = router.captured_prompt
        self.assertIsNotNone(prompt)
        # memory-add 五个必填字段全在提示词里
        for field in ("text", "type", "confidence", "evidence_grade",
                      "source"):
            self.assertIn(field, prompt)
        # router-call / memory-recall / read_task 的真实字段
        self.assertIn('"logical": "fast"', prompt)
        self.assertIn('"prompt":', prompt)
        self.assertIn('"query":', prompt)
        self.assertIn('"id":', prompt)
        # 方向规则：查/检索是读，记住/记录是写
        self.assertIn("绝不许用 memory-add", prompt)
        self.assertIn("才是写操作", prompt)
        # 复合目标全覆盖 + 禁止整句原文塞参数
        self.assertIn("每个动作都要有对应的步骤", prompt)
        self.assertIn("不许把整句目标原文塞进", prompt)

    def test_02_good_decomposition_new_schema_passes(self):
        good = json.dumps({"steps": [
            {"tool": "memory-recall", "args": {"query": "跑步"}},
            {"tool": "memory-add", "args": {
                "text": "用户喜欢喝茶", "type": "fact", "confidence": 0.6,
                "evidence_grade": "single_occurrence",
                "source": "planner"}}]}, ensure_ascii=False)
        result = _planner().decompose("查跑步并记住喝茶", _canned_router(good))
        self.assertEqual(result["decision"], "decomposed")
        self.assertEqual(len(result["steps"]), 2)
        add = result["steps"][1]["args"]
        self.assertEqual(set(add.keys()),
                         {"text", "type", "confidence", "evidence_grade",
                          "source"})

    def test_03_bad_tool_still_rejected(self):
        bad = '{"steps": [{"tool": "delete_file", "args": {"path": "x"}}]}'
        result = _planner().decompose("删文件", _canned_router(bad))
        self.assertEqual(result["decision"], "failed")
        self.assertIn("unknown_tool", result["reason"])

    def test_04_garbage_still_unparsable(self):
        result = _planner().decompose("早上问候", _canned_router("我不知道怎么拆"))
        self.assertEqual(result["decision"], "failed")
        self.assertEqual(result["reason"], "unparsable_output")

    def test_05_degraded_still_failed(self):
        result = _planner().decompose("早上问候",
                                       _canned_router("降级回答", degraded=True))
        self.assertEqual(result["decision"], "failed")
        self.assertEqual(result["reason"], "model_degraded")

    def test_06_empty_goal_rejected(self):
        result = _planner().decompose("   ", _canned_router('{"steps": []}'))
        self.assertEqual(result["decision"], "rejected")
        self.assertEqual(result["reason"], "empty_goal")


def _load_probe(name):
    path = S2_DIR / "tests" / "data" / name
    if not path.exists():
        return None
    goals = []
    executions = []
    with open(path) as f:
        for line in f:
            rec = json.loads(line)
            if "n" in rec:
                goals.append(rec)
            elif rec.get("kind") == "execution":
                executions.append(rec)
    return goals, executions


class TestS25ProbeA(unittest.TestCase):
    """候选 (a) 探查原文结构校验（冻结文件，只验结构与字段契约，
    不在这里做人工评分——评分见 S2-5 汇报）。"""

    def test_07_probe_a_ten_goals_all_decomposed_whitelisted(self):
        loaded = _load_probe("decompose-probe-s2-5-a-prompt-0.5b.jsonl")
        self.assertIsNotNone(loaded, "候选 (a) 探查文件缺失")
        goals, executions = loaded
        self.assertEqual(len(goals), 10)
        self.assertEqual(len(executions), 2)
        for rec in goals:
            result = rec["result"]
            self.assertEqual(result["decision"], "decomposed",
                             f"#{rec['n']} 未分解")
            self.assertEqual(result["model"], "ollama-local")
            for step in result["steps"]:
                self.assertIn(step["tool"], DECOMPOSE_ALLOWED_TOOLS)
                self.assertIsInstance(step["args"], dict)
                # 关键改进：自造字段 0 次——args 键必须落在真实契约里
                self.assertTrue(
                    set(step["args"].keys()) <= REAL_ARG_KEYS[step["tool"]],
                    f"#{rec['n']} {step['tool']} 有自造字段: "
                    f"{set(step['args'].keys()) - REAL_ARG_KEYS[step['tool']]}")

    def test_08_probe_a_execution_spot_checks(self):
        loaded = _load_probe("decompose-probe-s2-5-a-prompt-0.5b.jsonl")
        self.assertIsNotNone(loaded)
        _, executions = loaded
        by_n = {e["goal_n"]: e for e in executions}
        self.assertEqual(by_n[4]["plan_run"]["decision"], "done")
        self.assertEqual(by_n[1]["plan_run"]["decision"], "denied")
        self.assertEqual(by_n[1]["plan_run"]["reason"],
                         "external_requires_network")


class TestS25ProbeB(unittest.TestCase):
    """候选 (b) 7b 探查原文结构校验（文件不存在时跳过）。"""

    def test_09_probe_b_structure_if_present(self):
        loaded = _load_probe("decompose-probe-s2-5-b-7b.jsonl")
        if loaded is None:
            self.skipTest("候选 (b) 探查文件尚未生成")
        goals, _ = loaded
        self.assertEqual(len(goals), 10)
        # (b) 实测：8 条 decomposed，#7、#8 因模型输出缺最后的 } 而
        # unparsable_output（两次独立重跑均复现，见 S2-5 汇报）。
        decomposed = [r for r in goals
                      if r["result"]["decision"] == "decomposed"]
        failed = [r for r in goals
                  if r["result"]["decision"] == "failed"]
        self.assertEqual(len(decomposed), 8)
        self.assertEqual([r["n"] for r in failed], [7, 8])
        for rec in failed:
            self.assertEqual(rec["result"]["reason"], "unparsable_output")
        for rec in decomposed:
            result = rec["result"]
            for step in result["steps"]:
                self.assertIn(step["tool"], DECOMPOSE_ALLOWED_TOOLS)
                self.assertIsInstance(step["args"], dict)
                self.assertTrue(
                    set(step["args"].keys()) <= REAL_ARG_KEYS[step["tool"]],
                    f"#{rec['n']} {step['tool']} 有自造字段: "
                    f"{set(step['args'].keys()) - REAL_ARG_KEYS[step['tool']]}")


class TestS25ProbeC(unittest.TestCase):
    """候选 (c) Gemini 探查原文结构校验（2026-11-23 实测，11 次调用）。

    评分见 S2-5 汇报 §五：严格 8/10（#1-#8）、宽松 8/10；
    #9、#10 因模型输出截断而 unparsable_output（与 (b) #7、#8 同类）。
    """

    def test_10_probe_c_structure_if_present(self):
        loaded = _load_probe("decompose-probe-s2-5-c-gemini.jsonl")
        if loaded is None:
            self.skipTest("候选 (c) 探查文件尚未生成")
        goals, executions = loaded
        self.assertEqual(len(goals), 10)
        self.assertEqual(len(executions), 2)
        decomposed = [r for r in goals
                      if r["result"]["decision"] == "decomposed"]
        failed = [r for r in goals
                  if r["result"]["decision"] == "failed"]
        self.assertEqual(len(decomposed), 8)
        self.assertEqual([r["n"] for r in failed], [9, 10])
        for rec in failed:
            self.assertEqual(rec["result"]["reason"], "unparsable_output")
        for rec in decomposed:
            result = rec["result"]
            self.assertEqual(result["model"], "gemini-flash")
            for step in result["steps"]:
                self.assertIn(step["tool"], DECOMPOSE_ALLOWED_TOOLS)
                self.assertIsInstance(step["args"], dict)
                self.assertTrue(
                    set(step["args"].keys()) <= REAL_ARG_KEYS[step["tool"]],
                    f"#{rec['n']} {step['tool']} 有自造字段: "
                    f"{set(step['args'].keys()) - REAL_ARG_KEYS[step['tool']]}")
        by_n = {e["goal_n"]: e for e in executions}
        self.assertEqual(by_n[4]["plan_run"]["decision"], "done")
        self.assertEqual(by_n[1]["plan_run"]["decision"], "denied")
        self.assertEqual(by_n[1]["plan_run"]["reason"],
                         "external_requires_network")


if __name__ == "__main__":
    unittest.main()
