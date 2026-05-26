"""
Catálogo de bodegas ERP: nombres mostrados en formularios y renombre seguro en tablas relacionadas.
"""

from __future__ import annotations

import re

from sqlalchemy import func

from app.extensions import db
from app.bodega.models import (
    CatalogoBodega,
    IngresoDocumentoItem,
    MovimientoStock,
    ProductoVarianteStock,
)
from app.inventario.models import TransferenciaStock
from app.ventas.models import DocumentoVentaItem, NotaCreditoItem


def _sort_key(name: str) -> tuple:
    m = re.match(r"^Bodega\s+(\d+)$", name, re.I)
    if m:
        return (0, int(m.group(1)))
    return (1, (name or "").lower())


def _distinct_bodegas_from_stock() -> list[str]:
    rows = db.session.query(ProductoVarianteStock.bodega).distinct().all()
    out = sorted({(r[0] or "").strip() for r in rows if (r[0] or "").strip()}, key=_sort_key)
    return out


def seed_catalogo_if_empty() -> None:
    if db.session.query(CatalogoBodega.id).limit(1).first():
        return
    names: set[str] = {f"Bodega {i}" for i in range(1, 6)}
    names.update(_distinct_bodegas_from_stock())
    ordered = sorted(names, key=_sort_key)
    for idx, nombre in enumerate(ordered):
        db.session.add(CatalogoBodega(nombre=nombre, activo=True, orden=idx))
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()


def sync_new_warehouses_into_catalogo() -> None:
    existing = {r[0] for r in db.session.query(CatalogoBodega.nombre).all()}
    max_orden = db.session.query(func.max(CatalogoBodega.orden)).scalar()
    next_ord = int(max_orden if max_orden is not None else -1) + 1
    added = False
    for n in _distinct_bodegas_from_stock():
        if n not in existing:
            db.session.add(CatalogoBodega(nombre=n, activo=True, orden=next_ord))
            existing.add(n)
            next_ord += 1
            added = True
    if added:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()


def list_bodegas_operativas() -> list[str]:
    """Lista para selects: orden del catálogo (activas), luego bodegas con stock aunque estén inactivas, luego Bodega 1–5 por defecto."""
    seed_catalogo_if_empty()
    sync_new_warehouses_into_catalogo()
    out: list[str] = []
    seen: set[str] = set()
    for row in (
        CatalogoBodega.query.filter_by(activo=True)
        .order_by(CatalogoBodega.orden.asc(), CatalogoBodega.nombre.asc())
        .all()
    ):
        n = (row.nombre or "").strip()
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    for n in _distinct_bodegas_from_stock():
        if n not in seen:
            seen.add(n)
            out.append(n)
    for i in range(1, 6):
        d = f"Bodega {i}"
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def conteos_variantes_por_bodega() -> dict[str, int]:
    rows = (
        db.session.query(ProductoVarianteStock.bodega, func.count(ProductoVarianteStock.id))
        .group_by(ProductoVarianteStock.bodega)
        .all()
    )
    return {((b or "").strip()): int(c or 0) for b, c in rows if (b or "").strip()}


def _variant_rename_conflict_msg(old: str, new: str) -> str | None:
    pairs = (
        db.session.query(ProductoVarianteStock.codigo_producto, ProductoVarianteStock.marca)
        .filter_by(bodega=old)
        .distinct()
        .all()
    )
    for codigo, marca in pairs:
        if ProductoVarianteStock.query.filter_by(
            codigo_producto=codigo, marca=marca, bodega=new
        ).first():
            return (
                f"No se puede renombrar: ya existe la variante {codigo} / {marca} en «{new}». "
                "Unifica stock manualmente o elige otro nombre."
            )
    return None


def _aplicar_updates_nombre_bodega(old: str, new: str) -> None:
    ProductoVarianteStock.query.filter_by(bodega=old).update(
        {ProductoVarianteStock.bodega: new}, synchronize_session=False
    )
    MovimientoStock.query.filter_by(bodega=old).update(
        {MovimientoStock.bodega: new}, synchronize_session=False
    )
    IngresoDocumentoItem.query.filter_by(bodega=old).update(
        {IngresoDocumentoItem.bodega: new}, synchronize_session=False
    )
    DocumentoVentaItem.query.filter_by(bodega=old).update(
        {DocumentoVentaItem.bodega: new}, synchronize_session=False
    )
    NotaCreditoItem.query.filter_by(bodega=old).update(
        {NotaCreditoItem.bodega: new}, synchronize_session=False
    )
    TransferenciaStock.query.filter_by(bodega_origen=old).update(
        {TransferenciaStock.bodega_origen: new}, synchronize_session=False
    )
    TransferenciaStock.query.filter_by(bodega_destino=old).update(
        {TransferenciaStock.bodega_destino: new}, synchronize_session=False
    )


def actualizar_fila_catalogo(
    row: CatalogoBodega,
    nuevo_nombre: str,
    orden: int,
    activo: bool,
    nota: str,
) -> str | None:
    """
    Actualiza metadatos y, si cambia el nombre, propaga el nuevo texto en todas las tablas ERP.
    Retorna mensaje de error o None si OK.
    """
    old = (row.nombre or "").strip()
    new = (nuevo_nombre or "").strip()
    if not new or len(new) > 120:
        return "El nombre no puede estar vacío ni superar 120 caracteres."
    if new != old:
        other = CatalogoBodega.query.filter_by(nombre=new).first()
        if other is not None and other.id != row.id:
            return f"Ya existe otra bodega con el nombre «{new}»."
        msg = _variant_rename_conflict_msg(old, new)
        if msg:
            return msg
        try:
            _aplicar_updates_nombre_bodega(old, new)
            row.nombre = new
        except Exception as exc:
            db.session.rollback()
            return f"No se pudo renombrar: {exc}"
    row.orden = int(orden)
    row.activo = bool(activo)
    row.nota = (nota or "").strip()[:255]
    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        return f"No se pudo guardar: {exc}"
    return None


def crear_bodega_catalogo(nombre: str, orden: int | None = None) -> str | None:
    n = (nombre or "").strip()
    if not n or len(n) > 120:
        return "Nombre inválido (vacío o demasiado largo)."
    if CatalogoBodega.query.filter_by(nombre=n).first():
        return f"Ya existe la bodega «{n}»."
    max_orden = db.session.query(func.max(CatalogoBodega.orden)).scalar()
    ord_val = int(orden) if orden is not None else int(max_orden if max_orden is not None else -1) + 1
    db.session.add(CatalogoBodega(nombre=n, activo=True, orden=ord_val))
    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        return f"No se pudo crear: {exc}"
    return None
