"""search_catalog — READ tool. Calls ERP internal catalog search via HTTP adapter."""
from __future__ import annotations

from typing import Any

from andes_agent.adapters.erp_http import AdapterError, ERPAdapter

NAME = "search_catalog"
WRITE = False


def handle(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    context = context or {}
    actor = str(context.get("actor_user") or "").strip()
    if not actor:
        return {"ok": False, "error_code": "principal_required", "message": "Human principal is required"}

    adapter = context.get("adapter")
    if not isinstance(adapter, ERPAdapter):
        return {"ok": False, "error_code": "erp_unavailable", "message": "ERP adapter is not configured"}

    try:
        payload = adapter.call(
            "catalog/search",
            {"q": arguments.get("q"), "limit": arguments.get("limit")},
            actor_user=actor,
        )
    except AdapterError as exc:
        return {"ok": False, "error_code": exc.code, "message": exc.message}

    if not payload.get("ok"):
        return {
            "ok": False,
            "error_code": payload.get("error_code") or "erp_error",
            "message": payload.get("message") or "Catalog search failed",
        }

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    items = data.get("items") if isinstance(data.get("items"), list) else []
    clean_items = []
    for row in items:
        if not isinstance(row, dict):
            continue
        clean_items.append(
            {
                "codigo": str(row.get("codigo") or ""),
                "descripcion": str(row.get("descripcion") or ""),
                "marca": str(row.get("marca") or ""),
                "modelo": str(row.get("modelo") or ""),
            }
        )
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    try:
        limit = int(arguments.get("limit") or meta.get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(25, limit))
    if len(clean_items) > limit:
        clean_items = clean_items[:limit]
        truncated = True
    else:
        truncated = bool(meta.get("truncated")) and len(clean_items) == limit
    return {
        "ok": True,
        "tool": NAME,
        "classification": "INTERNAL",
        "write": False,
        "data": {"items": clean_items, "count": len(clean_items)},
        "meta": {
            "limit": limit,
            "truncated": truncated,
            "environment": str(context.get("environment") or meta.get("environment") or ""),
        },
    }
