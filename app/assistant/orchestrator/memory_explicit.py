"""FASE 7B.3 — explicit memory intent detection + controlled writes.

Only preference | ui_pref | pinned_entity.
Never derived types. Never permissions/WRITE elevation.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from app.assistant.orchestrator.memory_config import (
    memory_enabled,
    memory_explicit_enabled,
)
from app.assistant.orchestrator.memory_schema import MEMORY_TYPES, MemorySchemaError
from app.assistant.orchestrator.memory_store import MemoryStore, get_default_memory_store

logger = logging.getLogger(__name__)

EXPLICIT_WRITABLE_TYPES = frozenset({"preference", "ui_pref", "pinned_entity"})

# Unequivocal save signals (Spanish)
_RECUERDA_RE = re.compile(
    r"\b(recuerda(?:\s+que)?|acu[eé]rdate(?:\s+de)?|guarda(?:\s+esto)?|"
    r"mi\s+preferencia\s+es|prefiero)\b",
    re.IGNORECASE,
)
_PIN_RE = re.compile(
    r"\b(?P<verb>fij[ae]|pinnea|pinea|pin(?:ea)?|ancla)\b",
    re.IGNORECASE,
)
_PIN_PRODUCT_RE = re.compile(
    r"\b(?:producto|c[oó]digo)\s+(?P<code>[A-Za-z0-9][A-Za-z0-9._\-]{1,39})\b",
    re.IGNORECASE,
)
_PIN_AFTER_VERB_RE = re.compile(
    r"\b(?:fij[ae]|pinnea|pinea|ancla)\b(?:\s+el)?(?:\s+producto)?\s+"
    r"(?P<code>[A-Za-z0-9][A-Za-z0-9._\-]{1,39})\b",
    re.IGNORECASE,
)
_TEMPORAL_RE = re.compile(
    r"\b(hoy|ahora|esta\s+vez|por\s+ahora|moment[aá]neamente|solo\s+hoy)\b",
    re.IGNORECASE,
)
_STYLE_BRIEF_RE = re.compile(
    r"\b(breve|breves|corto|cortas|concis[oa]s?|resumid[oa])\b",
    re.IGNORECASE,
)
_STYLE_DETAILED_RE = re.compile(
    r"\b(detallad[oa]s?|extens[oa]s?|completas?|larg[oa]s?)\b",
    re.IGNORECASE,
)
_STYLE_OPERATIONAL_RE = re.compile(
    r"\b(operacional(?:es)?|t[eé]cnic[oa]s?|pr[aá]ctic[oa]s?)\b",
    re.IGNORECASE,
)
_COMPACT_ON_RE = re.compile(r"\b(compact[oa]|ui\s+compacta|vista\s+compacta)\b", re.IGNORECASE)
_COMPACT_OFF_RE = re.compile(r"\b(sin\s+compact[oa]|vista\s+amplia|no\s+compact[oa])\b", re.IGNORECASE)
_CODIGO_RE = re.compile(
    r"\b(?:producto|c[oó]digo|sku)?\s*[#:]?\s*(?P<code>[A-Za-z0-9][A-Za-z0-9._\-]{1,39})\b",
    re.IGNORECASE,
)
_FUTURE_SCOPE_RE = re.compile(
    r"\b(futuras?\s+conversaciones?|siempre|para\s+siempre|en\s+general)\b",
    re.IGNORECASE,
)
_THIS_CONV_RE = re.compile(
    r"\b(esta\s+conversaci[oó]n|esta\s+charla|aqu[ií])\b",
    re.IGNORECASE,
)

# Memory poisoning / permission / WRITE / system override
_POISON_RE = re.compile(
    r"\b(ignora(?:r)?\s+(todas\s+)?(las\s+)?reglas|bypass|jailbreak|"
    r"system\s*prompt|instrucciones?\s+de\s+sistema|"
    r"acceso\s+financiero|ver_finanzas|mod_finanzas|"
    r"permiso(?:s)?\s+(para\s+)?(ver|tener|usar)|"
    r"tengo\s+permiso|ot[oó]rgame\s+permiso|"
    r"write\s*=\s*true|herramientas?\s+de\s+escritura|"
    r"crear\s+factura|anular\s+factura|modificar\s+permisos?|"
    r"allowlist|capabilities?)\b|"
    r"\b(Bearer|Authorization|api[_-]?key|password|csrf|m2m)\b",
    re.IGNORECASE,
)


@dataclass
class ExplicitMemoryIntent:
    matched: bool
    memory_type: str | None = None
    key: str | None = None
    value: dict[str, Any] | None = None
    scope: str = "user"
    conversation_id: str | None = None
    reject_code: str | None = None
    reject_reason: str | None = None
    confirmation: str | None = None
    remainder_message: str | None = None  # query part after stripping memory clause


@dataclass
class ExplicitWriteResult:
    ok: bool
    rejected: bool = False
    error_code: str | None = None
    message: str | None = None
    slot: dict[str, Any] | None = None
    confirmation: str | None = None
    memory_type: str | None = None
    scope: str | None = None


def is_poison_or_forbidden_text(text: str) -> bool:
    return bool(_POISON_RE.search(text or ""))


def detect_explicit_memory_intent(
    message: str,
    *,
    conversation_id: str = "",
) -> ExplicitMemoryIntent:
    """Detect unequivocal explicit-memory signals. Ambiguous → no match."""
    text = (message or "").strip()
    if not text:
        return ExplicitMemoryIntent(matched=False)

    if is_poison_or_forbidden_text(text):
        return ExplicitMemoryIntent(
            matched=True,
            reject_code="memory_poisoning",
            reject_reason="forbidden_instruction_or_permission_claim",
        )

    # Temporal "hoy prefiero" without recuerda → not permanent
    if _TEMPORAL_RE.search(text) and re.search(r"\bprefiero\b", text, re.I):
        if not re.search(r"\b(recuerda|acu[eé]rdate|guarda)\b", text, re.I):
            return ExplicitMemoryIntent(matched=False)

    pin_m = _PIN_RE.search(text)
    if pin_m and (
        re.search(r"\b(producto|c[oó]digo)\b", text, re.I)
        or _PIN_AFTER_VERB_RE.search(text)
    ):
        code = ""
        for rx in (_PIN_AFTER_VERB_RE, _PIN_PRODUCT_RE):
            cm = rx.search(text)
            if cm:
                code = (cm.group("code") or "").strip()
                if code:
                    break
        skip = {"este", "producto", "codigo", "código", "para", "esta", "el", "la"}
        if code and code.lower() not in skip:
            scope = "conversation"
            if _FUTURE_SCOPE_RE.search(text) and not _THIS_CONV_RE.search(text):
                scope = "user"
            cid = (conversation_id or "").strip()[:80] or None
            if scope == "conversation" and not cid:
                return ExplicitMemoryIntent(
                    matched=True,
                    reject_code="conversation_required",
                    reject_reason="pinned_entity requires conversation_id",
                )
            return ExplicitMemoryIntent(
                matched=True,
                memory_type="pinned_entity",
                key=f"pin.{code.upper()}",
                value={"kind": "codigo", "value": code.upper()},
                scope=scope,
                conversation_id=cid if scope == "conversation" else None,
                confirmation=(
                    f"Dejaré fijado el producto {code.upper()} "
                    + (
                        "en esta conversación."
                        if scope == "conversation"
                        else "para tus conversaciones."
                    )
                ),
                remainder_message=_strip_memory_clause(text),
            )

    if not _RECUERDA_RE.search(text):
        return ExplicitMemoryIntent(matched=False)

    # ui_pref compact
    if _COMPACT_ON_RE.search(text) or _COMPACT_OFF_RE.search(text):
        compact = bool(_COMPACT_ON_RE.search(text)) and not bool(_COMPACT_OFF_RE.search(text))
        if _COMPACT_OFF_RE.search(text):
            compact = False
        return ExplicitMemoryIntent(
            matched=True,
            memory_type="ui_pref",
            key="ui.compact",
            value={"compact": compact},
            scope="user",
            confirmation="Guardaré tu preferencia de interfaz.",
            remainder_message=_strip_memory_clause(text),
        )

    # preference style
    style = None
    if _STYLE_BRIEF_RE.search(text):
        style = "brief"
    elif _STYLE_DETAILED_RE.search(text):
        style = "detailed"
    elif _STYLE_OPERATIONAL_RE.search(text):
        style = "operational"
    if style:
        return ExplicitMemoryIntent(
            matched=True,
            memory_type="preference",
            key="response_style",
            value={"answer_style": style},
            scope="user",
            confirmation="Lo recordaré para futuras conversaciones.",
            remainder_message=_strip_memory_clause(text),
        )

    # recuerda... without extractable closed value → reject rather than invent
    if re.search(r"\b(recuerda|acu[eé]rdate|guarda)\b", text, re.I):
        return ExplicitMemoryIntent(
            matched=True,
            reject_code="unparseable_explicit",
            reject_reason="explicit_signal_without_closed_value",
        )

    return ExplicitMemoryIntent(matched=False)


def _strip_memory_clause(text: str) -> str | None:
    """Remove memory clause; return remainder query or None if memory-only."""
    # Split on " y recuerda / y prefiero / ; recuerda"
    parts = re.split(
        r"\s+(?:y|,|;)\s+(?=.*\b(?:recuerda|acu[eé]rdate|guarda|prefiero|fij[ae]|pinnea)\b)",
        text,
        maxsplit=1,
        flags=re.IGNORECASE,
    )
    if len(parts) == 2:
        left = parts[0].strip()
        right = parts[1].strip()
        # If memory is on the right, keep left as query
        if _RECUERDA_RE.search(right) or _PIN_RE.search(right) or re.search(r"\bprefiero\b", right, re.I):
            return left or None
        # If memory is on the left, keep right
        if _RECUERDA_RE.search(left) or _PIN_RE.search(left):
            return right or None
    # Memory-only
    if _RECUERDA_RE.search(text) or (
        re.search(r"\b(fij[ae]|pinnea)\b", text, re.I) and re.search(r"\bproducto\b", text, re.I)
    ):
        # If also has search verbs, try remove memory phrase
        cleaned = re.sub(
            r"(?:,?\s*)?\b(?:recuerda(?:\s+que)?|acu[eé]rdate(?:\s+de)?|guarda(?:\s+esto)?|"
            r"mi\s+preferencia\s+es|prefiero)\b.*$",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip(" ,;")
        cleaned = re.sub(
            r"(?:,?\s*)?\b(?:y\s+)?(?:fij[ae]|pinnea|ancla)\b.*$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip(" ,;")
        if cleaned and cleaned.lower() != text.lower():
            return cleaned or None
        return None
    return text


def write_explicit_memory(
    *,
    actor_user: str,
    memory_type: str,
    key: str,
    value: dict[str, Any],
    scope: str = "user",
    conversation_id: str | None = None,
    store: MemoryStore | None = None,
    require_explicit_flag: bool = False,
    slot_id: str | None = None,
    confirmation: str | None = None,
) -> ExplicitWriteResult:
    """Persist explicit memory via sanitize→schema→upsert. Soft-fails on DB errors."""
    if not memory_enabled():
        return ExplicitWriteResult(
            ok=False,
            rejected=True,
            error_code="memory_disabled",
            message="Memoria deshabilitada.",
        )
    if require_explicit_flag and not memory_explicit_enabled():
        return ExplicitWriteResult(
            ok=False,
            rejected=True,
            error_code="explicit_disabled",
            message="Escritura explícita deshabilitada.",
        )

    actor = (actor_user or "").strip()
    if not actor:
        return ExplicitWriteResult(ok=False, rejected=True, error_code="unauthorized", message="Sin actor.")

    mt = (memory_type or "").strip().lower()
    if mt not in EXPLICIT_WRITABLE_TYPES:
        return ExplicitWriteResult(
            ok=False,
            rejected=True,
            error_code="type_not_allowed",
            message="Tipo de memoria no permitido para escritura explícita.",
            memory_type=mt,
        )
    if mt not in MEMORY_TYPES:
        return ExplicitWriteResult(
            ok=False,
            rejected=True,
            error_code="invalid_memory_type",
            message="Tipo de memoria inválido.",
        )

    # Reject poison inside structured value strings
    try:
        blob = str(value)
    except Exception:
        blob = ""
    if is_poison_or_forbidden_text(blob) or is_poison_or_forbidden_text(key):
        return ExplicitWriteResult(
            ok=False,
            rejected=True,
            error_code="memory_poisoning",
            message="No puedo guardar esa información.",
            memory_type=mt,
            scope=scope,
        )

    mem = store if store is not None else get_default_memory_store()
    try:
        # Optional: verify slot ownership on PUT
        if slot_id:
            existing = mem.get_slot(actor, slot_id)
            if not existing:
                return ExplicitWriteResult(
                    ok=False,
                    rejected=True,
                    error_code="not_found",
                    message="Memoria no encontrada.",
                )
            # Force same identity key fields from existing for upsert identity
            key = str(existing.get("key") or key)
            scope = str(existing.get("scope") or scope)
            conversation_id = existing.get("conversation_id")
            mt = str(existing.get("memory_type") or mt)

        from app.assistant.orchestrator.memory_epoch import (
            resolve_actor_permission_epoch,
            sensitivity_for_memory_type,
        )

        # Server-derived sensitivity — never from client payload
        sens = sensitivity_for_memory_type(mt)
        epoch_arg: int | None = None
        if sens == "contextual":
            epoch_res = resolve_actor_permission_epoch(actor)
            if not epoch_res.available or epoch_res.epoch is None:
                return ExplicitWriteResult(
                    ok=False,
                    rejected=True,
                    error_code="permission_epoch_unavailable",
                    message="No pude guardar esa memoria en este momento.",
                    memory_type=mt,
                    scope=scope,
                )
            epoch_arg = int(epoch_res.epoch)
        # benign → permission_epoch NULL (survives epoch bumps)

        slot = mem.upsert(
            actor_user=actor,
            scope=scope,
            memory_type=mt,
            key=key,
            value=value,
            conversation_id=conversation_id,
            source="explicit",
            confidence=1.0,
            permission_epoch=epoch_arg,
            sensitivity=sens,
            verify_conversation=(scope == "conversation"),
        )
    except MemorySchemaError as exc:
        return ExplicitWriteResult(
            ok=False,
            rejected=True,
            error_code=exc.code,
            message="No pude guardar esa memoria.",
            memory_type=mt,
            scope=scope,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("assistant_memory explicit write failed: %s", type(exc).__name__)
        return ExplicitWriteResult(
            ok=False,
            rejected=False,
            error_code="memory_write_failed",
            message="No pude guardar la memoria en este momento.",
            memory_type=mt,
            scope=scope,
        )

    if slot is None:
        err = getattr(mem, "last_error", "") or ""
        code = "memory_write_rejected"
        if "schema" in err or "rejected" in err:
            code = "memory_write_rejected"
        elif "conversation" in err:
            code = "conversation_required"
        return ExplicitWriteResult(
            ok=False,
            rejected=True,
            error_code=code,
            message="No pude guardar esa memoria.",
            memory_type=mt,
            scope=scope,
        )

    return ExplicitWriteResult(
        ok=True,
        slot=slot,
        confirmation=confirmation or "Listo, lo recordaré.",
        memory_type=mt,
        scope=str(slot.get("scope") or scope),
    )


def apply_chat_explicit_memory(
    *,
    message: str,
    actor_user: str,
    conversation_id: str,
    store: MemoryStore | None = None,
) -> tuple[str, ExplicitWriteResult | None]:
    """Chat helper: if EXPLICIT on, try write; return (message_for_planner, write_result|None).

    When MEMORY/EXPLICIT off → (message, None) unchanged.
    Memory-only success → ("", result) so caller can reply with confirmation.
    Dual intent → (remainder, result).
    Rejected poison → (message or "", result) without persisting.
    """
    if not memory_enabled() or not memory_explicit_enabled():
        return message, None

    intent = detect_explicit_memory_intent(message, conversation_id=conversation_id)
    if not intent.matched:
        return message, None

    if intent.reject_code:
        # Treat as memory-only rejection (do not send poison text to planner)
        return "", ExplicitWriteResult(
            ok=False,
            rejected=True,
            error_code=intent.reject_code,
            message=(
                "No puedo guardar eso."
                if intent.reject_code == "memory_poisoning"
                else "No entendí qué debo recordar de forma segura."
            ),
            memory_type=intent.memory_type,
            scope=intent.scope,
        )

    result = write_explicit_memory(
        actor_user=actor_user,
        memory_type=str(intent.memory_type),
        key=str(intent.key),
        value=dict(intent.value or {}),
        scope=intent.scope,
        conversation_id=intent.conversation_id,
        store=store,
        require_explicit_flag=True,
        confirmation=intent.confirmation,
    )
    remainder = intent.remainder_message
    if result.ok and not (remainder and remainder.strip()):
        return "", result
    if result.ok and remainder:
        return remainder.strip(), result
    # Failed write: keep original message for planner if there is a query; else empty for confirm-fail
    if remainder and remainder.strip():
        return remainder.strip(), result
    return "", result
