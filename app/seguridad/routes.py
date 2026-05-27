from flask import render_template, request, redirect, session, jsonify, current_app, url_for, send_file
from sqlalchemy import func, or_
from . import seguridad_bp
from .models import AuditEvent, Usuario, Rol, UsuarioPermiso, UsuarioPermisoDetalle
from werkzeug.security import check_password_hash, generate_password_hash
from app.extensions import db, limiter
from app.utils.decorators import admin_required, login_required
from app.seguridad.models import PasswordResetRequest
from app.chat.models import ChatMessage
from app.utils.datetime_utils import format_utc_to_chile
from app.utils.permissions import ALL_PERMISSION_KEYS, LEGACY_KEY_MAP, PERMISSION_CATALOG
from app.utils.permissions import has_permission
from app.utils.rut_utils import clean_rut, format_rut, is_valid_rut
from app.utils.audit_log import record_audit_event
from app.utils.user_photo import (
    delete_user_photo_file,
    photo_file_path,
    save_user_photo,
    user_has_photo,
    user_photo_url,
)
from app.utils.csrf import rotate_csrf_token
from app.rrhh.models import RRHHContratoAnexo, RRHHPerfil, RRHHLiquidacion, RRHHLiquidacionDetalle, RRHHVacacionRegistro
from datetime import datetime
from collections import Counter
from pathlib import Path
import json
import re


# =====================================================
# 🔥 VALIDATION HELPERS
# =====================================================

def validar_correo(correo):
    """Validate email format"""
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, correo) is not None


def validar_rut(rut):
    """Validate RUT format (Chilean ID)"""
    return is_valid_rut(rut)


def validar_telefono(telefono):
    """Validate phone format"""
    # Basic format: +56 9 XXXX XXXX or 9 XXXX XXXX etc
    pattern = r'^[\d\s\-\+\(\)]{9,20}$'
    return re.match(pattern, telefono) is not None


def _normalized_rut_sql(column):
    return func.upper(func.replace(func.replace(func.coalesce(column, ""), ".", ""), "-", ""))


def _load_chile_geo() -> list[dict]:
    geo_path = Path(__file__).resolve().parent.parent / "ventas" / "data" / "chile_geo.json"
    try:
        return json.loads(geo_path.read_text(encoding="utf-8"))
    except Exception:
        return []


def _compose_address_from_payload(data, fallback=None):
    keys_present = any(k in data for k in ("direccion", "comuna", "ciudad", "region"))
    if not keys_present:
        return fallback
    direccion = str(data.get("direccion") or "").strip()
    comuna = str(data.get("comuna") or data.get("ciudad") or "").strip()
    region = str(data.get("region") or "").strip()
    parts = [p for p in (direccion, comuna, region) if p]
    return ", ".join(parts) if parts else None


def _is_vendedor_role_id(rol_id) -> bool:
    try:
        rid = int(rol_id or 0)
    except Exception:
        return False
    if rid <= 0:
        return False
    rol = Rol.query.get(rid)
    if rol is None:
        return False
    return "vendedor" in ((rol.nombre or "").strip().lower())


def _get_delete_context():
    """Build delete-protection context from session + DB state."""
    current_user_id = session.get("usuario_id")
    current_username = session.get("user")
    superadmin_count = db.session.query(Usuario).join(Rol).filter(Rol.nombre == "SuperAdmin").count()
    return current_user_id, current_username, superadmin_count


def _purge_usuario_dependencies(user_id: int) -> None:
    """Elimina filas que referencian al usuario para que el DELETE del ORM no falle por FK."""
    uid = int(user_id)
    data_root = Path(__file__).resolve().parents[2] / "data"
    db.session.query(ChatMessage).filter(
        (ChatMessage.sender_id == uid) | (ChatMessage.receiver_id == uid)
    ).delete(synchronize_session=False)
    for liq in RRHHLiquidacion.query.filter_by(usuario_id=uid).all():
        RRHHLiquidacionDetalle.query.filter_by(liquidacion_id=liq.id).delete(synchronize_session=False)
    RRHHLiquidacion.query.filter_by(usuario_id=uid).delete(synchronize_session=False)
    RRHHContratoAnexo.query.filter_by(creado_por_usuario_id=uid).update(
        {"creado_por_usuario_id": None},
        synchronize_session=False,
    )
    for an in RRHHContratoAnexo.query.filter_by(usuario_id=uid).all():
        if an.archivo_relpath:
            try:
                fp = (data_root / an.archivo_relpath).resolve()
                if fp.is_file() and str(fp).startswith(str(data_root.resolve())):
                    fp.unlink()
            except OSError:
                pass
        db.session.delete(an)
    RRHHVacacionRegistro.query.filter_by(usuario_id=uid).delete(synchronize_session=False)
    pf = RRHHPerfil.query.filter_by(usuario_id=uid).first()
    if pf and pf.contrato_pdf_relpath:
        try:
            fp = (data_root / pf.contrato_pdf_relpath).resolve()
            if fp.is_file() and str(fp).startswith(str(data_root.resolve())):
                fp.unlink()
        except OSError:
            pass
    RRHHPerfil.query.filter_by(usuario_id=uid).delete(synchronize_session=False)
    UsuarioPermisoDetalle.query.filter_by(usuario_id=uid).delete(synchronize_session=False)
    perm = UsuarioPermiso.query.filter_by(usuario_id=uid).first()
    if perm is not None:
        db.session.delete(perm)
    PasswordResetRequest.query.filter_by(usuario_id=uid).update(
        {"usuario_id": None},
        synchronize_session=False,
    )
    db.session.flush()


def _can_delete_user(target_user, current_user_id=None, current_username=None, superadmin_count=None):
    """Return (allowed, reason) according to integrity rules."""
    if target_user is None:
        return False, "Usuario no encontrado"

    if current_user_id is not None:
        try:
            if target_user.id == int(current_user_id):
                return False, "No puedes eliminar tu propio usuario"
        except (TypeError, ValueError):
            pass

    if current_username and target_user.usuario == current_username:
        return False, "No puedes eliminar tu propio usuario"

    is_superadmin = target_user.rol and target_user.rol.nombre == "SuperAdmin"
    if is_superadmin:
        if superadmin_count is None:
            superadmin_count = db.session.query(Usuario).join(Rol).filter(Rol.nombre == "SuperAdmin").count()
        if superadmin_count <= 1:
            return False, "No se puede eliminar el ultimo SuperAdmin"

    return True, None


