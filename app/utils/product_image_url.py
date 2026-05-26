"""URLs de imágenes de producto (local estático o Cloudinary)."""
from __future__ import annotations

from flask import url_for

from app.utils.cloudinary_static_map import get_cloudinary_url


def is_remote_image_url(value: str | None) -> bool:
    v = (value or "").strip().lower()
    return v.startswith("http://") or v.startswith("https://")


def product_image_src(value: str | None) -> str:
    """
    Filtro Jinja / helper: URL Cloudinary (mapa o http), o static local.
    Acepta: URL completa, clave en CLOUDINARY_STATIC, ruta relativa (productos_img/, epc_despiece/, productos360/).
    """
    ref = (value or "").strip()
    if not ref:
        return ""
    if is_remote_image_url(ref):
        return ref
    mapped = get_cloudinary_url(ref)
    if mapped:
        return mapped
    if ref.startswith(
        ("productos_img/", "epc_despiece/", "productos360/", "icons/", "img/")
    ):
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
    if r.startswith(
        ("productos_img/", "epc_despiece/", "productos360/", "icons/", "img/")
    ):
        return r
    return f"productos_img/{r}"
