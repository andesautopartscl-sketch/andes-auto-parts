"""FASE 7B.1 — sanitize memory payloads before DB persist (never raw→DB)."""
from __future__ import annotations

import re
from typing import Any

from app.assistant.orchestrator.audit import redact
from app.assistant.orchestrator.memory_schema import (
    MemorySchemaError,
    validate_key,
    validate_memory_type,
    validate_scope,
    validate_sensitivity,
    validate_status,
    validate_source,
    validate_value_for_type,
)

DENY_VALUE_KEYS = frozenset(
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
        "token",
        "authorization",
        "cookie",
        "cookies",
        "csrf",
        "api_key",
        "apikey",
        "secret",
        "access_token",
        "bearer",
        "service_token",
        "session",
        "connection_string",
        "database_url",
        "prompt",
        "system",
        "system_prompt",
        "user_prompt",
        "message",
        "reply",
        "monto",
        "precio",
        "costo",
        "total",
        "ventas_periodo",
        "ventas_hoy",
        "ventas_mes",
        "saldo",
        "banco",
        "cuenta_bancaria",
        "role",
        "rol",
        "permiso",
        "permisos",
        "permission",
        "permissions",
        "credential",
        "credentials",
    }
)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
RUT_RE = re.compile(r"\b\d{1,2}\.?\d{3}\.?\d{3}-[\dkK]\b")
PHONE_RE = re.compile(r"\+?\d[\d\s\-.]{7,}\d")

# Credential material with scheme / assignment (strip scheme + secret together)
_AUTH_CREDENTIAL_RE = re.compile(
    r"(?:\bAuthorization\s*:?\s*)?(?:\b(?:Bearer|Basic|Token)\b)(?:\s+\S+)?"
    r"|\b(?:api[_-]?key|apikey|secret|password|passwd|cookie|cookies|csrf|"
    r"m2m(?:[_\s-]*(?:secret|credential|token|key))?|service[_-]?token|"
    r"access[_-]?token)\b\s*[=:]\s*\S+",
    re.IGNORECASE,
)

# Residual auth markers that must never persist (even as "Bearer [redacted]")
_AUTH_MARKER_RE = re.compile(
    r"\b(?:Bearer|Authorization|Basic|Token|api[_-]?key|apikey|secret|"
    r"password|passwd|cookie|cookies|csrf|m2m)\b",
    re.IGNORECASE,
)

_REDACTED_RUN_RE = re.compile(r"(?:\[redacted\]\s*){2,}")


def _scrub_string(text: str) -> str:
    """Redact PII and authentication markers. Never leave Bearer/Authorization/etc."""
    out = text
    out = EMAIL_RE.sub("[redacted]", out)
    out = RUT_RE.sub("[redacted]", out)
    # Avoid scrubbing pure product codes; only long digit runs that look like phones
    if PHONE_RE.search(out) and any(ch.isdigit() for ch in out) and len(re.sub(r"\D", "", out)) >= 8:
        if "@" in text or "tel" in text.lower() or "+" in text:
            out = PHONE_RE.sub("[redacted]", out)
    # Remove auth schemes + credential assignments in one pass, then bare markers
    out = _AUTH_CREDENTIAL_RE.sub("[redacted]", out)
    out = _AUTH_MARKER_RE.sub("[redacted]", out)
    out = _REDACTED_RUN_RE.sub("[redacted] ", out)
    return out.strip()


def _deep_deny(obj: Any) -> Any:
    obj = redact(obj)
    if isinstance(obj, dict):
        out = {}
        for key, val in obj.items():
            lk = str(key).strip().lower()
            if lk in DENY_VALUE_KEYS or any(
                p in lk
                for p in (
                    "password",
                    "secret",
                    "authorization",
                    "cookie",
                    "api_key",
                    "csrf",
                    "token",
                    "permiso",
                    "permission",
                    "credential",
                )
            ):
                # Keep aggregate token counters out of memory values entirely
                continue
            out[key] = _deep_deny(val)
        return out
    if isinstance(obj, list):
        return [_deep_deny(v) for v in obj]
    if isinstance(obj, str):
        return _scrub_string(obj)
    return obj


