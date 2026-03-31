from flask import Blueprint, request, jsonify, render_template
from datetime import datetime, timedelta
from ..models import SessionDB, Producto
from ..extensions import db
from ..utils.decorators import admin_required
from ..models import Etiqueta
from app.seguridad.models import Usuario
from app.import_excel import import_products_from_excel


admin_bp = Blueprint("admin", __name__)


# ===============================
# IMPORTAR EXCEL
# ===============================

@admin_bp.route("/importar_excel", methods=["POST"])
@admin_required
def importar_excel():

    archivo = request.files.get("archivo") or request.files.get("file")

    if not archivo:
        return jsonify(success=False, message="No se seleccionó archivo")

    try:
        summary = import_products_from_excel(archivo, batch_size=2000)
        notes = " ".join(summary.get("import_notes") or [])

        return jsonify(
            success=True,
            message=(
                f"Importacion completada | Actualizados: {summary['updated']} | "
                f"Nuevos: {summary['inserted']} | Omitidos: {summary['skipped']} | "
                f"Errores: {len(summary['errors'])}"
                + (f" | {notes}" if notes else "")
            ),
            summary=summary,
            reload=True,
        )

    except Exception as e:
        return jsonify(success=False, message=str(e))


# ===============================
# BUSCAR PRODUCTOS + USUARIOS
# ===============================

@admin_bp.route("/buscar")
@admin_required
def buscar():

    print("🔥 RUTA /admin/buscar EJECUTADA 🔥")

    termino = request.args.get("q", "").strip()

    # Use a distinct name so the imported Flask-SQLAlchemy 'db' is not shadowed.
    sess = SessionDB()
    online_users = []

    try:
        # ===============================
        # PRODUCTOS
        # ===============================
        query = sess.query(Producto)

        if termino:
            query = query.filter(
                Producto.codigo.contains(termino) |
                Producto.descripcion.contains(termino) |
                Producto.modelo.contains(termino)
            )

        productos = query.all()
        productos = [p for p in productos if p is not None]
        for p in productos:
            if not (p.codigo or "").strip():
                print("Producto sin código detectado")

        # ===============================
        # USUARIOS (DESDE SEGURIDAD) — use the Flask-SQLAlchemy 'db', not sess
        # ===============================
        usuarios = db.session.query(Usuario).all()
        threshold = datetime.utcnow() - timedelta(minutes=2)
        online_users = (
            db.session.query(Usuario)
            .filter(Usuario.last_seen >= threshold)
            .order_by(Usuario.usuario.asc())
            .all()
        )

        # ===============================
        # DEBUG REAL
        # ===============================
        print("👥 USUARIOS LISTA:", usuarios)
        print("📊 TOTAL USUARIOS:", len(usuarios))

    except Exception as e:
        print("❌ ERROR EN BUSCAR:", e)
        productos = []
        usuarios = []

    finally:
        sess.close()

    # ===============================
    # RENDER
    # ===============================

    def stock_total(p):
        if p is None:
            return 0
        return (
            (p.stock_10jul or 0) +
            (p.stock_brasil or 0) +
            (p.stock_g_avenida or 0) +
            (p.stock_orientales or 0) +
            (p.stock_b20_outlet or 0) +
            (p.stock_transito or 0)
        )

    return render_template(
        "buscar.html",
        productos=productos,
        q=termino,
        termino=termino,
        usuarios=usuarios,
        online_users=online_users,
        stock_total=stock_total,
    )
# ===============================
# VER PRODUCTO (LUPA)
# ===============================

@admin_bp.route("/producto/<codigo>")
@admin_required
def ver_producto(codigo):

    db = SessionDB()

    producto = db.query(Producto).filter_by(codigo=codigo).first()

    if not producto:
        db.close()
        return "Producto no encontrado"

    # 🔥 Forzamos la carga de etiquetas antes de cerrar la sesión
    producto.etiquetas

    etiquetas = db.query(Etiqueta).all()

    db.close()

    return render_template(
        "producto.html",
        producto=producto,
        etiquetas=etiquetas
    )
