from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.extensions import db
from app.vehiculos_vin.models import VehiculoVin
from app.vehiculos_vin.routes import (
    _build_vehicle_data,
    desglosar_modelo,
    normalizar_texto,
    normalizar_vin,
)

# Marcas frecuentes en OC / parque Andes (orden: más largas primero)
_MARCAS_CONOCIDAS = (
    "GREAT WALL",
    "BRILLIANCE",
    "DONGFENG",
    "SSANGYONG",
    "CHANGAN",
    "CHERY",
    "HAVAL",
    "GEELY",
    "FOTON",
    "MAXUS",
    "JMC",
    "JAC",
    "BYD",
    "FAW",
    "KYC",
    "MG",
    "DFSK",
    "ZX AUTO",
    "ZXAUTO",
)

_VIN_LINE_RE = re.compile(
    r"(?:VIN|CHASIS|CHASSIS|N[°º]?\s*CHASIS|NRO\.?\s*CHASIS)\s*[;:=\-]?\s*"
    r"([A-HJ-NPR-Z0-9]{11,17})",
    re.IGNORECASE,
)
_VIN_BARE_RE = re.compile(r"\b([A-HJ-NPR-Z0-9]{17})\b", re.IGNORECASE)
_YEAR_IN_LINE_RE = re.compile(r"\b((?:19|20)\d{2})\b")


def parse_vehiculo_desde_texto(texto: str) -> dict[str, Any] | None:
    """
    Extrae VIN + marca/modelo/año desde observaciones de OC (u OCR).
    Ejemplo:
        JMC GRAND AVENUE 2024
        VIN ; LEFADEF1XSTP00861
    """
    raw = (texto or "").strip()
    if not raw:
        return None

    vin = None
    m_vin = _VIN_LINE_RE.search(raw)
    if m_vin:
        vin = normalizar_vin(m_vin.group(1))
    if not vin:
        for ln in raw.splitlines():
            m2 = _VIN_BARE_RE.search(ln.upper())
            if m2:
                cand = normalizar_vin(m2.group(1))
                if cand and len(cand) >= 11:
                    vin = cand
                    break

    if not vin or len(vin) < 11:
        return None

    marca = ""
    modelo_raw = ""
    upper = raw.upper()
    for brand in _MARCAS_CONOCIDAS:
        if brand in upper:
            # línea que contiene la marca (preferir la que también tiene año)
            candidatos: list[str] = []
            for ln in raw.splitlines():
                lu = ln.upper()
                if brand in lu and not re.search(r"\bVIN\b|\bCHASIS\b", lu):
                    if re.match(r"^[-–]", ln.strip()):
                        continue
                    candidatos.append(re.sub(r"\s+", " ", ln).strip())
            if candidatos:
                with_year = [c for c in candidatos if _YEAR_IN_LINE_RE.search(c)]
                modelo_raw = (with_year[0] if with_year else candidatos[0]).upper()
            marca = brand
            break

    if not modelo_raw:
        # fallback: primera línea con año que no sea VIN/guía
        for ln in raw.splitlines():
            s = re.sub(r"\s+", " ", ln).strip()
            if not s or re.match(r"^[-–]", s):
                continue
            if re.search(r"\bVIN\b|\bCHASIS\b|\bGUIA\b|\bSINIESTRO\b", s, re.I):
                continue
            if _YEAR_IN_LINE_RE.search(s) and len(s) >= 6:
                modelo_raw = s.upper()
                break

    parsed = desglosar_modelo(modelo_raw, marca) if modelo_raw else {
        "modelo": "",
        "modelo_completo": "",
        "anio": None,
        "cilindrada": "",
        "transmision": "",
    }
    if not marca and parsed.get("modelo"):
        # intenta inferir marca del primer token
        first = (parsed["modelo"] or "").split()[:1]
        if first and first[0] in {b.split()[0] for b in _MARCAS_CONOCIDAS}:
            marca = first[0]

    return {
        "vin": vin,
        "marca": marca,
        "modelo_raw": modelo_raw or parsed.get("modelo_completo") or "",
        "anio": parsed.get("anio"),
        "cilindrada": parsed.get("cilindrada") or "",
        "transmision": parsed.get("transmision") or "",
        "modelo": parsed.get("modelo") or "",
        "modelo_completo": parsed.get("modelo_completo") or modelo_raw,
    }


def upsert_vehiculo_desde_oc(
    *,
    observaciones: str | None,
    numero_oc: str | None = None,
    usuario: str | None = None,
) -> VehiculoVin | None:
    """
    Si observaciones trae VIN, crea o actualiza vehiculos_vin.
    No rompe el flujo de OC si falla (caller debe capturar excepciones).
    """
    parsed = parse_vehiculo_desde_texto(observaciones or "")
    if not parsed:
        return None

    data = _build_vehicle_data(
        vin=parsed["vin"],
        chasis=parsed["vin"],
        marca=normalizar_texto(parsed.get("marca"), 80).upper(),
        modelo_raw=parsed.get("modelo_raw") or parsed.get("modelo_completo") or "",
        anio_explicit=parsed.get("anio"),
        motor="",
        version="",
        transmision_explicit=normalizar_texto(parsed.get("transmision"), 80).upper(),
        cilindrada_explicit=normalizar_texto(parsed.get("cilindrada"), 40).upper(),
        patente="",
        nombre_china="",
        notas="",
        auto_desglosar=True,
    )

    nota_oc = f"OC cliente {numero_oc}".strip() if numero_oc else "OC cliente"
    existing = VehiculoVin.query.filter_by(vin=data["vin"]).first()
    user = (usuario or "sistema").strip() or "sistema"

    if existing:
        # No borrar motor/patente/china si ya existían; sí refrescar marca/modelo/año
        for key in ("marca", "modelo", "modelo_completo", "anio", "cilindrada", "transmision", "chasis"):
            val = data.get(key)
            if val is None or val == "":
                continue
            setattr(existing, key, val)
        # Anotar origen OC sin pisar notas largas
        prev = (existing.notas or "").strip()
        if nota_oc not in prev:
            existing.notas = f"{prev}\n{nota_oc}".strip() if prev else nota_oc
        if not existing.fuente or existing.fuente == "manual":
            existing.fuente = "oc_cliente"
        existing.activo = True
        existing.usuario_edicion = user
        existing.updated_at = datetime.utcnow()
        db.session.add(existing)
        db.session.commit()
        return existing

    notas = nota_oc
    data["notas"] = notas
    v = VehiculoVin(
        **data,
        fuente="oc_cliente",
        activo=True,
        usuario_alta=user,
        usuario_edicion=user,
    )
    db.session.add(v)
    db.session.commit()
    return v
