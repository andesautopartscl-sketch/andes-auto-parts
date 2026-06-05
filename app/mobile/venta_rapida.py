"""Venta rápida mobile: reutiliza persistencia y stock del módulo Ventas."""
from __future__ import annotations

from datetime import datetime

from flask import session

from app.extensions import db
from app.utils.permissions import has_permission
from app.utils.rut_utils import clean_rut
from app.utils.stock_control import check_stock_availability, validate_sale_items
from app.ventas.models import Cliente
from app.ventas.routes import (
    METODO_PAGO_OPTIONS,
    _apply_stock_for_document,
    _calculate_totals,
    _client_by_id,
    _doc_validation_errors,
    _entity_snapshot,
    _fill_precio_desde_ingreso_si_vacio,
    _next_doc_number,
    _normalize_origen_compra,
    _origin_selection_errors,
    _product_by_code,
    _product_variants_by_code,
    _sales_doc_prefix,
    _save_or_update_document,
    _serialize_product,
    _ultimo_ingreso_ref,
)

MOBILE_ORIGIN_TAG = "[Andes Mobile]"
CONSUMIDOR_FINAL_NOMBRE = "Consumidor final"

METODO_MOBILE_MAP = {
    "efectivo": "efectivo",
    "transferencia": "transferencia",
    "tarjeta": "tarjeta_debito",
    "credito": "credito_30",
}

METODOS_PAGO_INMEDIATO = {
    "efectivo",
    "transferencia",
    "tarjeta_debito",
    "tarjeta_credito",
    "cheque",
}


def puede_registrar_venta(user: str | None, rol: str | None) -> bool:
    return has_permission(user, rol, "ventas_guardar_documento")


def buscar_clientes(q: str, limit: int = 30) -> list[dict]:
    term = (q or "").strip()
    query = Cliente.query.filter_by(activo=True)
    if term:
        like = f"%{term}%"
        normalized = clean_rut(term)
        filters = [
            Cliente.nombre.ilike(like),
            Cliente.rut.ilike(like),
            Cliente.giro.ilike(like),
            Cliente.email.ilike(like),
        ]
        if normalized:
            filters.append(Cliente.rut.ilike(f"%{normalized}%"))
        from sqlalchemy import or_

        query = query.filter(or_(*filters))
    rows = query.order_by(Cliente.nombre).limit(max(1, min(limit, 50))).all()
    return [c.to_dict() for c in rows]


def _cliente_ancla_consumidor_final() -> Cliente | None:
    """Cliente registrado que cumple validación ERP para boleta sin RUT en documento."""
    from sqlalchemy import or_

    patrones = Cliente.query.filter_by(activo=True).filter(
        or_(
            Cliente.nombre.ilike("%consumidor%"),
            Cliente.nombre.ilike("%mostrador%"),
            Cliente.nombre.ilike("%sin rut%"),
        )
    ).order_by(Cliente.id.asc()).first()
    if patrones is not None:
        return patrones
    sin_rut = (
        Cliente.query.filter_by(activo=True)
        .filter(or_(Cliente.rut == "", Cliente.rut.is_(None)))
        .order_by(Cliente.id.asc())
        .first()
    )
    if sin_rut is not None:
        return sin_rut
    return Cliente.query.filter_by(activo=True).order_by(Cliente.id.asc()).first()


def _party_desde_cliente(cliente: Cliente | None, consumidor_final: bool) -> dict:
    if consumidor_final:
        return {
            "name": CONSUMIDOR_FINAL_NOMBRE,
            "rut": "",
            "address": "",
            "telefono": "",
            "email": "",
        }
    snap = _entity_snapshot(cliente, False)
    return {
        "name": snap["name"],
        "rut": snap["rut"],
        "address": snap["address"],
        "telefono": snap["telefono"],
        "email": snap["email"],
    }


def producto_linea_venta(codigo_raw: str, cantidad: int = 1, precio: float | None = None) -> dict | None:
    codigo = (codigo_raw or "").strip().upper()
    if not codigo:
        return None
    producto = _product_by_code(codigo)
    if producto is None:
        return None
    variantes = _product_variants_by_code(codigo)
    payload = _serialize_product(producto, codigo=codigo, variantes=variantes)
    ref_marca = payload.get("default_marca") or ""
    ref_bodega = (payload.get("default_bodega") or "").strip() or "Bodega 1"
    ref_origen = _normalize_origen_compra(payload.get("default_origen_compra") or "")
    payload["ingreso_ref"] = _ultimo_ingreso_ref(codigo, ref_marca, ref_bodega, ref_origen)
    _fill_precio_desde_ingreso_si_vacio(payload)
    unit = float(precio if precio is not None and precio > 0 else payload.get("precio") or 0)
    qty = max(1, int(cantidad or 1))
    return {
        "codigo": codigo,
        "descripcion": (payload.get("descripcion") or "").strip(),
        "cantidad": qty,
        "precio": round(unit, 2),
        "subtotal": round(qty * unit, 2),
        "marca": ref_marca,
        "bodega": ref_bodega,
        "origen_compra": ref_origen,
        "stock_disponible": int(payload.get("default_stock") or payload.get("stock") or 0),
        "precio_fmt": None,
    }


def _items_para_stock_check(items: list[dict]) -> list[dict]:
    return [
        {
            "codigo_producto": (it.get("codigo") or "").strip().upper(),
            "marca": (it.get("marca") or "").strip().upper() or None,
            "bodega": (it.get("bodega") or "").strip() or "Bodega 1",
            "cantidad": int(it.get("cantidad") or 0),
            "precio_unitario": float(it.get("precio") or 0),
        }
        for it in items
        if (it.get("codigo") or "").strip()
    ]