def sanitize_memory_record(
    *,
    actor_user: str,
    scope: str,
    conversation_id: str | None,
    memory_type: str,
    key: str,
    value: Any,
    source: str = "explicit",
    confidence: float | None = 1.0,
    permission_epoch: int | None = 0,
    sensitivity: str | None = None,
    status: str | None = None,
    source_turn_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate + scrub a memory slot payload. Raises MemorySchemaError on reject."""
    actor = (actor_user or "").strip()
    if not actor:
        raise MemorySchemaError("unauthorized", "actor_user required")

    sc = validate_scope(scope)
    mt = validate_memory_type(memory_type)
    src = validate_source(source)
    k = validate_key(key)

    conv = (conversation_id or "").strip()[:80] or None
    if sc == "user":
        conv = None
    elif sc == "conversation":
        if not conv:
            raise MemorySchemaError("invalid_scope", "conversation_id required for scope=conversation")

    # Drop forbidden keys before type schema (defense in depth)
    if isinstance(value, dict):
        for bad in list(value.keys()):
            if str(bad).strip().lower() in DENY_VALUE_KEYS:
                raise MemorySchemaError("forbidden_field", f"field not allowed in value: {bad}")
        # Reject entity values that look like PII before scrub loses the signal
        if isinstance(value.get("value"), str):
            raw_ent = value["value"]
            if EMAIL_RE.search(raw_ent) or RUT_RE.search(raw_ent) or "@" in raw_ent:
                raise MemorySchemaError("forbidden_pii", "entity value looks like PII")

    scrubbed_value = _deep_deny(value if isinstance(value, dict) else {})
    if not isinstance(scrubbed_value, dict):
        raise MemorySchemaError("invalid_value", "value must be object after scrub")
    # Remove keys nulled by financial scrub
    scrubbed_value = {k2: v2 for k2, v2 in scrubbed_value.items() if v2 is not None}

    normalized = validate_value_for_type(mt, scrubbed_value)

    # Re-scrub normalized strings (summary text)
    if mt == "conversation_summary":
        normalized["text"] = _scrub_string(str(normalized.get("text") or ""))[:240]
        if EMAIL_RE.search(normalized["text"]) or RUT_RE.search(str(value.get("text") or "")):
            # If original had email/RUT patterns heavily, keep redacted form only
            pass
        if not normalized["text"].strip():
            raise MemorySchemaError("invalid_value", "conversation_summary.text empty after scrub")

    # Entity values must not look like emails
    if mt in {"frequent_entity", "pinned_entity"}:
        ev = str(normalized.get("value") or "")
        if EMAIL_RE.search(ev) or RUT_RE.search(ev) or "@" in ev:
            raise MemorySchemaError("forbidden_pii", "entity value looks like PII")

    from app.assistant.orchestrator.memory_schema import DEFAULT_SENSITIVITY

    # Server derives sensitivity from type when omitted; never trust client as authority.
    sens = validate_sensitivity(sensitivity or DEFAULT_SENSITIVITY.get(mt, "contextual"))
    # FASE 10.2.1 — el estado tambien lo decide el servidor, no el cliente.
    st = validate_status(status)

    conf = 1.0 if confidence is None else float(confidence)
    if conf < 0 or conf > 1:
        raise MemorySchemaError("invalid_confidence", "confidence must be 0..1")

    # Benign may persist NULL epoch (survives permission_epoch changes).
    # Contextual must carry a non-negative integer epoch stamped at write time.
    if permission_epoch is None:
        epoch: int | None = None
    else:
        try:
            epoch = int(permission_epoch)
        except (TypeError, ValueError) as exc:
            raise MemorySchemaError("invalid_epoch", "permission_epoch must be int") from exc
        if epoch < 0:
            raise MemorySchemaError("invalid_epoch", "permission_epoch must be >= 0")
    if sens == "contextual" and epoch is None:
        raise MemorySchemaError("invalid_epoch", "contextual memory requires permission_epoch")

    safe_meta: dict[str, Any] = {}
    if isinstance(meta, dict):
        # Only allow hit_count / last_seen_at style counters
        if "hit_count" in meta:
            try:
                safe_meta["hit_count"] = int(meta["hit_count"])
            except (TypeError, ValueError):
                pass
        if "last_seen_at" in meta and isinstance(meta["last_seen_at"], str):
            safe_meta["last_seen_at"] = str(meta["last_seen_at"])[:40]

    turn_id = (source_turn_id or "").strip()[:80] or None

    return {
        "actor_user": actor[:80],
        "scope": sc,
        "conversation_id": conv,
        "memory_type": mt,
        "key": k,
        "value": normalized,
        "confidence": conf,
        "source": src,
        "permission_epoch": epoch,
        "sensitivity": sens,
        "status": st,
        "source_turn_id": turn_id,
        "meta": safe_meta,
    }
