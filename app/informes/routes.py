from __future__ import annotations

from datetime import date, timedelta

from flask import Blueprint, render_template, request
from sqlalchemy import func

from app.extensions import db
from app.utils.decorators import login_required
from app.ventas.models import DocumentoVenta, DocumentoVentaItem, Cliente, Proveedor
from app.bodega.models import MovimientoStock, ProductoVarianteStock

informes_bp = Blueprint(
    "informes", __name__, url_prefix="/informes",
    template_folder="../../templates"
)

_FACTURA_TIPOS = ("factura", "boleta")


# ──────────────────────────────────────────────────────────────
# INDEX
# ──────────────────────────────────────────────────────────────

@informes_bp.route("/")
@login_required
def index():
    return render_template("informes/index.html", active_page="informes")


# ──────────────────────────────────────────────────────────────
# VENTAS POR PERÍODO
# ──────────────────────────────────────────────────────────────

@informes_bp.route("/ventas-periodo")
@login_required
def ventas_periodo():
    desde_str = request.args.get("desde", "")
    hasta_str = request.args.get("hasta", "")

    today = date.today()
    if not desde_str:
        desde = today.replace(day=1)
        desde_str = str(desde)
    else:
        try:
            desde = date.fromisoformat(desde_str)
        except ValueError:
            desde = today.replace(day=1)
            desde_str = str(desde)

    if not hasta_str:
        hasta = today
        hasta_str = str(hasta)
    else:
        try:
            hasta = date.fromisoformat(hasta_str)
        except ValueError:
            hasta = today
            hasta_str = str(hasta)

    docs = (
        DocumentoVenta.query
        .filter(
            DocumentoVenta.tipo.in_(_FACTURA_TIPOS),
            DocumentoVenta.status != "anulada",
            func.date(DocumentoVenta.fecha_documento) >= desde,
            func.date(DocumentoVenta.fecha_documento) <= hasta,
        )
        .order_by(DocumentoVenta.fecha_documento.desc())
        .all()
    )

    total_neto = sum(float(d.subtotal or 0) for d in docs)
    total_iva = sum(float(d.impuesto or 0) for d in docs)
    total_general = sum(float(d.total or 0) for d in docs)

    return render_template(
        "informes/ventas_periodo.html",
        documentos=docs,
        desde=desde_str,
        hasta=hasta_str,
        total_neto=total_neto,
        total_iva=total_iva,
        total_general=total_general,
        active_page="informes",
    )


# ──────────────────────────────────────────────────────────────
# VENTAS POR CLIENTE
# ──────────────────────────────────────────────────────────────

@informes_bp.route("/ventas-cliente")
@login_required
def ventas_cliente():
    rows = (
        db.session.query(
            DocumentoVenta.cliente_nombre,
            DocumentoVenta.cliente_rut,
            func.count(DocumentoVenta.id).label("num_docs"),
            func.sum(DocumentoVenta.total).label("total_venta"),
        )
        .filter(
            DocumentoVenta.tipo.in_(_FACTURA_TIPOS),
            DocumentoVenta.status != "anulada",
        )
        .group_by(DocumentoVenta.cliente_nombre, DocumentoVenta.cliente_rut)
        .order_by(func.sum(DocumentoVenta.total).desc())
        .all()
    )

    clientes = [
        {
            "nombre": r.cliente_nombre or "Sin nombre",
            "rut": r.cliente_rut or "",
            "num_docs": int(r.num_docs or 0),
            "total": float(r.total_venta or 0),
        }
        for r in rows
    ]

    return render_template(
        "informes/ventas_cliente.html",
        clientes=clientes,
        active_page="informes",
    )


# ──────────────────────────────────────────────────────────────
# UTILIDAD / MARGEN
# ──────────────────────────────────────────────────────────────

@informes_bp.route("/utilidad-margen")
@login_required
def utilidad_margen():
    rows = (
        db.session.query(
            DocumentoVentaItem.codigo_producto,
            DocumentoVentaItem.descripcion,
            func.sum(DocumentoVentaItem.cantidad).label("total_qty"),
            func.sum(DocumentoVentaItem.subtotal).label("total_venta"),
        )
        .join(DocumentoVenta, DocumentoVentaItem.documento_id == DocumentoVenta.id)
        .filter(
            DocumentoVenta.tipo.in_(_FACTURA_TIPOS),
            DocumentoVenta.status != "anulada",
        )
        .group_by(DocumentoVentaItem.codigo_producto, DocumentoVentaItem.descripcion)
        .order_by(func.sum(DocumentoVentaItem.subtotal).desc())
        .limit(200)
        .all()
    )

    productos = [
        {
            "codigo": r.codigo_producto,
            "descripcion": r.descripcion or r.codigo_producto,
            "qty": int(r.total_qty or 0),
            "venta": float(r.total_venta or 0),
        }
        for r in rows
    ]

    total_venta = sum(p["venta"] for p in productos)

    return render_template(
        "informes/utilidad_margen.html",
        productos=productos,
        total_venta=total_venta,
        active_page="informes",
    )


