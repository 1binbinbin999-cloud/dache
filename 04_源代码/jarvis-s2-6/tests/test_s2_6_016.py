"""S2-6 测试：JIS-016 解析器容错 + 输出上限可配 + 防编造提示词。

S2-5 残留（见 S2-5 汇报 §五）：JSON 截断 2/10（7b #7/#8 缺最后
的括号、Gemini #9/#10 中途断在 JSON 中间）与内容幻觉 2/10
（7b #1 编待办、#9 编心情）。根因查证（见 S2-6 记录 §四）：
4 条截断输出原文均只有 96-245 字符，远低于当时的输出上限
（Ollama num_predict 512、Gemini maxOutputTokens 1024），上限
不是这 4 起的直接原因——最可能是模型侧提前停；但上限本身
是真实存在的硬编码，decompose 的长输出可逼近它，仍须改由配置
控制并留出余量。解析器容错是本轮主修：只补缺失的闭合符号，
不编步骤；白名单与 schema 校验不放宽。

本文件只用假模型与已存档的探查原文，不依赖 Ollama、不发真实请求。
"""

import json
import os
import unittest
from pathlib import Path
from unittest import mock

S2_DIR = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(S2_DIR))

from adapters import build_adapter  # noqa: E402
from gateway import Gateway  # noqa: E402
from planner import DECOMPOSE_ALLOWED_TOOLS, Planner  # noqa: E402
from core import Core  # noqa: E402


class FakeLog:
    def append(self, *args, **kwargs):
        return True


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


# S2-5 探查原文里的 4 条真实截断输出（verbatim，见 S2-5 汇报 §五）。
TRUNC_7B_7 = ('{"steps": [{"tool": "router-call", "args": {"logical": "fast", '
              '"prompt": "制定明天的学习计划并记录"}}, {"tool": "memory-add", '
              '"args": {"text": "明天的学习计划已经制定并记录", "type": "fact", '
              '"confidence": 0.6, "evidence_grade": "single_occurrence", '
              '"source": "planner"}}]')
TRUNC_7B_8 = ('{"steps": [{"tool": "router-call", "args": {"logical": "fast", '
              '"prompt": "生成问候语"}}, {"tool": "list_tasks", "args": {}}}')
TRUNC_GEMINI_9 = ('{"steps": [{"tool": "memory-add", "args": {"text": '
                  '"用户今天的心情", "type": "fact", "confidence": 0.6,')
TRUNC_GEMINI_10 = ('```json\n{\n  "steps": [\n    {\n      "tool": "memory-recall",'
                   '\n      "args": {\n        "query": "最近')


class TestS26PromptAntiHallucination(unittest.TestCase):
    """提示词加防编造一句；S2-5 的契约要求仍在。"""

    def test_01_prompt_forbids_invented_details(self):
        router = _canned_router('{"steps": []}')
        _planner().decompose("早上问候并记下今天的待办", router)
        prompt = router.captured_prompt
        self.assertIsNotNone(prompt)
        self.assertIn("不许编造目标里没提到的具体内容", prompt)
        self.assertIn("通用表述", prompt)
        # S2-5 的要求未被新句子挤掉
        self.assertIn("绝不许用 memory-add", prompt)
        self.assertIn("每个动作都要有对应的步骤", prompt)
        self.assertIn('"evidence_grade": "single_occurrence"', prompt)


