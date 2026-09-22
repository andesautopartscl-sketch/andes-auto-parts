"""FASE 7B.5 — controlled derived memory (frequent_entity + conversation_summary).

Post-turn, best-effort. Never ToolRunner. Never permissions/WRITE.
Pre-threshold counters are technical-only (DerivedCounterStore), not memory slots.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.assistant.orchestrator.catalog import ALLOWED_TOOLS
from app.assistant.orchestrator.conversation_context import extract_entities_from_evidence
from app.assistant.orchestrator.memory_config import (
    memory_approval_enabled,
    memory_derived_enabled,
    memory_enabled,
)
from app.assistant.orchestrator.memory_derived_counters import (
    MIN_DISTINCT_CONVERSATIONS,
    MIN_GAP_SECONDS,
    MIN_QUALIFIED_HITS,
    WINDOW_DAYS,
    DerivedCounterStore,
    get_default_derived_counter_store,
)
from app.assistant.orchestrator.memory_epoch import (
    resolve_actor_permission_epoch,
    sensitivity_for_memory_type,
)
from app.assistant.orchestrator.memory_explicit import is_poison_or_forbidden_text
from app.assistant.orchestrator.memory_schema import MemorySchemaError
from app.assistant.orchestrator.memory_store import MemoryStore, get_default_memory_store

logger = logging.getLogger(__name__)

DERIVED_ENTITY_KINDS = frozenset(
    {"codigo", "sku", "producto_id", "proveedor_id", "cliente_id", "bodega_id"}
)
MIN_CONFIDENCE = 0.75
SUMMARY_TEXT_SOFT_MAX = 160
SUMMARY_TEXT_HARD_MAX = 240
SUMMARY_MAX_TOOLS = 3
SUMMARY_MIN_EVIDENCE_TURNS = 2
SUMMARY_KEY = "summary.v1"
MAX_FREQUENT_HINTS = 2
MAX_SUMMARY_HINTS = 1

_INSTRUCTION_RE = re.compile(
    r"\b(recuerda(?:\s+que)?|acu[eé]rdate(?:\s+de)?|guarda(?:\s+esto)?)\b",
    re.IGNORECASE,
)
_CODE_RE = re.compile(r"\b([A-Za-z0-9][A-Za-z0-9._\-]{1,39})\b")
_PII_RE = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    r"|\b\d{1,2}\.?\d{3}\.?\d{3}-[\dkK]\b"
    r"|\b\+?\d[\d\s\-()]{7,}\b",
)
_AMOUNT_RE = re.compile(r"\$\s*\d|\b\d+[.,]\d{2}\b|\bmonto\b|\bprecio\b", re.IGNORECASE)

_READ_TOOLS = frozenset(ALLOWED_TOOLS)

_TOPIC_BY_TOOL: dict[str, str] = {
    "search_catalog": "Catalogo",
    "get_product": "Producto",
    "get_inventory": "Inventario",
    "check_stock": "Inventario",
    "get_stock_movements": "Movimientos",
    "get_ingresos": "Ingresos",
    "get_purchase_orders": "Ordenes",
    "get_customer": "Cliente",
    "get_supplier": "Proveedor",
    "get_dashboard_kpis": "Dashboard",
}


@dataclass
class DerivedApplyResult:
    candidates: int = 0
    accepted: int = 0
    rejected: int = 0
    reject_reason: str | None = None
    memory_type: str | None = None
    scope: str | None = None
    confidence: float | None = None
    slots: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _looks_unsafe_value(raw: str) -> bool:
    text = raw or ""
    if is_poison_or_forbidden_text(text):
        return True
    if _PII_RE.search(text) or "@" in text:
        return True
    if _AMOUNT_RE.search(text):
        return True
    return False


def _normalize_kind_value(kind: str, value: str) -> tuple[str, str] | None:
    k = (kind or "").strip().lower()
    v = (value or "").strip()
    if k == "sku":
        k = "codigo"
    if k not in DERIVED_ENTITY_KINDS:
        return None
    if k == "codigo":
        v = v.upper()
    if not v or len(v) > 40:
        return None
    if _looks_unsafe_value(v):
        return None
    return k, v


def extract_derived_entities(
    *,
    message: str,
    evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return at most one observation per (kind, value) for this turn."""
    if _INSTRUCTION_RE.search(message or ""):
        return []
    if is_poison_or_forbidden_text(message or ""):
        return []
    if _looks_unsafe_value(message or ""):
        return []

    found: dict[tuple[str, str], dict[str, Any]] = {}
    ok_items = [
        item
        for item in (evidence or [])
        if isinstance(item, dict) and item.get("ok") and not item.get("empty")
    ]
    if not ok_items:
        return []

    ents = extract_entities_from_evidence(ok_items)
    code = str(ents.get("codigo") or "").strip().upper()
    if code:
        nv = _normalize_kind_value("codigo", code)
        if nv:
            found[nv] = {"kind": nv[0], "value": nv[1], "confidence": 0.9, "source": "tool_data"}

    for item in ok_items:
        tool = str(item.get("tool") or "")
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        extra: list[tuple[str, str, float]] = []
        if tool == "get_product" and data.get("producto_id"):
            extra.append(("producto_id", str(data.get("producto_id")), 0.9))
        if tool == "get_supplier" and data.get("id"):
            extra.append(("proveedor_id", str(data.get("id")), 0.9))
        if tool == "get_customer" and data.get("id"):
            extra.append(("cliente_id", str(data.get("id")), 0.9))
        bodega = data.get("bodega_id") or data.get("bodega")
        if bodega and tool in {"get_inventory", "check_stock"}:
            extra.append(("bodega_id", str(bodega), 0.85))
        for kind, raw, conf in extra:
            nv = _normalize_kind_value(kind, raw)
            if nv and nv not in found:
                found[nv] = {
                    "kind": nv[0],
                    "value": nv[1],
                    "confidence": conf,
                    "source": "tool_data",
                }

    # Secondary: message mentions a tool-backed codigo (same turn).
    msg_tokens = {m.group(1).upper() for m in _CODE_RE.finditer(message or "")}
    for item in ok_items:
        if str(item.get("tool") or "") not in _READ_TOOLS:
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        codes: list[str] = []
        if data.get("codigo"):
            codes.append(str(data["codigo"]).strip().upper())
        for row in data.get("items") or []:
            if isinstance(row, dict) and row.get("codigo"):
                codes.append(str(row["codigo"]).strip().upper())
        for c in codes:
            nv = _normalize_kind_value("codigo", c)
            if not nv:
                continue
            if nv in found:
                continue
            if c in msg_tokens:
                found[nv] = {
                    "kind": nv[0],
                    "value": nv[1],
                    "confidence": 0.75,
                    "source": "message_and_tool",
                }

    out = [v for v in found.values() if float(v.get("confidence") or 0) >= MIN_CONFIDENCE]
    return out[:8]


