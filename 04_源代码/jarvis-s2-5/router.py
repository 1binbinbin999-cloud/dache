"""Jarvis S2-1 — Model Router（直接适配器版）

S2-1 变化（第 5 题 A + 第 6 题）：
- router_mode: stub（默认）时行为与 P2–P6 完全一致：全部 Stub，
  切换链 [首选, capable, local]，embedding 只走 embedding 类。
- router_mode: live 时走真实适配器（adapters.py）：映射改用
  live_logical_models，切换链按 config 的 fallback_chain
  （Gemini -> Ollama 本地 -> Stub）；Groq / OpenRouter 只留桩位，
  报 not_implemented 后继续切链，不产生调用。
- 适配器错误一律 AdapterError（ProviderError 子类，带 error_class），
  Router 只按它切换；回退审计里记 error_class。
- embedding 在 live 模式下走适配器的 embed()，返回向量；全失败
  仍按 P6-15 返回 embedding-unavailable（空内容，不回落文本）。

P2 原则保留：
- Jarvis 代码只用逻辑模型名 fast / capable / reasoning / local / embedding，
  映射表在配置文件里，不进业务代码。换模型只改配置。
- 故障切换链：首选 -> 备选 -> 本地，每家最多试一次，不死循环重试。
  例外：logical=local 时只走本地；logical=embedding 时只走 embedding 类
  供应商，不回落到文本模型（否则会静默返回非向量结果，P6-15）。
- 全失败时走 L0 规则降级：返回确定性模板回答，不报错瘫痪，标记 degraded=true。
  例外：embedding 全失败时不返回文本，只返回空内容 + error 标记（P6-15）。
- 每次调用都记审计：模型、token、成本、耗时、结果 hash，写进事件日志。

不 import 任何模型供应商 SDK（adapters.py 只用标准库 urllib）。

原则（来自总体架构与 ADR-002）：
- P2 先用 Stub（本地假模型，返回固定格式），不接任何真实 API，不产生费用。
  LiteLLM 配置占位在 config.yaml 里，P2 后期接真实供应商时才启用。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

# S2-1：ProviderError 与适配器工厂改由 adapters.py 提供；
# 这里重新导出，旧引用点（StubProvider / Gateway / 测试）不受影响。
from adapters import AdapterError, ProviderError, build_adapter  # noqa: F401

# 逻辑模型名 -> Stub 供应商名（默认映射，config.yaml 可覆盖）
DEFAULT_MAPPING = {
    "fast": "stub-fast",
    "capable": "stub-capable",
    "reasoning": "stub-reasoning",
    "local": "stub-local",
    "embedding": "stub-embedding",
}

LOGICAL_MODELS = tuple(DEFAULT_MAPPING.keys())


class StubProvider:
    """本地假模型：确定性回答，token 按词数算，成本 0。"""

    def __init__(self, name: str):
        self.name = name

    def probe(self) -> dict:
        return {"ok": True, "reason": "stub_no_network"}

    def embed(self, texts: list[str]) -> dict:
        """Stub 向量：由文本 sha256 派生的 8 维确定性假向量。

        只用于 live 模式下 embedding 链走到 stub-embedding 时
        （离线底），与 Stub 文本回答同性质——确定性假数据，
        不是真实向量。P6-15 仍成立：embedding 全失败时返回
        embedding-unavailable，绝不回落文本模型。
        """
        import hashlib as _hashlib
        out: list[list[float]] = []
        for text in texts:
            digest = _hashlib.sha256(text.encode("utf-8")).digest()
            out.append([b / 255.0 for b in digest[:8]])
        return {
            "model": self.name,
            "embeddings": out,
            "dimensions": 8,
            "tokens_in": sum(len(t.split()) for t in texts),
            "cost_usd": 0.0,
        }

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
    def __init__(self, config: dict, event_log, usage_log=None):
        # S2-2：usage_log 可选；传了才记真实调用 usage（Stub 结果
        # 无 backend_usage，本来也不会记；不传则不记，旧调用点不受影响）。
        # config 只读 model 一节；映射不在业务代码里
        model_cfg = config.get("model", {}) or {}
        self.router_mode = str(model_cfg.get("router_mode", "stub"))
        if self.router_mode not in ("stub", "live"):
            raise ValueError(f"unknown router_mode: {self.router_mode}")
        # stub 模式用 logical_models（P2–P6 原样）；live 模式用
        # live_logical_models + fallback_chain（S2-1）。
        self.mapping = dict(DEFAULT_MAPPING)
        if self.router_mode == "live":
            self.mapping.update(model_cfg.get("live_logical_models") or {})
        else:
            self.mapping.update(model_cfg.get("logical_models") or {})
        self.providers_cfg = dict(model_cfg.get("providers") or {})
        self.fallback_chain = list(model_cfg.get("fallback_chain") or [])
        self.embedding_fallback_chain = list(
            model_cfg.get("embedding_fallback_chain") or [])
        self._adapters: dict[str, object] = {}
        self.event_log = event_log
        self.usage_log = usage_log

    def _provider(self, name: str):
        """供应商实例（带缓存）：stub-* 走 StubProvider，其余按
        config providers 一节建适配器；配置里没有的供应商名是
        配置错误，报 ProviderError（Router 当失败切链，不崩）。"""
        if name in self._adapters:
            return self._adapters[name]
        if name.startswith("stub-"):
            provider = StubProvider(name)
        elif name in self.providers_cfg:
            provider = build_adapter(name, self.providers_cfg[name])
        else:
            raise ProviderError(f"unknown provider: {name}")
        self._adapters[name] = provider
        return provider

    def probe_all(self) -> dict:
        """各供应商状态（无推理成本）：providers 一节 + live 映射
        里的全部供应商名；Stub 恒为 ok；Gemini 只报 Key 有无，
        不发请求、不耗额度。"""
        names = sorted(set(self.providers_cfg)
                       | set(self.mapping.values()))
        out: dict[str, dict] = {}
        for name in names:
            try:
                provider = self._provider(name)
            except ProviderError as exc:
                out[name] = {"ok": False, "reason": f"unknown_provider:{exc}"}
                continue
            probe = getattr(provider, "probe", None)
            out[name] = probe() if callable(probe) else {"ok": True, "reason": "no_probe"}
        return out

    def chain_for(self, logical: str) -> list[str]:
        """切换链：首选 -> 备选 -> 本地，去重，顺序固定。
        例外：logical=local 时只走本地，不回落到云端——否则“本地”承诺会被悄悄打破。
        例外：logical=embedding 时只走 embedding 类供应商，不回落到文本模型——
        否则 embedding 失败后会静默返回文本结果（P6-15；如需多个 embedding
        供应商，须在配置里为 embedding 指定多个同类供应商，Phase 2）。
        S2-1 live 模式：云端逻辑模型走 [首选] + config fallback_chain
        （Gemini -> Ollama 本地 -> Stub）；local 走 [首选, stub-local]；
        embedding 走 [首选] + embedding_fallback_chain（只在 embedding
        类内回落，P6-15）。
        """
        if logical not in self.mapping:
            raise ValueError(f"unknown logical model: {logical}")
        primary = self.mapping[logical]
        if self.router_mode == "live":
            if logical == "local":
                candidates = [primary, "stub-local"]
            elif logical == "embedding":
                candidates = [primary] + self.embedding_fallback_chain
            else:
                candidates = [primary] + self.fallback_chain
            chain: list[str] = []
            for name in candidates:
                if name and name not in chain:
                    chain.append(name)
            return chain
        if logical in ("local", "embedding"):
            return [primary]
        backup = self.mapping.get("capable", "stub-capable")
        local = self.mapping.get("local", "stub-local")
        chain = []
        for name in (primary, backup, local):
            if name and name not in chain:
                chain.append(name)
        return chain

    def _complete_embedding(self, provider, prompt: str) -> dict:
        """live 模式的 embedding 调用：走适配器 embed()，返回向量。

        返回与 complete() 同形的 dict，但 content 为空字符串、
        另带 embeddings 与 dimensions。适配器不支持 embed
        （如 Gemini）时报 AdapterError，由调用方按链切换。
        """
        result = provider.embed([prompt])
        embeddings = result.get("embeddings") or []
        if not embeddings:
            raise AdapterError("invalid_response",
                               "empty embeddings from provider")
        return {
            "model": result["model"],
            "content": "",
            "embeddings": embeddings,
            "dimensions": result.get("dimensions", len(embeddings[0])),
            "tokens_in": result.get("tokens_in", 0),
            "tokens_out": 0,
            "cost_usd": result.get("cost_usd", 0.0),
            # S2-2：透传后端原样 usage，供 usage 审计比对。
            "backend_usage": result.get("backend_usage", {}),
        }

    def complete(self, logical: str, prompt: str,
                 fail_providers: tuple[str, ...] = ()) -> dict:
        """走切换链，每家最多一次；全失败走降级，不抛异常。

        S2-1：stub 模式沿用 StubProvider；live 模式按 _provider()
        建适配器调用。live + embedding 时走适配器的 embed()，返回
        向量（embeddings / dimensions），content 为空字符串；
        全失败仍按 P6-15 返回 embedding-unavailable。
        """
        chain = self.chain_for(logical)
        attempted: list[str] = []
        for provider_name in chain:
            attempted.append(provider_name)
            start = time.monotonic()
            try:
                provider = self._provider(provider_name)
                if provider_name in fail_providers:
                    raise ProviderError(f"{provider_name} simulated failure (timeout/500)")
                if logical == "embedding" and self.router_mode == "live":
                    # live embedding：一律走 embed()（含 stub-embedding
                    # 的确定性假向量），绝不返回文本结果（P6-15）。
                    result = self._complete_embedding(provider, prompt)
                elif isinstance(provider, StubProvider):
                    result = provider.complete(prompt)
                else:
                    result = provider.complete(prompt)
            except ProviderError as exc:
                latency_ms = int((time.monotonic() - start) * 1000)
                detail = {"logical": logical, "provider": provider_name,
                          "error": str(exc), "latency_ms": latency_ms}
                if isinstance(exc, AdapterError):
                    detail["error_class"] = exc.error_class
                self.event_log.append(
                    "router_fallback", "fallback",
                    json.dumps(detail, ensure_ascii=False),
                )
                continue
            latency_ms = int((time.monotonic() - start) * 1000)
            content = result.get("content", "")
            result_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            record = {
                "logical": logical,
                "model": result["model"],
                "tokens_in": result.get("tokens_in", 0),
                "tokens_out": result.get("tokens_out", 0),
                "cost_usd": result.get("cost_usd", 0.0),
                "latency_ms": latency_ms,
                "result_hash": result_hash,
                "degraded": False,
                "attempted": attempted,
            }
            self.event_log.append("router_call", "ok", json.dumps(record, ensure_ascii=False))
            # S2-2：真实调用才记 usage 日志。判断标准是成功的供应商
            # 不是 StubProvider（真实适配器才记）；Stub 结果与降级
            # 路径不记，stub 模式回归因此不污染 usage 日志。
            # backend_usage 为空 dict 时也记——审计会报
            # missing_backend_usage，不静默漏记。
            if self.usage_log is not None and not isinstance(provider, StubProvider):
                self.usage_log.append({
                    "logical": logical,
                    "provider": attempted[-1],
                    "model": result["model"],
                    "tokens_in": result.get("tokens_in", 0),
                    "tokens_out": result.get("tokens_out", 0),
                    "backend_usage": result.get("backend_usage", {}),
                    "latency_ms": latency_ms,
                    "cost_usd": result.get("cost_usd", 0.0),
                    "result_hash": result_hash,
                })
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
