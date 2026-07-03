from __future__ import annotations

import json
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote
import os
import sys

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from sqlalchemy import func, or_, text
from werkzeug.security import check_password_hash

from app.extensions import db
from app.seguridad.models import Usuario as UsuarioSistema
from app.utils.decorators import login_required
from app.utils.permissions import has_permission
from app.utils.rut_utils import clean_rut, format_rut, is_valid_rut
from app.utils.phone_format import format_phone_display, phone_to_compact_e164
from app.utils.party_fields import normalize_party_email, party_text_upper
from app.utils.variante_comercial import find_variante_stock, merge_ingreso_ref_variante_overrides
from app.bodega.models import (
    IngresoDocumento,
    IngresoDocumentoItem,
    MovimientoStock,
    PickingVenta,
    PickingVentaLine,
    ProductoVarianteStock,
    ProveedorCodigoInterno,
)
from .models import (
    Cliente,
    ClienteSaldoFavorMovimiento,
    Proveedor,
    DocumentoVenta,
    DocumentoVentaItem,
    NotaCredito,
    NotaCreditoItem,
)

ventas_bp = Blueprint("ventas", __name__, url_prefix="/ventas")


@ventas_bp.before_request
def _ventas_module_guard():
    # login_required de cada vista sigue siendo la barrera principal para sesión.
    if "user" not in session:
        return None
    if has_permission(session.get("user"), session.get("rol"), "mod_ventas"):
        return None
    is_ajax = request.is_json or (request.headers.get("X-Requested-With") or "").lower() == "xmlhttprequest"
    if is_ajax or request.path.startswith("/ventas/api/"):
        return jsonify({"success": False, "message": "Permiso denegado para modulo Ventas"}), 403
    flash("No tienes permisos para acceder al modulo Ventas.", "error")
    return redirect(url_for("productos.buscar"))


def _deny_perm_response(message: str):
    is_ajax = request.is_json or (request.headers.get("X-Requested-With") or "").lower() == "xmlhttprequest"
    if is_ajax or request.path.startswith("/ventas/api/"):
        return jsonify({"success": False, "message": message}), 403
    flash(message, "error")
    return redirect(url_for("productos.buscar"))


def _ajax_doc_save_response(ctx: dict, *, default_ok_message: str):
    """Respuesta JSON para guardar documento vía fetch (SPA); None si no aplica."""
    is_ajax = (request.headers.get("X-Requested-With") or "").lower() == "xmlhttprequest"
    if request.method != "POST" or not is_ajax:
        return None
    if ctx.get("generated"):
        return jsonify(
            {
                "success": True,
                "message": default_ok_message,
                "doc_number": (ctx.get("saved_number") or ctx.get("doc_number") or "").strip(),
                "doc_id": int(ctx.get("loaded_document_id") or 0),
            }
        )
    errs = list(ctx.get("validation_errors") or [])
    save_err = (ctx.get("save_error") or "").strip()
    if save_err:
        return jsonify(
            {
                "success": False,
                "message": save_err,
                "validation_errors": errs,
            }
        ), 500
    msg = errs[0] if errs else "No se pudo guardar el documento. Revisa los datos e inténtalo nuevamente."
    return jsonify(
        {
            "success": False,
            "message": msg,
            "validation_errors": errs,
        }
    ), 400

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
ORIGEN_COMPRA_DEFAULT = "nacional"
ORIGEN_COMPRA_OPCIONES = ("nacional", "importacion")

# Lista de precios en ventas: P_PUBLICO si es distinto de 0; si no PREC_MAYOR; si no 0 (luego puede rellenarse desde ingreso).
_SQL_PRECIO_LISTA = "COALESCE(NULLIF(P_PUBLICO, 0), NULLIF(PREC_MAYOR, 0), 0)"

METODO_PAGO_OPTIONS = [
    "efectivo",
    "transferencia",
    "tarjeta_debito",
    "tarjeta_credito",
    "credito_30",
    "credito_60",
    "credito_90",
    "cheque",
    "saldo_favor",
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
    "saldo_favor": "Saldo a favor (crédito del cliente)",
}


def _round_money_cl(x: float | None) -> float:
    return round(float(x or 0), 2)


def _cliente_saldo_favor_ledger(cliente_id: int) -> float:
    if not cliente_id:
        return 0.0
    q = (
        db.session.query(func.coalesce(func.sum(ClienteSaldoFavorMovimiento.monto), 0.0))
        .filter(ClienteSaldoFavorMovimiento.cliente_id == int(cliente_id))
        .scalar()
    )
    return float(q or 0)


def _max_monto_saldo_usable_por_cliente(
    cliente_id: int, documento: DocumentoVenta | None, prev_monto_en_doc: float
) -> float:
    base = _cliente_saldo_favor_ledger(cliente_id)
    if documento and getattr(documento, "id", None) and (prev_monto_en_doc or 0) > 0:
        return _round_money_cl(base + float(prev_monto_en_doc or 0))
    return _round_money_cl(base)


def _aplicar_pago_saldo_favor_en_documento(
    documento: DocumentoVenta,
    prev_monto: float,
    new_monto: float,
    metodo_resto: str,
) -> str | None:
    """Aplica abono con saldo a favor; registra movimiento y estado de pago. None = ok."""
    new_monto = _round_money_cl(new_monto)
    prev_monto = _round_money_cl(prev_monto)
    if new_monto <= 0.001 and prev_monto <= 0.001:
        documento.monto_saldo_favor = 0.0
        return None
    cid = int(documento.cliente_id or 0)
    if not cid:
        return "Se requiere cliente para usar saldo a favor en documentos con total."
    total = _round_money_cl(documento.total or 0)
    if new_monto < 0:
        return "El monto a descontar del saldo no puede ser negativo."
    if new_monto > total + 0.02:
        return "No puedes aplicar un saldo mayor al total del documento."
    cap = _max_monto_saldo_usable_por_cliente(cid, documento, prev_monto)
    if new_monto > cap + 0.02:
        return f"El cliente no tiene saldo a favor suficiente. Máximo aplicable: ${cap:,.0f}".replace(",", ".")
    delta = _round_money_cl(float(prev_monto) - new_monto)
    if abs(delta) > 0.001:
        db.session.add(
            ClienteSaldoFavorMovimiento(
                cliente_id=cid,
                monto=delta,
                tipo="ajuste_documento",
                documento_venta_id=documento.id,
                ref_factura_numero=(documento.numero or "")[:100] if documento.numero else None,
                razon="Uso de saldo a favor en factura o boleta",
                usuario=session.get("user") or "sistema",
            )
        )
    documento.monto_saldo_favor = new_monto
    resto = _round_money_cl(total - new_monto)
    met = (metodo_resto or "").strip().lower()
    if resto <= 0.02:
        documento.metodo_pago = "saldo_favor"
        documento.estado_pago = "pagado"
        documento.pago_referencia = f"Cubierto con saldo a favor (${_format_currency(new_monto)})"[:200]
    else:
        if met in METODO_PAGO_OPTIONS and met != "saldo_favor":
            documento.metodo_pago = met
            documento.estado_pago = "pagado"
            documento.pago_referencia = (
                f"Saldo a favor: ${_format_currency(new_monto)}; resto {METODO_PAGO_LABELS.get(met, met)}: "
                f"${_format_currency(resto)}"
            )[:200]
        else:
            documento.estado_pago = "pendiente"
            documento.pago_referencia = (
                f"Parcial con saldo: ${_format_currency(new_monto)}; pendiente resto ${_format_currency(resto)}. "
                f"Elegí método de pago para el resto o registrá pago luego."
            )[:200]
    return None


def _normalize_origen_compra(raw: str | None) -> str:
    value = (raw or "").strip().lower()
    if value in ORIGEN_COMPRA_OPCIONES:
        return value
    return ORIGEN_COMPRA_DEFAULT


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


def _upper_text(raw: str | None) -> str:
    """Strip and uppercase free-text party fields (names, addresses, giro)."""
    return party_text_upper(raw)


def _normalize_party_email(raw: str | None) -> str:
    """Strip and lowercase email for stable storage and lookup."""
    return normalize_party_email(raw)


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


def _extract_pagos_filtro() -> str:
    raw = (_clean_text(request.args.get("filtro")) or "pendientes").lower()
    if raw in ("pendientes", "pagados", "todos"):
        return raw
    return "pendientes"


def _find_factura_boleta_por_busqueda(term: str) -> DocumentoVenta | None:
    t = (term or "").strip().upper()
    if not t:
        return None
    base = DocumentoVenta.query.filter(DocumentoVenta.tipo.in_(["factura", "boleta"]))
    exact = base.filter(func.upper(DocumentoVenta.numero) == t).order_by(DocumentoVenta.id.desc()).first()
    if exact:
        return exact
    exact_oc = (
        base.filter(func.upper(DocumentoVenta.numero_oc_cliente) == t)
        .order_by(DocumentoVenta.id.desc())
        .first()
    )
    if exact_oc:
        return exact_oc
    return (
        base.filter(
            or_(
                DocumentoVenta.numero.ilike(f"%{t}%"),
                DocumentoVenta.numero_oc_cliente.ilike(f"%{t}%"),
            )
        )
        .order_by(DocumentoVenta.fecha_documento.desc(), DocumentoVenta.id.desc())
        .first()
    )


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
        codigos = descripciones = cantidades = precios = marcas = bodegas = origenes = margenes = modelos_linea = []
    elif hasattr(form, "getlist"):
        codigos = form.getlist("item_codigo[]")
        descripciones = form.getlist("item_descripcion[]")
        cantidades = form.getlist("item_cantidad[]")
        precios = form.getlist("item_precio[]")
        marcas = form.getlist("item_marca[]")
        bodegas = form.getlist("item_bodega[]")
        origenes = form.getlist("item_origen_compra[]")
        margenes = form.getlist("item_margen[]")
        modelos_linea = form.getlist("item_modelo_linea[]")
    else:
        codigos = form.get("item_codigo[]", []) or []
        descripciones = form.get("item_descripcion[]", []) or []
        cantidades = form.get("item_cantidad[]", []) or []
        precios = form.get("item_precio[]", []) or []
        marcas = form.get("item_marca[]", []) or []
        bodegas = form.get("item_bodega[]", []) or []
        origenes = form.get("item_origen_compra[]", []) or []
        margenes = form.get("item_margen[]", []) or []
        modelos_linea = form.get("item_modelo_linea[]", []) or []
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
        if not isinstance(origenes, list):
            origenes = [origenes]
        if not isinstance(margenes, list):
            margenes = [margenes]
        if not isinstance(modelos_linea, list):
            modelos_linea = [modelos_linea]

    max_len = max(
        len(codigos),
        len(descripciones),
        len(cantidades),
        len(precios),
        len(marcas),
        len(bodegas),
        len(origenes),
        len(margenes),
        len(modelos_linea),
        1,
    )
    items = []
    for idx in range(max_len):
        codigo = (codigos[idx] if idx < len(codigos) else "").strip().upper()
        descripcion = (descripciones[idx] if idx < len(descripciones) else "").strip()
        cantidad = _safe_int(cantidades[idx] if idx < len(cantidades) else "1", default=1)
        precio = _safe_float(precios[idx] if idx < len(precios) else "0")
        marca = (marcas[idx] if idx < len(marcas) else "").strip().upper()
        bodega = (bodegas[idx] if idx < len(bodegas) else "").strip() or "Bodega 1"
        origen_compra = _normalize_origen_compra(origenes[idx] if idx < len(origenes) else "")
        margen_raw = (margenes[idx] if idx < len(margenes) else "").strip()
        margen_val = _safe_float(margen_raw) if margen_raw else None
        if margen_val is not None and (margen_val < 0 or margen_val > 999.999):
            margen_val = None
        modelo_linea = (modelos_linea[idx] if idx < len(modelos_linea) else "").strip()[:255]
        if not codigo and not descripcion:
            continue
        items.append({
            "codigo": codigo,
            "descripcion": descripcion,
            "cantidad": cantidad,
            "precio": precio,
            "marca": marca,
            "bodega": bodega,
            "origen_compra": origen_compra,
            "margen": margen_val,
            "modelo_linea": modelo_linea,
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
            "origen_compra": ORIGEN_COMPRA_DEFAULT,
            "margen": None,
            "modelo_linea": "",
            "subtotal": 0.0,
        })
    return items


def _wholesale_discount_pct(cliente: Cliente | None) -> float:
    if cliente is None:
        return 0.0
    if not getattr(cliente, "cliente_mayorista", False):
        return 0.0
    try:
        pct = float(getattr(cliente, "margen_descuento_pct", 0) or 0)
    except (TypeError, ValueError):
        pct = 0.0
    return max(0.0, min(100.0, pct))


def _calculate_totals(items: list[dict], cliente: Cliente | None = None) -> dict:
    subtotal_bruto = round(sum(i.get("subtotal", 0.0) for i in items), 2)
    pct = _wholesale_discount_pct(cliente)
    descuento_monto = round(subtotal_bruto * (pct / 100.0), 2) if pct else 0.0
    subtotal = round(subtotal_bruto - descuento_monto, 2)
    iva = round(subtotal * 0.19, 2)
    total = round(subtotal + iva, 2)
    return {
        "subtotal": subtotal,
        "iva": iva,
        "total": total,
        "descuento_monto": descuento_monto,
        "subtotal_bruto": subtotal_bruto,
    }


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


