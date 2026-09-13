"""get_dashboard_kpis — READ tool. KPI snapshot via ERP (no SQL in Gateway)."""
from __future__ import annotations

from typing import Any

from andes_agent.adapters.erp_http import AdapterError, ERPAdapter

NAME = "get_dashboard_kpis"
WRITE = False


def _error(code: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error_code": code,
        "message": message,
        "error": {"code": code, "message": message},
    }


def _as_float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _public_chart(rows: Any) -> list[dict[str, Any]]:
    out = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        point: dict[str, Any] = {"dia": str(row.get("dia") or "").strip()}
        if "total" in row:
            point["total"] = _as_float_or_none(row.get("total"))
        out.append(point)
    return out


def _public_products(rows: Any) -> list[dict[str, Any]]:
    out = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        item: dict[str, Any] = {
            "codigo": str(row.get("codigo") or "").strip().upper(),
            "descripcion": str(row.get("descripcion") or "").strip(),
            "qty": int(row.get("qty") or 0),
        }
        if "venta" in row:
            item["venta"] = _as_float_or_none(row.get("venta"))
        out.append(item)
    return out[:10]


def _public_clients(rows: Any) -> list[dict[str, Any]]:
    out = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        item: dict[str, Any] = {
            "nombre": str(row.get("nombre") or "").strip(),
            "docs": int(row.get("docs") or 0),
        }
        if "total" in row:
            item["total"] = _as_float_or_none(row.get("total"))
        out.append(item)
    return out[:10]


def _public_stock(rows: Any) -> list[dict[str, Any]] | None:
    if rows is None:
        return None
    out = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append(
            {
                "codigo": str(row.get("codigo") or "").strip().upper(),
                "marca": str(row.get("marca") or "").strip(),
                "bodega": str(row.get("bodega") or "").strip(),
                "stock": int(row.get("stock") or 0),
            }
        )
    return out[:20]


def handle(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    context = context or {}
    actor = str(context.get("actor_user") or "").strip()
    if not actor:
        return _error("principal_required", "Human principal is required")

    adapter = context.get("adapter")
    if not isinstance(adapter, ERPAdapter):
        return _error("erp_unavailable", "ERP adapter is not configured")

    payload_args: dict[str, Any] = {}
    for key in ("periodo", "fecha_desde", "fecha_hasta"):
        if arguments.get(key):
            payload_args[key] = str(arguments.get(key) or "").strip()
    for key in ("top_limit", "stock_threshold", "stock_limit"):
        if key in arguments and arguments.get(key) is not None:
            try:
                payload_args[key] = int(arguments[key])
            except (TypeError, ValueError):
                return _error("invalid_args", f"'{key}' must be an integer")

    try:
        payload = adapter.call("dashboard/kpis", payload_args, actor_user=actor, method="POST")
    except AdapterError as exc:
        message = exc.message or "Dashboard KPI lookup failed"
        nested = exc.payload.get("error") if isinstance(exc.payload, dict) else None
        if isinstance(nested, dict) and nested.get("message"):
            message = str(nested.get("message") or message)
        return _error(exc.code, message)

    if not payload.get("ok"):
        code = str(payload.get("error_code") or (payload.get("error") or {}).get("code") or "erp_error")
        message = str(
            payload.get("message")
            or (payload.get("error") or {}).get("message")
            or "Dashboard KPI lookup failed"
        )
        return _error(code, message)

    raw = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    data = {
        "ventas_hoy": _as_float_or_none(raw["ventas_hoy"]) if "ventas_hoy" in raw else None,
        "ventas_mes": _as_float_or_none(raw["ventas_mes"]) if "ventas_mes" in raw else None,
        "ventas_periodo": _as_float_or_none(raw["ventas_periodo"]) if "ventas_periodo" in raw else None,
        "docs_hoy": int(raw.get("docs_hoy") or 0),
        "docs_mes": int(raw.get("docs_mes") or 0),
        "docs_periodo": int(raw.get("docs_periodo") or 0),
        "chart_data": _public_chart(raw.get("chart_data")),
        "top_productos": _public_products(raw.get("top_productos")),
        "top_clientes": _public_clients(raw.get("top_clientes")),
        "stock_critico": _public_stock(raw.get("stock_critico")),
    }
    # Preserve explicit nulls for finance fields when ERP redacted them
    for key in ("ventas_hoy", "ventas_mes", "ventas_periodo"):
        if key in raw and raw.get(key) is None:
            data[key] = None
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    return {
        "ok": True,
        "tool": NAME,
        "classification": "CONFIDENTIAL",
        "write": False,
        "data": data,
        "meta": {
            "environment": str(context.get("environment") or meta.get("environment") or ""),
            "periodo": str(meta.get("periodo") or payload_args.get("periodo") or "snapshot"),
            "fecha_desde": str(meta.get("fecha_desde") or ""),
            "fecha_hasta": str(meta.get("fecha_hasta") or ""),
            "finanzas": bool(meta.get("finanzas")),
            "stock_incluido": bool(meta.get("stock_incluido")),
        },
    }
