"""get_supplier — READ tool. Proveedor directory (ventas_proveedores only).

Never merges clientes or oc_clientes. PII is never copied.
"""
from __future__ import annotations

from typing import Any

from andes_agent.adapters.erp_http import AdapterError, ERPAdapter

NAME = "get_supplier"
WRITE = False
PUBLIC_FIELDS = (
    "id",
    "nombre",
    "empresa",
    "rut",
    "giro",
    "comuna",
    "ciudad",
    "region",
    "pais",
    "activo",
)


def _error(code: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error_code": code,
        "message": message,
        "error": {"code": code, "message": message},
    }


def _public_supplier(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    try:
        supplier_id = int(row.get("id") or 0)
    except (TypeError, ValueError):
        supplier_id = 0
    if supplier_id < 1:
        return None
    return {
        "id": supplier_id,
        "nombre": str(row.get("nombre") or "").strip(),
        "empresa": str(row.get("empresa") or "").strip(),
        "rut": str(row.get("rut") or "").strip(),
        "giro": str(row.get("giro") or "").strip(),
        "comuna": str(row.get("comuna") or "").strip(),
        "ciudad": str(row.get("ciudad") or "").strip(),
        "region": str(row.get("region") or "").strip(),
        "pais": str(row.get("pais") or "").strip() or "Chile",
        "activo": bool(row.get("activo", True)),
    }


def handle(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    context = context or {}
    actor = str(context.get("actor_user") or "").strip()
    if not actor:
        return _error("principal_required", "Human principal is required")

    adapter = context.get("adapter")
    if not isinstance(adapter, ERPAdapter):
        return _error("erp_unavailable", "ERP adapter is not configured")

    payload_args: dict[str, Any] = {}
    q = str(arguments.get("q") or "").strip()
    if q:
        payload_args["q"] = q
    rut = str(arguments.get("rut") or "").strip()
    if rut:
        payload_args["rut"] = rut
    if "id" in arguments and arguments.get("id") is not None:
        try:
            payload_args["id"] = int(arguments["id"])
        except (TypeError, ValueError):
            return _error("invalid_args", "'id' must be a positive integer")
    try:
        limit = int(arguments.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(20, limit))
    payload_args["limit"] = limit

    try:
        payload = adapter.call("ventas/suppliers", payload_args, actor_user=actor, method="POST")
    except AdapterError as exc:
        message = exc.message or "Supplier lookup failed"
        nested = exc.payload.get("error") if isinstance(exc.payload, dict) else None
        if isinstance(nested, dict) and nested.get("message"):
            message = str(nested.get("message") or message)
        return _error(exc.code, message)

    if not payload.get("ok"):
        code = str(payload.get("error_code") or (payload.get("error") or {}).get("code") or "erp_error")
        message = str(
            payload.get("message")
            or (payload.get("error") or {}).get("message")
            or "Supplier lookup failed"
        )
        return _error(code, message)

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    items = []
    for row in data.get("items") if isinstance(data.get("items"), list) else []:
        clean = _public_supplier(row)
        if clean:
            items.append(clean)
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