def _numero_es_siguiente_correlativo_libre(numero: str) -> bool:
    """True si el número coincide con el siguiente correlativo libre para su prefijo (p. ej. CO-0002 cuando el máximo existente es CO-0001)."""
    clean = (numero or "").strip().upper()
    if not clean or "-" not in clean:
        return False
    prefix, _, suffix = clean.partition("-")
    if not suffix.isdigit():
        return False
    siguiente = _next_doc_number(prefix)
    return clean == siguiente


def _mensaje_correlativo_invalido(numero: str) -> str:
    clean = (numero or "").strip().upper()
    if "-" in clean:
        prefix = clean.split("-", 1)[0]
        siguiente = _next_doc_number(prefix)
        return (
            f"No se puede abrir el documento {clean}. "
            f"Use el siguiente correlativo disponible ({siguiente}) o busque un documento ya guardado."
        )
    return "Documento no encontrado."


def _serialize_documento_borrador_siguiente(doc_type: str, numero: str) -> dict:
    """Payload igual a un documento nuevo sin fila en BD (id=0), para el correlativo que sigue al máximo existente."""
    today = datetime.now().date().strftime("%Y-%m-%d")
    clean = (numero or "").strip().upper()
    party_payload: dict = {
        "id": 0,
        "proveedor_id": 0,
        "name": "",
        "rut": "",
        "address": "",
        "telefono": "",
        "email": "",
        "region": "",
        "ciudad": "",
        "pais": CHILE_COUNTRY_NAME,
    }
    if doc_type not in {"orden_compra", "factura_proveedor"}:
        party_payload["cliente_mayorista"] = False
        party_payload["margen_descuento_pct"] = 0.0

    if doc_type == "factura":
        ext_kind = "boleta" if clean.startswith("BO-") else "factura"
        tipo_documento = ext_kind
        serialized_doc_type = "factura"
    else:
        tipo_documento = "factura"
        serialized_doc_type = doc_type

    zero = 0.0
    totals = {
        "subtotal": zero,
        "iva": zero,
        "total": zero,
        "descuento": zero,
        "subtotal_bruto": zero,
        "subtotal_fmt": _format_currency(zero),
        "iva_fmt": _format_currency(zero),
        "total_fmt": _format_currency(zero),
        "descuento_fmt": _format_currency(zero),
        "subtotal_bruto_fmt": _format_currency(zero),
    }
    payload = {
        "id": 0,
        "doc_type": serialized_doc_type,
        "tipo_documento": tipo_documento,
        "source_id": None,
        "source_type": "",
        "root_id": None,
        "numero": clean,
        "numero_oc_cliente": "",
        "fecha_documento": today,
        "fecha_vencimiento": today,
        "status": "pendiente",
        "metodo_pago": "",
        "estado_pago": "pendiente",
        "pago_referencia": "",
        "party": party_payload,
        "items": [],
        "totals": totals,
        "notes": "",
    }
    payload.update(
        {
            "puede_convertir_a_orden_venta": True,
            "puede_convertir_a_factura_boleta": True,
            "documento_hijo_resumen": None,
            "documento_hijo_tipo": None,
            "picking_bloquea_facturacion": False,
            "picking_status": None,
        }
    )
    return payload


def _doc_prefix(doc_type: str) -> str:
    mapping = {
        "cotizacion": "CO",
        "orden_venta": "OV",
        "orden_compra": "OC",
        "factura": "FA",
        "factura_proveedor": "FP",
    }
    return mapping.get((doc_type or "").strip().lower(), "DOC")


def _sales_doc_prefix(tipo_documento: str) -> str:
    """Prefijo correlativo: factura → FA, boleta → BO (series independientes)."""
    t = (tipo_documento or "factura").strip().lower()
    if t == "boleta":
        return "BO"
    return "FA"


def _doc_tipo_value(doc_type: str, tipo_documento: str) -> str:
    if doc_type == "factura":
        return (tipo_documento or "factura").strip().lower()
    return doc_type


def _rol_autoriza_margen_bajo(rol_nombre: str | None) -> bool:
    rol = (rol_nombre or "").strip().lower()
    if not rol:
        return False
    return (
        "superadmin" in rol
        or "admin" in rol
        or "encargado" in rol
        or "subencargado" in rol
        or "sub encargado" in rol
    )


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
    pago_saldo_monto: float | None = None,
    metodo_pago_resto: str = "",
    numero_oc_cliente: str = "",
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
    try:
        prev_monto_saldo_favor = float(getattr(documento, "monto_saldo_favor", 0) or 0) if not is_new else 0.0
    except (TypeError, ValueError):
        prev_monto_saldo_favor = 0.0

    party_name = (party.get("name") or "").strip()
    if not party_name and doc_type in {"orden_compra", "factura_proveedor"}:
        party_name = "Compra mostrador"
    party_rut = clean_rut(party.get("rut"))
    party_address = (party.get("address") or "").strip()
    party_phone_raw = (party.get("telefono") or "").strip()
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

    party_phone = phone_to_compact_e164(party_phone_raw, pais) if party_phone_raw else ""

    documento.fecha_documento = fecha_documento
    documento.fecha_vencimiento = fecha_vencimiento
    documento.numero_oc_cliente = (numero_oc_cliente or "").strip()[:100] or None
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
    documento.descuento = float(totals.get("descuento_monto") or 0)
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
        m = item.get("margen")
        margen_db = float(m) if m is not None and m != "" else None
        ml = (item.get("modelo_linea") or "").strip()[:255]
        documento.items.append(
            DocumentoVentaItem(
                codigo_producto=codigo,
                descripcion=descripcion,
                modelo_linea=ml or None,
                marca=(item.get("marca") or "").strip().upper(),
                bodega=(item.get("bodega") or "").strip() or "Bodega 1",
                origen_compra=_normalize_origen_compra(item.get("origen_compra") or ""),
                cantidad=cantidad,
                precio_unitario=precio,
                margen_porcentaje=margen_db,
                subtotal=subtotal,
            )
        )

    db.session.flush()
    if doc_type == "factura" and tipo_value in ("factura", "boleta") and pago_saldo_monto is not None:
        err = _aplicar_pago_saldo_favor_en_documento(
            documento,
            prev_monto_saldo_favor,
            float(pago_saldo_monto or 0),
            metodo_pago_resto or "",
        )
        if err:
            raise ValueError(err)
        db.session.flush()
    return documento


def _documento_hijo_directo(source_id: int | None, tipos: set[str]) -> DocumentoVenta | None:
    """Primer documento cuya trazabilidad apunta a `source_id` (evita conversiones duplicadas)."""
    if not source_id:
        return None
    return (
        DocumentoVenta.query.filter(DocumentoVenta.source_id == source_id)
        .filter(DocumentoVenta.tipo.in_(list(tipos)))
        .order_by(DocumentoVenta.id.asc())
        .first()
    )


def _factura_boleta_hija_con_pago_caja(parent_id: int | None) -> DocumentoVenta | None:
    """Factura/boleta emitida desde la OV y ya cobrada en caja (ahi la cadena queda cerrada para nuevas conversiones)."""
    child = _documento_hijo_directo(parent_id, {"factura", "boleta"})
    if child is None:
        return None
    if (child.estado_pago or "").strip().lower() != "pagado":
        return None
    return child


def _conversion_flags_for_documento(documento: DocumentoVenta) -> dict:
    """Bloqueo de conversion si hay factura/boleta hija ya cobrada en caja, o picking de bodega pendiente."""
    tipo = (documento.tipo or "").strip().lower()
    base = {
        "puede_convertir_a_orden_venta": True,
        "puede_convertir_a_factura_boleta": True,
        "documento_hijo_resumen": None,
        "documento_hijo_tipo": None,
        "picking_bloquea_facturacion": False,
        "picking_status": None,
    }
    if tipo == "cotizacion":
        hijo_ov = _documento_hijo_directo(documento.id, {"orden_venta"})
        pagado = _factura_boleta_hija_con_pago_caja(hijo_ov.id) if hijo_ov is not None else None
        if pagado is not None:
            base["puede_convertir_a_orden_venta"] = False
            base["documento_hijo_resumen"] = (pagado.numero or str(pagado.id)).strip()
            base["documento_hijo_tipo"] = (pagado.tipo or "").strip().lower()
    elif tipo == "orden_venta":
        pagado = _factura_boleta_hija_con_pago_caja(documento.id)
        if pagado is not None:
            base["puede_convertir_a_factura_boleta"] = False
            base["documento_hijo_resumen"] = (pagado.numero or str(pagado.id)).strip()
            base["documento_hijo_tipo"] = (pagado.tipo or "").strip().lower()
        else:
            pv = PickingVenta.query.filter_by(orden_venta_id=documento.id).first()
            if pv is not None:
                st = (pv.status or "").strip().lower()
                base["picking_status"] = st
                if st != "entregado":
                    base["puede_convertir_a_factura_boleta"] = False
                    base["picking_bloquea_facturacion"] = True
    return base


def _serialize_document(documento: DocumentoVenta) -> dict:
    doc_kind = (documento.tipo or "").strip().lower()
    if doc_kind in {"factura", "boleta"}:
        doc_type = "factura"
        tipo_documento = doc_kind
    else:
        doc_type = doc_kind
        tipo_documento = "factura"

    items = []
    for item in documento.items:
        cantidad = int(item.cantidad or 0)
        precio = float(item.precio_unitario or 0)
        mp = getattr(item, "margen_porcentaje", None)
        ml = getattr(item, "modelo_linea", None)
        items.append(
            {
                "codigo": (item.codigo_producto or "").strip().upper(),
                "descripcion": item.descripcion or "",
                "marca": (item.marca or "").strip().upper(),
                "bodega": (item.bodega or "").strip() or "Bodega 1",
                "origen_compra": _normalize_origen_compra(getattr(item, "origen_compra", None)),
                "cantidad": cantidad,
                "precio": precio,
                "margen": round(float(mp), 4) if mp is not None else None,
                "modelo_linea": (ml or "").strip() if ml is not None else "",
                "subtotal": round(float(item.subtotal or (cantidad * precio)), 2),
            }
        )

    subtotal = round(float(documento.subtotal or 0), 2)
    iva = round(float(documento.impuesto or 0), 2)
    total = round(float(documento.total or 0), 2)
    descuento_amt = round(float(getattr(documento, "descuento", None) or 0), 2)
    subtotal_bruto = round(subtotal + descuento_amt, 2)

    party_payload = {
        "id": (documento.proveedor_id if doc_kind in {"orden_compra", "factura_proveedor"} else documento.cliente_id)
        or 0,
        "proveedor_id": 0
        if doc_kind in {"orden_compra", "factura_proveedor"}
        else int(documento.proveedor_id or 0),
        "name": documento.cliente_nombre or "",
        "rut": format_rut(documento.cliente_rut),
        "address": documento.cliente_direccion or "",
        "telefono": format_phone_display(documento.cliente_telefono or ""),
        "email": documento.cliente_email or "",
        "region": documento.cliente_region or "",
        "ciudad": documento.cliente_ciudad or "",
        "pais": documento.cliente_pais or CHILE_COUNTRY_NAME,
    }
    if doc_kind not in {"orden_compra", "factura_proveedor"} and documento.cliente_id:
        cl = db.session.get(Cliente, documento.cliente_id)
        if cl is not None:
            party_payload["cliente_mayorista"] = bool(getattr(cl, "cliente_mayorista", False))
            party_payload["margen_descuento_pct"] = round(float(getattr(cl, "margen_descuento_pct", 0) or 0), 4)

    msf = _round_money_cl(getattr(documento, "monto_saldo_favor", None) or 0)
    payload = {
        "id": documento.id,
        "doc_type": doc_type,
        "tipo_documento": tipo_documento,
        "source_id": documento.source_id,
        "source_type": (documento.source_type or "").strip().lower(),
        "root_id": documento.root_id,
        "numero": (documento.numero or "").strip(),
        "numero_oc_cliente": (getattr(documento, "numero_oc_cliente", None) or "").strip(),
        "fecha_documento": documento.fecha_documento.strftime("%Y-%m-%d") if documento.fecha_documento else "",
        "fecha_vencimiento": documento.fecha_vencimiento.strftime("%Y-%m-%d") if documento.fecha_vencimiento else "",
        "status": (documento.status or "pendiente").strip().lower(),
        "metodo_pago": (documento.metodo_pago or "").strip(),
        "estado_pago": (documento.estado_pago or "pendiente").strip(),
        "monto_saldo_favor": msf,
        "pago_referencia": (getattr(documento, "pago_referencia", None) or "").strip(),
        "party": party_payload,
        "items": items,
        "totals": {
            "subtotal": subtotal,
            "iva": iva,
            "total": total,
            "descuento": descuento_amt,
            "subtotal_bruto": subtotal_bruto,
            "subtotal_fmt": _format_currency(subtotal),
            "iva_fmt": _format_currency(iva),
            "total_fmt": _format_currency(total),
            "descuento_fmt": _format_currency(descuento_amt),
            "subtotal_bruto_fmt": _format_currency(subtotal_bruto),
        },
        "notes": documento.observacion or "",
    }
    payload.update(_conversion_flags_for_documento(documento))
    return payload


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


