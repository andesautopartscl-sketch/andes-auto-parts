"""get_purchase_orders — READ tool. Supplier POs (tipo=orden_compra), not oc_clientes.

Finance fields are copied only if the ERP included them.
"""
from __future__ import annotations

from typing import Any

from andes_agent.adapters.erp_http import AdapterError, ERPAdapter

NAME = "get_purchase_orders"
WRITE = False
PUBLIC_LINE_FIELDS = (
    "codigo",
    "descripcion",
    "cantidad",
    "marca",
    "bodega",
    "origen_compra",
)
FINANCE_LINE_FIELDS = ("precio", "margen_pct", "subtotal")
PUBLIC_DOC_FIELDS = ("numero", "tipo", "fecha", "estado", "proveedor", "lineas", "items")
FINANCE_DOC_FIELDS = ("total",)


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


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _public_line(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    item = {
        "codigo": str(row.get("codigo") or "").strip().upper(),
        "descripcion": str(row.get("descripcion") or "").strip(),
        "cantidad": _as_int(row.get("cantidad")),
        "marca": str(row.get("marca") or "").strip(),
        "bodega": str(row.get("bodega") or "").strip() or "Bodega 1",
        "origen_compra": str(row.get("origen_compra") or "").strip() or "nacional",
    }
    for key in FINANCE_LINE_FIELDS:
        parsed = _as_float(row.get(key))
        if parsed is not None:
            item[key] = parsed
    return item


def _public_doc(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    lines = []
    for line in row.get("items") if isinstance(row.get("items"), list) else []:
        clean = _public_line(line)
        if clean:
            lines.append(clean)
    doc = {
        "numero": str(row.get("numero") or "").strip(),
        "tipo": str(row.get("tipo") or "orden_compra").strip() or "orden_compra",
        "fecha": str(row.get("fecha") or "").strip(),
        "estado": str(row.get("estado") or "").strip().lower() or "pendiente",
        "proveedor": str(row.get("proveedor") or "").strip(),
        "lineas": _as_int(row.get("lineas")) if row.get("lineas") is not None else len(lines),
        "items": lines,
    }
    total = _as_float(row.get("total"))
    if total is not None:
        doc["total"] = total
    return doc


def handle(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    context = context or {}
    actor = str(context.get("actor_user") or "").strip()
    if not actor:
        return _error("principal_required", "Human principal is required")

    adapter = context.get("adapter")
    if not isinstance(adapter, ERPAdapter):
        return _error("erp_unavailable", "ERP adapter is not configured")

    payload_args: dict[str, Any] = {}
    for key in ("numero", "proveedor", "estado", "codigo", "fecha_desde", "fecha_hasta"):
        if arguments.get(key):
            value = str(arguments.get(key) or "").strip()
            if key in {"numero", "codigo"}:
                value = value.upper()
            if key == "estado":
                value = value.lower()
            payload_args[key] = value
    try:
        limit = int(arguments.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(20, limit))
    payload_args["limit"] = limit

    try:
        payload = adapter.call("ventas/purchase-orders", payload_args, actor_user=actor, method="POST")
    except AdapterError as exc:
        message = exc.message or "Purchase order lookup failed"
        nested = exc.payload.get("error") if isinstance(exc.payload, dict) else None
        if isinstance(nested, dict) and nested.get("message"):
            message = str(nested.get("message") or message)
        return _error(exc.code, message)

    if not payload.get("ok"):
        code = str(payload.get("error_code") or (payload.get("error") or {}).get("code") or "erp_error")
        message = str(
            payload.get("message")
            or (payload.get("error") or {}).get("message")
            or "Purchase order lookup failed"
        )
        return _error(code, message)

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    items = []
    for row in data.get("items") if isinstance(data.get("items"), list) else []:
        doc = _public_doc(row)
        if doc:
            items.append(doc)
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    if len(items) > limit:
        items = items[:limit]
        truncated = True
    else:
        truncated = bool(meta.get("truncated")) and len(items) == limit
    return {
        "ok": True,
        "tool": NAME,
        "classification": "CONFIDENTIAL",
        "write": False,
        "data": {"items": items, "count": len(items)},
        "meta": {
            "limit": limit,
            "truncated": truncated,
            "environment": str(context.get("environment") or meta.get("environment") or ""),
        },
    }
