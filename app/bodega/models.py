from datetime import datetime

from app.extensions import db
from sqlalchemy import UniqueConstraint


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
    ingreso_documento_id = db.Column(db.Integer, index=True)
    observacion = db.Column(db.String(255))


class ProductoVarianteStock(db.Model):
    __tablename__ = "productos_variantes_stock"
    __table_args__ = (
        UniqueConstraint("codigo_producto", "marca", "bodega", name="uq_variante_codigo_marca_bodega"),
    )

    id = db.Column(db.Integer, primary_key=True)
    codigo_producto = db.Column(db.String(100), nullable=False, index=True)
    marca = db.Column(db.String(120), nullable=False, index=True)
    proveedor = db.Column(db.String(150))
    bodega = db.Column(db.String(120), nullable=False, index=True)
    stock = db.Column(db.Integer, nullable=False, default=0)
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
    cantidad = db.Column(db.Integer, nullable=False)
    nota = db.Column(db.String(255), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