def _adjust_product_stock(
    codigo: str,
    marca: str,
    bodega: str,
    origen_compra: str,
    delta: int,
    reason: str,
    resolved_ref: dict | None = None,
) -> str | None:
    if not codigo or delta == 0:
        return None

    code = codigo.strip().upper()
    orig_marca_empty = not (marca or "").strip()
    orig_origen_empty = not (origen_compra or "").strip()
    brand = (marca or "").strip().upper()
    warehouse = (bodega or "").strip() or "Bodega 1"
    origin = _normalize_origen_compra(origen_compra)

    variant = (
        db.session.query(ProductoVarianteStock)
        .filter_by(codigo_producto=code, marca=brand, bodega=warehouse, origen_compra=origin)
        .first()
    )
    # Documento sin marca en linea (cotizacion/OV antigua) pero stock es por variante: resolver codigo+bodega.
    if variant is None and delta < 0 and not brand:
        need = abs(int(delta))
        rows_wh = (
            db.session.query(ProductoVarianteStock)
            .filter_by(codigo_producto=code, bodega=warehouse)
            .order_by(ProductoVarianteStock.stock.desc())
            .all()
        )
        for row in rows_wh:
            if int(row.stock or 0) >= need:
                variant = row
                brand = (row.marca or "").strip().upper()
                origin = _normalize_origen_compra(getattr(row, "origen_compra", None))
                break
        if variant is None and rows_wh:
            total_wh = sum(int(r.stock or 0) for r in rows_wh)
            return (
                f"Stock insuficiente para {code} en {warehouse} (marca no indicada en documento). "
                f"Disponible total: {total_wh}, requerido: {need}."
            )
        if variant is None:
            rows_any = (
                db.session.query(ProductoVarianteStock)
                .filter_by(codigo_producto=code)
                .order_by(ProductoVarianteStock.stock.desc())
                .all()
            )
            for row in rows_any:
                if int(row.stock or 0) >= need:
                    variant = row
                    brand = (row.marca or "").strip().upper()
                    warehouse = (row.bodega or "").strip() or warehouse
                    origin = _normalize_origen_compra(getattr(row, "origen_compra", None))
                    break
            if variant is None and rows_any:
                total_any = sum(int(r.stock or 0) for r in rows_any)
                return (
                    f"Stock insuficiente para {code} (marca no indicada; bodega del documento distinta al stock). "
                    f"Disponible total en sistema: {total_any}, requerido: {need}."
                )

    if variant is None:
        if delta < 0 and orig_origen_empty:
            by_any_origin = (
                db.session.query(ProductoVarianteStock)
                .filter_by(codigo_producto=code, marca=brand, bodega=warehouse)
                .order_by(ProductoVarianteStock.stock.desc())
                .all()
            )
            for row in by_any_origin:
                if int(row.stock or 0) >= abs(int(delta)):
                    variant = row
                    origin = _normalize_origen_compra(getattr(row, "origen_compra", None))
                    break
        if delta < 0:
            return f"No existe variante {code}/{brand} en {warehouse} ({origin})."
        variant = ProductoVarianteStock(
            codigo_producto=code,
            marca=brand,
            bodega=warehouse,
            origen_compra=origin,
            stock=0,
        )
        db.session.add(variant)
        db.session.flush()

    current_stock = int(variant.stock or 0)
    next_stock = current_stock + int(delta)
    if next_stock < 0:
        return f"Stock insuficiente para {code}/{brand} en {warehouse} ({origin}). Disponible: {current_stock}."

    variant.stock = next_stock
    db.session.add(
        MovimientoStock(
            codigo_producto=code,
            tipo="ingreso" if delta > 0 else "salida",
            cantidad=int(delta),
            usuario=session.get("user") or "sistema",
            marca=brand,
            bodega=warehouse,
            origen_compra=origin,
            observacion=reason,
        )
    )

    if resolved_ref is not None and (orig_marca_empty or orig_origen_empty) and brand:
        resolved_ref["marca"] = brand
        resolved_ref["bodega"] = warehouse
        resolved_ref["origen_compra"] = origin

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
        resolved: dict = {}
        error = _adjust_product_stock(
            codigo=(item.codigo_producto or "").strip().upper(),
            marca=(item.marca or "").strip().upper(),
            bodega=(item.bodega or "").strip() or "Bodega 1",
            origen_compra=(getattr(item, "origen_compra", None) or "").strip(),
            delta=delta_sign * qty,
            reason=reason,
            resolved_ref=resolved,
        )
        if not error and resolved.get("marca"):
            item.marca = resolved["marca"]
            item.bodega = resolved.get("bodega") or item.bodega
            item.origen_compra = resolved.get("origen_compra") or getattr(item, "origen_compra", ORIGEN_COMPRA_DEFAULT)
        if error:
            errors.append(error)

    if errors:
        return False, errors

    documento.stock_deducted = True
    return True, []


def _serialize_chain_node(doc: DocumentoVenta) -> dict:
    tipo = (doc.tipo or "").strip().lower()
    metodo = (doc.metodo_pago or "").strip().lower()
    ep = (doc.estado_pago or "pendiente").strip().lower()
    ref = (getattr(doc, "pago_referencia", None) or "").strip()
    return {
        "id": doc.id,
        "type": tipo,
        "number": (doc.numero or "").strip(),
        "numero_oc_cliente": (getattr(doc, "numero_oc_cliente", None) or "").strip(),
        "status": (doc.status or "pendiente").strip().lower(),
        "total": round(float(doc.total or 0), 2),
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
        "source_id": doc.source_id,
        "source_type": (doc.source_type or "").strip().lower(),
        "root_id": doc.root_id,
        "estado_pago": ep,
        "metodo_pago": metodo,
        "metodo_pago_label": METODO_PAGO_LABELS.get(metodo, metodo) if metodo else "",
        "pago_referencia": ref,
    }


def _history_doc_label(doc_type: str) -> str:
    labels = {
        "cotizacion": "Cotizacion",
        "orden_venta": "Orden de Venta",
        "orden_compra": "Orden de Compra",
        "factura": "Factura",
        "boleta": "Boleta",
        "factura_proveedor": "Factura proveedor",
        "ingreso": "Ingreso de stock",
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
        "ingreso": "orange",
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
        "ingreso": "bodega.movimientos",
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
        "numero_oc_cliente": (node.get("numero_oc_cliente") or "").strip(),
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
                "estado_pago": "",
                "metodo_pago": "",
                "metodo_pago_label": "",
                "pago_referencia": "",
            }
        )

    return nodes