def _serialize_password_reset_request(req):
    usuario = req.usuario.usuario if req.usuario is not None else req.usuario_solicitado
    rol = req.usuario.rol.nombre if req.usuario is not None and req.usuario.rol is not None else "-"
    return {
        "id": req.id,
        "usuario": usuario or req.usuario_solicitado,
        "rol": rol,
        "estado": req.estado,
        "motivo": req.motivo or "",
        "solicitado_por": req.solicitado_por or req.usuario_solicitado,
        "creado_at": format_utc_to_chile(req.creado_at),
        "resuelto_at": format_utc_to_chile(req.resuelto_at),
        "resuelto_por": req.resuelto_por or "",
        "nota_admin": req.nota_admin or "",
    }


def _ensure_user_permission_row(user: Usuario) -> UsuarioPermiso:
    perm = UsuarioPermiso.query.filter_by(usuario_id=user.id).first()
    if perm is None:
        perm = UsuarioPermiso(
            usuario_id=user.id,
            ver_finanzas=False,
            ver_precio_mayor=False,
        )
        db.session.add(perm)
        db.session.flush()
    return perm


def _ensure_user_permission_details(user: Usuario) -> list[UsuarioPermisoDetalle]:
    rows = UsuarioPermisoDetalle.query.filter_by(usuario_id=user.id).all()
    by_key = {(r.permiso_key or "").strip(): r for r in rows}
    changed = False
    for key in ALL_PERMISSION_KEYS:
        if key in by_key:
            continue
        item = UsuarioPermisoDetalle(usuario_id=user.id, permiso_key=key, allowed=False)
        db.session.add(item)
        rows.append(item)
        changed = True
    if changed:
        db.session.flush()
    return rows


def _serialize_user_permission_payload(user: Usuario) -> dict:
    out = {k: False for k in ALL_PERMISSION_KEYS}
    for d in _ensure_user_permission_details(user):
        key = (d.permiso_key or "").strip()
        if key:
            out[key] = bool(d.allowed)
    out["ver_finanzas"] = bool(out.get(LEGACY_KEY_MAP.get("ver_finanzas", "mod_finanzas"), False))
    out["ver_precio_mayor"] = bool(out.get(LEGACY_KEY_MAP.get("ver_precio_mayor", "ver_precio_costo"), False))
    return out


# -----------------------------
# LOGIN LIMPIO (FINAL)
# -----------------------------
@seguridad_bp.route("/login2", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def login():
    error = None

    if request.method == "POST":

        username = request.form.get("username")
        password = request.form.get("password")

        user = Usuario.query.filter_by(usuario=username).first() if username else None

        if user and user.activo:
            if user.bloqueado_seguridad:
                error = "Usuario bloqueado por seguridad. El administrador debe desbloquear tu cuenta."
                return render_template("seguridad/login.html", error=error)

            if check_password_hash(user.password_hash, password or ""):
                # Misma forma de sesión que auth.login: permisos, idle timeout, decoradores
                session["user"] = user.usuario
                session["rol"] = user.rol.nombre if user.rol else ""
                session["usuario_id"] = user.id
                session["usuario_nombre"] = user.nombre
                if user.rol:
                    session["usuario_rol"] = user.rol.nombre
                rotate_csrf_token()

                user.ultimo_acceso = datetime.utcnow()
                user.ultimo_ingreso = datetime.utcnow()
                user.last_seen = datetime.utcnow()
                user.en_linea = True
                user.intentos_fallidos = 0
                user.bloqueado_seguridad = False
                user.bloqueado_at = None
                db.session.commit()

                return redirect(url_for("productos.buscar"))
            user.intentos_fallidos = int(user.intentos_fallidos or 0) + 1
            if user.intentos_fallidos >= 3:
                user.bloqueado_seguridad = True
                user.bloqueado_at = datetime.utcnow()
                user.en_linea = False
            db.session.commit()
            if user.bloqueado_seguridad:
                error = "Cuenta bloqueada por 3 intentos fallidos. Solicita desbloqueo al administrador."
                return render_template("seguridad/login.html", error=error)
        else:
            pass

        if error is None:
            error = "Usuario o contraseña incorrectos"

    return render_template("seguridad/login.html", error=error)


# -----------------------------
# LOGOUT
# -----------------------------
@seguridad_bp.route("/logout", methods=["POST"])
@login_required
def logout():

    username = session.get("user")
    user = None
    if username:
        user = Usuario.query.filter_by(usuario=username).first()
    if user is None and session.get("usuario_id"):
        user = db.session.get(Usuario, session.get("usuario_id"))
    if user:
        user.en_linea = False
        db.session.commit()

    actor = username or session.get("usuario_nombre")
    record_audit_event("logout", actor_usuario=actor)
    session.clear()
    return redirect(url_for("auth.login"))


# -----------------------------
# AUDITORÍA (solo SuperAdmin, lectura)
# -----------------------------
@seguridad_bp.route("/auditoria-sesion")
@login_required
def auditoria_sesion():
    if (session.get("rol") or "").strip() != "SuperAdmin":
        return redirect(url_for("productos.buscar"))

    # Filtros (solo UI): búsqueda por texto + filtros por usuario/acción.
    q = (request.args.get("q") or "").strip()
    usuario = (request.args.get("usuario") or "").strip()
    accion = (request.args.get("accion") or "").strip()
    solo_logout = (request.args.get("solo_logout") or "").strip() == "1"

    query = AuditEvent.query
    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(
                AuditEvent.actor_usuario.ilike(like),
                AuditEvent.accion.ilike(like),
                AuditEvent.ip.ilike(like),
                AuditEvent.ruta.ilike(like),
                AuditEvent.detalle.ilike(like),
            )
        )
    if usuario:
        query = query.filter(AuditEvent.actor_usuario.ilike(f"%{usuario}%"))
    if accion:
        query = query.filter(AuditEvent.accion.ilike(f"%{accion}%"))
    if solo_logout:
        query = query.filter(AuditEvent.accion.ilike("%logout%"))

    total_eventos = query.count()
    eventos = query.order_by(AuditEvent.created_at.desc()).limit(300).all()

    # Listas para los <select> usando los eventos efectivamente mostrados.
    available_users = sorted({(e.actor_usuario or "").strip() for e in eventos if (e.actor_usuario or "").strip()})
    available_acciones = sorted({(e.accion or "").strip() for e in eventos if (e.accion or "").strip()})

    # Resumen rápido (chips) basado en los eventos que se están mostrando.
    top_usuarios = Counter([(e.actor_usuario or "").strip() for e in eventos if (e.actor_usuario or "").strip()])
    top_acciones = Counter([(e.accion or "").strip() for e in eventos if (e.accion or "").strip()])
    top_usuarios_list = [(u, c) for u, c in top_usuarios.most_common(6)]
    top_acciones_list = [(a, c) for a, c in top_acciones.most_common(6)]
    return render_template(
        "seguridad/auditoria_sesion.html",
        eventos=eventos,
        q=q,
        usuario=usuario,
        accion=accion,
        solo_logout=solo_logout,
        total_eventos=total_eventos,
        available_users=available_users,
        available_acciones=available_acciones,
        top_usuarios_list=top_usuarios_list,
        top_acciones_list=top_acciones_list,
        active_page="seguridad_auditoria_sesion",
    )


