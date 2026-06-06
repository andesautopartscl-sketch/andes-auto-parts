"""Proveedores mobile: reutiliza validación y persistencia del módulo Ventas."""
from __future__ import annotations

from sqlalchemy import or_

from app.bodega.models import IngresoDocumento
from app.extensions import db
from app.utils.permissions import has_permission
from app.utils.rut_utils import clean_rut
from app.ventas.models import Proveedor
from app.ventas.routes import (
    _build_supplier_history_payload,
    _hydrate_proveedor,
    _proveedor_by_id,
    _proveedor_form_data,
    _validate_proveedor_data,
)


def puede_gestionar_proveedores(user: str | None, rol: str | None) -> bool:
    return has_permission(user, rol, "ventas_guardar_documento")


def _buscar_proveedores_query(q: str):
    query = Proveedor.query.filter_by(activo=True)
    term = (q or "").strip()
    if not term:
        return query
    like = f"%{term}%"
    normalized = clean_rut(term)
    filters = [
        Proveedor.nombre.ilike(like),
        Proveedor.empresa.ilike(like),
        Proveedor.rut.ilike(like),
        Proveedor.giro.ilike(like),
        Proveedor.email.ilike(like),
        Proveedor.telefono.ilike(like),
    ]
    if normalized:
        filters.append(Proveedor.rut.ilike(f"%{normalized}%"))
    return query.filter(or_(*filters))


def _ultimo_ingreso_label(proveedor: Proveedor) -> str:
    rut_norm = clean_rut(proveedor.rut or "")
    ing = (
        IngresoDocumento.query.filter(
            or_(
                IngresoDocumento.proveedor_id == proveedor.id,
                IngresoDocumento.proveedor_rut == rut_norm,
            )
        )
        .order_by(IngresoDocumento.created_at.desc(), IngresoDocumento.id.desc())
        .first()
    )
    if not ing:
        return "—"
    if ing.fecha_documento:
        return ing.fecha_documento.strftime("%d-%m-%Y")
    if ing.created_at:
        return ing.created_at.strftime("%d-%m-%Y")
    return f"#{ing.id}"


def listar_proveedores(q: str = "", limit: int = 200) -> list[dict]:
    rows = _buscar_proveedores_query(q).order_by(Proveedor.empresa, Proveedor.nombre).limit(limit).all()
    out = []
    for p in rows:
        d = p.to_dict()
        d["display_name"] = (p.empresa or p.nombre or "").strip()
        d["ultimo_ingreso"] = _ultimo_ingreso_label(p)
        out.append(d)
    return out


def proveedor_detalle(pid: int) -> dict | None:
    proveedor, payload = _build_supplier_history_payload(pid)
    if proveedor is None or payload is None:
        return None
    data = proveedor.to_dict()
    data["display_name"] = (proveedor.empresa or proveedor.nombre or "").strip()
    ingresos = [d for d in (payload.get("documentos") or []) if d.get("type") == "ingreso"][:10]
    data["ingresos"] = ingresos
    data["productos"] = payload.get("homologaciones") or []
    return data


def buscar_proveedores(q: str, limit: int = 30) -> list[dict]:
    return listar_proveedores(q, limit=limit)


def guardar_proveedor_nuevo(form) -> tuple[bool, dict]:
    form_data = _proveedor_form_data(form)
    errors = _validate_proveedor_data(form_data)
    if errors:
        return False, {"errors": errors, "proveedor": form_data}
    proveedor = _hydrate_proveedor(Proveedor(), form_data)
    db.session.add(proveedor)
    db.session.commit()
    return True, {"proveedor_id": proveedor.id}


def guardar_proveedor_editar(pid: int, form) -> tuple[bool, dict]:
    proveedor = _proveedor_by_id(pid)
    if proveedor is None:
        return False, {"errors": ["Proveedor no encontrado."], "proveedor": {}}
    form_data = _proveedor_form_data(form)
    errors = _validate_proveedor_data(form_data)
    if errors:
        return False, {"errors": errors, "proveedor": form_data}
    _hydrate_proveedor(proveedor, form_data)
    db.session.commit()
    return True, {"proveedor_id": proveedor.id}


def desactivar_proveedor(pid: int) -> bool:
    proveedor = db.session.get(Proveedor, pid)
    if proveedor is None or not proveedor.activo:
        return False
    proveedor.activo = False
    db.session.commit()
    return True
