"""Jarvis P2 — Model Router（最小版，Stub 优先）

原则（来自总体架构与 ADR-002）：
- Jarvis 代码只用逻辑模型名 fast / capable / reasoning / local / embedding，
  映射表在配置文件里，不进业务代码。换模型只改配置。
- P2 先用 Stub（本地假模型，返回固定格式），不接任何真实 API，不产生费用。
  LiteLLM 配置占位在 config.yaml 里，P2 后期接真实供应商时才启用。
- 故障切换链：首选 -> 备选 -> 本地，每家最多试一次，不死循环重试。
  例外：logical=local 时只走本地；logical=embedding 时只走 embedding 类
  供应商，不回落到文本模型（否则会静默返回非向量结果，P6-15）。
- 全失败时走 L0 规则降级：返回确定性模板回答，不报错瘫痪，标记 degraded=true。
  例外：embedding 全失败时不返回文本，只返回空内容 + error 标记（P6-15）。
- 每次调用都记审计：模型、token、成本、耗时、结果 hash，写进事件日志。

不联网，不 import 任何模型供应商 SDK。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

# 逻辑模型名 -> Stub 供应商名（默认映射，config.yaml 可覆盖）
DEFAULT_MAPPING = {
    "fast": "stub-fast",
    "capable": "stub-capable",
    "reasoning": "stub-reasoning",
    "local": "stub-local",
    "embedding": "stub-embedding",
}

LOGICAL_MODELS = tuple(DEFAULT_MAPPING.keys())


class ProviderError(Exception):
    """供应商失败：超时 / 5xx / 配置错误，统一用这个异常，Router 只按它切换。"""


class StubProvider:
    """本地假模型：确定性回答，token 按词数算，成本 0。"""

    def __init__(self, name: str):
        self.name = name

    def complete(self, prompt: str, fail: bool = False) -> dict:
        if fail:
            raise ProviderError(f"{self.name} simulated failure (timeout/500)")
        words = prompt.split()
        content = f"[{self.name}] stub response to: {prompt[:80]}"
        return {
            "model": self.name,
            "content": content,
            "tokens_in": len(words),
            "tokens_out": len(content.split()),
            "cost_usd": 0.0,
        }


def degraded_response(prompt: str) -> dict:
    """L0 规则降级：不依赖任何模型，返回确定性模板。"""
    content = (
        "降级回答：所有模型均不可用，已切换到本地规则层。"
        f"收到的问题是：{prompt[:80]}。请稍后重试，或用本地事实检索代替。"
    )
    return {
        "model": "local-rule",
        "content": content,
        "tokens_in": len(prompt.split()),
        "tokens_out": len(content.split()),
        "cost_usd": 0.0,
        "degraded": True,
    }


class Router:
    def __init__(self, config: dict, event_log):
        # config 只读 model 一节；映射不在业务代码里
        model_cfg = config.get("model", {}) or {}
        self.mapping = dict(DEFAULT_MAPPING)
        self.mapping.update(model_cfg.get("logical_models") or {})
        self.event_log = event_log

    def chain_for(self, logical: str) -> list[str]:
        """切换链：首选 -> 备选 -> 本地，去重，顺序固定。
        例外：logical=local 时只走本地，不回落到云端——否则“本地”承诺会被悄悄打破。
        例外：logical=embedding 时只走 embedding 类供应商，不回落到文本模型——
        否则 embedding 失败后会静默返回文本结果（P6-15；如需多个 embedding
        供应商，须在配置里为 embedding 指定多个同类供应商，Phase 2）。
        """
        if logical not in self.mapping:
            raise ValueError(f"unknown logical model: {logical}")
        primary = self.mapping[logical]
        if logical in ("local", "embedding"):
            return [primary]
        backup = self.mapping.get("capable", "stub-capable")
        local = self.mapping.get("local", "stub-local")
        chain: list[str] = []
        for name in (primary, backup, local):
            if name and name not in chain:
                chain.append(name)
        return chain

    def complete(self, logical: str, prompt: str,
                 fail_providers: tuple[str, ...] = ()) -> dict:
        """走切换链，每家最多一次；全失败走降级，不抛异常。"""
        chain = self.chain_for(logical)
        attempted: list[str] = []
        for provider_name in chain:
            attempted.append(provider_name)
            start = time.monotonic()
            try:
                provider = StubProvider(provider_name)
                result = provider.complete(prompt, fail=provider_name in fail_providers)
            except ProviderError as exc:
                latency_ms = int((time.monotonic() - start) * 1000)
                self.event_log.append(
                    "router_fallback", "fallback",
                    json.dumps({"logical": logical, "provider": provider_name,
                                "error": str(exc), "latency_ms": latency_ms},
                               ensure_ascii=False),
                )
                continue
            latency_ms = int((time.monotonic() - start) * 1000)
            result_hash = hashlib.sha256(result["content"].encode("utf-8")).hexdigest()
            record = {
                "logical": logical,
                "model": result["model"],
                "tokens_in": result["tokens_in"],
                "tokens_out": result["tokens_out"],
                "cost_usd": result["cost_usd"],
                "latency_ms": latency_ms,
                "result_hash": result_hash,
                "degraded": False,
                "attempted": attempted,
            }
            self.event_log.append("router_call", "ok", json.dumps(record, ensure_ascii=False))
            return {**result, **record}
        # 全失败：降级，不抛异常
        if logical == "embedding":
            # P6-15：embedding 全失败不得返回文本降级回答（那不是向量）。
            # 返回空内容 + error 标记，调用方按 error 处理。
            result = {
                "model": "embedding-unavailable",
                "content": "",
                "tokens_in": len(prompt.split()),
                "tokens_out": 0,
                "cost_usd": 0.0,
                "degraded": True,
                "error": "embedding_unavailable",
            }
            result_hash = hashlib.sha256(result["content"].encode("utf-8")).hexdigest()
            record = {
                "logical": logical,
                "model": result["model"],
                "tokens_in": result["tokens_in"],
                "tokens_out": result["tokens_out"],
                "cost_usd": 0.0,
                "latency_ms": 0,
                "result_hash": result_hash,
                "degraded": True,
                "attempted": attempted,
                "error": "embedding_unavailable",
            }
            self.event_log.append("router_call", "ok", json.dumps(record, ensure_ascii=False))
            return {**result, **record}
        result = degraded_response(prompt)
        result_hash = hashlib.sha256(result["content"].encode("utf-8")).hexdigest()
        record = {
            "logical": logical,
            "model": result["model"],
            "tokens_in": result["tokens_in"],
            "tokens_out": result["tokens_out"],
            "cost_usd": 0.0,
            "latency_ms": 0,
            "result_hash": result_hash,
            "degraded": True,
            "attempted": attempted,
        }
        self.event_log.append("router_call", "ok", json.dumps(record, ensure_ascii=False))
        return {**result, **record}
