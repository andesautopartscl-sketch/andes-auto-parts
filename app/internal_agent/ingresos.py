"""Read warehouse receipts from ingresos_documentos / items. Does not rewrite bodega logic.

An ingreso is IngresoDocumento (supplier receipt). Each IngresoDocumentoItem is a line
(codigo, qty, marca, bodega, origen_compra, valor_neto, precio_venta_neto, margen_pct).
Cancellation is the boolean ``anulado`` on the document (plus anulado_at / anulado_por).
Costs are ``valor_neto`` (unit net); sale price is ``precio_venta_neto``; margin is ``margen_pct``.
Finance fields follow user_can_view_finanzas (ver_finanzas → mod_finanzas), same as historial.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from sqlalchemy import func

from app.internal_agent.m2m import FORBIDDEN_BODY_KEYS, InternalAuthError
from app.internal_agent.product import validate_product_codigo

SQL_VALUE_RE = re.compile(
    r"\b(SELECT|INSERT|UPDATE|DELETE|DROP|UNION|ALTER|EXEC|MERGE|TRUNCATE)\b",
    re.IGNORECASE,
)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MIN_LIMIT = 1
MAX_LIMIT = 20
DEFAULT_LIMIT = 20
MAX_PROVEEDOR_LEN = 120
MAX_NUMERO_LEN = 60
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
BLOCKED_INGRESO_FIELDS = frozenset({
    "password",
    "token",
    "email",
    "telefono",
    "proveedor_rut",
    "proveedor_email",
    "proveedor_direccion",
    "rut",
    "id",
    "ingreso_documento_id",
    "total_factura",
    "iva_factura",
    "monto_saldo_favor",
    "metodo_pago",
    "anulado_por",
    "anulacion_motivo",
    "codigo_proveedor",
})


def _parse_date(raw: Any, label: str) -> date | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise InternalAuthError("invalid_args", f"'{label}' must be a string", status=400)
    text = raw.strip()
    if not text:
        return None
    if SQL_VALUE_RE.search(text):
        raise InternalAuthError("invalid_args", "SQL is not allowed in arguments", status=400)
    if not DATE_RE.match(text):
        raise InternalAuthError("invalid_args", f"'{label}' must be YYYY-MM-DD", status=400)
    try:
        return date.fromisoformat(text)
    except ValueError as orig:
        raise InternalAuthError("invalid_args", f"'{label}' must be YYYY-MM-DD", status=400) from orig


def _opt_limited_str(payload: dict[str, Any], key: str, max_len: int) -> str | None:
    if key not in payload:
        return None
    raw = payload[key]
    if not isinstance(raw, str):
        raise InternalAuthError("invalid_args", f"'{key}' must be a string", status=400)
    text = raw.strip()
    if not text:
        return None
    if SQL_VALUE_RE.search(text):
        raise InternalAuthError("invalid_args", "SQL is not allowed in arguments", status=400)
    if len(text) > max_len:
        raise InternalAuthError("invalid_args", f"'{key}' is too long", status=400)
    return text


def validate_ingreso_args(payload: Any) -> dict[str, Any]:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise InternalAuthError("invalid_args", "Body must be a JSON object", status=400)
    extra = set(payload.keys()) - {"codigo", "proveedor", "numero_documento", "fecha_desde", "fecha_hasta", "limit"}
    if extra:
        raise InternalAuthError("invalid_args", f"Unknown argument(s): {', '.join(sorted(extra))}", status=400)
    for key in payload:
        if str(key).strip().lower() in FORBIDDEN_BODY_KEYS:
            raise InternalAuthError("invalid_args", f"Forbidden argument '{key}'", status=400)

    codigo = None
    if "codigo" in payload and payload.get("codigo") not in (None, ""):
        codigo = validate_product_codigo(payload.get("codigo"))
    proveedor = _opt_limited_str(payload, "proveedor", MAX_PROVEEDOR_LEN)
    numero_documento = _opt_limited_str(payload, "numero_documento", MAX_NUMERO_LEN)
    fecha_desde = _parse_date(payload.get("fecha_desde"), "fecha_desde") if "fecha_desde" in payload else None
    fecha_hasta = _parse_date(payload.get("fecha_hasta"), "fecha_hasta") if "fecha_hasta" in payload else None
    if fecha_desde and fecha_hasta and fecha_desde > fecha_hasta:
        raise InternalAuthError("invalid_args", "fecha_desde must be <= fecha_hasta", status=400)

    limit = DEFAULT_LIMIT
    if "limit" in payload:
        raw = payload["limit"]
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise InternalAuthError("invalid_args", "'limit' must be an integer", status=400)
        if raw < MIN_LIMIT or raw > MAX_LIMIT:
            raise InternalAuthError("invalid_args", "'limit' must be between 1 and 20", status=400)
        limit = raw
    return {
        "codigo": codigo,
        "proveedor": proveedor,
        "numero_documento": numero_documento,
        "fecha_desde": fecha_desde,
        "fecha_hasta": fecha_hasta,
        "limit": limit,
    }


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _public_item(item: Any, doc: Any, *, include_finance: bool) -> dict[str, Any]:
    numero = str(getattr(doc, "numero_documento", "") or "").strip()
    if not numero:
        numero = f"ING-{int(getattr(doc, 'id', 0) or 0)}"
    fecha = getattr(doc, "fecha_documento", None)
    fecha_txt = fecha.isoformat() if hasattr(fecha, "isoformat") else str(fecha or "").strip()
    try:
        cantidad = int(getattr(item, "cantidad", 0) or 0)
    except (TypeError, ValueError):
        cantidad = 0
    origen = str(getattr(item, "origen_compra", "") or "").strip() or "nacional"
    row = {
        "fecha": fecha_txt,
        "numero_documento": numero,
        "proveedor": str(getattr(doc, "proveedor_nombre", "") or "").strip(),
        "anulado": bool(getattr(doc, "anulado", False)),
        "codigo": str(getattr(item, "codigo_producto", "") or "").strip().upper(),
        "descripcion": str(getattr(item, "descripcion_producto", "") or "").strip(),
        "marca": str(getattr(item, "marca", "") or "").strip(),
        "bodega": str(getattr(item, "bodega", "") or "").strip(),
        "origen_compra": origen,
        "cantidad": cantidad,
    }
    if include_finance:
        costo = _as_float(getattr(item, "valor_neto", None))
        pvp = _as_float(getattr(item, "precio_venta_neto", None))
        margen = _as_float(getattr(item, "margen_pct", None))
        if costo is not None:
            row["costo_neto"] = costo
        if pvp is not None:
            row["precio_venta_neto"] = pvp
        if margen is not None:
            row["margen_pct"] = margen
    return row


def get_public_ingresos(
    *,
    codigo: str | None = None,
    proveedor: str | None = None,
    numero_documento: str | None = None,
    fecha_desde: date | None = None,
    fecha_hasta: date | None = None,
    limit: int = DEFAULT_LIMIT,
    include_finance: bool = False,
) -> tuple[dict[str, Any], bool] | None:
    from app.bodega.models import IngresoDocumento, IngresoDocumentoItem
    from app.extensions import db
    from app.internal_agent.product import get_public_product

    product = None
    if codigo:
        product = get_public_product(codigo)
        if product is None:
            return None

    limit = max(MIN_LIMIT, min(MAX_LIMIT, int(limit or DEFAULT_LIMIT)))
    q = db.session.query(IngresoDocumentoItem, IngresoDocumento).join(
        IngresoDocumento,
        IngresoDocumento.id == IngresoDocumentoItem.ingreso_documento_id,
    )
    if codigo:
        q = q.filter(func.upper(func.trim(IngresoDocumentoItem.codigo_producto)) == codigo)
    if proveedor:
        q = q.filter(IngresoDocumento.proveedor_nombre.ilike(f"%{proveedor}%"))
    if numero_documento:
        q = q.filter(IngresoDocumento.numero_documento.ilike(f"%{numero_documento}%"))
    if fecha_desde is not None:
        q = q.filter(IngresoDocumento.fecha_documento >= fecha_desde)
    if fecha_hasta is not None:
        q = q.filter(IngresoDocumento.fecha_documento <= fecha_hasta)
    rows = (
        q.order_by(
            IngresoDocumento.fecha_documento.desc(),
            IngresoDocumento.id.desc(),
            IngresoDocumentoItem.id.desc(),
        )
        .limit(limit + 1)
        .all()
    )
    truncated = len(rows) > limit
    items = [_public_item(item, doc, include_finance=include_finance) for item, doc in rows[:limit]]
    data: dict[str, Any] = {"items": items, "count": len(items)}
    if product is not None:
        data["codigo"] = product["codigo"]
        data["descripcion"] = product["descripcion"]
    return data, truncated