def _resolver_tipo_documento(cliente: Cliente | None, consumidor_final: bool) -> str:
    if consumidor_final:
        return "boleta"
    rut = clean_rut(getattr(cliente, "rut", "") or "")
    return "factura" if rut else "boleta"


def registrar_venta_rapida(payload: dict) -> tuple[bool, dict]:
    """
    Persiste boleta/factura usando las mismas funciones del ERP.
    payload: cliente_id|consumidor_final, items[], metodo_pago, observacion
    """
    if not puede_registrar_venta(session.get("user"), session.get("rol")):
        return False, {"message": "Sin permiso para registrar ventas."}

    consumidor_final = bool(payload.get("consumidor_final"))
    cliente_id = int(payload.get("cliente_id") or 0)
    raw_items = payload.get("items") or []
    metodo_mobile = (payload.get("metodo_pago") or "efectivo").strip().lower()
    observacion_usuario = (payload.get("observacion") or "").strip()
    metodo_erp = METODO_MOBILE_MAP.get(metodo_mobile, metodo_mobile)
    if metodo_erp not in METODO_PAGO_OPTIONS:
        return False, {"message": f"Método de pago no válido: {metodo_mobile}"}

    if not raw_items:
        return False, {"message": "Agrega al menos un producto."}

    items: list[dict] = []
    for row in raw_items:
        codigo = (row.get("codigo") or "").strip().upper()
        if not codigo:
            continue
        qty = max(1, int(row.get("cantidad") or 1))
        precio_override = row.get("precio")
        precio_val = float(precio_override) if precio_override not in (None, "") else None
        linea = producto_linea_venta(codigo, qty, precio_val)
        if linea is None:
            return False, {"message": f"Producto no encontrado: {codigo}"}
        if linea["precio"] <= 0:
            return False, {"message": f"El producto {codigo} no tiene precio de venta."}
        items.append(linea)

    if not items:
        return False, {"message": "No hay productos válidos en la venta."}

    selected_client: Cliente | None = None
    selected_client_id = 0
    if consumidor_final:
        selected_client = _cliente_ancla_consumidor_final()
        if selected_client is None:
            return False, {
                "message": "No hay clientes activos. Cree un cliente en Ventas para ventas sin registrar.",
            }
        selected_client_id = int(selected_client.id)
    else:
        if cliente_id <= 0:
            return False, {"message": "Selecciona un cliente o usa venta a consumidor final."}
        selected_client = _client_by_id(cliente_id)
        if selected_client is None:
            return False, {"message": "Cliente no encontrado o inactivo."}
        selected_client_id = int(selected_client.id)

    party = _party_desde_cliente(selected_client, consumidor_final)
    tipo_documento = _resolver_tipo_documento(selected_client, consumidor_final)
    totals = _calculate_totals(items, selected_client if not consumidor_final else None)

    errors = _doc_validation_errors(
        "factura",
        tipo_documento,
        selected_client,
        None,
        party,
        items,
    )
    errors.extend(_origin_selection_errors(items))
    if errors:
        return False, {"message": errors[0], "errors": errors}

    stock_rows = _items_para_stock_check(items)
    ok_items, msg_items = validate_sale_items(stock_rows)
    if not ok_items:
        return False, {"message": msg_items or "Items inválidos."}
    ok_stock, msg_stock = check_stock_availability(stock_rows)
    if not ok_stock:
        return False, {"message": msg_stock or "Stock insuficiente."}

    today = datetime.now().strftime("%Y-%m-%d")
    prefix = _sales_doc_prefix(tipo_documento)
    doc_number = _next_doc_number(prefix)
    notes_parts = [MOBILE_ORIGIN_TAG]
    if observacion_usuario:
        notes_parts.append(observacion_usuario)
    notes = " — ".join(notes_parts)[:500]

    doc_status = "aprobada" if metodo_erp in METODOS_PAGO_INMEDIATO else "pendiente"

    try:
        documento = _save_or_update_document(
            doc_type="factura",
            doc_number=doc_number,
            doc_date=today,
            doc_valid_until=today,
            tipo_documento=tipo_documento,
            status=doc_status,
            selected_client_id=selected_client_id,
            selected_proveedor_id=0,
            selected_party=selected_client,
            party=party,
            items=items,
            totals=totals,
            notes=notes,
        )
        documento.metodo_pago = metodo_erp
        if metodo_erp in METODOS_PAGO_INMEDIATO:
            documento.estado_pago = "pagado"
        else:
            documento.estado_pago = "pendiente"

        if not documento.stock_deducted:
            ok, stock_errors = _apply_stock_for_document(
                documento,
                direction="out",
                reason=f"{tipo_documento.capitalize()} {documento.numero or documento.id} {MOBILE_ORIGIN_TAG}",
            )
            if not ok:
                raise ValueError("; ".join(stock_errors))

        db.session.commit()
        return True, {
            "doc_id": documento.id,
            "numero": (documento.numero or "").strip(),
            "tipo": (documento.tipo or tipo_documento).strip(),
            "total": float(documento.total or 0),
            "cliente": (documento.cliente_nombre or "").strip(),
            "metodo_pago": documento.metodo_pago,
            "estado_pago": documento.estado_pago,
        }
    except Exception as exc:
        db.session.rollback()
        return False, {"message": str(exc)}
