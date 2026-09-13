"""READ-ONLY dashboard KPI snapshot for the Agent Gateway.

Reuses the same business rules as app/dashboard/routes.py:
factura/boleta, not anulada. Tops are always period-bounded (never all-time).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func

from app.internal_agent.m2m import FORBIDDEN_BODY_KEYS, InternalAuthError

SQL_VALUE_RE = re.compile(
    r"\b(SELECT|INSERT|UPDATE|DELETE|DROP|UNION|ALTER|EXEC|MERGE|TRUNCATE)\b",
    re.IGNORECASE,
)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ALLOWED_PERIODOS = frozenset({"snapshot", "hoy", "mes", "7d", "30d", "custom"})
FACTURA_TIPOS = ("factura", "boleta")
MAX_RANGE_DAYS = 90
DEFAULT_TOP_LIMIT = 5
MAX_TOP_LIMIT = 10
DEFAULT_STOCK_THRESHOLD = 3
MAX_STOCK_THRESHOLD = 10
DEFAULT_STOCK_LIMIT = 10
MAX_STOCK_LIMIT = 20


def _parse_date(raw: str, label: str) -> date:
    if not DATE_RE.match(raw):
        raise InternalAuthError("invalid_args", f"'{label}' must be YYYY-MM-DD", status=400)
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError as exc:
        raise InternalAuthError("invalid_args", f"'{label}' is not a valid date", status=400) from exc


def validate_dashboard_args(payload: Any) -> dict[str, Any]:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise InternalAuthError("invalid_args", "Body must be a JSON object", status=400)
    allowed = {"periodo", "fecha_desde", "fecha_hasta", "top_limit", "stock_threshold", "stock_limit"}
    extra = set(payload.keys()) - allowed
    if extra:
        raise InternalAuthError("invalid_args", f"Unknown argument(s): {', '.join(sorted(extra))}", status=400)
    for key in payload:
        if str(key).strip().lower() in FORBIDDEN_BODY_KEYS:
            raise InternalAuthError("invalid_args", f"Forbidden argument '{key}'", status=400)

    periodo = "snapshot"
    if "periodo" in payload:
        raw = payload["periodo"]
        if not isinstance(raw, str):
            raise InternalAuthError("invalid_args", "'periodo' must be a string", status=400)
        periodo = raw.strip().lower()
        if SQL_VALUE_RE.search(periodo):
            raise InternalAuthError("invalid_args", "SQL is not allowed in arguments", status=400)
        if periodo not in ALLOWED_PERIODOS:
            raise InternalAuthError("invalid_args", "'periodo' is not valid", status=400)

    fecha_desde = None
    fecha_hasta = None
    if "fecha_desde" in payload:
        raw = payload["fecha_desde"]
        if not isinstance(raw, str):
            raise InternalAuthError("invalid_args", "'fecha_desde' must be a string", status=400)
        if SQL_VALUE_RE.search(raw):
            raise InternalAuthError("invalid_args", "SQL is not allowed in arguments", status=400)
        fecha_desde = _parse_date(raw.strip(), "fecha_desde")
    if "fecha_hasta" in payload:
        raw = payload["fecha_hasta"]
        if not isinstance(raw, str):
            raise InternalAuthError("invalid_args", "'fecha_hasta' must be a string", status=400)
        if SQL_VALUE_RE.search(raw):
            raise InternalAuthError("invalid_args", "SQL is not allowed in arguments", status=400)
        fecha_hasta = _parse_date(raw.strip(), "fecha_hasta")

    if fecha_desde is not None or fecha_hasta is not None:
        if fecha_desde is None or fecha_hasta is None:
            raise InternalAuthError("invalid_args", "Provide both fecha_desde and fecha_hasta", status=400)
        if periodo not in {"snapshot", "custom"}:
            # explicit custom range overrides named periodo
            periodo = "custom"
        elif periodo == "snapshot":
            periodo = "custom"

    top_limit = DEFAULT_TOP_LIMIT
    if "top_limit" in payload:
        raw = payload["top_limit"]
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1 or raw > MAX_TOP_LIMIT:
            raise InternalAuthError("invalid_args", "'top_limit' must be between 1 and 10", status=400)
        top_limit = raw

    stock_threshold = DEFAULT_STOCK_THRESHOLD
    if "stock_threshold" in payload:
        raw = payload["stock_threshold"]
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0 or raw > MAX_STOCK_THRESHOLD:
            raise InternalAuthError("invalid_args", "'stock_threshold' must be between 0 and 10", status=400)
        stock_threshold = raw

    stock_limit = DEFAULT_STOCK_LIMIT
    if "stock_limit" in payload:
        raw = payload["stock_limit"]
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1 or raw > MAX_STOCK_LIMIT:
            raise InternalAuthError("invalid_args", "'stock_limit' must be between 1 and 20", status=400)
        stock_limit = raw

    if periodo == "custom" and (fecha_desde is None or fecha_hasta is None):
        raise InternalAuthError("invalid_args", "Custom periodo requires fecha_desde and fecha_hasta", status=400)

    return {
        "periodo": periodo,
        "fecha_desde": fecha_desde,
        "fecha_hasta": fecha_hasta,
        "top_limit": top_limit,
        "stock_threshold": stock_threshold,
        "stock_limit": stock_limit,
    }


def resolve_period_window(args: dict[str, Any], *, today: date | None = None) -> tuple[str, date, date]:
    today = today or date.today()
    periodo = args["periodo"]
    if periodo == "custom":
        start = args["fecha_desde"]
        end = args["fecha_hasta"]
        if start is None or end is None:
            raise InternalAuthError("invalid_args", "Custom periodo requires fecha_desde and fecha_hasta", status=400)
    elif periodo == "hoy":
        start = end = today
    elif periodo == "mes":
        start = today.replace(day=1)
        end = today
    elif periodo == "7d":
        start = today - timedelta(days=6)
        end = today
    elif periodo == "30d":
        start = today - timedelta(days=29)
        end = today
    else:  # snapshot — primary window = current month
        start = today.replace(day=1)
        end = today
        periodo = "snapshot"

    if start > end:
        raise InternalAuthError("invalid_args", "fecha_desde must be <= fecha_hasta", status=400)
    if end > today + timedelta(days=1):
        raise InternalAuthError("invalid_args", "fecha_hasta cannot be far in the future", status=400)
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise InternalAuthError("invalid_args", "Date range must be at most 90 days", status=400)
    return periodo, start, end


def _sale_docs_filter(model, start: date, end: date):
    return (
        model.tipo.in_(FACTURA_TIPOS),
        model.status != "anulada",
        func.date(model.fecha_documento) >= start,
        func.date(model.fecha_documento) <= end,
    )


def _sum_ventas(start: date, end: date) -> float:
    from app.extensions import db
    from app.ventas.models import DocumentoVenta

    result = (
        db.session.query(func.sum(DocumentoVenta.total))
        .filter(*_sale_docs_filter(DocumentoVenta, start, end))
        .scalar()
    )
    return float(result or 0)


def _count_docs(start: date, end: date) -> int:
    from app.ventas.models import DocumentoVenta

    return (
        DocumentoVenta.query.filter(*_sale_docs_filter(DocumentoVenta, start, end)).count()
    )


def _chart_data(start: date, end: date) -> list[dict[str, Any]]:
    from app.extensions import db
    from app.ventas.models import DocumentoVenta

    rows = (
        db.session.query(
            func.date(DocumentoVenta.fecha_documento).label("dia"),
            func.sum(DocumentoVenta.total).label("total"),
        )
        .filter(*_sale_docs_filter(DocumentoVenta, start, end))
        .group_by(func.date(DocumentoVenta.fecha_documento))
        .order_by(func.date(DocumentoVenta.fecha_documento))
        .all()
    )
    totals = {str(r.dia): float(r.total or 0) for r in rows}
    out = []
    cursor = start
    while cursor <= end:
        key = str(cursor)
        out.append({"dia": key, "total": totals.get(key, 0.0)})
        cursor += timedelta(days=1)
    return out


def _top_productos(start: date, end: date, limit: int) -> list[dict[str, Any]]:
    from app.extensions import db
    from app.ventas.models import DocumentoVenta, DocumentoVentaItem

    rows = (
        db.session.query(
            DocumentoVentaItem.codigo_producto,
            DocumentoVentaItem.descripcion,
            func.sum(DocumentoVentaItem.cantidad).label("total_qty"),
            func.sum(DocumentoVentaItem.subtotal).label("total_venta"),
        )
        .join(DocumentoVenta, DocumentoVentaItem.documento_id == DocumentoVenta.id)
        .filter(*_sale_docs_filter(DocumentoVenta, start, end))
        .group_by(DocumentoVentaItem.codigo_producto, DocumentoVentaItem.descripcion)
        .order_by(func.sum(DocumentoVentaItem.subtotal).desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "codigo": str(r.codigo_producto or "").strip().upper(),
            "descripcion": str(r.descripcion or r.codigo_producto or "").strip(),
            "qty": int(r.total_qty or 0),
            "venta": float(r.total_venta or 0),
        }
        for r in rows
    ]


def _top_clientes(start: date, end: date, limit: int) -> list[dict[str, Any]]:
    """Aggregate by documento.cliente_nombre only — never join ventas_clientes."""
    from app.extensions import db
    from app.ventas.models import DocumentoVenta

    rows = (
        db.session.query(
            DocumentoVenta.cliente_nombre,
            func.sum(DocumentoVenta.total).label("total_venta"),
            func.count(DocumentoVenta.id).label("num_docs"),
        )
        .filter(*_sale_docs_filter(DocumentoVenta, start, end))
        .group_by(DocumentoVenta.cliente_nombre)
        .order_by(func.sum(DocumentoVenta.total).desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "nombre": str(r.cliente_nombre or "Sin nombre").strip(),
            "docs": int(r.num_docs or 0),
            "total": float(r.total_venta or 0),
        }
        for r in rows
    ]


def _stock_critico(threshold: int, limit: int) -> list[dict[str, Any]]:
    from app.bodega.models import ProductoVarianteStock

    rows = (
        ProductoVarianteStock.query.filter(
            ProductoVarianteStock.stock <= threshold,
            ProductoVarianteStock.stock >= 0,
        )
        .order_by(ProductoVarianteStock.stock.asc())
        .limit(limit)
        .all()
    )
    return [
        {
            "codigo": str(r.codigo_producto or "").strip().upper(),
            "marca": str(r.marca or "").strip(),
            "bodega": str(r.bodega or "").strip(),
            "stock": int(r.stock or 0),
        }
        for r in rows
    ]


def _redact_finance(data: dict[str, Any]) -> dict[str, Any]:
    out = dict(data)
    out["ventas_hoy"] = None
    out["ventas_mes"] = None
    out["ventas_periodo"] = None
    out["chart_data"] = [{**point, "total": None} for point in (out.get("chart_data") or [])]
    out["top_productos"] = [
        {**row, "venta": None} for row in (out.get("top_productos") or [])
    ]
    out["top_clientes"] = [
        {**row, "total": None} for row in (out.get("top_clientes") or [])
    ]
    return out


def get_public_dashboard_kpis(
    args: dict[str, Any],
    *,
    include_finance: bool,
    include_stock: bool,
    today: date | None = None,
) -> dict[str, Any]:
    today = today or date.today()
    periodo, start, end = resolve_period_window(args, today=today)
    first_of_month = today.replace(day=1)

    # Chart window: snapshot uses 30d; otherwise the resolved period window
    if periodo == "snapshot":
        chart_start = today - timedelta(days=29)
        chart_end = today
        tops_start, tops_end = first_of_month, today
    else:
        chart_start, chart_end = start, end
        tops_start, tops_end = start, end

    data: dict[str, Any] = {
        "ventas_hoy": _sum_ventas(today, today),
        "ventas_mes": _sum_ventas(first_of_month, today),
        "docs_hoy": _count_docs(today, today),
        "docs_mes": _count_docs(first_of_month, today),
        "ventas_periodo": _sum_ventas(start, end),
        "docs_periodo": _count_docs(start, end),
        "chart_data": _chart_data(chart_start, chart_end),
        "top_productos": _top_productos(tops_start, tops_end, args["top_limit"]),
        "top_clientes": _top_clientes(tops_start, tops_end, args["top_limit"]),
    }
    if include_stock:
        data["stock_critico"] = _stock_critico(args["stock_threshold"], args["stock_limit"])
    else:
        data["stock_critico"] = None

    if not include_finance:
        data = _redact_finance(data)

    meta = {
        "periodo": periodo,
        "fecha_desde": str(start),
        "fecha_hasta": str(end),
        "chart_desde": str(chart_start),
        "chart_hasta": str(chart_end),
        "finanzas": bool(include_finance),
        "stock_incluido": bool(include_stock),
        "top_limit": args["top_limit"],
    }
    return {"data": data, "meta": meta}
