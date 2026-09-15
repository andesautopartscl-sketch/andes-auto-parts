"""Assistant observability metrics — no secrets, no PII, no full prompts/replies."""
from __future__ import annotations

import json
import os
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.assistant.orchestrator.audit import message_hash, redact

# Alert thresholds (local ops signals)
HIGH_TOOL_COUNT = 3
HIGH_CLARIFY_STREAK = 2
HIGH_LATENCY_MS = 5000
HIGH_ERROR_RATE = 0.5  # within a conversation with >= 4 turns

PII_METRIC_KEYS = frozenset(
    {
        "rut",
        "email",
        "correo",
        "telefono",
        "teléfono",
        "phone",
        "direccion",
        "dirección",
        "address",
        "password",
        "message",
        "reply",
        "prompt",
        "system",
        "user",
        "content",
        "authorization",
        "cookie",
        "cookies",
        "api_key",
        "apikey",
        "token",
        "ventas_periodo",
        "ventas_hoy",
        "ventas_mes",
        "total",
        "monto",
        "precio",
        "costo",
    }
)


def _metrics_path() -> Path:
    raw = (os.environ.get("ANDES_ASSISTANT_METRICS_PATH") or "data/assistant_metrics.jsonl").strip()
    return Path(raw)


def _cost_per_1k(kind: str) -> float | None:
    key = "ANDES_LLM_COST_INPUT_PER_1K" if kind == "input" else "ANDES_LLM_COST_OUTPUT_PER_1K"
    raw = (os.environ.get(key) or "").strip()
    if not raw:
        return None
    try:
        val = float(raw)
    except ValueError:
        return None
    return val if val >= 0 else None


def estimate_cost_usd(
    prompt_tokens: int | None,
    completion_tokens: int | None,
) -> float | None:
    """Estimate USD cost when per-1k rates are configured; else None."""
    pin = _cost_per_1k("input")
    pout = _cost_per_1k("output")
    if pin is None and pout is None:
        return None
    total = 0.0
    known = False
    if pin is not None and prompt_tokens is not None:
        total += (int(prompt_tokens) / 1000.0) * pin
        known = True
    if pout is not None and completion_tokens is not None:
        total += (int(completion_tokens) / 1000.0) * pout
        known = True
    return round(total, 6) if known else None


def sanitize_metric(record: dict[str, Any]) -> dict[str, Any]:
    """Drop/redact PII and secret-bearing fields from a metric record."""
    safe = redact(record)

    def _scrub(obj: Any) -> Any:
        if isinstance(obj, dict):
            out = {}
            for key, val in obj.items():
                lk = str(key).strip().lower()
                # Keep aggregate token/cost counters (never raw auth tokens)
                if lk in {
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                    "cost_estimated_usd",
                }:
                    out[key] = _scrub(val)
                    continue
                if lk in PII_METRIC_KEYS or any(
                    p in lk for p in ("password", "token", "secret", "authorization", "cookie", "api_key")
                ):
                    continue
                if lk in {"message", "reply", "prompt", "system_prompt", "user_prompt"}:
                    continue
                out[key] = _scrub(val)
            return out
        if isinstance(obj, list):
            return [_scrub(v) for v in obj]
        if isinstance(obj, str):
            # Never keep email-looking strings in metrics
            if "@" in obj and "." in obj:
                return "[redacted]"
            return obj
        return obj

    cleaned = _scrub(safe)
    if not isinstance(cleaned, dict):
        return {"ok": False, "error_code": "invalid_metric"}
    # Never persist full message text
    cleaned.pop("message", None)
    cleaned.pop("reply", None)
    return cleaned


