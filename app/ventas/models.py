from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import validates

from app.extensions import db
from app.utils.party_fields import party_text_upper
from app.utils.rut_utils import format_rut
from app.utils.phone_format import format_phone_display


# =====================================================
# VENTAS (SALES DOCUMENTS)
# =====================================================

class DocumentoVenta(db.Model):
    """Sales document: invoice, receipt, order, etc."""
    __tablename__ = "ventas_documentos"

    id = db.Column(db.Integer, primary_key=True)
    
    # Document metadata
    tipo = db.Column(db.String(20), nullable=False)  # factura, boleta, orden_venta, orden_compra, cotizacion
    numero = db.Column(db.String(60), index=True)  # Document number (optional for orders/quotes)
    fecha_documento = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    fecha_vencimiento = db.Column(db.DateTime)
    
    # Party info
    cliente_id = db.Column(db.Integer, db.ForeignKey("ventas_clientes.id"), index=True)
    proveedor_id = db.Column(db.Integer, db.ForeignKey("ventas_proveedores.id"), index=True)
    cliente_rut = db.Column(db.String(20))
    cliente_nombre = db.Column(db.String(200), nullable=False)
    cliente_giro = db.Column(db.String(200))
    cliente_direccion = db.Column(db.String(300))
    cliente_ciudad = db.Column(db.String(120))
    cliente_region = db.Column(db.String(120))
    cliente_pais = db.Column(db.String(120), default="Chile")
    cliente_telefono = db.Column(db.String(50))
    cliente_email = db.Column(db.String(150))
    
    # Fiscal info
    subtotal = db.Column(db.Float, default=0.0)
    impuesto = db.Column(db.Float, default=0.0)
    descuento = db.Column(db.Float, default=0.0)
    total = db.Column(db.Float, default=0.0)
    
    # Status
    status = db.Column(db.String(50), default="pendiente")  # pendiente, aprobada, entregada, anulada

    # Payment
    metodo_pago = db.Column(db.String(50), default="")   # efectivo, transferencia, tarjeta_debito, tarjeta_credito, credito_30, credito_60, credito_90, cheque, saldo_favor
    estado_pago = db.Column(db.String(30), default="pendiente")  # pendiente, pagado
    pago_referencia = db.Column(db.String(200), default="")  # voucher, turno, nota de caja (opcional)
    monto_saldo_favor = db.Column(db.Float, default=0.0)  # monto Tributario cubierto con crédito a favor del cliente
    
    # Tracking
    source_id = db.Column(db.Integer, index=True)
    source_type = db.Column(db.String(40), index=True)
    root_id = db.Column(db.Integer, index=True)
    observacion = db.Column(db.Text)
    usuario = db.Column(db.String(100), index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Stock deduction info
    stock_deducted = db.Column(db.Boolean, default=False)  # True if stock was deducted
    
    # Relationships
    items = db.relationship("DocumentoVentaItem", back_populates="documento", cascade="all, delete-orphan")
    credit_notes = db.relationship("NotaCredito", back_populates="documento_original")


class DocumentoVentaItem(db.Model):
    """Line item in a sales document."""
    __tablename__ = "ventas_documentos_items"

    id = db.Column(db.Integer, primary_key=True)
    documento_id = db.Column(db.Integer, db.ForeignKey("ventas_documentos.id"), nullable=False, index=True)
    
    # Product info
    codigo_producto = db.Column(db.String(100), nullable=False, index=True)
    descripcion = db.Column(db.String(255))
    modelo_linea = db.Column(db.String(255))  # OEM / modelo referencia (editable en linea)
    marca = db.Column(db.String(120))  # Variant: brand/model
    bodega = db.Column(db.String(120))  # Warehouse
    origen_compra = db.Column(db.String(20), nullable=False, default="nacional", index=True)  # nacional/importacion
    
    # Quantity & pricing
    cantidad = db.Column(db.Integer, nullable=False, default=1)
    precio_unitario = db.Column(db.Float, nullable=False, default=0.0)
    descuento_item = db.Column(db.Float, default=0.0)  # Item discount percentage
    margen_porcentaje = db.Column(db.Float, nullable=True)
    subtotal = db.Column(db.Float, default=0.0)
    
    # Relationship
    documento = db.relationship("DocumentoVenta", back_populates="items")


class NotaCredito(db.Model):
    """Credit note (devolución de venta)."""
    __tablename__ = "ventas_notas_credito"

    id = db.Column(db.Integer, primary_key=True)
    
    # Link to original sale
    documento_venta_id = db.Column(db.Integer, db.ForeignKey("ventas_documentos.id"), nullable=False, index=True)
    source_id = db.Column(db.Integer, index=True)
    source_type = db.Column(db.String(40), index=True)
    root_id = db.Column(db.Integer, index=True)
    numero = db.Column(db.String(60), index=True)  # Credit note number
    
    # Reason
    razon = db.Column(db.String(255))  # Reason for return/refund
    
    # Fiscal info
    subtotal = db.Column(db.Float, default=0.0)
    impuesto = db.Column(db.Float, default=0.0)
    total = db.Column(db.Float, default=0.0)
    
    # Status
    status = db.Column(db.String(50), default="pendiente")  # pendiente, aprobada, procesada
    
    # Tracking
    usuario = db.Column(db.String(100))
    fecha_documento = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    
    # Stock reversal info
    stock_restored = db.Column(db.Boolean, default=False)  # True if stock was restored

    # Cómo se liquida el monto de la NC frente al cliente (contable / operativo)
    modo_liquidacion = db.Column(
        db.String(32),
        default="saldo_favor",
    )  # saldo_favor | devolucion_dinero

    # Relationships
    documento_original = db.relationship("DocumentoVenta", back_populates="credit_notes")
    items = db.relationship("NotaCreditoItem", back_populates="nota_credito", cascade="all, delete-orphan")


class NotaCreditoItem(db.Model):
    """Line item in a credit note."""
    __tablename__ = "ventas_notas_credito_items"

    id = db.Column(db.Integer, primary_key=True)
    nota_credito_id = db.Column(db.Integer, db.ForeignKey("ventas_notas_credito.id"), nullable=False, index=True)
    
    # Product info
    codigo_producto = db.Column(db.String(100), nullable=False, index=True)
    descripcion = db.Column(db.String(255))
    marca = db.Column(db.String(120))  # Variant: brand/model
    bodega = db.Column(db.String(120))  # Warehouse
    origen_compra = db.Column(db.String(20), nullable=False, default="nacional", index=True)
    
    # Quantity & pricing (from original sale)
    cantidad = db.Column(db.Integer, nullable=False, default=1)
    precio_unitario = db.Column(db.Float, nullable=False, default=0.0)
    subtotal = db.Column(db.Float, default=0.0)
    
    # Relationship
    nota_credito = db.relationship("NotaCredito", back_populates="items")


class ClienteSaldoFavorMovimiento(db.Model):
    """Movimientos de saldo a favor (crédito no facturado a favor del cliente)."""

    __tablename__ = "ventas_clientes_saldo_movimientos"

    id = db.Column(db.Integer, primary_key=True)
    cliente_id = db.Column(db.Integer, db.ForeignKey("ventas_clientes.id"), index=True, nullable=False)
    monto = db.Column(db.Float, nullable=False)  # suma: positivo acredita, negativo consume
    tipo = db.Column(db.String(32), nullable=False)  # manual_ingreso | ajuste_documento | nota_credito_credito
    ref_factura_numero = db.Column(db.String(100))
    ref_nota_credito_numero = db.Column(db.String(100))
    razon = db.Column(db.String(2000))
    documento_venta_id = db.Column(db.Integer, db.ForeignKey("ventas_documentos.id"), index=True)
    nota_credito_id = db.Column(db.Integer, db.ForeignKey("ventas_notas_credito.id"), index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    usuario = db.Column(db.String(100))


class Cliente(db.Model):
    __tablename__ = "ventas_clientes"

    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(200), nullable=False)
    rut = db.Column(db.String(20), default="")
    giro = db.Column(db.String(200), default="")
    direccion = db.Column(db.String(300), default="")
    region = db.Column(db.String(120), default="")
    comuna = db.Column(db.String(120), default="")
    ciudad = db.Column(db.String(120), default="")
    pais = db.Column(db.String(120), default="Chile")
    telefono = db.Column(db.String(50), default="")
    email = db.Column(db.String(150), default="")
    activo = db.Column(db.Boolean, default=True)
    cliente_mayorista = db.Column(db.Boolean, default=False)
    margen_descuento_pct = db.Column(db.Float, default=0.0)  # % sobre subtotal de lineas antes de IVA
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "nombre": self.nombre or "",
            "rut": format_rut(self.rut),
            "giro": self.giro or "",
            "direccion": self.direccion or "",
            "region": self.region or "",
            "comuna": self.comuna or "",
            "ciudad": self.ciudad or "",
            "pais": self.pais or "Chile",
            "telefono": format_phone_display(self.telefono or ""),
            "email": self.email or "",
            "cliente_mayorista": bool(getattr(self, "cliente_mayorista", False)),
            "margen_descuento_pct": round(float(getattr(self, "margen_descuento_pct", 0) or 0), 4),
        }


