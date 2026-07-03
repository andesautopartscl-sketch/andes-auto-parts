"""Lógica de negocio y stock para Órdenes de Compra Cliente."""

from __future__ import annotations

from sqlalchemy import text

from app.extensions import db
from app.utils.stock_control import get_available_stock
from .models import OrdenCompraCliente, OrdenCompraClienteItem, oc_estado_label


def codigo_en_inventario(codigo: str) -> bool:
    code = (codigo or "").strip().upper()
    if not code:
        return False
    row = db.session.execute(
        text(
            """
            SELECT 1 FROM productos
            WHERE UPPER(CODIGO) = :codigo AND COALESCE(ACTIVO, 1) = 1
            LIMIT 1
            """
        ),
        {"codigo": code},
    ).first()
    return row is not None


def normalizar_numero_oc(numero: str | None) -> str:
    return (numero or "").strip().upper()


def buscar_oc_por_numero(
    numero_oc: str,
    exclude_id: int | None = None,
) -> OrdenCompraCliente | None:
    """Busca OC por número (insensible a mayúsculas/espacios)."""
    key = normalizar_numero_oc(numero_oc)
    if not key:
        return None
    q = OrdenCompraCliente.query.filter(
        db.func.upper(db.func.trim(OrdenCompraCliente.numero_oc)) == key
    )
    if exclude_id:
        q = q.filter(OrdenCompraCliente.id != exclude_id)
    return q.order_by(OrdenCompraCliente.id.desc()).first()


def calcular_totales_items(items: list[dict]) -> dict:
    neto = 0.0
    for it in items:
        cant = max(int(it.get("cantidad") or 0), 0)
        precio = float(it.get("precio_unitario") or 0)
        desc_pct = float(it.get("descuento_item") or 0)
        bruto = cant * precio
        desc_monto = bruto * (desc_pct / 100.0) if desc_pct else 0.0
        neto += round(bruto - desc_monto, 2)
    neto = round(neto, 2)
    iva = round(neto * 0.19, 2)
    total = round(neto + iva, 2)
    return {"neto": neto, "iva": iva, "total": total}


def validar_stock_items_inventario(items: list[OrdenCompraClienteItem]) -> list[str]:
    errors: list[str] = []
    for item in items:
        if not item.en_inventario:
            continue
        codigo = (item.codigo_producto or "").strip().upper()
        cant = int(item.cantidad or 0)
        if cant <= 0:
            continue
        avail = get_available_stock(codigo, item.marca or None, item.bodega or None)
        if avail < cant:
            errors.append(
                f"{codigo}: stock insuficiente (disponible {avail}, requerido {cant})"
            )
    return errors


def descontar_stock_oc(
    oc: OrdenCompraCliente,
    usuario: str,
) -> tuple[int, int, list[str]]:
    """
    Descuenta stock de ítems en inventario. Todo-o-nada.
    Retorna (descontados, omitidos, errores).
    """
    from app.ventas.routes import _adjust_product_stock, _normalize_origen_compra

    inventario_items = [i for i in oc.items if i.en_inventario]
    omitidos = len([i for i in oc.items if not i.en_inventario])

    errors = validar_stock_items_inventario(inventario_items)
    if errors:
        return 0, omitidos, errors

    descontados = 0
    reason = f"Entrega OC cliente {oc.numero_oc or oc.id}"
    for item in inventario_items:
        qty = int(item.cantidad or 0)
        if qty <= 0:
            continue
        err = _adjust_product_stock(
            codigo=(item.codigo_producto or "").strip().upper(),
            marca=(item.marca or "").strip().upper(),
            bodega=(item.bodega or "").strip() or "Bodega 1",
            origen_compra=_normalize_origen_compra(""),
            delta=-qty,
            reason=reason,
        )
        if err:
            return 0, omitidos, [err]
        item.stock_descontado = True
        descontados += 1

    oc.stock_deducted = descontados > 0
    return descontados, omitidos, []


def timeline_eventos(oc: OrdenCompraCliente) -> list[dict]:
    events: list[dict] = []
    if oc.created_at:
        events.append(
            {
                "estado": "recibida",
                "label": "OC recibida",
                "fecha": oc.created_at,
                "detalle": f"Registrada por {oc.usuario or 'sistema'}",
            }
        )
    if oc.fecha_entrega_real and (oc.estado or "") in {"entregada", "pagada"}:
        guia = (oc.numero_guia_despacho or "").strip()
        det = "Entrega confirmada"
        if guia:
            det += f" · Guía {guia}"
        if oc.stock_deducted:
            det += " · Stock descontado"
        events.append(
            {
                "estado": "entregada",
                "label": oc_estado_label("entregada"),
                "fecha": oc.fecha_entrega_real,
                "detalle": det,
            }
        )
    if oc.fecha_pago and (oc.estado or "") == "pagada":
        nf = (oc.numero_factura or "").strip()
        mp = (oc.metodo_pago or "").strip()
        det = f"Factura {nf}" if nf else "Pago registrado"
        if mp:
            det += f" · {mp.replace('_', ' ')}"
        events.append(
            {
                "estado": "pagada",
                "label": "Pagada",
                "fecha": oc.fecha_pago,
                "detalle": det,
            }
        )
    if (oc.estado or "") == "anulada":
        events.append(
            {
                "estado": "anulada",
                "label": "Anulada",
                "fecha": oc.updated_at or oc.created_at,
                "detalle": "Orden anulada",
            }
        )
    return sorted(events, key=lambda e: e["fecha"] or oc.created_at)


def listar_oc_por_cliente(cliente_id: int) -> list[dict]:
    from flask import url_for

    rows = (
        OrdenCompraCliente.query.filter_by(cliente_id=cliente_id)
        .order_by(OrdenCompraCliente.fecha_oc.desc(), OrdenCompraCliente.id.desc())
        .all()
    )
    labels = {
        "recibida": ("Recibida", "blue"),
        "entregada": (oc_estado_label("entregada"), "orange"),
        "pagada": ("Pagada", "green"),
        "anulada": ("Anulada", "slate"),
    }
    out = []
    for oc in rows:
        lab, badge = labels.get((oc.estado or "").strip().lower(), (oc.estado, "slate"))
        out.append(
            {
                "id": oc.id,
                "numero_oc": oc.numero_oc,
                "estado": oc.estado,
                "estado_label": lab,
                "badge": badge,
                "fecha_oc": oc.fecha_oc.strftime("%d-%m-%Y") if oc.fecha_oc else "—",
                "total": float(oc.total or 0),
                "view_url": url_for("oc_clientes.detalle", oid=oc.id),
            }
        )
    return out
