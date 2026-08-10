import logging
import os
from hmac import compare_digest
from flask import Blueprint, request, jsonify, render_template, send_file
from datetime import datetime, timedelta
from pathlib import Path
import io
from ..models import SessionDB, Producto
from ..extensions import db
from ..utils.decorators import admin_required
from ..utils.gdrive_backup import (
    download_drive_file,
    get_backup_config,
    humanize_gdrive_error,
    list_drive_backups,
    load_last_status,
    restore_db_from_upload,
    run_gdrive_backup,
)
from ..utils.csrf import validate_csrf_request
from ..models import Etiqueta
from app.seguridad.models import Usuario
from app.import_excel import import_products_from_excel

logger = logging.getLogger(__name__)


def _valid_db_sync_token() -> bool:
    expected = (os.environ.get("ANDES_DB_SYNC_TOKEN") or "").strip()
    if not expected or len(expected) < 16:
        return False
    provided = (
        (request.headers.get("X-Andes-Sync-Token") or "").strip()
        or (request.form.get("sync_token") or "").strip()
    )
    if not provided:
        return False
    return compare_digest(expected, provided)


admin_bp = Blueprint("admin", __name__)

ALLOWED_IMPORT_EXTENSIONS = {".xlsx", ".xls", ".csv"}
# Alto para no romper flujos actuales; evita DoS por archivos enormes.
MAX_IMPORT_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB


# ===============================
# IMPORTAR EXCEL
# ===============================

@admin_bp.route("/importar_excel", methods=["POST"])
@admin_required
def importar_excel():

    archivo = request.files.get("archivo") or request.files.get("file")

    if not archivo:
        return jsonify(success=False, message="No se seleccionó archivo")

    if request.content_length and int(request.content_length) > MAX_IMPORT_UPLOAD_BYTES:
        return jsonify(success=False, message="Archivo demasiado grande para importar"), 413

    filename = (getattr(archivo, "filename", "") or "").strip()
    ext = Path(filename).suffix.lower()
    if ext and ext not in ALLOWED_IMPORT_EXTENSIONS:
        return jsonify(success=False, message="Tipo de archivo no permitido para importar"), 400

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

ADMIN_BUSCAR_MAX = 500


@admin_bp.route("/buscar")
@admin_required
def buscar():
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

        # Sin tope, esta vista cargaba el catálogo entero (~28k filas) en memoria.
        productos = [
            p
            for p in query.order_by(Producto.codigo.asc()).limit(ADMIN_BUSCAR_MAX).all()
            if p is not None
        ]

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
        logger.debug("Usuarios cargados: %s", len(usuarios))

    except Exception:
        logger.exception("Error en /admin/buscar")
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

@admin_bp.route("/producto/<codigo>/toggle_etiqueta/<int:etiqueta_id>", methods=["POST"])
@admin_required
def toggle_etiqueta(codigo, etiqueta_id):

    db = SessionDB()

    producto = db.query(Producto).filter_by(codigo=codigo).first()
    etiqueta = db.query(Etiqueta).get(etiqueta_id)

    if not producto or not etiqueta:
        db.close()
        return jsonify(success=False, message="Producto o etiqueta no encontrados"), 404

    if etiqueta in producto.etiquetas:
        producto.etiquetas.remove(etiqueta)
        attached = False
    else:
        producto.etiquetas.append(etiqueta)
        attached = True

    db.commit()
    db.close()

    return jsonify(success=True, attached=attached)

# ===============================
# GENERAR ETIQUETA IMPRIMIBLE
# ===============================

import qrcode
import barcode
from barcode.writer import ImageWriter
import io
import base64

logger = logging.getLogger(__name__)

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


# ===============================
# BACKUPS GOOGLE DRIVE
# ===============================

@admin_bp.route("/backups")
@admin_required
def backups_view():
    backups = []
    list_error = None
    last_status = None
    configured = False
    try:
        cfg = get_backup_config()
        configured = bool(cfg.get("folder_id"))
        try:
            last_status = load_last_status()
        except Exception as exc:
            logger.warning("backups_view: load_last_status falló: %s", exc)
            last_status = None
        try:
            backups = list_drive_backups()
        except Exception as exc:
            list_error = humanize_gdrive_error(exc)
            logger.warning("backups_view: list_drive_backups falló: %s", exc)
    except Exception as exc:
        list_error = humanize_gdrive_error(exc)
        logger.exception("backups_view: error de configuración")

    if last_status and not last_status.get("success") and last_status.get("message"):
        last_status = dict(last_status)
        last_status["message"] = humanize_gdrive_error(last_status["message"])

    try:
        return render_template(
            "admin/backups.html",
            backups=backups or [],
            last_status=last_status,
            list_error=list_error,
            configured=configured,
            active_page="admin_backups",
        )
    except Exception:
        logger.exception("backups_view: error al renderizar plantilla")
        return (
            '<div style="padding:28px 20px;text-align:center;color:#b91c1c;">'
            "No se pudo cargar Backups Google Drive. Revisá el log del servidor."
            "</div>",
            500,
            {"Content-Type": "text/html; charset=utf-8"},
        )


@admin_bp.route("/backups/run", methods=["POST"])
@admin_required
def backups_run_now():
    if not validate_csrf_request():
        return jsonify(success=False, message="Token CSRF inválido"), 403

    result = run_gdrive_backup()
    return jsonify(
        success=result.success,
        message=result.message,
        filename=result.filename,
        size_bytes=result.size_bytes,
        ran_at=result.ran_at,
    )


@admin_bp.route("/backups/download/<file_id>")
@admin_required
def backups_download(file_id):
    try:
        data, filename = download_drive_file(file_id)
        return send_file(
            io.BytesIO(data),
            mimetype="application/zip",
            as_attachment=True,
            download_name=filename,
        )
    except Exception as exc:
        return jsonify(success=False, message=str(exc)), 500


@admin_bp.route("/backups/restore", methods=["POST"])
@admin_required
def backups_restore():
    """Sube la base local (andes.db o zip de backup) al servidor de producción."""
    if not validate_csrf_request():
        return jsonify(success=False, message="Token CSRF inválido"), 403

    archivo = request.files.get("archivo") or request.files.get("file")
    if not archivo or not (archivo.filename or "").strip():
        return jsonify(success=False, message="Selecciona un archivo .db o .zip"), 400

    result = restore_db_from_upload(archivo)
    status = 200 if result.success else 400
    return jsonify(
        success=result.success,
        message=result.message,
        filename=result.filename,
        size_bytes=result.size_bytes,
        ran_at=result.ran_at,
    ), status


@admin_bp.route("/backups/sync", methods=["POST"])
def backups_sync():
    """
    Sync unidireccional PC → Render (sin sesión de usuario).

    Requiere header X-Andes-Sync-Token == ANDES_DB_SYNC_TOKEN.
    Pensado para el script local cada 15 min; no reemplaza el ERP del PC.
    """
    if not _valid_db_sync_token():
        logger.warning("backups_sync: token inválido o no configurado")
        return jsonify(success=False, message="No autorizado"), 401

    archivo = request.files.get("archivo") or request.files.get("file")
    if not archivo or not (archivo.filename or "").strip():
        return jsonify(success=False, message="Falta archivo .db o .zip"), 400

    result = restore_db_from_upload(archivo)
    status = 200 if result.success else 400
    return jsonify(
        success=result.success,
        message=result.message,
        filename=result.filename,
        size_bytes=result.size_bytes,
        ran_at=result.ran_at,
        source="pc_sync",
    ), status
