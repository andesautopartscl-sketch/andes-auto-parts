from app.extensions import db
from datetime import datetime
from sqlalchemy import UniqueConstraint


class Rol(db.Model):

    __tablename__ = "roles"

    id = db.Column(db.Integer, primary_key=True)

    nombre = db.Column(db.String(50), unique=True)

    nivel = db.Column(db.Integer)

    descripcion = db.Column(db.String(200))


class Usuario(db.Model):

    __tablename__ = "usuarios_sistema"

    id = db.Column(db.Integer, primary_key=True)

    # Info de acceso
    nombre = db.Column(db.String(120))
    usuario = db.Column(db.String(80), unique=True)
    password_hash = db.Column(db.String(200))
    
    # Información personal
    correo = db.Column(db.String(120), unique=True, nullable=True)
    telefono = db.Column(db.String(20), nullable=True)
    direccion = db.Column(db.String(255), nullable=True)
    genero = db.Column(db.String(20), nullable=True)  # "Masculino" o "Femenino"
    fecha_nacimiento = db.Column(db.Date, nullable=True)
    rut = db.Column(db.String(20), unique=True, nullable=True)  # RUT único
    
    # Estados de sesión
    rol_id = db.Column(db.Integer, db.ForeignKey("roles.id"))
    activo = db.Column(db.Boolean, default=True)
    en_linea = db.Column(db.Boolean, default=False)
    intentos_fallidos = db.Column(db.Integer, default=0, nullable=False)
    bloqueado_seguridad = db.Column(db.Boolean, default=False, nullable=False)
    bloqueado_at = db.Column(db.DateTime, nullable=True)
    
    # Timestamps
    ultimo_acceso = db.Column(db.DateTime, nullable=True)
    ultimo_ingreso = db.Column(db.DateTime, nullable=True)  # Nuevo: registro de último login
    last_seen = db.Column(db.DateTime, nullable=True)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)

    rol = db.relationship("Rol")


class PasswordResetRequest(db.Model):
    __tablename__ = "password_reset_requests"

    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey("usuarios_sistema.id"), nullable=True, index=True)
    usuario_solicitado = db.Column(db.String(80), nullable=False, index=True)
    solicitado_por = db.Column(db.String(80), nullable=True)
    motivo = db.Column(db.String(255), nullable=True)
    estado = db.Column(db.String(20), nullable=False, default="pendiente", index=True)
    creado_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    resuelto_at = db.Column(db.DateTime, nullable=True)
    resuelto_por = db.Column(db.String(80), nullable=True)
    nota_admin = db.Column(db.String(255), nullable=True)

    usuario = db.relationship("Usuario", lazy="joined")


class UsuarioPermiso(db.Model):
    __tablename__ = "usuarios_permisos"

    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey("usuarios_sistema.id"), nullable=False, unique=True, index=True)
    ver_finanzas = db.Column(db.Boolean, default=True, nullable=False)
    ver_precio_mayor = db.Column(db.Boolean, default=True, nullable=False)
    actualizado_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    usuario = db.relationship("Usuario", lazy="joined")