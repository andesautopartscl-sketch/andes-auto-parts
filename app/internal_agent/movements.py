"""Read stock movements from movimientos_stock. Does not rewrite bodega logic.

Quantity semantics (do not invert signs):
- ``tipo`` is the ERP classification: ingreso | salida | ajuste.
- ``cantidad`` is stored as written. The same tipo may use a positive or
  negative quantity depending on the source (bodega vs ventas vs ajustes).
- A negative quantity does not by itself mean salida.
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
MAX_LIMIT = 50
DEFAULT_LIMIT = 20
ALLOWED_ITEM_FIELDS = (
    "fecha",
    "tipo",
    "cantidad",
    "marca",
    "bodega",
    "origen_compra",
    "usuario",
    "observacion",
)
BLOCKED_MOVEMENT_FIELDS = frozenset({
    "precio",
    "precio_venta_neto",
    "total_neto",
    "costo",
    "margen",
    "proveedor",
    "proveedor_rut",
    "password",
    "token",
    "rut",
    "email",
    "telefono",
    "id",
    "ingreso_documento_id",
    "ref_sii",
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


def validate_movement_args(payload: Any) -> tuple[str, date | None, date | None, int]:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise InternalAuthError("invalid_args", "Body must be a JSON object", status=400)
    extra = set(payload.keys()) - {"codigo", "fecha_desde", "fecha_hasta", "limit"}
    if extra:
        raise InternalAuthError("invalid_args", f"Unknown argument(s): {', '.join(sorted(extra))}", status=400)
    for key in payload:
        if str(key).strip().lower() in FORBIDDEN_BODY_KEYS:
            raise InternalAuthError("invalid_args", f"Forbidden argument '{key}'", status=400)

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
            raise InternalAuthError("invalid_args", "'limit' must be between 1 and 50", status=400)
        limit = raw
    return codigo, fecha_desde, fecha_hasta, limit


def _public_item(row: Any) -> dict[str, Any]:
    from app.utils.datetime_utils import utc_to_chile

    local = utc_to_chile(getattr(row, "fecha", None))
    fecha = local.strftime("%Y-%m-%d") if local is not None else ""
    try:
        cantidad = int(getattr(row, "cantidad", 0) or 0)
    except (TypeError, ValueError):
        cantidad = 0
    usuario = str(getattr(row, "usuario", "") or "").strip()
    if "@" in usuario:
        usuario = ""
    observacion = str(getattr(row, "observacion", "") or "").strip()
    if len(observacion) > 180:
        observacion = observacion[:180] + "…"
    origen = str(getattr(row, "origen_compra", "") or "").strip() or "nacional"
    tipo = str(getattr(row, "tipo", "") or "").strip().lower()
    return {
        "fecha": fecha,
        "tipo": tipo,
        "cantidad": cantidad,
        "marca": str(getattr(row, "marca", "") or "").strip(),
        "bodega": str(getattr(row, "bodega", "") or "").strip(),
        "origen_compra": origen,
        "usuario": usuario,
        "observacion": observacion,
    }


def get_public_movements(
    codigo: str,
    fecha_desde: date | None = None,
    fecha_hasta: date | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[dict[str, Any], bool] | None:
    from app.bodega.models import MovimientoStock
    from app.internal_agent.product import get_public_product
    from app.utils.datetime_utils import chile_day_end_exclusive_utc, chile_day_start_utc

    product = get_public_product(codigo)
    if product is None:
        return None

    limit = max(MIN_LIMIT, min(MAX_LIMIT, int(limit or DEFAULT_LIMIT)))
    q = MovimientoStock.query.filter(
        func.upper(func.trim(MovimientoStock.codigo_producto)) == codigo
    )
    if fecha_desde is not None:
        q = q.filter(MovimientoStock.fecha >= chile_day_start_utc(fecha_desde))
    if fecha_hasta is not None:
        q = q.filter(MovimientoStock.fecha < chile_day_end_exclusive_utc(fecha_hasta))
    rows = (
        q.order_by(MovimientoStock.fecha.desc(), MovimientoStock.id.desc())
        .limit(limit + 1)
        .all()
    )
    truncated = len(rows) > limit
    items = [_public_item(row) for row in rows[:limit]]
    return (
        {
            "codigo": product["codigo"],
            "descripcion": product["descripcion"],
            "items": items,
            "count": len(items),
        },
        truncated,
    )