# -----------------------------
# LISTAR USUARIOS
# -----------------------------
@seguridad_bp.route("/usuarios")
@admin_required
def usuarios():
    if not has_permission(session.get("user"), session.get("rol"), "seguridad_gestion_usuarios"):
        return redirect("/login")

    lista_usuarios = Usuario.query.all()

    return render_template(
        "seguridad/usuarios.html",
        usuarios=lista_usuarios,
        active_page="seguridad_usuarios",
    )


# =====================================================
# 🔥 API NUEVA PARA EL MODAL (PASO 1)
# =====================================================

# -----------------------------
# API LISTAR USUARIOS
# -----------------------------
@seguridad_bp.route("/api/usuarios")
@admin_required
def api_usuarios():
    if not has_permission(session.get("user"), session.get("rol"), "seguridad_gestion_usuarios"):
        return jsonify({"success": False, "error": "Permiso denegado"}), 403
    print("\n" + "="*60)
    print("[API] /usuarios CALLED")
    print("="*60)
    print(f"[SEGURIDAD API] SQLALCHEMY_DATABASE_URI: {current_app.config.get('SQLALCHEMY_DATABASE_URI', '(not configured)')}")

    usuarios = Usuario.query.all()
    current_user_id, current_username, superadmin_count = _get_delete_context()

    data = []

    for u in usuarios:
        # Format UTC timestamps to Chile local time for display.
        ultimo_ingreso_fmt = format_utc_to_chile(u.ultimo_ingreso)
        ultimo_acceso_fmt = format_utc_to_chile(u.ultimo_acceso)
        fecha_creacion_fmt = format_utc_to_chile(u.fecha_creacion)
        fecha_nacimiento_fmt = u.fecha_nacimiento.strftime("%d-%m-%Y") if u.fecha_nacimiento else "-"
        
        print(f"[API] {u.usuario}: ultimo_ingreso = {u.ultimo_ingreso} -> formatted: {ultimo_ingreso_fmt}")

        can_delete, delete_reason = _can_delete_user(
            u,
            current_user_id=current_user_id,
            current_username=current_username,
            superadmin_count=superadmin_count,
        )
        can_toggle = True
        toggle_reason = None
        try:
            if current_user_id is not None and int(current_user_id) == int(u.id):
                can_toggle = False
                toggle_reason = "No puedes activar/inactivar tu propio usuario."
        except Exception:
            pass
        if current_username and current_username == (u.usuario or ""):
            can_toggle = False
            toggle_reason = "No puedes activar/inactivar tu propio usuario."
        if u.rol and (u.rol.nombre or "").strip().lower() == "superadmin":
            can_toggle = False
            toggle_reason = "No se puede activar/inactivar un usuario SuperAdmin."
        
        data.append({
            "id": u.id,
            "nombre": u.nombre,
            "usuario": u.usuario,
            "rol": u.rol.nombre if u.rol else "",
            "activo": u.activo,
            "intentos_fallidos": int(u.intentos_fallidos or 0),
            "bloqueado_seguridad": bool(u.bloqueado_seguridad),
            "bloqueado_at": format_utc_to_chile(u.bloqueado_at),
            # New fields
            "correo": u.correo or "-",
            "telefono": u.telefono or "-",
            "direccion": u.direccion or "-",
            "genero": u.genero or "-",
            "fecha_nacimiento": fecha_nacimiento_fmt,
            "rut": format_rut(u.rut) or "-",
            "ultimo_ingreso": ultimo_ingreso_fmt,
            "ultimo_acceso": ultimo_acceso_fmt,
            "fecha_creacion": fecha_creacion_fmt,
            "can_delete": can_delete,
            "delete_reason": delete_reason,
            "can_toggle": can_toggle,
            "toggle_reason": toggle_reason,
            "foto_url": user_photo_url(u),
            "has_foto": user_has_photo(u),
        })

    print(f"[API] Returning {len(data)} usuarios")
    print("="*60 + "\n")
    return jsonify(data)


