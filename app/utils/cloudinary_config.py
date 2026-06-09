"""Integración Cloudinary para imágenes de productos."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, BinaryIO

_configured = False


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def is_configured() -> bool:
    return bool(_env("CLOUDINARY_CLOUD_NAME") and _env("CLOUDINARY_API_KEY") and _env("CLOUDINARY_API_SECRET"))


def _ensure_configured() -> None:
    global _configured
    if not is_configured():
        raise RuntimeError("Cloudinary no está configurado (variables CLOUDINARY_*).")
    if _configured:
        return
    import cloudinary

    cloudinary.config(
        cloud_name=_env("CLOUDINARY_CLOUD_NAME"),
        api_key=_env("CLOUDINARY_API_KEY"),
        api_secret=_env("CLOUDINARY_API_SECRET"),
        secure=True,
    )
    _configured = True


def _sanitize_public_id(value: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_\-]", "_", (value or "").strip())
    return safe.strip("_") or "imagen"


def upload_image(
    file: str | Path | BinaryIO,
    folder: str = "andes_erp/productos",
    public_id: str | None = None,
) -> dict[str, str]:
    """
    Sube una imagen y retorna secure_url y public_id.
    file: ruta en disco o objeto file-like (p. ej. FileStorage).
    """
    _ensure_configured()
    import cloudinary.uploader

    opts: dict[str, Any] = {
        "resource_type": "image",
        "overwrite": True,
    }
    if public_id:
        opts["public_id"] = public_id if "/" in public_id else f"{folder}/{_sanitize_public_id(public_id)}"
    else:
        opts["folder"] = folder

    if isinstance(file, (str, Path)):
        result = cloudinary.uploader.upload(str(file), **opts)
    else:
        result = cloudinary.uploader.upload(file, **opts)

    return {
        "url": result.get("secure_url") or result.get("url") or "",
        "public_id": result.get("public_id") or "",
    }


def delete_image(public_id: str) -> bool:
    """Elimina un recurso por public_id. Retorna True si Cloudinary respondió OK."""
    pid = (public_id or "").strip()
    if not pid or not is_configured():
        return False
    _ensure_configured()
    import cloudinary.uploader

    try:
        resp = cloudinary.uploader.destroy(pid, resource_type="image")
        return (resp or {}).get("result") in {"ok", "not found"}
    except Exception:
        return False


def public_id_from_url(url: str) -> str | None:
    """Extrae public_id desde una URL de entrega de Cloudinary."""
    u = (url or "").strip()
    if "cloudinary.com" not in u or "/upload/" not in u:
        return None
    tail = u.split("/upload/", 1)[1]
    parts = tail.split("/")
    if parts and parts[0].startswith("v") and len(parts[0]) > 1 and parts[0][1:].isdigit():
        parts = parts[1:]
    if not parts:
        return None
    path_no_ext = ".".join("/".join(parts).split(".")[:-1])
    return path_no_ext or None


def image_ref_dedupe_key(ref: str) -> str:
    """Clave estable para deduplicar referencias (ignora versión vNNN en Cloudinary)."""
    pid = public_id_from_url(ref)
    return (pid or (ref or "").strip()).lower()


def same_image_ref(a: str, b: str) -> bool:
    """True si dos refs apuntan al mismo asset (misma URL o mismo public_id)."""
    aa = (a or "").strip()
    bb = (b or "").strip()
    if not aa or not bb:
        return False
    if aa == bb:
        return True
    ka = image_ref_dedupe_key(aa)
    kb = image_ref_dedupe_key(bb)
    return bool(ka and kb and ka == kb)


def delete_image_by_url(url: str) -> bool:
    pid = public_id_from_url(url)
    if not pid:
        return False
    return delete_image(pid)
