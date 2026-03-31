from sqlalchemy import Column, Integer, String, ForeignKey, Boolean, Float, Table
from sqlalchemy.orm import relationship
from ..models import Base


# ==============================
# MODELO MAESTRO (CATÁLOGO LIMPIO)
# ==============================

class ModeloMaestro(Base):
    __tablename__ = "modelos_maestros"

    id = Column(Integer, primary_key=True)
    marca = Column(String)
    modelo = Column(String)


# ==============================
# TABLA MANY TO MANY VEHICULO ↔ OEM
# ==============================

vehiculo_oem = Table(
    "vehiculo_oem",
    Base.metadata,
    Column("vehiculo_id", Integer, ForeignKey("vehiculos.id")),
    Column("oem_id", Integer, ForeignKey("oems.id"))
)


# ==============================
# VEHICULOS
# ==============================

class Vehiculo(Base):
    __tablename__ = "vehiculos"

    id = Column(Integer, primary_key=True)
    marca = Column(String)
    modelo = Column(String)
    anio_desde = Column(Integer)
    anio_hasta = Column(Integer)

    oems = relationship(
        "OEM",
        secondary=vehiculo_oem,
        back_populates="vehiculos"
    )

    motores = relationship("Motor", back_populates="vehiculo")
    compatibilidades = relationship("Compatibilidad", back_populates="vehiculo")


# ==============================
# MOTORES
# ==============================

class Motor(Base):
    __tablename__ = "motores"

    id = Column(Integer, primary_key=True)
    vehiculo_id = Column(Integer, ForeignKey("vehiculos.id"))

    codigo_motor = Column(String)
    cilindrada = Column(String)
    combustible = Column(String)

    vehiculo = relationship("Vehiculo", back_populates="motores")
    compatibilidades = relationship("Compatibilidad", back_populates="motor")


# ==============================
# OEM
# ==============================

class OEM(Base):
    __tablename__ = "oems"

    id = Column(Integer, primary_key=True)
    codigo_oem = Column(String, unique=True)
    descripcion_tecnica = Column(String)

    vehiculos = relationship(
        "Vehiculo",
        secondary=vehiculo_oem,
        back_populates="oems"
    )

    compatibilidades = relationship("Compatibilidad", back_populates="oem")
    medidas = relationship("Medida", back_populates="oem")
    productos = relationship("ProductoComercial", back_populates="oem")


# ==============================
# COMPATIBILIDAD
# ==============================

class Compatibilidad(Base):
    __tablename__ = "compatibilidades"

    id = Column(Integer, primary_key=True)

    oem_id = Column(Integer, ForeignKey("oems.id"))
    vehiculo_id = Column(Integer, ForeignKey("vehiculos.id"))
    motor_id = Column(Integer, ForeignKey("motores.id"), nullable=True)

    oem = relationship("OEM", back_populates="compatibilidades")
    vehiculo = relationship("Vehiculo", back_populates="compatibilidades")
    motor = relationship("Motor", back_populates="compatibilidades")


# ==============================
# MEDIDAS
# ==============================

class Medida(Base):
    __tablename__ = "medidas"

    id = Column(Integer, primary_key=True)
    oem_id = Column(Integer, ForeignKey("oems.id"))

    tipo = Column(String)
    valor = Column(String)
    unidad = Column(String)

    oem = relationship("OEM", back_populates="medidas")


# ==============================
# PRODUCTO COMERCIAL
# ==============================

class ProductoComercial(Base):
    __tablename__ = "productos_comerciales"

    id = Column(Integer, primary_key=True)

    oem_id = Column(Integer, ForeignKey("oems.id"))

    codigo_interno = Column(String, unique=True)
    marca = Column(String)

    precio_publico = Column(Float)
    precio_mayor = Column(Float)

    activo = Column(Boolean, default=True)

    oem = relationship("OEM", back_populates="productos")
    stocks = relationship("Stock", back_populates="producto")


# ==============================
# BODEGAS
# ==============================

class Bodega(Base):
    __tablename__ = "bodegas"

    id = Column(Integer, primary_key=True)
    nombre = Column(String, unique=True)

    stocks = relationship("Stock", back_populates="bodega")


# ==============================
# STOCK
# ==============================

class Stock(Base):
    __tablename__ = "stocks"

    id = Column(Integer, primary_key=True)

    producto_id = Column(Integer, ForeignKey("productos_comerciales.id"))
    bodega_id = Column(Integer, ForeignKey("bodegas.id"))

    cantidad = Column(Integer, default=0)

    producto = relationship("ProductoComercial", back_populates="stocks")
    bodega = relationship("Bodega", back_populates="stocks")


# ==============================
# VENTAS
# ==============================

class Venta(Base):
    __tablename__ = "ventas"

    id = Column(Integer, primary_key=True)

    producto_id = Column(Integer, ForeignKey("productos_comerciales.id"))
    bodega_id = Column(Integer, ForeignKey("bodegas.id"))

    cantidad = Column(Integer)
    fecha = Column(String)

    producto = relationship("ProductoComercial")
    bodega = relationship("Bodega")