def _evidence_turn_count(turns: list[dict[str, Any]]) -> int:
    """Count turns that already include tool evidence (call after _save_turn)."""
    n = 0
    for turn in turns or []:
        if not isinstance(turn, dict):
            continue
        ev = turn.get("evidence") if isinstance(turn.get("evidence"), list) else []
        tools = turn.get("tools_used") or []
        ok = any(isinstance(x, dict) and x.get("ok") and not x.get("empty") for x in ev)
        if ok or tools:
            n += 1
    return n


def build_conversation_summary(
    *,
    evidence: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Deterministic summary from successful tools + typed entities. No LLM."""
    ok_items = [
        item
        for item in (evidence or [])
        if isinstance(item, dict)
        and item.get("ok")
        and not item.get("empty")
        and str(item.get("tool") or "") in ALLOWED_TOOLS
    ]
    if not ok_items:
        return None
    tools: list[str] = []
    for item in ok_items:
        tool = str(item.get("tool") or "").strip()
        if tool and tool not in tools:
            tools.append(tool)
        if len(tools) >= SUMMARY_MAX_TOOLS:
            break
    if not tools:
        return None
    ents = extract_entities_from_evidence(ok_items)
    codes: list[str] = []
    for c in [ents.get("codigo"), *(ents.get("codigos") or [])]:
        raw = str(c or "").strip().upper()
        nv = _normalize_kind_value("codigo", raw) if raw else None
        if nv and nv[1] not in codes:
            codes.append(nv[1])
        if len(codes) >= 3:
            break
    topic = _TOPIC_BY_TOOL.get(tools[0], "Consulta")
    parts = [topic]
    if codes:
        parts.append("entidades: " + ",".join(codes))
    parts.append("tools: " + ",".join(tools))
    text = "; ".join(parts)
    text = text[:SUMMARY_TEXT_SOFT_MAX].strip()
    if not text:
        return None
    if is_poison_or_forbidden_text(text) or _looks_unsafe_value(text):
        return None
    return {"text": text[:SUMMARY_TEXT_HARD_MAX], "tools": tools[:SUMMARY_MAX_TOOLS]}


def _stamp_and_upsert(
    *,
    actor: str,
    memory_type: str,
    key: str,
    value: dict[str, Any],
    scope: str,
    conversation_id: str | None,
    confidence: float,
    store: MemoryStore,
) -> tuple[dict[str, Any] | None, str | None]:
    sens = sensitivity_for_memory_type(memory_type)
    epoch_arg: int | None = None
    if sens == "contextual":
        epoch_res = resolve_actor_permission_epoch(actor)
        if not epoch_res.available or epoch_res.epoch is None:
            return None, "permission_epoch_unavailable"
        epoch_arg = int(epoch_res.epoch)
    slot = store.upsert(
        actor_user=actor,
        scope=scope,
        memory_type=memory_type,
        key=key,
        value=value,
        conversation_id=conversation_id,
        source="derived",
        confidence=confidence,
        permission_epoch=epoch_arg,
        sensitivity=sens,
        # FASE 10.2.2 — LA POLITICA. Lo que el sistema INFIERE nace pendiente de
        # aprobacion; lo que el usuario pide explicitamente no pasa por aqui.
        #
        # Con la bandera apagada nace `approved`, que es el comportamiento de
        # 10.2.1 y de siempre. Solo lo NUEVO cambia de estado: esto no reetiqueta
        # nada retroactivamente, porque el upsert no toca `status` salvo que se
        # le pase (ver el CASE WHEN de MemoryStore.upsert).
        status="suggested" if memory_approval_enabled() else None,
        verify_conversation=False,
    )
    if slot is None:
        return None, "memory_write_failed"
    return slot, None


def apply_derived_memory(
    *,
    message: str,
    actor_user: str,
    conversation_id: str,
    evidence: list[dict[str, Any]] | None,
    turns: list[dict[str, Any]] | None = None,
    store: MemoryStore | None = None,
    counters: DerivedCounterStore | None = None,
    now: datetime | None = None,
    tools_used: list[Any] | None = None,
    reuse_prior_evidence: bool = False,
) -> DerivedApplyResult:
    """Post-turn evaluator. Soft-fails. Never raises.

    Frequent hits require a tool executed on THIS turn. Replayed prior evidence
    (reuse_prior_evidence or tools_used=[]) is not a qualified hit.
    tools_used=None keeps evaluator-only tests that pass evidence directly.
    """
    out = DerivedApplyResult()
    if not memory_enabled() or not memory_derived_enabled():
        return out
    actor = (actor_user or "").strip()
    cid = (conversation_id or "").strip()[:80]
    if not actor:
        return out
    try:
        mem = store if store is not None else get_default_memory_store()
        if counters is not None:
            ctr = counters
        elif store is not None:
            ctr = DerivedCounterStore(path=mem.path)
        else:
            ctr = get_default_derived_counter_store()
        ev = evidence or []
        now_dt = now or _utc_now()

        current_tool = False
        if reuse_prior_evidence:
            current_tool = False
        elif tools_used is None:
            current_tool = True
        else:
            current_tool = any(str(t) in _READ_TOOLS for t in tools_used if t)

        entities = extract_derived_entities(message=message, evidence=ev) if current_tool else []
        if not current_tool and ev:
            out.reject_reason = out.reject_reason or "reuse_or_no_tool"
        out.candidates += len(entities)
        if entities and not cid:
            out.rejected += len(entities)
            out.reject_reason = "conversation_required"
            entities = []

        for ent in entities:
            conf = float(ent.get("confidence") or 0)
            out.confidence = conf
            if conf < MIN_CONFIDENCE:
                out.rejected += 1
                out.reject_reason = "confidence"
                continue
            snap = ctr.record_hit(
                actor_user=actor,
                kind=str(ent["kind"]),
                value=str(ent["value"]),
                conversation_id=cid,
                now=now_dt,
                window_days=WINDOW_DAYS,
                min_gap_seconds=MIN_GAP_SECONDS,
            )
            if snap is None:
                out.rejected += 1
                out.reject_reason = "counter_failed"
                continue
            if not snap.get("hit_accepted"):
                out.rejected += 1
                out.reject_reason = str(snap.get("reason") or "gap")
                continue
            if not snap.get("meets_threshold"):
                out.rejected += 1
                out.reject_reason = "threshold"
                continue
            slot, err = _stamp_and_upsert(
                actor=actor,
                memory_type="frequent_entity",
                key=f"freq.{ent['kind']}.{ent['value']}",
                value={
                    "kind": ent["kind"],
                    "value": ent["value"],
                    "hit_count": int(snap.get("qualified_hits") or MIN_QUALIFIED_HITS),
                },
                scope="user",
                conversation_id=None,
                confidence=conf,
                store=mem,
            )
            if slot is None:
                out.rejected += 1
                out.reject_reason = err or "memory_write_failed"
                continue
            out.accepted += 1
            out.memory_type = "frequent_entity"
            out.scope = "user"
            out.slots.append(slot)

        # conversation_summary — conversation-scoped, deterministic
        summary = build_conversation_summary(evidence=ev)
        if summary is None:
            if out.candidates == 0 and not entities:
                # chitchat / no tools
                pass
        else:
            out.candidates += 1
            if not cid:
                out.rejected += 1
                out.reject_reason = out.reject_reason or "conversation_required"
            else:
                n_turns = _evidence_turn_count(turns or [])
                if n_turns < SUMMARY_MIN_EVIDENCE_TURNS:
                    out.rejected += 1
                    out.reject_reason = out.reject_reason or "summary_min_turns"
                else:
                    slot, err = _stamp_and_upsert(
                        actor=actor,
                        memory_type="conversation_summary",
                        key=SUMMARY_KEY,
                        value=summary,
                        scope="conversation",
                        conversation_id=cid,
                        confidence=0.8,
                        store=mem,
                    )
                    if slot is None:
                        out.rejected += 1
                        out.reject_reason = out.reject_reason or err or "memory_write_failed"
                    else:
                        out.accepted += 1
                        out.memory_type = out.memory_type or "conversation_summary"
                        out.scope = out.scope or "conversation"
                        out.slots.append(slot)
                        out.confidence = out.confidence or 0.8
        return out
    except MemorySchemaError as exc:
        out.rejected += 1
        out.reject_reason = exc.code
        logger.warning("assistant_memory derived schema rejected: %s", exc.code)
        return out
    except Exception as exc:  # noqa: BLE001
        out.error = type(exc).__name__
        logger.warning("assistant_memory derived evaluator failed: %s", type(exc).__name__)
        return out
