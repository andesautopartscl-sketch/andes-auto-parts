from datetime import datetime
from pathlib import Path
import logging
import os
import sys

from flask import Flask, render_template, request, session
from sqlalchemy import text
from .extensions import db
from .automotriz import models
from .ventas import models as ventas_models  # noqa: F401 – registers ORM models
from .auth.routes import auth_bp
from .productos.routes import productos_bp
from .admin.routes import admin_bp
from .automotriz.routes import automotriz_bp
from app.seguridad import seguridad_bp
from .ventas.routes import ventas_bp
from .bodega.routes import bodega_bp
from .chat import chat_bp
from .chat import models as chat_models  # noqa: F401 – registers ORM models
# ERP expansion modules
from .inventario import models as inventario_models  # noqa: F401
from .oportunidades import models as oportunidades_models  # noqa: F401
from .postventa import models as postventa_models  # noqa: F401
from .contabilidad import models as contabilidad_models  # noqa: F401
from .inventario.routes import inventario_bp
from .oportunidades.routes import oportunidades_bp
from .postventa.routes import postventa_bp
from .dashboard.routes import dashboard_bp
from .contabilidad.routes import contabilidad_bp, finanzas_bp
from .informes.routes import informes_bp
from app.seguridad.init_roles import crear_roles
from app.seguridad.crear_superadmin import crear_superadmin
from app.utils.datetime_utils import chile_datetime_filter
from app.utils.rut_utils import format_rut
from app.utils.audit_metadata_filter import format_audit_metadata
from app.utils.permissions import get_user_permissions


EXPECTED_VENV_PYTHON = str(
    (Path(__file__).resolve().parents[1] / ".venv" / "Scripts" / "python.exe").resolve()
)


def _runtime_reportlab_diagnostics() -> tuple[bool, str]:
    try:
        import reportlab  # noqa: F401

        return True, "reportlab import OK"
    except Exception as exc:
        return False, f"reportlab import FAILED: {exc}"


def enforce_expected_python() -> None:
    current_python = str(Path(sys.executable).resolve())
    if os.name != "nt":
        return

    expected_exists = Path(EXPECTED_VENV_PYTHON).exists()
    in_venv = (getattr(sys, "base_prefix", sys.prefix) != sys.prefix) or bool(os.environ.get("VIRTUAL_ENV"))

    if expected_exists and current_python.lower() != EXPECTED_VENV_PYTHON.lower():
        print(
            "WARN: Intérprete distinto al venv esperado.\n"
            f"  Esperado: {EXPECTED_VENV_PYTHON}\n"
            f"  Actual:   {current_python}\n"
            "Continuando igualmente..."
        )
    elif not in_venv:
        print(
            "WARN: Estás corriendo fuera de un entorno virtual (venv). "
            "Si faltan dependencias, crea/activa un venv e instala requirements."
        )


def log_runtime_startup_info(app: Flask) -> None:
    reportlab_ok, reportlab_msg = _runtime_reportlab_diagnostics()
    app.logger.info("Python executable: %s", sys.executable)
    app.logger.info("Expected executable: %s", EXPECTED_VENV_PYTHON)
    app.logger.info("%s", reportlab_msg)
    if not reportlab_ok:
        app.logger.error("reportlab is not available in the active interpreter")