def _build_client_history_payload(client_id: int, solo_con_oc: bool = False) -> tuple[Cliente | None, dict | None]:
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
    if solo_con_oc:
        documentos = [d for d in documentos if (d.get("numero_oc_cliente") or "").strip()]
    timeline = sorted(documentos, key=lambda item: item.get("created_at") or "", reverse=False)

    saldo_actual = _round_money_cl(_cliente_saldo_favor_ledger(int(cliente.id)))
    movimientos_saldo = (
        ClienteSaldoFavorMovimiento.query.filter_by(cliente_id=cliente.id)
        .order_by(ClienteSaldoFavorMovimiento.created_at.desc(), ClienteSaldoFavorMovimiento.id.desc())
        .limit(80)
        .all()
    )
    saldo_movimientos = [
        {
            "id": m.id,
            "monto": m.monto,
            "tipo": m.tipo or "",
            "ref_factura": m.ref_factura_numero or "",
            "ref_nc": m.ref_nota_credito_numero or "",
            "razon": (m.razon or "")[:500],
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in movimientos_saldo
    ]
    facturas_pago = []
    for d in docs:
        if (d.tipo or "").strip().lower() not in ("factura", "boleta"):
            continue
        facturas_pago.append(
            {
                "id": d.id,
                "tipo": d.tipo,
                "numero": d.numero or f"#{d.id}",
                "total": float(d.total or 0),
                "estado_pago": (d.estado_pago or "pendiente").strip().lower(),
                "metodo_pago": (d.metodo_pago or "").strip(),
                "metodo_label": METODO_PAGO_LABELS.get((d.metodo_pago or "").strip().lower(), d.metodo_pago or "—"),
                "monto_saldo_favor": _round_money_cl(getattr(d, "monto_saldo_favor", 0) or 0),
                "fecha": d.fecha_documento.strftime("%d-%m-%Y") if d.fecha_documento else "—",
            }
        )

    try:
        from app.oc_clientes.services import listar_oc_por_cliente

        ordenes_compra_cliente = listar_oc_por_cliente(client_id)
    except Exception:
        ordenes_compra_cliente = []

    payload = {
        "client": cliente.to_dict(),
        "saldo_favor": saldo_actual,
        "saldo_movimientos": saldo_movimientos,
        "facturas_pago": facturas_pago,
        "cotizaciones": cotizaciones,
        "ordenes_venta": ordenes_venta,
        "facturas": facturas,
        "notas_credito": notas_credito,
        "ordenes_compra_cliente": ordenes_compra_cliente,
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
    pagos = []
    total_pagado = 0.0
    total_pendiente = 0.0
    for node in docs:
        item = _serialize_chain_node(node)
        estado_pago = (item.get("estado_pago") or "pendiente").strip().lower()
        total_doc = round(float(item.get("total") or 0), 2)
        pagos.append(
            {
                "id": item.get("id"),
                "type": item.get("type"),
                "label": _history_doc_label(item.get("type") or ""),
                "number": item.get("number") or (f"#{item.get('id')}" if item.get("id") else ""),
                "fecha": (item.get("created_at") or "")[:10] if item.get("created_at") else "-",
                "estado_pago": estado_pago or "pendiente",
                "metodo_pago": item.get("metodo_pago_label") or "",
                "referencia": item.get("pago_referencia") or "",
                "total": total_doc,
            }
        )
        if estado_pago == "pagado":
            total_pagado += total_doc
        else:
            total_pendiente += total_doc

    # Ingresos de stock de bodega vinculados a este proveedor (por id o por RUT).
    proveedor_rut_norm = clean_rut(proveedor.rut or "")
    ingresos_query = db.session.query(IngresoDocumento).filter(
        or_(
            IngresoDocumento.proveedor_id == supplier_id,
            IngresoDocumento.proveedor_rut == proveedor_rut_norm,
        )
    )
    ingresos = ingresos_query.order_by(IngresoDocumento.created_at.desc(), IngresoDocumento.id.desc()).all()
    for ing in ingresos:
        created_iso = ing.created_at.isoformat() if ing.created_at else None
        total_neto = (
            db.session.query(func.sum(IngresoDocumentoItem.valor_neto))
            .filter(IngresoDocumentoItem.ingreso_documento_id == ing.id)
            .scalar()
            or 0
        )
        numero = (ing.numero_documento or "").strip() or f"ING-{ing.id}"
        documentos.append(
            {
                "id": ing.id,
                "type": "ingreso",
                "label": _history_doc_label("ingreso"),
                "badge_tone": _history_doc_tone("ingreso"),
                "number": numero,
                "status": "registrado",
                "total": round(float(total_neto or 0), 2),
                "created_at": created_iso,
                "fecha": created_iso[:10] if created_iso else "-",
                "view_url": url_for("bodega.movimientos"),
                "source_id": None,
                "root_id": None,
            }
        )

    homologaciones: list[dict] = []
    if proveedor_rut_norm:
        map_rows = (
            ProveedorCodigoInterno.query.filter_by(proveedor_rut=proveedor_rut_norm)
            .order_by(
                ProveedorCodigoInterno.codigo_interno.asc(),
                ProveedorCodigoInterno.codigo_proveedor.asc(),
            )
            .all()
        )
        for m in map_rows:
            ci = (m.codigo_interno or "").strip().upper()
            cp = (m.codigo_proveedor or "").strip()
            upd = m.updated_at.isoformat() if m.updated_at else None
            homologaciones.append(
                {
                    "codigo_proveedor": cp,
                    "codigo_interno": ci,
                    "updated_at": upd,
                    "updated_at_label": upd[:10] if upd else "—",
                    "producto_buscar_url": url_for("productos.buscar", q=ci) if ci else None,
                }
            )

    payload = {
        "supplier": proveedor.to_dict(),
        "ordenes_compra": ordenes_compra,
        "facturas_proveedor": facturas_proveedor,
        "pagos": pagos,
        "pagos_total_pagado": round(total_pagado, 2),
        "pagos_total_pendiente": round(total_pendiente, 2),
        "documentos": sorted(documentos, key=lambda item: item.get("created_at") or "", reverse=True),
        "timeline": sorted(documentos, key=lambda item: item.get("created_at") or "", reverse=False),
        "homologaciones": homologaciones,
    }
    return proveedor, payload


def _copy_document_with_trace(source: DocumentoVenta, target_doc_type: str, target_tipo_documento: str = "factura") -> DocumentoVenta:
    if target_doc_type == "factura":
        target_number = _next_doc_number(_sales_doc_prefix(target_tipo_documento))
    else:
        target_number = _next_doc_number(_doc_prefix(target_doc_type))
    source_doc_type = (source.tipo or "").strip().lower()

    selected_client_id = int(source.cliente_id or 0)
    selected_proveedor_id = int(source.proveedor_id or 0)

    # Venta: cliente y proveedor son excluyentes; venta a proveedor conserva proveedor_id.
    if target_doc_type in {"factura", "orden_venta", "cotizacion"}:
        if selected_client_id > 0:
            selected_proveedor_id = 0
    if target_doc_type in {"orden_compra", "factura_proveedor"}:
        if selected_proveedor_id > 0:
            selected_client_id = 0

    selected_party = (
        _client_by_id(selected_client_id)
        if selected_client_id > 0
        else _proveedor_by_id(selected_proveedor_id)
    )

    party = {
        "name": source.cliente_nombre or "",
        "rut": format_rut(source.cliente_rut),
        "address": source.cliente_direccion or "",
        "telefono": format_phone_display(source.cliente_telefono or ""),
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
            "origen_compra": _normalize_origen_compra(getattr(item, "origen_compra", None)),
            "margen": (
                round(float(item.margen_porcentaje), 4)
                if getattr(item, "margen_porcentaje", None) is not None
                else None
            ),
            "modelo_linea": ((getattr(item, "modelo_linea", None) or "") or "").strip(),
            "subtotal": round(float(item.subtotal or 0), 2),
        }
        for item in source.items
    ]
    totals = {
        "subtotal": round(float(source.subtotal or 0), 2),
        "iva": round(float(source.impuesto or 0), 2),
        "total": round(float(source.total or 0), 2),
        "descuento_monto": round(float(getattr(source, "descuento", None) or 0), 2),
        "subtotal_bruto": round(
            float(source.subtotal or 0) + float(getattr(source, "descuento", None) or 0), 2
        ),
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
        numero_oc_cliente=(getattr(source, "numero_oc_cliente", None) or "").strip(),
    )
    _mark_source_aprobada_after_conversion(source)
    return target


def _mark_source_aprobada_after_conversion(source: DocumentoVenta | None) -> None:
    """Al generar el siguiente documento (cotización→OV, OV→factura/boleta, etc.), el origen queda aprobado."""
    if source is None:
        return
    st = (source.status or "pendiente").strip().lower()
    if st in ("anulada", "entregada"):
        return
    source.status = "aprobada"
    source.updated_at = datetime.utcnow()


def _mark_documento_aprobada_por_cobro(doc: DocumentoVenta) -> None:
    """Factura/boleta cobrada en caja: estado de documento aprobado si no está entregada o anulada."""
    if (doc.tipo or "").strip().lower() not in {"factura", "boleta"}:
        return
    st = (doc.status or "pendiente").strip().lower()
    if st in ("anulada", "entregada"):
        return
    doc.status = "aprobada"


def _cascade_upstream_aprobada(doc: DocumentoVenta) -> None:
    """Cotización y orden de venta anteriores quedan aprobados al cobrar la factura o boleta final."""
    if (doc.tipo or "").strip().lower() not in {"factura", "boleta"}:
        return
    current_id = doc.source_id
    seen: set[int] = set()
    while current_id and current_id not in seen:
        seen.add(current_id)
        parent = db.session.get(DocumentoVenta, current_id)
        if parent is None:
            break
        ptipo = (parent.tipo or "").strip().lower()
        if ptipo in {"cotizacion", "orden_venta", "factura", "boleta"}:
            pst = (parent.status or "pendiente").strip().lower()
            if pst not in ("anulada", "entregada"):
                parent.status = "aprobada"
                parent.updated_at = datetime.utcnow()
        current_id = parent.source_id


def _product_by_code(codigo: str):
    q = text(f"""
        SELECT CODIGO AS codigo, DESCRIPCION AS descripcion,
               MODELO AS modelo, COALESCE([CODIGO OEM], '') AS codigo_oem,
               {_SQL_PRECIO_LISTA} AS precio,
               COALESCE(STOCK_10JUL, 0) AS stock
        FROM productos
        WHERE COALESCE(ACTIVO, 1) = 1
          AND (
            UPPER(CODIGO) = :codigo
            OR UPPER(TRIM(COALESCE([CODIGO OEM], ''))) = :codigo
          )
        ORDER BY
            CASE WHEN UPPER(CODIGO) = :codigo THEN 0 ELSE 1 END,
            COALESCE(STOCK_10JUL, 0) DESC,
            CODIGO ASC
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
        .order_by(
            ProductoVarianteStock.marca.asc(),
            ProductoVarianteStock.bodega.asc(),
            ProductoVarianteStock.origen_compra.asc(),
        )
        .all()
    )
    return [
        {
            "id": row.id,
            "marca": row.marca or "",
            "bodega": row.bodega or "",
            "origen_compra": _normalize_origen_compra(getattr(row, "origen_compra", None)),
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
            ProductoVarianteStock.origen_compra.asc(),
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
                "origen_compra": _normalize_origen_compra(getattr(row, "origen_compra", None)),
                "stock": int(row.stock or 0),
                "proveedor": row.proveedor or "",
            }
        )
    return variant_map


def _ultimo_ingreso_ref(codigo: str, marca: str | None, bodega: str | None, origen_compra: str | None = None) -> dict | None:
    """
    Referencia comercial para ventas basada en ingresos ERP.

    - costo_unitario_neto: promedio ponderado por cantidad de todos los ingresos
      que coinciden con código + marca + bodega.
    - precio_sugerido_neto / margen_registrado_pct: se toma de la última línea
      ingresada para esa combinación (si existe), y si no hay P. venta explícito,
      se infiere desde costo promedio y margen.
    """
    code = (codigo or "").strip().upper()
    if not code:
        return None
    marca_n = (marca or "").strip().upper()
    bodega_n = (bodega or "").strip() or "Bodega 1"
    origen_n = _normalize_origen_compra(origen_compra)

    q = (
        db.session.query(IngresoDocumentoItem)
        .join(IngresoDocumento, IngresoDocumento.id == IngresoDocumentoItem.ingreso_documento_id)
        .filter(func.upper(IngresoDocumentoItem.codigo_producto) == code)
        .filter(IngresoDocumentoItem.bodega == bodega_n)
        .filter(IngresoDocumentoItem.origen_compra == origen_n)
    )
    if marca_n:
        q = q.filter(func.upper(func.trim(IngresoDocumentoItem.marca)) == marca_n)
    else:
        q = q.filter(
            or_(
                IngresoDocumentoItem.marca.is_(None),
                IngresoDocumentoItem.marca == "",
                func.upper(func.trim(IngresoDocumentoItem.marca)) == "",
            )
        )

    rows = q.all()
    if not rows:
        v = (
            db.session.query(ProductoVarianteStock)
            .filter_by(
                codigo_producto=code,
                marca=marca_n,
                bodega=bodega_n,
                origen_compra=origen_n,
            )
            .first()
        )
        om = getattr(v, "margen_override_pct", None) if v else None
        op = getattr(v, "precio_publico_neto_override", None) if v else None
        if om is None and op is None:
            return None
        return merge_ingreso_ref_variante_overrides(None, om, op)

    total_qty = 0
    total_vn = 0.0
    for it in rows:
        qty = int(it.cantidad or 0)
        vn = float(it.valor_neto or 0)
        if qty > 0 and vn > 0:
            total_qty += qty
            total_vn += vn
    costo_u = (total_vn / total_qty) if total_qty > 0 and total_vn > 0 else None

    item = q.order_by(IngresoDocumento.created_at.desc(), IngresoDocumentoItem.id.desc()).first()
    pv = item.precio_venta_neto if item is not None else None
    mg = item.margen_pct if item is not None else None
    precio_sug: float | None = None
    if pv is not None:
        precio_sug = round(float(pv), 2)
    elif costo_u is not None and mg is not None and float(mg) < 100:
        denom = 1.0 - float(mg) / 100.0
        if denom > 0:
            precio_sug = round(costo_u / denom, 2)

    ref = {
        "costo_unitario_neto": round(costo_u, 2) if costo_u is not None else None,
        "precio_sugerido_neto": precio_sug,
        "margen_registrado_pct": float(mg) if mg is not None else None,
        "origen_compra": origen_n,
    }
    v = (
        db.session.query(ProductoVarianteStock)
        .filter_by(
            codigo_producto=code,
            marca=marca_n,
            bodega=bodega_n,
            origen_compra=origen_n,
        )
        .first()
    )
    om = getattr(v, "margen_override_pct", None) if v else None
    op = getattr(v, "precio_publico_neto_override", None) if v else None
    return merge_ingreso_ref_variante_overrides(ref, om, op)


def _fill_precio_desde_ingreso_si_vacio(payload: dict) -> None:
    """Si catálogo no tiene precio de lista, usar precio sugerido del último ingreso (misma variante)."""
    try:
        p = float(payload.get("precio") or 0)
    except (TypeError, ValueError):
        p = 0.0
    if p > 0:
        return
    ref = payload.get("ingreso_ref")
    if not isinstance(ref, dict):
        return
    psn = ref.get("precio_sugerido_neto")
    if psn is None:
        return
    try:
        v = float(psn)
        if v > 0:
            payload["precio"] = round(v, 2)
    except (TypeError, ValueError):
        pass


def _serialize_product(producto, codigo: str | None = None, variantes: list[dict] | None = None) -> dict:
    code = (producto.get("codigo") or codigo or "").strip().upper()
    variant_rows = list(variantes if variantes is not None else _product_variants_by_code(code))

    stock_entries = [
        {
            "marca": (variant.get("marca") or "").strip().upper(),
            "bodega": (variant.get("bodega") or "").strip() or "Bodega 1",
            "origen_compra": _normalize_origen_compra(variant.get("origen_compra")),
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
                "origen_compra": ORIGEN_COMPRA_DEFAULT,
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
        "default_origen_compra": _normalize_origen_compra(default_entry.get("origen_compra")),
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

    search_select = f"""
            CODIGO AS codigo,
            COALESCE(DESCRIPCION, '') AS descripcion,
            COALESCE(MODELO, '') AS modelo,
            COALESCE(MARCA, '') AS marca,
            COALESCE([CODIGO OEM], '') AS codigo_oem,
            COALESCE([CODIGO ALTERNATIVO O ANTIGUO], '') AS codigo_alternativo,
            COALESCE([HOMOLOGADOS], '') AS homologados,
            {_SQL_PRECIO_LISTA} AS precio,
            COALESCE(STOCK_10JUL, 0) AS stock
    """
    search_like_extra = """
            OR UPPER(COALESCE([CODIGO ALTERNATIVO O ANTIGUO], '')) LIKE UPPER(:like)
            OR UPPER(COALESCE([HOMOLOGADOS], '')) LIKE UPPER(:like)
    """

    query = text(
        f"""
        SELECT
            {search_select}
        FROM productos
        WHERE COALESCE(ACTIVO, 1) = 1
          AND (
            UPPER(CODIGO) LIKE UPPER(:like)
            OR UPPER(COALESCE([CODIGO OEM], '')) LIKE UPPER(:like)
            OR UPPER(COALESCE(DESCRIPCION, '')) LIKE UPPER(:like)
            {search_like_extra}
          )
        ORDER BY
            CASE
                WHEN UPPER(COALESCE([CODIGO OEM], '')) LIKE UPPER(:starts) THEN 0
                WHEN UPPER(COALESCE([CODIGO ALTERNATIVO O ANTIGUO], '')) LIKE UPPER(:like) THEN 1
                WHEN UPPER(COALESCE([HOMOLOGADOS], '')) LIKE UPPER(:like) THEN 2
                WHEN :is_numeric = 1 AND UPPER(CODIGO) LIKE UPPER(:starts) THEN 3
                WHEN :is_numeric = 1 AND UPPER(COALESCE(DESCRIPCION, '')) LIKE UPPER(:starts) THEN 4
                WHEN :is_numeric = 0 AND UPPER(COALESCE(DESCRIPCION, '')) LIKE UPPER(:starts) THEN 3
                WHEN :is_numeric = 0 AND UPPER(CODIGO) LIKE UPPER(:starts) THEN 4
                ELSE 5
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

    # Fallback "SAP-like": búsqueda por tokens + ranking flexible (tolerante a orden y variaciones).
    if not rows:
        tokens = [t.strip().upper() for t in search_term.split() if t.strip()]
        if len(tokens) >= 1:
            stop_tokens = {"DE", "DEL", "LA", "EL", "LOS", "LAS", "Y", "PARA", "CON", "SIN"}
            sig_tokens = [t for t in tokens if t not in stop_tokens]
            token_source = sig_tokens or tokens
            params = {"limit": safe_limit * 12}
            token_predicates_any = []
            token_predicates_all = []
            for idx, tok in enumerate(token_source):
                key = f"tk{idx}"
                params[key] = f"%{tok}%"
                pred = (
                    f"""(
                        UPPER(CODIGO) LIKE UPPER(:{key})
                        OR UPPER(COALESCE([CODIGO OEM], '')) LIKE UPPER(:{key})
                        OR UPPER(COALESCE(DESCRIPCION, '')) LIKE UPPER(:{key})
                        OR UPPER(COALESCE(MARCA, '')) LIKE UPPER(:{key})
                        OR UPPER(COALESCE(MODELO, '')) LIKE UPPER(:{key})
                        OR UPPER(COALESCE([CODIGO ALTERNATIVO O ANTIGUO], '')) LIKE UPPER(:{key})
                        OR UPPER(COALESCE([HOMOLOGADOS], '')) LIKE UPPER(:{key})
                    )"""
                )
                token_predicates_any.append(pred)
                token_predicates_all.append(pred)

            all_match = " AND ".join(token_predicates_all) if token_predicates_all else "1=1"
            strict_query = text(
                f"""
                SELECT
                    {search_select}
                FROM productos
                WHERE COALESCE(ACTIVO, 1) = 1
                  AND ({all_match})
                ORDER BY LENGTH(CODIGO) ASC, CODIGO ASC
                LIMIT :limit
                """
            )
            strict_rows = db.session.execute(strict_query, params).mappings().all()

            if strict_rows:
                candidate_rows = strict_rows
            else:
                any_match = " OR ".join(token_predicates_any) if token_predicates_any else "1=1"
                fallback_query = text(
                    f"""
                    SELECT
                        {search_select}
                    FROM productos
                    WHERE COALESCE(ACTIVO, 1) = 1
                      AND ({any_match})
                    ORDER BY LENGTH(CODIGO) ASC, CODIGO ASC
                    LIMIT :limit
                    """
                )
                candidate_rows = db.session.execute(fallback_query, params).mappings().all()
            phrase = search_term.upper()

            def _best_token_fuzzy(token: str, words: list[str]) -> float:
                best = 0.0
                for w in words:
                    if not w:
                        continue
                    ratio = SequenceMatcher(None, token, w).ratio()
                    if ratio > best:
                        best = ratio
                return best

            def _score_candidate(r):
                code = (r.get("codigo") or "").strip().upper()
                oem = (r.get("codigo_oem") or "").strip().upper()
                alt = (r.get("codigo_alternativo") or "").strip().upper()
                hom = (r.get("homologados") or "").strip().upper()
                desc = (r.get("descripcion") or "").strip().upper()
                marca = (r.get("marca") or "").strip().upper()
                modelo = (r.get("modelo") or "").strip().upper()
                blob = f"{code} {oem} {alt} {hom} {desc} {marca} {modelo}"
                words = [w for w in blob.split() if w]
                score = 0.0
                if phrase and phrase in blob:
                    score += 10.0
                for tk in token_source:
                    if tk in blob:
                        score += 3.0
                        if code.startswith(tk):
                            score += 2.0
                        if oem.startswith(tk):
                            score += 2.0
                        if alt and tk in alt:
                            score += 2.5
                        if hom and tk in hom:
                            score += 2.0
                    elif len(tk) >= 3:
                        fuzzy = _best_token_fuzzy(tk, words)
                        if fuzzy >= 0.84:
                            score += 1.8
                        elif fuzzy >= 0.74:
                            score += 0.9
                return (-score, len(code), code)

            ranked = sorted(candidate_rows, key=_score_candidate)
            rows = ranked[:safe_limit]

    codes = [(row.get("codigo") or "").strip().upper() for row in rows if row.get("codigo")]
    variant_map = _product_variants_map(codes)

    results = []
    for row in rows:
        code = (row.get("codigo") or "").strip().upper()
        payload = _serialize_product(row, codigo=code, variantes=variant_map.get(code, []))
        entries = payload.get("stock_entries") or []
        if entries:
            for entry in entries:
                item_payload = {
                    **payload,
                    "marca": entry.get("marca") or "",
                    "bodega": entry.get("bodega") or "Bodega 1",
                    "origen_compra": _normalize_origen_compra(entry.get("origen_compra")),
                    "variant_stock": int(entry.get("stock") or 0),
                }
                item_payload["ingreso_ref"] = _ultimo_ingreso_ref(
                    code,
                    item_payload.get("marca"),
                    item_payload.get("bodega"),
                    item_payload.get("origen_compra"),
                )
                _fill_precio_desde_ingreso_si_vacio(item_payload)
                results.append(item_payload)
        else:
            item_payload = {
                **payload,
                "marca": "",
                "bodega": "Bodega 1",
                "origen_compra": ORIGEN_COMPRA_DEFAULT,
                "variant_stock": int(payload.get("stock") or 0),
            }
            item_payload["ingreso_ref"] = _ultimo_ingreso_ref(
                code,
                item_payload.get("marca"),
                item_payload.get("bodega"),
                item_payload.get("origen_compra"),
            )
            _fill_precio_desde_ingreso_si_vacio(item_payload)
            results.append(item_payload)
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
        origen_raw = (item.get("origen_compra") or "").strip().lower()
        origen_compra = _normalize_origen_compra(origen_raw)

        variants = _product_variants_by_code(codigo)
        if variants:
            if not marca:
                errors.append(f"El item {codigo} requiere seleccionar marca/variante.")
                continue

            same_variant_rows = (
                db.session.query(ProductoVarianteStock)
                .filter_by(codigo_producto=codigo, marca=marca, bodega=bodega)
                .order_by(ProductoVarianteStock.origen_compra.asc())
                .all()
            )
            origins_available = sorted({
                _normalize_origen_compra(getattr(row, "origen_compra", None))
                for row in same_variant_rows
            })
            if not origen_raw and len(origins_available) == 1:
                origen_compra = origins_available[0]
            if len(origins_available) > 1 and not origen_raw:
                errors.append(
                    f"El item {codigo} / {marca} en {bodega} requiere seleccionar origen de compra."
                )
                continue

            variante = (
                db.session.query(ProductoVarianteStock)
                .filter_by(
                    codigo_producto=codigo,
                    marca=marca,
                    bodega=bodega,
                    origen_compra=origen_compra,
                )
                .first()
            )
            if variante is None:
                errors.append(
                    f"La variante {codigo} / {marca} en {bodega} ({origen_compra}) no existe."
                )
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
                    origen_compra=origen_compra,
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
                    origen_compra=origen_compra,
                observacion=f"Venta {doc_number}",
            )
        )

    return errors


