from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
import os
import sys

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from sqlalchemy import func, text

from app.extensions import db
from app.utils.decorators import login_required
from app.utils.rut_utils import clean_rut, format_rut, is_valid_rut
from app.bodega.models import MovimientoStock, ProductoVarianteStock
from .models import Cliente, Proveedor, DocumentoVenta, DocumentoVentaItem, NotaCredito, NotaCreditoItem

ventas_bp = Blueprint("ventas", __name__, url_prefix="/ventas")

COMPANY_INFO = {
    "name": "ANDES AUTO PARTS LTDA",
    "rut": "78.074.288-7",
    "business": "VENTA DE PARTES, PIEZAS Y ACCESORIOS AUTOMOTRICES",
    "address": "LA CONCEPCION 81 OFICINA 214, PROVIDENCIA",
}

STATUS_OPTIONS = ["pendiente", "aprobada", "entregada"]
TIPO_DOCUMENTO_OPTIONS = ["factura", "boleta"]
CHILE_COUNTRY_NAME = "Chile"
CHILE_GEO_PATH = Path(__file__).resolve().parent / "data" / "chile_geo.json"

METODO_PAGO_OPTIONS = [
    "efectivo",
    "transferencia",
    "tarjeta_debito",
    "tarjeta_credito",
    "credito_30",
    "credito_60",
    "credito_90",
    "cheque",
]
METODO_PAGO_LABELS = {
    "efectivo": "Efectivo",
    "transferencia": "Transferencia bancaria",
    "tarjeta_debito": "Tarjeta débito",
    "tarjeta_credito": "Tarjeta crédito",
    "credito_30": "Crédito 30 días",
    "credito_60": "Crédito 60 días",
    "credito_90": "Crédito 90 días",
    "cheque": "Cheque",
}


# ─────────────────────────────────────────────────────────────
#  UTILITY HELPERS
# ─────────────────────────────────────────────────────────────

def _safe_float(raw: str) -> float:
    v = (raw or "").strip().replace(",", ".")
    try:
        n = float(v)
        return n if n >= 0 else 0.0
    except (ValueError, TypeError):
        return 0.0


def _safe_int(raw: str, default: int = 1) -> int:
    try:
        v = int((raw or "").strip())
        return v if v > 0 else default
    except (ValueError, TypeError):
        return default


def _clean_text(raw: str) -> str:
    return (raw or "").strip()


def _normalize_country(raw: str, default: str = CHILE_COUNTRY_NAME) -> str:
    return _clean_text(raw) or default


def _is_chile_country(country: str) -> bool:
    normalized = _clean_text(country).lower()
    return normalized in {"chile", "cl", "chile (cl)"}


def _load_chile_geo() -> list[dict]:
    try:
        if not CHILE_GEO_PATH.exists():
            return []
        with CHILE_GEO_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return data
    except Exception:
        return []
    return []


def _chile_regions(chile_geo: list[dict]) -> list[str]:
    return [region.get("nombre", "") for region in chile_geo if region.get("nombre")]


def _is_valid_email(raw: str) -> bool:
    email = _clean_text(raw)
    return not email or ("@" in email and "." in email.split("@")[-1])


def _normalized_rut_sql(column):
    return func.upper(func.replace(func.replace(func.coalesce(column, ""), ".", ""), "-", ""))


def _format_currency(value: float) -> str:
    return f"${value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def _entity_display_name(entity) -> str:
    if isinstance(entity, Proveedor):
        return entity.empresa or entity.nombre or ""
    return entity.nombre or ""


def _extract_search_term() -> str:
    return _clean_text(request.args.get("q"))


def _apply_entity_search(query, model, search_term: str):
    if not search_term:
        return query
    term = f"%{search_term}%"
    normalized_term = clean_rut(search_term)
    rut_expr = _normalized_rut_sql(model.rut)
    filters = (
        model.nombre.ilike(term)
        | model.rut.ilike(term)
        | model.giro.ilike(term)
        | model.email.ilike(term)
        | model.comuna.ilike(term)
        | model.ciudad.ilike(term)
        | model.pais.ilike(term)
        | model.telefono.ilike(term)
    )
    if normalized_term:
        filters = filters | rut_expr.ilike(f"%{normalized_term}%")
    return query.filter(filters)


def _full_address(entity) -> str:
    parts = [
        _clean_text(getattr(entity, "direccion", "")),
        _clean_text(getattr(entity, "comuna", "")),
        _clean_text(getattr(entity, "region", "")),
        _normalize_country(getattr(entity, "pais", ""), default=CHILE_COUNTRY_NAME),
    ]
    return ", ".join([p for p in parts if p])


def _extract_items_from_form(form) -> list[dict]:
    if form is None:
        codigos = descripciones = cantidades = precios = marcas = bodegas = []
    elif hasattr(form, "getlist"):
        codigos = form.getlist("item_codigo[]")
        descripciones = form.getlist("item_descripcion[]")
        cantidades = form.getlist("item_cantidad[]")
        precios = form.getlist("item_precio[]")
        marcas = form.getlist("item_marca[]")
        bodegas = form.getlist("item_bodega[]")
    else:
        codigos = form.get("item_codigo[]", []) or []
        descripciones = form.get("item_descripcion[]", []) or []
        cantidades = form.get("item_cantidad[]", []) or []
        precios = form.get("item_precio[]", []) or []
        marcas = form.get("item_marca[]", []) or []
        bodegas = form.get("item_bodega[]", []) or []
        if not isinstance(codigos, list):
            codigos = [codigos]
        if not isinstance(descripciones, list):
            descripciones = [descripciones]
        if not isinstance(cantidades, list):
            cantidades = [cantidades]
        if not isinstance(precios, list):
            precios = [precios]
        if not isinstance(marcas, list):
            marcas = [marcas]
        if not isinstance(bodegas, list):
            bodegas = [bodegas]

    max_len = max(len(codigos), len(descripciones), len(cantidades), len(precios), len(marcas), len(bodegas), 1)
    items = []
    for idx in range(max_len):
        codigo = (codigos[idx] if idx < len(codigos) else "").strip().upper()
        descripcion = (descripciones[idx] if idx < len(descripciones) else "").strip()
        cantidad = _safe_int(cantidades[idx] if idx < len(cantidades) else "1", default=1)
        precio = _safe_float(precios[idx] if idx < len(precios) else "0")
        marca = (marcas[idx] if idx < len(marcas) else "").strip().upper()
        bodega = (bodegas[idx] if idx < len(bodegas) else "").strip() or "Bodega 1"
        if not codigo and not descripcion:
            continue
        items.append({
            "codigo": codigo,
            "descripcion": descripcion,
            "cantidad": cantidad,
            "precio": precio,
            "marca": marca,
            "bodega": bodega,
            "subtotal": round(cantidad * precio, 2),
        })

    if not items:
        items.append({
            "codigo": "",
            "descripcion": "",
            "cantidad": 1,
            "precio": 0.0,
            "marca": "",
            "bodega": "Bodega 1",
            "subtotal": 0.0,
        })
    return items


def _calculate_totals(items: list[dict]) -> dict:
    subtotal = round(sum(i.get("subtotal", 0.0) for i in items), 2)
    iva = round(subtotal * 0.19, 2)
    total = round(subtotal + iva, 2)
    return {"subtotal": subtotal, "iva": iva, "total": total}


def _next_doc_number(prefix: str) -> str:
    safe_prefix = (prefix or "DOC").strip().upper()
    pattern = f"{safe_prefix}-%"
    rows = (
        db.session.query(DocumentoVenta.numero)
        .filter(DocumentoVenta.numero.ilike(pattern))
        .all()
    )
    max_seq = 0
    for row in rows:
        numero = (row[0] or "").strip().upper()
        if not numero.startswith(f"{safe_prefix}-"):
            continue
        suffix = numero.split("-", 1)[1]
        if suffix.isdigit():
            max_seq = max(max_seq, int(suffix))
    return f"{safe_prefix}-{max_seq + 1:04d}"


def _doc_prefix(doc_type: str) -> str:
    mapping = {
        "cotizacion": "CO",
        "orden_venta": "OV",
        "orden_compra": "OC",
        "factura": "FA",
        "factura_proveedor": "FP",
    }
    return mapping.get((doc_type or "").strip().lower(), "DOC")


def _doc_tipo_value(doc_type: str, tipo_documento: str) -> str:
    if doc_type == "factura":
        return (tipo_documento or "factura").strip().lower()
    return doc_type


def _save_or_update_document(
    doc_type: str,
    doc_number: str,
    doc_date: str,
    doc_valid_until: str,
    tipo_documento: str,
    status: str,
    selected_client_id: int,
    selected_proveedor_id: int,
    selected_party,
    party: dict,
    items: list[dict],
    totals: dict,
    notes: str,
    source_id: int | None = None,
    source_type: str = "",
    root_id: int | None = None,
) -> DocumentoVenta:
    tipo_value = _doc_tipo_value(doc_type, tipo_documento)
    numero = (doc_number or "").strip().upper()
    fecha_documento = datetime.strptime(doc_date, "%Y-%m-%d") if doc_date else datetime.now()
    fecha_vencimiento = datetime.strptime(doc_valid_until, "%Y-%m-%d") if doc_valid_until else None

    documento = (
        DocumentoVenta.query.filter_by(tipo=tipo_value, numero=numero).order_by(DocumentoVenta.id.desc()).first()
    )
    is_new = documento is None
    if documento is None:
        documento = DocumentoVenta(tipo=tipo_value, numero=numero)
        db.session.add(documento)

    party_name = (party.get("name") or "").strip()
    if not party_name and doc_type in {"orden_compra", "factura_proveedor"}:
        party_name = "Compra mostrador"
    party_rut = clean_rut(party.get("rut"))
    party_address = (party.get("address") or "").strip()
    party_phone = (party.get("telefono") or "").strip()
    party_email = (party.get("email") or "").strip()

    region = ""
    comuna = ""
    ciudad = ""
    pais = CHILE_COUNTRY_NAME
    if selected_party is not None:
        region = (getattr(selected_party, "region", "") or "").strip()
        comuna = (getattr(selected_party, "comuna", "") or "").strip()
        ciudad = (getattr(selected_party, "ciudad", "") or "").strip()
        pais = (getattr(selected_party, "pais", CHILE_COUNTRY_NAME) or CHILE_COUNTRY_NAME).strip()

    documento.fecha_documento = fecha_documento
    documento.fecha_vencimiento = fecha_vencimiento
    documento.cliente_id = selected_client_id if selected_client_id > 0 else None
    documento.proveedor_id = selected_proveedor_id if selected_proveedor_id > 0 else None
    documento.cliente_rut = party_rut
    documento.cliente_nombre = party_name
    documento.cliente_direccion = party_address
    documento.cliente_region = region
    documento.cliente_ciudad = ciudad or comuna
    documento.cliente_pais = pais
    documento.cliente_telefono = party_phone
    documento.cliente_email = party_email
    documento.subtotal = float(totals.get("subtotal") or 0)
    documento.impuesto = float(totals.get("iva") or 0)
    documento.total = float(totals.get("total") or 0)
    documento.status = (status or "pendiente").strip().lower()
    if is_new and source_id:
        documento.source_id = source_id
        documento.source_type = (source_type or "").strip().lower()
        documento.root_id = root_id or source_id
    elif is_new and root_id:
        documento.root_id = root_id
    documento.observacion = notes
    documento.usuario = (session.get("user") or "sistema")

    documento.items.clear()
    for item in items:
        codigo = (item.get("codigo") or "").strip().upper()
        descripcion = (item.get("descripcion") or "").strip()
        if not codigo and not descripcion:
            continue
        cantidad = _safe_int(str(item.get("cantidad") or "1"), default=1)
        precio = _safe_float(str(item.get("precio") or "0"))
        subtotal = round(cantidad * precio, 2)
        documento.items.append(
            DocumentoVentaItem(
                codigo_producto=codigo,
                descripcion=descripcion,
                marca=(item.get("marca") or "").strip().upper(),
                bodega=(item.get("bodega") or "").strip() or "Bodega 1",
                cantidad=cantidad,
                precio_unitario=precio,
                subtotal=subtotal,
            )
        )

    db.session.flush()
    return documento