# -----------------------------
# API CREAR USUARIO
# -----------------------------
@seguridad_bp.route("/api/usuarios/crear", methods=["POST"])
@admin_required
def api_crear_usuario():
    if not has_permission(session.get("user"), session.get("rol"), "seguridad_gestion_usuarios"):
        return jsonify({"success": False, "error": "Permiso denegado"}), 403

    try:
        data = request.get_json()

        if not data:
            return jsonify({"success": False, "error": "Payload vacío"}), 400

        # Validaciones básicas
        for key in ["nombre", "usuario", "password", "rol_id"]:
            if not data.get(key):
                return jsonify({"success": False, "error": f"Falta campo requerido: {key}"}), 400

        if "correo" in data and data["correo"]:
            if not validar_correo(data["correo"]):
                return jsonify({"success": False, "error": "Correo inválido"}), 400

        if "telefono" in data and data["telefono"]:
            if not validar_telefono(data["telefono"]):
                return jsonify({"success": False, "error": "Teléfono inválido"}), 400

        if "genero" in data and data["genero"]:
            if data["genero"] not in ["Masculino", "Femenino"]:
                return jsonify({"success": False, "error": "Género inválido"}), 400

        fecha_nacimiento = None
        if "fecha_nacimiento" in data and data["fecha_nacimiento"]:
            try:
                from datetime import datetime as dt

                fecha_nacimiento = dt.strptime(data["fecha_nacimiento"], "%Y-%m-%d").date()
            except ValueError:
                return jsonify({"success": False, "error": "Fecha inválida (formato: YYYY-MM-DD)"}), 400

        normalized_rut = None
        if "rut" in data and data["rut"]:
            normalized_rut = clean_rut(data["rut"])
            if not validar_rut(normalized_rut):
                return jsonify({"success": False, "error": "RUT inválido (formato: XX.XXX.XXX-K)"}), 400

        nuevo = Usuario(
            nombre=data["nombre"],
            usuario=data["usuario"],
            password_hash=generate_password_hash(data["password"]),
            rol_id=int(data["rol_id"]),
            activo=True
        )

        # Campos extendidos (opcionales)
        if "correo" in data:
            nuevo.correo = data["correo"] or None
        if "telefono" in data:
            nuevo.telefono = data["telefono"] or None
        nuevo.direccion = _compose_address_from_payload(data, fallback=nuevo.direccion)
        if "genero" in data:
            nuevo.genero = data["genero"] or None
        if fecha_nacimiento is not None:
            nuevo.fecha_nacimiento = fecha_nacimiento
        if normalized_rut is not None:
            nuevo.rut = normalized_rut

        db.session.add(nuevo)
        db.session.flush()

        # RRHH perfil (opcional). Se crea siempre para mantener 1:1 y facilitar futuras liquidaciones.
        perfil = RRHHPerfil(usuario_id=nuevo.id)
        perfil.salud_tipo = (data.get("rrhh_salud_tipo") or "").strip().upper()[:20]
        perfil.salud_entidad = (data.get("rrhh_salud_entidad") or "").strip()[:120]
        perfil.salud_numero = (data.get("rrhh_salud_numero") or "").strip()[:60]
        perfil.afp_nombre = (data.get("rrhh_afp_nombre") or "").strip()[:120]
        perfil.banco_nombre = (data.get("rrhh_banco_nombre") or "").strip()[:120]
        perfil.banco_tipo_cuenta = (data.get("rrhh_banco_tipo_cuenta") or "").strip()[:40]
        perfil.banco_numero_cuenta = (data.get("rrhh_banco_numero_cuenta") or "").strip()[:60]
        if not perfil.banco_nombre or not perfil.banco_tipo_cuenta or not perfil.banco_numero_cuenta:
            return jsonify({"success": False, "error": "Banco, tipo de cuenta y N° de cuenta son obligatorios para nómina."}), 400
        afc_raw = data.get("rrhh_afc_afiliado")
        perfil.afc_afiliado = False if str(afc_raw).strip().lower() in {"0", "false", "no", "off"} else True
        perfil.es_vendedor = _is_vendedor_role_id(data.get("rol_id"))
        try:
            cp = float(str(data.get("rrhh_comision_pct") or "0").replace(",", "."))
        except Exception:
            cp = 0.0
        if cp < 0:
            cp = 0.0
        if cp > 100:
            cp = 100.0
        perfil.comision_pct = cp
        db.session.add(perfil)

        # Seed permisos granulares: deny-by-default, excepto SuperAdmin.
        rol_nombre = ""
        try:
            rol_obj = Rol.query.get(int(nuevo.rol_id))
            rol_nombre = (rol_obj.nombre if rol_obj else "") or ""
        except Exception:
            rol_nombre = ""
        is_superadmin = "superadmin" in rol_nombre.strip().lower()

        perm_legacy = _ensure_user_permission_row(nuevo)
        perm_legacy.ver_finanzas = bool(is_superadmin)
        perm_legacy.ver_precio_mayor = bool(is_superadmin)
        for pkey in ALL_PERMISSION_KEYS:
            db.session.add(
                UsuarioPermisoDetalle(
                    usuario_id=nuevo.id,
                    permiso_key=pkey,
                    allowed=bool(is_superadmin),
                )
            )

        db.session.commit()

        current_app.logger.info("Usuario creado: %s", nuevo.usuario)

        return jsonify({"success": True, "id": nuevo.id})

    except Exception as e:
        db.session.rollback()
        print("ERROR REAL:", e)
        return jsonify({"success": False, "error": str(e)}), 500

# -----------------------------
# API TOGGLE ACTIVO
# -----------------------------
@seguridad_bp.route("/api/usuarios/toggle/<int:id>", methods=["POST"])
@admin_required
def api_toggle_usuario(id):
    if not has_permission(session.get("user"), session.get("rol"), "seguridad_gestion_usuarios"):
        return jsonify({"success": False, "error": "Permiso denegado"}), 403

    user = Usuario.query.get(id)

    if not user:
        return jsonify({"success": False})

    current_user_id = session.get("usuario_id")
    current_username = (session.get("user") or "").strip()
    try:
        if current_user_id is not None and int(current_user_id) == int(user.id):
            return jsonify({"success": False, "error": "No puedes cambiar el estado de tu propio usuario."}), 403
    except Exception:
        pass
    if current_username and current_username == (user.usuario or ""):
        return jsonify({"success": False, "error": "No puedes cambiar el estado de tu propio usuario."}), 403
    if user.rol and (user.rol.nombre or "").strip().lower() == "superadmin":
        return jsonify({"success": False, "error": "No se puede activar/inactivar un usuario SuperAdmin."}), 403

    user.activo = not user.activo
    db.session.commit()

    return jsonify({"success": True})


@seguridad_bp.route("/api/usuarios/unlock/<int:id>", methods=["POST"])
@admin_required
def api_unlock_usuario(id):
    if not has_permission(session.get("user"), session.get("rol"), "seguridad_gestion_usuarios"):
        return jsonify({"success": False, "error": "Permiso denegado"}), 403
    user = Usuario.query.get(id)
    if not user:
        return jsonify({"success": False, "error": "Usuario no encontrado"}), 404

    user.bloqueado_seguridad = False
    user.bloqueado_at = None
    user.intentos_fallidos = 0
    if not user.activo:
        user.activo = True
    db.session.commit()
    return jsonify({"success": True})


# -----------------------------
# API ELIMINAR USUARIO
# -----------------------------
@seguridad_bp.route("/api/usuarios/eliminar/<int:id>", methods=["DELETE"])
@admin_required
def api_eliminar_usuario(id):
    if not has_permission(session.get("user"), session.get("rol"), "seguridad_gestion_usuarios"):
        return jsonify({"success": False, "error": "Permiso denegado"}), 403

    user = Usuario.query.get(id)

    if not user:
        return jsonify({"success": False, "error": "Usuario no encontrado"}), 404

    current_user_id, current_username, superadmin_count = _get_delete_context()
    allowed, reason = _can_delete_user(
        user,
        current_user_id=current_user_id,
        current_username=current_username,
        superadmin_count=superadmin_count,
    )
    if not allowed:
        return jsonify({"success": False, "error": reason}), 403

    try:
        delete_user_photo_file(user.id)
        _purge_usuario_dependencies(user.id)
        db.session.delete(user)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("Error eliminando usuario %s", id)
        return jsonify({"success": False, "error": f"No se pudo eliminar: {exc}"}), 500

    return jsonify({"success": True})


