"""Autorización del mapa de relaciones: superadmin directo; resto con usuario/clave."""

from __future__ import annotations

from datetime import datetime, timedelta

from flask import session
from werkzeug.security import check_password_hash

from app.seguridad.models import Usuario as UsuarioSistema

RELMAP_SESSION_KEY = "relmap_unlocked_until"
RELMAP_UNLOCK_MINUTES = 30


def is_superadmin_role(rol_nombre: str | None) -> bool:
    return "superadmin" in ((rol_nombre or "").strip().lower())


def is_superadmin_session() -> bool:
    return is_superadmin_role(session.get("rol"))


def relmap_session_unlocked() -> bool:
    until = session.get(RELMAP_SESSION_KEY)
    if until is None:
        return False
    try:
        return float(until) >= datetime.utcnow().timestamp()
    except (TypeError, ValueError):
        return False


def can_access_relmap() -> bool:
    return is_superadmin_session() or relmap_session_unlocked()


def unlock_relmap_session(minutes: int = RELMAP_UNLOCK_MINUTES) -> None:
    session[RELMAP_SESSION_KEY] = (datetime.utcnow() + timedelta(minutes=max(1, int(minutes)))).timestamp()
    session.modified = True


def rol_autoriza_relmap(rol_nombre: str | None) -> bool:
    rol = (rol_nombre or "").strip().lower()
    if not rol:
        return False
    return (
        "superadmin" in rol
        or "admin" in rol
        or "encargado" in rol
        or "subencargado" in rol
        or "sub encargado" in rol
    )


def authorize_relmap_credentials(usuario: str, password: str) -> tuple[bool, str]:
    user_name = (usuario or "").strip()
    pwd = password or ""
    if not user_name or not pwd:
        return False, "Debes indicar usuario y clave de autorización."

    u = UsuarioSistema.query.filter_by(usuario=user_name).first()
    if u is None:
        return False, "Usuario autorizador no válido."
    if not bool(u.activo):
        return False, "Usuario autorizador inactivo."
    if not rol_autoriza_relmap(u.rol.nombre if u.rol else ""):
        return False, "El usuario no tiene permiso para autorizar el mapa de relaciones."

    try:
        ok = check_password_hash(u.password_hash or "", pwd)
    except Exception:
        ok = False
    if not ok:
        return False, "Clave incorrecta para autorización."

    unlock_relmap_session()
    return True, f"Autorizado por {u.usuario}."
