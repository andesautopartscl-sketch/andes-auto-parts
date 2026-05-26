import os

from werkzeug.security import generate_password_hash

from app.extensions import db
from app.seguridad.models import Rol, Usuario


def _admin_config():
    return {
        "username": (os.environ.get("ANDES_ADMIN_USERNAME") or "admin").strip(),
        "password": (os.environ.get("ANDES_ADMIN_PASSWORD") or "1234").strip(),
        "email": (os.environ.get("ANDES_ADMIN_EMAIL") or "admin@andesautoparts.cl").strip(),
    }


def crear_superadmin():
    """
    Crea el usuario SuperAdmin inicial si no existe el username configurado.

    Variables de entorno (Render / local):
      ANDES_ADMIN_USERNAME  (default: admin)
      ANDES_ADMIN_PASSWORD  (default: 1234)
      ANDES_ADMIN_EMAIL     (default: admin@andesautoparts.cl)
    """
    cfg = _admin_config()
    username = cfg["username"]
    password = cfg["password"]
    email = cfg["email"]

    if not username:
        print("crear_superadmin: ANDES_ADMIN_USERNAME vacío — no se crea usuario.")
        return
    if not password:
        print("crear_superadmin: ANDES_ADMIN_PASSWORD vacío — no se crea usuario.")
        return

    existing = Usuario.query.filter_by(usuario=username).first()
    if existing:
        print(f"crear_superadmin: usuario '{username}' ya existe — sin cambios.")
        return

    rol = Rol.query.filter_by(nombre="SuperAdmin").first()
    if not rol:
        print("crear_superadmin: rol SuperAdmin no encontrado.")
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

    print(f"crear_superadmin: SuperAdmin '{username}' creado (correo: {email}).")