# ===============================
# ETIQUETAS DE PRODUCTO
# ===============================

@admin_bp.route("/producto/<codigo>/toggle_etiqueta/<int:etiqueta_id>")
@admin_required
def toggle_etiqueta(codigo, etiqueta_id):

    db = SessionDB()

    producto = db.query(Producto).filter_by(codigo=codigo).first()
    etiqueta = db.query(Etiqueta).get(etiqueta_id)

    if not producto or not etiqueta:
        db.close()
        return "Error"

    if etiqueta in producto.etiquetas:
        producto.etiquetas.remove(etiqueta)
    else:
        producto.etiquetas.append(etiqueta)

    db.commit()
    db.close()

    return "OK"

# ===============================
# GENERAR ETIQUETA IMPRIMIBLE
# ===============================

import qrcode
import barcode
from barcode.writer import ImageWriter
import io
import base64

@admin_bp.route("/producto/<codigo>/etiqueta")
@admin_required
def generar_etiqueta(codigo):

    db = SessionDB()
    producto = db.query(Producto).filter_by(codigo=codigo).first()
    db.close()

    if not producto:
        return "Producto no encontrado"

    # ---------- QR ----------
    # Use the URL param 'codigo' as fallback if the DB row has a NULL primary key.
    producto_codigo = producto.codigo or codigo
    url = request.host_url + "producto/" + producto_codigo

    qr = qrcode.QRCode(
        version=None,
        box_size=4,   # tamaño del QR (optimizado para 5.5x3.5cm)
        border=1
    )
    qr.add_data(url)
    qr.make(fit=True)

    img_qr = qr.make_image(fill_color="black", back_color="white")
    buffer_qr = io.BytesIO()
    img_qr.save(buffer_qr, format="PNG")
    qr_base64 = base64.b64encode(buffer_qr.getvalue()).decode()


    # ---------- BARCODE (OPTIMIZADO PARA ETIQUETA PEQUEÑA) ----------
    if not producto_codigo:
        return "Código de producto no disponible para generar código de barras"
    code128 = barcode.get(
        "code128",
        producto_codigo,
        writer=ImageWriter()
    )

    buffer_bar = io.BytesIO()

    code128.write(buffer_bar, {
        "module_width": 0.70,   # grosor barras
        "module_height": 20,   # altura barras
        "quiet_zone": 0,       # espacio lateral
        "font_size": 0         # quita texto debajo (más limpio)
    })

    barcode_base64 = base64.b64encode(buffer_bar.getvalue()).decode()


    return render_template(
        "etiqueta_print.html",
        producto=producto,
        qr_img=qr_base64,
        barcode_img=barcode_base64
    )

# ===============================
# GENERAR HOJA DE ETIQUETAS
# ===============================

@admin_bp.route("/producto/<codigo>/etiquetas")
@admin_required
def generar_hoja_etiquetas(codigo):

    cantidad = int(request.args.get("cantidad", 1))

    db = SessionDB()
    producto = db.query(Producto).filter_by(codigo=codigo).first()
    db.close()

    if not producto:
        return "Producto no encontrado"

    # QR
    producto_codigo = producto.codigo or codigo
    url = request.host_url + "producto/" + producto_codigo
    qr = qrcode.make(url)
    buffer_qr = io.BytesIO()
    qr.save(buffer_qr, format="PNG")
    qr_base64 = base64.b64encode(buffer_qr.getvalue()).decode()

    # Barcode
    if not producto_codigo:
        return "Código de producto no disponible para generar código de barras"
    code128 = barcode.get('code128', producto_codigo, writer=ImageWriter())
    buffer_bar = io.BytesIO()
    code128.write(buffer_bar)
    barcode_base64 = base64.b64encode(buffer_bar.getvalue()).decode()

    return render_template(
        "etiquetas_hoja.html",
        producto=producto,
        qr_img=qr_base64,
        barcode_img=barcode_base64,
        cantidad=cantidad
    )