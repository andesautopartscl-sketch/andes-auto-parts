"""Caché en memoria para taxonomía de productos (categorías / subcategorías)."""
from __future__ import annotations

import time
from threading import Lock
from typing import Any, Callable, TypeVar

T = TypeVar("T")

_TTL_SECONDS = 300
_lock = Lock()
_store: dict[str, dict[str, Any]] = {}


def _get_cached(key: str, ttl_seconds: int | None = None) -> Any | None:
    ttl = _TTL_SECONDS if ttl_seconds is None else ttl_seconds
    now = time.time()
    with _lock:
        entry = _store.get(key)
        if not entry:
            return None
        if now - entry["ts"] > ttl:
            _store.pop(key, None)
            return None
        return entry["data"]


def _set_cached(key: str, data: Any) -> None:
    with _lock:
        _store[key] = {"ts": time.time(), "data": data}


def get_or_load(key: str, loader: Callable[[], T]) -> T:
    cached = _get_cached(key)
    if cached is not None:
        return cached
    data = loader()
    _set_cached(key, data)
    return data


def get_or_load_ttl(key: str, loader: Callable[[], T], ttl_seconds: int) -> T:
    cached = _get_cached(key, ttl_seconds=ttl_seconds)
    if cached is not None:
        return cached
    data = loader()
    _set_cached(key, data)
    return data


def invalidate_taxonomia() -> None:
    with _lock:
        _store.pop("taxonomia_productos", None)


def invalidate_ficha_despiece(producto_codigo: str) -> None:
    """Limpia caché de despiece en ficha (p. ej. tras subir imagen EPC compartida por OEM)."""
    key = f"ficha_despiece:{(producto_codigo or '').strip().upper()}"
    if not key or key == "ficha_despiece:":
        return
    with _lock:
        _store.pop(key, None)


def invalidate_ficha_despiece_for_oem(sess, oem_norm: str) -> None:
    """Invalida caché de ficha para todos los productos con el mismo OEM."""
    from sqlalchemy import func

    from app.models import Producto

    oem = (oem_norm or "").strip().upper()
    if not oem:
        return
    try:
        rows = (
            sess.query(Producto.codigo)
            .filter(func.upper(func.trim(Producto.codigo_oem)) == oem)
            .all()
        )
        for row in rows:
            codigo = (row[0] if isinstance(row, tuple) else getattr(row, "codigo", "") or "")
            invalidate_ficha_despiece(codigo)
    except Exception:
        pass


def invalidate_all() -> None:
    with _lock:
        _store.clear()