def _origin_selection_errors(items: list[dict]) -> list[str]:
    errors: list[str] = []
    for item in items:
        codigo = (item.get("codigo") or "").strip().upper()
        marca = (item.get("marca") or "").strip().upper()
        bodega = (item.get("bodega") or "").strip() or "Bodega 1"
        origen = (item.get("origen_compra") or "").strip().lower()
        if not codigo or not marca:
            continue
        rows = (
            db.session.query(ProductoVarianteStock.origen_compra)
            .filter_by(codigo_producto=codigo, marca=marca, bodega=bodega)
            .distinct()
            .all()
        )
        origins = sorted({
            _normalize_origen_compra(r[0])
            for r in rows
        })
        if len(origins) > 1 and not origen:
            errors.append(
                f"Debes seleccionar origen para {codigo} / {marca} en {bodega}."
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


def _sii_contribuyente_user_can_lookup() -> bool:
    """Quién puede consultar datos SII al crear/editar terceros en Ventas."""
    user = session.get("user")
    rol = session.get("rol")
    return (
        has_permission(user, rol, "sii_ver")
        or has_permission(user, rol, "mod_ventas")
        or has_permission(user, rol, "ventas_guardar_documento")
    )


def _sii_contribuyente_form_ctx() -> dict:
    """Contexto base para autocompletar RUT desde SII."""
    from app.sii_sync.sii_service import SIIService

    svc = SIIService()
    api_ready = svc.contribuyente_lookup_ready()
    enabled = api_ready and _sii_contribuyente_user_can_lookup()
    return {
        "sii_contribuyente_lookup_available": api_ready,
        "sii_contribuyente_enabled": enabled,
        "sii_contribuyente_api_url": url_for("sii_sync.api_contribuyente"),
    }


def _recent_parties_sii_preload(party: str, limit: int = 20) -> list[dict]:
    """Precarga datos locales (sin SII) para sessionStorage del autocompletado."""
    from app.utils.rut_utils import clean_rut, format_rut

    rows: list = []
    if party == "proveedores":
        rows = (
            Proveedor.query.filter_by(activo=True)
            .filter(Proveedor.rut != "", Proveedor.rut.isnot(None))
            .order_by(Proveedor.created_at.desc())
            .limit(limit)
            .all()
        )
    else:
        rows = (
            Cliente.query.filter_by(activo=True)
            .filter(Cliente.rut != "", Cliente.rut.isnot(None))
            .order_by(Cliente.created_at.desc())
            .limit(limit)
            .all()
        )

    preload: list[dict] = []
    for row in rows:
        rut_raw = (row.rut or "").strip()
        if len(clean_rut(rut_raw)) < 8:
            continue
        nombre = (
            (getattr(row, "empresa", None) or "").strip()
            or (row.nombre or "").strip()
        )
        preload.append(
            {
                "rut": format_rut(rut_raw) or rut_raw,
                "razon_social": nombre,
                "giro": (row.giro or "").strip(),
                "direccion": (row.direccion or "").strip(),
                "comuna": (row.comuna or "").strip(),
                "region": (row.region or "").strip(),
                "estado_sii": "REGISTRO LOCAL",
            }
        )
    return preload


def _sii_contribuyente_config_payload(
    *profiles: dict, party_preload: str = "clientes"
) -> dict:
    """Añade sii_contribuyente_config con perfiles de campos (página o panel inline)."""
    ctx = _sii_contribuyente_form_ctx()
    config: dict = {
        "enabled": ctx["sii_contribuyente_enabled"],
        "apiUrl": ctx["sii_contribuyente_api_url"],
        "profiles": list(profiles),
    }
    if party_preload in ("clientes", "proveedores"):
        config["partiesPreload"] = _recent_parties_sii_preload(party_preload)
    ctx["sii_contribuyente_config"] = config
    return ctx


def _sii_profile_cliente_form() -> dict:
    return {
        "rutInputId": "rut",
        "nombreFieldId": "nombre",
        "giroFieldId": "giro",
        "direccionFieldId": "direccion",
        "regionFieldId": "region",
        "comunaFieldId": "comuna",
        "ciudadFieldId": "ciudad",
        "locationMode": "chile_geo",
        "badgeId": "siiRutBadge",
        "spinnerId": "siiRutSpinner",
    }


def _sii_profile_proveedor_form() -> dict:
    return {
        "rutInputId": "rut",
        "nombreFieldId": "empresa",
        "giroFieldId": "giro",
        "direccionFieldId": "direccion",
        "regionFieldId": "region",
        "comunaFieldId": "comuna",
        "ciudadFieldId": "ciudad",
        "locationMode": "chile_geo",
        "badgeId": "siiRutBadge",
        "spinnerId": "siiRutSpinner",
    }


def _sii_profile_inline_party(is_supplier_doc: bool) -> dict:
    """Panel «Crear nuevo cliente/proveedor» en cotización / OV / factura."""
    profile = {
        "rutInputId": "ic_rut",
        "giroFieldId": "ic_giro",
        "direccionFieldId": "ic_direccion",
        "regionFieldId": "ic_region",
        "comunaFieldId": "ic_ciudad",
        "ciudadFieldId": "ic_ciudad",
        "locationMode": "inline_city",
        "badgeId": "siiIcRutBadge",
        "spinnerId": "siiIcRutSpinner",
    }
    if is_supplier_doc:
        profile["empresaFieldId"] = "ic_empresa"
        profile["nombreFieldId"] = "ic_nombre"
    else:
        profile["nombreFieldId"] = "ic_nombre"
    return profile


def _parse_bool_flag(value) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    return s in ("1", "true", "on", "yes", "si", "y")


def _parse_margen_descuento_pct(value) -> float:
    if value is None or value == "":
        return 0.0
    try:
        x = float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(100.0, x))


def _is_metropolitana_region(region: str) -> bool:
    r = (region or "").strip().lower()
    return "metropolitana" in r


def _cliente_form_data(source=None) -> dict:
    source = source or {}
    pais = _normalize_country(source.get("pais"), default=CHILE_COUNTRY_NAME)
    region = _clean_text(source.get("region") or source.get("region_text"))
    comuna = _clean_text(source.get("comuna") or source.get("comuna_text"))
    ciudad = _clean_text(source.get("ciudad"))
    if _is_chile_country(pais) and comuna and not ciudad:
        ciudad = "Santiago" if _is_metropolitana_region(region) else comuna
    ciudad = _upper_text(ciudad)
    return {
        "nombre": _upper_text(source.get("nombre")),
        "rut": clean_rut(source.get("rut")),
        "giro": _upper_text(source.get("giro")),
        "direccion": _upper_text(source.get("direccion")),
        "region": region,
        "comuna": comuna,
        "ciudad": ciudad,
        "pais": pais,
        "telefono": phone_to_compact_e164(_clean_text(source.get("telefono")), pais),
        "email": _normalize_party_email(source.get("email")),
        "cliente_mayorista": _parse_bool_flag(source.get("cliente_mayorista")),
        "margen_descuento_pct": _parse_margen_descuento_pct(source.get("margen_descuento_pct")),
    }


def _proveedor_form_data(source=None) -> dict:
    source = source or {}
    pais = _normalize_country(source.get("pais"), default=CHILE_COUNTRY_NAME)
    region = _upper_text(source.get("region") or source.get("region_text"))
    comuna = _upper_text(source.get("comuna") or source.get("comuna_text"))
    ciudad = _clean_text(source.get("ciudad"))
    if _is_chile_country(pais) and comuna and not ciudad:
        ciudad = "Santiago" if _is_metropolitana_region(region) else comuna
    ciudad = _upper_text(ciudad)
    pais = _upper_text(pais) if pais else CHILE_COUNTRY_NAME
    empresa = _upper_text(source.get("empresa"))
    nombre = _upper_text(source.get("nombre"))
    # Form/API may send only one of the two; listado usa "empresa" y la ficha tenia solo "nombre".
    if not empresa and nombre:
        empresa = nombre
    if not nombre and empresa:
        nombre = empresa
    return {
        "nombre": nombre,
        "empresa": empresa,
        "rut": clean_rut(source.get("rut")),
        "giro": _upper_text(source.get("giro")),
        "direccion": _upper_text(source.get("direccion")),
        "region": region,
        "comuna": comuna,
        "ciudad": ciudad,
        "pais": pais,
        "telefono": phone_to_compact_e164(_clean_text(source.get("telefono")), pais),
        "email": _normalize_party_email(source.get("email")),
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
    if not data["nombre"] and not data["empresa"]:
        errors.append("La empresa / razon social o el nombre de contacto es obligatorio.")
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
    instance.cliente_mayorista = bool(data.get("cliente_mayorista"))
    instance.margen_descuento_pct = float(data.get("margen_descuento_pct") or 0)
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
            "telefono": format_phone_display(entity.telefono or ""),
            "email": entity.email or "",
        }
    return {
        "name": entity.nombre or "",
        "rut": format_rut(entity.rut),
        "address": _full_address(entity),
        "telefono": format_phone_display(entity.telefono or ""),
        "email": entity.email or "",
    }


