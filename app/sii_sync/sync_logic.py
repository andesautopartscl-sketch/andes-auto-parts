"""Persistencia de documentos RCV consultados al ERP."""
from __future__ import annotations

from datetime import datetime

from app.extensions import db
from app.sii_sync.models import SIIDocumento
from app.sii_sync.sii_service import SIIService, SIIServiceError


def sincronizar_periodo(periodo: str) -> dict:
    """
    Consulta RCV ventas del periodo y hace upsert en sii_documentos.
    Retorna contadores nuevos / actualizados / errores.
    """
    resultado = {"nuevos": 0, "actualizados": 0, "errores": 0, "mensaje": ""}
    service = SIIService()
    try:
        documentos = service.consultar_rcv(periodo)
    except SIIServiceError as exc:
        resultado["errores"] = 1
        resultado["mensaje"] = str(exc)
        return resultado
    except Exception as exc:
        resultado["errores"] = 1
        resultado["mensaje"] = f"Error inesperado al sincronizar: {exc}"
        return resultado

    ahora = datetime.utcnow()
    for data in documentos:
        try:
            tipo = str(data.get("tipo_dte") or "").strip()
            folio = data.get("folio")
            if not tipo or folio is None:
                resultado["errores"] += 1
                continue
            existente = (
                SIIDocumento.query.filter_by(tipo_dte=tipo, folio=int(folio)).first()
            )
            if existente:
                existente.estado_sii = data.get("estado_sii") or existente.estado_sii
                existente.track_id = data.get("track_id") or existente.track_id
                existente.xml_disponible = bool(data.get("xml_disponible"))
                existente.sincronizado_en = ahora
                if data.get("periodo"):
                    existente.periodo = data.get("periodo")
                if data.get("notas"):
                    existente.notas = data.get("notas")
                resultado["actualizados"] += 1
            else:
                row = SIIDocumento(
                    tipo_dte=tipo,
                    folio=int(folio),
                    fecha_emision=data.get("fecha_emision"),
                    rut_receptor=data.get("rut_receptor"),
                    razon_social_receptor=data.get("razon_social_receptor"),
                    monto_neto=int(data.get("monto_neto") or 0),
                    monto_iva=int(data.get("monto_iva") or 0),
                    monto_total=int(data.get("monto_total") or 0),
                    estado_sii=data.get("estado_sii") or "PENDIENTE",
                    track_id=data.get("track_id"),
                    xml_disponible=bool(data.get("xml_disponible")),
                    sincronizado_en=ahora,
                    periodo=data.get("periodo") or periodo,
                    notas=data.get("notas"),
                )
                db.session.add(row)
                resultado["nuevos"] += 1
        except Exception:
            resultado["errores"] += 1

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        resultado["errores"] += 1
        resultado["mensaje"] = f"No se pudo guardar en base de datos: {exc}"
        return resultado

    total = resultado["nuevos"] + resultado["actualizados"]
    resultado["mensaje"] = (
        f"Sincronización {periodo}: {resultado['nuevos']} nuevos, "
        f"{resultado['actualizados']} actualizados"
        + (f", {resultado['errores']} con error" if resultado["errores"] else "")
        + f" (API: {service.provider})."
    )
    if total == 0 and resultado["errores"] == 0:
        resultado["mensaje"] = f"No se encontraron documentos en el periodo {periodo}."
    return resultado