# ──────────────────────────────────────────────────────────────
# STOCK CRÍTICO
# ──────────────────────────────────────────────────────────────

@informes_bp.route("/stock-critico")
@login_required
def stock_critico():
    threshold = int(request.args.get("umbral", 3))
    filas = (
        ProductoVarianteStock.query
        .filter(
            ProductoVarianteStock.stock <= threshold,
            ProductoVarianteStock.stock >= 0,
        )
        .order_by(ProductoVarianteStock.stock.asc(), ProductoVarianteStock.codigo_producto.asc())
        .all()
    )
    return render_template(
        "informes/stock_critico.html",
        filas=filas,
        threshold=threshold,
        active_page="informes",
    )


# ──────────────────────────────────────────────────────────────
# PRODUCTOS TOP VENDIDOS
# ──────────────────────────────────────────────────────────────

@informes_bp.route("/productos-top")
@login_required
def productos_top():
    rows = (
        db.session.query(
            DocumentoVentaItem.codigo_producto,
            DocumentoVentaItem.descripcion,
            func.sum(DocumentoVentaItem.cantidad).label("total_qty"),
            func.sum(DocumentoVentaItem.subtotal).label("total_venta"),
        )
        .join(DocumentoVenta, DocumentoVentaItem.documento_id == DocumentoVenta.id)
        .filter(
            DocumentoVenta.tipo.in_(_FACTURA_TIPOS),
            DocumentoVenta.status != "anulada",
        )
        .group_by(DocumentoVentaItem.codigo_producto, DocumentoVentaItem.descripcion)
        .order_by(func.sum(DocumentoVentaItem.cantidad).desc())
        .limit(50)
        .all()
    )

    productos = [
        {
            "codigo": r.codigo_producto,
            "descripcion": r.descripcion or r.codigo_producto,
            "qty": int(r.total_qty or 0),
            "venta": float(r.total_venta or 0),
        }
        for r in rows
    ]

    return render_template(
        "informes/productos_top.html",
        productos=productos,
        active_page="informes",
    )


# ──────────────────────────────────────────────────────────────
# MOVIMIENTOS DE INVENTARIO
# ──────────────────────────────────────────────────────────────

@informes_bp.route("/movimientos-inventario")
@login_required
def movimientos_inventario():
    tipo = request.args.get("tipo", "")
    desde_str = request.args.get("desde", "")
    hasta_str = request.args.get("hasta", "")

    today = date.today()
    if not desde_str:
        desde = today - timedelta(days=29)
        desde_str = str(desde)
    else:
        try:
            desde = date.fromisoformat(desde_str)
        except ValueError:
            desde = today - timedelta(days=29)
            desde_str = str(desde)

    if not hasta_str:
        hasta = today
        hasta_str = str(hasta)
    else:
        try:
            hasta = date.fromisoformat(hasta_str)
        except ValueError:
            hasta = today
            hasta_str = str(hasta)

    q = MovimientoStock.query.filter(
        func.date(MovimientoStock.fecha) >= desde,
        func.date(MovimientoStock.fecha) <= hasta,
    )
    if tipo in ("ingreso", "salida", "ajuste"):
        q = q.filter(MovimientoStock.tipo == tipo)

    movimientos = q.order_by(MovimientoStock.fecha.desc()).limit(300).all()

    return render_template(
        "informes/movimientos_inventario.html",
        movimientos=movimientos,
        desde=desde_str,
        hasta=hasta_str,
        tipo_filter=tipo,
        active_page="informes",
    )


# ──────────────────────────────────────────────────────────────
# COMPRAS POR PROVEEDOR
# ──────────────────────────────────────────────────────────────

@informes_bp.route("/compras-proveedor")
@login_required
def compras_proveedor():
    rows = (
        db.session.query(
            DocumentoVenta.cliente_nombre.label("proveedor_nombre"),
            func.count(DocumentoVenta.id).label("num_docs"),
            func.sum(DocumentoVenta.total).label("total_compra"),
        )
        .filter(
            DocumentoVenta.tipo.in_(["orden_compra"]),
            DocumentoVenta.status != "anulada",
        )
        .group_by(DocumentoVenta.cliente_nombre)
        .order_by(func.sum(DocumentoVenta.total).desc())
        .all()
    )

    proveedores = [
        {
            "nombre": r.proveedor_nombre or "Sin nombre",
            "num_docs": int(r.num_docs or 0),
            "total": float(r.total_compra or 0),
        }
        for r in rows
    ]

    return render_template(
        "informes/compras_proveedor.html",
        proveedores=proveedores,
        active_page="informes",
    )
