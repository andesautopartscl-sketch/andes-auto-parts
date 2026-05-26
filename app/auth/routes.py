from flask import Blueprint, render_template, request, redirect, url_for, session, current_app, jsonify
from werkzeug.security import check_password_hash, generate_password_hash
from datetime import datetime
import time
import traceback

from app.seguridad.models import Usuario as UsuarioSistema, PasswordResetRequest
from app.extensions import db
from app.models import Usuario as UsuarioLegacy, SessionDB
from app.utils.csrf import rotate_csrf_token
from app.utils.audit_log import record_audit_event
from app.utils.login_wall import safe_next_path


auth_bp = Blueprint("auth", __name__)


def _looks_like_werkzeug_hash(value: str | None) -> bool:
    raw = (value or "").strip()
    return raw.startswith("scrypt:") or raw.startswith("pbkdf2:")


def _migrate_plaintext_passwords() -> None:
    try:
        changed = False
        for user in UsuarioSistema.query.all():
            raw = (user.password_hash or "").strip()
            if raw and not _looks_like_werkzeug_hash(raw):
                user.password_hash = generate_password_hash(raw)
                changed = True
        if changed:
            db.session.commit()
    except Exception:
        db.session.rollback()

    legacy_db = SessionDB()
    try:
        changed = False
        for user in legacy_db.query(UsuarioLegacy).all():
            raw = (user.password or "").strip()
            if raw and not _looks_like_werkzeug_hash(raw):
                user.password = generate_password_hash(raw)
                changed = True
        if changed:
            legacy_db.commit()
    except Exception:
        legacy_db.rollback()
    finally:
        legacy_db.close()


# -----------------------------
# PERMISOS
# -----------------------------
def login_required():
    return "user" in session


def admin_required():
    rol_actual = (session.get("rol") or "").strip().lower()
    return "user" in session and "admin" in rol_actual


# -----------------------------
# HOME
# -----------------------------
@auth_bp.route("/")
def home():
    return redirect(url_for("auth.login"))


# -----------------------------
# LOGIN
# -----------------------------
@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    next_url = safe_next_path(
        (request.values.get("next") or request.args.get("next") or "").strip() or None
    )
    if request.method == "GET" and (request.args.get("expirado") or "").strip().lower() in ("1", "true", "si", "yes"):
        error = "Sesión cerrada por inactividad. Vuelve a iniciar sesión."
    try:
        _migrate_plaintext_passwords()
        if request.method == "POST":
            username = request.form.get("username")
            password = request.form.get("password")

            db_uri = current_app.config.get("SQLALCHEMY_DATABASE_URI", "(not configured)")
            print(f"[AUTH LOGIN] SQLALCHEMY_DATABASE_URI: {db_uri}")

            # Unified auth source: same table/model used by /seguridad/api/usuarios
            user = UsuarioSistema.query.filter_by(usuario=username).first()
            password_ok = False
            if user:
                try:
                    password_ok = check_password_hash(user.password_hash or "", password or "")
                except Exception:
                    password_ok = False

            # -----------------------------
            # VALIDAR LOGIN (con bloqueo por intentos)
            # -----------------------------
            if user:
                is_superadmin = bool(user.rol and user.rol.nombre == "SuperAdmin")
                if not user.activo:
                    error = "Usuario inactivo. Contacta al administrador."
                    return render_template("login.html", error=error, next_url=next_url)
                if user.bloqueado_seguridad and not is_superadmin:
                    error = "Usuario bloqueado por seguridad. El administrador debe desbloquear tu cuenta."
                    return render_template("login.html", error=error, next_url=next_url)

            if user and password_ok:

                session["user"] = user.usuario
                session["rol"] = user.rol.nombre if user.rol else ""
                session["usuario_id"] = user.id
                rotate_csrf_token()

                # Update timestamps using the same SQLAlchemy session queried by API routes.
                print("\n" + "="*70)
                print(f"[LOGIN] SUCCESSFUL: {username}")
                print("="*70)
                
                try:
                    print(f"[LOGIN] Updating timestamps for user: {username}")

                    now = datetime.utcnow()
                    user.ultimo_acceso = now
                    user.ultimo_ingreso = now
                    user.last_seen = now
                    user.en_linea = True
                    user.intentos_fallidos = 0
                    user.bloqueado_seguridad = False
                    user.bloqueado_at = None

                    db.session.commit()

                    # Explicit re-query to verify persistence after commit in the same DB.
                    user_after = UsuarioSistema.query.filter_by(usuario=username).first()
                    persisted_ultimo_ingreso = user_after.ultimo_ingreso if user_after else None
                    
                    print(f"[LOGIN] Database updated successfully")
                    print(f"   - ultimo_acceso: SET to current timestamp")
                    print(f"   - ultimo_ingreso: SET to current timestamp")
                    print(f"   - persisted ultimo_ingreso after re-query: {persisted_ultimo_ingreso}")
                    print("="*70 + "\n")

                except Exception as e:
                    db.session.rollback()
                    print(f"[LOGIN][ERROR] Error updating timestamps: {e}")
                    print("="*70 + "\n")

                if next_url:
                    return redirect(next_url)
                return redirect(url_for("productos.buscar"))

            else:
                if user:
                    is_superadmin = bool(user.rol and user.rol.nombre == "SuperAdmin")
                    if not is_superadmin:
                        user.intentos_fallidos = int(user.intentos_fallidos or 0) + 1
                        if user.intentos_fallidos >= 3:
                            user.bloqueado_seguridad = True
                            user.bloqueado_at = datetime.utcnow()
                            user.en_linea = False
                            db.session.commit()
                            error = "Cuenta bloqueada por 3 intentos fallidos. Solicita desbloqueo al administrador."
                            return render_template("login.html", error=error, next_url=next_url)
                        db.session.commit()

                if user is None:
                    # Legacy fallback: keep old 'usuarios' accounts accessible when present.
                    legacy_db = SessionDB()
                    try:
                        legacy_user = legacy_db.query(UsuarioLegacy).filter_by(username=username).first()
                    finally:
                        legacy_db.close()

                    legacy_password_ok = False
                    if legacy_user is not None:
                        raw_pass = legacy_user.password or ""
                        try:
                            legacy_password_ok = check_password_hash(raw_pass, password)
                        except Exception:
                            legacy_password_ok = False

                    if legacy_user is not None and legacy_password_ok:
                        session["user"] = legacy_user.username
                        session["rol"] = legacy_user.rol or ""
                        rotate_csrf_token()
                        if next_url:
                            return redirect(next_url)
                        return redirect(url_for("productos.buscar"))

                error = "Usuario o clave incorrectos"

        return render_template("login.html", error=error, next_url=next_url)
    except Exception as exc:
        db.session.rollback()
        print("[AUTH LOGIN][FATAL]", exc)
        traceback.print_exc()
        return render_template(
            "login.html",
            error="Error temporal al iniciar sesión. Intenta nuevamente en unos segundos.",
            next_url=next_url,
        )


