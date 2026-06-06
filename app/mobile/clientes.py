"""Clientes mobile: reutiliza validación y persistencia del módulo Ventas."""
from __future__ import annotations

from app.extensions import db
from app.utils.permissions import has_permission
from app.ventas.models import Cliente, DocumentoVenta
from app.ventas.routes import (
    _apply_entity_search,
    _client_by_id,
    _cliente_form_data,
    _hydrate_cliente,
    _validate_cliente_data,
)

_TIPO_LABELS = {
    "cotizacion": "Cotización",
    "orden_venta": "Orden de venta",
    "factura": "Factura",
    "boleta": "Boleta",
}


def puede_gestionar_clientes(user: str | None, rol: str | None) -> bool:
    return has_permission(user, rol, "ventas_guardar_documento")


def listar_clientes(q: str = "", limit: int = 200) -> list[dict]:
    query = Cliente.query.filter_by(activo=True)
    lista = _apply_entity_search(query, Cliente, (q or "").strip()).order_by(Cliente.nombre).limit(limit).all()
    return [c.to_dict() for c in lista]


def cliente_card(c: Cliente) -> dict:
    d = c.to_dict()
    d["direccion_full"] = ", ".join(
        p for p in [(c.direccion or "").strip(), (c.comuna or "").strip(), (c.region or "").strip()] if p
    )
    return d


def cliente_detalle(cid: int) -> dict | None:
    cliente = _client_by_id(cid)
    if cliente is None:
        return None
    data = cliente_card(cliente)
    docs = (
        DocumentoVenta.query.filter_by(cliente_id=cid)
        .order_by(DocumentoVenta.fecha_documento.desc(), DocumentoVenta.id.desc())
        .limit(10)
        .all()
    )
    historial = []
    for doc in docs:
        tipo = (doc.tipo or "").strip().lower()
        historial.append(
            {
                "id": doc.id,
                "tipo": tipo,
                "tipo_label": _TIPO_LABELS.get(tipo, tipo.capitalize() or "Documento"),
                "numero": (doc.numero or f"#{doc.id}").strip(),
                "total": float(doc.total or 0),
                "total_fmt": _fmt_total(doc.total),
                "fecha": doc.fecha_documento.strftime("%d-%m-%Y") if doc.fecha_documento else "—",
                "status": (doc.status or "").strip(),
            }
        )
    data["historial"] = historial
    return data


def _fmt_total(valor) -> str:
    try:
        n = round(float(valor or 0))
    except (TypeError, ValueError):
        n = 0
    return "${:,.0f}".format(n).replace(",", ".")


def guardar_cliente_nuevo(form) -> tuple[bool, dict]:
    form_data = _cliente_form_data(form)
    errors = _validate_cliente_data(form_data)
    if errors:
        return False, {"errors": errors, "cliente": form_data}
    cliente = _hydrate_cliente(Cliente(), form_data)
    db.session.add(cliente)
    db.session.commit()
    return True, {"cliente_id": cliente.id}


def guardar_cliente_editar(cid: int, form) -> tuple[bool, dict]:
    cliente = _client_by_id(cid)
    if cliente is None:
        return False, {"errors": ["Cliente no encontrado."], "cliente": {}}
    form_data = _cliente_form_data(form)
    errors = _validate_cliente_data(form_data)
    if errors:
        return False, {"errors": errors, "cliente": form_data}
    _hydrate_cliente(cliente, form_data)
    db.session.commit()
    return True, {"cliente_id": cliente.id}


def desactivar_cliente(cid: int) -> bool:
    cliente = db.session.get(Cliente, cid)
    if cliente is None or not cliente.activo:
        return False
    cliente.activo = False
    db.session.commit()
    return True
