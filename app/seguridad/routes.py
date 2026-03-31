from flask import render_template, request, redirect, session, jsonify, current_app
from sqlalchemy import func
from . import seguridad_bp
from .models import Usuario, Rol, UsuarioPermiso
from werkzeug.security import check_password_hash, generate_password_hash
from app.extensions import db
from app.utils.decorators import admin_required
from app.seguridad.models import PasswordResetRequest
from app.utils.datetime_utils import format_utc_to_chile
from app.utils.rut_utils import clean_rut, format_rut, is_valid_rut
from datetime import datetime
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


def _get_delete_context():
    """Build delete-protection context from session + DB state."""
    current_user_id = session.get("usuario_id")
    current_username = session.get("user")
    superadmin_count = db.session.query(Usuario).join(Rol).filter(Rol.nombre == "SuperAdmin").count()
    return current_user_id, current_username, superadmin_count


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
            ver_finanzas=True,
            ver_precio_mayor=True,
        )
        db.session.add(perm)
        db.session.flush()
    return perm


# -----------------------------
# LOGIN LIMPIO (FINAL)
# -----------------------------
@seguridad_bp.route("/login2", methods=["GET", "POST"])
def login():
    print("\n" + "="*60)
    print("LOGIN ROUTE CALLED")
    print("="*60)
    
    error = None

    if request.method == "POST":

        username = request.form.get("username")
        password = request.form.get("password")
        print(f"Login attempt: {username}")

        user = Usuario.query.filter_by(usuario=username).first()

        if user and user.activo:

            if check_password_hash(user.password_hash, password):

                session["usuario_id"] = user.id
                session["usuario_nombre"] = user.nombre
                session["usuario_rol"] = user.rol.nombre

                # Update access timestamps
                print(f"Password correct for user: {username}")
                print(f"Setting ultimo_acceso and ultimo_ingreso to: {datetime.utcnow()}")
                
                user.ultimo_acceso = datetime.utcnow()
                user.ultimo_ingreso = datetime.utcnow()  # Record last login
                user.last_seen = datetime.utcnow()
                user.en_linea = True
                db.session.commit()
                
                print("Database updated:")
                print(f"   - user.ultimo_acceso: {user.ultimo_acceso}")
                print(f"   - user.ultimo_ingreso: {user.ultimo_ingreso}")
                print("="*60 + "\n")
                
                return redirect("/buscar")
            else:
                print(f"Password incorrect for user: {username}")
        else:
            print(f"User not found or inactive: {username}")

        error = "Usuario o contraseña incorrectos"

    return render_template("seguridad/login.html", error=error)


# -----------------------------
# LOGOUT
# -----------------------------
@seguridad_bp.route("/logout")
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

    session.clear()
    return redirect("/login")


# -----------------------------
# LISTAR USUARIOS
# -----------------------------
@seguridad_bp.route("/usuarios")
def usuarios():

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
            "delete_reason": delete_reason
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

    try:
        data = request.get_json()

        print("DATA RECIBIDA:", data)

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
        db.session.commit()

        print("USUARIO CREADO")

        return jsonify({"success": True})

    except Exception as e:
        print("ERROR REAL:", e)
        return jsonify({"success": False, "error": str(e)}), 500

# -----------------------------
# API TOGGLE ACTIVO
# -----------------------------
@seguridad_bp.route("/api/usuarios/toggle/<int:id>", methods=["POST"])
@admin_required
def api_toggle_usuario(id):

    user = Usuario.query.get(id)

    if not user:
        return jsonify({"success": False})

    user.activo = not user.activo
    db.session.commit()

    return jsonify({"success": True})


@seguridad_bp.route("/api/usuarios/unlock/<int:id>", methods=["POST"])
@admin_required
def api_unlock_usuario(id):
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

    db.session.delete(user)
    db.session.commit()

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
    perm = UsuarioPermiso.query.filter_by(usuario_id=user.id).first()
    data["permisos"] = {
        "ver_finanzas": bool(perm.ver_finanzas) if perm is not None else True,
        "ver_precio_mayor": bool(perm.ver_precio_mayor) if perm is not None else True,
    }
    
    print(f"Usuario encontrado: {user.usuario}")
    return jsonify({"success": True, "data": data})


# =====================================================
# 🔥 API EDITAR USUARIO
# =====================================================

@seguridad_bp.route("/api/usuarios/editar/<int:id>", methods=["PUT", "POST"])
@admin_required
def api_editar_usuario(id):
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
            if "ver_finanzas" in perm_payload:
                perm.ver_finanzas = bool(perm_payload.get("ver_finanzas"))
            if "ver_precio_mayor" in perm_payload:
                perm.ver_precio_mayor = bool(perm_payload.get("ver_precio_mayor"))
        
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
    status = (request.args.get("status") or "pendiente").strip().lower()
    q = PasswordResetRequest.query.order_by(PasswordResetRequest.creado_at.desc())
    if status in {"pendiente", "resuelta", "rechazada"}:
        q = q.filter(PasswordResetRequest.estado == status)
    rows = q.limit(120).all()
    return jsonify({"success": True, "items": [_serialize_password_reset_request(x) for x in rows]})


@seguridad_bp.route("/api/password-reset-requests/<int:req_id>/resolve", methods=["POST"])
@admin_required
def api_password_reset_resolve(req_id):
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

@seguridad_bp.route("/usuarios/toggle/<int:id>")
def toggle_usuario(id):

    user = Usuario.query.get_or_404(id)
    user.activo = not user.activo
    db.session.commit()

    return redirect("/usuarios")


@seguridad_bp.route("/usuarios/nuevo", methods=["GET", "POST"])
def nuevo_usuario():

    roles = Rol.query.all()

    if request.method == "POST":

        nombre = request.form["nombre"]
        usuario = request.form["usuario"]
        password = request.form["password"]
        rol_id = request.form["rol"]

        nuevo = Usuario(
            nombre=nombre,
            usuario=usuario,
            password_hash=generate_password_hash(password),
            rol_id=rol_id,
            activo=True
        )

        db.session.add(nuevo)
        db.session.commit()

        return redirect("/usuarios")

    return render_template(
        "seguridad/nuevo_usuario.html",
        roles=roles,
        active_page="seguridad_nuevo_usuario",
    )


@seguridad_bp.route("/usuarios/editar/<int:id>", methods=["GET","POST"])
def editar_usuario(id):

    user = Usuario.query.get_or_404(id)
    roles = Rol.query.all()

    if request.method == "POST":

        user.nombre = request.form["nombre"]
        user.usuario = request.form["usuario"]
        user.rol_id = request.form["rol"]

        if request.form["password"] != "":
            user.password_hash = generate_password_hash(request.form["password"])

        user.activo = True if request.form.get("activo") == "on" else False

        db.session.commit()

        return redirect("/usuarios")

    return render_template(
        "seguridad/editar_usuario.html",
        user=user,
        roles=roles
    )


@seguridad_bp.route("/usuarios/eliminar/<int:id>")
def eliminar_usuario(id):

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

    db.session.delete(user)
    db.session.commit()

    return redirect("/usuarios")