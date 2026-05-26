"""Importación masiva de imágenes de producto → Cloudinary + vínculo en BD."""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import func, or_

from app.models import Producto, ProductoImagen

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


def resolver_producto_por_codigo(sess, code: str) -> dict:
    """Resuelve código escrito o detectado en nombre de archivo."""
    c = (code or "").strip().upper()
    if not c:
        return {"found": False, "match_type": None, "codigo": ""}
    producto, match_type = find_producto_by_image_code(sess, c)
    if producto and match_type:
        return producto_resolver_payload(producto, match_type)
    return {"found": False, "match_type": None, "codigo": c, "display_codigo": c}
