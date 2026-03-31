from datetime import datetime
from decimal import Decimal

from app.extensions import db
from app.utils.rut_utils import format_rut


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
    metodo_pago = db.Column(db.String(50), default="")   # efectivo, transferencia, tarjeta_debito, tarjeta_credito, credito_30, credito_60, credito_90, cheque
    estado_pago = db.Column(db.String(30), default="pendiente")  # pendiente, pagado
    
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
    marca = db.Column(db.String(120))  # Variant: brand/model
    bodega = db.Column(db.String(120))  # Warehouse
    
    # Quantity & pricing
    cantidad = db.Column(db.Integer, nullable=False, default=1)
    precio_unitario = db.Column(db.Float, nullable=False, default=0.0)
    descuento_item = db.Column(db.Float, default=0.0)  # Item discount percentage
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
    
    # Quantity & pricing (from original sale)
    cantidad = db.Column(db.Integer, nullable=False, default=1)
    precio_unitario = db.Column(db.Float, nullable=False, default=0.0)
    subtotal = db.Column(db.Float, default=0.0)
    
    # Relationship
    nota_credito = db.relationship("NotaCredito", back_populates="items")


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
            "telefono": self.telefono or "",
            "email": self.email or "",
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
            "telefono": self.telefono or "",
            "email": self.email or "",
        }
