"""FASE 7B.1 — closed schemas for memory value_json (no arbitrary client types)."""
from __future__ import annotations

import hashlib
import json
from typing import Any

MEMORY_TYPES = frozenset(
    {
        "preference",
        "ui_pref",
        "frequent_entity",
        "pinned_entity",
        "conversation_summary",
    }
)

SCOPES = frozenset({"user", "conversation"})
SOURCES = frozenset({"explicit", "derived", "ui"})
SENSITIVITIES = frozenset({"benign", "contextual"})

# FASE 10.2.1 — ciclo de vida de una memoria.
#
# Hoy toda memoria nace utilizable. El objetivo de 10.2 es que lo que el sistema
# INFIERE tenga que ser aprobado antes de volver al modelo, mientras que lo que
# el usuario pide explicitamente siga siendo inmediato.
#
# Esta unidad SOLO introduce el estado. La politica —que `derived` nazca
# `suggested`— y la puerta del selector pertenecen a 10.2.2, y por eso aqui el
# default es `approved`: ninguna fila existente ni ninguna nueva cambia de
# comportamiento por este cambio.
STATUSES = frozenset({"approved", "suggested", "rejected", "expired"})
DEFAULT_STATUS = "approved"

ANSWER_STYLES = frozenset({"brief", "detailed", "operational"})
ENTITY_KINDS = frozenset(
    {
        "codigo",
        "sku",
        "producto_id",
        "proveedor_id",
        "cliente_id",
        "bodega_id",
        "proveedor_q",
        "oc",
    }
)
DERIVED_ENTITY_KINDS = frozenset(
    {"codigo", "sku", "producto_id", "proveedor_id", "cliente_id", "bodega_id"}
)

# Default TTL days by type (None = no expires_at unless caller sets one)
DEFAULT_TTL_DAYS: dict[str, int | None] = {
    "preference": 365,
    "ui_pref": None,
    "frequent_entity": 30,
    "pinned_entity": 90,
    "conversation_summary": 30,
}

DEFAULT_SENSITIVITY: dict[str, str] = {
    "preference": "benign",
    "ui_pref": "benign",
    "frequent_entity": "contextual",
    "pinned_entity": "contextual",
    "conversation_summary": "contextual",
}


class MemorySchemaError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def validate_memory_type(memory_type: str) -> str:
    mt = (memory_type or "").strip().lower()
    if mt not in MEMORY_TYPES:
        raise MemorySchemaError("invalid_memory_type", f"memory_type not allowed: {memory_type!r}")
    return mt


def validate_scope(scope: str) -> str:
    sc = (scope or "").strip().lower()
    if sc not in SCOPES:
        raise MemorySchemaError("invalid_scope", f"scope must be user|conversation, got {scope!r}")
    return sc


def validate_source(source: str) -> str:
    src = (source or "").strip().lower()
    if src not in SOURCES:
        raise MemorySchemaError("invalid_source", f"source not allowed: {source!r}")
    return src


def validate_sensitivity(sensitivity: str) -> str:
    s = (sensitivity or "").strip().lower()
    if s not in SENSITIVITIES:
        raise MemorySchemaError("invalid_sensitivity", f"sensitivity not allowed: {sensitivity!r}")
    return s


def validate_status(status: str | None) -> str:
    """Un estado vacio es `approved`: es el comportamiento de siempre."""
    st = (status or "").strip().lower() or DEFAULT_STATUS
    if st not in STATUSES:
        raise MemorySchemaError("invalid_status", f"status not allowed: {status!r}")
    return st


def validate_key(key: str) -> str:
    k = (key or "").strip()
    if not k or len(k) > 120:
        raise MemorySchemaError("invalid_key", "key required (max 120 chars)")
    # Prevent path-like injection / whitespace-only abuse
    if any(ch in k for ch in ("\n", "\r", "\0")):
        raise MemorySchemaError("invalid_key", "key contains invalid characters")
    return k