# =====================================================# 🔥 API OBTENER ROLES (PARA SELECTS)
# =====================================================

@seguridad_bp.route("/api/roles", methods=["GET"])
@admin_required
def api_roles():
    """Obtener lista de roles para selects"""
    print("Obteniendo lista de roles...")

    roles = Rol.query.order_by(Rol.nivel.asc(), Rol.nombre.asc()).all()

    data = []
    for rol in roles:
        data.append({
            "id": rol.id,
            "nombre": rol.nombre,
            "nivel": rol.nivel,
            "descripcion": rol.descripcion or ""
        })

    print(f"{len(data)} roles encontrados")
    return jsonify(data)


@seguridad_bp.route("/api/geo/chile", methods=["GET"])
def api_geo_chile():
    return jsonify(_load_chile_geo())


# =====================================================# � API OBTENER UN USUARIO (PARA EDITAR)
# =====================================================

@seguridad_bp.route("/api/usuarios/<int:id>", methods=["GET"])
@admin_required
def api_obtener_usuario(id):
    if not has_permission(session.get("user"), session.get("rol"), "seguridad_gestion_usuarios"):
        return jsonify({"success": False, "error": "Permiso denegado"}), 403
    """Obtener datos de un usuario para editarlo"""
    print(f"Obteniendo usuario ID: {id}")
    
    user = Usuario.query.get(id)
    
    if not user:
        print(f"Usuario ID {id} no encontrado")
        return jsonify({"success": False, "error": "Usuario no encontrado"}), 404
    
    # Format dates
    fecha_nacimiento_fmt = user.fecha_nacimiento.strftime("%Y-%m-%d") if user.fecha_nacimiento else ""
    
    data = {
        "id": user.id,
        "nombre": user.nombre,
        "usuario": user.usuario,
        "rol_id": user.rol_id,
        "rol": user.rol.nombre if user.rol else "",
        "activo": user.activo,
        # New fields
        "correo": user.correo or "",
        "telefono": user.telefono or "",
        "direccion": user.direccion or "",
        "genero": user.genero or "",
        "fecha_nacimiento": fecha_nacimiento_fmt,
        "rut": format_rut(user.rut)
    }
    perfil = RRHHPerfil.query.filter_by(usuario_id=user.id).first()
    data["rrhh"] = {
        "salud_tipo": (perfil.salud_tipo if perfil else "") or "",
        "salud_entidad": (perfil.salud_entidad if perfil else "") or "",
        "salud_numero": (perfil.salud_numero if perfil else "") or "",
        "afp_nombre": (perfil.afp_nombre if perfil else "") or "",
        "afc_afiliado": bool(perfil.afc_afiliado) if perfil else True,
        "banco_nombre": (perfil.banco_nombre if perfil else "") or "",
        "banco_tipo_cuenta": (perfil.banco_tipo_cuenta if perfil else "") or "",
        "banco_numero_cuenta": (perfil.banco_numero_cuenta if perfil else "") or "",
        "es_vendedor": bool(perfil.es_vendedor) if perfil else False,
        "comision_pct": float(perfil.comision_pct or 0) if perfil else 0.0,
    }
    data["permisos"] = _serialize_user_permission_payload(user)
    data["permisos_catalogo"] = PERMISSION_CATALOG
    data["foto_url"] = user_photo_url(user)
    data["has_foto"] = user_has_photo(user)
    
    print(f"Usuario encontrado: {user.usuario}")
    return jsonify({"success": True, "data": data})


@seguridad_bp.route("/archivo/foto-usuario/<int:uid>")
@login_required
def usuario_foto(uid: int):
    user = Usuario.query.get(uid)
    if not user or not user_has_photo(user):
        return ("", 404)
    path = photo_file_path(uid)
    if not path.is_file():
        return ("", 404)
    response = send_file(path, mimetype="image/jpeg", max_age=0)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response


@seguridad_bp.route("/api/usuarios/<int:uid>/foto", methods=["POST"])
@admin_required
def api_usuario_subir_foto(uid: int):
    if not has_permission(session.get("user"), session.get("rol"), "seguridad_gestion_usuarios"):
        return jsonify({"success": False, "error": "Permiso denegado"}), 403
    user = Usuario.query.get(uid)
    if not user:
        return jsonify({"success": False, "error": "Usuario no encontrado"}), 404
    file = request.files.get("foto")
    ok, msg = save_user_photo(uid, file)
    if not ok:
        return jsonify({"success": False, "error": msg}), 400
    user.foto_perfil = f"{uid}.jpg"
    db.session.commit()
    return jsonify({"success": True, "foto_url": user_photo_url(user)})


@seguridad_bp.route("/api/usuarios/<int:uid>/foto", methods=["DELETE"])
@admin_required
def api_usuario_eliminar_foto(uid: int):
    if not has_permission(session.get("user"), session.get("rol"), "seguridad_gestion_usuarios"):
        return jsonify({"success": False, "error": "Permiso denegado"}), 403
    user = Usuario.query.get(uid)
    if not user:
        return jsonify({"success": False, "error": "Usuario no encontrado"}), 404
    delete_user_photo_file(uid)
    user.foto_perfil = None
    db.session.commit()
    return jsonify({"success": True})


# =====================================================
# 🔥 API EDITAR USUARIO
# =====================================================

