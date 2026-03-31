from flask import Blueprint, render_template, request, redirect, url_for, flash
from ..models import SessionDB
from .models import (
    Vehiculo, OEM, Bodega, Stock,
    ProductoComercial, Venta, ModeloMaestro
)
from ..utils.decorators import login_required
from sqlalchemy.orm import joinedload
from datetime import datetime


automotriz_bp = Blueprint("automotriz", __name__, url_prefix="/automotriz")


# ==============================
# NORMALIZADOR
# ==============================

def normalizar_modelo(texto):
    if not texto:
        return ""
    texto = texto.upper().strip()
    texto = texto.replace(".", "")
    texto = texto.replace("  ", " ")
    return texto


# ==============================
# PANEL ULTRA LIVIANO
# ==============================

@automotriz_bp.route("/")
@login_required
def panel():

    db = SessionDB()

    total_oems = db.query(OEM).count()
    total_vehiculos = db.query(Vehiculo).count()
    total_productos = db.query(ProductoComercial).count()
    total_ventas = db.query(Venta).count()

    ultimas_ventas = db.query(Venta).options(
    joinedload(Venta.producto)
        .joinedload(ProductoComercial.oem),
    joinedload(Venta.bodega)
).order_by(Venta.id.desc()).limit(20).all()

    db.close()

    return render_template(
        "automotriz_panel.html",
        total_oems=total_oems,
        total_vehiculos=total_vehiculos,
        total_productos=total_productos,
        total_ventas=total_ventas,
        ventas=ultimas_ventas
    )


# ==============================
# CREAR VEHICULO
# ==============================

@automotriz_bp.route("/crear_vehiculo", methods=["POST"])
@login_required
def crear_vehiculo():

    db = SessionDB()

    nuevo = Vehiculo(
        marca=request.form.get("marca"),
        modelo=request.form.get("modelo"),
        anio_desde=request.form.get("anio_desde") or None,
        anio_hasta=request.form.get("anio_hasta") or None
    )

    db.add(nuevo)
    db.commit()
    db.close()

    return redirect(url_for("automotriz.panel"))


# ==============================
# CREAR OEM
# ==============================

@automotriz_bp.route("/crear_oem", methods=["POST"])
@login_required
def crear_oem():

    db = SessionDB()

    nuevo = OEM(
        codigo_oem=request.form.get("codigo_oem"),
        descripcion_tecnica=request.form.get("descripcion")
    )

    db.add(nuevo)
    db.commit()
    db.close()

    return redirect(url_for("automotriz.panel"))

# ==============================
# ASIGNAR OEM A VEHICULO
# ==============================

@automotriz_bp.route("/asignar_oem_vehiculo", methods=["POST"])
@login_required
def asignar_oem_vehiculo():

    db = SessionDB()

    oem_id = request.form.get("oem_id")
    vehiculo_ids = request.form.getlist("vehiculo_ids")

    if not oem_id or not vehiculo_ids:
        db.close()
        return redirect(url_for("automotriz.panel"))

    oem = db.get(OEM, int(oem_id))

    for vid in vehiculo_ids:
        vehiculo = db.get(Vehiculo, int(vid))
        if vehiculo and vehiculo not in oem.vehiculos:
            oem.vehiculos.append(vehiculo)

    db.commit()
    db.close()

    return redirect(url_for("automotriz.panel"))


# ==============================
# CREAR BODEGA
# ==============================

@automotriz_bp.route("/crear_bodega", methods=["POST"])
@login_required
def crear_bodega():

    db = SessionDB()
    db.add(Bodega(nombre=request.form.get("nombre")))
    db.commit()
    db.close()

    return redirect(url_for("automotriz.panel"))


# ==============================
# CREAR PRODUCTO COMERCIAL
# ==============================

@automotriz_bp.route("/crear_producto_comercial", methods=["POST"])
@login_required
def crear_producto_comercial():

    db = SessionDB()
    codigo = request.form.get("codigo_interno")

    existe = db.query(ProductoComercial)\
        .filter_by(codigo_interno=codigo)\
        .first()

    if existe:
        flash("⚠️ Código duplicado", "error")
        db.close()
        return redirect(url_for("automotriz.panel"))

    nuevo = ProductoComercial(
        oem_id=request.form.get("oem_id"),
        codigo_interno=codigo,
        marca=request.form.get("marca"),
        precio_publico=request.form.get("precio_publico"),
        precio_mayor=request.form.get("precio_mayor"),
    )

    db.add(nuevo)
    db.commit()
    db.close()

    return redirect(url_for("automotriz.panel"))


# ==============================
# INGRESAR STOCK
# ==============================

@automotriz_bp.route("/ingresar_stock", methods=["POST"])
@login_required
def ingresar_stock():

    db = SessionDB()

    producto_id = int(request.form.get("producto_id"))
    bodega_id = int(request.form.get("bodega_id"))
    cantidad = int(request.form.get("cantidad"))

    stock = db.query(Stock)\
        .filter_by(producto_id=producto_id, bodega_id=bodega_id)\
        .first()

    if stock:
        stock.cantidad += cantidad
    else:
        db.add(Stock(
            producto_id=producto_id,
            bodega_id=bodega_id,
            cantidad=cantidad
        ))

    db.commit()
    db.close()

    return redirect(url_for("automotriz.panel"))


# ==============================
# VENDER PRODUCTO
# ==============================

@automotriz_bp.route("/vender_producto", methods=["POST"])
@login_required
def vender_producto():

    db = SessionDB()

    producto_id = int(request.form.get("producto_id"))
    bodega_id = int(request.form.get("bodega_id"))
    cantidad = int(request.form.get("cantidad"))

    stock = db.query(Stock)\
        .filter_by(producto_id=producto_id, bodega_id=bodega_id)\
        .first()

    if not stock or stock.cantidad < cantidad:
        db.close()
        return "Stock insuficiente"

    stock.cantidad -= cantidad

    db.add(Venta(
        producto_id=producto_id,
        bodega_id=bodega_id,
        cantidad=cantidad,
        fecha=str(datetime.now())
    ))

    db.commit()
    db.close()

    return redirect(url_for("automotriz.panel"))


# ==============================
# BUSCAR POR VEHICULO (OPTIMIZADO)
# ==============================

@automotriz_bp.route("/buscar_por_vehiculo", methods=["GET", "POST"])
@login_required
def buscar_por_vehiculo():

    db = SessionDB()

    marcas = [m[0] for m in db.query(Vehiculo.marca).distinct().all()]
    resultados = []

    if request.method == "POST":

        marca = request.form.get("marca")
        modelo = request.form.get("modelo")

        vehiculos = db.query(Vehiculo)\
            .options(joinedload(Vehiculo.oems))\
            .filter_by(marca=marca, modelo=modelo)\
            .all()

        for v in vehiculos:
            for oem in v.oems:
                resultados.append(oem)

    db.close()

    return render_template(
        "automotriz/buscar_por_vehiculo.html",
        marcas=marcas,
        resultados=resultados
    )