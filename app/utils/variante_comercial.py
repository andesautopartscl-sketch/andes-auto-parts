"""Precio/margen por variante: overrides en productos_variantes_stock + referencia de ingresos."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, or_

from app.bodega.models import ProductoVarianteStock


def find_variante_stock(sess, codigo: str, marca: str | None, bodega: str | None) -> ProductoVarianteStock | None:
    code = (codigo or "").strip().upper()
    if not code:
        return None
    bodega_n = (bodega or "").strip() or "Bodega 1"
    marca_n = (marca or "").strip().upper()
    q = (
        sess.query(ProductoVarianteStock)
        .filter(func.upper(ProductoVarianteStock.codigo_producto) == code)
        .filter(ProductoVarianteStock.bodega == bodega_n)
    )
    if marca_n:
        q = q.filter(func.upper(func.trim(ProductoVarianteStock.marca)) == marca_n)
    else:
        q = q.filter(
            or_(
                ProductoVarianteStock.marca.is_(None),
                ProductoVarianteStock.marca == "",
                func.upper(func.trim(ProductoVarianteStock.marca)) == "",
            )
        )
    return q.first()


def merge_ingreso_ref_variante_overrides(
    ref: dict[str, Any] | None,
    override_margen_pct: float | None,
    override_precio_publico_neto: float | None,
) -> dict[str, Any | None]:
    """
    ref: salida tipo _ultimo_ingreso_ref (costo_unitario_neto, precio_sugerido_neto, margen_registrado_pct).
    Overrides en BD (None = no override, usar solo ingreso).
    """
    costo = None
    margen = None
    precio = None
    if ref:
        costo = ref.get("costo_unitario_neto")
        margen = ref.get("margen_registrado_pct")
        precio = ref.get("precio_sugerido_neto")

    om = override_margen_pct
    op = override_precio_publico_neto

    if om is not None:
        margen = float(om)
    if op is not None:
        precio = round(float(op), 2)

    if op is not None and om is None and costo is not None:
        try:
            c = float(costo)
            p = float(precio) if precio is not None else None
            if c > 0 and p is not None and p > 0:
                margen = round((1.0 - c / p) * 100.0, 4)
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    elif om is not None and op is None and costo is not None and margen is not None:
        try:
            c = float(costo)
            m = float(margen)
            if c > 0 and m < 100:
                d = 1.0 - m / 100.0
                if d > 0:
                    precio = round(c / d, 2)
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    return {
        "costo_unitario_neto": costo,
        "precio_sugerido_neto": precio,
        "margen_registrado_pct": margen,
    }
