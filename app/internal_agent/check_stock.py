"""READ-ONLY multi-line stock availability check.

Uses ProductoVarianteStock via get_available_stock only.
Never calls deduct_stock_* or any WRITE helper.
"""
from __future__ import annotations

import re
from typing import Any

from app.internal_agent.m2m import FORBIDDEN_BODY_KEYS, InternalAuthError
from app.internal_agent.product import validate_product_codigo

SQL_VALUE_RE = re.compile(
    r"\b(SELECT|INSERT|UPDATE|DELETE|DROP|UNION|ALTER|EXEC|MERGE|TRUNCATE)\b",
    re.IGNORECASE,
)
MAX_ITEMS = 20
MAX_FILTER_LEN = 64
PUBLIC_ITEM_FIELDS = ("codigo", "cantidad", "marca", "bodega", "disponible", "ok")
BLOCKED_FIELDS = frozenset({
    "precio",
    "costo",
    "margen",
    "proveedor",
    "cliente",
    "password",
    "token",
    "email",
    "telefono",
    "direccion",
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
    if SQL_VALUE_RE.search(text):
        raise InternalAuthError("invalid_args", "SQL is not allowed in arguments", status=400)
    return text


def validate_check_stock_args(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise InternalAuthError("invalid_args", "Body must be a JSON object", status=400)
    extra = set(payload.keys()) - {"items"}
    if extra:
        raise InternalAuthError("invalid_args", f"Unknown argument(s): {', '.join(sorted(extra))}", status=400)
    for key in payload:
        if str(key).strip().lower() in FORBIDDEN_BODY_KEYS:
            raise InternalAuthError("invalid_args", f"Forbidden argument '{key}'", status=400)

    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise InternalAuthError("invalid_args", "'items' must be a non-empty list", status=400)
    if len(items) > MAX_ITEMS:
        raise InternalAuthError("invalid_args", "'items' must have at most 20 entries", status=400)

    clean: list[dict[str, Any]] = []
    for idx, row in enumerate(items):
        if not isinstance(row, dict):
            raise InternalAuthError("invalid_args", f"items[{idx}] must be an object", status=400)
        row_extra = set(row.keys()) - {"codigo", "cantidad", "marca", "bodega"}
        if row_extra:
            raise InternalAuthError(
                "invalid_args",
                f"Unknown argument(s) in items[{idx}]: {', '.join(sorted(row_extra))}",
                status=400,
            )
        for key in row:
            if str(key).strip().lower() in FORBIDDEN_BODY_KEYS:
                raise InternalAuthError("invalid_args", f"Forbidden argument '{key}'", status=400)

        codigo = validate_product_codigo(row.get("codigo"))
        cantidad = row.get("cantidad")
        if isinstance(cantidad, bool) or not isinstance(cantidad, int) or cantidad < 1:
            raise InternalAuthError("invalid_args", "Each item.cantidad must be a positive integer", status=400)

        item: dict[str, Any] = {"codigo": codigo, "cantidad": cantidad}
        marca = _clean_filter(row.get("marca"), "marca") if "marca" in row else None
        bodega = _clean_filter(row.get("bodega"), "bodega") if "bodega" in row else None
        if marca:
            item["marca"] = marca.upper()
        if bodega:
            item["bodega"] = bodega
        clean.append(item)
    return clean


def check_public_stock(items: list[dict[str, Any]]) -> dict[str, Any]:
    """READ-ONLY availability snapshot. Never mutates inventory."""
    from app.utils.stock_control import get_available_stock

    results = []
    all_ok = True
    for row in items:
        codigo = row["codigo"]
        cantidad = int(row["cantidad"])
        marca = row.get("marca")
        bodega = row.get("bodega")
        disponible = int(get_available_stock(codigo, marca, bodega) or 0)
        line_ok = disponible >= cantidad
        if not line_ok:
            all_ok = False
        out: dict[str, Any] = {
            "codigo": codigo,
            "cantidad": cantidad,
            "disponible": disponible,
            "ok": line_ok,
        }
        if marca:
            out["marca"] = marca
        if bodega:
            out["bodega"] = bodega
        results.append(out)
    return {"available": all_ok, "items": results}
