"""Directorio de emisores contables (separado de clientes/proveedores de ventas)."""

from __future__ import annotations

from datetime import datetime

from app.extensions import db
from app.utils.rut_utils import clean_rut, format_rut, is_valid_rut

from .models import EmisorContable, MovimientoContable


def _upper_text(raw: str | None) -> str:
    """Texto libre: strip + MAYÚSCULAS (misma convención que clientes/proveedores)."""
    return (raw or "").strip().upper()


def _normalize_email(raw: str | None) -> str:
    e = (raw or "").strip()
    return e.lower() if e else ""


def _normalize_pais(raw: str | None) -> str:
    p = (raw or "").strip()[:120]
    return p or "Chile"


def emisor_rut_norm_sql(column=MovimientoContable.emisor_rut):
    from sqlalchemy import func

    expr = func.coalesce(column, "")
    for ch in (".", "-", " "):
        expr = func.replace(expr, ch, "")
    return func.upper(expr)


def find_emisor_libro_diario_por_rut(rut_raw: str | None) -> MovimientoContable | None:
    """Último movimiento con este RUT emisor y razón social."""
    cr = clean_rut(rut_raw)
    if len(cr) < 7:
        return None
    return (
        MovimientoContable.query.filter(emisor_rut_norm_sql() == cr.upper())
        .filter(MovimientoContable.emisor_nombre.isnot(None))
        .filter(MovimientoContable.emisor_nombre != "")
        .order_by(MovimientoContable.fecha.desc(), MovimientoContable.id.desc())
        .first()
    )


def find_emisor_contable_por_rut(rut_raw: str | None) -> EmisorContable | None:
    cr = clean_rut(rut_raw)
    if len(cr) < 7:
        return None
    return EmisorContable.query.filter_by(rut=cr, activo=True).first()


def emisor_form_data(form) -> dict:
    src = form if form is not None else {}
    rut_raw = (src.get("rut") or "").strip()
    cr = clean_rut(rut_raw)
    region = (src.get("region") or src.get("region_text") or "").strip()[:120]
    comuna = (src.get("comuna") or src.get("comuna_text") or "").strip()[:120]
    return {
        "rut": format_rut(cr) if cr else rut_raw,
        "nombre": _upper_text(src.get("nombre"))[:200],
        "giro": _upper_text(src.get("giro"))[:200],
        "direccion": _upper_text(src.get("direccion"))[:300],
        "region": _upper_text(region)[:120],
        "comuna": _upper_text(comuna)[:120],
        "ciudad": _upper_text(src.get("ciudad"))[:120],
        "pais": _normalize_pais(src.get("pais")),
        "telefono": (src.get("telefono") or "").strip()[:50],
        "email": _normalize_email(src.get("email"))[:150],
        "notas": _upper_text(src.get("notas"))[:2000],
    }


def emisor_to_form_dict(emisor: EmisorContable) -> dict:
    return {
        "rut": format_rut(emisor.rut) if emisor.rut else "",
        "nombre": emisor.nombre or "",
        "giro": emisor.giro or "",
        "direccion": emisor.direccion or "",
        "region": emisor.region or "",
        "comuna": emisor.comuna or "",
        "ciudad": emisor.ciudad or "",
        "pais": emisor.pais or "Chile",
        "telefono": emisor.telefono or "",
        "email": emisor.email or "",
        "notas": emisor.notas or "",
    }


def validate_emisor_form(data: dict, emisor_id: int | None = None) -> list[str]:
    errors: list[str] = []
    nombre = (data.get("nombre") or "").strip()
    rut_display = (data.get("rut") or "").strip()
    cr = clean_rut(rut_display)
    if not nombre:
        errors.append("La razón social es obligatoria.")
    if not cr:
        errors.append("El RUT es obligatorio.")
    elif not is_valid_rut(cr):
        errors.append("El RUT no es válido.")
    else:
        q = EmisorContable.query.filter_by(rut=cr)
        if emisor_id:
            q = q.filter(EmisorContable.id != emisor_id)
        if q.first():
            errors.append("Ya existe un emisor con ese RUT en el directorio contable.")
    return errors