@seguridad_bp.route("/api/usuarios/editar/<int:id>", methods=["PUT", "POST"])
@admin_required
def api_editar_usuario(id):
    if not has_permission(session.get("user"), session.get("rol"), "seguridad_gestion_permisos"):
        return jsonify({"success": False, "error": "Permiso denegado"}), 403
    """Editar datos de un usuario"""
    print(f"Editando usuario ID: {id}")
    
    user = Usuario.query.get(id)
    
    if not user:
        print(f"Usuario ID {id} no encontrado")
        return jsonify({"success": False, "error": "Usuario no encontrado"}), 404
    
    try:
        data = request.get_json()
        
        print(f"Datos recibidos: {list(data.keys())}")
        
        # Proteger superadmin
        if user.usuario == "albert" and "usuario" in data and data["usuario"] != "albert":
            print("Intento de cambiar nombre del superadmin bloqueado")
            return jsonify({"success": False, "error": "No se puede modificar al superadmin"})
        
        # Actualizar campos básicos
        if "nombre" in data and data["nombre"]:
            user.nombre = data["nombre"]
            print(f"   - nombre: {data['nombre']}")
        
        if "usuario" in data and data["usuario"]:
            # Verificar que el nuevo usuario no exista
            existing = Usuario.query.filter_by(usuario=data["usuario"]).first()
            if existing and existing.id != id:
                print(f"El usuario {data['usuario']} ya existe")
                return jsonify({"success": False, "error": "El usuario ya existe"}), 400
            user.usuario = data["usuario"]
            print(f"   - usuario: {data['usuario']}")
        
        # Actualizar campos nuevos
        if "correo" in data and data["correo"]:
            if not validar_correo(data["correo"]):
                return jsonify({"success": False, "error": "Correo inválido"}), 400
            # Verificar que no exista otro usuario con ese correo
            existing_email = Usuario.query.filter_by(correo=data["correo"]).first()
            if existing_email and existing_email.id != id:
                return jsonify({"success": False, "error": "El correo ya existe"}), 400
            user.correo = data["correo"]
            print(f"   - correo: {data['correo']}")
        elif "correo" in data and data["correo"] == "":
            user.correo = None
        
        if "telefono" in data and data["telefono"]:
            if not validar_telefono(data["telefono"]):
                return jsonify({"success": False, "error": "Teléfono inválido"}), 400
            user.telefono = data["telefono"]
            print(f"   - telefono: {data['telefono']}")
        elif "telefono" in data and data["telefono"] == "":
            user.telefono = None
        
        if any(k in data for k in ("direccion", "comuna", "ciudad", "region")):
            user.direccion = _compose_address_from_payload(data, fallback=user.direccion)
            print(f"   - direccion: {user.direccion}")
        
        if "genero" in data:
            if data["genero"] and data["genero"] not in ["Masculino", "Femenino"]:
                return jsonify({"success": False, "error": "Género inválido"}), 400
            user.genero = data["genero"] if data["genero"] else None
            print(f"   - genero: {user.genero}")
        
        if "fecha_nacimiento" in data and data["fecha_nacimiento"]:
            try:
                from datetime import datetime as dt
                fecha = dt.strptime(data["fecha_nacimiento"], "%Y-%m-%d").date()
                user.fecha_nacimiento = fecha
                print(f"   - fecha_nacimiento: {fecha}")
            except ValueError:
                return jsonify({"success": False, "error": "Fecha inválida (formato: YYYY-MM-DD)"}), 400
        elif "fecha_nacimiento" in data and data["fecha_nacimiento"] == "":
            user.fecha_nacimiento = None
        
        if "rut" in data and data["rut"]:
            normalized_rut = clean_rut(data["rut"])
            if not validar_rut(normalized_rut):
                return jsonify({"success": False, "error": "RUT inválido (formato: XX.XXX.XXX-K)"}), 400
            # Verificar que no exista otro usuario con ese RUT
            existing_rut = (
                Usuario.query
                .filter(_normalized_rut_sql(Usuario.rut) == normalized_rut.upper())
                .first()
            )
            if existing_rut and existing_rut.id != id:
                return jsonify({"success": False, "error": "El RUT ya existe"}), 400
            user.rut = normalized_rut
            print(f"   - rut: {normalized_rut}")
        elif "rut" in data and data["rut"] == "":
            user.rut = None
        
        if "rol_id" in data:
            user.rol_id = int(data["rol_id"])
            print(f"   - rol_id: {data['rol_id']}")
        
        if "password" in data and data["password"]:
            user.password_hash = generate_password_hash(data["password"])
            print(f"   - password: ***")
        
        if "activo" in data:
            user.activo = bool(data["activo"])
            print(f"   - activo: {user.activo}")

        if "permisos" in data and isinstance(data["permisos"], dict):
            perm_payload = data["permisos"] or {}
            perm = _ensure_user_permission_row(user)
            details = _ensure_user_permission_details(user)
            by_key = {(d.permiso_key or "").strip(): d for d in details}
            for key in ALL_PERMISSION_KEYS:
                if key not in perm_payload:
                    continue
                item = by_key.get(key)
                if item is None:
                    item = UsuarioPermisoDetalle(usuario_id=user.id, permiso_key=key, allowed=bool(perm_payload.get(key)))
                    db.session.add(item)
                    by_key[key] = item
                else:
                    item.allowed = bool(perm_payload.get(key))

            # Compatibilidad tabla legacy.
            perm.ver_finanzas = bool(perm_payload.get("mod_finanzas", perm_payload.get("ver_finanzas", False)))
            perm.ver_precio_mayor = bool(perm_payload.get("ver_precio_costo", perm_payload.get("ver_precio_mayor", False)))

        # Datos RRHH (opcionales)
        if any(k in data for k in (
            "rrhh_salud_tipo", "rrhh_salud_entidad", "rrhh_salud_numero",
            "rrhh_afp_nombre", "rrhh_afc_afiliado",
            "rrhh_banco_nombre", "rrhh_banco_tipo_cuenta", "rrhh_banco_numero_cuenta",
            "rrhh_es_vendedor", "rrhh_comision_pct",
        )):
            perfil = RRHHPerfil.query.filter_by(usuario_id=user.id).first()
            if perfil is None:
                perfil = RRHHPerfil(usuario_id=user.id)
                db.session.add(perfil)
            if "rrhh_salud_tipo" in data:
                perfil.salud_tipo = (data.get("rrhh_salud_tipo") or "").strip().upper()[:20]
            if "rrhh_salud_entidad" in data:
                perfil.salud_entidad = (data.get("rrhh_salud_entidad") or "").strip()[:120]
            if "rrhh_salud_numero" in data:
                perfil.salud_numero = (data.get("rrhh_salud_numero") or "").strip()[:60]
            if "rrhh_afp_nombre" in data:
                perfil.afp_nombre = (data.get("rrhh_afp_nombre") or "").strip()[:120]
            if "rrhh_banco_nombre" in data:
                perfil.banco_nombre = (data.get("rrhh_banco_nombre") or "").strip()[:120]
            if "rrhh_banco_tipo_cuenta" in data:
                perfil.banco_tipo_cuenta = (data.get("rrhh_banco_tipo_cuenta") or "").strip()[:40]
            if "rrhh_banco_numero_cuenta" in data:
                perfil.banco_numero_cuenta = (data.get("rrhh_banco_numero_cuenta") or "").strip()[:60]
            if not (perfil.banco_nombre or "").strip() or not (perfil.banco_tipo_cuenta or "").strip() or not (perfil.banco_numero_cuenta or "").strip():
                return jsonify({"success": False, "error": "Banco, tipo de cuenta y N° de cuenta son obligatorios para nómina."}), 400
            if "rrhh_afc_afiliado" in data:
                perfil.afc_afiliado = bool(data.get("rrhh_afc_afiliado"))
            # Regla de negocio: ser vendedor depende del rol seleccionado.
            perfil.es_vendedor = _is_vendedor_role_id(data.get("rol_id", user.rol_id))
            if "rrhh_comision_pct" in data:
                try:
                    cp = float(str(data.get("rrhh_comision_pct") or "0").replace(",", "."))
                except Exception:
                    cp = 0.0
                if cp < 0:
                    cp = 0.0
                if cp > 100:
                    cp = 100.0
                perfil.comision_pct = cp
        
        db.session.commit()
        print(f"Usuario {user.usuario} actualizado correctamente")
        
        return jsonify({"success": True, "message": "Usuario actualizado correctamente"})
    
    except Exception as e:
        print(f"Error al editar usuario: {e}")
        import traceback
        traceback.print_exc()
        db.session.rollback()
        return jsonify({"success": False, "error": str(e)}), 500