def build_turn_metric(
    *,
    actor_user: str,
    conversation_id: str,
    correlation_id: str,
    message_hash_value: str,
    ok: bool,
    error_code: str | None = None,
    phase: str | None = None,
    scenario: str | None = None,
    classification: str | None = None,
    tools_used: list[str] | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    invoke_count: int | None = None,
    needs_clarification: bool = False,
    reuse_prior_evidence: bool = False,
    replan_count: int = 0,
    llm_latency_ms: int = 0,
    total_latency_ms: int = 0,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
    planner_mode: str | None = None,
    memory_candidates: int = 0,
    memory_selected: int = 0,
    memory_budget_chars: int = 0,
    memory_types: list[str] | None = None,
    memory_write_attempt: bool = False,
    memory_write_success: bool = False,
    memory_write_rejected: bool = False,
    memory_write_reason: str | None = None,
    memory_write_type: str | None = None,
    memory_write_scope: str | None = None,
    permission_epoch_read: int | None = None,
    memory_contextual_invalidated: int = 0,
    memory_contextual_selected: int = 0,
    permission_epoch_error: bool = False,
    derived_candidates: int = 0,
    derived_accepted: int = 0,
    derived_rejected: int = 0,
    derived_reject_reason: str | None = None,
    derived_type: str | None = None,
    derived_scope: str | None = None,
    derived_confidence: float | None = None,
) -> dict[str, Any]:
    tools = [str(t) for t in (tools_used or []) if t]
    calls = []
    permission_denied = False
    agent_unavailable = False
    for call in tool_calls or []:
        if not isinstance(call, dict):
            continue
        code = call.get("error_code")
        if code == "permission_denied":
            permission_denied = True
        if code in {"agent_unavailable", "agent_timeout", "erp_unavailable"}:
            agent_unavailable = True
        calls.append(
            {
                "tool": call.get("tool"),
                "ok": bool(call.get("ok")),
                "error_code": code,
                "latency_ms": call.get("latency_ms"),
            }
        )
    if invoke_count is None:
        invoke_count = len(calls) if calls else len(tools)

    cost = estimate_cost_usd(prompt_tokens, completion_tokens)
    alerts: list[str] = []
    if len(tools) >= HIGH_TOOL_COUNT or invoke_count >= HIGH_TOOL_COUNT:
        alerts.append("too_many_tools")
    if needs_clarification:
        alerts.append("clarification")
    if total_latency_ms >= HIGH_LATENCY_MS:
        alerts.append("high_latency")
    if not ok or error_code:
        alerts.append("error")
    if permission_denied:
        alerts.append("permission_denied")
    if agent_unavailable:
        alerts.append("agent_unavailable")

    record = {
        "kind": "turn_metric",
        "actor_user": (actor_user or "")[:80],
        "conversation_id": (conversation_id or "")[:80],
        "correlation_id": (correlation_id or "")[:80],
        "message_hash": message_hash_value,
        "ok": bool(ok),
        "error_code": error_code,
        "phase": phase,
        "scenario": scenario,
        "classification": classification,
        "planner_mode": planner_mode,
        "tools_used": tools,
        "tool_selected": tools[0] if len(tools) == 1 else None,
        "tools_count": len(tools),
        "invoke_count": int(invoke_count),
        "tool_calls": calls,
        "needs_clarification": bool(needs_clarification),
        "reuse_prior_evidence": bool(reuse_prior_evidence),
        "replan_count": int(replan_count or 0),
        "llm_latency_ms": int(llm_latency_ms or 0),
        "total_latency_ms": int(total_latency_ms or 0),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cost_estimated_usd": cost,
        "permission_denied": permission_denied,
        "agent_unavailable": agent_unavailable,
        "alerts": alerts,
        "success": bool(ok) and not error_code,
        "memory_candidates": int(memory_candidates or 0),
        "memory_selected": int(memory_selected or 0),
        "memory_budget_chars": int(memory_budget_chars or 0),
        "memory_types": [str(t) for t in (memory_types or []) if t][:12],
        "memory_write_attempt": bool(memory_write_attempt),
        "memory_write_success": bool(memory_write_success),
        "memory_write_rejected": bool(memory_write_rejected),
        "memory_write_reason": (str(memory_write_reason)[:80] if memory_write_reason else None),
        "memory_write_type": (str(memory_write_type)[:40] if memory_write_type else None),
        "memory_write_scope": (str(memory_write_scope)[:20] if memory_write_scope else None),
        "permission_epoch_read": (
            int(permission_epoch_read) if permission_epoch_read is not None else None
        ),
        "memory_contextual_invalidated": int(memory_contextual_invalidated or 0),
        "memory_contextual_selected": int(memory_contextual_selected or 0),
        "permission_epoch_error": bool(permission_epoch_error),
        "derived_candidates": int(derived_candidates or 0),
        "derived_accepted": int(derived_accepted or 0),
        "derived_rejected": int(derived_rejected or 0),
        "derived_reject_reason": (
            str(derived_reject_reason)[:80] if derived_reject_reason else None
        ),
        "derived_type": (str(derived_type)[:40] if derived_type else None),
        "derived_scope": (str(derived_scope)[:20] if derived_scope else None),
        "derived_confidence": (
            round(float(derived_confidence), 3) if derived_confidence is not None else None
        ),
    }
    return sanitize_metric(record)


