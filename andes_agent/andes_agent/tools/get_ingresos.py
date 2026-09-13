"""get_ingresos — READ tool. Calls ERP internal warehouse-receipt lookup via HTTP adapter.

Finance fields (costo_neto, precio_venta_neto, margen_pct) are copied only if the ERP
included them. The Gateway does not grant finance visibility.
"""
from __future__ import annotations

from typing import Any

from andes_agent.adapters.erp_http import AdapterError, ERPAdapter

NAME = "get_ingresos"
WRITE = False
PUBLIC_ITEM_FIELDS = (
    "fecha",
    "numero_documento",
    "proveedor",
    "anulado",
    "codigo",
    "descripcion",
    "marca",
    "bodega",
    "origen_compra",
    "cantidad",
)
FINANCE_ITEM_FIELDS = (
    "costo_neto",
    "precio_venta_neto",
    "margen_pct",
)


def _error(code: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error_code": code,
        "message": message,
        "error": {"code": code, "message": message},
    }


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _public_item(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    try:
        cantidad = int(row.get("cantidad") or 0)
    except (TypeError, ValueError):
        cantidad = 0
    item = {
        "fecha": str(row.get("fecha") or "").strip(),
        "numero_documento": str(row.get("numero_documento") or "").strip(),
        "proveedor": str(row.get("proveedor") or "").strip(),
        "anulado": bool(row.get("anulado")),
        "codigo": str(row.get("codigo") or "").strip().upper(),
        "descripcion": str(row.get("descripcion") or "").strip(),
        "marca": str(row.get("marca") or "").strip(),
        "bodega": str(row.get("bodega") or "").strip(),
        "origen_compra": str(row.get("origen_compra") or "").strip() or "nacional",
        "cantidad": cantidad,
    }
    for key in FINANCE_ITEM_FIELDS:
        parsed = _as_float(row.get(key))
        if parsed is not None:
            item[key] = parsed
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
    if arguments.get("codigo"):
        payload_args["codigo"] = str(arguments.get("codigo") or "").strip().upper()
    if arguments.get("proveedor"):
        payload_args["proveedor"] = str(arguments.get("proveedor") or "").strip()
    if arguments.get("numero_documento"):
        payload_args["numero_documento"] = str(arguments.get("numero_documento") or "").strip()
    if arguments.get("fecha_desde"):
        payload_args["fecha_desde"] = str(arguments.get("fecha_desde") or "").strip()
    if arguments.get("fecha_hasta"):
        payload_args["fecha_hasta"] = str(arguments.get("fecha_hasta") or "").strip()
    try:
        limit = int(arguments.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(20, limit))
    payload_args["limit"] = limit

    try:
        payload = adapter.call("bodega/ingresos", payload_args, actor_user=actor, method="POST")
    except AdapterError as exc:
        message = exc.message or "Receipt lookup failed"
        nested = exc.payload.get("error") if isinstance(exc.payload, dict) else None
        if isinstance(nested, dict) and nested.get("message"):
            message = str(nested.get("message") or message)
        return _error(exc.code, message)

    if not payload.get("ok"):
        code = str(payload.get("error_code") or (payload.get("error") or {}).get("code") or "erp_error")
        message = str(
            payload.get("message")
            or (payload.get("error") or {}).get("message")
            or "Receipt lookup failed"
        )
        return _error(code, message)

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    items = []
    for row in data.get("items") if isinstance(data.get("items"), list) else []:
        item = _public_item(row)
        if item:
            items.append(item)
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    if len(items) > limit:
        items = items[:limit]
        truncated = True
    else:
        truncated = bool(meta.get("truncated")) and len(items) == limit
    out_data: dict[str, Any] = {"items": items, "count": len(items)}
    if data.get("codigo"):
        out_data["codigo"] = str(data.get("codigo") or "").strip().upper()
    if data.get("descripcion"):
        out_data["descripcion"] = str(data.get("descripcion") or "").strip()
    return {
        "ok": True,
        "tool": NAME,
        "classification": "CONFIDENTIAL",
        "write": False,
        "data": out_data,
        "meta": {
            "limit": limit,
            "truncated": truncated,
            "environment": str(context.get("environment") or meta.get("environment") or ""),
        },
    }
