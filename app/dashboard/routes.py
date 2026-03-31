from __future__ import annotations

from datetime import date, datetime, timedelta

from flask import Blueprint, render_template, jsonify
from sqlalchemy import func

from app.extensions import db
from app.utils.decorators import login_required
from app.ventas.models import DocumentoVenta, DocumentoVentaItem, Cliente
from app.bodega.models import ProductoVarianteStock

dashboard_bp = Blueprint(
    "dashboard", __name__, url_prefix="/dashboard",
    template_folder="../../templates"
)

_FACTURA_TIPOS = ("factura", "boleta")


def _ventas_periodo(fecha_inicio: date, fecha_fin: date) -> float:
    result = (
        db.session.query(func.sum(DocumentoVenta.total))
        .filter(
            DocumentoVenta.tipo.in_(_FACTURA_TIPOS),
            DocumentoVenta.status != "anulada",
            func.date(DocumentoVenta.fecha_documento) >= fecha_inicio,
            func.date(DocumentoVenta.fecha_documento) <= fecha_fin,
        )
        .scalar()
    )
    return float(result or 0)


def _count_documentos_periodo(fecha_inicio: date, fecha_fin: date) -> int:
    return (
        DocumentoVenta.query
        .filter(
            DocumentoVenta.tipo.in_(_FACTURA_TIPOS),
            DocumentoVenta.status != "anulada",
            func.date(DocumentoVenta.fecha_documento) >= fecha_inicio,
            func.date(DocumentoVenta.fecha_documento) <= fecha_fin,
        )
        .count()
    )


def _top_productos(limit: int = 10) -> list[dict]:
    rows = (
        db.session.query(
            DocumentoVentaItem.codigo_producto,
            DocumentoVentaItem.descripcion,
            func.sum(DocumentoVentaItem.cantidad).label("total_qty"),
            func.sum(DocumentoVentaItem.subtotal).label("total_venta"),
        )
        .join(DocumentoVenta, DocumentoVentaItem.documento_id == DocumentoVenta.id)
        .filter(DocumentoVenta.tipo.in_(_FACTURA_TIPOS), DocumentoVenta.status != "anulada")
        .group_by(DocumentoVentaItem.codigo_producto, DocumentoVentaItem.descripcion)
        .order_by(func.sum(DocumentoVentaItem.subtotal).desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "codigo": r.codigo_producto,
            "descripcion": r.descripcion or r.codigo_producto,
            "qty": int(r.total_qty or 0),
            "venta": float(r.total_venta or 0),
        }
        for r in rows
    ]


def _top_clientes(limit: int = 10) -> list[dict]:
    rows = (
        db.session.query(
            DocumentoVenta.cliente_nombre,
            func.sum(DocumentoVenta.total).label("total_venta"),
            func.count(DocumentoVenta.id).label("num_docs"),
        )
        .filter(DocumentoVenta.tipo.in_(_FACTURA_TIPOS), DocumentoVenta.status != "anulada")
        .group_by(DocumentoVenta.cliente_nombre)
        .order_by(func.sum(DocumentoVenta.total).desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "nombre": r.cliente_nombre or "Sin nombre",
            "total": float(r.total_venta or 0),
            "docs": int(r.num_docs or 0),
        }
        for r in rows
    ]


def _stock_critico(threshold: int = 3, limit: int = 20) -> list[dict]:
    rows = (
        ProductoVarianteStock.query
        .filter(ProductoVarianteStock.stock <= threshold, ProductoVarianteStock.stock >= 0)
        .order_by(ProductoVarianteStock.stock.asc())
        .limit(limit)
        .all()
    )
    return [
        {
            "codigo": r.codigo_producto,
            "marca": r.marca,
            "bodega": r.bodega,
            "stock": r.stock,
        }
        for r in rows
    ]


def _ventas_ultimos_dias(days: int = 30) -> list[dict]:
    today = date.today()
    result = []
    rows = (
        db.session.query(
            func.date(DocumentoVenta.fecha_documento).label("dia"),
            func.sum(DocumentoVenta.total).label("total"),
        )
        .filter(
            DocumentoVenta.tipo.in_(_FACTURA_TIPOS),
            DocumentoVenta.status != "anulada",
            func.date(DocumentoVenta.fecha_documento) >= today - timedelta(days=days - 1),
        )
        .group_by(func.date(DocumentoVenta.fecha_documento))
        .order_by(func.date(DocumentoVenta.fecha_documento))
        .all()
    )
    totals_by_day = {str(r.dia): float(r.total or 0) for r in rows}
    for i in range(days):
        d = today - timedelta(days=days - 1 - i)
        result.append({"dia": str(d), "total": totals_by_day.get(str(d), 0)})
    return result


@dashboard_bp.route("/")
@login_required
def index():
    today = date.today()
    first_of_month = today.replace(day=1)
    ventas_hoy = _ventas_periodo(today, today)
    ventas_mes = _ventas_periodo(first_of_month, today)
    docs_hoy = _count_documentos_periodo(today, today)
    docs_mes = _count_documentos_periodo(first_of_month, today)
    top_productos = _top_productos(10)
    top_clientes = _top_clientes(10)
    stock_critico = _stock_critico(threshold=3, limit=15)
    chart_data = _ventas_ultimos_dias(30)

    return render_template(
        "dashboard/index.html",
        ventas_hoy=ventas_hoy,
        ventas_mes=ventas_mes,
        docs_hoy=docs_hoy,
        docs_mes=docs_mes,
        top_productos=top_productos,
        top_clientes=top_clientes,
        stock_critico=stock_critico,
        chart_data=chart_data,
        active_page="dashboard",
    )


@dashboard_bp.route("/api/data")
@login_required
def api_data():
    today = date.today()
    first_of_month = today.replace(day=1)
    return jsonify({
        "ventas_hoy": _ventas_periodo(today, today),
        "ventas_mes": _ventas_periodo(first_of_month, today),
        "top_productos": _top_productos(5),
        "top_clientes": _top_clientes(5),
        "stock_critico": _stock_critico(3, 10),
        "chart_data": _ventas_ultimos_dias(30),
    })
