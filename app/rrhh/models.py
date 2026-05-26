from __future__ import annotations

from datetime import datetime, date
from decimal import Decimal

from app.extensions import db


class RRHHPerfil(db.Model):
    __tablename__ = "rrhh_perfil"

    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey("usuarios_sistema.id"), nullable=False, unique=True, index=True)

    salud_tipo = db.Column(db.String(20), default="", nullable=False)  # FONASA | ISAPRE
    salud_entidad = db.Column(db.String(120), default="", nullable=False)  # isapre name if applies
    salud_numero = db.Column(db.String(60), default="", nullable=False)  # contrato/afiliado (opcional)

    afp_nombre = db.Column(db.String(120), default="", nullable=False)
    afc_afiliado = db.Column(db.Boolean, default=True, nullable=False)

    banco_nombre = db.Column(db.String(120), default="", nullable=False)
    banco_tipo_cuenta = db.Column(db.String(40), default="", nullable=False)  # vista/rut/corriente/ahorro
    banco_numero_cuenta = db.Column(db.String(60), default="", nullable=False)

    es_vendedor = db.Column(db.Boolean, default=False, nullable=False)
    comision_pct = db.Column(db.Numeric(6, 3), default=Decimal("0.0"), nullable=False)  # 0-100

    # Expediente laboral
    contrato_vigencia_desde = db.Column(db.Date, nullable=True)
    contrato_notas = db.Column(db.String(500), nullable=False, default="")
    contrato_pdf_relpath = db.Column(db.String(500), nullable=True)
    contrato_pdf_original = db.Column(db.String(260), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class RRHHAfpTasa(db.Model):
    __tablename__ = "rrhh_afp_tasas"

    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(120), nullable=False, unique=True, index=True)
    tasa_pct = db.Column(db.Numeric(6, 4), nullable=False, default=Decimal("0.0"))  # e.g. 11.44 (as percent)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class RRHHImpuestoTramo(db.Model):
    __tablename__ = "rrhh_impuesto_tramos"

    id = db.Column(db.Integer, primary_key=True)
    vigente_desde = db.Column(db.Date, nullable=False, index=True, default=date.today)
    # Base imponible mensual en pesos CLP (rangos inclusive).
    desde = db.Column(db.Integer, nullable=False, default=0)
    hasta = db.Column(db.Integer, nullable=True)  # null = sin tope
    tasa_pct = db.Column(db.Numeric(6, 4), nullable=False, default=Decimal("0.0"))
    rebaja = db.Column(db.Integer, nullable=False, default=0)  # rebaja en CLP

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class RRHHParametrosPeriodo(db.Model):
    __tablename__ = "rrhh_parametros_periodo"

    id = db.Column(db.Integer, primary_key=True)
    periodo = db.Column(db.String(7), nullable=False, unique=True, index=True)  # YYYY-MM

    fonasa_tasa_pct = db.Column(db.Numeric(6, 4), nullable=False, default=Decimal("7.0"))
    afc_trabajador_tasa_pct = db.Column(db.Numeric(6, 4), nullable=False, default=Decimal("0.6"))
    # Isapre: si quieres operar por % en vez de UF, se permite configurar acá.
    isapre_tasa_pct = db.Column(db.Numeric(6, 4), nullable=False, default=Decimal("7.0"))

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class RRHHLiquidacion(db.Model):
    __tablename__ = "rrhh_liquidaciones"

    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey("usuarios_sistema.id"), nullable=False, index=True)
    periodo = db.Column(db.String(7), nullable=False, index=True)  # YYYY-MM

    sueldo_base = db.Column(db.Integer, nullable=False, default=0)  # CLP
    comision_bruta = db.Column(db.Integer, nullable=False, default=0)  # CLP
    haberes_otros = db.Column(db.Integer, nullable=False, default=0)  # CLP
    descuentos_otros = db.Column(db.Integer, nullable=False, default=0)  # CLP (positive number stored; subtracted)

    base_imponible = db.Column(db.Integer, nullable=False, default=0)

    salud_descuento = db.Column(db.Integer, nullable=False, default=0)
    afp_descuento = db.Column(db.Integer, nullable=False, default=0)
    afc_descuento = db.Column(db.Integer, nullable=False, default=0)
    impuesto_unico = db.Column(db.Integer, nullable=False, default=0)

    total_liquido = db.Column(db.Integer, nullable=False, default=0)

    estado = db.Column(db.String(20), nullable=False, default="borrador")  # borrador|cerrada|pagada
    pago_fecha = db.Column(db.Date, nullable=True)
    pago_medio = db.Column(db.String(40), nullable=False, default="")
    pago_referencia = db.Column(db.String(120), nullable=False, default="")

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class RRHHContratoAnexo(db.Model):
    """Anexos al contrato (PDF); el trabajador debe aceptarlos con registro de auditoría ligera."""

    __tablename__ = "rrhh_contrato_anexos"

    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey("usuarios_sistema.id"), nullable=False, index=True)
    titulo = db.Column(db.String(200), nullable=False)
    archivo_relpath = db.Column(db.String(500), nullable=False)
    nombre_original = db.Column(db.String(260), nullable=False, default="")
    mensaje = db.Column(db.String(500), nullable=False, default="")
    estado = db.Column(db.String(20), nullable=False, default="pendiente")  # pendiente | aceptado
    aceptado_at = db.Column(db.DateTime, nullable=True)
    aceptado_evidencia_hash = db.Column(db.String(64), nullable=True)
    creado_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    creado_por_usuario_id = db.Column(db.Integer, db.ForeignKey("usuarios_sistema.id"), nullable=True, index=True)


class RRHHVacacionRegistro(db.Model):
    """Solicitudes de vacaciones y períodos tomados (registro interno RRHH)."""

    __tablename__ = "rrhh_vacaciones_registro"

    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey("usuarios_sistema.id"), nullable=False, index=True)
    tipo = db.Column(db.String(16), nullable=False)  # solicitud | tomada
    fecha_inicio = db.Column(db.Date, nullable=False)
    fecha_fin = db.Column(db.Date, nullable=True)
    dias = db.Column(db.Integer, nullable=True)
    estado = db.Column(db.String(24), nullable=False, default="")  # pendiente | aprobada | rechazada (solicitud)
    notas = db.Column(db.String(500), nullable=False, default="")

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)


class RRHHLiquidacionDetalle(db.Model):
    __tablename__ = "rrhh_liquidacion_detalles"

    id = db.Column(db.Integer, primary_key=True)
    liquidacion_id = db.Column(db.Integer, db.ForeignKey("rrhh_liquidaciones.id"), nullable=False, index=True)

    tipo = db.Column(db.String(30), nullable=False, default="ajuste")  # comision|ajuste|descuento|haber
    referencia = db.Column(db.String(120), nullable=False, default="")  # doc_id/nc_id/texto
    descripcion = db.Column(db.String(255), nullable=False, default="")
    monto = db.Column(db.Integer, nullable=False, default=0)  # CLP (+/-)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

