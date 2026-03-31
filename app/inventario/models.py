from datetime import datetime
from app.extensions import db


class TransferenciaStock(db.Model):
    """Stock transfer between warehouses."""
    __tablename__ = "stock_transferencias"

    id = db.Column(db.Integer, primary_key=True)
    codigo_producto = db.Column(db.String(100), nullable=False, index=True)
    descripcion_producto = db.Column(db.String(255), default="")
    marca = db.Column(db.String(120), default="", index=True)
    cantidad = db.Column(db.Integer, nullable=False)
    bodega_origen = db.Column(db.String(120), nullable=False, index=True)
    bodega_destino = db.Column(db.String(120), nullable=False, index=True)
    fecha = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    usuario = db.Column(db.String(100), index=True)
    observacion = db.Column(db.String(255), default="")
    # References to stock movement log entries
    movimiento_salida_id = db.Column(db.Integer)
    movimiento_entrada_id = db.Column(db.Integer)


class LabelPrintHistory(db.Model):
    """Audit trail for label print operations."""

    __tablename__ = "inventario_label_print_history"

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, nullable=False, index=True)
    product_name = db.Column(db.String(255), nullable=False, index=True)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    user_id = db.Column(db.String(100), nullable=False, index=True)
    date_time = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    document_reference = db.Column(db.String(120), default="", index=True)
