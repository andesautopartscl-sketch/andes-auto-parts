from __future__ import annotations

import csv
import io
from datetime import date, timedelta

from flask import Blueprint, render_template, request, Response
from sqlalchemy import func

from app.extensions import db
from app.utils.decorators import login_required
from app.ventas.models import DocumentoVenta, DocumentoVentaItem, Cliente, Proveedor
from app.bodega.models import MovimientoStock, ProductoVarianteStock, IngresoDocumentoItem

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
    desde_str = request.args.get("desde", "").strip()
    hasta_str = request.args.get("hasta", "").strip()
    codigo_q = request.args.get("codigo", "").strip().upper()
    origen_q = request.args.get("origen", "").strip().lower()
    limit_str = request.args.get("limit", "200").strip()
    export = request.args.get("export", "").strip().lower()

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

    try:
        limit = max(50, min(1000, int(limit_str or "200")))
    except ValueError:
        limit = 200

    q = (
        db.session.query(
            DocumentoVentaItem.codigo_producto,
            DocumentoVentaItem.descripcion,
            DocumentoVentaItem.origen_compra,
            func.sum(DocumentoVentaItem.cantidad).label("total_qty"),
            func.sum(DocumentoVentaItem.subtotal).label("total_venta"),
        )
        .join(DocumentoVenta, DocumentoVentaItem.documento_id == DocumentoVenta.id)
        .filter(
            DocumentoVenta.tipo.in_(_FACTURA_TIPOS),
            DocumentoVenta.status != "anulada",
            func.date(DocumentoVenta.fecha_documento) >= desde,
            func.date(DocumentoVenta.fecha_documento) <= hasta,
        )
    )

    if codigo_q:
        q = q.filter(DocumentoVentaItem.codigo_producto.ilike(f"%{codigo_q}%"))
    if origen_q in {"nacional", "importacion"}:
        q = q.filter(DocumentoVentaItem.origen_compra == origen_q)

    rows = (
        q
        .group_by(DocumentoVentaItem.codigo_producto, DocumentoVentaItem.descripcion)
        .group_by(DocumentoVentaItem.origen_compra)
        .order_by(func.sum(DocumentoVentaItem.subtotal).desc())
        .limit(limit)
        .all()
    )

    cost_cache: dict[tuple[str, str], float | None] = {}
    productos = []
    for r in rows:
        qty = int(r.total_qty or 0)
        venta = float(r.total_venta or 0)
        codigo = (r.codigo_producto or "").strip()
        origen = (r.origen_compra or "nacional").strip().lower()
        cache_key = (codigo, origen)
        if cache_key not in cost_cache:
            last_ing = (
                IngresoDocumentoItem.query
                .filter(
                    IngresoDocumentoItem.codigo_producto == codigo,
                    IngresoDocumentoItem.origen_compra == origen,
                    IngresoDocumentoItem.cantidad > 0,
                    IngresoDocumentoItem.valor_neto.isnot(None),
                )
                .order_by(IngresoDocumentoItem.created_at.desc(), IngresoDocumentoItem.id.desc())
                .first()
            )
            if last_ing and float(last_ing.valor_neto or 0) > 0 and int(last_ing.cantidad or 0) > 0:
                cost_cache[cache_key] = float(last_ing.valor_neto) / float(last_ing.cantidad)
            else:
                cost_cache[cache_key] = None
        costo_unit = cost_cache[cache_key]
        venta_unit = (venta / qty) if qty > 0 else 0.0
        utilidad_unit = (venta_unit - costo_unit) if (costo_unit is not None and qty > 0) else None
        utilidad_total = (utilidad_unit * qty) if utilidad_unit is not None else None
        margen_pct = ((utilidad_unit / venta_unit) * 100.0) if (utilidad_unit is not None and venta_unit > 0) else None
        productos.append(
            {
                "codigo": codigo,
                "descripcion": r.descripcion or codigo,
                "origen_compra": origen,
                "qty": qty,
                "venta": venta,
                "venta_unit": venta_unit,
                "costo_unit_ref": costo_unit,
                "utilidad_unit": utilidad_unit,
                "utilidad_total": utilidad_total,
                "margen_pct": margen_pct,
            }
        )

    total_venta = sum(p["venta"] for p in productos)
    total_utilidad = sum(float(p["utilidad_total"] or 0) for p in productos)
    cobertura_costo = sum(1 for p in productos if p["costo_unit_ref"] is not None)
    total_unidades = sum(int(p["qty"] or 0) for p in productos)
    margen_sobre_venta_pct = (
        (total_utilidad / total_venta * 100.0) if total_venta and total_venta > 0 else None
    )

    if export in {"csv", "excel"}:
        headers = [
            "Codigo",
            "Descripcion",
            "Origen",
            "Unidades",
            "Costo unit ref",
            "Venta unit prom",
            "Utilidad unit",
            "Utilidad total",
            "Margen pct",
            "Venta total",
        ]
        if export == "csv":
            sio = io.StringIO()
            writer = csv.writer(sio)
            writer.writerow(headers)
            for p in productos:
                writer.writerow([
                    p["codigo"],
                    p["descripcion"],
                    p["origen_compra"],
                    p["qty"],
                    "" if p["costo_unit_ref"] is None else round(float(p["costo_unit_ref"]), 4),
                    round(float(p["venta_unit"] or 0), 4),
                    "" if p["utilidad_unit"] is None else round(float(p["utilidad_unit"]), 4),
                    "" if p["utilidad_total"] is None else round(float(p["utilidad_total"]), 4),
                    "" if p["margen_pct"] is None else round(float(p["margen_pct"]), 4),
                    round(float(p["venta"] or 0), 4),
                ])
            writer.writerow([])
            writer.writerow([
                "TOTALES",
                "",
                "",
                total_unidades,
                "",
                "",
                "",
                round(float(total_utilidad), 2),
                round(margen_sobre_venta_pct, 2) if margen_sobre_venta_pct is not None else "",
                round(float(total_venta), 2),
            ])
            writer.writerow([
                "Resumen",
                (
                    f"Periodo {desde_str} a {hasta_str}; "
                    f"unidades {total_unidades}; "
                    f"productos con costo ref {cobertura_costo} de {len(productos)}"
                ),
                "", "", "", "", "", "", "", "",
            ])
            filename = f"rentabilidad_productos_{date.today().isoformat()}.csv"
            return Response(
                sio.getvalue(),
                mimetype="text/csv; charset=utf-8",
                headers={"Content-Disposition": f"attachment; filename={filename}"},
            )

        lines = ["\t".join(headers)]
        for p in productos:
            lines.append("\t".join([
                str(p["codigo"] or ""),
                str(p["descripcion"] or ""),
                str(p["origen_compra"] or ""),
                str(p["qty"] or 0),
                "" if p["costo_unit_ref"] is None else str(round(float(p["costo_unit_ref"]), 4)),
                str(round(float(p["venta_unit"] or 0), 4)),
                "" if p["utilidad_unit"] is None else str(round(float(p["utilidad_unit"]), 4)),
                "" if p["utilidad_total"] is None else str(round(float(p["utilidad_total"]), 4)),
                "" if p["margen_pct"] is None else str(round(float(p["margen_pct"]), 4)),
                str(round(float(p["venta"] or 0), 4)),
            ]))
        lines.append("")
        lines.append("\t".join([
            "TOTALES",
            "",
            "",
            str(total_unidades),
            "",
            "",
            "",
            str(round(float(total_utilidad), 2)),
            "" if margen_sobre_venta_pct is None else str(round(margen_sobre_venta_pct, 2)),
            str(round(float(total_venta), 2)),
        ]))
        lines.append("\t".join([
            "Resumen",
            (
                f"Periodo {desde_str} a {hasta_str}; "
                f"unidades {total_unidades}; "
                f"productos con costo ref {cobertura_costo} de {len(productos)}"
            ),
            "", "", "", "", "", "", "", "",
        ]))
        filename = f"rentabilidad_productos_{date.today().isoformat()}.xls"
        return Response(
            "\n".join(lines),
            mimetype="application/vnd.ms-excel; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    return render_template(
        "informes/utilidad_margen.html",
        productos=productos,
        total_venta=total_venta,
        total_utilidad=total_utilidad,
        cobertura_costo=cobertura_costo,
        filtros={
            "desde": desde_str,
            "hasta": hasta_str,
            "codigo": codigo_q,
            "origen": origen_q,
            "limit": limit,
        },
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
