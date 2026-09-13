"""Catalog search for the Agent Gateway. Reuses ventas._search_products; does not rewrite it."""
from __future__ import annotations

from typing import Any

from app.internal_agent.m2m import FORBIDDEN_BODY_KEYS, InternalAuthError

MAX_Q_LEN = 80
MIN_Q_LEN = 2
MIN_LIMIT = 1
MAX_LIMIT = 25
DEFAULT_LIMIT = 10


def validate_search_args(payload: Any) -> tuple[str, int]:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise InternalAuthError("invalid_args", "Body must be a JSON object", status=400)
    extra = set(payload.keys()) - {"q", "limit"}
    if extra:
        raise InternalAuthError("invalid_args", f"Unknown argument(s): {', '.join(sorted(extra))}", status=400)
    for key in payload:
        if str(key).strip().lower() in FORBIDDEN_BODY_KEYS:
            raise InternalAuthError("invalid_args", f"Forbidden argument '{key}'", status=400)

    q = payload.get("q")
    if not isinstance(q, str):
        raise InternalAuthError("invalid_args", "'q' must be a string", status=400)
    q = q.strip()
    if len(q) < MIN_Q_LEN:
        raise InternalAuthError("invalid_args", "'q' is too short", status=400)
    if len(q) > MAX_Q_LEN:
        raise InternalAuthError("invalid_args", "'q' is too long", status=400)

    limit = DEFAULT_LIMIT
    if "limit" in payload:
        raw = payload["limit"]
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise InternalAuthError("invalid_args", "'limit' must be an integer", status=400)
        limit = max(MIN_LIMIT, min(MAX_LIMIT, raw))
    return q, limit


ALLOWED_ITEM_FIELDS = ("codigo", "descripcion", "marca", "modelo")
BLOCKED_ITEM_FIELDS = frozenset({
    "precio",
    "costo",
    "margen",
    "stock",
    "variant_stock",
    "rut",
    "email",
    "correo",
    "telefono",
    "token",
    "password",
    "secret",
    "authorization",
})


def _public_item(row: dict[str, Any]) -> dict[str, str]:
    return {
        "codigo": str(row.get("codigo") or "").strip().upper(),
        "descripcion": str(row.get("descripcion") or "").strip(),
        "marca": str(row.get("marca") or "").strip(),
        "modelo": str(row.get("modelo") or "").strip(),
    }


def _fetch_product_rows(q: str, limit: int) -> list[Any]:
    from app.ventas.routes import _search_products

    return _search_products(q, limit=limit) or []


def search_catalog_page(q: str, limit: int) -> tuple[list[dict[str, str]], bool]:
    """Return at most `limit` public items and whether the list was cut."""
    probe = min(100, max(limit + 1, 1))
    rows = _fetch_product_rows(q, probe)
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = _public_item(row)
        code = item["codigo"]
        if not code or code in seen:
            continue
        seen.add(code)
        items.append(item)
        if len(items) > limit:
            break
    truncated = len(items) > limit
    return items[:limit], truncated


def search_catalog_items(q: str, limit: int) -> list[dict[str, str]]:
    items, _truncated = search_catalog_page(q, limit)
    return items
