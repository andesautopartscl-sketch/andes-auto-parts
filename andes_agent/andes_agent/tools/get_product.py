"""get_product — READ tool. Calls ERP internal product lookup via HTTP adapter."""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from andes_agent.adapters.erp_http import AdapterError, ERPAdapter

NAME = "get_product"
WRITE = False
ALLOWED_FIELDS = (
    "codigo",
    "descripcion",
    "marca",
    "modelo",
    "motor",
    "anio",
    "activo",
    "categoria",
    "subcategoria",
)


def _public_product(row: dict[str, Any] | None) -> dict[str, Any]:
    row = row if isinstance(row, dict) else {}
    activo_raw = row.get("activo", True)
    activo = True if activo_raw is None else bool(activo_raw)
    return {
        "codigo": str(row.get("codigo") or "").strip().upper(),
        "descripcion": str(row.get("descripcion") or "").strip(),
        "marca": str(row.get("marca") or "").strip(),
        "modelo": str(row.get("modelo") or "").strip(),
        "motor": str(row.get("motor") or "").strip(),
        "anio": str(row.get("anio") or "").strip(),
        "activo": activo,
        "categoria": str(row.get("categoria") or "").strip(),
        "subcategoria": str(row.get("subcategoria") or "").strip(),
    }


def _error(code: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error_code": code,
        "message": message,
        "error": {"code": code, "message": message},
    }


def handle(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    context = context or {}
    actor = str(context.get("actor_user") or "").strip()
    if not actor:
        return _error("principal_required", "Human principal is required")

    adapter = context.get("adapter")
    if not isinstance(adapter, ERPAdapter):
        return _error("erp_unavailable", "ERP adapter is not configured")

    codigo = str(arguments.get("codigo") or "").strip().upper()
    path = f"catalog/product/{quote(codigo, safe='')}"
    try:
        payload = adapter.call(path, None, actor_user=actor, method="GET")
    except AdapterError as exc:
        message = exc.message or "Product lookup failed"
        nested = exc.payload.get("error") if isinstance(exc.payload, dict) else None
        if isinstance(nested, dict) and nested.get("message"):
            message = str(nested.get("message") or message)
        return _error(exc.code, message)

    if not payload.get("ok"):
        code = str(payload.get("error_code") or (payload.get("error") or {}).get("code") or "erp_error")
        message = str(
            payload.get("message")
            or (payload.get("error") or {}).get("message")
            or "Product lookup failed"
        )
        return _error(code, message)

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    product = _public_product(data)
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    return {
        "ok": True,
        "tool": NAME,
        "classification": "INTERNAL",
        "write": False,
        "data": {key: product[key] for key in ALLOWED_FIELDS},
        "meta": {
            "environment": str(context.get("environment") or meta.get("environment") or ""),
        },
    }