@auth_bp.route("/session/idle-status", methods=["GET"])
def session_idle_status():
    """Solo lectura: no renueva _last_activity_ts (así el polling no mantiene viva la sesión)."""
    max_sec = int(current_app.config.get("ANDES_IDLE_LOGOUT_SECONDS") or 0)
    if max_sec <= 0:
        return jsonify(enabled=False)
    if "user" not in session:
        return jsonify(enabled=True, logged_in=False)
    if (session.get("rol") or "").strip().lower() == "superadmin":
        return jsonify(enabled=True, superadmin=True, remaining_sec=None)
    now = time.time()
    raw_last = session.get("_last_activity_ts")
    if raw_last is None:
        remaining = float(max_sec)
    else:
        try:
            elapsed = now - float(raw_last)
            remaining = max(0.0, float(max_sec) - elapsed)
        except (TypeError, ValueError):
            remaining = float(max_sec)
    return jsonify(enabled=True, remaining_sec=round(remaining, 1), max_sec=max_sec)


@auth_bp.route("/login/password-reset-request", methods=["POST"])
def password_reset_request():
    payload = request.get_json(silent=True) if request.is_json else {}
    username = (request.form.get("username") or payload.get("username") or "").strip()
    motivo = (request.form.get("motivo") or payload.get("motivo") or "").strip()

    # Respuesta deliberadamente genérica para no exponer existencia de usuarios.
    generic_msg = (
        "Solicitud enviada. El administrador revisará el caso y asignará una nueva clave si corresponde."
    )

    if not username:
        return jsonify(success=False, message="Debes indicar tu usuario."), 400

    user = UsuarioSistema.query.filter_by(usuario=username).first()
    if not user or not user.activo:
        return jsonify(success=True, message=generic_msg)

    existente = (
        PasswordResetRequest.query
        .filter_by(usuario_id=user.id, estado="pendiente")
        .first()
    )
    if existente:
        return jsonify(success=True, message=generic_msg)

    req = PasswordResetRequest(
        usuario_id=user.id,
        usuario_solicitado=user.usuario,
        solicitado_por=user.usuario,
        motivo=(motivo or None),
        estado="pendiente",
    )
    db.session.add(req)
    db.session.commit()
    return jsonify(success=True, message=generic_msg)

# -----------------------------
# LOGOUT
# -----------------------------
@auth_bp.route("/logout", methods=["POST"])
def logout():

    username = session.get("user")
    if username:
        user = UsuarioSistema.query.filter_by(usuario=username).first()
        if user:
            user.en_linea = False
            db.session.commit()
    record_audit_event("logout", actor_usuario=username)
    session.clear()

    return redirect(url_for("auth.login"))


@auth_bp.route("/inicio-seguro")
def inicio_seguro():
    if "user" not in session:
        return redirect(url_for("auth.login"))
    return render_template(
        "inicio_seguro.html",
        usuario=session.get("user"),
        rol=session.get("rol"),
    )