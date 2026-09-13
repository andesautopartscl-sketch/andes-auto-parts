"""get_stock_movements — READ tool. Calls ERP internal movement history via HTTP adapter.

``tipo`` is ingreso|salida|ajuste. ``cantidad`` is returned as the ERP stored it
(not inverted). A negative quantity does not by itself mean salida.
"""
from __future__ import annotations

from typing import Any

from andes_agent.adapters.erp_http import AdapterError, ERPAdapter

NAME = "get_stock_movements"
WRITE = False
ALLOWED_ITEM_FIELDS = (
    "fecha",
    "tipo",
    "cantidad",
    "marca",
    "bodega",
    "origen_compra",
    "usuario",
    "observacion",
)


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
    try:
        cantidad = int(row.get("cantidad") or 0)
    except (TypeError, ValueError):
        cantidad = 0
    usuario = str(row.get("usuario") or "").strip()
    if "@" in usuario:
        usuario = ""
    observacion = str(row.get("observacion") or "").strip()
    if len(observacion) > 180:
        observacion = observacion[:180] + "…"
    return {
        "fecha": str(row.get("fecha") or "").strip(),
        "tipo": str(row.get("tipo") or "").strip().lower(),
        "cantidad": cantidad,
        "marca": str(row.get("marca") or "").strip(),
        "bodega": str(row.get("bodega") or "").strip(),
        "origen_compra": str(row.get("origen_compra") or "").strip() or "nacional",
        "usuario": usuario,
        "observacion": observacion,
    }


def handle(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    context = context or {}
    actor = str(context.get("actor_user") or "").strip()
    if not actor:
        return _error("principal_required", "Human principal is required")

    adapter = context.get("adapter")
    if not isinstance(adapter, ERPAdapter):
        return _error("erp_unavailable", "ERP adapter is not configured")

    payload_args: dict[str, Any] = {"codigo": str(arguments.get("codigo") or "").strip().upper()}
    if arguments.get("fecha_desde"):
        payload_args["fecha_desde"] = str(arguments.get("fecha_desde") or "").strip()
    if arguments.get("fecha_hasta"):
        payload_args["fecha_hasta"] = str(arguments.get("fecha_hasta") or "").strip()
    try:
        limit = int(arguments.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(50, limit))
    payload_args["limit"] = limit

    try:
        payload = adapter.call("stock/movements", payload_args, actor_user=actor, method="POST")
    except AdapterError as exc:
        message = exc.message or "Movement lookup failed"
        nested = exc.payload.get("error") if isinstance(exc.payload, dict) else None
        if isinstance(nested, dict) and nested.get("message"):
            message = str(nested.get("message") or message)
        return _error(exc.code, message)

    if not payload.get("ok"):
        code = str(payload.get("error_code") or (payload.get("error") or {}).get("code") or "erp_error")
        message = str(
            payload.get("message")
            or (payload.get("error") or {}).get("message")
            or "Movement lookup failed"
        )
        return _error(code, message)

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    items = []
    for row in data.get("items") if isinstance(data.get("items"), list) else []:
        item = _public_item(row)
        if item:
            items.append({key: item[key] for key in ALLOWED_ITEM_FIELDS})
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    if len(items) > limit:
        items = items[:limit]
        truncated = True
    else:
        truncated = bool(meta.get("truncated")) and len(items) == limit
    return {
        "ok": True,
        "tool": NAME,
        "classification": "INTERNAL",
        "write": False,
        "data": {
            "codigo": str(data.get("codigo") or payload_args["codigo"]).strip().upper(),
            "descripcion": str(data.get("descripcion") or "").strip(),
            "items": items,
            "count": len(items),
        },
        "meta": {
            "limit": limit,
            "truncated": truncated,
            "environment": str(context.get("environment") or meta.get("environment") or ""),
        },
    }