@seguridad_bp.route("/api/password-reset-requests", methods=["GET"])
@admin_required
def api_password_reset_requests():
    if not has_permission(session.get("user"), session.get("rol"), "seguridad_reset_password"):
        return jsonify({"success": False, "error": "Permiso denegado"}), 403
    status = (request.args.get("status") or "pendiente").strip().lower()
    q = PasswordResetRequest.query.order_by(PasswordResetRequest.creado_at.desc())
    if status in {"pendiente", "resuelta", "rechazada"}:
        q = q.filter(PasswordResetRequest.estado == status)
    rows = q.limit(120).all()
    return jsonify({"success": True, "items": [_serialize_password_reset_request(x) for x in rows]})


@seguridad_bp.route("/api/password-reset-requests/<int:req_id>/resolve", methods=["POST"])
@admin_required
def api_password_reset_resolve(req_id):
    if not has_permission(session.get("user"), session.get("rol"), "seguridad_reset_password"):
        return jsonify({"success": False, "error": "Permiso denegado"}), 403
    data = request.get_json(silent=True) or {}
    action = (data.get("action") or "approve").strip().lower()
    new_password = (data.get("new_password") or "").strip()
    note = (data.get("note") or "").strip()

    req = PasswordResetRequest.query.get(req_id)
    if not req:
        return jsonify({"success": False, "error": "Solicitud no encontrada"}), 404
    if req.estado != "pendiente":
        return jsonify({"success": False, "error": "La solicitud ya fue procesada"}), 400

    admin_user = (session.get("user") or "").strip() or "admin"

    if action == "reject":
        req.estado = "rechazada"
        req.resuelto_at = datetime.utcnow()
        req.resuelto_por = admin_user
        req.nota_admin = note or "Solicitud rechazada por administración"
        db.session.commit()
        return jsonify({"success": True})

    if action != "approve":
        return jsonify({"success": False, "error": "Acción inválida"}), 400
    if len(new_password) < 6:
        return jsonify({"success": False, "error": "La nueva contraseña debe tener al menos 6 caracteres"}), 400

    target_user = req.usuario
    if target_user is None:
        target_user = Usuario.query.filter_by(usuario=req.usuario_solicitado).first()
    if target_user is None:
        return jsonify({"success": False, "error": "Usuario asociado no encontrado"}), 404

    target_user.password_hash = generate_password_hash(new_password)
    req.estado = "resuelta"
    req.resuelto_at = datetime.utcnow()
    req.resuelto_por = admin_user
    req.nota_admin = note or "Contraseña reasignada por administración"
    db.session.commit()
    return jsonify({"success": True})

@seguridad_bp.route("/usuarios/toggle/<int:id>", methods=["POST"])
@admin_required
def toggle_usuario(id):
    if not has_permission(session.get("user"), session.get("rol"), "seguridad_gestion_usuarios"):
        return redirect("/login")

    user = Usuario.query.get_or_404(id)
    current_user_id = session.get("usuario_id")
    current_username = (session.get("user") or "").strip()
    try:
        if current_user_id is not None and int(current_user_id) == int(user.id):
            return redirect("/usuarios")
    except Exception:
        pass
    if current_username and current_username == (user.usuario or ""):
        return redirect("/usuarios")
    if user.rol and (user.rol.nombre or "").strip().lower() == "superadmin":
        return redirect("/usuarios")
    user.activo = not user.activo
    db.session.commit()

    return redirect("/usuarios")


