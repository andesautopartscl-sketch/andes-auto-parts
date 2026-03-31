from datetime import datetime
from app.extensions import db


ESTADOS_OPORTUNIDAD = ["Nueva", "En proceso", "Ganada", "Perdida"]


class Oportunidad(db.Model):
    """CRM light: sales opportunities."""
    __tablename__ = "oportunidades"

    id = db.Column(db.Integer, primary_key=True)
    cliente_id = db.Column(db.Integer, db.ForeignKey("ventas_clientes.id"), index=True)
    cliente_nombre = db.Column(db.String(200), default="")  # snapshot
    descripcion = db.Column(db.Text, default="")
    monto_estimado = db.Column(db.Float, default=0.0)
    estado = db.Column(db.String(50), default="Nueva", index=True)
    fecha_seguimiento = db.Column(db.Date, index=True)
    usuario = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    cliente = db.relationship("Cliente", backref=db.backref("oportunidades", lazy="dynamic"))
