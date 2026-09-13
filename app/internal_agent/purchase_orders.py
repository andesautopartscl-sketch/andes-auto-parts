"""Read supplier purchase orders (DocumentoVenta.tipo=orden_compra).

This is NOT oc_clientes (customer POs). Andes stores supplier POs as ventas_documentos
with tipo='orden_compra'; the party name lives in cliente_nombre + proveedor_id.
Status values used in the ERP: pendiente, aprobada, entregada (UI STATUS_OPTIONS)
and anulada (checked in conversion/stock paths).
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
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
MAX_NUMERO_LEN = 60
MAX_PROVEEDOR_LEN = 120
OC_TIPO = "orden_compra"
OC_ESTADOS = frozenset({"pendiente", "aprobada", "entregada", "anulada"})
PUBLIC_LINE_FIELDS = (
    "codigo",
    "descripcion",
    "cantidad",
    "marca",
    "bodega",
    "origen_compra",
)
FINANCE_LINE_FIELDS = ("precio", "margen_pct", "subtotal")
BLOCKED_OC_FIELDS = frozenset({
    "password",
    "token",
    "email",
    "telefono",
    "rut",
    "cliente_rut",
    "cliente_email",
    "cliente_telefono",
    "id",
    "source_id",
    "root_id",
    "pago_referencia",
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


def validate_purchase_order_args(payload: Any) -> dict[str, Any]:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise InternalAuthError("invalid_args", "Body must be a JSON object", status=400)
    extra = set(payload.keys()) - {
        "numero",
        "proveedor",
        "estado",
        "fecha_desde",
        "fecha_hasta",
        "codigo",
        "limit",
    }
    if extra:
        raise InternalAuthError("invalid_args", f"Unknown argument(s): {', '.join(sorted(extra))}", status=400)
    for key in payload:
        if str(key).strip().lower() in FORBIDDEN_BODY_KEYS:
            raise InternalAuthError("invalid_args", f"Forbidden argument '{key}'", status=400)

    numero = _opt_limited_str(payload, "numero", MAX_NUMERO_LEN)
    if numero:
        numero = numero.upper()
    proveedor = _opt_limited_str(payload, "proveedor", MAX_PROVEEDOR_LEN)
    estado = _opt_limited_str(payload, "estado", 40)
    if estado:
        estado = estado.lower()
        if estado not in OC_ESTADOS:
            raise InternalAuthError("invalid_args", "'estado' is not a valid OC status", status=400)
    codigo = None
    if "codigo" in payload and payload.get("codigo") not in (None, ""):
        codigo = validate_product_codigo(payload.get("codigo"))
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
        "numero": numero,
        "proveedor": proveedor,
        "estado": estado,
        "codigo": codigo,
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


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _fecha_txt(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    return str(value).strip()[:10]


def _public_line(item: Any, *, include_finance: bool) -> dict[str, Any]:
    cantidad = _as_int(getattr(item, "cantidad", 0))
    precio = _as_float(getattr(item, "precio_unitario", None)) or 0.0
    row = {
        "codigo": str(getattr(item, "codigo_producto", "") or "").strip().upper(),
        "descripcion": str(getattr(item, "descripcion", "") or "").strip(),
        "cantidad": cantidad,
        "marca": str(getattr(item, "marca", "") or "").strip(),
        "bodega": str(getattr(item, "bodega", "") or "").strip() or "Bodega 1",
        "origen_compra": str(getattr(item, "origen_compra", "") or "").strip() or "nacional",
    }
    if include_finance:
        row["precio"] = round(precio, 2)
        margen = _as_float(getattr(item, "margen_porcentaje", None))
        if margen is not None:
            row["margen_pct"] = round(margen, 4)
        subtotal = _as_float(getattr(item, "subtotal", None))
        if subtotal is None:
            subtotal = cantidad * precio
        row["subtotal"] = round(subtotal, 2)
    return row


def _public_doc(doc: Any, *, include_finance: bool) -> dict[str, Any]:
    lines = [_public_line(item, include_finance=include_finance) for item in list(getattr(doc, "items", None) or [])]
    row = {
        "numero": str(getattr(doc, "numero", "") or "").strip(),
        "tipo": OC_TIPO,
        "fecha": _fecha_txt(getattr(doc, "fecha_documento", None)),
        "estado": str(getattr(doc, "status", "") or "pendiente").strip().lower() or "pendiente",
        "proveedor": str(getattr(doc, "cliente_nombre", "") or "").strip(),
        "lineas": len(lines),
        "items": lines,
    }
    if include_finance:
        total = _as_float(getattr(doc, "total", None))
        if total is not None:
            row["total"] = round(total, 2)
    return row


def get_public_purchase_orders(
    *,
    numero: str | None = None,
    proveedor: str | None = None,
    estado: str | None = None,
    codigo: str | None = None,
    fecha_desde: date | None = None,
    fecha_hasta: date | None = None,
    limit: int = DEFAULT_LIMIT,
    include_finance: bool = False,
) -> tuple[dict[str, Any], bool] | None:
    from app.extensions import db
    from app.internal_agent.product import get_public_product
    from app.ventas.models import DocumentoVenta, DocumentoVentaItem

    if codigo:
        product = get_public_product(codigo)
        if product is None:
            return None

    limit = max(MIN_LIMIT, min(MAX_LIMIT, int(limit or DEFAULT_LIMIT)))
    q = DocumentoVenta.query.filter(DocumentoVenta.tipo == OC_TIPO)
    if numero:
        q = q.filter(func.upper(DocumentoVenta.numero) == numero)
    if proveedor:
        q = q.filter(DocumentoVenta.cliente_nombre.ilike(f"%{proveedor}%"))
    if estado:
        q = q.filter(func.lower(DocumentoVenta.status) == estado)
    if fecha_desde is not None:
        q = q.filter(DocumentoVenta.fecha_documento >= datetime.combine(fecha_desde, time.min))
    if fecha_hasta is not None:
        q = q.filter(DocumentoVenta.fecha_documento < datetime.combine(fecha_hasta, time.min) + timedelta(days=1))
    if codigo:
        item_ids = db.session.query(DocumentoVentaItem.documento_id).filter(
            func.upper(func.trim(DocumentoVentaItem.codigo_producto)) == codigo
        )
        q = q.filter(DocumentoVenta.id.in_(item_ids))

    rows = (
        q.order_by(DocumentoVenta.fecha_documento.desc(), DocumentoVenta.id.desc())
        .limit(limit + 1)
        .all()
    )
    if numero and not rows:
        return None
    truncated = len(rows) > limit
    items = [_public_doc(doc, include_finance=include_finance) for doc in rows[:limit]]
    return {"items": items, "count": len(items)}, truncated