@seguridad_bp.route("/usuarios/nuevo", methods=["GET", "POST"])
@admin_required
def nuevo_usuario():
    if not has_permission(session.get("user"), session.get("rol"), "seguridad_gestion_usuarios"):
        return redirect("/login")

    roles = Rol.query.all()

    if request.method == "POST":

        nombre = request.form["nombre"]
        usuario = request.form["usuario"]
        password = request.form["password"]
        rol_id = request.form["rol"]

        existing = Usuario.query.filter_by(usuario=usuario).first()
        if existing:
            flash("El usuario ya existe.", "error")
            return redirect("/usuarios/nuevo")
        if not password or len(password) < 6:
            flash("La contraseña debe tener al menos 6 caracteres.", "error")
            return redirect("/usuarios/nuevo")

        nuevo = Usuario(
            nombre=nombre,
            usuario=usuario,
            password_hash=generate_password_hash(password),
            rol_id=rol_id,
            activo=True
        )

        db.session.add(nuevo)
        db.session.flush()

        # RRHH perfil (opcional)
        perfil = RRHHPerfil(usuario_id=nuevo.id)
        perfil.salud_tipo = (request.form.get("rrhh_salud_tipo") or "").strip().upper()[:20]
        perfil.salud_entidad = (request.form.get("rrhh_salud_entidad") or "").strip()[:120]
        perfil.salud_numero = (request.form.get("rrhh_salud_numero") or "").strip()[:60]
        perfil.afp_nombre = (request.form.get("rrhh_afp_nombre") or "").strip()[:120]
        perfil.banco_nombre = (request.form.get("rrhh_banco_nombre") or "").strip()[:120]
        perfil.banco_tipo_cuenta = (request.form.get("rrhh_banco_tipo_cuenta") or "").strip()[:40]
        perfil.banco_numero_cuenta = (request.form.get("rrhh_banco_numero_cuenta") or "").strip()[:60]
        if not perfil.banco_nombre or not perfil.banco_tipo_cuenta or not perfil.banco_numero_cuenta:
            flash("Banco, tipo de cuenta y N° de cuenta son obligatorios para nómina.", "error")
            return redirect("/usuarios/nuevo")
        perfil.afc_afiliado = bool(request.form.get("rrhh_afc_afiliado") == "on")
        perfil.es_vendedor = _is_vendedor_role_id(rol_id)
        try:
            cp = float(str(request.form.get("rrhh_comision_pct") or "0").replace(",", "."))
        except Exception:
            cp = 0.0
        if cp < 0:
            cp = 0.0
        if cp > 100:
            cp = 100.0
        perfil.comision_pct = cp
        db.session.add(perfil)

        # Seed permisos granulares: deny-by-default excepto SuperAdmin (misma regla que API).
        rol_nombre = ""
        try:
            rol_obj = Rol.query.get(int(nuevo.rol_id))
            rol_nombre = (rol_obj.nombre if rol_obj else "") or ""
        except Exception:
            rol_nombre = ""
        is_superadmin = "superadmin" in rol_nombre.strip().lower()
        perm_legacy = _ensure_user_permission_row(nuevo)
        perm_legacy.ver_finanzas = bool(is_superadmin)
        perm_legacy.ver_precio_mayor = bool(is_superadmin)
        for pkey in ALL_PERMISSION_KEYS:
            db.session.add(
                UsuarioPermisoDetalle(
                    usuario_id=nuevo.id,
                    permiso_key=pkey,
                    allowed=bool(is_superadmin),
                )
            )
        db.session.commit()

        return redirect("/usuarios")

    return render_template(
        "seguridad/nuevo_usuario.html",
        roles=roles,
        active_page="seguridad_nuevo_usuario",
    )


@seguridad_bp.route("/usuarios/editar/<int:id>", methods=["GET","POST"])
@admin_required
def editar_usuario(id):
    if not has_permission(session.get("user"), session.get("rol"), "seguridad_gestion_permisos"):
        return redirect("/login")

    user = Usuario.query.get_or_404(id)
    roles = Rol.query.all()

    if request.method == "POST":

        nombre = request.form["nombre"]
        usuario = request.form["usuario"]
        # Proteger superadmin "albert" (misma regla que API).
        if user.usuario == "albert" and usuario != "albert":
            flash("No se puede modificar al superadmin.", "error")
            return redirect(f"/usuarios/editar/{id}")
        if usuario and usuario != (user.usuario or ""):
            existing = Usuario.query.filter_by(usuario=usuario).first()
            if existing and existing.id != id:
                flash("El usuario ya existe.", "error")
                return redirect(f"/usuarios/editar/{id}")

        user.nombre = nombre
        user.usuario = usuario
        user.rol_id = request.form["rol"]

        if request.form["password"] != "":
            if len(request.form["password"]) < 6:
                flash("La contraseña debe tener al menos 6 caracteres.", "error")
                return redirect(f"/usuarios/editar/{id}")
            user.password_hash = generate_password_hash(request.form["password"])

        user.activo = True if request.form.get("activo") == "on" else False

        # RRHH perfil (opcional)
        perfil = RRHHPerfil.query.filter_by(usuario_id=user.id).first()
        if perfil is None:
            perfil = RRHHPerfil(usuario_id=user.id)
            db.session.add(perfil)
        perfil.salud_tipo = (request.form.get("rrhh_salud_tipo") or "").strip().upper()[:20]
        perfil.salud_entidad = (request.form.get("rrhh_salud_entidad") or "").strip()[:120]
        perfil.salud_numero = (request.form.get("rrhh_salud_numero") or "").strip()[:60]
        perfil.afp_nombre = (request.form.get("rrhh_afp_nombre") or "").strip()[:120]
        perfil.banco_nombre = (request.form.get("rrhh_banco_nombre") or "").strip()[:120]
        perfil.banco_tipo_cuenta = (request.form.get("rrhh_banco_tipo_cuenta") or "").strip()[:40]
        perfil.banco_numero_cuenta = (request.form.get("rrhh_banco_numero_cuenta") or "").strip()[:60]
        if not perfil.banco_nombre or not perfil.banco_tipo_cuenta or not perfil.banco_numero_cuenta:
            flash("Banco, tipo de cuenta y N° de cuenta son obligatorios para nómina.", "error")
            return redirect(f"/usuarios/editar/{id}")
        perfil.afc_afiliado = bool(request.form.get("rrhh_afc_afiliado") == "on")
        perfil.es_vendedor = _is_vendedor_role_id(request.form.get("rol"))
        try:
            cp = float(str(request.form.get("rrhh_comision_pct") or "0").replace(",", "."))
        except Exception:
            cp = 0.0
        if cp < 0:
            cp = 0.0
        if cp > 100:
            cp = 100.0
        perfil.comision_pct = cp

        db.session.commit()

        return redirect("/usuarios")

    rrhh = RRHHPerfil.query.filter_by(usuario_id=user.id).first()
    return render_template(
        "seguridad/editar_usuario.html",
        user=user,
        roles=roles,
        rrhh=rrhh,
    )


@seguridad_bp.route("/usuarios/eliminar/<int:id>", methods=["POST"])
@admin_required
def eliminar_usuario(id):
    if not has_permission(session.get("user"), session.get("rol"), "seguridad_gestion_usuarios"):
        return redirect("/login")

    user = Usuario.query.get_or_404(id)

    current_user_id, current_username, superadmin_count = _get_delete_context()
    allowed, _ = _can_delete_user(
        user,
        current_user_id=current_user_id,
        current_username=current_username,
        superadmin_count=superadmin_count,
    )
    if not allowed:
        return redirect("/usuarios")

    try:
        _purge_usuario_dependencies(user.id)
        db.session.delete(user)
        db.session.commit()
    except Exception:
        db.session.rollback()
        return redirect("/usuarios")

    return redirect("/usuarios")