class MetricsStore:
    """In-memory conversation aggregates + append-only JSONL for local ops."""

    def __init__(self, path: Path | None = None, *, ttl_seconds: int = 1800):
        self.path = path or _metrics_path()
        self.ttl_seconds = int(ttl_seconds)
        self._lock = threading.Lock()
        self._convos: dict[tuple[str, str], dict[str, Any]] = {}

    def _key(self, actor: str, conversation_id: str) -> tuple[str, str] | None:
        a = (actor or "").strip()
        c = (conversation_id or "").strip()[:80]
        if not a or not c:
            return None
        return (a, c)

    def _prune(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        dead = [
            k
            for k, v in self._convos.items()
            if now - float(v.get("updated_ts") or 0) > self.ttl_seconds
        ]
        for k in dead:
            self._convos.pop(k, None)

    def record_turn(self, metric: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
        safe = sanitize_metric(metric)
        key = self._key(str(safe.get("actor_user") or ""), str(safe.get("conversation_id") or ""))
        with self._lock:
            self._prune()
            agg = None
            if key is not None:
                agg = self._convos.get(key) or _empty_conversation(key[0], key[1])
                _update_conversation(agg, safe)
                self._convos[key] = agg
                safe["conversation"] = _public_conversation_snapshot(agg)
            if persist:
                self._append_jsonl(safe)
            return deepcopy(safe)

    def _append_jsonl(self, record: dict[str, Any]) -> None:
        line_obj = dict(record)
        line_obj.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line_obj, ensure_ascii=False) + "\n")

    def get_conversation(self, actor_user: str, conversation_id: str) -> dict[str, Any] | None:
        key = self._key(actor_user, conversation_id)
        if key is None:
            return None
        with self._lock:
            self._prune()
            agg = self._convos.get(key)
            return deepcopy(agg) if agg else None

    def summary(self, *, limit_conversations: int = 50) -> dict[str, Any]:
        """Local dashboard summary — aggregates only, no messages/PII."""
        with self._lock:
            self._prune()
            convos = sorted(
                self._convos.values(),
                key=lambda c: float(c.get("updated_ts") or 0),
                reverse=True,
            )[: max(1, limit_conversations)]
            snapshots = [_public_conversation_snapshot(c) for c in convos]

        # Also scan recent JSONL for process-wide counters (best-effort)
        file_stats = _summarize_jsonl(self.path, max_lines=500)
        hotspots = {
            "too_many_tools": file_stats.get("alert_counts", {}).get("too_many_tools", 0),
            "clarifications": file_stats.get("alert_counts", {}).get("clarification", 0),
            "high_latency": file_stats.get("alert_counts", {}).get("high_latency", 0),
            "errors": file_stats.get("alert_counts", {}).get("error", 0),
            "permission_denied": file_stats.get("alert_counts", {}).get("permission_denied", 0),
            "agent_unavailable": file_stats.get("alert_counts", {}).get("agent_unavailable", 0),
        }
        return {
            "ok": True,
            "source": str(self.path),
            "in_memory_conversations": snapshots,
            "file_stats": file_stats,
            "hotspots": hotspots,
        }

    def clear(self) -> None:
        with self._lock:
            self._convos.clear()


def _empty_conversation(actor: str, conversation_id: str) -> dict[str, Any]:
    now = time.time()
    return {
        "actor_user": actor,
        "conversation_id": conversation_id,
        "turns": 0,
        "invokes": 0,
        "clarifications": 0,
        "replans": 0,
        "reuse_count": 0,
        "successes": 0,
        "errors": 0,
        "tools_histogram": {},
        "permission_denied_count": 0,
        "agent_unavailable_count": 0,
        "total_latency_ms": 0,
        "llm_latency_ms": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cost_estimated_usd": 0.0,
        "cost_known": False,
        "created_ts": now,
        "updated_ts": now,
        "alerts": [],
    }


