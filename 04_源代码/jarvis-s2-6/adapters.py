"""Jarvis S2-1 — 直接适配器（Direct Adapters）

原则（第 5 题 A + 第 6 题：Ollama 本地 + Gemini 免费层，不用 LiteLLM）：
- 只用 Python 标准库（urllib），不 import 任何模型供应商 SDK，
  不新增依赖；core.py 的禁 import 检查（FORBIDDEN_MODULE_PREFIXES：
  openai / anthropic / google.generativeai / google.genai / litellm /
  langchain / ollama）在 live 模式下仍须全绿。
- 统一接口：complete(prompt) / embed(texts)，返回与 Router 旧结果
  同形的 dict（model / content 或 embeddings / tokens_in /
  tokens_out / cost_usd）。成本一律 0.0（Gemini 免费层、Ollama 本地，
  $0；Step 0 报告：免费层超额只 429，不扣费）。
- 错误一律 AdapterError（ProviderError 的子类，带 error_class），
  Router 只按它切换。error_class：
  timeout / rate_limited(429) / auth_error(401/403) /
  invalid_request(400) / not_found(404) / server_error(5xx) /
  unavailable(连接失败) / invalid_response(响应解析失败) /
  missing_key(Key 未设置) / not_implemented(桩位，未实现)。
  重试只对 429 与 5xx，按配置 retry_max 执行，超时不重试。
- Key 只从环境变量读（GEMINI_API_KEY），绝不写进代码、配置或
  记录文件；缺 Key 时明确报错 missing_key，绝不静默降级、
  绝不用假 Key 冒充实测。
- 审计只记 provider / model / 耗时 / error_class，提示词原文与
  响应内容不进审计（P4 脱敏先例）；Key 永不进日志。
- Groq / OpenRouter 在 S2-1 只留桩位：NotImplementedAdapter，
  complete/embed 一律报 not_implemented，不产生任何调用。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_KEY_ENV = "GEMINI_API_KEY"


class ProviderError(Exception):
    """供应商失败：超时 / 5xx / 配置错误，统一用这个异常，Router 只按它切换。

    （原定义在 router.py；S2-1 起由 adapters.py 定义，router.py
    从这里导入并重新导出，旧引用点不受影响。）
    """


class AdapterError(ProviderError):
    """适配器失败，带错误分类。Router 按 error_class 记审计、按链切换。

    retry_after：服务端 Retry-After 头（秒，S2-4）：429 时
    _post_json 从响应头解析后挂在此属性上；_call_with_retry
    优先按它退避（上限 retry_backoff_max_s），无头时用配置退避。
    """

    def __init__(self, error_class: str, message: str,
                 retry_after: float | None = None):
        super().__init__(f"{error_class}: {message}")
        self.error_class = error_class
        self.message = message
        self.retry_after = retry_after


def _parse_retry_after(headers) -> float | None:
    """解析 Retry-After 头：只认秒数；HTTP-date 形式与非法值忽略。"""
    if headers is None:
        return None
    try:
        raw = headers.get("Retry-After")
    except Exception:
        return None
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return value


def _post_json(url: str, payload: dict, timeout_s: float) -> dict:
    """POST JSON，返回解析后的响应 dict。

    HTTP 状态映射为 AdapterError；连接失败为 unavailable；
    超时为 timeout；响应不是 JSON 或结构不对由调用方判 invalid_response。
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        if exc.code == 429:
            raise AdapterError(
                "rate_limited", f"HTTP 429: {detail}",
                retry_after=_parse_retry_after(exc.headers)) from exc
        if exc.code in (401, 403):
            raise AdapterError("auth_error", f"HTTP {exc.code}: {detail}") from exc
        if exc.code == 400:
            raise AdapterError("invalid_request", f"HTTP 400: {detail}") from exc
        if exc.code == 404:
            raise AdapterError("not_found", f"HTTP 404: {detail}") from exc
        if 500 <= exc.code < 600:
            raise AdapterError("server_error", f"HTTP {exc.code}: {detail}") from exc
        raise AdapterError("server_error", f"HTTP {exc.code}: {detail}") from exc
    except TimeoutError as exc:
        raise AdapterError("timeout", f"request timed out after {timeout_s}s") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError):
            raise AdapterError("timeout", f"request timed out after {timeout_s}s") from exc
        raise AdapterError("unavailable", f"connection failed: {reason}") from exc
    except AdapterError:
        raise
    except Exception as exc:  # socket.timeout 等
        if "timed out" in str(exc).lower():
            raise AdapterError("timeout", f"request timed out after {timeout_s}s") from exc
        raise AdapterError("unavailable", f"connection failed: {exc}") from exc
    try:
        parsed = json.loads(body)
    except Exception as exc:
        raise AdapterError("invalid_response", "response is not JSON") from exc
    if not isinstance(parsed, dict):
        raise AdapterError("invalid_response", "response JSON is not an object")
    return parsed


