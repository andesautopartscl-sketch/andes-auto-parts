"""get_equivalences — READ tool. Cruce OEM ↔ código interno ↔ alternativo.

Sin datos financieros: este cruce es técnico, no comercial. No hay campos de
precio ni costo que copiar, así que tampoco hay nada que redactar por permisos.

``aplicaciones`` lleva nombre propio a propósito: son modelos de vehículo
(HOMOLOGADOS en el ERP), NO códigos equivalentes. Mezclarlos haría que el agente
ofreciera un coche donde el usuario espera una pieza.
"""
from __future__ import annotations

from typing import Any

from andes_agent.adapters.erp_http import AdapterError, ERPAdapter

NAME = "get_equivalences"
WRITE = False

PUBLIC_ITEM_FIELDS = ("codigo", "descripcion", "marca", "modelo", "motor")
PUBLIC_LIST_FIELDS = ("oem", "alternativos", "aplicaciones")
MAX_LIMIT = 20
MAX_LIST_ENTRIES = 12


def _error(code: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error_code": code,
        "message": message,
        "error": {"code": code, "message": message},
    }


def _public_item(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    item: dict[str, Any] = {
        key: str(row.get(key) or "").strip() for key in PUBLIC_ITEM_FIELDS
    }
    if not item.get("codigo"):
        return None
    item["codigo"] = item["codigo"].upper()
    for key in PUBLIC_LIST_FIELDS:
        raw = row.get(key)
        if isinstance(raw, list):
            item[key] = [str(v).strip() for v in raw if str(v or "").strip()][
                :MAX_LIST_ENTRIES]
        else:
            item[key] = []
    return item


def handle(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    context = context or {}
    actor = str(context.get("actor_user") or "").strip()
    if not actor:
        return _error("principal_required", "Human principal is required")

    adapter = context.get("adapter")
    if not isinstance(adapter, ERPAdapter):
        return _error("erp_unavailable", "ERP adapter is not configured")

    payload_args: dict[str, Any] = {}
    for key in ("oem", "codigo", "marca", "modelo"):
        value = arguments.get(key)
        if value:
            text = str(value).strip()
            payload_args[key] = text.upper() if key == "codigo" else text
    try:
        limit = int(arguments.get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    payload_args["limit"] = max(1, min(MAX_LIMIT, limit))

    try:
        payload = adapter.call("catalog/equivalences", payload_args,
                               actor_user=actor, method="POST")
    except AdapterError as exc:
        message = exc.message or "Equivalence lookup failed"
        nested = exc.payload.get("error") if isinstance(exc.payload, dict) else None
        if isinstance(nested, dict) and nested.get("message"):
            message = str(nested.get("message") or message)
        return _error(exc.code, message)

    if not payload.get("ok"):
        code = str(payload.get("error_code")
                   or (payload.get("error") or {}).get("code") or "erp_error")
        message = str(payload.get("message")
                      or (payload.get("error") or {}).get("message")
                      or "Equivalence lookup failed")
        return _error(code, message)

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    items = []
    for row in (data.get("items") if isinstance(data.get("items"), list) else []):
        item = _public_item(row)
        if item:
            items.append(item)
    limit_out = payload_args["limit"]
    items = items[:limit_out]

    out_data: dict[str, Any] = {"items": items, "count": len(items)}
    query = data.get("query")
    if isinstance(query, dict):
        out_data["query"] = {k: str(v)[:64] for k, v in query.items() if v}
    if data.get("matched_on"):
        out_data["matched_on"] = str(data.get("matched_on"))[:16]
    # Dos hechos distintos que el agente NO puede confundir: "ese codigo no
    # existe" y "existe pero no declara OEM". El segundo no es un fallo.
    for flag in ("not_found", "no_oem_declared"):
        if data.get(flag):
            out_data[flag] = True

    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    return {
        "ok": True,
        "tool": NAME,
        "classification": "INTERNAL",
        "write": False,
        "data": out_data,
        "meta": {
            "limit": limit_out,
            "truncated": bool(meta.get("truncated")),
            "environment": str(context.get("environment") or meta.get("environment") or ""),
        },
    }
