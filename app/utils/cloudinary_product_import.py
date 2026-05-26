"""Importación masiva de imágenes de producto → Cloudinary + vínculo en BD."""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import func, or_

from app.models import Producto, ProductoImagen


def codigo_from_filename(filename: str) -> str:
    """Nombre sin extensión, normalizado (ej. CS4022RC.jpg → CS4022RC)."""
    stem = Path((filename or "").strip()).stem.strip()
    if stem.lower().endswith("_despiece"):
        stem = stem[: -len("_despiece")]
    return stem.upper()


def find_producto_by_image_code(sess, code: str) -> Producto | None:
    """Busca producto activo por CODIGO o codigo_oem exacto."""
    c = (code or "").strip().upper()
    if not c:
        return None
    base = sess.query(Producto).filter(Producto.activo.is_(True))
    p = base.filter(func.upper(func.trim(Producto.codigo)) == c).first()
    if p:
        return p
    return (
        base.filter(
            Producto.codigo_oem.isnot(None),
            func.upper(func.trim(Producto.codigo_oem)) == c,
        )
        .first()
    )


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


def search_productos_for_assign(sess, q: str, *, limit: int = 12) -> list[dict]:
    """Búsqueda liviana para asignación manual."""
    term = (q or "").strip()
    if len(term) < 1:
        return []
    qu = term.upper()
    like = f"%{term}%"
    base = sess.query(Producto).filter(Producto.activo.is_(True))
    rows: list[Producto] = []

    exact = base.filter(func.upper(func.trim(Producto.codigo)) == qu).limit(1).all()
    rows.extend(exact)
    if len(rows) < limit:
        oem = (
            base.filter(
                Producto.codigo_oem.isnot(None),
                func.upper(func.trim(Producto.codigo_oem)) == qu,
            )
            .limit(1)
            .all()
        )
        for p in oem:
            if p not in rows:
                rows.append(p)
    if len(rows) < limit and len(term) >= 2:
        extra = (
            base.filter(
                or_(
                    Producto.codigo.ilike(like),
                    Producto.descripcion.ilike(like),
                    Producto.codigo_oem.ilike(like),
                )
            )
            .order_by(Producto.codigo.asc())
            .limit(limit)
            .all()
        )
        seen = {(p.codigo or "").upper() for p in rows}
        for p in extra:
            k = (p.codigo or "").upper()
            if k not in seen:
                rows.append(p)
                seen.add(k)
            if len(rows) >= limit:
                break

    return [
        {
            "codigo": (p.codigo or "").strip().upper(),
            "descripcion": (p.descripcion or "")[:120],
            "marca": (p.marca or "")[:40],
            "oem": (p.codigo_oem or "")[:40],
        }
        for p in rows[:limit]
    ]


def resolver_producto_por_codigo(sess, code: str) -> dict | None:
    """Retorna datos del producto si el código u OEM coincide exactamente."""
    producto = find_producto_by_image_code(sess, code)
    if not producto:
        return None
    return {
        "codigo": (producto.codigo or "").strip().upper(),
        "descripcion": (producto.descripcion or "")[:120],
        "marca": (producto.marca or "")[:40],
        "oem": (producto.codigo_oem or "")[:40],
    }