def validate_value_for_type(memory_type: str, value: Any) -> dict[str, Any]:
    """Return a normalized closed value_json dict or raise MemorySchemaError."""
    mt = validate_memory_type(memory_type)
    if not isinstance(value, dict):
        raise MemorySchemaError("invalid_value", "value_json must be an object")

    if mt == "preference":
        style = str(value.get("answer_style") or "").strip().lower()
        if style not in ANSWER_STYLES:
            raise MemorySchemaError(
                "invalid_value",
                "preference.answer_style must be brief|detailed|operational",
            )
        extra = set(value.keys()) - {"answer_style"}
        if extra:
            raise MemorySchemaError("invalid_value", f"preference unknown fields: {sorted(extra)}")
        return {"answer_style": style}

    if mt == "ui_pref":
        if "compact" not in value:
            raise MemorySchemaError("invalid_value", "ui_pref.compact required")
        compact = value.get("compact")
        if not isinstance(compact, bool):
            raise MemorySchemaError("invalid_value", "ui_pref.compact must be boolean")
        extra = set(value.keys()) - {"compact"}
        if extra:
            raise MemorySchemaError("invalid_value", f"ui_pref unknown fields: {sorted(extra)}")
        return {"compact": compact}

    if mt in {"frequent_entity", "pinned_entity"}:
        kind = str(value.get("kind") or "").strip().lower()
        if kind not in ENTITY_KINDS:
            raise MemorySchemaError(
                "invalid_value",
                f"{mt}.kind must be codigo|sku|producto_id|proveedor_id|cliente_id|bodega_id|proveedor_q|oc",
            )
        raw_val = str(value.get("value") or "").strip()
        if not raw_val or len(raw_val) > 40:
            raise MemorySchemaError("invalid_value", f"{mt}.value required (max 40 chars)")
        out: dict[str, Any] = {
            "kind": "codigo" if kind == "sku" else kind,
            "value": raw_val.upper() if kind in {"codigo", "sku"} else raw_val,
        }
        if mt == "frequent_entity":
            hit = value.get("hit_count", 1)
            try:
                hit_i = int(hit)
            except (TypeError, ValueError) as exc:
                raise MemorySchemaError("invalid_value", "frequent_entity.hit_count must be int") from exc
            if hit_i < 1 or hit_i > 1_000_000:
                raise MemorySchemaError("invalid_value", "frequent_entity.hit_count out of range")
            out["hit_count"] = hit_i
            allowed = {"kind", "value", "hit_count"}
        else:
            allowed = {"kind", "value"}
        extra = set(value.keys()) - allowed
        if extra:
            raise MemorySchemaError("invalid_value", f"{mt} unknown fields: {sorted(extra)}")
        return out

    if mt == "conversation_summary":
        text = str(value.get("text") or "").strip()
        if not text or len(text) > 240:
            raise MemorySchemaError("invalid_value", "conversation_summary.text required (max 240)")
        tools = value.get("tools") or []
        if not isinstance(tools, list):
            raise MemorySchemaError("invalid_value", "conversation_summary.tools must be a list")
        clean_tools = [str(t).strip() for t in tools if str(t).strip()][:3]
        extra = set(value.keys()) - {"text", "tools"}
        if extra:
            raise MemorySchemaError(
                "invalid_value", f"conversation_summary unknown fields: {sorted(extra)}"
            )
        return {"text": text[:240], "tools": clean_tools}

    raise MemorySchemaError("invalid_memory_type", f"unhandled type {mt}")


def memory_version(slot: dict[str, Any]) -> str:
    """FASE 10.2.3 — huella de LA memoria que el usuario vio.

    Es el testigo de concurrencia optimista de approve/reject. Cubre estado,
    marcas de tiempo Y contenido, a proposito: si otro cambia el valor entre que
    el usuario lee la sugerencia y pulsa Aprobar, el testigo deja de coincidir y
    la aprobacion se rechaza. Sin el contenido dentro, aprobar podria confirmar
    un texto distinto del que se mostro, que es exactamente el riesgo que esta
    unidad tiene que cerrar.

    Opaca a proposito: el cliente la devuelve tal cual, no la interpreta.
    """
    valor = slot.get("value")
    try:
        crudo = json.dumps(valor, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    except (TypeError, ValueError):
        crudo = repr(valor)
    partes = "|".join([
        str(slot.get("id") or ""),
        str(slot.get("status") or DEFAULT_STATUS),
        str(slot.get("status_changed_at") or ""),
        str(slot.get("updated_at") or ""),
        crudo,
    ])
    return hashlib.sha256(partes.encode("utf-8")).hexdigest()[:16]
