from datetime import datetime
from pathlib import Path
import logging
import time
import os
import secrets
import sys

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from werkzeug.middleware.proxy_fix import ProxyFix
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
from .rrhh import models as rrhh_models  # noqa: F401
from .sii_sync import models as sii_sync_models  # noqa: F401
from .inventario.routes import inventario_bp
from .oportunidades.routes import oportunidades_bp
from .postventa.routes import postventa_bp
from .dashboard.routes import dashboard_bp
from .contabilidad.routes import contabilidad_bp, finanzas_bp
from .informes.routes import informes_bp
from .rrhh.routes import rrhh_bp
from .sii_sync import sii_sync_bp
from .mobile import mobile_bp
from app.seguridad.init_roles import crear_roles
from app.seguridad.crear_superadmin import crear_superadmin
from app.utils.datetime_utils import chile_datetime_filter
from app.utils.rut_utils import format_rut
from app.utils.phone_format import format_phone_display
from app.utils.audit_metadata_filter import format_audit_metadata
from app.utils.csrf import get_csrf_token, validate_csrf_request
from app.utils.format_currency_cl import format_precio_publico_con_iva
from app.utils.product_image_url import product_image_src
from app.utils.cloudinary_static_map import static_or_cloud
from app.utils.http_security import apply_security_headers
from app.utils.login_wall import is_logged_in_session, is_public_auth_route, safe_next_path
from app.utils.permissions import ALL_PERMISSION_KEYS, DEFAULT_PERMISSIONS, get_user_permissions


EXPECTED_VENV_PYTHON = str(
    (Path(__file__).resolve().parents[1] / ".venv" / "Scripts" / "python.exe").resolve()
)


@event.listens_for(Engine, "connect")
def _sqlite_pragmas_on_connect(dbapi_connection, _connection_record):
    """Reduce 'database is locked' y mejora concurrencia en SQLite (Render / Gunicorn)."""
    try:
        cur = dbapi_connection.cursor()
    except Exception:
        return
    try:
        cur.execute("PRAGMA busy_timeout=30000")
    except Exception:
        pass
    try:
        cur.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    try:
        cur.close()
    except Exception:
        pass


