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
    # Emisor de la factura/boleta asociada al movimiento (gastos, compras de servicio, etc.)
    emisor_nombre = db.Column(db.String(200), default="", nullable=True)
    emisor_rut = db.Column(db.String(24), default="", nullable=True, index=True)
    usuario = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    cuenta = db.relationship("CuentaContable", back_populates="movimientos")


class EmisorContable(db.Model):
    """Directorio de emisores de facturas (gastos/pagos). Separado de clientes y proveedores."""

    __tablename__ = "emisores_contables"

    id = db.Column(db.Integer, primary_key=True)
    rut = db.Column(db.String(20), unique=True, nullable=False, index=True)
    nombre = db.Column(db.String(200), nullable=False)
    giro = db.Column(db.String(200), default="")
    direccion = db.Column(db.String(300), default="")
    region = db.Column(db.String(120), default="")
    comuna = db.Column(db.String(120), default="")
    ciudad = db.Column(db.String(120), default="")
    pais = db.Column(db.String(120), default="Chile")
    telefono = db.Column(db.String(50), default="")
    email = db.Column(db.String(150), default="")
    notas = db.Column(db.Text, default="")
    activo = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    def to_dict(self) -> dict:
        from app.utils.rut_utils import format_rut

        return {
            "id": self.id,
            "emisor_nombre": self.nombre or "",
            "emisor_rut": format_rut(self.rut) if self.rut else "",
            "giro": self.giro or "",
            "direccion": self.direccion or "",
            "region": self.region or "",
            "comuna": self.comuna or "",
            "ciudad": self.ciudad or "",
            "pais": self.pais or "Chile",
            "telefono": self.telefono or "",
            "email": self.email or "",
            "notas": self.notas or "",
            "activo": bool(self.activo),
        }