def _serialize_document(documento: DocumentoVenta) -> dict:
    doc_kind = (documento.tipo or "").strip().lower()
    if doc_kind in {"factura", "boleta"}:
        doc_type = "factura"
        tipo_documento = doc_kind
    else:
        doc_type = doc_kind
        tipo_documento = "factura"

    party_id = documento.proveedor_id if doc_kind in {"orden_compra", "factura_proveedor"} else documento.cliente_id

    items = []
    for item in documento.items:
        cantidad = int(item.cantidad or 0)
        precio = float(item.precio_unitario or 0)
        items.append(
            {
                "codigo": (item.codigo_producto or "").strip().upper(),
                "descripcion": item.descripcion or "",
                "marca": (item.marca or "").strip().upper(),
                "bodega": (item.bodega or "").strip() or "Bodega 1",
                "cantidad": cantidad,
                "precio": precio,
                "subtotal": round(float(item.subtotal or (cantidad * precio)), 2),
            }
        )

    subtotal = round(float(documento.subtotal or 0), 2)
    iva = round(float(documento.impuesto or 0), 2)
    total = round(float(documento.total or 0), 2)

    return {
        "id": documento.id,
        "doc_type": doc_type,
        "tipo_documento": tipo_documento,
        "source_id": documento.source_id,
        "source_type": (documento.source_type or "").strip().lower(),
        "root_id": documento.root_id,
        "numero": (documento.numero or "").strip(),
        "fecha_documento": documento.fecha_documento.strftime("%Y-%m-%d") if documento.fecha_documento else "",
        "fecha_vencimiento": documento.fecha_vencimiento.strftime("%Y-%m-%d") if documento.fecha_vencimiento else "",
        "status": (documento.status or "pendiente").strip().lower(),
        "metodo_pago": (documento.metodo_pago or "").strip(),
        "estado_pago": (documento.estado_pago or "pendiente").strip(),
        "party": {
            "id": party_id or 0,
            "name": documento.cliente_nombre or "",
            "rut": format_rut(documento.cliente_rut),
            "address": documento.cliente_direccion or "",
            "telefono": documento.cliente_telefono or "",
            "email": documento.cliente_email or "",
            "region": documento.cliente_region or "",
            "ciudad": documento.cliente_ciudad or "",
            "pais": documento.cliente_pais or CHILE_COUNTRY_NAME,
        },
        "items": items,
        "totals": {
            "subtotal": subtotal,
            "iva": iva,
            "total": total,
            "subtotal_fmt": _format_currency(subtotal),
            "iva_fmt": _format_currency(iva),
            "total_fmt": _format_currency(total),
        },
        "notes": documento.observacion or "",
    }


def _load_document_by_number(doc_type: str, numero: str) -> DocumentoVenta | None:
    clean_number = (numero or "").strip().upper()
    if not clean_number:
        return None
    if doc_type == "factura":
        return (
            DocumentoVenta.query.filter(DocumentoVenta.tipo.in_(["factura", "boleta"]), func.upper(DocumentoVenta.numero) == clean_number)
            .order_by(DocumentoVenta.id.desc())
            .first()
        )
    return (
        DocumentoVenta.query.filter_by(tipo=doc_type)
        .filter(func.upper(DocumentoVenta.numero) == clean_number)
        .order_by(DocumentoVenta.id.desc())
        .first()
    )


def _load_document_by_numero_or_id(doc_type: str, value: str) -> DocumentoVenta | None:
    lookup_value = (value or "").strip().upper()
    if not lookup_value:
        return None

    documento = _load_document_by_number(doc_type, lookup_value)
    if documento is not None:
        return documento

    if not lookup_value.isdigit():
        return None

    documento = db.session.get(DocumentoVenta, int(lookup_value))
    if documento is None:
        return None

    current_type = (documento.tipo or "").strip().lower()
    if doc_type == "factura":
        return documento if current_type in {"factura", "boleta"} else None
    return documento if current_type == doc_type else None


def _is_admin_user() -> bool:
    return "admin" in ((session.get("rol") or "").strip().lower())


def _next_credit_note_number() -> str:
    rows = db.session.query(NotaCredito.numero).filter(NotaCredito.numero.ilike("NC-%")).all()
    max_seq = 0
    for row in rows:
        numero = (row[0] or "").strip().upper()
        if not numero.startswith("NC-"):
            continue
        suffix = numero.split("-", 1)[1]
        if suffix.isdigit():
            max_seq = max(max_seq, int(suffix))
    return f"NC-{max_seq + 1:04d}"


def _adjust_product_stock(codigo: str, marca: str, bodega: str, delta: int, reason: str) -> str | None:
    if not codigo or delta == 0:
        return None

    code = codigo.strip().upper()
    brand = (marca or "").strip().upper()
    warehouse = (bodega or "").strip() or "Bodega 1"

    variant = (
        db.session.query(ProductoVarianteStock)
        .filter_by(codigo_producto=code, marca=brand, bodega=warehouse)
        .first()
    )
    if variant is None:
        if delta < 0:
            return f"No existe variante {code}/{brand} en {warehouse}."
        variant = ProductoVarianteStock(
            codigo_producto=code,
            marca=brand,
            bodega=warehouse,
            stock=0,
        )
        db.session.add(variant)
        db.session.flush()

    current_stock = int(variant.stock or 0)
    next_stock = current_stock + int(delta)
    if next_stock < 0:
        return f"Stock insuficiente para {code}/{brand} en {warehouse}. Disponible: {current_stock}."

    variant.stock = next_stock
    db.session.add(
        MovimientoStock(
            codigo_producto=code,
            tipo="ingreso" if delta > 0 else "salida",
            cantidad=int(delta),
            usuario=session.get("user") or "sistema",
            marca=brand,
            bodega=warehouse,
            observacion=reason,
        )
    )

    total_variants = db.session.execute(
        text(
            """
            SELECT COALESCE(SUM(stock), 0)
            FROM productos_variantes_stock
            WHERE UPPER(codigo_producto) = :codigo
            """
        ),
        {"codigo": code},
    ).scalar() or 0
    db.session.execute(
        text(
            """
            UPDATE productos
            SET STOCK_10JUL = :stock
            WHERE UPPER(CODIGO) = :codigo
            """
        ),
        {"codigo": code, "stock": int(total_variants)},
    )

    return None


def _apply_stock_for_document(documento: DocumentoVenta, direction: str, reason: str) -> tuple[bool, list[str]]:
    if documento is None:
        return False, ["Documento no encontrado."]

    if documento.stock_deducted:
        return True, []

    delta_sign = -1 if direction == "out" else 1
    errors: list[str] = []

    for item in documento.items:
        qty = _safe_int(str(item.cantidad or 0), default=0)
        if qty <= 0:
            continue
        error = _adjust_product_stock(
            codigo=(item.codigo_producto or "").strip().upper(),
            marca=(item.marca or "").strip().upper(),
            bodega=(item.bodega or "").strip() or "Bodega 1",
            delta=delta_sign * qty,
            reason=reason,
        )
        if error:
            errors.append(error)

    if errors:
        return False, errors

    documento.stock_deducted = True
    return True, []


def _serialize_chain_node(doc: DocumentoVenta) -> dict:
    return {
        "id": doc.id,
        "type": (doc.tipo or "").strip().lower(),
        "number": (doc.numero or "").strip(),
        "status": (doc.status or "pendiente").strip().lower(),
        "total": round(float(doc.total or 0), 2),
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
        "source_id": doc.source_id,
        "source_type": (doc.source_type or "").strip().lower(),
        "root_id": doc.root_id,
    }


def _history_doc_label(doc_type: str) -> str:
    labels = {
        "cotizacion": "Cotizacion",
        "orden_venta": "Orden de Venta",
        "orden_compra": "Orden de Compra",
        "factura": "Factura",
        "boleta": "Boleta",
        "factura_proveedor": "Factura proveedor",
        "nota_credito": "Nota de credito",
    }
    return labels.get((doc_type or "").strip().lower(), (doc_type or "Documento").replace("_", " ").title())


def _history_doc_tone(doc_type: str) -> str:
    tones = {
        "cotizacion": "blue",
        "orden_venta": "orange",
        "orden_compra": "orange",
        "factura": "green",
        "boleta": "green",
        "factura_proveedor": "green",
        "nota_credito": "slate",
    }
    return tones.get((doc_type or "").strip().lower(), "slate")


def _history_doc_url(doc_type: str, numero: str) -> str | None:
    clean_number = (numero or "").strip()
    if not clean_number:
        return None

    endpoint_map = {
        "cotizacion": "ventas.cotizacion",
        "orden_venta": "ventas.orden_venta",
        "factura": "ventas.facturacion",
        "boleta": "ventas.facturacion",
        "orden_compra": "ventas.orden_compra",
    }
    endpoint = endpoint_map.get((doc_type or "").strip().lower())
    if endpoint is None:
        return None
    return url_for(endpoint, numero=clean_number)


def _history_row_from_node(node: dict) -> dict:
    doc_type = (node.get("type") or "").strip().lower()
    number = (node.get("number") or node.get("numero") or "").strip()
    created_at = node.get("created_at")
    return {
        "id": node.get("id"),
        "type": doc_type,
        "label": _history_doc_label(doc_type),
        "badge_tone": _history_doc_tone(doc_type),
        "number": number,
        "status": node.get("status") or "pendiente",
        "total": round(float(node.get("total") or 0), 2),
        "created_at": created_at,
        "fecha": created_at[:10] if created_at else "-",
        "view_url": _history_doc_url(doc_type, number),
        "source_id": node.get("source_id"),
        "root_id": node.get("root_id"),
    }


def _trace_chain_from_document(root: DocumentoVenta) -> list[dict]:
    if root is None:
        return []

    nodes = [_serialize_chain_node(root)]
    cursor = root
    seen = {root.id}

    while True:
        child = (
            DocumentoVenta.query.filter_by(source_id=cursor.id)
            .order_by(DocumentoVenta.created_at.asc(), DocumentoVenta.id.asc())
            .first()
        )
        if child is None or child.id in seen:
            break
        nodes.append(_serialize_chain_node(child))
        seen.add(child.id)
        cursor = child

    notas = (
        NotaCredito.query.filter_by(source_id=cursor.id, source_type="factura")
        .order_by(NotaCredito.created_at.asc(), NotaCredito.id.asc())
        .all()
    )
    for nota in notas:
        nodes.append(
            {
                "id": nota.id,
                "type": "nota_credito",
                "number": (nota.numero or "").strip(),
                "status": (nota.status or "pendiente").strip().lower(),
                "total": round(float(nota.total or 0), 2),
                "created_at": nota.created_at.isoformat() if nota.created_at else None,
                "source_id": nota.source_id,
                "source_type": (nota.source_type or "").strip().lower(),
                "root_id": nota.root_id,
            }
        )

    return nodes


def _build_client_history_payload(client_id: int) -> tuple[Cliente | None, dict | None]:
    cliente = db.session.get(Cliente, client_id)
    if cliente is None or not cliente.activo:
        return None, None

    docs = (
        DocumentoVenta.query.filter_by(cliente_id=client_id)
        .order_by(DocumentoVenta.created_at.desc(), DocumentoVenta.id.desc())
        .all()
    )
    ids = [doc.id for doc in docs]
    notas = []
    if ids:
        notas = (
            NotaCredito.query.filter(NotaCredito.documento_venta_id.in_(ids))
            .order_by(NotaCredito.created_at.desc(), NotaCredito.id.desc())
            .all()
        )

    cotizaciones = [_serialize_chain_node(doc) for doc in docs if (doc.tipo or "") == "cotizacion"]
    ordenes_venta = [_serialize_chain_node(doc) for doc in docs if (doc.tipo or "") == "orden_venta"]
    facturas = [_serialize_chain_node(doc) for doc in docs if (doc.tipo or "") in {"factura", "boleta"}]
    notas_credito = [
        {
            "id": nota.id,
            "type": "nota_credito",
            "numero": nota.numero,
            "number": nota.numero,
            "source_id": nota.source_id,
            "root_id": nota.root_id,
            "total": nota.total,
            "status": nota.status,
            "created_at": nota.created_at.isoformat() if nota.created_at else None,
        }
        for nota in notas
    ]
    documentos = [_history_row_from_node(node) for node in cotizaciones + ordenes_venta + facturas + notas_credito]
    timeline = sorted(documentos, key=lambda item: item.get("created_at") or "", reverse=False)

    payload = {
        "client": cliente.to_dict(),
        "cotizaciones": cotizaciones,
        "ordenes_venta": ordenes_venta,
        "facturas": facturas,
        "notas_credito": notas_credito,
        "documentos": sorted(documentos, key=lambda item: item.get("created_at") or "", reverse=True),
        "timeline": timeline,
    }
    return cliente, payload


