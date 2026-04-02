from functools import wraps
from flask import session, redirect, url_for, request, jsonify
from app.utils.permissions import get_user_permissions

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)
    return decorated_function


def _wants_json_response() -> bool:
    """True si el cliente espera JSON (API, AJAX, previsualización con ajax=1)."""
    if request.is_json or request.method == "POST":
        return True
    if (request.values.get("ajax") or "").strip() == "1":
        return True
    return (request.headers.get("X-Requested-With") or "").lower() == "xmlhttprequest"


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        rol_actual = (session.get("rol") or "").strip().lower()
        es_admin = "admin" in rol_actual

        if "user" not in session or not es_admin:
            if _wants_json_response():
                return jsonify(success=False, message="No autorizado"), 403
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)
    return decorated_function


def permission_required(permission_key: str):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if "user" not in session:
                if request.is_json or request.method == "POST":
                    return jsonify(success=False, message="No autorizado"), 403
                return redirect(url_for("auth.login"))

            perms = get_user_permissions(session.get("user"), session.get("rol"))
            if not perms.get(permission_key, True):
                if request.is_json or request.method == "POST":
                    return jsonify(success=False, message="Permiso denegado"), 403
                return redirect(url_for("productos.buscar"))

            return f(*args, **kwargs)

        return decorated_function

    return decorator