def _merge_party(form, entity, is_supplier_doc: bool) -> dict:
    base = _entity_snapshot(entity, is_supplier_doc)
    if not form:
        return base
    pais_ent = (getattr(entity, "pais", CHILE_COUNTRY_NAME) or CHILE_COUNTRY_NAME).strip() if entity else CHILE_COUNTRY_NAME
    ph_form = _clean_text(form.get("party_telefono"))
    if ph_form:
        c = phone_to_compact_e164(ph_form, pais_ent)
        tel_out = format_phone_display(c) if c else ""
    else:
        tel_out = base["telefono"]
    return {
        "name": _clean_text(form.get("party_name")) or base["name"],
        "rut": format_rut(_clean_text(form.get("party_rut")) or base["rut"]),
        "address": _clean_text(form.get("party_address")) or base["address"],
        "telefono": tel_out,
        "email": _clean_text(form.get("party_email")) or base["email"],
    }


def _doc_validation_errors(doc_type: str, tipo_documento: str, selected_client, selected_proveedor, party: dict, items: list[dict]) -> list[str]:
    errors = []
    if doc_type in {"cotizacion", "orden_venta", "factura"} and selected_client is None and selected_proveedor is None:
        errors.append("Debe seleccionar un cliente o un proveedor registrado (venta a proveedor).")

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
    tipo_documento = ((form.get("tipo_documento") if form else None) or "factura").strip().lower()

    if request.method == "GET" and doc_type in {"factura", "orden_venta"}:
        raw_tipo = _clean_text(request.args.get("tipo_documento") or request.args.get("tipo")).lower()
        if raw_tipo in {"factura", "boleta"}:
            tipo_documento = raw_tipo

    prefix = _doc_prefix(doc_type)
    if doc_type == "factura":
        prefix = _sales_doc_prefix(tipo_documento)

    doc_number = (form.get("doc_number") if form else None) or _next_doc_number(prefix)
    doc_date = (form.get("doc_date") if form else None) or now.strftime("%Y-%m-%d")
    doc_valid_until = (form.get("doc_valid_until") if form else None) or now.strftime("%Y-%m-%d")
    notes = ((form.get("notes") if form else None) or "").strip()
    numero_oc_cliente = ((form.get("numero_oc_cliente") if form else None) or "").strip()
    status = ((form.get("status") if form else None) or "pendiente").strip().lower()

    selected_client_id = _safe_int((form.get("client_id") if form else None) or "0", default=0)
    selected_proveedor_id = _safe_int((form.get("proveedor_id") if form else None) or "0", default=0)

    clientes = _all_clientes() if not is_supplier_doc else []
    proveedores = _all_proveedores() if is_supplier_doc else []
    selected_client = _client_by_id(selected_client_id)
    selected_proveedor = _proveedor_by_id(selected_proveedor_id)
    if is_supplier_doc:
        selected_party = selected_proveedor
    else:
        selected_party = selected_client if selected_client_id else selected_proveedor
    party = _merge_party(form, selected_party, is_supplier_doc)
    loaded_document_id = _safe_int((form.get("loaded_document_id") if form else None) or "0", default=0)

    items = _extract_items_from_form(form)
    cliente_totales = (
        selected_client
        if (not is_supplier_doc and selected_client_id and selected_client is not None)
        else None
    )
    totals = _calculate_totals(items, cliente_totales)
    validation_errors = []
    save_error = ""
    saved_successfully = False
    saved_number = ""
    estado_pago = "pendiente"
    metodo_pago = ""
    monto_saldo_favor_val = 0.0
    metodo_pago_resto = ""

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
                numero_oc_cliente = serialized.get("numero_oc_cliente") or numero_oc_cliente
                party = serialized["party"] or party
                selected_client_id = int(loaded_document.cliente_id or 0)
                selected_proveedor_id = int(loaded_document.proveedor_id or 0)
                selected_client = _client_by_id(selected_client_id)
                selected_proveedor = _proveedor_by_id(selected_proveedor_id)
                if is_supplier_doc:
                    selected_party = selected_proveedor
                else:
                    selected_party = selected_client if selected_client_id else selected_proveedor
                loaded_document_id = loaded_document.id
                items = serialized["items"] or items
                estado_pago = serialized.get("estado_pago", "pendiente")
                metodo_pago = serialized.get("metodo_pago", "")
                totals = {
                    "subtotal": serialized["totals"].get("subtotal", totals["subtotal"]),
                    "iva": serialized["totals"].get("iva", totals["iva"]),
                    "total": serialized["totals"].get("total", totals["total"]),
                }
                if doc_type == "factura":
                    monto_saldo_favor_val = float(serialized.get("monto_saldo_favor") or 0)

    if request.method == "POST":
        if form and doc_type == "factura":
            monto_saldo_favor_val = _safe_float((form.get("monto_saldo_favor") or "0").strip())
            metodo_pago_resto = (form.get("metodo_pago_resto") or "").strip().lower()
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
        if doc_type in {"orden_venta", "factura"}:
            validation_errors.extend(_origin_selection_errors(items))

        for error in validation_errors:
            flash(error, "error")

        if not validation_errors:
            try:
                pay_kw: dict = {}
                if doc_type == "factura":
                    pay_kw = {
                        "pago_saldo_monto": monto_saldo_favor_val,
                        "metodo_pago_resto": metodo_pago_resto,
                    }
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
                    numero_oc_cliente=numero_oc_cliente,
                    **pay_kw,
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

                serialized = _serialize_document(saved_document)
                doc_date = serialized["fecha_documento"] or doc_date
                doc_valid_until = serialized["fecha_vencimiento"] or doc_valid_until
                status = serialized["status"] or status
                tipo_documento = serialized["tipo_documento"] or tipo_documento
                notes = serialized["notes"] or notes
                numero_oc_cliente = serialized.get("numero_oc_cliente") or numero_oc_cliente
                party = serialized["party"] or party
                items = serialized["items"] or items
                estado_pago = serialized.get("estado_pago", estado_pago)
                metodo_pago = serialized.get("metodo_pago", metodo_pago)
                totals = {
                    "subtotal": serialized["totals"].get("subtotal", totals["subtotal"]),
                    "iva": serialized["totals"].get("iva", totals["iva"]),
                    "total": serialized["totals"].get("total", totals["total"]),
                }
                if doc_type == "factura":
                    monto_saldo_favor_val = float(serialized.get("monto_saldo_favor") or 0)

                selected_client_id = int(saved_document.cliente_id or 0)
                selected_proveedor_id = int(saved_document.proveedor_id or 0)
                selected_client = _client_by_id(selected_client_id)
                selected_proveedor = _proveedor_by_id(selected_proveedor_id)
                if is_supplier_doc:
                    selected_party = selected_proveedor
                else:
                    selected_party = selected_client if selected_client_id else selected_proveedor
            except Exception as exc:
                db.session.rollback()
                current_app.logger.exception(
                    "Error guardando documento ventas tipo=%s numero=%s", doc_type, doc_number
                )
                save_error = f"No se pudo guardar el documento: {exc}"
                flash(save_error, "error")

    doc_for_conv = db.session.get(DocumentoVenta, loaded_document_id) if loaded_document_id else None
    doc_conversion = (
        _conversion_flags_for_documento(doc_for_conv)
        if doc_for_conv is not None
        else {
            "puede_convertir_a_orden_venta": True,
            "puede_convertir_a_factura_boleta": True,
            "documento_hijo_resumen": None,
            "documento_hijo_tipo": None,
            "picking_bloquea_facturacion": False,
            "picking_status": None,
        }
    )

    cliente_saldo_favor_erp = 0.0
    if not is_supplier_doc and int(selected_client_id or 0) > 0:
        base_ldg = _cliente_saldo_favor_ledger(int(selected_client_id))
        cliente_saldo_favor_erp = _round_money_cl(base_ldg)
        if doc_type == "factura" and monto_saldo_favor_val > 0:
            cliente_saldo_favor_erp = _round_money_cl(base_ldg + monto_saldo_favor_val)

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
        "numero_oc_cliente": numero_oc_cliente,
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
            "descuento": _format_currency(totals.get("descuento_monto") or 0),
            "subtotal_bruto": _format_currency(totals.get("subtotal_bruto") or totals["subtotal"]),
        },
        "notes": notes,
        "generated": saved_successfully,
        "saved_number": saved_number,
        "save_error": save_error,
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
        "monto_saldo_favor": monto_saldo_favor_val,
        "metodo_pago_resto": metodo_pago_resto,
        "cliente_saldo_favor_erp": cliente_saldo_favor_erp,
        "metodo_pago_resto_choices": [(k, METODO_PAGO_LABELS.get(k, k)) for k in METODO_PAGO_OPTIONS if k != "saldo_favor"],
        "party_email": (party or {}).get("email", ""),
        "party_phone": (party or {}).get("telefono", ""),
        "client_email": (party or {}).get("email", ""),
        "client_phone": (party or {}).get("telefono", ""),
        "doc_conversion": doc_conversion,
        **_base_ctx(),
        **_sii_contribuyente_config_payload(
            _sii_profile_inline_party(is_supplier_doc),
            party_preload="proveedores" if is_supplier_doc else "clientes",
        ),
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
    if request.method == "POST" and not has_permission(session.get("user"), session.get("rol"), "ventas_guardar_documento"):
        return _deny_perm_response("No tienes permiso para guardar documentos de venta.")
    _partial = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    ctx = _build_doc_context("cotizacion", "Cotización", "Cliente", False, False)
    ajax_resp = _ajax_doc_save_response(ctx, default_ok_message="Cotización guardada correctamente")
    if ajax_resp is not None:
        return ajax_resp
    if request.method == "POST" and ctx.get("generated"):
        numero = (ctx.get("saved_number") or ctx.get("doc_number") or "").strip()
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
    if request.method == "POST" and not has_permission(session.get("user"), session.get("rol"), "ventas_guardar_documento"):
        return _deny_perm_response("No tienes permiso para guardar documentos de venta.")
    _partial = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    ctx = _build_doc_context("orden_venta", "Orden de Venta", "Cliente", False, True)
    ajax_resp = _ajax_doc_save_response(ctx, default_ok_message="Orden de venta guardada correctamente")
    if ajax_resp is not None:
        return ajax_resp
    ctx["_partial"] = _partial
    return render_template("ventas/orden_venta.html", **ctx)


@ventas_bp.route("/orden-compra", methods=["GET", "POST"])
@ventas_bp.route("/orden_compra", methods=["GET", "POST"])
@login_required
def orden_compra():
    if request.method == "POST" and not has_permission(session.get("user"), session.get("rol"), "ventas_guardar_documento"):
        return _deny_perm_response("No tienes permiso para guardar documentos de venta.")
    _partial = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    ctx = _build_doc_context("orden_compra", "Orden de Compra", "Proveedor", True, False)
    ajax_resp = _ajax_doc_save_response(ctx, default_ok_message="Orden de compra guardada correctamente")
    if ajax_resp is not None:
        return ajax_resp
    ctx["_partial"] = _partial
    return render_template("ventas/documento.html", **ctx)


@ventas_bp.route("/facturacion", methods=["GET", "POST"])
@ventas_bp.route("/factura", methods=["GET", "POST"])
@login_required
def facturacion():
    if request.method == "POST" and not has_permission(session.get("user"), session.get("rol"), "ventas_guardar_documento"):
        return _deny_perm_response("No tienes permiso para guardar documentos de venta.")
    _partial = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    ctx = _build_doc_context("factura", "Facturación", "Cliente", False, True)
    if (ctx.get("tipo_documento") or "").lower() == "boleta":
        ctx["active_page"] = "boleta"
        ctx["title"] = "Boleta"
    tipo = (ctx.get("tipo_documento") or "factura").lower()
    label = "Boleta" if tipo == "boleta" else "Factura"
    ajax_resp = _ajax_doc_save_response(ctx, default_ok_message=f"{label} guardada correctamente")
    if ajax_resp is not None:
        return ajax_resp
    ctx["_partial"] = _partial
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
    if request.method == "POST" and not has_permission(session.get("user"), session.get("rol"), "ventas_guardar_documento"):
        return _deny_perm_response("No tienes permiso para crear clientes.")
    chile_geo = _load_chile_geo()
    form_data = _cliente_form_data(request.form if request.method == "POST" else None)
    validation_errors = []
    if request.method == "POST":
        errors = _validate_cliente_data(form_data)
        validation_errors = errors
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
        validation_errors=validation_errors,
        chile_geo=chile_geo,
        chile_regions=_chile_regions(chile_geo),
        active_page="clientes",
        **_base_ctx(),
        **_sii_contribuyente_config_payload(_sii_profile_cliente_form()),
    )


@ventas_bp.route("/clientes/<int:cid>/editar", methods=["GET", "POST"])
@login_required
def cliente_editar(cid: int):
    if request.method == "POST" and not has_permission(session.get("user"), session.get("rol"), "ventas_guardar_documento"):
        return _deny_perm_response("No tienes permiso para editar clientes.")
    chile_geo = _load_chile_geo()
    c = db.session.get(Cliente, cid)
    if c is None or not c.activo:
        flash("Cliente no encontrado.", "error")
        return redirect(url_for("ventas.clientes"))

    validation_errors = []
    if request.method == "POST":
        form_data = _cliente_form_data(request.form)
        errors = _validate_cliente_data(form_data)
        validation_errors = errors
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
        validation_errors=validation_errors,
        chile_geo=chile_geo,
        chile_regions=_chile_regions(chile_geo),
        cliente_id=cid,
        active_page="clientes",
        **_base_ctx(),
        **_sii_contribuyente_config_payload(_sii_profile_cliente_form()),
    )


