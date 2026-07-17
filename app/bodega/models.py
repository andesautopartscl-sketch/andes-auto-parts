from datetime import datetime

from app.extensions import db
from sqlalchemy import UniqueConstraint


class CatalogoBodega(db.Model):
    """Catálogo operativo de bodegas (nombre = clave usada en variantes y movimientos)."""

    __tablename__ = "bodegas_catalogo"

    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(120), unique=True, nullable=False, index=True)
    activo = db.Column(db.Boolean, nullable=False, default=True)
    orden = db.Column(db.Integer, nullable=False, default=0)
    nota = db.Column(db.String(255), default="")


class CatalogoVarianteMarca(db.Model):
    """Catálogo de nombres de variante/marca (sin asignar a código; eso ocurre en ingreso u otros menús)."""

    __tablename__ = "variantes_marcas_catalogo"

    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(120), unique=True, nullable=False, index=True)
    activo = db.Column(db.Boolean, nullable=False, default=True)
    orden = db.Column(db.Integer, nullable=False, default=0)
    nota = db.Column(db.String(255), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class MovimientoStock(db.Model):
    __tablename__ = "movimientos_stock"

    id = db.Column(db.Integer, primary_key=True)
    codigo_producto = db.Column(db.String(100), nullable=False, index=True)
    tipo = db.Column(db.String(20), nullable=False)  # ingreso / salida / ajuste
    cantidad = db.Column(db.Integer, nullable=False)
    fecha = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    usuario = db.Column(db.String(100))
    proveedor = db.Column(db.String(150))
    marca = db.Column(db.String(120), index=True)
    bodega = db.Column(db.String(120), index=True)
    origen_compra = db.Column(db.String(20), nullable=False, default="nacional", index=True)
    ingreso_documento_id = db.Column(db.Integer, index=True)
    observacion = db.Column(db.String(255))


class ProductoVarianteStock(db.Model):
    __tablename__ = "productos_variantes_stock"
    __table_args__ = (
        UniqueConstraint("codigo_producto", "marca", "bodega", "origen_compra", name="uq_variante_codigo_marca_bodega_origen"),
    )

    id = db.Column(db.Integer, primary_key=True)
    codigo_producto = db.Column(db.String(100), nullable=False, index=True)
    marca = db.Column(db.String(120), nullable=False, index=True)
    proveedor = db.Column(db.String(150))
    bodega = db.Column(db.String(120), nullable=False, index=True)
    origen_compra = db.Column(db.String(20), nullable=False, default="nacional", index=True)
    stock = db.Column(db.Integer, nullable=False, default=0)
    # Overrides de referencia comercial (ventas / edición producto); si son NULL se usa solo el último ingreso.
    margen_override_pct = db.Column(db.Float, nullable=True)
    precio_publico_neto_override = db.Column(db.Float, nullable=True)
    metadata_json = db.Column(db.Text)  # Reserved for lote/fecha/etc without schema churn.
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class HistorialEtiqueta(db.Model):
    __tablename__ = "historial_etiquetas"

    id = db.Column(db.Integer, primary_key=True)
    codigo_producto = db.Column(db.String(100), nullable=False, index=True)
    descripcion = db.Column(db.String(255), nullable=False, index=True)
    modelo = db.Column(db.String(120), default="")
    cantidad = db.Column(db.Integer, nullable=False, default=1)
    fecha = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    usuario = db.Column(db.String(100), index=True)


class IngresoDocumento(db.Model):
    __tablename__ = "ingresos_documentos"

    id = db.Column(db.Integer, primary_key=True)
    numero_documento = db.Column(db.String(60), index=True)
    fecha_documento = db.Column(db.Date, nullable=False, index=True)
    proveedor_id = db.Column(db.Integer, index=True)
    proveedor_rut = db.Column(db.String(20), nullable=False, index=True)
    proveedor_nombre = db.Column(db.String(200), nullable=False)
    proveedor_giro = db.Column(db.String(200), default="")
    proveedor_email = db.Column(db.String(150), default="")
    proveedor_direccion = db.Column(db.String(300), default="")
    proveedor_comuna = db.Column(db.String(120), default="")
    proveedor_region = db.Column(db.String(120), default="")
    proveedor_pais = db.Column(db.String(120), default="Chile")
    observacion = db.Column(db.String(255), default="")
    metodo_pago = db.Column(db.String(120), default="")
    # Total con IVA según factura física (opcional); iva_factura = total - suma netos líneas al guardar.
    total_factura = db.Column(db.Float, nullable=True)
    iva_factura = db.Column(db.Float, nullable=True)
    anulado = db.Column(db.Boolean, nullable=False, default=False, index=True)
    anulado_at = db.Column(db.DateTime, nullable=True)
    anulado_por = db.Column(db.String(100), nullable=True)
    anulacion_motivo = db.Column(db.String(255), default="")
    usuario = db.Column(db.String(100), index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)


class IngresoDocumentoItem(db.Model):
    __tablename__ = "ingresos_documentos_items"

    id = db.Column(db.Integer, primary_key=True)
    ingreso_documento_id = db.Column(db.Integer, nullable=False, index=True)
    codigo_producto = db.Column(db.String(100), nullable=False, index=True)
    descripcion_producto = db.Column(db.String(255), default="")
    marca = db.Column(db.String(120), default="", index=True)
    bodega = db.Column(db.String(120), nullable=False, index=True)
    origen_compra = db.Column(db.String(20), nullable=False, default="nacional", index=True)
    cantidad = db.Column(db.Integer, nullable=False)
    valor_neto = db.Column(db.Float, nullable=True)
    # Referencia comercial al ingresar: margen % sobre P. venta neto y/o precio neto unitario (sin IVA).
    margen_pct = db.Column(db.Float, nullable=True)
    precio_venta_neto = db.Column(db.Float, nullable=True)
    nota = db.Column(db.String(255), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class PickingVenta(db.Model):
    """Preparacion de mercaderia para una orden de venta (antes de facturar/boletear)."""

    __tablename__ = "bodega_picking_ventas"

    id = db.Column(db.Integer, primary_key=True)
    orden_venta_id = db.Column(db.Integer, db.ForeignKey("ventas_documentos.id"), nullable=False, unique=True, index=True)
    status = db.Column(db.String(30), nullable=False, default="pendiente")  # pendiente, en_preparacion, entregado
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    usuario_creacion = db.Column(db.String(100))
    usuario_entrega = db.Column(db.String(100))
    nota = db.Column(db.String(500), default="")

    lineas = db.relationship(
        "PickingVentaLine",
        back_populates="picking",
        cascade="all, delete-orphan",
    )


class PickingVentaLine(db.Model):
    __tablename__ = "bodega_picking_venta_lineas"

    id = db.Column(db.Integer, primary_key=True)
    picking_id = db.Column(db.Integer, db.ForeignKey("bodega_picking_ventas.id"), nullable=False, index=True)
    codigo_producto = db.Column(db.String(100), nullable=False)
    descripcion = db.Column(db.String(255), default="")
    marca = db.Column(db.String(120), default="")
    bodega = db.Column(db.String(120), nullable=False, default="Bodega 1")
    cantidad_pedida = db.Column(db.Integer, nullable=False, default=0)
    cantidad_entregada = db.Column(db.Integer, nullable=False, default=0)
    orden_linea = db.Column(db.Integer, nullable=False, default=0)

    picking = db.relationship("PickingVenta", back_populates="lineas")


class ProveedorCodigoInterno(db.Model):
    __tablename__ = "proveedor_codigo_interno"
    __table_args__ = (
        UniqueConstraint("proveedor_rut", "codigo_proveedor", name="uq_prov_rut_codigo_prov"),
    )

    id = db.Column(db.Integer, primary_key=True)
    proveedor_rut = db.Column(db.String(20), nullable=False, index=True)
    codigo_proveedor = db.Column(db.String(120), nullable=False, index=True)
    codigo_interno = db.Column(db.String(100), nullable=False, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