def hydrate_emisor(emisor: EmisorContable, data: dict) -> EmisorContable:
    cr = clean_rut(data.get("rut"))
    emisor.rut = cr
    emisor.nombre = _upper_text(data.get("nombre"))[:200]
    emisor.giro = _upper_text(data.get("giro"))[:200]
    emisor.direccion = _upper_text(data.get("direccion"))[:300]
    emisor.region = _upper_text(data.get("region"))[:120]
    emisor.comuna = _upper_text(data.get("comuna"))[:120]
    emisor.ciudad = _upper_text(data.get("ciudad"))[:120]
    emisor.pais = _normalize_pais(data.get("pais"))
    emisor.telefono = (data.get("telefono") or "").strip()[:50]
    emisor.email = _normalize_email(data.get("email"))[:150]
    emisor.notas = _upper_text(data.get("notas"))[:2000]
    emisor.updated_at = datetime.utcnow()
    return emisor


def upsert_emisor_contable_desde_movimiento(
    rut_raw: str | None, nombre: str | None
) -> EmisorContable | None:
    """Crea o actualiza ficha mínima al registrar un movimiento (sin pisar datos ya editados)."""
    cr = clean_rut(rut_raw)
    nombre_s = _upper_text(nombre)[:200]
    if len(cr) < 7 or not nombre_s:
        return None
    row = EmisorContable.query.filter_by(rut=cr).first()
    if row is None:
        row = EmisorContable(rut=cr, nombre=nombre_s)
        db.session.add(row)
        return row
    if not (row.nombre or "").strip():
        row.nombre = nombre_s
    row.updated_at = datetime.utcnow()
    return row


def backfill_emisores_desde_movimientos() -> int:
    """Importa emisores únicos del libro diario al directorio (idempotente)."""
    movs = (
        MovimientoContable.query.filter(
            MovimientoContable.emisor_rut.isnot(None),
            MovimientoContable.emisor_rut != "",
            MovimientoContable.emisor_nombre.isnot(None),
            MovimientoContable.emisor_nombre != "",
        )
        .order_by(MovimientoContable.fecha.desc(), MovimientoContable.id.desc())
        .all()
    )
    existentes = {e.rut for e in EmisorContable.query.with_entities(EmisorContable.rut).all()}
    nuevos = 0
    vistos: set[str] = set()
    for mov in movs:
        cr = clean_rut(mov.emisor_rut or "")
        if len(cr) < 7 or cr in vistos or cr in existentes:
            continue
        vistos.add(cr)
        nombre = _upper_text(mov.emisor_nombre)[:200]
        if not nombre:
            continue
        db.session.add(EmisorContable(rut=cr, nombre=nombre))
        nuevos += 1
    if nuevos:
        db.session.commit()
    return nuevos


def normalizar_emisores_existentes_mayusculas() -> int:
    """Pasa a MAYÚSCULAS fichas ya guardadas (idempotente; no toca email/teléfono/RUT)."""
    caps = {
        "nombre": 200,
        "giro": 200,
        "direccion": 300,
        "region": 120,
        "comuna": 120,
        "ciudad": 120,
        "notas": 2000,
    }
    actualizados = 0
    for emisor in EmisorContable.query.all():
        changed = False
        for attr, maxlen in caps.items():
            raw = getattr(emisor, attr, "") or ""
            up = _upper_text(raw)[:maxlen]
            if up != raw:
                setattr(emisor, attr, up)
                changed = True
        email_norm = _normalize_email(emisor.email)
        if email_norm != (emisor.email or ""):
            emisor.email = email_norm
            changed = True
        if changed:
            emisor.updated_at = datetime.utcnow()
            actualizados += 1
    if actualizados:
        db.session.commit()
    return actualizados


def resolve_emisor_por_rut(rut_raw: str | None) -> dict | None:
    """Catálogo contable primero; si no, último movimiento del libro diario."""
    emisor = find_emisor_contable_por_rut(rut_raw)
    if emisor is not None:
        data = emisor.to_dict()
        data["fuente"] = "catalogo"
        return data
    mov = find_emisor_libro_diario_por_rut(rut_raw or "")
    if mov is None:
        return None
    return {
        "fuente": "movimiento",
        "emisor_nombre": (mov.emisor_nombre or "").strip(),
        "emisor_rut": format_rut(mov.emisor_rut) or (mov.emisor_rut or "").strip(),
    }