def _build_supplier_history_payload(supplier_id: int) -> tuple[Proveedor | None, dict | None]:
    proveedor = db.session.get(Proveedor, supplier_id)
    if proveedor is None or not proveedor.activo:
        return None, None

    docs = (
        DocumentoVenta.query.filter_by(proveedor_id=supplier_id)
        .order_by(DocumentoVenta.created_at.desc(), DocumentoVenta.id.desc())
        .all()
    )
    ordenes_compra = [_serialize_chain_node(doc) for doc in docs if (doc.tipo or "") == "orden_compra"]
    facturas_proveedor = [_serialize_chain_node(doc) for doc in docs if (doc.tipo or "") == "factura_proveedor"]
    documentos = [_history_row_from_node(node) for node in ordenes_compra + facturas_proveedor]
    payload = {
        "supplier": proveedor.to_dict(),
        "ordenes_compra": ordenes_compra,
        "facturas_proveedor": facturas_proveedor,
        "documentos": sorted(documentos, key=lambda item: item.get("created_at") or "", reverse=True),
        "timeline": sorted(documentos, key=lambda item: item.get("created_at") or "", reverse=False),
    }
    return proveedor, payload


def _copy_document_with_trace(source: DocumentoVenta, target_doc_type: str, target_tipo_documento: str = "factura") -> DocumentoVenta:
    target_number = _next_doc_number(_doc_prefix(target_doc_type))
    source_doc_type = (source.tipo or "").strip().lower()

    selected_client_id = int(source.cliente_id or 0)
    selected_proveedor_id = int(source.proveedor_id or 0)

    if target_doc_type in {"factura", "orden_venta", "cotizacion"}:
        selected_proveedor_id = 0
    if target_doc_type in {"orden_compra", "factura_proveedor"}:
        selected_client_id = 0

    selected_party = _client_by_id(selected_client_id) if selected_client_id > 0 else _proveedor_by_id(selected_proveedor_id)

    party = {
        "name": source.cliente_nombre or "",
        "rut": format_rut(source.cliente_rut),
        "address": source.cliente_direccion or "",
        "telefono": source.cliente_telefono or "",
        "email": source.cliente_email or "",
    }

    items = [
        {
            "codigo": (item.codigo_producto or "").strip().upper(),
            "descripcion": item.descripcion or "",
            "cantidad": int(item.cantidad or 0),
            "precio": float(item.precio_unitario or 0),
            "marca": (item.marca or "").strip().upper(),
            "bodega": (item.bodega or "").strip() or "Bodega 1",
            "subtotal": round(float(item.subtotal or 0), 2),
        }
        for item in source.items
    ]
    totals = {
        "subtotal": round(float(source.subtotal or 0), 2),
        "iva": round(float(source.impuesto or 0), 2),
        "total": round(float(source.total or 0), 2),
    }

    source_root_id = source.root_id or source.id
    target = _save_or_update_document(
        doc_type=target_doc_type,
        doc_number=target_number,
        doc_date=(source.fecha_documento.strftime("%Y-%m-%d") if source.fecha_documento else datetime.now().strftime("%Y-%m-%d")),
        doc_valid_until=(source.fecha_vencimiento.strftime("%Y-%m-%d") if source.fecha_vencimiento else ""),
        tipo_documento=target_tipo_documento,
        status="pendiente",
        selected_client_id=selected_client_id,
        selected_proveedor_id=selected_proveedor_id,
        selected_party=selected_party,
        party=party,
        items=items,
        totals=totals,
        notes=f"Convertido desde {source_doc_type} {source.numero or source.id}",
        source_id=source.id,
        source_type=source_doc_type,
        root_id=source_root_id,
    )
    return target


def _product_by_code(codigo: str):
    q = text("""
        SELECT CODIGO AS codigo, DESCRIPCION AS descripcion,
               MODELO AS modelo, COALESCE(P_PUBLICO, 0) AS precio,
               COALESCE(STOCK_10JUL, 0) AS stock
        FROM productos
        WHERE UPPER(CODIGO) = :codigo AND COALESCE(ACTIVO, 1) = 1
        LIMIT 1
    """)
    return db.session.execute(q, {"codigo": (codigo or "").strip().upper()}).mappings().first()


def _product_variants_by_code(codigo: str) -> list[dict]:
    code = (codigo or "").strip().upper()
    if not code:
        return []
    rows = (
        db.session.query(ProductoVarianteStock)
        .filter_by(codigo_producto=code)
        .order_by(ProductoVarianteStock.marca.asc(), ProductoVarianteStock.bodega.asc())
        .all()
    )
    return [
        {
            "id": row.id,
            "marca": row.marca or "",
            "bodega": row.bodega or "",
            "stock": int(row.stock or 0),
            "proveedor": row.proveedor or "",
        }
        for row in rows
    ]


def _product_variants_map(codigos: list[str]) -> dict[str, list[dict]]:
    normalized = sorted({(codigo or "").strip().upper() for codigo in codigos if (codigo or "").strip()})
    if not normalized:
        return {}

    rows = (
        db.session.query(ProductoVarianteStock)
        .filter(ProductoVarianteStock.codigo_producto.in_(normalized))
        .order_by(
            ProductoVarianteStock.codigo_producto.asc(),
            ProductoVarianteStock.marca.asc(),
            ProductoVarianteStock.bodega.asc(),
        )
        .all()
    )

    variant_map: dict[str, list[dict]] = {codigo: [] for codigo in normalized}
    for row in rows:
        code = (row.codigo_producto or "").strip().upper()
        variant_map.setdefault(code, []).append(
            {
                "id": row.id,
                "marca": row.marca or "",
                "bodega": row.bodega or "",
                "stock": int(row.stock or 0),
                "proveedor": row.proveedor or "",
            }
        )
    return variant_map


def _serialize_product(producto, codigo: str | None = None, variantes: list[dict] | None = None) -> dict:
    code = (producto.get("codigo") or codigo or "").strip().upper()
    variant_rows = list(variantes if variantes is not None else _product_variants_by_code(code))

    stock_entries = [
        {
            "marca": (variant.get("marca") or "").strip().upper(),
            "bodega": (variant.get("bodega") or "").strip() or "Bodega 1",
            "stock": int(variant.get("stock") or 0),
            "proveedor": variant.get("proveedor") or "",
        }
        for variant in variant_rows
    ]

    if not stock_entries:
        stock_entries.append(
            {
                "marca": (producto.get("marca") or "").strip().upper(),
                "bodega": "Bodega 1",
                "stock": int(producto.get("stock") or 0),
                "proveedor": "",
            }
        )

    brand_totals: dict[str, int] = {}
    for entry in stock_entries:
        brand = entry["marca"] or "SIN VARIANTE"
        brand_totals[brand] = brand_totals.get(brand, 0) + int(entry.get("stock") or 0)

    default_entry = next((entry for entry in stock_entries if int(entry.get("stock") or 0) > 0), stock_entries[0])

    return {
        "codigo": code,
        "descripcion": (producto.get("descripcion") or "").strip(),
        "modelo": (producto.get("modelo") or "").strip(),
        "oem": (producto.get("codigo_oem") or "").strip(),
        "marca_base": (producto.get("marca") or "").strip(),
        "precio": float(producto.get("precio") or 0),
        "stock": int(producto.get("stock") or 0),
        "variantes": variant_rows,
        "stock_entries": stock_entries,
        "brand_totals": brand_totals,
        "has_variantes": len(variant_rows) > 0,
        "default_marca": (default_entry.get("marca") or "").strip().upper(),
        "default_bodega": (default_entry.get("bodega") or "").strip() or "Bodega 1",
        "default_stock": int(default_entry.get("stock") or 0),
    }


def _search_products(term: str, limit: int = 60) -> list[dict]:
    search_term = (term or "").strip()
    if not search_term:
        return []

    compact = search_term.replace(" ", "")
    is_numeric = compact.isdigit()
    like = f"%{search_term}%"
    starts = f"{search_term}%"
    safe_limit = max(1, min(limit, 100))

    query = text(
        """
        SELECT
            CODIGO AS codigo,
            COALESCE(DESCRIPCION, '') AS descripcion,
            COALESCE(MODELO, '') AS modelo,
            COALESCE(MARCA, '') AS marca,
            COALESCE([CODIGO OEM], '') AS codigo_oem,
            COALESCE(P_PUBLICO, 0) AS precio,
            COALESCE(STOCK_10JUL, 0) AS stock
        FROM productos
        WHERE COALESCE(ACTIVO, 1) = 1
          AND (
            UPPER(CODIGO) LIKE UPPER(:like)
            OR UPPER(COALESCE([CODIGO OEM], '')) LIKE UPPER(:like)
            OR UPPER(COALESCE(DESCRIPCION, '')) LIKE UPPER(:like)
          )
        ORDER BY
            CASE
                WHEN :is_numeric = 1 AND UPPER(CODIGO) LIKE UPPER(:starts) THEN 0
                WHEN :is_numeric = 1 AND UPPER(COALESCE([CODIGO OEM], '')) LIKE UPPER(:starts) THEN 1
                WHEN :is_numeric = 1 AND UPPER(COALESCE(DESCRIPCION, '')) LIKE UPPER(:starts) THEN 2
                WHEN :is_numeric = 0 AND UPPER(COALESCE(DESCRIPCION, '')) LIKE UPPER(:starts) THEN 0
                WHEN :is_numeric = 0 AND UPPER(COALESCE([CODIGO OEM], '')) LIKE UPPER(:starts) THEN 1
                WHEN :is_numeric = 0 AND UPPER(CODIGO) LIKE UPPER(:starts) THEN 2
                ELSE 3
            END,
            LENGTH(CODIGO) ASC,
            CODIGO ASC
        LIMIT :limit
        """
    )

    rows = db.session.execute(
        query,
        {
            "like": like,
            "starts": starts,
            "is_numeric": 1 if is_numeric else 0,
            "limit": safe_limit,
        },
    ).mappings().all()

    codes = [(row.get("codigo") or "").strip().upper() for row in rows if row.get("codigo")]
    variant_map = _product_variants_map(codes)

    results = []
    for row in rows:
        code = (row.get("codigo") or "").strip().upper()
        payload = _serialize_product(row, codigo=code, variantes=variant_map.get(code, []))
        entries = payload.get("stock_entries") or []
        if entries:
            for entry in entries:
                results.append(
                    {
                        **payload,
                        "marca": entry.get("marca") or "",
                        "bodega": entry.get("bodega") or "Bodega 1",
                        "variant_stock": int(entry.get("stock") or 0),
                    }
                )
        else:
            results.append(
                {
                    **payload,
                    "marca": "",
                    "bodega": "Bodega 1",
                    "variant_stock": int(payload.get("stock") or 0),
                }
            )
    return results