def _init_gdrive_backup_scheduler(app: Flask) -> None:
    """Programa backup diario a Google Drive (10:00 hora Chile)."""
    folder_id = (os.environ.get("GDRIVE_FOLDER_ID") or "").strip()
    if not folder_id:
        app.logger.info("Backup GDrive: GDRIVE_FOLDER_ID vacío; scheduler no iniciado.")
        return

    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return

    try:
        import atexit

        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger

        from app.utils.datetime_utils import CHILE_TZ
        from app.utils.gdrive_backup import run_gdrive_backup

        def _scheduled_backup():
            with app.app_context():
                try:
                    run_gdrive_backup(logger_instance=app.logger)
                except Exception:
                    app.logger.exception("Backup programado a Google Drive falló")

        scheduler = BackgroundScheduler(timezone=CHILE_TZ)
        scheduler.add_job(
            _scheduled_backup,
            trigger=CronTrigger(hour=10, minute=0),
            id="gdrive_daily_backup",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
        atexit.register(lambda: scheduler.shutdown(wait=False))
        app.logger.info("Backup GDrive: programado diariamente a las 10:00 (America/Santiago).")
    except Exception as exc:
        app.logger.warning("No se pudo iniciar scheduler de backup GDrive: %s", exc)


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


def _load_secret_key(base_dir: str) -> str:
    env_key = (
        os.environ.get("ANDES_SECRET_KEY")
        or os.environ.get("SECRET_KEY")
        or ""
    ).strip()
    if env_key:
        return env_key

    secret_path = Path(base_dir).resolve().parent / "data" / ".flask_secret_key"
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    if secret_path.exists():
        value = secret_path.read_text(encoding="utf-8").strip()
        if value:
            return value

    generated = secrets.token_urlsafe(64)
    secret_path.write_text(generated, encoding="utf-8")
    return generated


def create_app():

    enforce_expected_python()

    from app.utils.load_env import load_project_dotenv

    load_project_dotenv()

    app = Flask(__name__)
    if not app.logger.handlers:
        logging.basicConfig(level=logging.INFO)

    from app.extensions import limiter
    from flask_limiter.errors import RateLimitExceeded

    limiter.init_app(app)

    @app.errorhandler(RateLimitExceeded)
    def _handle_rate_limit_exceeded(_exc):
        msg = "Demasiados intentos. Espera un momento."
        if request.path.endswith("/login/password-reset-request"):
            return jsonify(success=False, message=msg), 429
        if request.endpoint == "auth.login" and request.method == "POST":
            next_url = safe_next_path(
                (request.values.get("next") or request.args.get("next") or "").strip() or None
            )
            return render_template("login.html", error=msg, next_url=next_url), 429
        return jsonify(success=False, message=msg), 429
    # Render / reverse proxy: esquema y host correctos para cookies y redirects.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)
    app.jinja_env.filters["chile_datetime"] = chile_datetime_filter
    app.jinja_env.filters["format_rut"] = format_rut
    app.jinja_env.filters["format_telefono"] = format_phone_display
    app.jinja_env.filters["audit_metadata"] = format_audit_metadata
    app.jinja_env.filters["format_precio_publico_con_iva"] = format_precio_publico_con_iva
    app.jinja_env.filters["product_image_src"] = product_image_src
    app.jinja_env.filters["static_or_cloud"] = static_or_cloud
    log_runtime_startup_info(app)
    from app.utils.load_env import log_sii_env_startup

    log_sii_env_startup(app.logger)

    # ===============================
    # CONFIGURACIÓN BASE DE DATOS
    # ===============================

    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    DB_PATH = os.path.join(BASE_DIR, "..", "data", "andes.db")
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    CHAT_UPLOADS_PATH = Path(BASE_DIR).resolve().parent / "data" / "chat_uploads"
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "connect_args": {"check_same_thread": False, "timeout": 30},
    }

    for _mail_key in (
        "MAIL_SERVER",
        "MAIL_PORT",
        "MAIL_USE_TLS",
        "MAIL_USERNAME",
        "MAIL_PASSWORD",
        "MAIL_FROM",
    ):
        _mail_val = os.environ.get(_mail_key)
        if _mail_val is not None and str(_mail_val).strip():
            app.config[_mail_key] = str(_mail_val).strip()

    _app_mode = (os.environ.get("ANDES_APP_MODE") or "").strip().lower()
    app.config["ANDES_APP_MODE"] = _app_mode

    db.init_app(app)

    # Importar modelos de seguridad
    from .seguridad import models

    # ===============================
    # SEGURIDAD DE SESIÓN
    # ===============================

    app.secret_key = _load_secret_key(BASE_DIR)

    app.config["SESSION_COOKIE_HTTPONLY"] = True
    # En Render (HTTPS) la cookie debe ser Secure. En local HTTP, False salvo que fuerces con env.
    _is_render = (os.environ.get("RENDER") or "").strip().lower() in {"1", "true", "yes"}
    _force_secure = (os.environ.get("ANDES_SESSION_COOKIE_SECURE") or "").strip().lower() in {"1", "true", "yes"}
    app.config["SESSION_COOKIE_SECURE"] = bool(_is_render or _force_secure)
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    # HSTS en HTTPS: desactivar con ANDES_HSTS=0 (pruebas locales con proxy, etc.)
    _hsts_env = (os.environ.get("ANDES_HSTS") or "1").strip().lower()
    app.config["ANDES_HSTS"] = _hsts_env not in {"0", "false", "no", "off"}

    # ===============================
    # REGISTRAR BLUEPRINTS
    # ===============================
    # ANDES_APP_MODE=search_lite → solo login + catálogo /buscar (despliegue liviano en Render u otro host).

    if _app_mode == "search_lite":
        app.register_blueprint(auth_bp)
        app.register_blueprint(productos_bp)

        @app.before_request
        def _search_lite_logueado_va_a_buscar():
            """auth.home sigue mapeado a /; con sesión activa, / debe ir al catálogo móvil."""
            if (app.config.get("ANDES_APP_MODE") or "").strip().lower() != "search_lite":
                return None
            if (request.path or "") != "/" or request.method != "GET":
                return None
            if not (session.get("user") or "").strip():
                return None
            return redirect(url_for("productos.buscar"))
    else:
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
        app.register_blueprint(rrhh_bp)
        app.register_blueprint(sii_sync_bp)
        app.register_blueprint(mobile_bp)

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
            if "foto_perfil" not in col_names:
                conn.execute(text("ALTER TABLE usuarios_sistema ADD COLUMN foto_perfil VARCHAR(255)"))
            conn.execute(text("UPDATE usuarios_sistema SET intentos_fallidos = COALESCE(intentos_fallidos, 0)"))
            conn.execute(text("UPDATE usuarios_sistema SET bloqueado_seguridad = COALESCE(bloqueado_seguridad, 0)"))
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS usuarios_permisos (
                        id INTEGER PRIMARY KEY,
                        usuario_id INTEGER NOT NULL UNIQUE,
                        ver_finanzas BOOLEAN NOT NULL DEFAULT 1,
                        ver_precio_mayor BOOLEAN NOT NULL DEFAULT 1,
                        actualizado_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_usuarios_permisos_usuario_id ON usuarios_permisos(usuario_id)"))
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS usuarios_permisos_detalle (
                        id INTEGER PRIMARY KEY,
                        usuario_id INTEGER NOT NULL,
                        permiso_key VARCHAR(120) NOT NULL,
                        allowed BOOLEAN NOT NULL DEFAULT 0,
                        actualizado_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        CONSTRAINT uq_usuario_permiso_key UNIQUE(usuario_id, permiso_key)
                    )
                    """
                )
            )
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_usuarios_perm_det_usuario_id ON usuarios_permisos_detalle(usuario_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_usuarios_perm_det_permiso_key ON usuarios_permisos_detalle(permiso_key)"))

            # Seed inicial de permisos granulares (deny-by-default para no superadmin).
            user_rows = conn.execute(text("SELECT u.id AS uid, COALESCE(r.nombre, '') AS rol_nombre FROM usuarios_sistema u LEFT JOIN roles r ON r.id = u.rol_id")).fetchall()
            for ur in user_rows:
                uid = int(ur[0])
                rol_name = (ur[1] or "").strip().lower()
                is_superadmin = ("superadmin" in rol_name)
                for pkey in ALL_PERMISSION_KEYS:
                    conn.execute(
                        text(
                            """
                            INSERT OR IGNORE INTO usuarios_permisos_detalle (usuario_id, permiso_key, allowed)
                            VALUES (:uid, :pkey, :allowed)
                            """
                        ),
                        {"uid": uid, "pkey": pkey, "allowed": 1 if is_superadmin else 0},
                    )

            # Seed RRHH perfil para usuarios existentes (1:1). No cambia permisos ni comportamiento.
            # Si la tabla aún no existe (primera corrida), db.create_all ya la crea por los modelos.
            conn.execute(
                text(
                    """
                    INSERT OR IGNORE INTO rrhh_perfil(
                        usuario_id,
                        salud_tipo, salud_entidad, salud_numero,
                        afp_nombre, afc_afiliado,
                        banco_nombre, banco_tipo_cuenta, banco_numero_cuenta,
                        es_vendedor, comision_pct,
                        created_at, updated_at
                    )
                    SELECT
                        u.id,
                        '', '', '',
                        '', 1,
                        '', '', '',
                        0, 0,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    FROM usuarios_sistema u
                    """
                )
            )

            rrhh_pf_cols = conn.execute(text("PRAGMA table_info(rrhh_perfil)")).fetchall()
            rrhh_pf_names = {c[1] for c in rrhh_pf_cols} if rrhh_pf_cols else set()
            if rrhh_pf_cols:
                if "contrato_vigencia_desde" not in rrhh_pf_names:
                    conn.execute(text("ALTER TABLE rrhh_perfil ADD COLUMN contrato_vigencia_desde DATE"))
                if "contrato_notas" not in rrhh_pf_names:
                    conn.execute(text("ALTER TABLE rrhh_perfil ADD COLUMN contrato_notas VARCHAR(500) DEFAULT ''"))
                if "contrato_pdf_relpath" not in rrhh_pf_names:
                    conn.execute(text("ALTER TABLE rrhh_perfil ADD COLUMN contrato_pdf_relpath VARCHAR(500)"))
                if "contrato_pdf_original" not in rrhh_pf_names:
                    conn.execute(text("ALTER TABLE rrhh_perfil ADD COLUMN contrato_pdf_original VARCHAR(260)"))
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS rrhh_vacaciones_registro (
                        id INTEGER PRIMARY KEY,
                        usuario_id INTEGER NOT NULL REFERENCES usuarios_sistema(id),
                        tipo VARCHAR(16) NOT NULL,
                        fecha_inicio DATE NOT NULL,
                        fecha_fin DATE,
                        dias INTEGER,
                        estado VARCHAR(24) NOT NULL DEFAULT '',
                        notas VARCHAR(500) NOT NULL DEFAULT '',
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_rrhh_vacaciones_usuario ON rrhh_vacaciones_registro(usuario_id)"))
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS rrhh_contrato_anexos (
                        id INTEGER PRIMARY KEY,
                        usuario_id INTEGER NOT NULL REFERENCES usuarios_sistema(id),
                        titulo VARCHAR(200) NOT NULL,
                        archivo_relpath VARCHAR(500) NOT NULL,
                        nombre_original VARCHAR(260) NOT NULL DEFAULT '',
                        mensaje VARCHAR(500) NOT NULL DEFAULT '',
                        estado VARCHAR(20) NOT NULL DEFAULT 'pendiente',
                        aceptado_at DATETIME,
                        aceptado_evidencia_hash VARCHAR(64),
                        creado_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        creado_por_usuario_id INTEGER REFERENCES usuarios_sistema(id)
                    )
                    """
                )
            )
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_rrhh_anexos_usuario ON rrhh_contrato_anexos(usuario_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_rrhh_anexos_estado ON rrhh_contrato_anexos(estado)"))

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
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_productos_marca ON productos(MARCA)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_productos_categoria ON productos(categoria_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_productos_subcategoria ON productos(subcategoria_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_productos_modelo ON productos(MODELO)"))
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_productos_activo_codigo "
                    "ON productos(ACTIVO, CODIGO)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_producto_imagenes_codigo "
                    "ON producto_imagenes(producto_codigo)"
                )
            )

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
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_variante_codigo_bodega "
                    "ON productos_variantes_stock(codigo_producto, bodega)"
                )
            )
            variante_cols = conn.execute(text("PRAGMA table_info(productos_variantes_stock)")).fetchall()
            variante_col_names = {col[1] for col in variante_cols}
            if variante_cols and "margen_override_pct" not in variante_col_names:
                conn.execute(text("ALTER TABLE productos_variantes_stock ADD COLUMN margen_override_pct REAL"))
            variante_cols = conn.execute(text("PRAGMA table_info(productos_variantes_stock)")).fetchall()
            variante_col_names = {col[1] for col in variante_cols}
            if variante_cols and "precio_publico_neto_override" not in variante_col_names:
                conn.execute(text("ALTER TABLE productos_variantes_stock ADD COLUMN precio_publico_neto_override REAL"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_mov_ingreso_documento ON movimientos_stock(ingreso_documento_id)"))

            ing_items_cols = conn.execute(text("PRAGMA table_info(ingresos_documentos_items)")).fetchall()
            ing_items_col_names = {col[1] for col in ing_items_cols}
            if ing_items_cols and "valor_neto" not in ing_items_col_names:
                conn.execute(text("ALTER TABLE ingresos_documentos_items ADD COLUMN valor_neto REAL"))
            ing_items_cols = conn.execute(text("PRAGMA table_info(ingresos_documentos_items)")).fetchall()
            ing_items_col_names = {col[1] for col in ing_items_cols}
            if ing_items_cols and "margen_pct" not in ing_items_col_names:
                conn.execute(text("ALTER TABLE ingresos_documentos_items ADD COLUMN margen_pct REAL"))
            if ing_items_cols and "precio_venta_neto" not in ing_items_col_names:
                conn.execute(text("ALTER TABLE ingresos_documentos_items ADD COLUMN precio_venta_neto REAL"))

            ing_docs_cols = conn.execute(text("PRAGMA table_info(ingresos_documentos)")).fetchall()
            ing_docs_col_names = {col[1] for col in ing_docs_cols}
            if ing_docs_cols and "metodo_pago" not in ing_docs_col_names:
                conn.execute(text("ALTER TABLE ingresos_documentos ADD COLUMN metodo_pago VARCHAR(120) DEFAULT ''"))
            if ing_docs_cols and "anulado" not in ing_docs_col_names:
                conn.execute(text("ALTER TABLE ingresos_documentos ADD COLUMN anulado BOOLEAN NOT NULL DEFAULT 0"))
            if ing_docs_cols and "anulado_at" not in ing_docs_col_names:
                conn.execute(text("ALTER TABLE ingresos_documentos ADD COLUMN anulado_at DATETIME"))
            if ing_docs_cols and "anulado_por" not in ing_docs_col_names:
                conn.execute(text("ALTER TABLE ingresos_documentos ADD COLUMN anulado_por VARCHAR(100)"))
            if ing_docs_cols and "anulacion_motivo" not in ing_docs_col_names:
                conn.execute(text("ALTER TABLE ingresos_documentos ADD COLUMN anulacion_motivo VARCHAR(255) DEFAULT ''"))
            ing_docs_cols = conn.execute(text("PRAGMA table_info(ingresos_documentos)")).fetchall()
            ing_docs_col_names = {col[1] for col in ing_docs_cols}
            if ing_docs_cols and "total_factura" not in ing_docs_col_names:
                conn.execute(text("ALTER TABLE ingresos_documentos ADD COLUMN total_factura REAL"))
            if ing_docs_cols and "iva_factura" not in ing_docs_col_names:
                conn.execute(text("ALTER TABLE ingresos_documentos ADD COLUMN iva_factura REAL"))

            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS bodegas_catalogo (
                        id INTEGER PRIMARY KEY,
                        nombre VARCHAR(120) NOT NULL UNIQUE,
                        activo BOOLEAN NOT NULL DEFAULT 1,
                        orden INTEGER NOT NULL DEFAULT 0,
                        nota VARCHAR(255) DEFAULT ''
                    )
                    """
                )
            )
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_bodegas_catalogo_nombre ON bodegas_catalogo(nombre)"))

            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS bodega_picking_ventas (
                        id INTEGER PRIMARY KEY,
                        orden_venta_id INTEGER NOT NULL UNIQUE,
                        status VARCHAR(30) NOT NULL DEFAULT 'pendiente',
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL,
                        usuario_creacion VARCHAR(100),
                        usuario_entrega VARCHAR(100),
                        nota VARCHAR(500) DEFAULT ''
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS bodega_picking_venta_lineas (
                        id INTEGER PRIMARY KEY,
                        picking_id INTEGER NOT NULL,
                        codigo_producto VARCHAR(100) NOT NULL,
                        descripcion VARCHAR(255) DEFAULT '',
                        marca VARCHAR(120) DEFAULT '',
                        bodega VARCHAR(120) NOT NULL DEFAULT 'Bodega 1',
                        cantidad_pedida INTEGER NOT NULL DEFAULT 0,
                        cantidad_entregada INTEGER NOT NULL DEFAULT 0,
                        orden_linea INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
            )
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_picking_vent_orden ON bodega_picking_ventas(orden_venta_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_picking_linea_picking ON bodega_picking_venta_lineas(picking_id)"))

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
                ("pago_referencia", "VARCHAR(200)"),
            ]:
                if ventas_documentos_cols and col_name not in ventas_documentos_col_names:
                    conn.execute(text(f"ALTER TABLE ventas_documentos ADD COLUMN {col_name} {col_type}"))
            if ventas_documentos_cols and "monto_saldo_favor" not in ventas_documentos_col_names:
                conn.execute(text("ALTER TABLE ventas_documentos ADD COLUMN monto_saldo_favor REAL NOT NULL DEFAULT 0"))

            ventas_nc_cols = conn.execute(text("PRAGMA table_info(ventas_notas_credito)")).fetchall()
            ventas_nc_col_names = {col[1] for col in ventas_nc_cols}
            for col_name, col_type in [
                ("source_id", "INTEGER"),
                ("source_type", "VARCHAR(40)"),
                ("root_id", "INTEGER"),
                ("modo_liquidacion", "VARCHAR(32) DEFAULT 'saldo_favor'"),
            ]:
                if ventas_nc_cols and col_name not in ventas_nc_col_names:
                    conn.execute(text(f"ALTER TABLE ventas_notas_credito ADD COLUMN {col_name} {col_type}"))

            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_ventas_documentos_tipo_numero ON ventas_documentos(tipo, numero)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_ventas_documentos_source ON ventas_documentos(source_type, source_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_ventas_documentos_root ON ventas_documentos(root_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_ventas_nc_source ON ventas_notas_credito(source_type, source_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_ventas_nc_root ON ventas_notas_credito(root_id)"))

            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS ventas_clientes_saldo_movimientos (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        cliente_id INTEGER NOT NULL,
                        monto REAL NOT NULL,
                        tipo VARCHAR(32) NOT NULL,
                        ref_factura_numero VARCHAR(100),
                        ref_nota_credito_numero VARCHAR(100),
                        razon TEXT,
                        documento_venta_id INTEGER,
                        nota_credito_id INTEGER,
                        created_at DATETIME NOT NULL,
                        usuario VARCHAR(100),
                        FOREIGN KEY (cliente_id) REFERENCES ventas_clientes (id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_vc_saldo_cliente ON "
                    "ventas_clientes_saldo_movimientos (cliente_id)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_vc_saldo_doc ON "
                    "ventas_clientes_saldo_movimientos (documento_venta_id)"
                )
            )

            ventas_items_cols = conn.execute(text("PRAGMA table_info(ventas_documentos_items)")).fetchall()
            ventas_items_col_names = {col[1] for col in ventas_items_cols}
            if ventas_items_cols and "margen_porcentaje" not in ventas_items_col_names:
                conn.execute(text("ALTER TABLE ventas_documentos_items ADD COLUMN margen_porcentaje REAL"))
            if ventas_items_cols and "modelo_linea" not in ventas_items_col_names:
                conn.execute(text("ALTER TABLE ventas_documentos_items ADD COLUMN modelo_linea VARCHAR(255)"))

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

            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS sii_documentos (
                        id INTEGER PRIMARY KEY,
                        tipo_dte VARCHAR(10) NOT NULL,
                        folio INTEGER NOT NULL,
                        fecha_emision DATE,
                        rut_receptor VARCHAR(20),
                        razon_social_receptor VARCHAR(255),
                        monto_neto INTEGER NOT NULL DEFAULT 0,
                        monto_iva INTEGER NOT NULL DEFAULT 0,
                        monto_total INTEGER NOT NULL DEFAULT 0,
                        estado_sii VARCHAR(30) NOT NULL DEFAULT 'PENDIENTE',
                        track_id VARCHAR(120),
                        xml_disponible BOOLEAN NOT NULL DEFAULT 0,
                        sincronizado_en DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        periodo VARCHAR(7),
                        documento_venta_id INTEGER,
                        notas VARCHAR(500),
                        FOREIGN KEY (documento_venta_id) REFERENCES ventas_documentos(id),
                        CONSTRAINT uq_sii_documentos_tipo_folio UNIQUE (tipo_dte, folio)
                    )
                    """
                )
            )
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_sii_documentos_periodo ON sii_documentos(periodo)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_sii_documentos_estado ON sii_documentos(estado_sii)"))
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_sii_documentos_documento_venta "
                    "ON sii_documentos(documento_venta_id)"
                )
            )

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

    # Sesión: cierre automático por inactividad (cualquier petición HTTP actualiza el reloj).
    # ANDES_IDLE_LOGOUT_MINUTES: default 15; use 0 para desactivar (p. ej. desarrollo local).
    _IDLE_LOGOUT_MINUTES = int((os.environ.get("ANDES_IDLE_LOGOUT_MINUTES") or "15").strip() or "15")
    _IDLE_LOGOUT_SECONDS = max(0, _IDLE_LOGOUT_MINUTES * 60)
    app.config["ANDES_IDLE_LOGOUT_MINUTES"] = _IDLE_LOGOUT_MINUTES
    app.config["ANDES_IDLE_LOGOUT_SECONDS"] = _IDLE_LOGOUT_SECONDS
    _IDLE_STATUS_ENDPOINT = "auth.session_idle_status"

    @app.before_request
    def require_login_for_erp():
        """Sin sesion valida, solo se permiten rutas publicas (login, estaticos, etc.)."""
        if is_public_auth_route():
            return None
        if is_logged_in_session():
            return None
        if request.method == "OPTIONS":
            return None
        p = (request.path or "")
        nxt: str | None
        if p in ("/", "/login") or p.rstrip("/") == "" or p.startswith("/login?"):
            nxt = None
        else:
            nxt = safe_next_path(request.full_path)
        is_api_like = (
            request.is_json
            or (request.headers.get("X-Requested-With") or "").lower() == "xmlhttprequest"
            or "/api/" in p
        )
        if is_api_like:
            return (
                jsonify(success=False, message="Debe iniciar sesion.", code="auth_required"),
                401,
            )
        if nxt:
            return redirect(url_for("auth.login", next=nxt))
        return redirect(url_for("auth.login"))

    @app.before_request
    def enforce_session_idle_timeout():
        if _IDLE_LOGOUT_SECONDS <= 0:
            return None
        if request.endpoint in {None, "static"}:
            return None
        if "user" not in session:
            return None
        rol = (session.get("rol") or "").strip().lower()
        if rol == "superadmin":
            return None
        now = time.time()
        raw_last = session.get("_last_activity_ts")
        if raw_last is not None:
            try:
                if now - float(raw_last) > _IDLE_LOGOUT_SECONDS:
                    session.clear()
                    if (
                        request.is_json
                        or (request.headers.get("X-Requested-With") or "").lower() == "xmlhttprequest"
                        or "/api/" in (request.path or "")
                        or request.endpoint == _IDLE_STATUS_ENDPOINT
                    ):
                        return (
                            jsonify(
                                success=False,
                                message="Sesión cerrada por inactividad. Vuelve a iniciar sesión.",
                                code="idle_timeout",
                            ),
                            401,
                        )
                    _n_idle = safe_next_path(request.full_path)
                    if _n_idle:
                        return redirect(url_for("auth.login", expirado=1, next=_n_idle))
                    return redirect(url_for("auth.login", expirado=1))
            except (TypeError, ValueError):
                pass
        # /session/idle-status solo consulta el tiempo: no extiende la sesión
        if request.endpoint == _IDLE_STATUS_ENDPOINT:
            return None
        session["_last_activity_ts"] = now
        session.modified = True
        return None

    @app.before_request
    def update_last_seen_activity() -> None:
        # Skip static/unknown endpoints to reduce noisy commits.
        if request.endpoint in {None, "static"}:
            return
        if request.endpoint == _IDLE_STATUS_ENDPOINT:
            return

        username = session.get("user")
        if not username:
            return

        from app.seguridad.models import Usuario

        try:
            current_user = db.session.query(Usuario).filter_by(usuario=username).first()
            if current_user is None:
                return
            now = datetime.utcnow()
            last = current_user.last_seen
            # Evita un commit a SQLite en cada click (mejora respuesta en formularios como ajuste de stock).
            if last is not None and (now - last).total_seconds() < 45:
                return
            current_user.last_seen = now
            current_user.en_linea = True
            db.session.commit()
        except Exception:
            db.session.rollback()

    @app.before_request
    def enforce_csrf() -> None:
        if request.method in {"GET", "HEAD", "OPTIONS", "TRACE"}:
            return None
        if request.endpoint in {None, "static"}:
            return None
        if request.path.startswith("/chat/api/messages/media/"):
            return None
        if not validate_csrf_request():
            is_ajax = request.is_json or (request.headers.get("X-Requested-With") or "").lower() == "xmlhttprequest"
            if is_ajax or request.path.startswith("/chat/api/") or request.path.startswith("/ventas/api/") or request.path.startswith("/seguridad/api/"):
                return jsonify(success=False, message="Token CSRF inválido o ausente"), 400
            return render_template("login.html", error="La sesión del formulario expiró. Vuelve a intentarlo."), 400
        return None

    # ===============================
    # USUARIO DISPONIBLE EN TEMPLATES
    # ===============================

    @app.context_processor
    def usuario_actual():
        try:
            perms = get_user_permissions(session.get("user"), session.get("rol"))
            if not isinstance(perms, dict):
                perms = dict(DEFAULT_PERMISSIONS)
        except Exception:
            perms = dict(DEFAULT_PERMISSIONS)
        foto_url = None
        try:
            from app.seguridad.models import Usuario
            from app.utils.user_photo import user_photo_url

            uid = session.get("usuario_id")
            if uid:
                u = db.session.get(Usuario, int(uid))
                if u:
                    foto_url = user_photo_url(u)
        except Exception:
            foto_url = None
        return dict(
            usuario_nombre=session.get("user"),
            usuario_rol=session.get("rol"),
            usuario_foto_url=foto_url,
            user_permissions=perms,
            csrf_token=get_csrf_token(),
        )

    @app.context_processor
    def session_idle_client_config():
        if _IDLE_LOGOUT_SECONDS <= 0 or "user" not in session:
            return {"session_idle_client": None}
        if (session.get("rol") or "").strip().lower() == "superadmin":
            return {"session_idle_client": None}
        return {
            "session_idle_client": {
                "statusUrl": url_for("auth.session_idle_status"),
                "warningBeforeSec": 60,
                "pollMs": 30000,
            }
        }

    @app.context_processor
    def inject_partial_flag():
        """Expose _partial=True whenever the request carries the AJAX marker.
        Templates use it for conditional extends:
            {% extends 'base_content.html' if _partial else 'real_base.html' %}
        Ventas routes that set _partial explicitly take precedence because
        render_template() kwargs override context-processor values.
        """
        return {"_partial": request.headers.get("X-Requested-With") == "XMLHttpRequest"}

    @app.context_processor
    def inject_search_lite():
        m = (app.config.get("ANDES_APP_MODE") or "").strip().lower()
        return {"search_lite": m == "search_lite"}

    @app.after_request
    def inject_chat_widget(response):
        # Inject chat globally for authenticated users in HTML responses.
        if (app.config.get("ANDES_APP_MODE") or "").strip().lower() == "search_lite":
            return response
        if (request.path or "").startswith("/m/"):
            return response
        if request.endpoint in {"auth.login", "auth.inicio_seguro"}:
            return response
        if response.status_code != 200:
            return response
        if "user" not in session:
            return response
        try:
            perms = get_user_permissions(session.get("user"), session.get("rol"))
            if not bool(perms.get("mod_chat")):
                return response
        except Exception:
            return response
        if request.endpoint in {
            "productos.ver_producto",
            "productos.historial_producto",
        }:
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

    @app.after_request
    def _apply_http_security_headers(response):
        try:
            apply_security_headers(
                response,
                session_cookie_secure=bool(app.config.get("SESSION_COOKIE_SECURE")),
                hsts_enabled=bool(app.config.get("ANDES_HSTS", True)),
            )
        except Exception:
            pass
        return response

    @app.after_request
    def _mobile_camera_permissions_policy(response):
        """Permite cámara en PWA /m/; apply_security_headers bloquea camera=() globalmente."""
        path = request.path or ""
        if not (path == "/m" or path.startswith("/m/")):
            return response
        try:
            response.headers["Permissions-Policy"] = (
                "camera=(self), microphone=(self), geolocation=(self)"
            )
            response.headers["Feature-Policy"] = "camera 'self'"
        except Exception:
            pass
        return response

    @app.errorhandler(500)
    def handle_internal_error(_error):
        try:
            app.logger.exception("Unhandled 500: %s", request.path)
        except Exception:
            pass
        try:
            db.session.rollback()
        except Exception:
            pass
        try:
            if request.path.startswith("/seguridad/api/") or request.path.startswith("/chat/api/") or request.path.startswith("/ventas/api/"):
                return jsonify(success=False, message="Error interno temporal"), 500
            if request.is_json:
                return jsonify(success=False, message="Error interno temporal"), 500
            if "user" in session and request.endpoint != "auth.inicio_seguro":
                return redirect(url_for("auth.inicio_seguro"))
            return render_template(
                "login.html",
                error="Error temporal del servidor. Intenta nuevamente en unos segundos.",
            ), 500
        except Exception:
            return (
                "Error temporal del servidor. Intenta nuevamente en unos segundos.",
                500,
                {"Content-Type": "text/plain; charset=utf-8"},
            )

    if _app_mode != "search_lite":
        _init_gdrive_backup_scheduler(app)

    return app


# Compatibilidad de despliegue: soporta start command legado "gunicorn app:app".
if (os.environ.get("ANDES_SKIP_AUTO_CREATE_APP") or "").strip().lower() in {"1", "true", "yes"}:
    app = None
else:
    app = create_app()
