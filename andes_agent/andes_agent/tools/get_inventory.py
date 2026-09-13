"""get_inventory — READ tool. Calls ERP internal stock lookup via HTTP adapter."""
from __future__ import annotations

from typing import Any

from andes_agent.adapters.erp_http import AdapterError, ERPAdapter

NAME = "get_inventory"
WRITE = False
ALLOWED_ITEM_FIELDS = ("marca", "bodega", "origen_compra", "stock")


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
        stock = int(row.get("stock") or 0)
    except (TypeError, ValueError):
        stock = 0
    origen = str(row.get("origen_compra") or "").strip() or "nacional"
    return {
        "marca": str(row.get("marca") or "").strip(),
        "bodega": str(row.get("bodega") or "").strip(),
        "origen_compra": origen,
        "stock": stock,
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
    if arguments.get("marca"):
        payload_args["marca"] = str(arguments.get("marca") or "").strip()
    if arguments.get("bodega"):
        payload_args["bodega"] = str(arguments.get("bodega") or "").strip()

    try:
        payload = adapter.call("inventory/stock", payload_args, actor_user=actor, method="POST")
    except AdapterError as exc:
        message = exc.message or "Inventory lookup failed"
        nested = exc.payload.get("error") if isinstance(exc.payload, dict) else None
        if isinstance(nested, dict) and nested.get("message"):
            message = str(nested.get("message") or message)
        return _error(exc.code, message)

    if not payload.get("ok"):
        code = str(payload.get("error_code") or (payload.get("error") or {}).get("code") or "erp_error")
        message = str(
            payload.get("message")
            or (payload.get("error") or {}).get("message")
            or "Inventory lookup failed"
        )
        return _error(code, message)

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    items = []
    for row in data.get("items") if isinstance(data.get("items"), list) else []:
        item = _public_item(row)
        if item:
            items.append({key: item[key] for key in ALLOWED_ITEM_FIELDS})
    try:
        total = int(data.get("total_stock"))
    except (TypeError, ValueError):
        total = sum(item["stock"] for item in items)
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    return {
        "ok": True,
        "tool": NAME,
        "classification": "INTERNAL",
        "write": False,
        "data": {
            "codigo": str(data.get("codigo") or payload_args["codigo"]).strip().upper(),
            "descripcion": str(data.get("descripcion") or "").strip(),
            "items": items,
            "total_stock": total,
        },
        "meta": {
            "environment": str(context.get("environment") or meta.get("environment") or ""),
        },
    }
