from flask import Blueprint, render_template, request, redirect, url_for, session, current_app, jsonify
from werkzeug.security import check_password_hash
from datetime import datetime
import traceback

from app.seguridad.models import Usuario as UsuarioSistema, PasswordResetRequest
from app.extensions import db
from app.models import Usuario as UsuarioLegacy, SessionDB


auth_bp = Blueprint("auth", __name__)


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
    try:
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
                    # Compatibilidad defensiva: hashes legados/rotos no deben tumbar login con 500.
                    password_ok = (str(user.password_hash or "") == str(password or ""))

            # -----------------------------
            # VALIDAR LOGIN (con bloqueo por intentos)
            # -----------------------------
            if user:
                is_superadmin = bool(user.rol and user.rol.nombre == "SuperAdmin")
                if not user.activo:
                    error = "Usuario inactivo. Contacta al administrador."
                    return render_template("login.html", error=error)
                if user.bloqueado_seguridad and not is_superadmin:
                    error = "Usuario bloqueado por seguridad. El administrador debe desbloquear tu cuenta."
                    return render_template("login.html", error=error)

            if user and password_ok:

                session["user"] = user.usuario
                session["rol"] = user.rol.nombre if user.rol else ""

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
                            return render_template("login.html", error=error)
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
                        if not legacy_password_ok:
                            legacy_password_ok = (raw_pass == (password or ""))

                    if legacy_user is not None and legacy_password_ok:
                        session["user"] = legacy_user.username
                        session["rol"] = legacy_user.rol or ""
                        return redirect(url_for("productos.buscar"))

                error = "Usuario o clave incorrectos"

        return render_template("login.html", error=error)
    except Exception as exc:
        db.session.rollback()
        print("[AUTH LOGIN][FATAL]", exc)
        traceback.print_exc()
        return render_template(
            "login.html",
            error="Error temporal al iniciar sesión. Intenta nuevamente en unos segundos.",
        )


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
@auth_bp.route("/logout")
def logout():

    username = session.get("user")
    if username:
        user = UsuarioSistema.query.filter_by(usuario=username).first()
        if user:
            user.en_linea = False
            db.session.commit()

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