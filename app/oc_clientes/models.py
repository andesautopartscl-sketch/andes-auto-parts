from datetime import datetime

from app.extensions import db

OC_ESTADOS = ("recibida", "entregada", "pagada", "anulada")

OC_ESTADO_LABELS = {
    "recibida": "Recibida",
    "entregada": "Entregada - pendiente de pago",
    "pagada": "Pagada",
    "anulada": "Anulada",
}


def oc_estado_label(estado: str | None) -> str:
    key = (estado or "").strip().lower()
    return OC_ESTADO_LABELS.get(key, estado or "—")


class OcVendedorCatalogo(db.Model):
    """Catálogo de vendedores que emiten OC de clientes (nombre único normalizado)."""

    __tablename__ = "oc_vendedores_catalogo"

    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(120), unique=True, nullable=False, index=True)
    activo = db.Column(db.Boolean, nullable=False, default=True)
    orden = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class OrdenCompraCliente(db.Model):
    __tablename__ = "oc_clientes"

    id = db.Column(db.Integer, primary_key=True)
    numero_oc = db.Column(db.String(100), nullable=False, index=True)
    cliente_id = db.Column(db.Integer, db.ForeignKey("ventas_clientes.id"), nullable=False, index=True)
    fecha_oc = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    fecha_entrega_comprometida = db.Column(db.DateTime)
    fecha_entrega_real = db.Column(db.DateTime)
    forma_pago = db.Column(db.String(100))
    vendedor = db.Column(db.String(120))
    direccion_despacho = db.Column(db.String(300))
    estado = db.Column(db.String(30), nullable=False, default="recibida", index=True)
    numero_factura = db.Column(db.String(60))
    fecha_pago = db.Column(db.DateTime)
    metodo_pago = db.Column(db.String(50))
    pago_grupo_id = db.Column(db.String(32), index=True)
    referencia_pago = db.Column(db.String(120))
    monto_pago_grupo = db.Column(db.Float)
    numero_guia_despacho = db.Column(db.String(60))
    observaciones = db.Column(db.Text)
    neto = db.Column(db.Float, default=0.0)
    iva = db.Column(db.Float, default=0.0)
    total = db.Column(db.Float, default=0.0)
    stock_deducted = db.Column(db.Boolean, default=False, nullable=False)
    usuario = db.Column(db.String(100), index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    cliente = db.relationship("Cliente", foreign_keys=[cliente_id])
    items = db.relationship(
        "OrdenCompraClienteItem",
        back_populates="orden",
        cascade="all, delete-orphan",
        order_by="OrdenCompraClienteItem.id",
    )


class OrdenCompraClienteItem(db.Model):
    __tablename__ = "oc_clientes_items"

    id = db.Column(db.Integer, primary_key=True)
    oc_id = db.Column(db.Integer, db.ForeignKey("oc_clientes.id"), nullable=False, index=True)
    codigo_producto = db.Column(db.String(100), nullable=False, index=True)
    descripcion = db.Column(db.String(255))
    marca = db.Column(db.String(120))
    bodega = db.Column(db.String(120))
    cantidad = db.Column(db.Integer, nullable=False, default=1)
    precio_unitario = db.Column(db.Float, nullable=False, default=0.0)
    descuento_item = db.Column(db.Float, default=0.0)
    subtotal = db.Column(db.Float, default=0.0)
    en_inventario = db.Column(db.Boolean, default=False, nullable=False)
    stock_descontado = db.Column(db.Boolean, default=False, nullable=False)

    orden = db.relationship("OrdenCompraCliente", back_populates="items")