class TestS26BraceRepair(unittest.TestCase):
    """补括号修复：4 条真实截断输出都能拆出来；底线不放宽。"""

    def test_02_repair_missing_final_brace_7b_7(self):
        result = _planner().decompose(
            "制定明天的学习计划并记录", _canned_router(TRUNC_7B_7))
        self.assertEqual(result["decision"], "decomposed")
        self.assertEqual(len(result["steps"]), 2)
        self.assertEqual(result["steps"][0]["tool"], "router-call")
        self.assertEqual(result["steps"][1]["tool"], "memory-add")
        self.assertEqual(result["steps"][1]["args"]["confidence"], 0.6)

    def test_03_repair_unclosed_array_7b_8(self):
        result = _planner().decompose(
            "问候用户并查询任务列表", _canned_router(TRUNC_7B_8))
        self.assertEqual(result["decision"], "decomposed")
        self.assertEqual(len(result["steps"]), 2)
        self.assertEqual(result["steps"][1]["tool"], "list_tasks")

    def test_04_repair_cut_mid_args_gemini_9(self):
        result = _planner().decompose(
            "记录用户今天的心情并回顾昨天的记忆",
            _canned_router(TRUNC_GEMINI_9))
        self.assertEqual(result["decision"], "decomposed")
        # 只修出已输出的那一步；截断丢掉的回顾步修不回来，不编。
        self.assertEqual(len(result["steps"]), 1)
        self.assertEqual(result["steps"][0]["tool"], "memory-add")
        self.assertEqual(result["steps"][0]["args"]["text"], "用户今天的心情")

    def test_05_repair_cut_mid_string_gemini_10(self):
        result = _planner().decompose(
            "总结最近的记忆再向我问好", _canned_router(TRUNC_GEMINI_10))
        self.assertEqual(result["decision"], "decomposed")
        self.assertEqual(len(result["steps"]), 1)
        self.assertEqual(result["steps"][0]["tool"], "memory-recall")
        self.assertEqual(result["steps"][0]["args"]["query"], "最近")

    def test_06_garbage_still_unparsable(self):
        for garbage in ("我不知道怎么拆", "{这不是 json",
                        '{"steps": "oops"}'):
            result = _planner().decompose("早上问候", _canned_router(garbage))
            self.assertEqual(result["decision"], "failed", garbage)
            self.assertEqual(result["reason"], "unparsable_output", garbage)

    def test_07_bad_tool_still_rejected_after_repair(self):
        # 修完能解析，但白名单仍拒非法工具（底线：不放宽）。
        bad = '{"steps": [{"tool": "delete_file", "args": {"path": "x"}}'
        result = _planner().decompose("删文件", _canned_router(bad))
        self.assertEqual(result["decision"], "failed")
        self.assertIn("unknown_tool", result["reason"])

    def test_08_cut_mid_number_still_unparsable(self):
        # 已知局限：切在数字/字面量中间（如 0.）修不了，不硬猜。
        cut = ('{"steps": [{"tool": "memory-add", "args": {"text": "x", '
               '"confidence": 0.')
        result = _planner().decompose("记住 x", _canned_router(cut))
        self.assertEqual(result["decision"], "failed")
        self.assertEqual(result["reason"], "unparsable_output")

    def test_09_good_output_unchanged(self):
        good = json.dumps({"steps": [
            {"tool": "memory-recall", "args": {"query": "跑步"}}]},
            ensure_ascii=False)
        result = _planner().decompose("查跑步", _canned_router(good))
        self.assertEqual(result["decision"], "decomposed")
        self.assertEqual(result["steps"][0]["args"]["query"], "跑步")


class TestS26OutputCaps(unittest.TestCase):
    """输出上限改由配置控制；适配器记录截断诊断字段。"""

    def test_10_config_caps(self):
        config = Core(S2_DIR).load_config()
        providers = config["model"]["providers"]
        self.assertEqual(providers["ollama-local"]["max_tokens"], 1024)
        self.assertEqual(providers["gemini-flash"]["max_tokens"], 2048)
        self.assertEqual(config["version"], "0.10.0-s2-6")

    def test_11_adapter_defaults_unchanged_without_config(self):
        ollama = build_adapter("ollama-local",
                               {"type": "ollama", "model": "qwen2.5:0.5b"})
        self.assertEqual(ollama.max_tokens, 512)
        gemini = build_adapter("gemini-flash",
                               {"type": "gemini", "model": "gemini-3.6-flash"})
        self.assertEqual(gemini.max_tokens, 1024)

    def test_12_ollama_sends_configured_num_predict_and_done_reason(self):
        captured = {}

        def fake_post(url, payload, timeout_s):
            captured.update(payload)
            return {"done": True, "response": "ok", "prompt_eval_count": 3,
                    "eval_count": 5, "done_reason": "length"}

        adapter = build_adapter(
            "ollama-local",
            {"type": "ollama", "model": "qwen2.5:0.5b", "max_tokens": 1024})
        with mock.patch("adapters._post_json", side_effect=fake_post):
            result = adapter.complete("hi")
        self.assertEqual(captured["options"]["num_predict"], 1024)
        self.assertEqual(result["done_reason"], "length")
        self.assertEqual(result["tokens_out"], 5)

    def test_13_gemini_sends_configured_max_tokens_and_finish_reason(self):
        captured = {}

        def fake_post(url, payload, timeout_s):
            captured.update(payload)
            return {"candidates": [
                {"content": {"parts": [{"text": "hello"}]},
                 "finishReason": "MAX_TOKENS"}],
                "usageMetadata": {"promptTokenCount": 10,
                                  "candidatesTokenCount": 20,
                                  "thoughtsTokenCount": 500}}

        adapter = build_adapter(
            "gemini-flash",
            {"type": "gemini", "model": "gemini-3.6-flash",
             "max_tokens": 2048})
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), \
                mock.patch("adapters._post_json", side_effect=fake_post):
            result = adapter.complete("hi")
        self.assertEqual(
            captured["generationConfig"]["maxOutputTokens"], 2048)
        self.assertEqual(result["finish_reason"], "MAX_TOKENS")
        self.assertEqual(result["thoughts_tokens"], 500)


