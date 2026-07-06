"""Lógica de negocio y stock para Órdenes de Compra Cliente."""

import uuid
from calendar import monthrange
from datetime import date, datetime

from sqlalchemy import text

from app.extensions import db
from app.utils.stock_control import get_available_stock
from app.ventas.models import Cliente
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
        if (oc.pago_grupo_id or "").strip():
            hermanas = (
                OrdenCompraCliente.query.filter(
                    OrdenCompraCliente.pago_grupo_id == oc.pago_grupo_id,
                    OrdenCompraCliente.id != oc.id,
                )
                .order_by(OrdenCompraCliente.numero_oc.asc())
                .all()
            )
            nums = [h.numero_oc for h in hermanas if h.numero_oc]
            if nums:
                det += f" · Pago conjunto con OC {', '.join(nums)}"
            if oc.monto_pago_grupo:
                det += f" · Monto único ${oc.monto_pago_grupo:,.0f}".replace(",", ".")
        ref = (oc.referencia_pago or "").strip()
        if ref:
            det += f" · Ref. {ref}"
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


def registrar_pagos_conjuntos(
    oc_ids: list[int],
    facturas: dict[int, str],
    fecha_pago: datetime,
    metodo_pago: str,
    monto_recibido: float | None = None,
    referencia_pago: str | None = None,
) -> tuple[list[OrdenCompraCliente], list[str]]:
    """Marca varias OC como pagadas con un mismo abono (factura distinta por OC)."""
    errors: list[str] = []
    ids = sorted({int(i) for i in oc_ids if int(i) > 0})
    if not ids:
        return [], ["Debe seleccionar al menos una OC."]

    ocs = OrdenCompraCliente.query.filter(OrdenCompraCliente.id.in_(ids)).all()
    if len(ocs) != len(ids):
        return [], ["Una o más órdenes de compra no existen."]

    cliente_ids = {o.cliente_id for o in ocs}
    if len(cliente_ids) > 1:
        return [], ["Todas las OC deben ser del mismo cliente."]

    for oc in ocs:
        if (oc.estado or "") != "entregada":
            errors.append(f"OC {oc.numero_oc}: no está pendiente de pago.")
        nf = (facturas.get(oc.id) or "").strip()
        if not nf:
            errors.append(f"OC {oc.numero_oc}: ingrese el número de factura.")

    suma = round(sum(float(o.total or 0) for o in ocs), 2)
    if monto_recibido is not None and monto_recibido > 0:
        tolerancia = max(10.0, suma * 0.01)
        if abs(monto_recibido - suma) > tolerancia:
            errors.append(
                f"El monto recibido (${monto_recibido:,.0f}) no cuadra con la suma "
                f"de las OC (${suma:,.0f})."
            )

    if errors:
        return [], errors

    grupo_id = uuid.uuid4().hex
    ref = (referencia_pago or "").strip()[:120] or None
    monto_grupo = round(float(monto_recibido), 2) if monto_recibido and monto_recibido > 0 else suma
    now = datetime.utcnow()

    for oc in ocs:
        oc.estado = "pagada"
        oc.numero_factura = facturas[oc.id][:60]
        oc.fecha_pago = fecha_pago
        oc.metodo_pago = metodo_pago
        oc.pago_grupo_id = grupo_id if len(ocs) > 1 else None
        oc.referencia_pago = ref
        oc.monto_pago_grupo = monto_grupo if len(ocs) > 1 else None
        oc.updated_at = now

    return ocs, []


_MESES_ES = (
    "",
    "Enero",
    "Febrero",
    "Marzo",
    "Abril",
    "Mayo",
    "Junio",
    "Julio",
    "Agosto",
    "Septiembre",
    "Octubre",
    "Noviembre",
    "Diciembre",
)


def historial_cobros_mes(
    year: int | None = None,
    month: int | None = None,
) -> dict:
    """Agrupa cobros del mes por abono (pago conjunto o individual)."""
    from flask import url_for

    today = date.today()
    y = int(year or today.year)
    m = int(month or today.month)
    mes_inicio = datetime(y, m, 1)
    _, last_day = monthrange(y, m)
    mes_fin = datetime(y, m, last_day, 23, 59, 59)

    ocs = (
        OrdenCompraCliente.query.filter(
            OrdenCompraCliente.estado == "pagada",
            OrdenCompraCliente.fecha_pago >= mes_inicio,
            OrdenCompraCliente.fecha_pago <= mes_fin,
        )
        .order_by(OrdenCompraCliente.fecha_pago.desc(), OrdenCompraCliente.id.desc())
        .all()
    )

    clientes_map: dict[int, str] = {}
    cids = {o.cliente_id for o in ocs if o.cliente_id}
    if cids:
        for cl in Cliente.query.filter(Cliente.id.in_(cids)).all():
            clientes_map[cl.id] = cl.nombre

    grupos: dict[str, list[OrdenCompraCliente]] = {}
    for oc in ocs:
        gid = (oc.pago_grupo_id or "").strip()
        key = gid if gid else f"single_{oc.id}"
        grupos.setdefault(key, []).append(oc)

    items: list[dict] = []
    for key, rows in grupos.items():
        rows.sort(key=lambda o: (o.numero_oc or ""))
        first = rows[0]
        total_ordenes = round(sum(float(o.total or 0) for o in rows), 2)
        monto_abono = first.monto_pago_grupo
        if monto_abono is None:
            monto_abono = total_ordenes
        fp = first.fecha_pago
        items.append(
            {
                "pago_key": key,
                "es_conjunto": bool((first.pago_grupo_id or "").strip()),
                "fecha_pago": fp.strftime("%d/%m/%Y") if fp else "—",
                "fecha_pago_sort": fp.isoformat() if fp else "",
                "metodo_pago": first.metodo_pago or "",
                "referencia_pago": (first.referencia_pago or "").strip(),
                "monto_abono": float(monto_abono or 0),
                "cliente_nombre": clientes_map.get(first.cliente_id, "—"),
                "total_ordenes": total_ordenes,
                "ordenes": [
                    {
                        "id": o.id,
                        "numero_oc": o.numero_oc,
                        "numero_factura": (o.numero_factura or "").strip() or "—",
                        "total": float(o.total or 0),
                        "detalle_url": url_for("oc_clientes.detalle", oid=o.id),
                    }
                    for o in rows
                ],
            }
        )

    items.sort(key=lambda x: x.get("fecha_pago_sort") or "", reverse=True)
    total_cobrado = round(sum(float(it["monto_abono"] or 0) for it in items), 2)

    mes_label = f"{_MESES_ES[m]} {y}" if 1 <= m <= 12 else f"{m:02d}/{y}"
    return {
        "mes_label": mes_label,
        "total_cobrado": total_cobrado,
        "cantidad_abonos": len(items),
        "cantidad_oc": len(ocs),
        "items": items,
    }
