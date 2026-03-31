from sqlalchemy import Column, String, Boolean, Float, Integer, ForeignKey, DateTime, Text, create_engine, Table, Index
from sqlalchemy.orm import relationship, sessionmaker, declarative_base
from datetime import datetime
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "..", "data", "andes.db")

engine = create_engine(f"sqlite:///{DB_PATH}", echo=False)

Base = declarative_base()
SessionDB = sessionmaker(bind=engine)

# =====================================================
# TABLA INTERMEDIA PRODUCTO - ETIQUETA
# =====================================================

producto_etiqueta = Table(
    "producto_etiqueta",
    Base.metadata,
    Column("producto_codigo", String, ForeignKey("productos.CODIGO")),
    Column("etiqueta_id", Integer, ForeignKey("etiquetas.id"))
)

# =====================================================
# ETIQUETAS
# =====================================================

class Etiqueta(Base):
    __tablename__ = "etiquetas"

    id = Column(Integer, primary_key=True)
    nombre = Column(String, unique=True)

    productos = relationship(
        "Producto",
        secondary=producto_etiqueta,
        back_populates="etiquetas"
    )

# =====================================================
# USUARIOS
# =====================================================

class Usuario(Base):
    __tablename__ = "usuarios"

    username = Column(String, primary_key=True)
    password = Column(String)
    rol = Column(String)

# =====================================================
# PRODUCTOS
# =====================================================

class Producto(Base):
    __tablename__ = "productos"

    codigo = Column("CODIGO", String, primary_key=True)
    descripcion = Column("DESCRIPCION", String)
    modelo = Column("MODELO", String)
    motor = Column("MOTOR", String)
    marca = Column("MARCA", String)

    p_publico = Column("P_PUBLICO", Float)
    prec_mayor = Column("PREC_MAYOR", Float)

    stock_10jul = Column("STOCK_10JUL", Float)
    stock_brasil = Column("STOCK_BRASIL", Float)
    stock_g_avenida = Column("STOCK_G_AVENIDA", Float)
    stock_orientales = Column("STOCK_ORIENTALES", Float)
    stock_b20_outlet = Column("STOCK_B20_OUTLET", Float)
    stock_transito = Column("STOCK_TRANSITO", Float)

    codigo_oem = Column("CODIGO OEM", String)
    codigo_alternativo = Column("CODIGO ALTERNATIVO O ANTIGUO", String)
    homologados = Column("HOMOLOGADOS", String)
    medidas = Column(String)
    anio = Column(String)
    version = Column(String)
    factura_proveedor = Column(String)
    activo = Column("ACTIVO", Boolean, default=True)

    # 🔵 RELACIÓN CON ETIQUETAS (Many-to-Many)
    etiquetas = relationship(
        "Etiqueta",
        secondary=producto_etiqueta,
        back_populates="productos",
        lazy="joined"
    )

    # 🔵 RELACIÓN CON CATEGORÍAS
    categoria_id = Column(Integer, ForeignKey("categorias.id"))
    subcategoria_id = Column(Integer, ForeignKey("subcategorias.id"))

    categoria_rel = relationship("Categoria", lazy="joined")
    subcategoria_rel = relationship("Subcategoria", lazy="joined")

    despiece = Column(String)
    imagen_url = Column(String)

    imagenes = relationship("ProductoImagen",backref="producto",cascade="all, delete-orphan")

# =====================================================
# IMAGENES MULTIPLES EN MODAL CREAR
# =====================================================

class ProductoImagen(Base):
    __tablename__ = "producto_imagenes"

    id = Column(Integer, primary_key=True)
    producto_codigo = Column(String, ForeignKey("productos.CODIGO"))
    ruta = Column(String)
    es_principal = Column(Boolean, default=False)


# =====================================================
# AUDITORIA DE PRODUCTOS
# =====================================================

class ProductoAuditEvent(Base):
    __tablename__ = "productos_audit_eventos"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    actor = Column(String, nullable=False, default="sistema")
    action = Column(String, nullable=False)  # create, update, deactivate, search, view, etc.
    modulo = Column(String, nullable=False, default="productos")
    producto_codigo = Column(String, index=True)
    ip = Column(String)
    user_agent = Column(String)
    request_path = Column(String)
    metadata_json = Column(Text)  # payload compacto serializado en JSON

    diffs = relationship(
        "ProductoAuditDiff",
        back_populates="event",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class ProductoAuditDiff(Base):
    __tablename__ = "productos_audit_diffs"

    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey("productos_audit_eventos.id"), nullable=False, index=True)
    campo = Column(String, nullable=False)
    valor_anterior = Column(Text)
    valor_nuevo = Column(Text)

    event = relationship("ProductoAuditEvent", back_populates="diffs")


