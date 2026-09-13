"""check_stock — READ tool. Multi-line availability via ERP (no WRITE)."""
from __future__ import annotations

from typing import Any

from andes_agent.adapters.erp_http import AdapterError, ERPAdapter

NAME = "check_stock"
WRITE = False
PUBLIC_ITEM_FIELDS = ("codigo", "cantidad", "marca", "bodega", "disponible", "ok")


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
    codigo = str(row.get("codigo") or "").strip().upper()
    if not codigo:
        return None
    try:
        cantidad = int(row.get("cantidad") or 0)
    except (TypeError, ValueError):
        return None
    try:
        disponible = int(row.get("disponible") or 0)
    except (TypeError, ValueError):
        disponible = 0
    item: dict[str, Any] = {
        "codigo": codigo,
        "cantidad": cantidad,
        "disponible": disponible,
        "ok": bool(row.get("ok")),
    }
    marca = str(row.get("marca") or "").strip()
    bodega = str(row.get("bodega") or "").strip()
    if marca:
        item["marca"] = marca
    if bodega:
        item["bodega"] = bodega
    return item


def handle(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    context = context or {}
    actor = str(context.get("actor_user") or "").strip()
    if not actor:
        return _error("principal_required", "Human principal is required")

    adapter = context.get("adapter")
    if not isinstance(adapter, ERPAdapter):
        return _error("erp_unavailable", "ERP adapter is not configured")

    raw_items = arguments.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        return _error("invalid_args", "'items' must be a non-empty list")

    payload_items: list[dict[str, Any]] = []
    for row in raw_items[:20]:
        if not isinstance(row, dict):
            continue
        item: dict[str, Any] = {
            "codigo": str(row.get("codigo") or "").strip().upper(),
            "cantidad": int(row.get("cantidad") or 0),
        }
        if row.get("marca"):
            item["marca"] = str(row.get("marca") or "").strip()
        if row.get("bodega"):
            item["bodega"] = str(row.get("bodega") or "").strip()
        payload_items.append(item)

    try:
        payload = adapter.call(
            "inventory/check-stock",
            {"items": payload_items},
            actor_user=actor,
            method="POST",
        )
    except AdapterError as exc:
        message = exc.message or "Stock check failed"
        nested = exc.payload.get("error") if isinstance(exc.payload, dict) else None
        if isinstance(nested, dict) and nested.get("message"):
            message = str(nested.get("message") or message)
        return _error(exc.code, message)

    if not payload.get("ok"):
        code = str(payload.get("error_code") or (payload.get("error") or {}).get("code") or "erp_error")
        message = str(
            payload.get("message")
            or (payload.get("error") or {}).get("message")
            or "Stock check failed"
        )
        return _error(code, message)

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    items = []
    for row in data.get("items") if isinstance(data.get("items"), list) else []:
        clean = _public_item(row)
        if clean:
            items.append(clean)
    available = bool(data.get("available")) if "available" in data else all(i["ok"] for i in items)
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    return {
        "ok": True,
        "tool": NAME,
        "classification": "INTERNAL",
        "write": False,
        "data": {"available": available, "items": items},
        "meta": {
            "environment": str(context.get("environment") or meta.get("environment") or ""),
            "count": len(items),
        },
    }
