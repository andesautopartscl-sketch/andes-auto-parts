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
    # Pago parcial por ítem (factura externa).
    pagado = db.Column(db.Boolean, default=False, nullable=False)
    numero_factura = db.Column(db.String(60))
    fecha_pago = db.Column(db.DateTime)
    metodo_pago = db.Column(db.String(50))

    orden = db.relationship("OrdenCompraCliente", back_populates="items")


class OrdenCompraClientePago(db.Model):
    """Abono / factura externa asociada a una o más líneas de la OC."""

    __tablename__ = "oc_clientes_pagos"

    id = db.Column(db.Integer, primary_key=True)
    oc_id = db.Column(db.Integer, db.ForeignKey("oc_clientes.id"), nullable=False, index=True)
    numero_factura = db.Column(db.String(60), nullable=False)
    fecha_pago = db.Column(db.DateTime, nullable=False, index=True)
    metodo_pago = db.Column(db.String(50))
    monto = db.Column(db.Float, nullable=False, default=0.0)  # total c/IVA del abono
    referencia_pago = db.Column(db.String(120))
    usuario = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    orden = db.relationship("OrdenCompraCliente", backref=db.backref("pagos", lazy="dynamic"))
    items = db.relationship(
        "OrdenCompraClientePagoItem",
        back_populates="pago",
        cascade="all, delete-orphan",
    )


class OrdenCompraClientePagoItem(db.Model):
    __tablename__ = "oc_clientes_pago_items"

    id = db.Column(db.Integer, primary_key=True)
    pago_id = db.Column(db.Integer, db.ForeignKey("oc_clientes_pagos.id"), nullable=False, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey("oc_clientes_items.id"), nullable=False, index=True)
    subtotal_neto = db.Column(db.Float, default=0.0)
    monto_con_iva = db.Column(db.Float, default=0.0)

    pago = db.relationship("OrdenCompraClientePago", back_populates="items")
    item = db.relationship("OrdenCompraClienteItem")