class Proveedor(db.Model):
    __tablename__ = "ventas_proveedores"

    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(200), nullable=False)
    empresa = db.Column(db.String(200), default="")
    rut = db.Column(db.String(20), default="")
    giro = db.Column(db.String(200), default="")
    direccion = db.Column(db.String(300), default="")
    region = db.Column(db.String(120), default="")
    comuna = db.Column(db.String(120), default="")
    ciudad = db.Column(db.String(120), default="")
    pais = db.Column(db.String(120), default="Chile")
    telefono = db.Column(db.String(50), default="")
    email = db.Column(db.String(150), default="")
    activo = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @validates(
        "nombre",
        "empresa",
        "giro",
        "direccion",
        "region",
        "comuna",
        "ciudad",
        "pais",
    )
    def _normalize_text_fields(self, _key: str, value: str | None) -> str:
        return party_text_upper(value)

    def to_dict(self):
        return {
            "id": self.id,
            "nombre": self.nombre or "",
            "empresa": self.empresa or "",
            "rut": format_rut(self.rut),
            "giro": self.giro or "",
            "direccion": self.direccion or "",
            "region": self.region or "",
            "comuna": self.comuna or "",
            "ciudad": self.ciudad or "",
            "pais": self.pais or "Chile",
            "telefono": format_phone_display(self.telefono or ""),
            "email": self.email or "",
        }
