"""Precio de lista cuando P_PUBLICO del catálogo está vacío (referencia de ingreso / variante)."""

from __future__ import annotations

from sqlalchemy import func

from app.bodega.models import IngresoDocumentoItem, ProductoVarianteStock


def batch_precio_neto_desde_ingreso_o_variante(sess, codigos: list[str]) -> dict[str, float]:
    """
    Precio neto para listados (buscar catálogo, etc.) si productos.P_PUBLICO es 0/null.
    Prioridad: mayor precio_publico_neto_override en variantes; si no, último precio_venta_neto en ingreso.
    """
    normalized = sorted({(c or "").strip().upper() for c in codigos if (c or "").strip()})
    if not normalized:
        return {}

    out: dict[str, float] = {}

    vrows = (
        sess.query(
            ProductoVarianteStock.codigo_producto,
            func.max(ProductoVarianteStock.precio_publico_neto_override),
        )
        .filter(ProductoVarianteStock.codigo_producto.in_(normalized))
        .filter(ProductoVarianteStock.precio_publico_neto_override.isnot(None))
        .filter(ProductoVarianteStock.precio_publico_neto_override > 0)
        .group_by(ProductoVarianteStock.codigo_producto)
        .all()
    )
    for cod, pv in vrows:
        key = (cod or "").strip().upper()
        if key and pv:
            out[key] = round(float(pv), 2)

    missing = [c for c in normalized if c not in out]
    if not missing:
        return out

    subq = (
        sess.query(
            func.upper(func.trim(IngresoDocumentoItem.codigo_producto)).label("cod"),
            func.max(IngresoDocumentoItem.id).label("max_id"),
        )
        .filter(func.upper(func.trim(IngresoDocumentoItem.codigo_producto)).in_(missing))
        .filter(IngresoDocumentoItem.precio_venta_neto.isnot(None))
        .filter(IngresoDocumentoItem.precio_venta_neto > 0)
        .group_by(func.upper(func.trim(IngresoDocumentoItem.codigo_producto)))
        .subquery()
    )
    irows = (
        sess.query(IngresoDocumentoItem.codigo_producto, IngresoDocumentoItem.precio_venta_neto)
        .join(subq, IngresoDocumentoItem.id == subq.c.max_id)
        .all()
    )
    for cod, pv in irows:
        key = (cod or "").strip().upper()
        if key and pv and key not in out:
            out[key] = round(float(pv), 2)

    return out