def _discount_stock_for_sale(items: list[dict], doc_number: str) -> list[str]:
    errors = []

    for item in items:
        codigo = (item.get("codigo") or "").strip().upper()
        if not codigo:
            continue
        qty = _safe_int(str(item.get("cantidad") or "1"), default=1)
        marca = (item.get("marca") or "").strip().upper()
        bodega = (item.get("bodega") or "").strip() or "Bodega 1"

        variants = _product_variants_by_code(codigo)
        if variants:
            if not marca:
                errors.append(f"El item {codigo} requiere seleccionar marca/variante.")
                continue

            variante = (
                db.session.query(ProductoVarianteStock)
                .filter_by(codigo_producto=codigo, marca=marca, bodega=bodega)
                .first()
            )
            if variante is None:
                errors.append(f"La variante {codigo} / {marca} en {bodega} no existe.")
                continue

            disponible = int(variante.stock or 0)
            if disponible < qty:
                errors.append(
                    f"Stock insuficiente para {codigo} / {marca} ({bodega}). Disponible: {disponible}, requerido: {qty}."
                )
                continue

            variante.stock = disponible - qty
            db.session.add(
                MovimientoStock(
                    codigo_producto=codigo,
                    tipo="salida",
                    cantidad=-qty,
                    usuario=session.get("user") or "sistema",
                    marca=marca,
                    bodega=bodega,
                    observacion=f"Venta {doc_number}",
                )
            )

            total_variantes = db.session.execute(
                text(
                    """
                    SELECT COALESCE(SUM(stock), 0)
                    FROM productos_variantes_stock
                    WHERE UPPER(codigo_producto) = :codigo
                    """
                ),
                {"codigo": codigo},
            ).scalar() or 0
            db.session.execute(
                text(
                    """
                    UPDATE productos
                    SET STOCK_10JUL = :stock
                    WHERE UPPER(CODIGO) = :codigo
                    """
                ),
                {"codigo": codigo, "stock": int(total_variantes)},
            )
            continue

        producto = _product_by_code(codigo)
        if producto is None:
            errors.append(f"Producto {codigo} no encontrado.")
            continue

        stock_actual = int(producto.get("stock") or 0)
        if stock_actual < qty:
            errors.append(f"Stock insuficiente para {codigo}. Disponible: {stock_actual}, requerido: {qty}.")
            continue

        db.session.execute(
            text(
                """
                UPDATE productos
                SET STOCK_10JUL = :stock
                WHERE UPPER(CODIGO) = :codigo
                """
            ),
            {"codigo": codigo, "stock": stock_actual - qty},
        )
        db.session.add(
            MovimientoStock(
                codigo_producto=codigo,
                tipo="salida",
                cantidad=-qty,
                usuario=session.get("user") or "sistema",
                bodega=bodega,
                observacion=f"Venta {doc_number}",
            )
        )

    return errors


def _all_clientes():
    return Cliente.query.filter_by(activo=True).order_by(Cliente.nombre).all()


def _all_proveedores():
    return Proveedor.query.filter_by(activo=True).order_by(Proveedor.nombre).all()


def _base_ctx():
    return {
        "company": COMPANY_INFO,
        "usuario_nombre": session.get("user"),
        "usuario_rol": session.get("rol"),
    }


def _cliente_form_data(source=None) -> dict:
    source = source or {}
    pais = _normalize_country(source.get("pais"), default=CHILE_COUNTRY_NAME)
    region = _clean_text(source.get("region") or source.get("region_text"))
    comuna = _clean_text(source.get("comuna") or source.get("comuna_text"))
    ciudad = _clean_text(source.get("ciudad"))
    if _is_chile_country(pais) and comuna and not ciudad:
        ciudad = comuna
    return {
        "nombre": _clean_text(source.get("nombre")),
        "rut": clean_rut(source.get("rut")),
        "giro": _clean_text(source.get("giro")),
        "direccion": _clean_text(source.get("direccion")),
        "region": region,
        "comuna": comuna,
        "ciudad": ciudad,
        "pais": pais,
        "telefono": _clean_text(source.get("telefono")),
        "email": _clean_text(source.get("email")),
    }


def _proveedor_form_data(source=None) -> dict:
    source = source or {}
    pais = _normalize_country(source.get("pais"), default=CHILE_COUNTRY_NAME)
    region = _clean_text(source.get("region") or source.get("region_text"))
    comuna = _clean_text(source.get("comuna") or source.get("comuna_text"))
    ciudad = _clean_text(source.get("ciudad"))
    if _is_chile_country(pais) and comuna and not ciudad:
        ciudad = comuna
    return {
        "nombre": _clean_text(source.get("nombre")),
        "empresa": _clean_text(source.get("empresa")),
        "rut": clean_rut(source.get("rut")),
        "giro": _clean_text(source.get("giro")),
        "direccion": _clean_text(source.get("direccion")),
        "region": region,
        "comuna": comuna,
        "ciudad": ciudad,
        "pais": pais,
        "telefono": _clean_text(source.get("telefono")),
        "email": _clean_text(source.get("email")),
    }


def _validate_cliente_data(data: dict, rut_required: bool = False) -> list[str]:
    errors = []
    if not data["nombre"]:
        errors.append("El nombre del cliente es obligatorio.")
    if not data["pais"]:
        errors.append("El pais del cliente es obligatorio.")
    if _is_chile_country(data["pais"]):
        if not data["region"]:
            errors.append("La region es obligatoria para clientes de Chile.")
        if not data["comuna"]:
            errors.append("La comuna es obligatoria para clientes de Chile.")
    if rut_required and not data["rut"]:
        errors.append("El RUT del cliente es obligatorio para factura.")
    if data["rut"] and not is_valid_rut(data["rut"]):
        errors.append("El RUT del cliente no es valido.")
    if not _is_valid_email(data["email"]):
        errors.append("El email del cliente no es valido.")
    return errors


def _validate_proveedor_data(data: dict, rut_required: bool = True) -> list[str]:
    errors = []
    if not data["nombre"]:
        errors.append("El nombre del proveedor es obligatorio.")
    if not data["pais"]:
        errors.append("El pais del proveedor es obligatorio.")
    if _is_chile_country(data["pais"]):
        if not data["region"]:
            errors.append("La region es obligatoria para proveedores de Chile.")
        if not data["comuna"]:
            errors.append("La comuna es obligatoria para proveedores de Chile.")
    if rut_required and not data["rut"]:
        errors.append("El RUT del proveedor es obligatorio.")
    if data["rut"] and not is_valid_rut(data["rut"]):
        errors.append("El RUT del proveedor no es valido.")
    if not _is_valid_email(data["email"]):
        errors.append("El email del proveedor no es valido.")
    return errors


def _hydrate_cliente(instance: Cliente, data: dict) -> Cliente:
    instance.nombre = data["nombre"]
    instance.rut = data["rut"]
    instance.giro = data["giro"]
    instance.direccion = data["direccion"]
    instance.region = data["region"]
    instance.comuna = data["comuna"]
    instance.ciudad = data["ciudad"]
    instance.pais = data["pais"]
    instance.telefono = data["telefono"]
    instance.email = data["email"]
    instance.activo = True
    return instance


def _hydrate_proveedor(instance: Proveedor, data: dict) -> Proveedor:
    instance.nombre = data["nombre"]
    instance.empresa = data["empresa"]
    instance.rut = data["rut"]
    instance.giro = data["giro"]
    instance.direccion = data["direccion"]
    instance.region = data["region"]
    instance.comuna = data["comuna"]
    instance.ciudad = data["ciudad"]
    instance.pais = data["pais"]
    instance.telefono = data["telefono"]
    instance.email = data["email"]
    instance.activo = True
    return instance


def _client_by_id(client_id: int) -> Cliente | None:
    if client_id <= 0:
        return None
    cliente = db.session.get(Cliente, client_id)
    return cliente if cliente and cliente.activo else None


def _proveedor_by_id(proveedor_id: int) -> Proveedor | None:
    if proveedor_id <= 0:
        return None
    proveedor = db.session.get(Proveedor, proveedor_id)
    return proveedor if proveedor and proveedor.activo else None


def _entity_snapshot(entity, is_supplier_doc: bool) -> dict:
    if entity is None:
        return {
            "name": "",
            "rut": "",
            "address": "",
            "telefono": "",
            "email": "",
        }
    if is_supplier_doc:
        return {
            "name": entity.empresa or entity.nombre or "",
            "rut": format_rut(entity.rut),
            "address": _full_address(entity),
            "telefono": entity.telefono or "",
            "email": entity.email or "",
        }
    return {
        "name": entity.nombre or "",
        "rut": format_rut(entity.rut),
        "address": _full_address(entity),
        "telefono": entity.telefono or "",
        "email": entity.email or "",
    }


def _merge_party(form, entity, is_supplier_doc: bool) -> dict:
    base = _entity_snapshot(entity, is_supplier_doc)
    if not form:
        return base
    return {
        "name": _clean_text(form.get("party_name")) or base["name"],
        "rut": format_rut(_clean_text(form.get("party_rut")) or base["rut"]),
        "address": _clean_text(form.get("party_address")) or base["address"],
        "telefono": _clean_text(form.get("party_telefono")) or base["telefono"],
        "email": _clean_text(form.get("party_email")) or base["email"],
    }


def _doc_validation_errors(doc_type: str, tipo_documento: str, selected_client, selected_proveedor, party: dict, items: list[dict]) -> list[str]:
    errors = []
    if doc_type in {"cotizacion", "orden_venta", "factura"} and selected_client is None:
        errors.append("Debe seleccionar un cliente.")

    if doc_type in {"orden_compra", "factura_proveedor"} and selected_proveedor is None:
        errors.append("Debe seleccionar un proveedor.")

    if doc_type == "factura" and tipo_documento == "factura" and not party.get("rut"):
        errors.append("La factura de venta requiere RUT del cliente.")

    if doc_type in {"orden_compra", "factura_proveedor"} and not party.get("rut"):
        errors.append("La compra requiere RUT del proveedor.")

    if not any(item.get("codigo") or item.get("descripcion") for item in items):
        errors.append("Debe agregar al menos un item al documento.")

    if doc_type in {"cotizacion", "orden_venta", "factura"} and not party.get("name"):
        errors.append("El cliente es obligatorio.")

    if doc_type in {"orden_compra", "factura_proveedor"} and not party.get("name"):
        errors.append("El proveedor es obligatorio.")

    return errors


def _document_summary(doc_type: str, tipo_documento: str) -> str:
    if doc_type == "cotizacion":
        return "Propuesta comercial lista para aprobacion del cliente."
    if doc_type == "orden_venta":
        return "Documento operativo para seguimiento y despacho de venta."
    if doc_type == "factura":
        return "Documento tributario de venta que descuenta stock automaticamente."
    return "Compra formal para recepcion de mercaderia y control de inventario."


# ─────────────────────────────────────────────────────────────
#  DOCUMENT CONTEXT BUILDER
# ─────────────────────────────────────────────────────────────