def create_app():

    enforce_expected_python()

    app = Flask(__name__)
    if not app.logger.handlers:
        logging.basicConfig(level=logging.INFO)
    app.jinja_env.filters["chile_datetime"] = chile_datetime_filter
    app.jinja_env.filters["format_rut"] = format_rut
    app.jinja_env.filters["audit_metadata"] = format_audit_metadata
    log_runtime_startup_info(app)

    # ===============================
    # CONFIGURACIÓN BASE DE DATOS
    # ===============================

    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    DB_PATH = os.path.join(BASE_DIR, "..", "data", "andes.db")
    CHAT_UPLOADS_PATH = Path(BASE_DIR).resolve().parent / "data" / "chat_uploads"
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)

    # Importar modelos de seguridad
    from .seguridad import models

    # ===============================
    # SEGURIDAD DE SESIÓN
    # ===============================

    app.secret_key = "andes_auto_parts_super_secret_key_2026"

    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SECURE"] = False
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    # ===============================
    # REGISTRAR BLUEPRINTS
    # ===============================

    app.register_blueprint(auth_bp)
    app.register_blueprint(productos_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(automotriz_bp)
    app.register_blueprint(seguridad_bp, url_prefix="/seguridad")
    app.register_blueprint(ventas_bp)
    app.register_blueprint(bodega_bp)
    app.register_blueprint(chat_bp)
    # ERP expansion blueprints
    app.register_blueprint(inventario_bp)
    app.register_blueprint(oportunidades_bp)
    app.register_blueprint(postventa_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(contabilidad_bp)
    app.register_blueprint(finanzas_bp)
    app.register_blueprint(informes_bp)

    print(app.url_map)

    # ===============================
    # CREAR TABLAS SEGURIDAD
    # ===============================

    with app.app_context():

        db.create_all()

        # Keep legacy DBs compatible: add last_seen when column is missing.
        with db.engine.begin() as conn:
            cols = conn.execute(text("PRAGMA table_info(usuarios_sistema)")).fetchall()
            col_names = {col[1] for col in cols}
            if "last_seen" not in col_names:
                conn.execute(text("ALTER TABLE usuarios_sistema ADD COLUMN last_seen DATETIME"))
            if "intentos_fallidos" not in col_names:
                conn.execute(text("ALTER TABLE usuarios_sistema ADD COLUMN intentos_fallidos INTEGER DEFAULT 0"))
            if "bloqueado_seguridad" not in col_names:
                conn.execute(text("ALTER TABLE usuarios_sistema ADD COLUMN bloqueado_seguridad BOOLEAN DEFAULT 0"))
            if "bloqueado_at" not in col_names:
                conn.execute(text("ALTER TABLE usuarios_sistema ADD COLUMN bloqueado_at DATETIME"))
            conn.execute(text("UPDATE usuarios_sistema SET intentos_fallidos = COALESCE(intentos_fallidos, 0)"))
            conn.execute(text("UPDATE usuarios_sistema SET bloqueado_seguridad = COALESCE(bloqueado_seguridad, 0)"))

            product_cols = conn.execute(text("PRAGMA table_info(productos)")).fetchall()
            product_col_names = {col[1] for col in product_cols}
            if "ACTIVO" not in product_col_names:
                conn.execute(text("ALTER TABLE productos ADD COLUMN ACTIVO BOOLEAN DEFAULT 1"))
            conn.execute(text("UPDATE productos SET ACTIVO = 1 WHERE ACTIVO IS NULL"))
            product_missing_columns = {
                "anio": "VARCHAR",
                "version": "VARCHAR",
                "factura_proveedor": "VARCHAR",
                "categoria_id": "INTEGER",
                "subcategoria_id": "INTEGER",
                "despiece": "VARCHAR",
                "imagen_url": "VARCHAR",
            }
            for column_name, column_type in product_missing_columns.items():
                if column_name not in product_col_names:
                    conn.execute(text(f"ALTER TABLE productos ADD COLUMN {column_name} {column_type}"))

            # Search performance indexes for ERP product finder and label search.
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_productos_codigo ON productos(CODIGO)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_productos_descripcion ON productos(DESCRIPCION)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_productos_codigo_oem ON productos([CODIGO OEM])"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_productos_activo ON productos(ACTIVO)"))

            movimientos_cols = conn.execute(text("PRAGMA table_info(movimientos_stock)")).fetchall()
            movimientos_col_names = {col[1] for col in movimientos_cols}
            if movimientos_cols and "proveedor" not in movimientos_col_names:
                conn.execute(text("ALTER TABLE movimientos_stock ADD COLUMN proveedor VARCHAR(150)"))
            if movimientos_cols and "marca" not in movimientos_col_names:
                conn.execute(text("ALTER TABLE movimientos_stock ADD COLUMN marca VARCHAR(120)"))
            if movimientos_cols and "bodega" not in movimientos_col_names:
                conn.execute(text("ALTER TABLE movimientos_stock ADD COLUMN bodega VARCHAR(120)"))
            if movimientos_cols and "ingreso_documento_id" not in movimientos_col_names:
                conn.execute(text("ALTER TABLE movimientos_stock ADD COLUMN ingreso_documento_id INTEGER"))

            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS productos_variantes_stock (
                        id INTEGER PRIMARY KEY,
                        codigo_producto VARCHAR(100) NOT NULL,
                        marca VARCHAR(120) NOT NULL,
                        proveedor VARCHAR(150),
                        bodega VARCHAR(120) NOT NULL,
                        stock INTEGER NOT NULL DEFAULT 0,
                        metadata_json TEXT,
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL,
                        CONSTRAINT uq_variante_codigo_marca_bodega UNIQUE (codigo_producto, marca, bodega)
                    )
                    """
                )
            )
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_variante_codigo ON productos_variantes_stock(codigo_producto)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_variante_marca ON productos_variantes_stock(marca)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_variante_bodega ON productos_variantes_stock(bodega)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_mov_ingreso_documento ON movimientos_stock(ingreso_documento_id)"))

            ventas_proveedores_cols = conn.execute(text("PRAGMA table_info(ventas_proveedores)")).fetchall()
            ventas_proveedores_col_names = {col[1] for col in ventas_proveedores_cols}
            if ventas_proveedores_cols and "empresa" not in ventas_proveedores_col_names:
                conn.execute(text("ALTER TABLE ventas_proveedores ADD COLUMN empresa VARCHAR(200) DEFAULT ''"))

            ventas_clientes_cols = conn.execute(text("PRAGMA table_info(ventas_clientes)")).fetchall()
            ventas_clientes_col_names = {col[1] for col in ventas_clientes_cols}
            for col_name, col_type, default_value in [
                ("giro", "VARCHAR(200)", "''"),
                ("region", "VARCHAR(120)", "''"),
                ("comuna", "VARCHAR(120)", "''"),
                ("ciudad", "VARCHAR(120)", "''"),
                ("pais", "VARCHAR(120)", "'Chile'"),
            ]:
                if ventas_clientes_cols and col_name not in ventas_clientes_col_names:
                    conn.execute(text(f"ALTER TABLE ventas_clientes ADD COLUMN {col_name} {col_type} DEFAULT {default_value}"))

            for col_name, col_type, default_value in [
                ("giro", "VARCHAR(200)", "''"),
                ("region", "VARCHAR(120)", "''"),
                ("comuna", "VARCHAR(120)", "''"),
                ("ciudad", "VARCHAR(120)", "''"),
                ("pais", "VARCHAR(120)", "'Chile'"),
            ]:
                if ventas_proveedores_cols and col_name not in ventas_proveedores_col_names:
                    conn.execute(text(f"ALTER TABLE ventas_proveedores ADD COLUMN {col_name} {col_type} DEFAULT {default_value}"))

            ventas_documentos_cols = conn.execute(text("PRAGMA table_info(ventas_documentos)")).fetchall()
            ventas_documentos_col_names = {col[1] for col in ventas_documentos_cols}
            for col_name, col_type in [
                ("proveedor_id", "INTEGER"),
                ("source_id", "INTEGER"),
                ("source_type", "VARCHAR(40)"),
                ("root_id", "INTEGER"),
                ("metodo_pago", "VARCHAR(50)"),
                ("estado_pago", "VARCHAR(30)"),
            ]:
                if ventas_documentos_cols and col_name not in ventas_documentos_col_names:
                    conn.execute(text(f"ALTER TABLE ventas_documentos ADD COLUMN {col_name} {col_type}"))

            ventas_nc_cols = conn.execute(text("PRAGMA table_info(ventas_notas_credito)")).fetchall()
            ventas_nc_col_names = {col[1] for col in ventas_nc_cols}
            for col_name, col_type in [
                ("source_id", "INTEGER"),
                ("source_type", "VARCHAR(40)"),
                ("root_id", "INTEGER"),
            ]:
                if ventas_nc_cols and col_name not in ventas_nc_col_names:
                    conn.execute(text(f"ALTER TABLE ventas_notas_credito ADD COLUMN {col_name} {col_type}"))

            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_ventas_documentos_tipo_numero ON ventas_documentos(tipo, numero)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_ventas_documentos_source ON ventas_documentos(source_type, source_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_ventas_documentos_root ON ventas_documentos(root_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_ventas_nc_source ON ventas_notas_credito(source_type, source_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_ventas_nc_root ON ventas_notas_credito(root_id)"))

            # Finance compatibility objects expected by older ERP screens.
            conn.execute(
                text(
                    """
                    CREATE VIEW IF NOT EXISTS cuentas AS
                    SELECT id, codigo, nombre, tipo, descripcion, activo
                    FROM cuentas_contables
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE VIEW IF NOT EXISTS asientos AS
                    SELECT id, fecha, cuenta_id, tipo, monto, descripcion, documento_ref, usuario, created_at
                    FROM movimientos_contables
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE VIEW IF NOT EXISTS movimientos AS
                    SELECT id, fecha, cuenta_id, tipo, monto, descripcion, documento_ref, usuario, created_at
                    FROM movimientos_contables
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS pagos (
                        id INTEGER PRIMARY KEY,
                        documento_ref VARCHAR(60),
                        tercero_nombre VARCHAR(200),
                        monto FLOAT NOT NULL DEFAULT 0,
                        fecha DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        estado VARCHAR(30) DEFAULT 'pendiente',
                        observacion TEXT,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS cobros (
                        id INTEGER PRIMARY KEY,
                        documento_ref VARCHAR(60),
                        tercero_nombre VARCHAR(200),
                        monto FLOAT NOT NULL DEFAULT 0,
                        fecha DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        estado VARCHAR(30) DEFAULT 'pendiente',
                        observacion TEXT,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )

            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS oem_despiece (
                        id INTEGER PRIMARY KEY,
                        oem_norm VARCHAR(64) NOT NULL UNIQUE,
                        producto_codigo VARCHAR(64) UNIQUE,
                        titulo VARCHAR(220),
                        imagen_static VARCHAR(512),
                        partes_json TEXT,
                        notas TEXT,
                        updated_at DATETIME
                    )
                    """
                )
            )
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_oem_despiece_oem_norm ON oem_despiece(oem_norm)"))

            # Tablas antiguas sin producto_codigo: ALTER primero; luego el índice (no indexar columna inexistente).
            oem_d_cols = conn.execute(text("PRAGMA table_info(oem_despiece)")).fetchall()
            oem_d_names = {c[1] for c in oem_d_cols} if oem_d_cols else set()
            if oem_d_cols and "producto_codigo" not in oem_d_names:
                conn.execute(text("ALTER TABLE oem_despiece ADD COLUMN producto_codigo VARCHAR(64)"))
            oem_d_names_after = {c[1] for c in conn.execute(text("PRAGMA table_info(oem_despiece)")).fetchall()}
            if "producto_codigo" in oem_d_names_after:
                conn.execute(
                    text("CREATE INDEX IF NOT EXISTS idx_oem_despiece_producto_codigo ON oem_despiece(producto_codigo)")
                )

            chat_cols = conn.execute(text("PRAGMA table_info(chat_messages)")).fetchall()
            chat_col_names = {col[1] for col in chat_cols}
            for col_name, col_type, default_value in [
                ("message_type", "VARCHAR(20)", "'text'"),
                ("media_path", "VARCHAR(500)", "NULL"),
                ("media_name", "VARCHAR(255)", "NULL"),
                ("media_mime", "VARCHAR(120)", "NULL"),
                ("media_size", "INTEGER", "NULL"),
                ("read_at", "DATETIME", "NULL"),
                ("edited_at", "DATETIME", "NULL"),
                ("status", "VARCHAR(20)", "'sent'"),
                ("deleted_for_sender", "BOOLEAN", "0"),
                ("deleted_for_receiver", "BOOLEAN", "0"),
                ("deleted_for_all", "BOOLEAN", "0"),
                ("deleted_at", "DATETIME", "NULL"),
            ]:
                if chat_cols and col_name not in chat_col_names:
                    conn.execute(text(f"ALTER TABLE chat_messages ADD COLUMN {col_name} {col_type} DEFAULT {default_value}"))

        CHAT_UPLOADS_PATH.mkdir(parents=True, exist_ok=True)

        crear_roles()

        crear_superadmin()

    @app.before_request
    def update_last_seen_activity() -> None:
        # Skip static/unknown endpoints to reduce noisy commits.
        if request.endpoint in {None, "static"}:
            return

        username = session.get("user")
        if not username:
            return

        from app.seguridad.models import Usuario

        try:
            current_user = db.session.query(Usuario).filter_by(usuario=username).first()
            if current_user is None:
                return
            current_user.last_seen = datetime.utcnow()
            current_user.en_linea = True
            db.session.commit()
        except Exception:
            db.session.rollback()

    # ===============================
    # USUARIO DISPONIBLE EN TEMPLATES
    # ===============================

    @app.context_processor
    def usuario_actual():
        perms = get_user_permissions(session.get("user"), session.get("rol"))
        return dict(
            usuario_nombre=session.get("user"),
            usuario_rol=session.get("rol"),
            user_permissions=perms,
        )

    @app.context_processor
    def inject_partial_flag():
        """Expose _partial=True whenever the request carries the AJAX marker.
        Templates use it for conditional extends:
            {% extends 'base_content.html' if _partial else 'real_base.html' %}
        Ventas routes that set _partial explicitly take precedence because
        render_template() kwargs override context-processor values.
        """
        return {"_partial": request.headers.get("X-Requested-With") == "XMLHttpRequest"}

    @app.after_request
    def inject_chat_widget(response):
        # Inject chat globally for authenticated users in HTML responses.
        if response.status_code != 200:
            return response
        if "user" not in session:
            return response
        if not response.mimetype or "html" not in response.mimetype:
            return response
        if response.direct_passthrough:
            return response

        try:
            body = response.get_data(as_text=True)
            if not body or "</body>" not in body:
                return response
            if "id=\"apchat-root\"" in body:
                return response

            widget_html = render_template("chat/widget.html")
            response.set_data(body.replace("</body>", f"{widget_html}\n</body>", 1))
            return response
        except Exception:
            return response

    return app