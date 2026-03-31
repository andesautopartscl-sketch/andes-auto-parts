from datetime import datetime
from app.extensions import db


ESTADOS_GARANTIA = ["Pendiente", "En revision", "Aprobado", "Rechazado", "Resuelto"]


class Garantia(db.Model):
    """Post-sales warranty registry."""
    __tablename__ = "garantias"

    id = db.Column(db.Integer, primary_key=True)
    numero = db.Column(db.String(60), unique=True, index=True)  # GR-0001
    cliente_id = db.Column(db.Integer, db.ForeignKey("ventas_clientes.id"), index=True)
    cliente_nombre = db.Column(db.String(200), default="")  # snapshot
    producto_codigo = db.Column(db.String(100), default="", index=True)
    producto_descripcion = db.Column(db.String(255), default="")
    documento_id = db.Column(db.Integer, index=True)  # reference to ventas_documentos.id
    documento_numero = db.Column(db.String(60), default="")  # snapshot
    motivo = db.Column(db.Text, default="")
    estado = db.Column(db.String(50), default="Pendiente", index=True)
    fecha = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    usuario = db.Column(db.String(100))

    cliente = db.relationship("Cliente", backref=db.backref("garantias", lazy="dynamic"))
