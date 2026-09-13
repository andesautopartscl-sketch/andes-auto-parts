"""Read a single catalog product. Reuses productos._find_producto_by_codigo."""
from __future__ import annotations

import re
from typing import Any

from flask import Request

from app.internal_agent.m2m import FORBIDDEN_BODY_KEYS, InternalAuthError

CODE_RE = re.compile(r"^[A-Z0-9._-]{1,64}$")
SQL_VALUE_RE = re.compile(
    r"\b(SELECT|INSERT|UPDATE|DELETE|DROP|UNION|ALTER|EXEC|MERGE|TRUNCATE)\b",
    re.IGNORECASE,
)
MAX_CODIGO_LEN = 64
ALLOWED_PRODUCT_FIELDS = (
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
BLOCKED_PRODUCT_FIELDS = frozenset({
    "precio",
    "costo",
    "margen",
    "p_publico",
    "prec_mayor",
    "stock",
    "stock_10jul",
    "factura_proveedor",
    "password",
    "token",
    "rut",
    "email",
    "telefono",
})


def reject_extra_product_request(request: Request) -> None:
    if request.args:
        keys = sorted(str(key) for key in request.args.keys())
        for key in keys:
            if key.strip().lower() in FORBIDDEN_BODY_KEYS:
                raise InternalAuthError("invalid_args", f"Forbidden argument '{key}'", status=400)
        raise InternalAuthError("invalid_args", f"Unknown argument(s): {', '.join(keys)}", status=400)
    payload = request.get_json(silent=True)
    if payload in (None, {}):
        return
    if not isinstance(payload, dict):
        raise InternalAuthError("invalid_args", "Request body is not allowed", status=400)
    extra = sorted(str(key) for key in payload.keys())
    for key in extra:
        if key.strip().lower() in FORBIDDEN_BODY_KEYS:
            raise InternalAuthError("invalid_args", f"Forbidden argument '{key}'", status=400)
    raise InternalAuthError("invalid_args", f"Unknown argument(s): {', '.join(extra)}", status=400)


def validate_product_codigo(raw: Any) -> str:
    if raw is None or not isinstance(raw, str):
        raise InternalAuthError("invalid_args", "'codigo' must be a string", status=400)
    code = raw.strip().upper()
    if len(code) < 1:
        raise InternalAuthError("invalid_args", "'codigo' is required", status=400)
    if len(code) > MAX_CODIGO_LEN:
        raise InternalAuthError("invalid_args", "'codigo' is too long", status=400)
    if SQL_VALUE_RE.search(code) or any(part in FORBIDDEN_BODY_KEYS for part in re.split(r"[\s,;/|]+", code.lower())):
        raise InternalAuthError("invalid_args", "SQL is not allowed in arguments", status=400)
    if not CODE_RE.match(code):
        raise InternalAuthError("invalid_args", "Invalid product code", status=400)
    return code


def _rel_name(rel: Any) -> str:
    if rel is None:
        return ""
    return str(getattr(rel, "nombre", "") or "").strip()


def project_product(producto: Any) -> dict[str, Any]:
    activo_raw = getattr(producto, "activo", True)
    activo = True if activo_raw is None else bool(activo_raw)
    data = {
        "codigo": str(getattr(producto, "codigo", "") or "").strip().upper(),
        "descripcion": str(getattr(producto, "descripcion", "") or "").strip(),
        "marca": str(getattr(producto, "marca", "") or "").strip(),
        "modelo": str(getattr(producto, "modelo", "") or "").strip(),
        "motor": str(getattr(producto, "motor", "") or "").strip(),
        "anio": str(getattr(producto, "anio", "") or "").strip(),
        "activo": activo,
        "categoria": _rel_name(getattr(producto, "categoria_rel", None)),
        "subcategoria": _rel_name(getattr(producto, "subcategoria_rel", None)),
    }
    return {key: data[key] for key in ALLOWED_PRODUCT_FIELDS}


def get_public_product(codigo: str) -> dict[str, Any] | None:
    from app.models import SessionDB
    from app.productos.routes import _find_producto_by_codigo

    sess = SessionDB()
    try:
        producto = _find_producto_by_codigo(sess, codigo)
        if producto is None:
            return None
        return project_product(producto)
    finally:
        sess.close()