# 各工具真实参数契约（gateway.py::_dispatch）；探查原文里出现的
# args 键必须落在这个集合里（自造字段 = 违规）。与 test_s2_5_016.py 同表。
REAL_ARG_KEYS = {
    "list_tasks": set(),
    "read_task": {"id"},
    "memory-recall": {"query"},
    "memory-add": {"text", "type", "confidence", "evidence_grade",
                   "source", "id", "valid_until"},
    "router-call": {"logical", "prompt", "fail"},
}


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


class TestS26ProbeA(unittest.TestCase):
    """候选 (a) S2-6 探查原文结构校验（人工评分见 S2-6 记录 §五，
    不在这里评分）。"""

    def test_14_probe_a_ten_goals_all_decomposed(self):
        loaded = _load_probe("decompose-probe-s2-6-a-prompt-0.5b.jsonl")
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
                self.assertTrue(
                    set(step["args"].keys()) <= REAL_ARG_KEYS[step["tool"]],
                    f"#{rec['n']} {step['tool']} 有自造字段: "
                    f"{set(step['args'].keys()) - REAL_ARG_KEYS[step['tool']]}")

    def test_15_probe_a_execution_spot_checks(self):
        loaded = _load_probe("decompose-probe-s2-6-a-prompt-0.5b.jsonl")
        self.assertIsNotNone(loaded)
        _, executions = loaded
        by_n = {e["goal_n"]: e for e in executions}
        self.assertEqual(by_n[4]["plan_run"]["decision"], "done")
        self.assertEqual(by_n[1]["plan_run"]["decision"], "denied")
        self.assertEqual(by_n[1]["plan_run"]["reason"],
                         "external_requires_network")


class TestS26ProbeB(unittest.TestCase):
    """候选 (b) S2-6 探查原文结构校验（人工评分见 S2-6 记录 §五，
    不在这里评分）。"""

    def test_16_probe_b_ten_goals_all_decomposed(self):
        loaded = _load_probe("decompose-probe-s2-6-b-7b.jsonl")
        self.assertIsNotNone(loaded, "候选 (b) 探查文件缺失")
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
                self.assertTrue(
                    set(step["args"].keys()) <= REAL_ARG_KEYS[step["tool"]],
                    f"#{rec['n']} {step['tool']} 有自造字段: "
                    f"{set(step['args'].keys()) - REAL_ARG_KEYS[step['tool']]}")

    def test_17_probe_b_execution_spot_checks(self):
        loaded = _load_probe("decompose-probe-s2-6-b-7b.jsonl")
        self.assertIsNotNone(loaded)
        _, executions = loaded
        by_n = {e["goal_n"]: e for e in executions}
        self.assertEqual(by_n[4]["plan_run"]["decision"], "done")
        self.assertEqual(by_n[1]["plan_run"]["decision"], "denied")
        self.assertEqual(by_n[1]["plan_run"]["reason"],
                         "external_requires_network")


if __name__ == "__main__":
    unittest.main()
