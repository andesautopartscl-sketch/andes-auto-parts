"""Caché en memoria para taxonomía de productos (categorías / subcategorías)."""
from __future__ import annotations

import time
from threading import Lock
from typing import Any, Callable, TypeVar

T = TypeVar("T")

_TTL_SECONDS = 300
_lock = Lock()
_store: dict[str, dict[str, Any]] = {}


def _get_cached(key: str) -> Any | None:
    now = time.time()
    with _lock:
        entry = _store.get(key)
        if not entry:
            return None
        if now - entry["ts"] > _TTL_SECONDS:
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


def invalidate_taxonomia() -> None:
    with _lock:
        _store.pop("taxonomia_productos", None)


def invalidate_all() -> None:
    with _lock:
        _store.clear()
