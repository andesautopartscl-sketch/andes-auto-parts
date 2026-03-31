from datetime import datetime
from app.extensions import db


TIPOS_CUENTA = ["Activo", "Pasivo", "Patrimonio", "Ingreso", "Egreso", "Costo"]


class CuentaContable(db.Model):
    """Chart of accounts (plan de cuentas)."""
    __tablename__ = "cuentas_contables"

    id = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(db.String(30), unique=True, nullable=False, index=True)
    nombre = db.Column(db.String(200), nullable=False)
    tipo = db.Column(db.String(50), nullable=False)  # Activo, Pasivo, etc.
    descripcion = db.Column(db.Text, default="")
    activo = db.Column(db.Boolean, default=True)

    movimientos = db.relationship(
        "MovimientoContable", back_populates="cuenta", lazy="dynamic"
    )


class MovimientoContable(db.Model):
    """Accounting journal entry line (libro diario entry)."""
    __tablename__ = "movimientos_contables"

    id = db.Column(db.Integer, primary_key=True)
    fecha = db.Column(db.Date, nullable=False, index=True)
    cuenta_id = db.Column(
        db.Integer, db.ForeignKey("cuentas_contables.id"), nullable=False, index=True
    )
    tipo = db.Column(db.String(10), nullable=False)  # debe / haber
    monto = db.Column(db.Float, nullable=False)
    descripcion = db.Column(db.String(300), default="")
    documento_ref = db.Column(db.String(60), default="", index=True)  # document number
    usuario = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    cuenta = db.relationship("CuentaContable", back_populates="movimientos")
