"""Validación de credenciales para accesos sensibles del menú de Productos."""

from __future__ import annotations

import unicodedata

from flask import session
from werkzeug.security import check_password_hash

from app.seguridad.models import Usuario


def _normalize_role(value: str | None) -> str:
    role = unicodedata.normalize("NFKD", (value or "").strip().lower())
    return "".join(char for char in role if not unicodedata.combining(char))


def is_product_menu_authorizer_role(role_name: str | None) -> bool:
    return _normalize_role(role_name) in {"dueno", "superadmin"}


def is_product_menu_exempt_session() -> bool:
    """El usuario conectado SuperAdmin no necesita reautenticarse."""
    return _normalize_role(session.get("rol")) == "superadmin"


def authorize_product_menu_credentials(username: str, password: str) -> tuple[bool, str]:
    """Acepta únicamente credenciales activas de Dueño o SuperAdmin."""
    user_name = (username or "").strip()
    secret = password or ""
    if not user_name or not secret:
        return False, "Ingresa el usuario y la clave de autorización."

    user = Usuario.query.filter_by(usuario=user_name).first()
    if user is None:
        return False, "Usuario o clave incorrectos."
    if not bool(user.activo) or bool(user.bloqueado_seguridad):
        return False, "La cuenta autorizadora no está disponible."
    if not is_product_menu_authorizer_role(user.rol.nombre if user.rol else ""):
        return False, "Esta cuenta no tiene permisos de Dueño o SuperAdmin."

    try:
        password_ok = check_password_hash(user.password_hash or "", secret)
    except (TypeError, ValueError):
        password_ok = False
    if not password_ok:
        return False, "Usuario o clave incorrectos."

    return True, f"Acceso autorizado por {user.usuario}."
