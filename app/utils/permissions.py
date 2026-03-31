from __future__ import annotations

from app.extensions import db
from sqlalchemy.exc import SQLAlchemyError


DEFAULT_PERMISSIONS = {
    "ver_finanzas": True,
    "ver_precio_mayor": True,
}


def get_user_permissions(username: str | None, role_name: str | None = None) -> dict:
    # Import local para evitar ciclo: seguridad.routes -> decorators -> permissions -> seguridad.models
    from app.seguridad.models import Usuario, UsuarioPermiso

    if not username:
        return dict(DEFAULT_PERMISSIONS)

    role = (role_name or "").strip().lower()
    if role == "superadmin":
        return {"ver_finanzas": True, "ver_precio_mayor": True}

    try:
        user = db.session.query(Usuario).filter_by(usuario=username).first()
        if user is None:
            return dict(DEFAULT_PERMISSIONS)

        if user.rol and (user.rol.nombre or "").strip().lower() == "superadmin":
            return {"ver_finanzas": True, "ver_precio_mayor": True}

        perm = db.session.query(UsuarioPermiso).filter_by(usuario_id=user.id).first()
        if perm is None:
            return dict(DEFAULT_PERMISSIONS)

        return {
            "ver_finanzas": bool(perm.ver_finanzas),
            "ver_precio_mayor": bool(perm.ver_precio_mayor),
        }
    except SQLAlchemyError:
        # Fallback seguro en entornos con esquema antiguo/desfasado.
        db.session.rollback()
        return dict(DEFAULT_PERMISSIONS)

