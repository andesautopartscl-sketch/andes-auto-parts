"""Importación masiva de imágenes de producto → Cloudinary + vínculo en BD."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from sqlalchemy import func, or_
from werkzeug.utils import secure_filename

from app.models import Producto, ProductoImagen

logger = logging.getLogger(__name__)

MIME_TO_EXT = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/pjpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/x-png": "png",
}

_MATCH_OEM = "oem"
_MATCH_ALTERNATIVO = "alternativo"
_MATCH_INTERNO = "interno"


def codigo_from_filename(filename: str) -> str:
    """Nombre sin extensión, normalizado (ej. CS4022RC.jpg → CS4022RC)."""
    stem = Path((filename or "").strip()).stem.strip()
    if stem.lower().endswith("_despiece"):
        stem = stem[: -len("_despiece")]
    return stem.upper()


def _split_codigos_alternativos(raw: str | None) -> list[str]:
    if not raw:
        return []
    out: list[str] = []
    for part in re.split(r"[/;,|\n]+", str(raw)):
        t = part.strip()
        if t:
            out.append(t.upper())
    return out


def _token_en_alternativo(codigo_alternativo: str | None, needle: str) -> bool:
    return needle.upper() in _split_codigos_alternativos(codigo_alternativo)


def _sanitize_storage_key(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", (value or "").strip().upper()) or "producto"


def cloudinary_storage_key(producto: Producto | None, fallback_code: str = "") -> str:
    """
    Nombre de archivo en Cloudinary: OEM si existe, si no código interno.
    """
    if producto:
        oem = (producto.codigo_oem or "").strip().upper()
        if oem:
            return _sanitize_storage_key(oem)
        return _sanitize_storage_key(producto.codigo or "")
    return _sanitize_storage_key(fallback_code)


def producto_resolver_payload(producto: Producto, match_type: str) -> dict:
    codigo = (producto.codigo or "").strip().upper()
    oem = (producto.codigo_oem or "").strip().upper()
    display = oem or codigo
    return {
        "found": True,
        "match_type": match_type,
        "codigo": codigo,
        "codigo_interno": codigo,
        "oem": oem,
        "display_codigo": display,
        "descripcion": (producto.descripcion or "")[:120],
        "marca": (producto.marca or "")[:40],
        "cloudinary_name": cloudinary_storage_key(producto),
    }


def find_producto_by_image_code(sess, code: str) -> tuple[Producto | None, str | None]:
    """
    Busca producto activo por código detectado o escrito.
    Orden: 1° codigo_oem → 2° codigo_alternativo → 3° CODIGO interno.
    """
    c = (code or "").strip().upper()
    if not c:
        return None, None
    base = sess.query(Producto).filter(Producto.activo.is_(True))

    p = (
        base.filter(
            Producto.codigo_oem.isnot(None),
            Producto.codigo_oem != "",
            func.upper(func.trim(Producto.codigo_oem)) == c,
        )
        .first()
    )
    if p:
        return p, _MATCH_OEM

    alt_candidates = (
        base.filter(
            Producto.codigo_alternativo.isnot(None),
            Producto.codigo_alternativo != "",
            or_(
                func.upper(func.trim(Producto.codigo_alternativo)) == c,
                Producto.codigo_alternativo.ilike(f"{c},%"),
                Producto.codigo_alternativo.ilike(f"%,{c},%"),
                Producto.codigo_alternativo.ilike(f"%,{c}"),
                Producto.codigo_alternativo.ilike(f"{c};%"),
                Producto.codigo_alternativo.ilike(f"%;{c};%"),
                Producto.codigo_alternativo.ilike(f"%;{c}"),
                Producto.codigo_alternativo.ilike(f"{c}/%"),
                Producto.codigo_alternativo.ilike(f"%/{c}/%"),
                Producto.codigo_alternativo.ilike(f"%/{c}"),
                Producto.codigo_alternativo.ilike(f"{c}|%"),
                Producto.codigo_alternativo.ilike(f"%|{c}|%"),
                Producto.codigo_alternativo.ilike(f"%|{c}"),
            ),
        )
        .limit(20)
        .all()
    )
    for p in alt_candidates:
        if _token_en_alternativo(p.codigo_alternativo, c):
            return p, _MATCH_ALTERNATIVO

    p = base.filter(func.upper(func.trim(Producto.codigo)) == c).first()
    if p:
        return p, _MATCH_INTERNO

    return None, None


def link_cloudinary_url_to_producto(sess, producto: Producto, url: str) -> None:
    """Asigna URL como imagen principal del producto (sin borrar otras en Cloudinary)."""
    url = (url or "").strip()
    if not url:
        return
    codigo = (producto.codigo or "").strip().upper()
    for img in list(producto.imagenes or []):
        img.es_principal = False
    exists = False
    for img in producto.imagenes or []:
        if (img.ruta or "").strip() == url:
            img.es_principal = True
            exists = True
            break
    if not exists:
        sess.add(
            ProductoImagen(
                producto_codigo=codigo,
                ruta=url,
                es_principal=True,
            )
        )
    producto.imagen_url = url


def _producto_search_item(p: Producto, match_type: str) -> dict:
    codigo = (p.codigo or "").strip().upper()
    oem = (p.codigo_oem or "").strip().upper()
    return {
        "codigo": codigo,
        "codigo_interno": codigo,
        "oem": oem,
        "display_codigo": oem or codigo,
        "descripcion": (p.descripcion or "")[:120],
        "marca": (p.marca or "")[:40],
        "match_type": match_type,
    }


def search_productos_for_assign(sess, q: str, *, limit: int = 12) -> list[dict]:
    """Búsqueda para asignación: prioridad OEM → alternativo → código interno."""
    term = (q or "").strip()
    if len(term) < 1:
        return []
    qu = term.upper()
    like = f"%{term}%"
    base = sess.query(Producto).filter(Producto.activo.is_(True))
    rows: list[tuple[Producto, str]] = []
    seen: set[str] = set()

    def add(p: Producto | None, mtype: str) -> None:
        if not p:
            return
        key = (p.codigo or "").upper()
        if not key or key in seen:
            return
        seen.add(key)
        rows.append((p, mtype))

    add(
        base.filter(
            Producto.codigo_oem.isnot(None),
            Producto.codigo_oem != "",
            func.upper(func.trim(Producto.codigo_oem)) == qu,
        ).first(),
        _MATCH_OEM,
    )

    if len(rows) < limit:
        for p in (
            base.filter(
                Producto.codigo_alternativo.isnot(None),
                Producto.codigo_alternativo != "",
                or_(
                    func.upper(func.trim(Producto.codigo_alternativo)) == qu,
                    Producto.codigo_alternativo.ilike(f"%{term}%"),
                ),
            )
            .order_by(Producto.codigo.asc())
            .limit(30)
            .all()
        ):
            if _token_en_alternativo(p.codigo_alternativo, qu):
                add(p, _MATCH_ALTERNATIVO)
            if len(rows) >= limit:
                break

    if len(rows) < limit:
        add(
            base.filter(func.upper(func.trim(Producto.codigo)) == qu).first(),
            _MATCH_INTERNO,
        )

    if len(rows) < limit and len(term) >= 2:
        for p, mtype in (
            (r, _MATCH_OEM)
            for r in base.filter(
                Producto.codigo_oem.isnot(None),
                Producto.codigo_oem.ilike(like),
            )
            .order_by(Producto.codigo.asc())
            .limit(limit)
            .all()
        ):
            add(p, mtype)
            if len(rows) >= limit:
                break
        for p in (
            base.filter(
                Producto.codigo_alternativo.isnot(None),
                Producto.codigo_alternativo.ilike(like),
            )
            .order_by(Producto.codigo.asc())
            .limit(limit)
            .all()
        ):
            if len(rows) >= limit:
                break
            if _token_en_alternativo(p.codigo_alternativo, qu) or qu in (p.codigo_alternativo or "").upper():
                add(p, _MATCH_ALTERNATIVO)
        for p in (
            base.filter(
                or_(
                    Producto.codigo.ilike(like),
                    Producto.descripcion.ilike(like),
                )
            )
            .order_by(Producto.codigo.asc())
            .limit(limit)
            .all()
        ):
            add(p, _MATCH_INTERNO)
            if len(rows) >= limit:
                break

    return [_producto_search_item(p, mt) for p, mt in rows[:limit]]


def ext_from_mime(mime: str | None) -> str:
    m = (mime or "").split(";")[0].strip().lower()
    return MIME_TO_EXT.get(m, "")


def sniff_image_ext_from_stream(file_obj) -> str:
    """Detecta extensión por magic bytes (útil si el archivo no trae nombre ni MIME fiable)."""
    stream = getattr(file_obj, "stream", file_obj)
    read_fn = getattr(stream, "read", None)
    if not read_fn:
        return ""
    pos = stream.tell() if hasattr(stream, "tell") else None
    try:
        head = read_fn(16) or b""
    except Exception:
        return ""
    finally:
        if pos is not None and hasattr(stream, "seek"):
            try:
                stream.seek(pos)
            except Exception:
                pass
    if len(head) >= 3 and head[:3] == b"\xff\xd8\xff":
        return "jpg"
    if len(head) >= 8 and head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if len(head) >= 6 and head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return ""


def resolve_upload_extension(
    file_obj,
    *,
    fallback_filename: str = "",
    allowed_exts: set[str] | None = None,
) -> tuple[str, str]:
    """
    Resuelve extensión y nombre de archivo seguro para subir.
    Imágenes arrastradas desde web suelen llegar sin extensión en el nombre.
    """
    allowed = allowed_exts or {"jpg", "jpeg", "png", "webp", "gif"}
    raw_name = (
        (getattr(file_obj, "filename", None) or "").strip()
        or (fallback_filename or "").strip()
        or "imagen.jpg"
    )
    safe = secure_filename(raw_name) or ""
    ext = ""
    if "." in safe:
        ext = safe.rsplit(".", 1)[-1].lower()
    if ext == "jpeg":
        ext = "jpg"
    if not ext or ext not in allowed:
        ext = ext_from_mime(getattr(file_obj, "content_type", None) or getattr(file_obj, "mimetype", None))
    if not ext or ext not in allowed:
        ext = sniff_image_ext_from_stream(file_obj)
    if not ext or ext not in allowed:
        raise ValueError(
            "Formato de imagen no permitido o no detectable. Use JPG, PNG, WEBP o GIF."
        )
    stem = Path(safe).stem if safe else ""
    if not stem or stem.lower() in {"image", "img", "photo", "download", "blob", "imagen", "imagen_web"}:
        stem = Path((fallback_filename or "imagen").strip()).stem or "imagen"
        stem = re.sub(r"[^a-zA-Z0-9_\-]", "_", stem) or "imagen"
    filename = f"{stem[:120]}.{ext}"
    return ext, filename


def describe_upload_file(file_obj, fallback_filename: str = "") -> dict:
    """Metadatos para logging del archivo recibido."""
    stream = getattr(file_obj, "stream", None)
    size = None
    if stream is not None and hasattr(stream, "seek") and hasattr(stream, "tell"):
        try:
            pos = stream.tell()
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(pos)
        except Exception:
            size = None
    return {
        "type": type(file_obj).__name__,
        "filename": getattr(file_obj, "filename", None),
        "content_type": getattr(file_obj, "content_type", None) or getattr(file_obj, "mimetype", None),
        "fallback_filename": fallback_filename or None,
        "size_bytes": size,
    }


def download_image_from_url(url: str, *, max_bytes: int = 8 * 1024 * 1024) -> tuple[bytes, str, str]:
    """Descarga bytes de imagen desde URL externa. Retorna (raw, ext, filename)."""
    import mimetypes
    import urllib.error
    import urllib.request
    from urllib.parse import unquote, urlparse

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; AndesAutoParts-ERP/1.0)",
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        },
    )
    logger.info("desde-url: descargando %s", url[:200])
    with urllib.request.urlopen(req, timeout=25) as resp:
        raw = resp.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ValueError("Imagen demasiado grande.")
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()

    ext = ext_from_mime(ctype)
    if not ext:
        guess = mimetypes.guess_extension(ctype or "") or ""
        ext = guess.lstrip(".").lower()
        if ext == "jpeg":
            ext = "jpg"
    if not ext:
        class _ByteStream:
            def read(self, n=16):
                return raw[:n]

        ext = sniff_image_ext_from_stream(_ByteStream()) or ""
    if ext not in {"jpg", "jpeg", "png", "webp", "gif"}:
        raise ValueError(f"Formato no permitido (Content-Type: {ctype or 'desconocido'}).")

    path = unquote(urlparse(url).path or "")
    stem = Path(path).stem.strip() if path else "imagen_web"
    if not stem or stem.lower() in {"image", "img", "photo", "download", "blob"}:
        stem = "imagen_web"
    filename = f"{stem[:80]}.{ext}"
    logger.info("desde-url: ok bytes=%s ext=%s filename=%s", len(raw), ext, filename)
    return raw, ext, filename


def resolver_producto_por_codigo(sess, code: str) -> dict:
    """Resuelve código escrito o detectado en nombre de archivo."""
    c = (code or "").strip().upper()
    if not c:
        return {"found": False, "match_type": None, "codigo": ""}
    producto, match_type = find_producto_by_image_code(sess, c)
    if producto and match_type:
        return producto_resolver_payload(producto, match_type)
    return {"found": False, "match_type": None, "codigo": c, "display_codigo": c}
