"""
Operaciones de variantes de stock (catálogo de nombres + reasignación de filas).

El catálogo (variantes_marcas_catalogo) solo guarda nombres sugeribles.
La asignación a un código ocurre en ingreso / ajuste / etc. vía productos_variantes_stock.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, text

from app.extensions import db
from app.bodega.models import (
    CatalogoVarianteMarca,
    IngresoDocumentoItem,
    MovimientoStock,
    PickingVentaLine,
    ProductoVarianteStock,
)
from app.ventas.models import DocumentoVentaItem, NotaCreditoItem

ORIGEN_DEFAULT = "nacional"
BODEGA_DEFAULT = "Bodega 1"


def normalize_brand(raw: str | None) -> str:
    return (raw or "").strip().upper()


def normalize_bodega(raw: str | None) -> str:
    value = (raw or "").strip()
    return value or BODEGA_DEFAULT


def normalize_origen(raw: str | None) -> str:
    v = (raw or "").strip().lower()
    if v in ("importacion", "importación", "imp"):
        return "importacion"
    return ORIGEN_DEFAULT


def listar_marcas_distintas(limit: int = 800) -> list[str]:
    """Nombres del catálogo + usados en stock (para datalist / sugerencias)."""
    sync_catalogo_from_stock()
    out: list[str] = []
    seen: set[str] = set()
    for m in listar_catalogo_nombres(limit=limit):
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    rows = (
        db.session.query(ProductoVarianteStock.marca)
        .filter(ProductoVarianteStock.marca.isnot(None))
        .filter(func.trim(ProductoVarianteStock.marca) != "")
        .distinct()
        .order_by(func.upper(ProductoVarianteStock.marca).asc())
        .limit(limit)
        .all()
    )
    for r in rows:
        m = normalize_brand(r[0])
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out[:limit]


def listar_catalogo_nombres(limit: int = 800, *, solo_activos: bool = True) -> list[str]:
    q = CatalogoVarianteMarca.query
    if solo_activos:
        q = q.filter_by(activo=True)
    rows = (
        q.order_by(CatalogoVarianteMarca.orden.asc(), func.upper(CatalogoVarianteMarca.nombre).asc())
        .limit(limit)
        .all()
    )
    out: list[str] = []
    seen: set[str] = set()
    for row in rows:
        m = normalize_brand(row.nombre)
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


def sync_catalogo_from_stock() -> None:
    """Asegura que toda marca ya usada en stock exista en el catálogo (no borra nada)."""
    try:
        existing = {
            normalize_brand(r[0])
            for r in db.session.query(CatalogoVarianteMarca.nombre).all()
            if normalize_brand(r[0])
        }
        max_orden = db.session.query(func.max(CatalogoVarianteMarca.orden)).scalar()
        next_ord = int(max_orden if max_orden is not None else -1) + 1
        stock_names = (
            db.session.query(ProductoVarianteStock.marca)
            .filter(ProductoVarianteStock.marca.isnot(None))
            .filter(func.trim(ProductoVarianteStock.marca) != "")
            .distinct()
            .all()
        )
        added = False
        now = datetime.utcnow()
        for r in stock_names:
            m = normalize_brand(r[0])
            if not m or m in existing:
                continue
            db.session.add(
                CatalogoVarianteMarca(
                    nombre=m,
                    activo=True,
                    orden=next_ord,
                    created_at=now,
                    updated_at=now,
                )
            )
            existing.add(m)
            next_ord += 1
            added = True
        if added:
            db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_catalogo_nombre(marca: str) -> None:
    m = normalize_brand(marca)
    if not m:
        return
    row = CatalogoVarianteMarca.query.filter(func.upper(CatalogoVarianteMarca.nombre) == m).first()
    if row is not None:
        if not bool(row.activo):
            row.activo = True
            row.updated_at = datetime.utcnow()
        return
    max_orden = db.session.query(func.max(CatalogoVarianteMarca.orden)).scalar()
    next_ord = int(max_orden if max_orden is not None else -1) + 1
    now = datetime.utcnow()
    db.session.add(
        CatalogoVarianteMarca(nombre=m, activo=True, orden=next_ord, created_at=now, updated_at=now)
    )


def crear_en_catalogo(marca: str) -> str | None:
    """Alta de nombre de variante (sin crear filas de stock ni exigir código)."""
    marca_n = normalize_brand(marca)
    if not marca_n or len(marca_n) > 120:
        return "Indicá una marca/variante válida (máx. 120 caracteres)."
    row = CatalogoVarianteMarca.query.filter(func.upper(CatalogoVarianteMarca.nombre) == marca_n).first()
    if row is not None:
        if bool(row.activo):
            return f"La variante «{marca_n}» ya existe en el catálogo."
        row.activo = True
        row.updated_at = datetime.utcnow()
        try:
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            return f"No se pudo reactivar: {exc}"
        return None
    _ensure_catalogo_nombre(marca_n)
    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        return f"No se pudo crear: {exc}"
    return None


def editar_marca_catalogo(marca_origen: str, marca_destino: str) -> str | None:
    """
    Renombra en catálogo. Si hay stock/filas con el nombre origen, reasigna
    (mismo comportamiento seguro que reasignar_marca_completa).
    """
    old = normalize_brand(marca_origen)
    new = normalize_brand(marca_destino)
    if not old:
        return "Indicá la marca de origen."
    if not new:
        return "Indicá el nuevo nombre."
    if old == new:
        return "El nuevo nombre debe ser distinto."
    if len(new) > 120:
        return "Nombre destino demasiado largo (máx. 120)."

    usage = (
        ProductoVarianteStock.query.filter(func.upper(func.trim(ProductoVarianteStock.marca)) == old).count()
    )
    if usage > 0:
        err = reasignar_marca_completa(old, new)
        if err:
            return err

    _ensure_catalogo_nombre(new)

    # Reconsultar tras posibles commits de reasignación de stock.
    src_row = CatalogoVarianteMarca.query.filter(func.upper(CatalogoVarianteMarca.nombre) == old).first()
    live_dest = CatalogoVarianteMarca.query.filter(func.upper(CatalogoVarianteMarca.nombre) == new).first()
    if src_row is not None:
        if live_dest is not None and live_dest.id != src_row.id:
            db.session.delete(src_row)
        else:
            src_row.nombre = new
            src_row.updated_at = datetime.utcnow()

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        return f"No se pudo editar el catálogo: {exc}"
    return None


def eliminar_marca_catalogo(marca: str, marca_destino: str | None = None) -> str | None:
    """
    Quita el nombre del catálogo. Si hay filas/stock, exige destino y reasigna antes.
    No toca otras marcas ni el flujo de ingreso.
    """
    old = normalize_brand(marca)
    if not old:
        return "Indicá la marca a eliminar."
    destino = normalize_brand(marca_destino)
    usage = (
        ProductoVarianteStock.query.filter(func.upper(func.trim(ProductoVarianteStock.marca)) == old).count()
    )
    if usage > 0:
        if not destino:
            return (
                f"«{old}» está asignada en {usage} fila(s) de stock. "
                "Indicá una marca destino para reasignar antes de eliminar."
            )
        err = reasignar_marca_completa(old, destino)
        if err:
            return err
        _ensure_catalogo_nombre(destino)

    row = CatalogoVarianteMarca.query.filter(func.upper(CatalogoVarianteMarca.nombre) == old).first()
    if row is not None:
        db.session.delete(row)
        try:
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            return f"No se pudo eliminar del catálogo: {exc}"
    elif usage == 0:
        return f"«{old}» no está en el catálogo."
    return None


def conteos_uso_por_marca() -> dict[str, dict[str, int]]:
    rows = (
        db.session.query(
            ProductoVarianteStock.marca.label("marca"),
            func.count(ProductoVarianteStock.id).label("filas"),
            func.count(func.distinct(ProductoVarianteStock.codigo_producto)).label("productos"),
            func.count(func.distinct(ProductoVarianteStock.bodega)).label("bodegas"),
            func.coalesce(func.sum(ProductoVarianteStock.stock), 0).label("stock_total"),
        )
        .group_by(ProductoVarianteStock.marca)
        .all()
    )
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        m = normalize_brand(r.marca)
        if not m:
            continue
        out[m] = {
            "filas": int(r.filas or 0),
            "productos": int(r.productos or 0),
            "bodegas": int(r.bodegas or 0),
            "stock_total": int(r.stock_total or 0),
        }
    return out


def _producto_existe(codigo: str) -> bool:
    row = db.session.execute(
        text(
            """
            SELECT 1 FROM productos
            WHERE UPPER(TRIM(CODIGO)) = :c AND COALESCE(ACTIVO, 1) = 1
            LIMIT 1
            """
        ),
        {"c": codigo},
    ).first()
    return bool(row)


def _sincronizar_stock_base(codigo: str) -> None:
    code = (codigo or "").strip().upper()
    if not code:
        return
    total = (
        db.session.query(func.coalesce(func.sum(ProductoVarianteStock.stock), 0))
        .filter(func.upper(ProductoVarianteStock.codigo_producto) == code)
        .scalar()
    )
    try:
        db.session.execute(
            text(
                """
                UPDATE productos
                SET STOCK = :stock
                WHERE UPPER(TRIM(CODIGO)) = :c
                """
            ),
            {"stock": int(total or 0), "c": code},
        )
    except Exception:
        pass


def _propagar_marca_historial(codigo: str, marca_old: str, marca_new: str, *, bodega: str | None = None) -> None:
    code = (codigo or "").strip().upper()
    old = normalize_brand(marca_old)
    new = normalize_brand(marca_new)
    if not code or not old or not new or old == new:
        return

    def _apply(model, codigo_col, marca_col, bodega_col=None):
        q = (
            db.session.query(model)
            .filter(func.upper(codigo_col) == code)
            .filter(func.upper(func.trim(marca_col)) == old)
        )
        if bodega and bodega_col is not None:
            q = q.filter(func.trim(bodega_col) == bodega)
        for row in q.all():
            setattr(row, marca_col.key, new)

    _apply(MovimientoStock, MovimientoStock.codigo_producto, MovimientoStock.marca, MovimientoStock.bodega)
    _apply(IngresoDocumentoItem, IngresoDocumentoItem.codigo_producto, IngresoDocumentoItem.marca, IngresoDocumentoItem.bodega)
    _apply(DocumentoVentaItem, DocumentoVentaItem.codigo_producto, DocumentoVentaItem.marca, DocumentoVentaItem.bodega)
    _apply(NotaCreditoItem, NotaCreditoItem.codigo_producto, NotaCreditoItem.marca, NotaCreditoItem.bodega)
    _apply(PickingVentaLine, PickingVentaLine.codigo_producto, PickingVentaLine.marca, PickingVentaLine.bodega)


def crear_variante(
    codigo: str,
    marca: str,
    bodega: str | None = None,
    origen_compra: str | None = None,
) -> str | None:
    """
    Legacy: crea fila de variante con stock 0 ligada a un código.
    Preferir crear_en_catalogo + asignación en ingreso.
    """
    code = (codigo or "").strip().upper()
    marca_n = normalize_brand(marca)
    bodega_n = normalize_bodega(bodega)
    origen = normalize_origen(origen_compra)
    if not code:
        return "Indicá el código interno del producto."
    if not marca_n or len(marca_n) > 120:
        return "Indicá una marca/variante válida (máx. 120 caracteres)."
    if not _producto_existe(code):
        return f"El producto «{code}» no existe o está inactivo."
    exists = (
        ProductoVarianteStock.query.filter_by(
            codigo_producto=code,
            marca=marca_n,
            bodega=bodega_n,
            origen_compra=origen,
        ).first()
    )
    if exists is not None:
        return f"Ya existe la variante {code} / {marca_n} en «{bodega_n}» ({origen})."
    db.session.add(
        ProductoVarianteStock(
            codigo_producto=code,
            marca=marca_n,
            bodega=bodega_n,
            origen_compra=origen,
            stock=0,
        )
    )
    _ensure_catalogo_nombre(marca_n)
    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        return f"No se pudo crear: {exc}"
    return None


def reasignar_variante_fila(
    variante_id: int,
    marca_destino: str,
    *,
    eliminar_origen: bool = True,
) -> str | None:
    """
    Mueve stock de la fila a otra marca (mismo código/bodega/origen).
    Actualiza historial del código y elimina (o deja en 0) la fila origen.
    """
    src = db.session.get(ProductoVarianteStock, int(variante_id))
    if src is None:
        return "Variante no encontrada."
    marca_new = normalize_brand(marca_destino)
    marca_old = normalize_brand(src.marca)
    if not marca_new:
        return "Indicá la marca destino."
    if marca_new == marca_old:
        return "La marca destino debe ser distinta a la actual."

    code = (src.codigo_producto or "").strip().upper()
    bodega = normalize_bodega(src.bodega)
    origen = normalize_origen(getattr(src, "origen_compra", None))
    stock_src = int(src.stock or 0)

    dest = (
        ProductoVarianteStock.query.filter_by(
            codigo_producto=code,
            marca=marca_new,
            bodega=bodega,
            origen_compra=origen,
        ).first()
    )
    if dest is None:
        dest = ProductoVarianteStock(
            codigo_producto=code,
            marca=marca_new,
            bodega=bodega,
            origen_compra=origen,
            stock=stock_src,
            margen_override_pct=src.margen_override_pct,
            precio_publico_neto_override=src.precio_publico_neto_override,
            proveedor=src.proveedor,
        )
        db.session.add(dest)
        db.session.flush()
    else:
        dest.stock = int(dest.stock or 0) + stock_src
        if dest.margen_override_pct is None and src.margen_override_pct is not None:
            dest.margen_override_pct = src.margen_override_pct
        if dest.precio_publico_neto_override is None and src.precio_publico_neto_override is not None:
            dest.precio_publico_neto_override = src.precio_publico_neto_override

    _propagar_marca_historial(code, marca_old, marca_new, bodega=bodega)

    if eliminar_origen:
        db.session.delete(src)
    else:
        src.stock = 0

    _ensure_catalogo_nombre(marca_new)
    _sincronizar_stock_base(code)
    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        return f"No se pudo reasignar: {exc}"
    return None


def eliminar_variante_fila(
    variante_id: int,
    marca_destino: str | None = None,
) -> str | None:
    """
    Elimina una fila. Si tiene stock > 0, exige marca_destino y reasigna.
    Si stock = 0 y hay marca_destino, también propaga historial y borra.
    Si stock = 0 sin destino, solo borra la fila (historial queda intacto).
    """
    src = db.session.get(ProductoVarianteStock, int(variante_id))
    if src is None:
        return "Variante no encontrada."
    stock_src = int(src.stock or 0)
    destino = normalize_brand(marca_destino)
    if stock_src > 0 and not destino:
        return (
            f"La variante {src.codigo_producto} / {src.marca} tiene stock {stock_src}. "
            "Indicá una marca destino para reasignar antes de eliminar."
        )
    if destino:
        return reasignar_variante_fila(variante_id, destino, eliminar_origen=True)

    code = (src.codigo_producto or "").strip().upper()
    db.session.delete(src)
    _sincronizar_stock_base(code)
    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        return f"No se pudo eliminar: {exc}"
    return None


def reasignar_marca_completa(marca_origen: str, marca_destino: str) -> str | None:
    """Reasigna todas las filas de una marca a otra (útil para tipográficos como AFTER → AFTERMARKET)."""
    old = normalize_brand(marca_origen)
    new = normalize_brand(marca_destino)
    if not old:
        return "Indicá la marca de origen."
    if not new:
        return "Indicá la marca destino."
    if old == new:
        return "Origen y destino deben ser distintos."

    rows = (
        ProductoVarianteStock.query.filter(func.upper(func.trim(ProductoVarianteStock.marca)) == old)
        .order_by(ProductoVarianteStock.id.asc())
        .all()
    )
    if not rows:
        return f"No hay variantes con marca «{old}»."

    for row in list(rows):
        live = db.session.get(ProductoVarianteStock, row.id)
        if live is None:
            continue
        if normalize_brand(live.marca) != old:
            continue
        err = reasignar_variante_fila(live.id, new, eliminar_origen=True)
        if err:
            return err
    _ensure_catalogo_nombre(new)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    return None


def renombrar_variante_fila(variante_id: int, nueva_marca: str) -> str | None:
    """Atajo legacy: editar fila = reasignar a nueva marca y eliminar la fila origen."""
    return reasignar_variante_fila(variante_id, nueva_marca, eliminar_origen=True)