def _build_doc_context(doc_type: str, title: str, party_label: str,
                       is_supplier_doc: bool, status_enabled: bool) -> dict:
    now = datetime.now()
    chile_geo = _load_chile_geo()
    form = request.form if request.method == "POST" else None
    prefix = _doc_prefix(doc_type)

    doc_number = (form.get("doc_number") if form else None) or _next_doc_number(prefix)
    doc_date = (form.get("doc_date") if form else None) or now.strftime("%Y-%m-%d")
    doc_valid_until = (form.get("doc_valid_until") if form else None) or now.strftime("%Y-%m-%d")
    notes = ((form.get("notes") if form else None) or "").strip()
    status = ((form.get("status") if form else None) or "pendiente").strip().lower()
    tipo_documento = ((form.get("tipo_documento") if form else None) or "factura").strip().lower()

    selected_client_id = _safe_int((form.get("client_id") if form else None) or "0", default=0)
    selected_proveedor_id = _safe_int((form.get("proveedor_id") if form else None) or "0", default=0)

    clientes = _all_clientes() if not is_supplier_doc else []
    proveedores = _all_proveedores() if is_supplier_doc else []
    selected_client = _client_by_id(selected_client_id)
    selected_proveedor = _proveedor_by_id(selected_proveedor_id)
    selected_party = selected_proveedor if is_supplier_doc else selected_client
    party = _merge_party(form, selected_party, is_supplier_doc)
    loaded_document_id = _safe_int((form.get("loaded_document_id") if form else None) or "0", default=0)

    items = _extract_items_from_form(form)
    totals = _calculate_totals(items)
    validation_errors = []
    saved_successfully = False
    saved_number = ""
    estado_pago = "pendiente"
    metodo_pago = ""

    if request.method == "GET":
        requested_number = _clean_text(request.args.get("numero")).upper()
        if requested_number:
            loaded_document = _load_document_by_numero_or_id(doc_type, requested_number)
            if loaded_document is not None:
                serialized = _serialize_document(loaded_document)
                doc_number = serialized["numero"] or doc_number
                doc_date = serialized["fecha_documento"] or doc_date
                doc_valid_until = serialized["fecha_vencimiento"] or doc_valid_until
                status = serialized["status"] or status
                tipo_documento = serialized["tipo_documento"] or tipo_documento
                notes = serialized["notes"] or notes
                party = serialized["party"] or party
                selected_client_id = int(loaded_document.cliente_id or 0)
                selected_proveedor_id = int(loaded_document.proveedor_id or 0)
                selected_client = _client_by_id(selected_client_id)
                selected_proveedor = _proveedor_by_id(selected_proveedor_id)
                selected_party = selected_proveedor if is_supplier_doc else selected_client
                loaded_document_id = loaded_document.id
                items = serialized["items"] or items
                estado_pago = serialized.get("estado_pago", "pendiente")
                metodo_pago = serialized.get("metodo_pago", "")
                totals = {
                    "subtotal": serialized["totals"].get("subtotal", totals["subtotal"]),
                    "iva": serialized["totals"].get("iva", totals["iva"]),
                    "total": serialized["totals"].get("total", totals["total"]),
                }

    if request.method == "POST":
        if doc_type in {"orden_venta", "factura"} and tipo_documento not in {"factura", "boleta"}:
            validation_errors.append("Selecciona un tipo de documento valido: factura o boleta.")

        validation_errors = _doc_validation_errors(
            doc_type,
            tipo_documento,
            selected_client,
            selected_proveedor,
            party,
            items,
        ) + validation_errors

        for error in validation_errors:
            flash(error, "error")

        if not validation_errors:
            try:
                print(f"Saving {doc_type}: {doc_number}")
                saved_document = _save_or_update_document(
                    doc_type=doc_type,
                    doc_number=doc_number,
                    doc_date=doc_date,
                    doc_valid_until=doc_valid_until,
                    tipo_documento=tipo_documento,
                    status=status,
                    selected_client_id=selected_client_id,
                    selected_proveedor_id=selected_proveedor_id,
                    selected_party=selected_party,
                    party=party,
                    items=items,
                    totals=totals,
                    notes=notes,
                )

                if doc_type == "factura" and not saved_document.stock_deducted:
                    success, stock_errors = _apply_stock_for_document(
                        saved_document,
                        direction="out",
                        reason=f"Factura {saved_document.numero or saved_document.id}",
                    )
                    if not success:
                        raise ValueError("; ".join(stock_errors))

                db.session.commit()
                saved_successfully = True
                saved_number = (saved_document.numero or doc_number).strip().upper()
                loaded_document_id = saved_document.id
                doc_number = saved_number
                success_messages = {
                    "cotizacion": "Cotización guardada",
                    "orden_venta": "Orden de venta guardada",
                    "orden_compra": "Orden de compra guardada",
                    "factura": f"{(tipo_documento or 'factura').capitalize()} guardada",
                    "factura_proveedor": "Documento de compra guardado",
                }
                flash(success_messages.get(doc_type, "Documento guardado"), "success")
                print(f"saved successfully: {saved_number}")

                serialized = _serialize_document(saved_document)
                doc_date = serialized["fecha_documento"] or doc_date
                doc_valid_until = serialized["fecha_vencimiento"] or doc_valid_until
                status = serialized["status"] or status
                tipo_documento = serialized["tipo_documento"] or tipo_documento
                notes = serialized["notes"] or notes
                party = serialized["party"] or party
                items = serialized["items"] or items
                estado_pago = serialized.get("estado_pago", estado_pago)
                metodo_pago = serialized.get("metodo_pago", metodo_pago)
                totals = {
                    "subtotal": serialized["totals"].get("subtotal", totals["subtotal"]),
                    "iva": serialized["totals"].get("iva", totals["iva"]),
                    "total": serialized["totals"].get("total", totals["total"]),
                }

                selected_client_id = int(saved_document.cliente_id or 0)
                selected_proveedor_id = int(saved_document.proveedor_id or 0)
                selected_client = _client_by_id(selected_client_id)
                selected_proveedor = _proveedor_by_id(selected_proveedor_id)
                selected_party = selected_proveedor if is_supplier_doc else selected_client
            except Exception as exc:
                db.session.rollback()
                print(f"Error saving {doc_type}: {exc}")
                flash(f"No se pudo guardar el documento: {exc}", "error")

    return {
        "active_page": doc_type,
        "doc_type": doc_type,
        "title": title,
        "party_label": party_label,
        "is_supplier_doc": is_supplier_doc,
        "status_enabled": status_enabled,
        "status": status,
        "status_options": STATUS_OPTIONS,
        "tipo_documento": tipo_documento,
        "tipo_documento_options": TIPO_DOCUMENTO_OPTIONS,
        "doc_number": doc_number,
        "doc_date": doc_date,
        "doc_valid_until": doc_valid_until,
        "party": party,
        "selected_client_id": selected_client_id,
        "selected_proveedor_id": selected_proveedor_id,
        "selected_client": selected_client,
        "selected_proveedor": selected_proveedor,
        "loaded_document_id": loaded_document_id,
        "clientes": clientes,
        "proveedores": proveedores,
        "items": items,
        "totals": totals,
        "money": {
            "subtotal": _format_currency(totals["subtotal"]),
            "iva": _format_currency(totals["iva"]),
            "total": _format_currency(totals["total"]),
        },
        "notes": notes,
        "generated": saved_successfully,
        "saved_number": saved_number,
        "can_traceability": _is_admin_user(),
        "validation_errors": validation_errors,
        "document_summary": _document_summary(doc_type, tipo_documento),
        "chile_geo": chile_geo,
        "chile_regions": _chile_regions(chile_geo),
        # Payment / contact (used by _erp_doc_header, _erp_footer, _erp_scripts)
        "doc_id": loaded_document_id or None,
        "tipo": doc_type,
        "numero": doc_number,
        "estado_pago": estado_pago,
        "metodo_pago": metodo_pago,
        "party_email": (party or {}).get("email", ""),
        "party_phone": (party or {}).get("telefono", ""),
        "client_email": (party or {}).get("email", ""),
        "client_phone": (party or {}).get("telefono", ""),
        **_base_ctx(),
    }


# ─────────────────────────────────────────────────────────────
#  DOCUMENT ROUTES
# ─────────────────────────────────────────────────────────────

@ventas_bp.route("/")
@login_required
def index():
    _partial = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    return render_template("ventas/index.html", active_page="index", _partial=_partial, **_base_ctx())


@ventas_bp.route("/cotizacion", methods=["GET", "POST"])
@login_required
def cotizacion():
    _partial = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    ctx = _build_doc_context("cotizacion", "Cotización", "Cliente", False, False)
    if request.method == "POST" and ctx.get("generated"):
        numero = (ctx.get("saved_number") or ctx.get("doc_number") or "").strip()
        if _partial:
            return jsonify({
                "success": True,
                "message": "Cotización guardada correctamente",
                "doc_number": numero,
            })
        flash("Cotización guardada correctamente", "success")
        if numero:
            return redirect(url_for("ventas.cotizacion", numero=numero))
        return redirect(url_for("ventas.cotizacion"))
    ctx["_partial"] = _partial
    return render_template("ventas/cotizacion.html", **ctx)


@ventas_bp.route("/orden-venta", methods=["GET", "POST"])
@ventas_bp.route("/orden_venta", methods=["GET", "POST"])
@login_required
def orden_venta():
    _partial = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    ctx = _build_doc_context("orden_venta", "Orden de Venta", "Cliente", False, True)
    if request.method == "POST" and ctx.get("generated") and _partial:
        numero = (ctx.get("saved_number") or ctx.get("doc_number") or "").strip()
        return jsonify({
            "success": True,
            "message": "Orden de venta guardada correctamente",
            "doc_number": numero,
        })
    ctx["_partial"] = _partial
    return render_template("ventas/orden_venta.html", **ctx)


@ventas_bp.route("/orden-compra", methods=["GET", "POST"])
@ventas_bp.route("/orden_compra", methods=["GET", "POST"])
@login_required
def orden_compra():
    _partial = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    ctx = _build_doc_context("orden_compra", "Orden de Compra", "Proveedor", True, False)
    if request.method == "POST" and ctx.get("generated") and _partial:
        numero = (ctx.get("saved_number") or ctx.get("doc_number") or "").strip()
        return jsonify({
            "success": True,
            "message": "Orden de compra guardada correctamente",
            "doc_number": numero,
        })
    ctx["_partial"] = _partial
    return render_template("ventas/documento.html", **ctx)


@ventas_bp.route("/facturacion", methods=["GET", "POST"])
@ventas_bp.route("/factura", methods=["GET", "POST"])
@login_required
def facturacion():
    ctx = _build_doc_context("factura", "Facturación", "Cliente", False, True)
    return render_template("ventas/factura.html", **ctx)


# ─────────────────────────────────────────────────────────────
#  CLIENT MANAGEMENT
# ─────────────────────────────────────────────────────────────

@ventas_bp.route("/clientes")
@login_required
def clientes():
    search_term = _extract_search_term()
    query = Cliente.query.filter_by(activo=True)
    lista = _apply_entity_search(query, Cliente, search_term).order_by(Cliente.nombre).all()
    return render_template(
        "ventas/clientes.html",
        clientes=lista,
        search_term=search_term,
        active_page="clientes",
        **_base_ctx(),
    )


@ventas_bp.route("/clientes/nuevo", methods=["GET", "POST"])
@login_required
def cliente_nuevo():
    chile_geo = _load_chile_geo()
    form_data = _cliente_form_data(request.form if request.method == "POST" else None)
    if request.method == "POST":
        errors = _validate_cliente_data(form_data)
        if not errors:
            cliente = _hydrate_cliente(Cliente(), form_data)
            db.session.add(cliente)
            db.session.commit()
            flash("Cliente creado correctamente.", "success")
            return redirect(url_for("ventas.clientes"))
        for error in errors:
            flash(error, "error")
    return render_template(
        "ventas/cliente_form.html",
        form_title="Nuevo cliente",
        submit_label="Crear cliente",
        cliente=form_data,
        chile_geo=chile_geo,
        chile_regions=_chile_regions(chile_geo),
        active_page="clientes",
        **_base_ctx(),
    )


@ventas_bp.route("/clientes/<int:cid>/editar", methods=["GET", "POST"])
@login_required
def cliente_editar(cid: int):
    chile_geo = _load_chile_geo()
    c = db.session.get(Cliente, cid)
    if c is None or not c.activo:
        flash("Cliente no encontrado.", "error")
        return redirect(url_for("ventas.clientes"))

    if request.method == "POST":
        form_data = _cliente_form_data(request.form)
        errors = _validate_cliente_data(form_data)
        if not errors:
            _hydrate_cliente(c, form_data)
            db.session.commit()
            flash("Cliente actualizado correctamente.", "success")
            return redirect(url_for("ventas.clientes"))
        for error in errors:
            flash(error, "error")
    else:
        form_data = c.to_dict()

    return render_template(
        "ventas/cliente_form.html",
        form_title="Editar cliente",
        submit_label="Guardar cambios",
        cliente=form_data,
        chile_geo=chile_geo,
        chile_regions=_chile_regions(chile_geo),
        cliente_id=cid,
        active_page="clientes",
        **_base_ctx(),
    )


@ventas_bp.route("/clientes/<int:cid>/eliminar", methods=["POST"])
@login_required
def cliente_eliminar(cid: int):
    c = db.session.get(Cliente, cid)
    if c and c.activo:
        c.activo = False
        db.session.commit()
        flash("Cliente desactivado correctamente.", "success")
    return redirect(url_for("ventas.clientes"))


@ventas_bp.route("/clientes/<int:cid>/historial")
@login_required
def cliente_historial(cid: int):
    cliente, payload = _build_client_history_payload(cid)
    if cliente is None or payload is None:
        flash("Cliente no encontrado.", "error")
        return redirect(url_for("ventas.clientes"))
    return render_template(
        "ventas/cliente_historial.html",
        cliente=cliente,
        data=payload,
        active_page="clientes",
        **_base_ctx(),
    )