def _get_json(url: str, timeout_s: float) -> dict:
    """GET JSON（只用于 Ollama /api/tags 这类无推理成本的探针）。"""
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise AdapterError("server_error", f"HTTP {exc.code}") from exc
    except TimeoutError as exc:
        raise AdapterError("timeout", f"request timed out after {timeout_s}s") from exc
    except urllib.error.URLError as exc:
        raise AdapterError("unavailable", f"connection failed: {exc.reason}") from exc
    try:
        parsed = json.loads(body)
    except Exception as exc:
        raise AdapterError("invalid_response", "response is not JSON") from exc
    if not isinstance(parsed, dict):
        raise AdapterError("invalid_response", "response JSON is not an object")
    return parsed


class BaseAdapter:
    """适配器基类：name 为配置里的供应商名；kind 声明能力。"""

    kind = "chat"  # chat | embedding | both

    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.cfg = dict(cfg or {})
        self.timeout_s = float(self.cfg.get("timeout_s", 60))
        self.retry_max = int(self.cfg.get("retry_max", 0))
        self.retry_backoff_s = float(self.cfg.get("retry_backoff_s", 2))
        # S2-4：Retry-After 上限（秒），防服务端给出的过大退避
        # 把调用拖死；默认 30s，可在 config.yaml 里按供应商覆盖。
        self.retry_backoff_max_s = float(
            self.cfg.get("retry_backoff_max_s", 30))

    def _effective_backoff(self, exc: AdapterError, attempt: int) -> float:
        """退避时长（S2-4）：429 带 Retry-After 头时按头退避
        （上限 retry_backoff_max_s）；否则用配置退避 ×(attempt+1)。"""
        header = getattr(exc, "retry_after", None)
        if header is not None:
            return min(header, self.retry_backoff_max_s)
        return self.retry_backoff_s * (attempt + 1)

    def complete(self, prompt: str) -> dict:
        raise NotImplementedError

    def embed(self, texts: list[str]) -> dict:
        raise NotImplementedError

    def probe(self) -> dict:
        """无推理成本的状态探针；不消耗任何额度。"""
        return {"ok": True, "reason": "no_probe"}


