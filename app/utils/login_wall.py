from __future__ import annotations

# Rutas cuyo view no requieren session["user"].
PUBLIC_AUTH_ENDPOINTS: frozenset[str] = frozenset(
    {
        "static",
        "auth.home",
        "auth.login",
        "auth.session_idle_status",
        "auth.password_reset_request",
        "seguridad.login",
        "admin.sync_cloudinary_urls",
        "admin.sync_oem_despiece",
    }
)
_SAFE_NEXT_MAX = 2000


def is_logged_in_session() -> bool:
    from flask import session

    return bool((session.get("user") or "").strip())


def safe_next_path(value: str | None) -> str | None:
    """Evita open redirect. Solo path + query de esta misma app."""
    if value is None:
        return None
    v = (value or "").strip()
    if not v or v[0] != "/":
        return None
    if v.startswith("//"):
        return None
    if "://" in v or "\n" in v or "\r" in v or "\\" in v:
        return None
    if "@" in v:
        return None
    if len(v) > _SAFE_NEXT_MAX:
        return None
    return v


def is_public_auth_route() -> bool:
    from flask import request

    path = request.path or ""
    if path.startswith("/static/"):
        return True
    ep = request.endpoint
    if ep in PUBLIC_AUTH_ENDPOINTS:
        return True
    if ep in {None, "static"}:
        if path == "/favicon.ico" or path.startswith("/favicon."):
            return True
    return False