# ─────────────────────────────────────────────────────────────
#  SUPPLIER MANAGEMENT
# ─────────────────────────────────────────────────────────────

@ventas_bp.route("/proveedores")
@login_required
def proveedores():
    search_term = _extract_search_term()
    query = Proveedor.query.filter_by(activo=True)
    if search_term:
        term = f"%{search_term}%"
        normalized_term = clean_rut(search_term)
        query = query.filter(
            Proveedor.nombre.ilike(term)
            | Proveedor.empresa.ilike(term)
            | Proveedor.rut.ilike(term)
            | Proveedor.giro.ilike(term)
            | Proveedor.comuna.ilike(term)
            | Proveedor.ciudad.ilike(term)
            | Proveedor.pais.ilike(term)
            | Proveedor.email.ilike(term)
            | (_normalized_rut_sql(Proveedor.rut).ilike(f"%{normalized_term}%") if normalized_term else False)
        )
    lista = query.order_by(Proveedor.empresa, Proveedor.nombre).all()
    return render_template(
        "ventas/proveedores.html",
        proveedores=lista,
        search_term=search_term,
        active_page="proveedores",
        **_base_ctx(),
    )


@ventas_bp.route("/proveedores/nuevo", methods=["GET", "POST"])
@login_required
def proveedor_nuevo():
    chile_geo = _load_chile_geo()
    form_data = _proveedor_form_data(request.form if request.method == "POST" else None)
    if request.method == "POST":
        errors = _validate_proveedor_data(form_data)
        if not errors:
            proveedor = _hydrate_proveedor(Proveedor(), form_data)
            db.session.add(proveedor)
            db.session.commit()
            flash("Proveedor creado correctamente.", "success")
            return redirect(url_for("ventas.proveedores"))
        for error in errors:
            flash(error, "error")
    return render_template(
        "ventas/proveedor_form.html",
        form_title="Nuevo proveedor",
        submit_label="Crear proveedor",
        proveedor=form_data,
        chile_geo=chile_geo,
        chile_regions=_chile_regions(chile_geo),
        active_page="proveedores",
        **_base_ctx(),
    )


@ventas_bp.route("/proveedores/<int:pid>/editar", methods=["GET", "POST"])
@login_required
def proveedor_editar(pid: int):
    chile_geo = _load_chile_geo()
    p = db.session.get(Proveedor, pid)
    if p is None or not p.activo:
        flash("Proveedor no encontrado.", "error")
        return redirect(url_for("ventas.proveedores"))

    if request.method == "POST":
        form_data = _proveedor_form_data(request.form)
        errors = _validate_proveedor_data(form_data)
        if not errors:
            _hydrate_proveedor(p, form_data)
            db.session.commit()
            flash("Proveedor actualizado correctamente.", "success")
            return redirect(url_for("ventas.proveedores"))
        for error in errors:
            flash(error, "error")
    else:
        form_data = p.to_dict()

    return render_template(
        "ventas/proveedor_form.html",
        form_title="Editar proveedor",
        submit_label="Guardar cambios",
        proveedor=form_data,
        chile_geo=chile_geo,
        chile_regions=_chile_regions(chile_geo),
        proveedor_id=pid,
        active_page="proveedores",
        **_base_ctx(),
    )


@ventas_bp.route("/proveedores/<int:pid>/eliminar", methods=["POST"])
@login_required
def proveedor_eliminar(pid: int):
    p = db.session.get(Proveedor, pid)
    if p and p.activo:
        p.activo = False
        db.session.commit()
        flash("Proveedor desactivado correctamente.", "success")
    return redirect(url_for("ventas.proveedores"))


@ventas_bp.route("/proveedores/<int:pid>/historial")
@login_required
def proveedor_historial(pid: int):
    proveedor, payload = _build_supplier_history_payload(pid)
    if proveedor is None or payload is None:
        flash("Proveedor no encontrado.", "error")
        return redirect(url_for("ventas.proveedores"))
    return render_template(
        "ventas/proveedor_historial.html",
        proveedor=proveedor,
        data=payload,
        active_page="proveedores",
        **_base_ctx(),
    )


# ─────────────────────────────────────────────────────────────
#  JSON API
# ─────────────────────────────────────────────────────────────

@ventas_bp.route("/api/producto", methods=["GET"])
@login_required
def api_producto():
    codigo = (request.args.get("codigo") or "").strip().upper()
    if not codigo:
        return jsonify({"success": False, "message": "Codigo vacio"}), 400
    producto = _product_by_code(codigo)
    if producto is None:
        return jsonify({"success": False, "message": "Producto no encontrado"}), 404
    variantes = _product_variants_by_code(codigo)
    return jsonify({
        "success": True,
        "producto": _serialize_product(producto, codigo=codigo, variantes=variantes),
    })


@ventas_bp.route("/api/productos/search", methods=["GET"])
@login_required
def api_productos_search():
    q = (request.args.get("q") or "").strip()
    limit = _safe_int(request.args.get("limit") or "60", default=60)
    if len(q) < 2:
        return jsonify({"success": True, "items": [], "count": 0})

    items = _search_products(q, limit=limit)
    return jsonify({
        "success": True,
        "items": items,
        "count": len(items),
        "query": q,
    })


@ventas_bp.route("/api/clientes", methods=["GET"])
@login_required
def api_clientes():
    q = (request.args.get("q") or "").strip().lower()
    query = Cliente.query.filter_by(activo=True)
    if q:
        term = f"%{q}%"
        normalized_term = clean_rut(q)
        query = query.filter(
            Cliente.nombre.ilike(term)
            | Cliente.rut.ilike(term)
            | Cliente.giro.ilike(term)
            | Cliente.comuna.ilike(term)
            | Cliente.ciudad.ilike(term)
            | Cliente.pais.ilike(term)
            | Cliente.email.ilike(term)
            | (_normalized_rut_sql(Cliente.rut).ilike(f"%{normalized_term}%") if normalized_term else False)
        )
    lista = query.order_by(Cliente.nombre).limit(50).all()
    return jsonify({"success": True, "clientes": [c.to_dict() for c in lista]})


@ventas_bp.route("/api/clientes/create", methods=["POST"])
@login_required
def api_cliente_create():
    data = request.get_json(silent=True) or {}
    form_data = _cliente_form_data(data)
    errors = _validate_cliente_data(form_data)
    if errors:
        return jsonify({"success": False, "message": errors[0]}), 400
    c = _hydrate_cliente(Cliente(), form_data)
    db.session.add(c)
    db.session.commit()
    return jsonify({"success": True, "cliente": c.to_dict()})


@ventas_bp.route("/api/proveedores", methods=["GET"])
@login_required
def api_proveedores():
    q = (request.args.get("q") or "").strip().lower()
    query = Proveedor.query.filter_by(activo=True)
    if q:
        term = f"%{q}%"
        normalized_term = clean_rut(q)
        query = query.filter(
            Proveedor.nombre.ilike(term)
            | Proveedor.empresa.ilike(term)
            | Proveedor.rut.ilike(term)
            | Proveedor.giro.ilike(term)
            | Proveedor.comuna.ilike(term)
            | Proveedor.ciudad.ilike(term)
            | Proveedor.pais.ilike(term)
            | Proveedor.email.ilike(term)
            | (_normalized_rut_sql(Proveedor.rut).ilike(f"%{normalized_term}%") if normalized_term else False)
        )
    lista = query.order_by(Proveedor.nombre).limit(50).all()
    return jsonify({"success": True, "proveedores": [p.to_dict() for p in lista]})


@ventas_bp.route("/api/proveedores/create", methods=["POST"])
@login_required
def api_proveedor_create():
    data = request.get_json(silent=True) or {}
    form_data = _proveedor_form_data(data)
    errors = _validate_proveedor_data(form_data)
    if errors:
        return jsonify({"success": False, "message": errors[0]}), 400
    p = _hydrate_proveedor(Proveedor(), form_data)
    db.session.add(p)
    db.session.commit()
    return jsonify({"success": True, "proveedor": p.to_dict()})


@ventas_bp.route("/api/cotizacion/<string:numero>", methods=["GET"])
@login_required
def api_cotizacion_by_numero(numero: str):
    documento = _load_document_by_number("cotizacion", numero)
    if documento is None:
        return jsonify({"success": False, "message": "Documento no encontrado"}), 404
    return jsonify({"success": True, "documento": _serialize_document(documento)})


@ventas_bp.route("/api/orden_venta/<string:numero>", methods=["GET"])
@login_required
def api_orden_venta_by_numero(numero: str):
    documento = _load_document_by_number("orden_venta", numero)
    if documento is None:
        return jsonify({"success": False, "message": "Documento no encontrado"}), 404
    return jsonify({"success": True, "documento": _serialize_document(documento)})


@ventas_bp.route("/api/orden_compra/<string:numero>", methods=["GET"])
@login_required
def api_orden_compra_by_numero(numero: str):
    documento = _load_document_by_number("orden_compra", numero)
    if documento is None:
        return jsonify({"success": False, "message": "Documento no encontrado"}), 404
    return jsonify({"success": True, "documento": _serialize_document(documento)})


@ventas_bp.route("/api/factura/<string:numero>", methods=["GET"])
@login_required
def api_factura_by_numero(numero: str):
    documento = _load_document_by_number("factura", numero)
    if documento is None:
        return jsonify({"success": False, "message": "Documento no encontrado"}), 404
    return jsonify({"success": True, "documento": _serialize_document(documento)})


@ventas_bp.route("/api/convert/cotizacion/<string:numero>/orden_venta", methods=["POST"])
@login_required
def api_convert_cotizacion_orden_venta(numero: str):
    safe_numero = (numero or "").strip().upper()
    print(f"Buscando cotizacion: {safe_numero}")
    source = _load_document_by_numero_or_id("cotizacion", safe_numero)
    if source is None or (source.tipo or "").strip().lower() != "cotizacion":
        return jsonify({"success": False, "message": "Cotizacion no encontrada"}), 404

    try:
        target = _copy_document_with_trace(source, "orden_venta")
        db.session.commit()
        return jsonify({
            "success": True,
            "documento": _serialize_document(target),
            "redirect_url": url_for("ventas.orden_venta", numero=target.numero),
        })
    except Exception as exc:
        db.session.rollback()
        return jsonify({"success": False, "message": f"No se pudo convertir: {exc}"}), 400


@ventas_bp.route("/api/convert/orden_venta/<string:numero>/factura", methods=["POST"])
@login_required
def api_convert_orden_venta_factura(numero: str):
    safe_numero = (numero or "").strip().upper()
    source = _load_document_by_numero_or_id("orden_venta", safe_numero)
    if source is None or (source.tipo or "").strip().lower() != "orden_venta":
        return jsonify({"success": False, "message": "Orden de venta no encontrada"}), 404

    payload = request.get_json(silent=True) or {}
    target_tipo_documento = (payload.get("tipo_documento") or "factura").strip().lower()
    if target_tipo_documento not in {"factura", "boleta"}:
        return jsonify({"success": False, "message": "Tipo de documento invalido. Usa factura o boleta."}), 400

    try:
        target = _copy_document_with_trace(source, "factura", target_tipo_documento=target_tipo_documento)
        ok, errors = _apply_stock_for_document(
            target,
            direction="out",
            reason=f"Facturacion desde OV {source.numero or source.id}",
        )
        if not ok:
            raise ValueError("; ".join(errors))
        db.session.commit()
        return jsonify({
            "success": True,
            "documento": _serialize_document(target),
            "redirect_url": url_for("ventas.facturacion", numero=target.numero),
        })
    except Exception as exc:
        db.session.rollback()
        return jsonify({"success": False, "message": f"No se pudo facturar: {exc}"}), 400