class OllamaAdapter(BaseAdapter):
    """本地 Ollama（/api/generate 与 /api/embed）。

    模型须已在本地拉取（ollama pull <model>）；未拉取时服务端
    返回 404，映射为 not_found。本地调用成本 0。
    """

    kind = "both"

    def __init__(self, name: str, cfg: dict):
        super().__init__(name, cfg)
        self.host = str(self.cfg.get("host", "http://localhost:11434")).rstrip("/")
        self.model = str(self.cfg.get("model", ""))
        # S2-6：num_predict 原硬编码 512。decompose 的 5 步中文 JSON
        # 可逼近 512 token，改由配置 max_tokens 控制（默认 512，老配置
        # 不写仍可用；s2-6 配置给 1024）。num_predict 只是上限，
        # 模型到 EOS 即停，不会为凑满而多生成。
        self.max_tokens = int(self.cfg.get("max_tokens", 512))

    def _call_with_retry(self, fn):
        """只对 429 / 5xx 重试；超时、连接失败、4xx 不重试。
        退避见 _effective_backoff（Retry-After 优先，S2-4）。"""
        last: AdapterError | None = None
        for attempt in range(self.retry_max + 1):
            try:
                return fn()
            except AdapterError as exc:
                last = exc
                if exc.error_class not in ("rate_limited", "server_error"):
                    raise
                if attempt >= self.retry_max:
                    raise
                time.sleep(self._effective_backoff(exc, attempt))
        raise last  # pragma: no cover - 循环必抛其一

    def complete(self, prompt: str) -> dict:
        def _once() -> dict:
            parsed = _post_json(
                f"{self.host}/api/generate",
                {"model": self.model, "prompt": prompt, "stream": False,
                 "options": {"temperature": 0.2,
                             "num_predict": self.max_tokens}},
                self.timeout_s,
            )
            if not parsed.get("done", False) or "response" not in parsed:
                raise AdapterError("invalid_response",
                                   "missing done/response in Ollama reply")
            return {
                "model": self.name,
                "content": str(parsed["response"]),
                "tokens_in": int(parsed.get("prompt_eval_count", 0) or 0),
                "tokens_out": int(parsed.get("eval_count", 0) or 0),
                "cost_usd": 0.0,
                # S2-6：done_reason（"stop"=模型自己停，"length"=撞上
                # num_predict 上限）留作截断诊断；S2-5 的 4 起截断
                # 发生时未记此字段，根因只能靠输出长度反推。
                "done_reason": parsed.get("done_reason"),
                # S2-2：后端原样 usage，供 usage 审计逐项比对。
                "backend_usage": {
                    "prompt_eval_count": int(parsed.get("prompt_eval_count", 0) or 0),
                    "eval_count": int(parsed.get("eval_count", 0) or 0),
                },
            }
        return self._call_with_retry(_once)

    def embed(self, texts: list[str]) -> dict:
        def _once() -> dict:
            parsed = _post_json(
                f"{self.host}/api/embed",
                {"model": self.model, "input": list(texts)},
                self.timeout_s,
            )
            embeddings = parsed.get("embeddings")
            if not isinstance(embeddings, list) or len(embeddings) != len(texts):
                raise AdapterError(
                    "invalid_response",
                    "embeddings count does not match input count")
            dims = {len(v) for v in embeddings if isinstance(v, list)}
            if len(dims) != 1:
                raise AdapterError("invalid_response",
                                   "embeddings have inconsistent dimensions")
            return {
                "model": self.name,
                "embeddings": embeddings,
                "dimensions": dims.pop(),
                "tokens_in": int(parsed.get("prompt_eval_count", 0) or 0),
                "cost_usd": 0.0,
                # S2-2：后端原样 usage（embedding 无 eval_count）。
                "backend_usage": {
                    "prompt_eval_count": int(parsed.get("prompt_eval_count", 0) or 0),
                },
            }
        return self._call_with_retry(_once)

    def probe(self) -> dict:
        try:
            parsed = _get_json(f"{self.host}/api/tags", timeout_s=5)
        except AdapterError as exc:
            return {"ok": False, "reason": f"{exc.error_class}:{exc.message}"}
        models = [m.get("name") for m in parsed.get("models", [])
                  if isinstance(m, dict) and m.get("name")]
        # Ollama 会把无标签名存为 name:latest；比较时按“名”归一化，
        # 否则 nomic-embed-text 会被误判为未拉取。
        present = (self.model in models
                   or f"{self.model}:latest" in models
                   or any(m.split(":")[0] == self.model.split(":")[0]
                          for m in models))
        return {"ok": True, "reason": "reachable",
                "models": models, "model_present": present}


