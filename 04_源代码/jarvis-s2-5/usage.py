"""Jarvis S2-2 — 真实调用 usage 日志与审计。

S2-2（真实调用测试，最小调用，记真实 usage/token/费用）：
- 每次真实适配器调用成功，Router 记一条 usage 记录到
  data/usage.jsonl（JSONL 追加写，与事件日志同一口径：
  每行 {time, event, result, detail}，detail 为 usage 记录
  的 JSON 字符串；提示词原文与响应内容不进日志，只记 token
  数、延迟、费用与后端原样 usage 对象）。
- 只记真实调用：Stub 结果没有 backend_usage，不记；降级
  回答不记。stub 模式全量回归因此不会污染 usage 日志。
- 审计 audit_usage_log(path)：逐条比对日志里的 token 数与
  后端返回的 usage 对象（Ollama 的 prompt_eval_count /
  eval_count；Gemini 的 usageMetadata.promptTokenCount /
  candidatesTokenCount），不一致即失败；费用恒为 $0 断言。
- Key 永不进日志；Gemini 实调用须由 parent 以环境变量
  GEMINI_API_KEY 临时传入，本模块不读、不存任何 Key。

局限（诚实写明）：
- usage.jsonl 目前没有 EventLog 那样的哈希链与检查点：
  审计只校验内容一致性（token 对得上、费用为 0、字段齐全），
  不防篡改。防篡改须把检查点放独立信任域，见 P6-18 遗留，
  Phase 2 处理。
  S2-4 已补：UsageLog.append 写 prev_hash/entry_hash + 检查点
  usage.checkpoint.json（与 EventLog 同口径）；audit_usage_log
  在内容审计之外附带链校验（chain 字段）。旧格式日志（无哈希）
  判为 legacy：内容审计照常，链状态明示 unverified，不冒充已验证；
  新格式日志链断即 broken、ok=False。检查点本身被删则无法确认
  完整性（与 EventLog 同局限），须把检查点放独立信任域才彻底。
- 审计按供应商名推断 usage 形状（gemini-* 用 usageMetadata，
  ollama-* 用 prompt_eval_count/eval_count）；未知供应商名
  且 usage 对象无上述字段时报 unknown_backend_usage_shape，
  不猜测比对。
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

USAGE_EVENT = "usage"
GENESIS = "GENESIS"

# usage 记录必填字段（detail 里的 usage 对象）。
REQUIRED_FIELDS = (
    "logical",
    "provider",
    "model",
    "tokens_in",
    "tokens_out",
    "backend_usage",
    "latency_ms",
    "cost_usd",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class UsageLog:
    """usage 日志：JSONL 追加写，每行一条 usage 事件。

    与 EventLog 同一信封口径（time / event / result / detail），
    S2-4 起带哈希链（prev_hash / entry_hash）与检查点文件
    usage.checkpoint.json（见 audit_usage_log 的 chain 字段）。
    detail 是 usage 记录的 JSON 字符串；usage 记录本身只含数字
    与标识，不含提示词与响应原文。
    """

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _checkpoint_path(self) -> Path:
        return self.path.parent / (self.path.stem + ".checkpoint.json")

    def _read_checkpoint(self) -> tuple[int, str]:
        try:
            data = json.loads(
                self._checkpoint_path().read_text(encoding="utf-8"))
            return int(data["count"]), str(data["last_hash"])
        except Exception:
            return 0, GENESIS

    def _last_hash(self) -> str:
        count, last_hash = self._read_checkpoint()
        if count > 0:
            return last_hash
        if not self.path.exists():
            return GENESIS
        last_line = None
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    last_line = line
        if last_line is None:
            return GENESIS
        try:
            rec = json.loads(last_line)
            if rec.get("entry_hash"):
                return rec["entry_hash"]
        except Exception:
            pass
        return "LEGACY:" + hashlib.sha256(
            last_line.encode("utf-8")).hexdigest()

    @staticmethod
    def _entry_hash(record: dict) -> str:
        payload = "|".join([
            record.get("prev_hash", ""),
            record.get("time", ""),
            record.get("event", ""),
            record.get("result", ""),
            record.get("detail", ""),
        ])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _write_checkpoint(self, count: int, last_hash: str) -> None:
        tmp = self._checkpoint_path().with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"count": count, "last_hash": last_hash},
                       ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._checkpoint_path())

    def append(self, record: dict) -> dict:
        envelope = {
            "time": now_iso(),
            "event": USAGE_EVENT,
            "result": "ok",
            "detail": json.dumps(record, ensure_ascii=False),
            "prev_hash": self._last_hash(),
        }
        envelope["entry_hash"] = self._entry_hash(envelope)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(envelope, ensure_ascii=False) + "\n")
        try:
            with open(self.path, encoding="utf-8") as f:
                actual = sum(1 for line in f if line.strip())
        except Exception:
            actual = self._read_checkpoint()[0] + 1
        self._write_checkpoint(actual, envelope["entry_hash"])
        return envelope

    def read_all(self) -> list[dict]:
        """读全部信封记录；无法解析的行原样返回为 {"_unparsable": line}。"""
        if not self.path.exists():
            return []
        out: list[dict] = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    out.append({"_unparsable": line})
        return out


def _check_record(usage: dict, line_no: int) -> list[str]:
    """校验一条 usage 记录，返回 issue 列表（空 = 通过）。"""
    issues: list[str] = []
    for field in REQUIRED_FIELDS:
        if field not in usage:
            issues.append(f"missing_field:{field}_at:{line_no}")
    if issues:
        return issues
    tokens_in = usage["tokens_in"]
    tokens_out = usage["tokens_out"]
    if not isinstance(tokens_in, int) or isinstance(tokens_in, bool) \
            or tokens_in < 0:
        issues.append(f"bad_tokens_in_at:{line_no}")
    if not isinstance(tokens_out, int) or isinstance(tokens_out, bool) \
            or tokens_out < 0:
        issues.append(f"bad_tokens_out_at:{line_no}")
    latency = usage["latency_ms"]
    if not isinstance(latency, int) or isinstance(latency, bool) \
            or latency < 0:
        issues.append(f"bad_latency_at:{line_no}")
    cost = usage["cost_usd"]
    if not isinstance(cost, (int, float)) or isinstance(cost, bool) \
            or cost != 0.0:
        issues.append(f"nonzero_cost_at:{line_no}")
    backend = usage["backend_usage"]
    if not isinstance(backend, dict) or not backend:
        issues.append(f"missing_backend_usage_at:{line_no}")
        return issues
    provider = str(usage.get("provider", ""))
    if "promptTokenCount" in backend or provider.startswith("gemini"):
        # Gemini：usageMetadata.promptTokenCount / candidatesTokenCount。
        if "promptTokenCount" not in backend:
            issues.append(f"missing_usage_field:promptTokenCount_at:{line_no}")
        elif backend["promptTokenCount"] != tokens_in:
            issues.append(
                f"token_mismatch:tokens_in={tokens_in},"
                f"promptTokenCount={backend['promptTokenCount']}_at:{line_no}")
        if "candidatesTokenCount" not in backend:
            issues.append(
                f"missing_usage_field:candidatesTokenCount_at:{line_no}")
        elif backend["candidatesTokenCount"] != tokens_out:
            issues.append(
                f"token_mismatch:tokens_out={tokens_out},"
                f"candidatesTokenCount={backend['candidatesTokenCount']}_at:{line_no}")
    elif "prompt_eval_count" in backend or provider.startswith("ollama"):
        # Ollama：prompt_eval_count / eval_count（embedding 无 eval_count，
        # 此时 tokens_out 必须为 0）。
        if "prompt_eval_count" not in backend:
            issues.append(f"missing_usage_field:prompt_eval_count_at:{line_no}")
        elif backend["prompt_eval_count"] != tokens_in:
            issues.append(
                f"token_mismatch:tokens_in={tokens_in},"
                f"prompt_eval_count={backend['prompt_eval_count']}_at:{line_no}")
        if "eval_count" in backend:
            if backend["eval_count"] != tokens_out:
                issues.append(
                    f"token_mismatch:tokens_out={tokens_out},"
                    f"eval_count={backend['eval_count']}_at:{line_no}")
        elif tokens_out != 0:
            issues.append(f"token_mismatch:embedding_tokens_out={tokens_out}_at:{line_no}")
    else:
        issues.append(f"unknown_backend_usage_shape_at:{line_no}")
    return issues


def verify_usage_chain(path: Path) -> dict:
    """校验 usage 日志的哈希链（S2-4，与 EventLog.verify 同口径）。

    返回 {"status": ..., "issues": [...], "count": int}，status：
    - "verified"：全部记录带哈希且链与检查点一致；
    - "legacy"：整本日志无任何哈希（旧格式写出）：内容审计照常，
      但链无法验证——明示，不冒充已验证；
    - "broken"：链断、哈希不符、截断或检查点丢失（新格式日志）；
    - "missing"：文件不存在。
    新旧混写（部分记录无哈希）按 broken 计：链一旦开始就不能断。
    """
    path = Path(path)
    if not path.exists():
        return {"status": "missing", "issues": ["log_missing"], "count": 0}
    try:
        with open(path, encoding="utf-8") as f:
            raw_lines = [line for line in f if line.strip()]
    except Exception as exc:
        return {"status": "broken", "issues": [f"log_unreadable:{exc}"],
                "count": 0}
    if not raw_lines:
        return {"status": "verified", "issues": [], "count": 0}
    try:
        records = [json.loads(line) for line in raw_lines]
    except Exception as exc:
        return {"status": "broken", "issues": [f"log_unreadable:{exc}"],
                "count": 0}
    if not any(isinstance(r, dict) and r.get("entry_hash") for r in records):
        return {"status": "legacy", "issues": ["legacy_log_unverified"],
                "count": len(records)}
    issues: list[str] = []
    prev = GENESIS
    for idx, rec in enumerate(records):
        if not isinstance(rec, dict):
            issues.append(f"unparsable_line:{idx}")
            continue
        if not rec.get("entry_hash"):
            issues.append(f"legacy_entry_at:{idx}")
            prev = "LEGACY:" + hashlib.sha256(
                raw_lines[idx].encode("utf-8")).hexdigest()
            continue
        if rec.get("prev_hash") != prev:
            issues.append(f"chain_break_at:{idx}")
        if rec.get("entry_hash") != UsageLog._entry_hash(rec):
            issues.append(f"hash_mismatch_at:{idx}")
        prev = rec["entry_hash"]
    cp_path = path.parent / (path.stem + ".checkpoint.json")
    if not cp_path.exists():
        issues.append("checkpoint_missing")
    else:
        try:
            data = json.loads(cp_path.read_text(encoding="utf-8"))
            cp_count, cp_last = int(data["count"]), str(data["last_hash"])
        except Exception as exc:
            issues.append(f"checkpoint_unreadable:{exc}")
            cp_count, cp_last = -1, ""
        if cp_count >= 0:
            if len(records) > cp_count:
                issues.append(
                    f"checkpoint_stale:log={len(records)},checkpoint={cp_count}")
            elif len(records) < cp_count:
                issues.append(
                    f"truncated:log={len(records)},checkpoint={cp_count}")
            elif records and isinstance(records[-1], dict) \
                    and records[-1].get("entry_hash") != cp_last:
                issues.append("last_hash_mismatch")
    return {"status": "broken" if issues else "verified",
            "issues": issues, "count": len(records)}


def audit_usage_log(path: Path) -> dict:
    """审计 usage 日志。

    返回 {"ok": bool, "issues": [...], "total": int, "checked": int,
    "mismatches": [...], "chain": verify_usage_chain 的结果}。
    ok 要求内容审计通过且链未断（chain.status != "broken"）；
    旧格式日志 chain.status="legacy"，内容通过则 ok 仍为 True，
    但链状态明示 unverified。文件缺失时 ok=False、
    issues=["log_missing"]、total=0（不是“没有调用所以通过”：
    无日志 = 无可审计的真实调用证据）。
    """
    path = Path(path)
    chain = verify_usage_chain(path)
    if not path.exists():
        return {"ok": False, "issues": ["log_missing"], "total": 0,
                "checked": 0, "mismatches": [], "chain": chain}
    issues: list[str] = []
    mismatches: list[dict] = []
    total = 0
    try:
        with open(path, encoding="utf-8") as f:
            lines = [line for line in f if line.strip()]
    except Exception as exc:
        return {"ok": False, "issues": [f"log_unreadable:{exc}"],
                "total": 0, "checked": 0, "mismatches": [], "chain": chain}
    for idx, line in enumerate(lines):
        total += 1
        try:
            envelope = json.loads(line)
        except Exception:
            issues.append(f"unparsable_line:{idx}")
            mismatches.append({"line": idx, "provider": "",
                               "model": "", "issues": ["unparsable_line"]})
            continue
        if not isinstance(envelope, dict) or "_unparsable" in envelope:
            issues.append(f"unparsable_line:{idx}")
            mismatches.append({"line": idx, "provider": "",
                               "model": "", "issues": ["unparsable_line"]})
            continue
        if envelope.get("event") != USAGE_EVENT:
            issues.append(f"bad_event_at:{idx}")
            mismatches.append({"line": idx, "provider": "",
                               "model": "", "issues": ["bad_event"]})
            continue
        try:
            usage = json.loads(envelope.get("detail", ""))
        except Exception:
            issues.append(f"bad_detail_at:{idx}")
            mismatches.append({"line": idx, "provider": "",
                               "model": "", "issues": ["bad_detail"]})
            continue
        if not isinstance(usage, dict):
            issues.append(f"bad_detail_at:{idx}")
            mismatches.append({"line": idx, "provider": "",
                               "model": "", "issues": ["bad_detail"]})
            continue
        record_issues = _check_record(usage, idx)
        if record_issues:
            issues.extend(record_issues)
            mismatches.append({
                "line": idx,
                "provider": str(usage.get("provider", "")),
                "model": str(usage.get("model", "")),
                "issues": record_issues,
            })
    ok = not issues and chain["status"] != "broken"
    return {"ok": ok, "issues": issues, "total": total,
            "checked": total, "mismatches": mismatches, "chain": chain}
