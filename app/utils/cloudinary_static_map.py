"""Mapa nombre estático → URL Cloudinary (UI: icons/, img/, logos)."""
from __future__ import annotations

from flask import has_request_context, url_for


def _normalize_key(filename: str) -> str:
    return (filename or "").strip().replace("\\", "/").lstrip("/")


def get_cloudinary_static() -> dict[str, str]:
    try:
        from app.utils import cloudinary_static_urls

        data = getattr(cloudinary_static_urls, "CLOUDINARY_STATIC", None)
        if isinstance(data, dict):
            return data
    except ImportError:
        pass
    return {}


def get_cloudinary_url(filename: str) -> str | None:
    key = _normalize_key(filename)
    if not key:
        return None
    url = get_cloudinary_static().get(key)
    return (url or "").strip() or None


def keys_with_prefix(prefix: str) -> list[str]:
    p = _normalize_key(prefix)
    if not p.endswith("/"):
        p = p + "/"
    return sorted(k for k in get_cloudinary_static() if k.startswith(p))


def static_or_cloud(filename: str) -> str:
    """
    Filtro Jinja: URL Cloudinary si está en CLOUDINARY_STATIC; si no, static local.
    """
    url = get_cloudinary_url(filename)
    if url:
        return url
    if has_request_context():
        return url_for("static", filename=_normalize_key(filename))
    return f"/static/{_normalize_key(filename)}"


def resolve_media_url(filename: str) -> str:
    """Igual que static_or_cloud pero siempre retorna string (productos / despiece / 360)."""
    return static_or_cloud(filename)