@ventas_bp.route("/clientes/<int:cid>/eliminar", methods=["POST"])
@login_required
def cliente_eliminar(cid: int):
    if not has_permission(session.get("user"), session.get("rol"), "ventas_guardar_documento"):
        return _deny_perm_response("No tienes permiso para desactivar clientes.")
    c = db.session.get(Cliente, cid)
    if c and c.activo:
        c.activo = False
        db.session.commit()
        flash("Cliente desactivado correctamente.", "success")
    return redirect(url_for("ventas.clientes"))


@ventas_bp.route("/clientes/<int:cid>/historial")
@login_required
def cliente_historial(cid: int):
    filtro_oc = _clean_text(request.args.get("filtro_oc")) == "1"
    cliente, payload = _build_client_history_payload(cid, solo_con_oc=filtro_oc)
    if cliente is None or payload is None:
        flash("Cliente no encontrado.", "error")
        return redirect(url_for("ventas.clientes"))
    return render_template(
        "ventas/cliente_historial.html",
        cliente=cliente,
        data=payload,
        filtro_oc=filtro_oc,
        active_page="clientes",
        **_base_ctx(),
    )


@ventas_bp.route("/clientes/<int:cid>/saldo_favor", methods=["POST"])
@login_required
def cliente_saldo_favor_manual(cid: int):
    if not has_permission(session.get("user"), session.get("rol"), "ventas_guardar_documento"):
        flash("Sin permiso para registrar saldo a favor.", "error")
        return redirect(url_for("ventas.cliente_historial", cid=cid))
    c = db.session.get(Cliente, cid)
    if c is None or not c.activo:
        flash("Cliente no encontrado.", "error")
        return redirect(url_for("ventas.clientes"))
    monto = _round_money_cl(_safe_float((request.form.get("monto_saldo_agregar") or "0").strip()))
    nfac = (request.form.get("ref_numero_factura") or "").strip()[:100]
    nnc = (request.form.get("ref_numero_nota_credito") or "").strip()[:100]
    razon = (request.form.get("ref_razon_saldo") or "").strip()
    if monto <= 0:
        flash("El monto a acreditar debe ser mayor a 0.", "error")
        return redirect(url_for("ventas.cliente_historial", cid=cid))
    if not nfac or not nnc or not razon:
        flash("Completá número de factura, nota de crédito y la razón del crédito.", "error")
        return redirect(url_for("ventas.cliente_historial", cid=cid))
    try:
        db.session.add(
            ClienteSaldoFavorMovimiento(
                cliente_id=c.id,
                monto=monto,
                tipo="manual_ingreso",
                ref_factura_numero=nfac,
                ref_nota_credito_numero=nnc,
                razon=razon[:2000],
                usuario=session.get("user") or "sistema",
            )
        )
        db.session.commit()
        flash("Saldo a favor registrado correctamente.", "success")
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("saldo_favor manual")
        flash(f"No se pudo registrar el saldo: {exc}", "error")
    return redirect(url_for("ventas.cliente_historial", cid=cid))


@ventas_bp.route("/api/cliente/<int:cid>/saldo_favor", methods=["GET"])
@login_required
def api_cliente_saldo_favor(cid: int):
    c = db.session.get(Cliente, cid)
    if c is None or not c.activo:
        return jsonify({"success": False, "message": "Cliente no encontrado"}), 404
    sal = _round_money_cl(_cliente_saldo_favor_ledger(c.id))
    return jsonify({"success": True, "cliente_id": c.id, "saldo_favor": sal})


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
    if request.method == "POST" and not has_permission(session.get("user"), session.get("rol"), "ventas_guardar_documento"):
        return _deny_perm_response("No tienes permiso para crear proveedores.")
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
        **_sii_contribuyente_config_payload(
            _sii_profile_proveedor_form(), party_preload="proveedores"
        ),
    )


@ventas_bp.route("/proveedores/<int:pid>/editar", methods=["GET", "POST"])
@login_required
def proveedor_editar(pid: int):
    if request.method == "POST" and not has_permission(session.get("user"), session.get("rol"), "ventas_guardar_documento"):
        return _deny_perm_response("No tienes permiso para editar proveedores.")
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
        **_sii_contribuyente_config_payload(
            _sii_profile_proveedor_form(), party_preload="proveedores"
        ),
    )


@ventas_bp.route("/proveedores/<int:pid>/eliminar", methods=["POST"])
@login_required
def proveedor_eliminar(pid: int):
    if not has_permission(session.get("user"), session.get("rol"), "ventas_guardar_documento"):
        return _deny_perm_response("No tienes permiso para desactivar proveedores.")
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


@ventas_bp.route("/notas_credito")
@login_required
def notas_credito():
    search_term = _extract_search_term()
    query = NotaCredito.query.join(
        DocumentoVenta, NotaCredito.documento_venta_id == DocumentoVenta.id
    )
    if search_term:
        term = f"%{search_term}%"
        query = query.filter(
            NotaCredito.numero.ilike(term)
            | NotaCredito.razon.ilike(term)
            | DocumentoVenta.cliente_nombre.ilike(term)
            | DocumentoVenta.numero.ilike(term)
            | DocumentoVenta.numero_oc_cliente.ilike(term)
        )
    lista = query.order_by(NotaCredito.fecha_documento.desc(), NotaCredito.id.desc()).all()
    return render_template(
        "ventas/notas_credito.html",
        notas=lista,
        search_term=search_term,
        active_page="notas_credito",
        **_base_ctx(),
    )


@ventas_bp.route("/notas_credito/<int:nid>")
@login_required
def nota_credito_detalle(nid: int):
    nc = db.session.get(NotaCredito, nid)
    if nc is None:
        flash("Nota de credito no encontrada.", "error")
        return redirect(url_for("ventas.notas_credito"))
    doc = nc.documento_original
    return render_template(
        "ventas/nota_credito_detalle.html",
        nota=nc,
        documento_origen=doc,
        active_page="notas_credito",
        **_base_ctx(),
    )


