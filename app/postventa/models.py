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
    cliente_rut = db.Column(db.String(40), default="")  # snapshot tributario
    producto_codigo = db.Column(db.String(100), default="", index=True)
    producto_descripcion = db.Column(db.String(255), default="")
    documento_id = db.Column(db.Integer, index=True)  # reference to ventas_documentos.id
    documento_item_id = db.Column(
        db.Integer,
        db.ForeignKey("ventas_documentos_items.id"),
        index=True,
        nullable=True,
    )  # línea de venta vinculada (trazabilidad)
    documento_numero = db.Column(db.String(60), default="")  # snapshot
    nota_credito_id = db.Column(
        db.Integer,
        db.ForeignKey("ventas_notas_credito.id"),
        index=True,
        nullable=True,
    )  # NC que formaliza la devolución (opcional)
    motivo = db.Column(db.Text, default="")
    estado = db.Column(db.String(50), default="Pendiente", index=True)
    fecha = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    usuario = db.Column(db.String(100))

    cliente = db.relationship("Cliente", backref=db.backref("garantias", lazy="dynamic"))
    documento_item = db.relationship(
        "DocumentoVentaItem",
        foreign_keys=[documento_item_id],
    )
    nota_credito = db.relationship(
        "NotaCredito",
        foreign_keys=[nota_credito_id],
    )
