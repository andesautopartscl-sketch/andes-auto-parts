"""Read customer directory from ventas_clientes (Cliente only).

Does NOT query ventas_proveedores or oc_clientes.
PII (email, telefono, direccion) is never returned.
Wholesale/margin fields require the same finance gate as OC prices.
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import func

from app.internal_agent.m2m import FORBIDDEN_BODY_KEYS, InternalAuthError
from app.utils.rut_utils import clean_rut, format_rut

SQL_VALUE_RE = re.compile(
    r"\b(SELECT|INSERT|UPDATE|DELETE|DROP|UNION|ALTER|EXEC|MERGE|TRUNCATE)\b",
    re.IGNORECASE,
)
MIN_LIMIT = 1
MAX_LIMIT = 20
DEFAULT_LIMIT = 20
MAX_Q_LEN = 80
MAX_RUT_LEN = 20
BLOCKED_CUSTOMER_FIELDS = frozenset({
    "password",
    "token",
    "email",
    "telefono",
    "direccion",
    "created_at",
})
PUBLIC_FIELDS = (
    "id",
    "nombre",
    "rut",
    "giro",
    "comuna",
    "ciudad",
    "region",
    "pais",
    "activo",
)
FINANCE_FIELDS = (
    "cliente_mayorista",
    "margen_descuento_pct",
)


def _normalized_rut_sql(column):
    return func.upper(func.replace(func.replace(func.coalesce(column, ""), ".", ""), "-", ""))


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


def validate_customer_args(payload: Any) -> dict[str, Any]:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise InternalAuthError("invalid_args", "Body must be a JSON object", status=400)
    extra = set(payload.keys()) - {"q", "rut", "id", "limit"}
    if extra:
        raise InternalAuthError("invalid_args", f"Unknown argument(s): {', '.join(sorted(extra))}", status=400)
    for key in payload:
        if str(key).strip().lower() in FORBIDDEN_BODY_KEYS:
            raise InternalAuthError("invalid_args", f"Forbidden argument '{key}'", status=400)

    q = _opt_limited_str(payload, "q", MAX_Q_LEN)
    rut_raw = _opt_limited_str(payload, "rut", MAX_RUT_LEN)
    rut = clean_rut(rut_raw) if rut_raw else None
    if rut_raw and not rut:
        raise InternalAuthError("invalid_args", "'rut' is invalid", status=400)

    customer_id = None
    if "id" in payload:
        raw_id = payload["id"]
        if isinstance(raw_id, bool) or not isinstance(raw_id, int) or raw_id < 1:
            raise InternalAuthError("invalid_args", "'id' must be a positive integer", status=400)
        customer_id = raw_id

    if not q and not rut and customer_id is None:
        raise InternalAuthError("invalid_args", "Provide q, rut or id", status=400)

    limit = DEFAULT_LIMIT
    if "limit" in payload:
        raw = payload["limit"]
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise InternalAuthError("invalid_args", "'limit' must be an integer", status=400)
        if raw < MIN_LIMIT or raw > MAX_LIMIT:
            raise InternalAuthError("invalid_args", "'limit' must be between 1 and 20", status=400)
        limit = raw

    return {"q": q, "rut": rut, "id": customer_id, "limit": limit}


def _public_customer(row: Any, *, include_finance: bool) -> dict[str, Any]:
    item = {
        "id": int(getattr(row, "id", 0) or 0),
        "nombre": str(getattr(row, "nombre", "") or "").strip(),
        "rut": format_rut(getattr(row, "rut", "") or ""),
        "giro": str(getattr(row, "giro", "") or "").strip(),
        "comuna": str(getattr(row, "comuna", "") or "").strip(),
        "ciudad": str(getattr(row, "ciudad", "") or "").strip(),
        "region": str(getattr(row, "region", "") or "").strip(),
        "pais": str(getattr(row, "pais", "") or "").strip() or "Chile",
        "activo": bool(getattr(row, "activo", True)),
    }
    if include_finance:
        item["cliente_mayorista"] = bool(getattr(row, "cliente_mayorista", False))
        margen = getattr(row, "margen_descuento_pct", None)
        try:
            item["margen_descuento_pct"] = round(float(margen or 0), 4)
        except (TypeError, ValueError):
            item["margen_descuento_pct"] = 0.0
    return item


def get_public_customers(
    *,
    q: str | None = None,
    rut: str | None = None,
    customer_id: int | None = None,
    limit: int = DEFAULT_LIMIT,
    include_finance: bool = False,
) -> tuple[dict[str, Any], bool] | None:
    from app.ventas.models import Cliente

    limit = max(MIN_LIMIT, min(MAX_LIMIT, int(limit or DEFAULT_LIMIT)))
    base = Cliente.query.filter(Cliente.activo.is_(True))

    if customer_id is not None:
        row = base.filter(Cliente.id == customer_id).first()
        if row is None:
            return None
        item = _public_customer(row, include_finance=include_finance)
        return {"items": [item], "count": 1}, False

    if rut:
        row = base.filter(_normalized_rut_sql(Cliente.rut) == rut.upper()).first()
        if row is None:
            return None
        item = _public_customer(row, include_finance=include_finance)
        return {"items": [item], "count": 1}, False

    # q search — Cliente only (never Proveedor / oc_clientes)
    term = f"%{(q or '').strip()}%"
    normalized = clean_rut(q or "")
    filters = (
        Cliente.nombre.ilike(term)
        | Cliente.rut.ilike(term)
        | Cliente.giro.ilike(term)
        | Cliente.comuna.ilike(term)
        | Cliente.ciudad.ilike(term)
        | Cliente.region.ilike(term)
        | Cliente.pais.ilike(term)
    )
    if normalized:
        filters = filters | _normalized_rut_sql(Cliente.rut).ilike(f"%{normalized.upper()}%")
    rows = (
        base.filter(filters)
        .order_by(Cliente.nombre.asc(), Cliente.id.asc())
        .limit(limit + 1)
        .all()
    )
    truncated = len(rows) > limit
    items = [_public_customer(row, include_finance=include_finance) for row in rows[:limit]]
    return {"items": items, "count": len(items)}, truncated