@ventas_bp.route("/pagos", methods=["GET"])
@login_required
def pagos_caja():
    if not has_permission(session.get("user"), session.get("rol"), "mod_finanzas"):
        return _deny_perm_response("Sin permiso para acceder a Caja / Pagos.")
    search_term = _extract_search_term()
    filtro = _extract_pagos_filtro()
    cliente_filtro = _clean_text(request.args.get("cliente"))
    documento = _find_factura_boleta_por_busqueda(search_term) if search_term else None

    lista_q = DocumentoVenta.query.filter(DocumentoVenta.tipo.in_(["factura", "boleta"]))
    if cliente_filtro:
        lista_q = lista_q.filter(DocumentoVenta.cliente_nombre.ilike(f"%{cliente_filtro}%"))
    if filtro == "pendientes":
        lista_q = lista_q.filter(or_(DocumentoVenta.estado_pago.is_(None), DocumentoVenta.estado_pago != "pagado"))
    elif filtro == "pagados":
        lista_q = lista_q.filter(DocumentoVenta.estado_pago == "pagado")

    if filtro == "pagados":
        lista_documentos = lista_q.order_by(DocumentoVenta.updated_at.desc(), DocumentoVenta.id.desc()).limit(25).all()
    else:
        lista_documentos = lista_q.order_by(DocumentoVenta.fecha_documento.desc(), DocumentoVenta.id.desc()).limit(25).all()

    return render_template(
        "ventas/pagos.html",
        documento=documento,
        search_term=search_term,
        filtro=filtro,
        cliente_filtro=cliente_filtro,
        lista_documentos=lista_documentos,
        metodo_labels=METODO_PAGO_LABELS,
        metodo_options=METODO_PAGO_OPTIONS,
        active_page="pagos",
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
    payload = _serialize_product(producto, codigo=codigo, variantes=variantes)
    if "marca" in request.args or "bodega" in request.args or "origen_compra" in request.args:
        ref_marca = request.args.get("marca", "")
        ref_bodega = (request.args.get("bodega") or "").strip() or "Bodega 1"
        ref_origen = _normalize_origen_compra(request.args.get("origen_compra") or "")
    else:
        ref_marca = payload.get("default_marca") or ""
        ref_bodega = (payload.get("default_bodega") or "").strip() or "Bodega 1"
        ref_origen = _normalize_origen_compra(payload.get("default_origen_compra") or "")
    payload["ingreso_ref"] = _ultimo_ingreso_ref(codigo, ref_marca, ref_bodega, ref_origen)
    _fill_precio_desde_ingreso_si_vacio(payload)
    return jsonify({
        "success": True,
        "producto": payload,
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
    """Clientes activos; si hay texto de búsqueda, incluye proveedores (misma búsqueda) para ventas a proveedor."""
    q_raw = (request.args.get("q") or "").strip()
    q = q_raw.lower()
    query = Cliente.query.filter_by(activo=True)
    if q:
        term = f"%{q}%"
        normalized_term = clean_rut(q_raw)
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
    cliente_ruts: set[str] = set()
    out: list[dict] = []
    for c in lista:
        cr = clean_rut(c.rut or "")
        if cr:
            cliente_ruts.add(cr)
        row = c.to_dict()
        row["es_proveedor"] = False
        out.append(row)

    if q:
        pq = Proveedor.query.filter_by(activo=True)
        term = f"%{q}%"
        normalized_term = clean_rut(q_raw)
        pq = pq.filter(
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
        for p in pq.order_by(Proveedor.nombre).limit(50).all():
            pr = clean_rut(p.rut or "")
            if pr and pr in cliente_ruts:
                continue
            row = p.to_dict()
            row["es_proveedor"] = True
            out.append(row)

    return jsonify({"success": True, "clientes": out})


@ventas_bp.route("/api/clientes/create", methods=["POST"])
@login_required
def api_cliente_create():
    if not has_permission(session.get("user"), session.get("rol"), "ventas_guardar_documento"):
        return jsonify({"success": False, "message": "Sin permiso para crear clientes."}), 403
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
    if not has_permission(session.get("user"), session.get("rol"), "ventas_guardar_documento"):
        return jsonify({"success": False, "message": "Sin permiso para crear proveedores."}), 403
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
        if _numero_es_siguiente_correlativo_libre(numero):
            return jsonify(
                {"success": True, "documento": _serialize_documento_borrador_siguiente("cotizacion", numero)}
            )
        return jsonify({"success": False, "message": _mensaje_correlativo_invalido(numero)}), 404
    return jsonify({"success": True, "documento": _serialize_document(documento)})


@ventas_bp.route("/api/orden_venta/<string:numero>", methods=["GET"])
@login_required
def api_orden_venta_by_numero(numero: str):
    documento = _load_document_by_number("orden_venta", numero)
    if documento is None:
        if _numero_es_siguiente_correlativo_libre(numero):
            return jsonify(
                {"success": True, "documento": _serialize_documento_borrador_siguiente("orden_venta", numero)}
            )
        return jsonify({"success": False, "message": _mensaje_correlativo_invalido(numero)}), 404
    return jsonify({"success": True, "documento": _serialize_document(documento)})


@ventas_bp.route("/api/orden_venta/<string:numero>/picking", methods=["POST"])
@login_required
def api_orden_venta_solicitar_picking(numero: str):
    if not has_permission(session.get("user"), session.get("rol"), "ventas_guardar_documento"):
        return jsonify({"success": False, "message": "Sin permiso para solicitar picking."}), 403
    if not has_permission(session.get("user"), session.get("rol"), "bodega_picking"):
        return jsonify({"success": False, "message": "Sin permiso de Bodega para gestionar picking."}), 403
    """Crea o reutiliza el picking de bodega para esta orden de venta y devuelve URL al modulo bodega."""
    safe = (numero or "").strip().upper()
    doc = _load_document_by_numero_or_id("orden_venta", safe)
    if doc is None or (doc.tipo or "").strip().lower() != "orden_venta":
        return jsonify({"success": False, "message": "Orden de venta no encontrada"}), 404

    line_items = [it for it in (doc.items or []) if (it.codigo_producto or "").strip() or (it.descripcion or "").strip()]
    if not line_items:
        return jsonify({"success": False, "message": "La orden no tiene lineas para pickear. Guarde la orden con productos primero."}), 400

    existing = PickingVenta.query.filter_by(orden_venta_id=doc.id).first()
    if existing is not None:
        return jsonify(
            {
                "success": True,
                "picking_id": existing.id,
                "redirect_url": url_for("bodega.picking_venta_detalle", pid=existing.id),
                "message": "Esta orden ya tiene picking en bodega.",
            }
        )

    picking = PickingVenta(
        orden_venta_id=doc.id,
        status="pendiente",
        usuario_creacion=(session.get("user") or "").strip() or None,
    )
    db.session.add(picking)
    db.session.flush()
    for idx, it in enumerate(line_items, start=1):
        db.session.add(
            PickingVentaLine(
                picking_id=picking.id,
                codigo_producto=(it.codigo_producto or "").strip().upper(),
                descripcion=(it.descripcion or "").strip(),
                marca=(it.marca or "").strip().upper(),
                bodega=(it.bodega or "").strip() or "Bodega 1",
                cantidad_pedida=max(0, int(it.cantidad or 0)),
                cantidad_entregada=0,
                orden_linea=idx,
            )
        )
    db.session.commit()
    return jsonify(
        {
            "success": True,
            "picking_id": picking.id,
            "redirect_url": url_for("bodega.picking_venta_detalle", pid=picking.id),
        }
    )


@ventas_bp.route("/api/orden_compra/<string:numero>", methods=["GET"])
@login_required
def api_orden_compra_by_numero(numero: str):
    documento = _load_document_by_number("orden_compra", numero)
    if documento is None:
        if _numero_es_siguiente_correlativo_libre(numero):
            return jsonify(
                {"success": True, "documento": _serialize_documento_borrador_siguiente("orden_compra", numero)}
            )
        return jsonify({"success": False, "message": _mensaje_correlativo_invalido(numero)}), 404
    return jsonify({"success": True, "documento": _serialize_document(documento)})


@ventas_bp.route("/api/factura/<string:numero>", methods=["GET"])
@login_required
def api_factura_by_numero(numero: str):
    documento = _load_document_by_number("factura", numero)
    if documento is None:
        if _numero_es_siguiente_correlativo_libre(numero):
            return jsonify(
                {"success": True, "documento": _serialize_documento_borrador_siguiente("factura", numero)}
            )
        return jsonify({"success": False, "message": _mensaje_correlativo_invalido(numero)}), 404
    return jsonify({"success": True, "documento": _serialize_document(documento)})


@ventas_bp.route("/api/convert/cotizacion/<string:numero>/orden_venta", methods=["POST"])
@login_required
def api_convert_cotizacion_orden_venta(numero: str):
    if not has_permission(session.get("user"), session.get("rol"), "ventas_convertir_documento"):
        return jsonify({"success": False, "message": "Sin permiso para convertir documentos."}), 403
    safe_numero = (numero or "").strip().upper()
    source = _load_document_by_numero_or_id("cotizacion", safe_numero)
    if source is None or (source.tipo or "").strip().lower() != "cotizacion":
        return jsonify({"success": False, "message": "Cotizacion no encontrada"}), 404

    exist_ov = _documento_hijo_directo(source.id, {"orden_venta"})
    pagado = _factura_boleta_hija_con_pago_caja(exist_ov.id) if exist_ov is not None else None
    if pagado is not None:
        t = (pagado.tipo or "documento").upper()
        n = (pagado.numero or str(pagado.id)).strip()
        return jsonify(
            {
                "success": False,
                "message": f"La cadena ya tiene {t} {n} cobrado en caja. No se puede generar otra orden de venta.",
            }
        ), 400

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
    if not has_permission(session.get("user"), session.get("rol"), "ventas_convertir_documento"):
        return jsonify({"success": False, "message": "Sin permiso para convertir documentos."}), 403
    safe_numero = (numero or "").strip().upper()
    source = _load_document_by_numero_or_id("orden_venta", safe_numero)
    if source is None or (source.tipo or "").strip().lower() != "orden_venta":
        return jsonify({"success": False, "message": "Orden de venta no encontrada"}), 404

    payload = request.get_json(silent=True) or {}
    target_tipo_documento = (payload.get("tipo_documento") or "factura").strip().lower()
    if target_tipo_documento not in {"factura", "boleta"}:
        return jsonify({"success": False, "message": "Tipo de documento invalido. Usa factura o boleta."}), 400

    pagado = _factura_boleta_hija_con_pago_caja(source.id)
    if pagado is not None:
        n = (pagado.numero or str(pagado.id)).strip()
        t = (pagado.tipo or "documento").upper()
        return jsonify(
            {
                "success": False,
                "message": f"Esta orden de venta ya tiene {t} {n} cobrado en caja. No se puede emitir otra factura o boleta.",
            }
        ), 400

    pv_ov = PickingVenta.query.filter_by(orden_venta_id=source.id).first()
    if pv_ov is not None and (pv_ov.status or "").strip().lower() != "entregado":
        return jsonify(
            {
                "success": False,
                "message": "Hay un picking de bodega pendiente para esta orden. Espere a que bodega marque la entrega al vendedor antes de facturar o boletear.",
            }
        ), 400

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
            "redirect_url": url_for(
                "ventas.facturacion",
                numero=target.numero,
                tipo_documento=target_tipo_documento,
            ),
        })
    except Exception as exc:
        db.session.rollback()
        return jsonify({"success": False, "message": f"No se pudo facturar: {exc}"}), 400


@ventas_bp.route("/api/convert/orden_compra/<int:documento_id>/ingreso", methods=["POST"])
@login_required
def api_convert_orden_compra_ingreso(documento_id: int):
    if not has_permission(session.get("user"), session.get("rol"), "ventas_convertir_documento"):
        return jsonify({"success": False, "message": "Sin permiso para convertir documentos."}), 403
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
    if not has_permission(session.get("user"), session.get("rol"), "ventas_convertir_documento"):
        return jsonify({"success": False, "message": "Sin permiso para convertir documentos."}), 403
    source = db.session.get(DocumentoVenta, documento_id)
    if source is None or (source.tipo or "").strip().lower() not in {"factura", "boleta"}:
        return jsonify({"success": False, "message": "Factura no encontrada"}), 404

    payload = request.get_json(silent=True) or {}
    razon = (payload.get("razon") or "Devolucion total").strip()
    raw_modo = (payload.get("modo_liquidacion") or payload.get("efecto_liquidacion") or "").strip().lower()
    if raw_modo in ("devolucion", "devolucion_dinero", "reembolso", "efectivo", "transferencia"):
        modo_liquidacion = "devolucion_dinero"
    elif raw_modo in ("saldo", "saldo_favor", "credito", "crédito"):
        modo_liquidacion = "saldo_favor"
    else:
        modo_liquidacion = "saldo_favor"

    try:
        nota = NotaCredito(
            documento_venta_id=source.id,
            numero=_next_credit_note_number(),
            razon=razon,
            modo_liquidacion=modo_liquidacion,
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
                    origen_compra=_normalize_origen_compra(getattr(src_item, "origen_compra", None)),
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
                origen_compra=(getattr(item, "origen_compra", None) or "").strip(),
                delta=qty,
                reason=f"Nota de credito {nota.numero}",
            )
            if err:
                raise ValueError(err)

        nota.stock_restored = True
        nota.status = "aprobada"
        if (
            modo_liquidacion == "saldo_favor"
            and int(source.cliente_id or 0) > 0
            and float(nota.total or 0) > 0
        ):
            db.session.add(
                ClienteSaldoFavorMovimiento(
                    cliente_id=int(source.cliente_id),
                    monto=float(nota.total or 0),
                    tipo="nota_credito_credito",
                    nota_credito_id=nota.id,
                    ref_nota_credito_numero=(nota.numero or "")[:100] if nota.numero else None,
                    razon=(razon or "")[:2000] if razon else None,
                    usuario=session.get("user") or "sistema",
                )
            )
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
                    "modo_liquidacion": nota.modo_liquidacion or "saldo_favor",
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


@ventas_bp.route("/api/orden_venta/autorizar_margen", methods=["POST"])
@login_required
def api_orden_venta_autorizar_margen():
    if not has_permission(session.get("user"), session.get("rol"), "ventas_autorizar_margen_bajo"):
        return jsonify({"success": False, "message": "Sin permiso para autorizar margen bajo."}), 403
    data = request.get_json(silent=True) or {}
    usuario = (data.get("usuario") or "").strip()
    password = data.get("password") or ""
    if not usuario or not password:
        return jsonify({"success": False, "message": "Debes indicar usuario y clave de autorización."}), 400

    u = UsuarioSistema.query.filter_by(usuario=usuario).first()
    if not u:
        return jsonify({"success": False, "message": "Usuario autorizador no válido."}), 403
    if not bool(u.activo):
        return jsonify({"success": False, "message": "Usuario autorizador inactivo."}), 403
    if not _rol_autoriza_margen_bajo(u.rol.nombre if u.rol else ""):
        return jsonify({"success": False, "message": "El usuario no tiene permiso para autorizar margen bajo."}), 403

    try:
        ok = check_password_hash(u.password_hash or "", password)
    except Exception:
        ok = False
    if not ok:
        return jsonify({"success": False, "message": "Clave incorrecta para autorización."}), 403

    return jsonify({"success": True, "message": f"Autorizado por {u.usuario}."})


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
    if not has_permission(session.get("user"), session.get("rol"), "ventas_convertir_documento"):
        return jsonify({"success": False, "message": "Sin permiso para emitir nota de crédito."}), 403
    """Create a credit note from an existing sales document."""
    from app.utils.stock_control import restore_stock_for_credit_note
    
    data = request.get_json(silent=True) or {}
    
    # Validate input
    documento_id = data.get("documento_id")
    items = data.get("items", [])
    razon = (data.get("razon") or "").strip()
    raw_modo_api = (data.get("modo_liquidacion") or data.get("efecto_liquidacion") or "").strip().lower()
    if raw_modo_api in ("devolucion", "devolucion_dinero", "reembolso", "efectivo", "transferencia"):
        modo_liquidacion_api = "devolucion_dinero"
    elif raw_modo_api in ("saldo", "saldo_favor", "credito", "crédito"):
        modo_liquidacion_api = "saldo_favor"
    else:
        modo_liquidacion_api = "saldo_favor"
    
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
            modo_liquidacion=modo_liquidacion_api,
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
        
        nota.status = "aprobada"
        db.session.commit()
        
        return jsonify({
            "success": True,
            "nota_credito": {
                "id": nota.id,
                "numero": nota.numero,
                "documento_venta_id": documento_id,
                "total": nota.total,
                "status": nota.status,
                "modo_liquidacion": nota.modo_liquidacion or "saldo_favor",
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

def _registrar_pago_documento(
    doc: DocumentoVenta,
    metodo: str,
    *,
    referencia: str = "",
    fecha_pago: datetime | None = None,
) -> str | None:
    """Marca documento como pagado. Devuelve mensaje de error o None si OK."""
    met = (metodo or "").strip().lower()
    if met not in METODO_PAGO_OPTIONS:
        return f"Método de pago inválido: {met}"
    doc.metodo_pago = met
    doc.estado_pago = "pagado"
    doc.pago_referencia = (referencia or "").strip()[:200]
    doc.updated_at = fecha_pago or datetime.utcnow()
    _mark_documento_aprobada_por_cobro(doc)
    _cascade_upstream_aprobada(doc)
    return None


@ventas_bp.route("/api/documento/<int:doc_id>/pago", methods=["POST"])
@login_required
def api_registrar_pago(doc_id: int):
    if not has_permission(session.get("user"), session.get("rol"), "mod_finanzas"):
        return jsonify({"ok": False, "error": "Sin permiso para registrar pagos."}), 403
    """Register payment method and mark document as paid."""
    doc = db.session.get(DocumentoVenta, doc_id)
    if doc is None:
        return jsonify({"ok": False, "error": "Documento no encontrado"}), 404

    data = request.get_json(force=True) or {}
    metodo = (data.get("metodo_pago") or "efectivo").strip().lower()
    referencia = (data.get("pago_referencia") or data.get("referencia_caja") or "").strip()
    fecha_raw = (data.get("fecha") or data.get("fecha_pago") or "").strip()
    fecha_pago = None
    if fecha_raw:
        try:
            fecha_pago = datetime.strptime(fecha_raw[:10], "%Y-%m-%d")
        except ValueError:
            return jsonify({"ok": False, "error": "Fecha de pago inválida (use AAAA-MM-DD)."}), 400

    err = _registrar_pago_documento(doc, metodo, referencia=referencia, fecha_pago=fecha_pago)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    db.session.commit()

    return jsonify({
        "ok": True,
        "metodo_pago": doc.metodo_pago,
        "estado_pago": doc.estado_pago,
        "metodo_label": METODO_PAGO_LABELS.get(metodo, metodo),
        "pago_referencia": (getattr(doc, "pago_referencia", None) or "").strip(),
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
        "pago_referencia": (getattr(doc, "pago_referencia", None) or "").strip(),
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
    if not has_permission(session.get("user"), session.get("rol"), "ventas_enviar_documento"):
        return jsonify({"success": False, "message": "Sin permiso para enviar documentos."}), 403
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


@ventas_bp.route("/api/enviar_email/<int:doc_id>", methods=["POST"])
@login_required
def api_enviar_email_por_id(doc_id: int):
    if not has_permission(session.get("user"), session.get("rol"), "ventas_enviar_documento"):
        return jsonify({"success": False, "message": "Sin permiso para enviar documentos."}), 403
    """Compatibility endpoint: send document by id."""
    return api_enviar_email_documento(doc_id)


@ventas_bp.route("/api/reenviar/<int:doc_id>", methods=["POST"])
@login_required
def api_reenviar_documento(doc_id: int):
    """Re-send endpoint (same behavior as send email)."""
    return api_enviar_email_documento(doc_id)


@ventas_bp.route("/api/whatsapp/<int:doc_id>", methods=["GET"])
@login_required
def api_whatsapp_por_id(doc_id: int):
    if not has_permission(session.get("user"), session.get("rol"), "ventas_enviar_documento"):
        return jsonify({"success": False, "message": "Sin permiso para enviar documentos."}), 403
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
    if not has_permission(session.get("user"), session.get("rol"), "ventas_enviar_documento"):
        return jsonify({"success": False, "message": "Sin permiso para enviar documentos."}), 403
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
    if not has_permission(session.get("user"), session.get("rol"), "ventas_enviar_documento"):
        return jsonify({"success": False, "message": "Sin permiso para enviar documentos."}), 403
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
