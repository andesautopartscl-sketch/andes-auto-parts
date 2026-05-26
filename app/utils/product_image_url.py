"""URLs de imágenes de producto (local estático o Cloudinary)."""
from __future__ import annotations

from flask import url_for


def is_remote_image_url(value: str | None) -> bool:
    v = (value or "").strip().lower()
    return v.startswith("http://") or v.startswith("https://")


def product_image_src(value: str | None) -> str:
    """
    Filtro Jinja / helper: si es URL http(s) la devuelve tal cual;
    si no, asume ruta relativa bajo static (p. ej. productos_img/foo.jpg).
    """
    ref = (value or "").strip()
    if not ref:
        return ""
    if is_remote_image_url(ref):
        return ref
    if ref.startswith("productos_img/") or ref.startswith("epc_despiece/"):
        return url_for("static", filename=ref)
    return url_for("static", filename=f"productos_img/{ref}")


def normalize_stored_image_ref(value: str | None) -> str:
    """Valor listo para guardar en BD (URL completa o ruta relativa local)."""
    return (value or "").strip()


def static_filename_from_ref(ref: str | None) -> str | None:
    """Ruta relativa dentro de static/ para borrado de archivo local."""
    r = (ref or "").strip()
    if not r or is_remote_image_url(r):
        return None
    if r.startswith("productos_img/") or r.startswith("epc_despiece/"):
        return r
    return f"productos_img/{r}"
