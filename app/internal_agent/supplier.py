"""Read supplier directory from ventas_proveedores (Proveedor only).

Does NOT query ventas_clientes, oc_clientes, saldos, documents or ingresos.
PII (email, telefono, direccion) is never returned.
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import func, or_

from app.internal_agent.m2m import FORBIDDEN_BODY_KEYS, InternalAuthError
from app.utils.rut_utils import clean_rut, display_tax_id, normalize_foreign_tax_id

SQL_VALUE_RE = re.compile(
    r"\b(SELECT|INSERT|UPDATE|DELETE|DROP|UNION|ALTER|EXEC|MERGE|TRUNCATE)\b",
    re.IGNORECASE,
)
MIN_LIMIT = 1
MAX_LIMIT = 20
DEFAULT_LIMIT = 20
MAX_Q_LEN = 80
MAX_RUT_LEN = 40
BLOCKED_SUPPLIER_FIELDS = frozenset({
    "password",
    "token",
    "email",
    "telefono",
    "direccion",
    "created_at",
    "saldo_favor",
    "saldo",
})
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


def _normalize_tax_key(raw: str) -> str:
    chilean = clean_rut(raw)
    if chilean:
        return chilean.upper()
    foreign = normalize_foreign_tax_id(raw)
    return foreign


def validate_supplier_args(payload: Any) -> dict[str, Any]:
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
    rut = _normalize_tax_key(rut_raw) if rut_raw else None
    if rut_raw and not rut:
        raise InternalAuthError("invalid_args", "'rut' is invalid", status=400)

    supplier_id = None
    if "id" in payload:
        raw_id = payload["id"]
        if isinstance(raw_id, bool) or not isinstance(raw_id, int) or raw_id < 1:
            raise InternalAuthError("invalid_args", "'id' must be a positive integer", status=400)
        supplier_id = raw_id

    if not q and not rut and supplier_id is None:
        raise InternalAuthError("invalid_args", "Provide q, rut or id", status=400)

    limit = DEFAULT_LIMIT
    if "limit" in payload:
        raw = payload["limit"]
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise InternalAuthError("invalid_args", "'limit' must be an integer", status=400)
        if raw < MIN_LIMIT or raw > MAX_LIMIT:
            raise InternalAuthError("invalid_args", "'limit' must be between 1 and 20", status=400)
        limit = raw

    return {"q": q, "rut": rut, "rut_raw": rut_raw, "id": supplier_id, "limit": limit}


def _public_supplier(row: Any) -> dict[str, Any]:
    return {
        "id": int(getattr(row, "id", 0) or 0),
        "nombre": str(getattr(row, "nombre", "") or "").strip(),
        "empresa": str(getattr(row, "empresa", "") or "").strip(),
        "rut": display_tax_id(getattr(row, "rut", "") or ""),
        "giro": str(getattr(row, "giro", "") or "").strip(),
        "comuna": str(getattr(row, "comuna", "") or "").strip(),
        "ciudad": str(getattr(row, "ciudad", "") or "").strip(),
        "region": str(getattr(row, "region", "") or "").strip(),
        "pais": str(getattr(row, "pais", "") or "").strip() or "Chile",
        "activo": bool(getattr(row, "activo", True)),
    }


def get_public_suppliers(
    *,
    q: str | None = None,
    rut: str | None = None,
    rut_raw: str | None = None,
    supplier_id: int | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[dict[str, Any], bool] | None:
    from app.ventas.models import Proveedor

    limit = max(MIN_LIMIT, min(MAX_LIMIT, int(limit or DEFAULT_LIMIT)))
    base = Proveedor.query.filter(Proveedor.activo.is_(True))

    if supplier_id is not None:
        row = base.filter(Proveedor.id == supplier_id).first()
        if row is None:
            return None
        return {"items": [_public_supplier(row)], "count": 1}, False

    if rut:
        clauses = [_normalized_rut_sql(Proveedor.rut) == rut.upper()]
        if rut_raw:
            clauses.append(func.upper(func.trim(Proveedor.rut)) == rut_raw.strip().upper())
        row = base.filter(or_(*clauses)).first()
        if row is None:
            return None
        return {"items": [_public_supplier(row)], "count": 1}, False

    # q search — Proveedor only (never Cliente / oc_clientes)
    term = f"%{(q or '').strip()}%"
    normalized = clean_rut(q or "") or normalize_foreign_tax_id(q or "")
    filters = (
        Proveedor.nombre.ilike(term)
        | Proveedor.empresa.ilike(term)
        | Proveedor.rut.ilike(term)
        | Proveedor.giro.ilike(term)
        | Proveedor.comuna.ilike(term)
        | Proveedor.ciudad.ilike(term)
        | Proveedor.region.ilike(term)
        | Proveedor.pais.ilike(term)
    )
    if normalized:
        filters = filters | _normalized_rut_sql(Proveedor.rut).ilike(f"%{normalized.upper()}%")
    rows = (
        base.filter(filters)
        .order_by(Proveedor.empresa.asc(), Proveedor.nombre.asc(), Proveedor.id.asc())
        .limit(limit + 1)
        .all()
    )
    truncated = len(rows) > limit
    items = [_public_supplier(row) for row in rows[:limit]]
    return {"items": items, "count": len(items)}, truncated