def _update_conversation(agg: dict[str, Any], turn: dict[str, Any]) -> None:
    agg["turns"] = int(agg.get("turns") or 0) + 1
    agg["invokes"] = int(agg.get("invokes") or 0) + int(turn.get("invoke_count") or 0)
    agg["replans"] = int(agg.get("replans") or 0) + int(turn.get("replan_count") or 0)
    if turn.get("needs_clarification"):
        agg["clarifications"] = int(agg.get("clarifications") or 0) + 1
    if turn.get("reuse_prior_evidence"):
        agg["reuse_count"] = int(agg.get("reuse_count") or 0) + 1
    if turn.get("success"):
        agg["successes"] = int(agg.get("successes") or 0) + 1
    else:
        agg["errors"] = int(agg.get("errors") or 0) + 1
    if turn.get("permission_denied"):
        agg["permission_denied_count"] = int(agg.get("permission_denied_count") or 0) + 1
    if turn.get("agent_unavailable"):
        agg["agent_unavailable_count"] = int(agg.get("agent_unavailable_count") or 0) + 1
    agg["total_latency_ms"] = int(agg.get("total_latency_ms") or 0) + int(
        turn.get("total_latency_ms") or 0
    )
    agg["llm_latency_ms"] = int(agg.get("llm_latency_ms") or 0) + int(turn.get("llm_latency_ms") or 0)
    for tool in turn.get("tools_used") or []:
        hist = agg.setdefault("tools_histogram", {})
        hist[str(tool)] = int(hist.get(str(tool)) or 0) + 1
    if turn.get("prompt_tokens") is not None:
        agg["prompt_tokens"] = int(agg.get("prompt_tokens") or 0) + int(turn["prompt_tokens"])
    if turn.get("completion_tokens") is not None:
        agg["completion_tokens"] = int(agg.get("completion_tokens") or 0) + int(
            turn["completion_tokens"]
        )
    if turn.get("cost_estimated_usd") is not None:
        agg["cost_estimated_usd"] = float(agg.get("cost_estimated_usd") or 0.0) + float(
            turn["cost_estimated_usd"]
        )
        agg["cost_known"] = True
    alerts = list(agg.get("alerts") or [])
    for a in turn.get("alerts") or []:
        if a not in alerts:
            alerts.append(a)
    # Conversation-level hotspot flags
    turns_n = int(agg["turns"])
    if turns_n >= 4:
        err_rate = float(agg["errors"]) / float(turns_n)
        if err_rate >= HIGH_ERROR_RATE and "frequent_errors" not in alerts:
            alerts.append("frequent_errors")
    if int(agg.get("clarifications") or 0) >= HIGH_CLARIFY_STREAK and "too_many_clarifications" not in alerts:
        alerts.append("too_many_clarifications")
    agg["alerts"] = alerts
    agg["updated_ts"] = time.time()


def _public_conversation_snapshot(agg: dict[str, Any]) -> dict[str, Any]:
    turns = max(1, int(agg.get("turns") or 0))
    return {
        "conversation_id": agg.get("conversation_id"),
        "actor_user": agg.get("actor_user"),
        "turns": agg.get("turns"),
        "invokes": agg.get("invokes"),
        "clarifications": agg.get("clarifications"),
        "replans": agg.get("replans"),
        "reuse_count": agg.get("reuse_count"),
        "successes": agg.get("successes"),
        "errors": agg.get("errors"),
        "tools_histogram": dict(agg.get("tools_histogram") or {}),
        "permission_denied_count": agg.get("permission_denied_count"),
        "agent_unavailable_count": agg.get("agent_unavailable_count"),
        "total_latency_ms": agg.get("total_latency_ms"),
        "llm_latency_ms": agg.get("llm_latency_ms"),
        "avg_latency_ms": int(int(agg.get("total_latency_ms") or 0) / turns),
        "prompt_tokens": agg.get("prompt_tokens"),
        "completion_tokens": agg.get("completion_tokens"),
        "cost_estimated_usd": agg.get("cost_estimated_usd") if agg.get("cost_known") else None,
        "alerts": list(agg.get("alerts") or []),
    }


def _summarize_jsonl(path: Path, *, max_lines: int = 500) -> dict[str, Any]:
    if not path.is_file():
        return {
            "turns": 0,
            "alert_counts": {},
            "error_codes": {},
            "tools_histogram": {},
            "avg_latency_ms": 0,
            "avg_llm_latency_ms": 0,
        }
    lines = path.read_text(encoding="utf-8").splitlines()[-max_lines:]
    turns = 0
    alert_counts: dict[str, int] = {}
    error_codes: dict[str, int] = {}
    tools_histogram: dict[str, int] = {}
    total_latency = 0
    llm_latency = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("kind") != "turn_metric":
            continue
        turns += 1
        total_latency += int(row.get("total_latency_ms") or 0)
        llm_latency += int(row.get("llm_latency_ms") or 0)
        for a in row.get("alerts") or []:
            alert_counts[str(a)] = alert_counts.get(str(a), 0) + 1
        if row.get("error_code"):
            code = str(row["error_code"])
            error_codes[code] = error_codes.get(code, 0) + 1
        for tool in row.get("tools_used") or []:
            tools_histogram[str(tool)] = tools_histogram.get(str(tool), 0) + 1
    return {
        "turns": turns,
        "alert_counts": alert_counts,
        "error_codes": error_codes,
        "tools_histogram": tools_histogram,
        "avg_latency_ms": int(total_latency / turns) if turns else 0,
        "avg_llm_latency_ms": int(llm_latency / turns) if turns else 0,
    }


_DEFAULT_STORE: MetricsStore | None = None
_DEFAULT_LOCK = threading.Lock()


def get_default_metrics_store() -> MetricsStore:
    global _DEFAULT_STORE
    with _DEFAULT_LOCK:
        if _DEFAULT_STORE is None:
            _DEFAULT_STORE = MetricsStore()
        return _DEFAULT_STORE


def hash_message_for_metrics(message: str) -> str:
    return message_hash(message)
