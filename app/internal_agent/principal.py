"""Resolve the human principal and check ERP permissions. The Gateway never grants access."""
from __future__ import annotations

from app.internal_agent.m2m import InternalAuthError
from app.utils.permissions import has_permission


def resolve_actor(username: str) -> tuple[str, str | None]:
    from app.seguridad.models import Usuario

    user = Usuario.query.filter_by(usuario=username).first()
    if user is None or not bool(getattr(user, "activo", True)):
        raise InternalAuthError("principal_invalid", "Human principal is not valid", status=403)
    role_name = None
    if getattr(user, "rol", None) is not None:
        role_name = getattr(user.rol, "nombre", None)
    return username, role_name


def require_mod_productos(username: str, role_name: str | None) -> None:
    if not has_permission(username, role_name, "mod_productos"):
        raise InternalAuthError("permission_denied", "Permission mod_productos is required", status=403)


def require_ver_stock(username: str, role_name: str | None) -> None:
    if not has_permission(username, role_name, "ver_stock"):
        raise InternalAuthError("permission_denied", "Permission ver_stock is required", status=403)


def require_mod_bodega(username: str, role_name: str | None) -> None:
    if not has_permission(username, role_name, "mod_bodega"):
        raise InternalAuthError("permission_denied", "Permission mod_bodega is required", status=403)


def actor_can_view_finanzas(username: str, role_name: str | None) -> bool:
    from app.utils.finance_visibility import user_can_view_finanzas

    return bool(user_can_view_finanzas(username, role_name))


def require_mod_ventas(username: str, role_name: str | None) -> None:
    if not has_permission(username, role_name, "mod_ventas"):
        raise InternalAuthError("permission_denied", "Permission mod_ventas is required", status=403)


def require_mod_dashboard(username: str, role_name: str | None) -> None:
    if not has_permission(username, role_name, "mod_dashboard"):
        raise InternalAuthError("permission_denied", "Permission mod_dashboard is required", status=403)


def actor_can_view_oc_finance(username: str, role_name: str | None) -> bool:
    return bool(has_permission(username, role_name, "ver_precio_costo") or actor_can_view_finanzas(username, role_name))


def actor_can_view_stock(username: str, role_name: str | None) -> bool:
    return bool(has_permission(username, role_name, "ver_stock"))