@ventas_bp.route("/api/convert/orden_compra/<int:documento_id>/ingreso", methods=["POST"])
@login_required
def api_convert_orden_compra_ingreso(documento_id: int):
    source = db.session.get(DocumentoVenta, documento_id)
    if source is None or (source.tipo or "").strip().lower() != "orden_compra":
        return jsonify({"success": False, "message": "Orden de compra no encontrada"}), 404

    try:
        target = _copy_document_with_trace(source, "factura_proveedor")
        ok, errors = _apply_stock_for_document(
            target,
            direction="in",
            reason=f"Ingreso desde OC {source.numero or source.id}",
        )
        if not ok:
            raise ValueError("; ".join(errors))
        db.session.commit()
        return jsonify({"success": True, "documento": _serialize_document(target)})
    except Exception as exc:
        db.session.rollback()
        return jsonify({"success": False, "message": f"No se pudo registrar ingreso: {exc}"}), 400


@ventas_bp.route("/api/convert/factura/<int:documento_id>/nota_credito", methods=["POST"])
@login_required
def api_convert_factura_nota_credito(documento_id: int):
    source = db.session.get(DocumentoVenta, documento_id)
    if source is None or (source.tipo or "").strip().lower() not in {"factura", "boleta"}:
        return jsonify({"success": False, "message": "Factura no encontrada"}), 404

    payload = request.get_json(silent=True) or {}
    razon = (payload.get("razon") or "Devolucion total").strip()

    try:
        nota = NotaCredito(
            documento_venta_id=source.id,
            numero=_next_credit_note_number(),
            razon=razon,
            subtotal=float(source.subtotal or 0),
            impuesto=float(source.impuesto or 0),
            total=float(source.total or 0),
            usuario=session.get("user") or "sistema",
            source_id=source.id,
            source_type="factura",
            root_id=source.root_id or source.id,
        )
        for src_item in source.items:
            nota.items.append(
                NotaCreditoItem(
                    codigo_producto=(src_item.codigo_producto or "").strip().upper(),
                    descripcion=src_item.descripcion or "",
                    marca=(src_item.marca or "").strip().upper(),
                    bodega=(src_item.bodega or "").strip() or "Bodega 1",
                    cantidad=int(src_item.cantidad or 0),
                    precio_unitario=float(src_item.precio_unitario or 0),
                    subtotal=float(src_item.subtotal or 0),
                )
            )

        db.session.add(nota)
        db.session.flush()

        for item in nota.items:
            qty = _safe_int(str(item.cantidad or 0), default=0)
            if qty <= 0:
                continue
            err = _adjust_product_stock(
                codigo=(item.codigo_producto or "").strip().upper(),
                marca=(item.marca or "").strip().upper(),
                bodega=(item.bodega or "").strip() or "Bodega 1",
                delta=qty,
                reason=f"Nota de credito {nota.numero}",
            )
            if err:
                raise ValueError(err)

        nota.stock_restored = True
        db.session.commit()

        return jsonify(
            {
                "success": True,
                "nota_credito": {
                    "id": nota.id,
                    "numero": nota.numero,
                    "source_id": nota.source_id,
                    "root_id": nota.root_id,
                    "total": nota.total,
                    "status": nota.status,
                },
            }
        )
    except Exception as exc:
        db.session.rollback()
        return jsonify({"success": False, "message": f"No se pudo crear nota de credito: {exc}"}), 400


@ventas_bp.route("/api/trace/<string:numero>", methods=["GET"])
@login_required
def api_traceability(numero: str):
    if not _is_admin_user():
        return jsonify({"success": False, "message": "Acceso denegado"}), 403

    safe_numero = (numero or "").strip().upper()
    start = DocumentoVenta.query.filter(func.upper(DocumentoVenta.numero) == safe_numero).order_by(DocumentoVenta.id.desc()).first()
    if start is None and safe_numero.isdigit():
        start = db.session.get(DocumentoVenta, int(safe_numero))
    if start is None:
        return jsonify({"success": False, "message": "Documento no encontrado"}), 404

    root = start
    if start.root_id:
        root_candidate = db.session.get(DocumentoVenta, start.root_id)
        if root_candidate is not None:
            root = root_candidate

    chain = _trace_chain_from_document(root)
    return jsonify(
        {
            "success": True,
            "root": _serialize_chain_node(root),
            "chain": chain,
        }
    )


@ventas_bp.route("/api/client/<int:client_id>/history", methods=["GET"])
@login_required
def api_client_history(client_id: int):
    cliente, payload = _build_client_history_payload(client_id)
    if cliente is None or payload is None:
        return jsonify({"success": False, "message": "Cliente no encontrado"}), 404
    return jsonify({"success": True, **payload})


@ventas_bp.route("/api/supplier/<int:supplier_id>/history", methods=["GET"])
@login_required
def api_supplier_history(supplier_id: int):
    proveedor, payload = _build_supplier_history_payload(supplier_id)
    if proveedor is None or payload is None:
        return jsonify({"success": False, "message": "Proveedor no encontrado"}), 404
    return jsonify({"success": True, **payload})


@ventas_bp.route("/api/product/<int:product_id>/history", methods=["GET"])
@login_required
def api_product_history_by_id(product_id: int):
    row = db.session.execute(
        text("SELECT CODIGO FROM productos WHERE id = :pid LIMIT 1"),
        {"pid": int(product_id)},
    ).mappings().first()
    if row is None:
        return jsonify({"success": False, "message": "Producto no encontrado"}), 404

    codigo = (row.get("CODIGO") or "").strip().upper()
    if not codigo:
        return jsonify({"success": False, "message": "Producto sin codigo"}), 404

    from app.utils.stock_control import get_product_history

    history = get_product_history(codigo)
    return jsonify({"success": True, "codigo": codigo, **history})


# ─────────────────────────────────────────────────────────────
#  STOCK CONTROL API (REAL-TIME INVENTORY)
# ─────────────────────────────────────────────────────────────

@ventas_bp.route("/api/stock/check", methods=["POST"])
@login_required
def api_stock_check():
    """Check if stock is available for sale items."""
    from app.utils.stock_control import check_stock_availability, validate_sale_items
    
    data = request.get_json(silent=True) or {}
    items = data.get("items", [])
    
    # Validate item structure
    is_valid, error_msg = validate_sale_items(items)
    if not is_valid:
        return jsonify({"success": False, "available": False, "message": error_msg}), 400
    
    # Check stock availability
    is_available, error_msg = check_stock_availability(items)
    
    return jsonify({
        "success": True,
        "available": is_available,
        "message": error_msg or "Stock disponible para todos los items",
    })


@ventas_bp.route("/api/stock/product/<codigo>", methods=["GET"])
@login_required
def api_product_stock(codigo: str):
    """Get detailed stock info for a product by variant/warehouse."""
    from app.utils.stock_control import get_stock_by_variant, get_available_stock
    
    codigo = (codigo or "").strip().upper()
    if not codigo:
        return jsonify({"success": False, "message": "Codigo vacio"}), 400
    
    variants = get_stock_by_variant(codigo)
    total = get_available_stock(codigo)
    
    return jsonify({
        "success": True,
        "codigo": codigo,
        "total_stock": total,
        "by_variant": variants,
    })


@ventas_bp.route("/api/product/history/<codigo>", methods=["GET"])
@login_required
def api_product_history(codigo: str):
    """Get full product traceability: all ingresos, sales, credit notes, and current stock."""
    from app.utils.stock_control import get_product_history
    
    codigo = (codigo or "").strip().upper()
    if not codigo:
        return jsonify({"success": False, "message": "Codigo vacio"}), 400
    
    history = get_product_history(codigo)
    
    return jsonify({
        "success": True,
        **history,
    })


@ventas_bp.route("/api/product/last-sale/<codigo>", methods=["GET"])
@login_required
def api_product_last_sale(codigo: str):
    """Get the most recent sale for a product."""
    from app.utils.stock_control import get_product_history
    
    codigo = (codigo or "").strip().upper()
    if not codigo:
        return jsonify({"success": False, "message": "Codigo vacio"}), 400
    
    history = get_product_history(codigo, limit=5)
    last_sale = history.get("last_sale")
    
    return jsonify({
        "success": True,
        "last_sale": last_sale,
    })


# ─────────────────────────────────────────────────────────────
#  CREDIT NOTE API
# ─────────────────────────────────────────────────────────────

@ventas_bp.route("/api/credit-note", methods=["POST"])
@login_required
def api_credit_note_create():
    """Create a credit note from an existing sales document."""
    from app.utils.stock_control import restore_stock_for_credit_note
    
    data = request.get_json(silent=True) or {}
    
    # Validate input
    documento_id = data.get("documento_id")
    items = data.get("items", [])
    razon = (data.get("razon") or "").strip()
    
    if not documento_id:
        return jsonify({"success": False, "message": "Documento original requerido"}), 400
    if not items:
        return jsonify({"success": False, "message": "Se requieren items para la nota de crédito"}), 400
    if not razon:
        return jsonify({"success": False, "message": "Razón de devolución requerida"}), 400
    
    try:
        # Get original document
        documento = DocumentoVenta.query.get(documento_id)
        if not documento:
            return jsonify({"success": False, "message": "Documento no encontrado"}), 400
        
        # Calculate totals
        subtotal = sum(item.get("cantidad", 0) * item.get("precio_unitario", 0) for item in items)
        impuesto = round(subtotal * 0.19, 2)
        total = round(subtotal +impuesto, 2)
        
        # Create credit note
        numero = _next_credit_note_number()
        nota = NotaCredito(
            documento_venta_id=documento_id,
            numero=numero,
            razon=razon,
            subtotal=subtotal,
            impuesto=impuesto,
            total=total,
            usuario=session.get("user") or "system",
            source_id=documento.id,
            source_type=(documento.tipo or "").strip().lower(),
            root_id=documento.root_id or documento.id,
        )
        
        # Create items
        for item in items:
            nota_item = NotaCreditoItem(
                codigo_producto=item.get("codigo_producto"),
                descripcion=item.get("descripcion"),
                marca=item.get("marca"),
                bodega=item.get("bodega"),
                cantidad=item.get("cantidad", 0),
                precio_unitario=item.get("precio_unitario", 0),
                subtotal=item.get("cantidad", 0) * item.get("precio_unitario", 0),
            )
            nota.items.append(nota_item)
        
        db.session.add(nota)
        db.session.flush()
        
        # Restore stock
        success, msg = restore_stock_for_credit_note(nota.id, session.get("user"))
        if not success:
            db.session.rollback()
            return jsonify({"success": False, "message": msg}), 400
        
        db.session.commit()
        
        return jsonify({
            "success": True,
            "nota_credito": {
                "id": nota.id,
                "numero": nota.numero,
                "documento_venta_id": documento_id,
                "total": nota.total,
                "status": nota.status,
            },
            "message": "Nota de crédito creada exitosamente",
        })
    
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "message": f"Error al crear nota de crédito: {str(e)}"}), 500


@ventas_bp.route("/api/credit-notes/documento/<int:documento_id>", methods=["GET"])
@login_required
def api_credit_notes_by_documento(documento_id: int):
    """Get all credit notes for a specific sales document."""
    from app.ventas.models import NotaCredito
    
    documento = DocumentoVenta.query.get(documento_id)
    if not documento:
        return jsonify({"success": False, "message": "Documento no encontrado"}), 400
    
    notas = NotaCredito.query.filter_by(documento_venta_id=documento_id).order_by(
        NotaCredito.fecha_documento.desc()
    ).all()
    
    notas_data = []
    for nota in notas:
        notas_data.append({
            "id": nota.id,
            "numero": nota.numero,
            "razon": nota.razon,
            "fecha": nota.fecha_documento.isoformat(),
            "total": nota.total,
            "status": nota.status,
            "items_count": len(nota.items),
        })
    
    return jsonify({
        "success": True,
        "documento_venta_id": documento_id,
        "credit_notes": notas_data,
    })


# ─────────────────────────────────────────────────────────────
#  PAYMENT API
# ─────────────────────────────────────────────────────────────

