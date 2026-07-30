from __future__ import annotations

from datetime import datetime

from app.extensions import db


class VehiculoVin(db.Model):
    """Registro de unidades por VIN / chasis (no es un producto del catálogo)."""

    __tablename__ = "vehiculos_vin"

    id = db.Column(db.Integer, primary_key=True)
    vin = db.Column(db.String(32), unique=True, index=True, nullable=True)
    chasis = db.Column(db.String(64), index=True, nullable=True)
    marca = db.Column(db.String(80), index=True, default="")
    modelo = db.Column(db.String(160), index=True, default="")
    modelo_completo = db.Column(db.String(200), default="")  # texto original del Excel
    anio = db.Column(db.Integer, nullable=True, index=True)
    motor = db.Column(db.String(120), default="")  # número de motor
    version = db.Column(db.String(120), default="")
    transmision = db.Column(db.String(80), default="")
    cilindrada = db.Column(db.String(40), default="")
    patente = db.Column(db.String(20), default="")
    nombre_china = db.Column(db.String(120), default="")
    imagen_url = db.Column(db.String(500), default="")  # Cloudinary URL o vacío si es archivo local
    notas = db.Column(db.Text, default="")
    fuente = db.Column(db.String(40), default="manual")
    activo = db.Column(db.Boolean, default=True, nullable=False, index=True)
    usuario_alta = db.Column(db.String(100), default="")
    usuario_edicion = db.Column(db.String(100), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    def etiqueta(self) -> str:
        partes = [p for p in (self.marca, self.modelo, str(self.anio or "").strip() or None) if p]
        return " ".join(partes) if partes else (self.vin or self.chasis or f"#{self.id}")