class GeminiAdapter(BaseAdapter):
    """Gemini REST API（generativelanguage.googleapis.com/v1beta）。

    Key 只从环境变量 GEMINI_API_KEY 读；缺 Key 时报 missing_key，
    绝不静默降级。免费层：输入输出免费、超额只 429、不扣费
    （Step 0 官方核查）；免费层内容会被用于改进 Google 产品，
    敏感内容应走本地模型（见 Step 0 报告第四节）。
    embedding 不实现：live 模式的 embedding 走 ollama-embedding。
    """

    kind = "chat"

    def __init__(self, name: str, cfg: dict):
        super().__init__(name, cfg)
        self.model = str(self.cfg.get("model", ""))
        # S2-6：maxOutputTokens 原硬编码 1024。3.x 模型的思考 token
        # 与输出共享该预算，1024 偏紧，改由配置 max_tokens 控制
        # （默认 1024，老配置不写仍可用；s2-6 配置给 2048）。
        self.max_tokens = int(self.cfg.get("max_tokens", 1024))
        # base_url 仅供测试时指向本地桩服务；生产默认官方 API，
        # 配置里不写此项即走官方地址。
        self.base_url = str(
            self.cfg.get("base_url", GEMINI_API_BASE)).rstrip("/")

    def _key(self) -> str:
        key = os.environ.get(GEMINI_KEY_ENV, "") or ""
        if not key.strip():
            raise AdapterError(
                "missing_key",
                f"{GEMINI_KEY_ENV} 未设置：Gemini 实调用须先由 parent 提供 Key，"
                "本适配器绝不用假 Key、不静默降级")
        return key.strip()

    def _call_with_retry(self, fn):
        last: AdapterError | None = None
        for attempt in range(self.retry_max + 1):
            try:
                return fn()
            except AdapterError as exc:
                last = exc
                if exc.error_class not in ("rate_limited", "server_error"):
                    raise
                if attempt >= self.retry_max:
                    raise
                time.sleep(self._effective_backoff(exc, attempt))
        raise last  # pragma: no cover - 循环必抛其一

    def complete(self, prompt: str) -> dict:
        key = self._key()
        url = (f"{self.base_url}/models/{self.model}:generateContent"
               f"?key={key}")

        def _once() -> dict:
            parsed = _post_json(
                url,
                {"contents": [{"parts": [{"text": prompt}]}],
                 "generationConfig": {"temperature": 0.2,
                                      "maxOutputTokens": self.max_tokens}},
                self.timeout_s,
            )
            candidates = parsed.get("candidates")
            if not isinstance(candidates, list) or not candidates:
                raise AdapterError("invalid_response",
                                   "no candidates in Gemini reply")
            parts = ((candidates[0].get("content") or {}).get("parts")
                     or [])
            text = "".join(p.get("text", "") for p in parts
                           if isinstance(p, dict))
            if not text:
                raise AdapterError("invalid_response",
                                   "empty text in Gemini reply")
            usage = parsed.get("usageMetadata") or {}
            return {
                "model": self.name,
                "content": text,
                "tokens_in": int(usage.get("promptTokenCount", 0) or 0),
                "tokens_out": int(usage.get("candidatesTokenCount", 0) or 0),
                "cost_usd": 0.0,
                # S2-6：finishReason（"STOP"/"MAX_TOKENS"/…）与
                # thoughtsTokenCount 留作截断诊断；S2-5 (c) #9/#10
                # 截断时未记，无法区分模型早停与预算撞顶。
                "finish_reason": candidates[0].get("finishReason"),
                "thoughts_tokens": usage.get("thoughtsTokenCount"),
                # S2-2：后端原样 usageMetadata，供 usage 审计逐项比对。
                "backend_usage": dict(usage),
            }
        return self._call_with_retry(_once)

    def embed(self, texts: list[str]) -> dict:
        raise AdapterError(
            "not_implemented",
            "Gemini embedding 未接入：live 模式的 embedding 走 ollama-embedding")

    def probe(self) -> dict:
        # 只检查 Key 是否存在，不发任何请求，不消耗额度。
        key = os.environ.get(GEMINI_KEY_ENV, "") or ""
        if not key.strip():
            return {"ok": False, "reason": "missing_key"}
        return {"ok": True, "reason": "key_present_no_call"}


class NotImplementedAdapter(BaseAdapter):
    """桩位适配器（Groq / OpenRouter，S2-1 不实现）。

    complete/embed 一律报 not_implemented，不产生任何调用；
    Router 按链继续切到下一个供应商。
    """

    kind = "chat"

    def complete(self, prompt: str) -> dict:
        raise AdapterError(
            "not_implemented",
            f"{self.name} 未实现（S2-1 桩位）：fallback 链的该环尚未接入")

    def embed(self, texts: list[str]) -> dict:
        raise AdapterError(
            "not_implemented",
            f"{self.name} 未实现（S2-1 桩位）：fallback 链的该环尚未接入")

    def probe(self) -> dict:
        return {"ok": False, "reason": "not_implemented"}


_ADAPTER_TYPES = {
    "ollama": OllamaAdapter,
    "gemini": GeminiAdapter,
    "groq": NotImplementedAdapter,
    "openrouter": NotImplementedAdapter,
}


def build_adapter(name: str, cfg: dict) -> BaseAdapter:
    """按 config.yaml providers 一节建适配器。

    未知 type 是配置错误，立即报 ValueError（启动/调用时当场
    发现，不静默当成供应商失败切链）。
    """
    cfg = dict(cfg or {})
    adapter_type = str(cfg.get("type", ""))
    cls = _ADAPTER_TYPES.get(adapter_type)
    if cls is None:
        raise ValueError(f"unknown provider type for {name}: {adapter_type!r}")
    if cfg.get("status") == "not_implemented":
        return NotImplementedAdapter(name, cfg)
    return cls(name, cfg)
