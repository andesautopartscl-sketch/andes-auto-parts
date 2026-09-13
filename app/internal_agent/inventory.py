"""Read inventory rows from productos_variantes_stock. Reuses stock_control helpers."""
from __future__ import annotations

import re
from typing import Any

from app.internal_agent.m2m import FORBIDDEN_BODY_KEYS, InternalAuthError
from app.internal_agent.product import validate_product_codigo

SQL_VALUE_RE = re.compile(
    r"\b(SELECT|INSERT|UPDATE|DELETE|DROP|UNION|ALTER|EXEC|MERGE|TRUNCATE)\b",
    re.IGNORECASE,
)
MAX_FILTER_LEN = 120
ALLOWED_ITEM_FIELDS = ("marca", "bodega", "origen_compra", "stock")
BLOCKED_INVENTORY_FIELDS = frozenset({
    "precio",
    "costo",
    "margen",
    "margen_override_pct",
    "precio_publico_neto_override",
    "p_publico",
    "prec_mayor",
    "proveedor",
    "proveedor_rut",
    "password",
    "token",
    "rut",
    "email",
    "telefono",
    "id",
    "metadata_json",
})


def _clean_filter(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InternalAuthError("invalid_args", f"'{label}' must be a string", status=400)
    text = value.strip()
    if not text:
        return None
    if len(text) > MAX_FILTER_LEN:
        raise InternalAuthError("invalid_args", f"'{label}' is too long", status=400)
    if SQL_VALUE_RE.search(text) or any(part in FORBIDDEN_BODY_KEYS for part in re.split(r"[\s,;/|]+", text.lower())):
        raise InternalAuthError("invalid_args", "SQL is not allowed in arguments", status=400)
    return text


def validate_inventory_args(payload: Any) -> tuple[str, str | None, str | None]:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise InternalAuthError("invalid_args", "Body must be a JSON object", status=400)
    extra = set(payload.keys()) - {"codigo", "marca", "bodega"}
    if extra:
        raise InternalAuthError("invalid_args", f"Unknown argument(s): {', '.join(sorted(extra))}", status=400)
    for key in payload:
        if str(key).strip().lower() in FORBIDDEN_BODY_KEYS:
            raise InternalAuthError("invalid_args", f"Forbidden argument '{key}'", status=400)
    codigo = validate_product_codigo(payload.get("codigo"))
    marca = _clean_filter(payload.get("marca"), "marca") if "marca" in payload else None
    bodega = _clean_filter(payload.get("bodega"), "bodega") if "bodega" in payload else None
    return codigo, marca, bodega


def _public_item(row: Any) -> dict[str, Any]:
    origen = str(getattr(row, "origen_compra", "") or "").strip() or "nacional"
    return {
        "marca": str(getattr(row, "marca", "") or "").strip(),
        "bodega": str(getattr(row, "bodega", "") or "").strip(),
        "origen_compra": origen,
        "stock": int(getattr(row, "stock", 0) or 0),
    }


def get_public_inventory(codigo: str, marca: str | None = None, bodega: str | None = None) -> dict[str, Any] | None:
    from app.internal_agent.product import get_public_product
    from app.utils.stock_control import _variant_stock_query, get_available_stock

    product = get_public_product(codigo)
    if product is None:
        return None
    rows = _variant_stock_query(codigo, marca, bodega).all()
    items = [_public_item(row) for row in rows]
    items.sort(key=lambda row: (row["bodega"], row["marca"], row["origen_compra"]))
    total = int(get_available_stock(codigo, marca, bodega) or 0)
    return {
        "codigo": product["codigo"],
        "descripcion": product["descripcion"],
        "items": items,
        "total_stock": total,
    }
