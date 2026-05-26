"""Documentos tributarios electrónicos sincronizados desde API intermediaria (portal SII)."""
from __future__ import annotations

from datetime import date, datetime

from app.extensions import db


class SIIDocumento(db.Model):
    __tablename__ = "sii_documentos"

    id = db.Column(db.Integer, primary_key=True)
    tipo_dte = db.Column(db.String(10), nullable=False, index=True)
    folio = db.Column(db.Integer, nullable=False, index=True)
    fecha_emision = db.Column(db.Date, nullable=True, index=True)
    rut_receptor = db.Column(db.String(20), nullable=True, index=True)
    razon_social_receptor = db.Column(db.String(255), nullable=True)
    monto_neto = db.Column(db.Integer, nullable=False, default=0)
    monto_iva = db.Column(db.Integer, nullable=False, default=0)
    monto_total = db.Column(db.Integer, nullable=False, default=0)
    estado_sii = db.Column(db.String(30), nullable=False, default="PENDIENTE", index=True)
    track_id = db.Column(db.String(120), nullable=True)
    xml_disponible = db.Column(db.Boolean, nullable=False, default=False)
    sincronizado_en = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    periodo = db.Column(db.String(7), nullable=True, index=True)
    documento_venta_id = db.Column(db.Integer, db.ForeignKey("ventas_documentos.id"), nullable=True, index=True)
    notas = db.Column(db.String(500), nullable=True)

    documento_venta = db.relationship(
        "DocumentoVenta",
        foreign_keys=[documento_venta_id],
        primaryjoin="SIIDocumento.documento_venta_id == DocumentoVenta.id",
    )

    __table_args__ = (
        db.UniqueConstraint("tipo_dte", "folio", name="uq_sii_documentos_tipo_folio"),
    )

    @property
    def tipo_dte_label(self) -> str:
        labels = {
            "33": "Factura electrónica",
            "34": "Factura no afecta",
            "39": "Boleta electrónica",
            "41": "Boleta exenta",
            "52": "Guía de despacho",
            "56": "Nota de débito",
            "61": "Nota de crédito",
        }
        key = str(self.tipo_dte or "").strip()
        return labels.get(key, f"DTE {key}" if key else "—")

    @property
    def conciliado(self) -> bool:
        return self.documento_venta_id is not None
