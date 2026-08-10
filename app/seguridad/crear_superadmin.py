import logging
import os
import secrets

from werkzeug.security import generate_password_hash

from app.extensions import db
from app.seguridad.models import Rol, Usuario

logger = logging.getLogger(__name__)


def _generar_password_inicial() -> str:
    """Contraseña de un solo uso; se muestra una vez en el log del arranque."""
    return secrets.token_urlsafe(15)


def _admin_config():
    password = (os.environ.get("ANDES_ADMIN_PASSWORD") or "").strip()
    return {
        "username": (os.environ.get("ANDES_ADMIN_USERNAME") or "admin").strip(),
        "password": password or _generar_password_inicial(),
        "password_generada": not password,
        "email": (os.environ.get("ANDES_ADMIN_EMAIL") or "admin@andesautoparts.cl").strip(),
    }


def crear_superadmin():
    """
    Crea el usuario SuperAdmin inicial solo si no hay ninguno en la BD.

    Si ya existe al menos un SuperAdmin (p. ej. albertadmin), no crea cuentas extra.

    Variables de entorno (Render / local):
      ANDES_ADMIN_USERNAME  (default: admin)
      ANDES_ADMIN_PASSWORD  (sin default: se genera una aleatoria y se registra en el log)
      ANDES_ADMIN_EMAIL     (default: admin@andesautoparts.cl)
    """
    rol = Rol.query.filter_by(nombre="SuperAdmin").first()
    if not rol:
        logger.warning("crear_superadmin: rol SuperAdmin no encontrado.")
        return

    superadmin_existente = (
        Usuario.query.filter_by(rol_id=rol.id, activo=True).first()
        or Usuario.query.filter_by(rol_id=rol.id).first()
    )
    if superadmin_existente:
        logger.debug(
            "crear_superadmin: ya hay SuperAdmin ('%s') — sin cambios.",
            superadmin_existente.usuario,
        )
        return

    cfg = _admin_config()
    username = cfg["username"]
    password = cfg["password"]
    email = cfg["email"]

    if not username:
        logger.warning("crear_superadmin: ANDES_ADMIN_USERNAME vacío — no se crea usuario.")
        return
    if not password:
        logger.warning("crear_superadmin: ANDES_ADMIN_PASSWORD vacío — no se crea usuario.")
        return

    existing = Usuario.query.filter_by(usuario=username).first()
    if existing:
        logger.debug("crear_superadmin: usuario '%s' ya existe — sin cambios.", username)
        return

    nuevo = Usuario(
        nombre="Administrador",
        usuario=username,
        correo=email or None,
        password_hash=generate_password_hash(password),
        rol_id=rol.id,
        activo=True,
    )

    db.session.add(nuevo)
    db.session.commit()

    logger.warning("crear_superadmin: SuperAdmin '%s' creado (correo: %s).", username, email)
    if cfg["password_generada"]:
        logger.warning(
            "crear_superadmin: contraseña inicial generada para '%s': %s\n"
            "Cámbiala tras el primer inicio de sesión o define ANDES_ADMIN_PASSWORD.",
            username,
            password,
        )