@ventas_bp.route("/api/documento/<int:doc_id>/pago", methods=["POST"])
@login_required
def api_registrar_pago(doc_id: int):
    """Register payment method and mark document as paid."""
    doc = db.session.get(DocumentoVenta, doc_id)
    if doc is None:
        return jsonify({"ok": False, "error": "Documento no encontrado"}), 404

    data = request.get_json(force=True) or {}
    metodo = (data.get("metodo_pago") or "efectivo").strip().lower()
    if metodo not in METODO_PAGO_OPTIONS:
        return jsonify({"ok": False, "error": f"Método de pago inválido: {metodo}"}), 400

    doc.metodo_pago = metodo
    doc.estado_pago = "pagado"
    doc.updated_at = datetime.utcnow()
    db.session.commit()

    return jsonify({
        "ok": True,
        "metodo_pago": doc.metodo_pago,
        "estado_pago": doc.estado_pago,
        "metodo_label": METODO_PAGO_LABELS.get(metodo, metodo),
    })


@ventas_bp.route("/api/documento/<int:doc_id>/metodo_pago", methods=["GET"])
@login_required
def api_get_pago(doc_id: int):
    """Return current payment state for a document."""
    doc = db.session.get(DocumentoVenta, doc_id)
    if doc is None:
        return jsonify({"ok": False, "error": "Documento no encontrado"}), 404
    return jsonify({
        "ok": True,
        "doc_id": doc.id,
        "metodo_pago": doc.metodo_pago or "",
        "estado_pago": doc.estado_pago or "pendiente",
        "metodo_label": METODO_PAGO_LABELS.get(doc.metodo_pago or "", ""),
    })


def _build_whatsapp_payload(doc: DocumentoVenta) -> tuple[str, str, str, str]:
    """Build normalized whatsapp payload (url, phone, message) for a document.

    This always generates the document PDF first and includes a public signed URL.
    """
    from .document_delivery import build_public_pdf_token, render_document_pdf

    def greeting_by_hour() -> str:
        now = datetime.now()
        hour = now.hour
        if 5 <= hour < 12:
            return "Buenos días"
        if 12 <= hour < 20:
            return "Buenas tardes"
        return "Buenas noches"

    tipo = (doc.tipo or "").strip().lower()
    numero = ((doc.numero or "").strip().upper() or str(doc.id))
    phone = (doc.cliente_telefono or "").strip()

    tipo_label = {
        "cotizacion": "cotización",
        "orden_venta": "orden de venta",
        "orden_compra": "orden de compra",
        "factura": "factura",
        "boleta": "boleta",
    }.get(tipo, tipo.replace("_", " "))

    # Ensure the PDF exists before we build/send the WhatsApp message.
    render_document_pdf(doc, COMPANY_INFO)
    token = build_public_pdf_token(doc.id)
    pdf_url = url_for("ventas.public_document_pdf", token=token, _external=True)

    greeting = greeting_by_hour()
    lines = [
        f"Hola 👋 {greeting}, le escribe Andes Auto Parts.",
        "",
        f"Adjuntamos su {tipo_label} N° {numero} 📄",
        "",
        "Puede revisarlo aquí:",
        f"🔗 Ver {tipo_label} N° {numero}",
        pdf_url,
    ]
    if tipo == "cotizacion":
        lines.extend([
            "",
            "⏳ Esta cotización tiene una validez de 10 días.",
        ])
    lines.extend([
        "",
        "Quedamos atentos a cualquier consulta 🙌",
        "Muchas gracias por su preferencia.",
    ])
    msg = "\n".join(lines)

    phone_clean = phone.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if not phone_clean.startswith("+"):
        if phone_clean.startswith("9"):
            phone_clean = "+56" + phone_clean
        elif phone_clean.startswith("0"):
            phone_clean = "+56" + phone_clean[1:]
        else:
            phone_clean = "+56" + phone_clean

    wa_url = f"https://wa.me/{phone_clean}?text={quote(msg)}"
    return wa_url, phone_clean, msg, pdf_url


def _runtime_diagnostics() -> dict[str, str | None]:
    return {
        "python": sys.executable,
        "venv": os.environ.get("VIRTUAL_ENV"),
        "cwd": os.getcwd(),
    }


@ventas_bp.route("/public/documento/<string:token>.pdf", methods=["GET"])
def public_document_pdf(token: str):
    """Serve a document PDF using a signed token without authentication.

    The token has expiration and is validated before serving.
    """
    from .document_delivery import read_public_pdf_token, render_document_pdf

    doc_id = read_public_pdf_token(token)
    if not doc_id:
        return jsonify({"ok": False, "error": "Token inválido o expirado"}), 404

    doc = db.session.get(DocumentoVenta, doc_id)
    if doc is None:
        return jsonify({"ok": False, "error": "Documento no encontrado"}), 404

    pdf_path = render_document_pdf(doc, COMPANY_INFO)
    return send_file(
        pdf_path,
        mimetype="application/pdf",
        as_attachment=False,
        download_name=pdf_path.name,
    )


# ─────────────────────────────────────────────────────────────
#  EMAIL API
# ─────────────────────────────────────────────────────────────

@ventas_bp.route("/api/documento/<int:doc_id>/enviar_email", methods=["POST"])
@login_required
def api_enviar_email_documento(doc_id: int):
    """Send document summary by email to the client address."""
    from app.utils.email_utils import send_document_email, build_document_email_body

    doc = db.session.get(DocumentoVenta, doc_id)
    if doc is None:
        return jsonify({"ok": False, "error": "Documento no encontrado"}), 404

    email_dest = (doc.cliente_email or "").strip()
    if not email_dest:
        return jsonify({"ok": False, "error": "El documento no tiene email de destinatario"}), 400

    subject = f"{COMPANY_INFO['name']} – {(doc.tipo or '').replace('_', ' ').title()} N° {doc.numero or doc.id}"
    body_html = build_document_email_body(
        tipo=doc.tipo or "",
        numero=doc.numero or str(doc.id),
        empresa=COMPANY_INFO["name"],
        cliente=doc.cliente_nombre or "",
        total=doc.total or 0.0,
        fecha=doc.fecha_documento.strftime("%d/%m/%Y") if doc.fecha_documento else "",
        items=[
            {
                "codigo": (i.codigo_producto or "").upper(),
                "descripcion": i.descripcion or "",
                "cantidad": int(i.cantidad or 0),
                "precio": float(i.precio_unitario or 0),
                "subtotal": float(i.subtotal or 0),
            }
            for i in doc.items
        ],
    )

    ok, msg = send_document_email(
        to=email_dest,
        subject=subject,
        body_html=body_html,
    )

    if ok:
        return jsonify({"ok": True, "message": f"Email enviado a {email_dest}"})
    return jsonify({"ok": False, "error": msg}), 500


@ventas_bp.route("/api/enviar_email/<int:doc_id>", methods=["GET", "POST"])
@login_required
def api_enviar_email_por_id(doc_id: int):
    """Compatibility endpoint: send document by id."""
    return api_enviar_email_documento(doc_id)


@ventas_bp.route("/api/reenviar/<int:doc_id>", methods=["GET", "POST"])
@login_required
def api_reenviar_documento(doc_id: int):
    """Re-send endpoint (same behavior as send email)."""
    return api_enviar_email_documento(doc_id)


@ventas_bp.route("/api/whatsapp/<int:doc_id>", methods=["GET"])
@login_required
def api_whatsapp_por_id(doc_id: int):
    """Compatibility endpoint: open WhatsApp by document id."""
    doc = db.session.get(DocumentoVenta, doc_id)
    if doc is None:
        return jsonify({"ok": False, "error": "Documento no encontrado"}), 404

    if not (doc.cliente_telefono or "").strip():
        return jsonify({"ok": False, "error": "El documento no tiene teléfono del cliente"}), 400

    try:
        wa_url, phone_clean, msg, pdf_url = _build_whatsapp_payload(doc)
    except Exception as exc:
        current_app.logger.exception("Error preparando WhatsApp para doc_id=%s", doc_id)
        diagnostics = _runtime_diagnostics()
        return jsonify({
            "ok": False,
            "error": f"No se pudo preparar el documento para WhatsApp: {exc}",
            "runtime": diagnostics,
        }), 500

    if request.args.get("format") == "json":
        return jsonify({
            "ok": True,
            "whatsapp_url": wa_url,
            "phone": phone_clean,
            "message": msg,
            "pdf_url": pdf_url,
        })
    return redirect(wa_url)


@ventas_bp.route("/api/metodo_pago_options", methods=["GET"])
@login_required
def api_metodo_pago_options():
    """Return list of valid payment methods with labels."""
    return jsonify({
        "ok": True,
        "options": [{"value": k, "label": METODO_PAGO_LABELS[k]} for k in METODO_PAGO_OPTIONS],
    })


# ─────────────────────────────────────────────────────────────
#  SEND DOCUMENT BY TIPO/NUMERO  (Alternative API endpoints)
# ─────────────────────────────────────────────────────────────

@ventas_bp.route("/api/enviar_email/<string:tipo>/<string:numero>", methods=["POST"])
@login_required
def api_enviar_email(tipo: str, numero: str):
    """Send document by email using tipo and numero (alternative to doc_id endpoint)."""
    from app.utils.email_utils import send_document_email, build_document_email_body

    tipo = (tipo or "").strip().lower()
    numero = (numero or "").strip().upper()

    if not tipo or not numero:
        return jsonify({"ok": False, "error": "Tipo y número requeridos"}), 400

    # Find document by tipo and numero
    doc = DocumentoVenta.query.filter_by(tipo=tipo).filter(
        func.upper(DocumentoVenta.numero) == numero
    ).order_by(DocumentoVenta.id.desc()).first()

    if doc is None:
        return jsonify({"ok": False, "error": "Documento no encontrado"}), 404

    email_dest = (doc.cliente_email or "").strip()
    if not email_dest:
        return jsonify({"ok": False, "error": "El documento no tiene email de destinatario"}), 400

    subject = f"{COMPANY_INFO['name']} – {(doc.tipo or '').replace('_', ' ').title()} N° {doc.numero or doc.id}"
    body_html = build_document_email_body(
        tipo=doc.tipo or "",
        numero=doc.numero or str(doc.id),
        empresa=COMPANY_INFO["name"],
        cliente=doc.cliente_nombre or "",
        total=doc.total or 0.0,
        fecha=doc.fecha_documento.strftime("%d/%m/%Y") if doc.fecha_documento else "",
        items=[
            {
                "codigo": (i.codigo_producto or "").upper(),
                "descripcion": i.descripcion or "",
                "cantidad": int(i.cantidad or 0),
                "precio": float(i.precio_unitario or 0),
                "subtotal": float(i.subtotal or 0),
            }
            for i in doc.items
        ],
    )

    ok, msg = send_document_email(
        to=email_dest,
        subject=subject,
        body_html=body_html,
    )

    if ok:
        return jsonify({"ok": True, "message": f"Email enviado a {email_dest}"})
    return jsonify({"ok": False, "error": msg}), 500


@ventas_bp.route("/api/whatsapp/<string:tipo>/<string:numero>", methods=["GET"])
@login_required
def api_whatsapp(tipo: str, numero: str):
    """Generate WhatsApp link for document using tipo and numero."""
    tipo = (tipo or "").strip().lower()
    numero = (numero or "").strip().upper()

    if not tipo or not numero:
        return jsonify({"ok": False, "error": "Tipo y número requeridos"}), 400

    # Find document to get phone
    doc = DocumentoVenta.query.filter_by(tipo=tipo).filter(
        func.upper(DocumentoVenta.numero) == numero
    ).order_by(DocumentoVenta.id.desc()).first()

    if doc is None:
        return jsonify({"ok": False, "error": "Documento no encontrado"}), 404

    phone = (doc.cliente_telefono or "").strip()
    if not phone:
        return jsonify({"ok": False, "error": "El documento no tiene teléfono del cliente"}), 400

    try:
        wa_url, phone_clean, msg, pdf_url = _build_whatsapp_payload(doc)
    except Exception as exc:
        current_app.logger.exception("Error preparando WhatsApp para tipo=%s numero=%s", tipo, numero)
        diagnostics = _runtime_diagnostics()
        return jsonify({
            "ok": False,
            "error": f"No se pudo preparar el documento para WhatsApp: {exc}",
            "runtime": diagnostics,
        }), 500

    return jsonify({
        "ok": True,
        "whatsapp_url": wa_url,
        "phone": phone_clean,
        "message": msg,
        "pdf_url": pdf_url,
    })