class ProductoDraft(Base):
    __tablename__ = "productos_drafts"

    id = Column(Integer, primary_key=True)
    user = Column(String, nullable=False, index=True)
    form_key = Column(String, nullable=False, index=True)  # crear / editar:<codigo>
    producto_codigo = Column(String, index=True)
    payload_json = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


Index("ix_productos_audit_eventos_action", ProductoAuditEvent.action)
Index("ix_productos_audit_eventos_actor", ProductoAuditEvent.actor)
Index("ix_productos_audit_eventos_created_at", ProductoAuditEvent.created_at)
Index("ix_productos_drafts_user_form_key", ProductoDraft.user, ProductoDraft.form_key, unique=True)


# =====================================================
# DESPIECE / CATÁLOGO OEM (datos propios, opcional)
# =====================================================


class OemDespiece(Base):
    """
    Información de despiece asociada a un número OEM normalizado (MAYÚSCULAS, sin espacios externos).
    Opcionalmente fila por código interno (producto_codigo): prioridad en búsqueda sobre OEM compartido.
    La imagen vive bajo app/static/ (campo imagen_static relativo, ej: epc_despiece/10046260.png).
    partes_json: lista JSON por posición. Opcional en cada ítem: x_pct, y_pct, r_pct (0–100, centro y radio del callout sobre la imagen).
    Ej.: [{ "callout": "24", "part_no": "10046260", "usage": "RING KIT-PSTN", "qty": "4", "x_pct": 48, "y_pct": 35, "r_pct": 4.5, "price": "72.43", "ref_price": "94.16" }, ...]
    """

    __tablename__ = "oem_despiece"

    id = Column(Integer, primary_key=True)
    oem_norm = Column(String(64), unique=True, nullable=False, index=True)
    producto_codigo = Column(String(64), unique=True, nullable=True, index=True)
    titulo = Column(String(220))
    imagen_static = Column(String(512))
    partes_json = Column(Text)
    notas = Column(Text)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# =====================================================
# CATEGORÍAS
# =====================================================

class Categoria(Base):
    __tablename__ = "categorias"

    id = Column(Integer, primary_key=True)
    nombre = Column(String, unique=True)

    subcategorias = relationship("Subcategoria", back_populates="categoria")

# =====================================================
# SUBCATEGORÍAS
# =====================================================

class Subcategoria(Base):
    __tablename__ = "subcategorias"

    id = Column(Integer, primary_key=True)
    nombre = Column(String)
    palabras_clave = Column(String)

    categoria_id = Column(Integer, ForeignKey("categorias.id"))
    categoria = relationship("Categoria", back_populates="subcategorias")

# =====================================================
# CREAR TABLAS
# =====================================================


def crear_etiquetas_base():
    db = SessionDB()

    etiquetas_base = [
        "OFERTA",
        "TOP VENTA",
        "NUEVO",
        "IMPORTACIÓN",
        "BAJO STOCK",
        "EXCLUSIVO"
    ]

    for nombre in etiquetas_base:
        existe = db.query(Etiqueta).filter_by(nombre=nombre).first()
        if not existe:
            db.add(Etiqueta(nombre=nombre))

    db.commit()
    db.close()


def crear_categorias_base():
    """Semilla categorías/subcategorías si la tabla está vacía (ERP)."""
    db = SessionDB()
    try:
        if db.query(Categoria).count() > 0:
            return
        grupos = [
            (
                "Motor",
                [
                    "Bujías e incandescentes",
                    "Filtros",
                    "Correas y tensores",
                    "Embrague",
                ],
            ),
            (
                "Electricidad",
                ["Encendido", "Batería", "Iluminación", "Sensores"],
            ),
            (
                "Frenos",
                ["Pastillas", "Discos", "Líquido y accesorios"],
            ),
            (
                "Suspensión y dirección",
                ["Amortiguadores", "Rótulas", "Terminales"],
            ),
            (
                "Carrocería",
                ["Espejos", "Paragolpes", "Accesorios"],
            ),
        ]
        for nombre_cat, subs in grupos:
            cat = Categoria(nombre=nombre_cat)
            db.add(cat)
            db.flush()
            for sn in subs:
                db.add(Subcategoria(nombre=sn, categoria_id=cat.id, palabras_clave=""))
        db.commit()
    except Exception as exc:
        db.rollback()
        print("crear_categorias_base:", exc)
    finally:
        db.close()


# Crear tablas
Base.metadata.create_all(engine)

# Crear etiquetas iniciales
crear_etiquetas_base()
crear_categorias_base()

Base.metadata.create_all(engine)