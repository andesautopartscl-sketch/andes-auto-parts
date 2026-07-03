from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import func, or_

from app.extensions import db
from app.utils.decorators import login_required, permission_required
from app.utils.permissions import has_permission
from app.utils.rut_utils import format_rut
from app.ventas.models import Cliente
from app.ventas.routes import METODO_PAGO_LABELS, METODO_PAGO_OPTIONS, _client_by_id, _entity_snapshot, _full_address

from .models import OC_ESTADOS, OC_ESTADO_LABELS, OrdenCompraCliente, OrdenCompraClienteItem, oc_estado_label
from .services import (
    buscar_oc_por_numero,
    calcular_totales_items,
    codigo_en_inventario,
    descontar_stock_oc,
    listar_oc_por_cliente,
    timeline_eventos,
)
from .ocr import escanear_oc

oc_clientes_bp = Blueprint(
    "oc_clientes",
    __name__,
    url_prefix="/oc-clientes",
    template_folder="../../templates",
)


def _current_user() -> str:
    return session.get("user") or "sistema"


def _deny(message: str = "Sin permiso para este módulo."):
    is_ajax = (request.headers.get("X-Requested-With") or "").lower() == "xmlhttprequest"
    if is_ajax or request.is_json:
        return jsonify({"ok": False, "error": message}), 403
    flash(message, "error")
    return redirect(url_for("productos.buscar"))


@oc_clientes_bp.before_request
def _oc_module_guard():
    if "user" not in session:
        return None
    if has_permission(session.get("user"), session.get("rol"), "ver_oc_clientes"):
        return None
    return _deny("No tienes permisos para acceder a Órdenes de Compra Cliente.")


def _can_modify() -> bool:
    return has_permission(session.get("user"), session.get("rol"), "mod_oc_clientes")


def _parse_date(value: str | None, default: datetime | None = None) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return default
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d")
    except ValueError:
        return default


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _extract_items_from_form(form) -> list[dict]:
    if form is None:
        return []
    codigos = form.getlist("item_codigo[]")
    descripciones = form.getlist("item_descripcion[]")
    marcas = form.getlist("item_marca[]")
    bodegas = form.getlist("item_bodega[]")
    cantidades = form.getlist("item_cantidad[]")
    precios = form.getlist("item_precio[]")
    descuentos = form.getlist("item_descuento[]")
    items: list[dict] = []
    n = max(len(codigos), len(descripciones), len(cantidades), len(precios))
    for i in range(n):
        codigo = (codigos[i] if i < len(codigos) else "").strip().upper()
        desc = (descripciones[i] if i < len(descripciones) else "").strip()
        if not codigo and not desc:
            continue
        cant = max(_safe_int(cantidades[i] if i < len(cantidades) else "1", 1), 1)
        precio = _safe_float(precios[i] if i < len(precios) else "0")
        desc_pct = _safe_float(descuentos[i] if i < len(descuentos) else "0")
        bruto = cant * precio
        subtotal = round(bruto - bruto * (desc_pct / 100.0), 2) if desc_pct else round(bruto, 2)
        items.append(
            {
                "codigo_producto": codigo,
                "descripcion": desc,
                "marca": (marcas[i] if i < len(marcas) else "").strip().upper(),
                "bodega": (bodegas[i] if i < len(bodegas) else "").strip() or "Bodega 1",
                "cantidad": cant,
                "precio_unitario": precio,
                "descuento_item": desc_pct,
                "subtotal": subtotal,
                "en_inventario": codigo_en_inventario(codigo) if codigo else False,
            }
        )
    return items


def _estado_badge(estado: str) -> str:
    return {
        "recibida": "blue",
        "entregada": "orange",
        "pagada": "green",
        "anulada": "slate",
    }.get((estado or "").strip().lower(), "slate")


def _estado_label(estado: str) -> str:
    return oc_estado_label(estado)


def _build_list_summary() -> dict:
    pendientes = OrdenCompraCliente.query.filter_by(estado="recibida").count()
    por_cobrar = (
        db.session.query(func.coalesce(func.sum(OrdenCompraCliente.total), 0.0))
        .filter(OrdenCompraCliente.estado == "entregada")
        .scalar()
    )
    today = date.today()
    mes_inicio = datetime(today.year, today.month, 1)
    _, last_day = monthrange(today.year, today.month)
    mes_fin = datetime(today.year, today.month, last_day, 23, 59, 59)
    cobrado_mes = (
        db.session.query(func.coalesce(func.sum(OrdenCompraCliente.total), 0.0))
        .filter(
            OrdenCompraCliente.estado == "pagada",
            OrdenCompraCliente.fecha_pago >= mes_inicio,
            OrdenCompraCliente.fecha_pago <= mes_fin,
        )
        .scalar()
    )
    return {
        "pendientes_entrega": int(pendientes or 0),
        "total_por_cobrar": float(por_cobrar or 0),
        "cobrado_mes": float(cobrado_mes or 0),
    }

@oc_clientes_bp.route("/")
@login_required
@permission_required("ver_oc_clientes")
def lista():
    estado_q = (request.args.get("estado") or "").strip().lower()
    cliente_q = (request.args.get("cliente") or "").strip()
    search_q = (request.args.get("q") or "").strip().upper()

    query = OrdenCompraCliente.query
    if estado_q in OC_ESTADOS:
        query = query.filter(OrdenCompraCliente.estado == estado_q)
    if cliente_q:
        query = query.join(Cliente, Cliente.id == OrdenCompraCliente.cliente_id).filter(
            Cliente.nombre.ilike(f"%{cliente_q}%")
        )
    if search_q:
        query = query.filter(
            or_(
                func.upper(OrdenCompraCliente.numero_oc).like(f"%{search_q}%"),
                func.upper(OrdenCompraCliente.numero_factura).like(f"%{search_q}%"),
            )
        )

    ordenes = query.order_by(OrdenCompraCliente.fecha_oc.desc(), OrdenCompraCliente.id.desc()).limit(300).all()
    clientes_map = {}
    if ordenes:
        cids = {o.cliente_id for o in ordenes if o.cliente_id}
        if cids:
            for c in Cliente.query.filter(Cliente.id.in_(cids)).all():
                clientes_map[c.id] = c

    filas = []
    hoy = date.today()
    for oc in ordenes:
        dias_entrega = None
        if (oc.estado or "") == "entregada" and oc.fecha_entrega_real:
            ref = oc.fecha_entrega_real.date() if isinstance(oc.fecha_entrega_real, datetime) else oc.fecha_entrega_real
            dias_entrega = max(0, (hoy - ref).days)
        cl = clientes_map.get(oc.cliente_id)
        filas.append(
            {
                "oc": oc,
                "cliente_nombre": cl.nombre if cl else "—",
                "estado_label": _estado_label(oc.estado),
                "badge": _estado_badge(oc.estado),
                "dias_entrega": dias_entrega,
            }
        )

    _partial = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    return render_template(
        "oc_clientes/lista.html",
        filas=filas,
        resumen=_build_list_summary(),
        filtros={"estado": estado_q, "cliente": cliente_q, "q": search_q},
        estados=OC_ESTADOS,
        estado_labels=OC_ESTADO_LABELS,
        puede_modificar=_can_modify(),
        active_page="oc_clientes",
        _partial=_partial,
    )


@oc_clientes_bp.route("/nueva", methods=["GET", "POST"])
@login_required
@permission_required("ver_oc_clientes")
def nueva():
    if request.method == "POST" and not _can_modify():
        return _deny("Sin permiso para crear órdenes de compra de clientes.")

    now = datetime.now()
    preload_id = _safe_int(request.args.get("cliente_id") or (request.form.get("cliente_id") if request.method == "POST" else "0"))
    selected_client = _client_by_id(preload_id) if preload_id > 0 else None
    party = _entity_snapshot(selected_client, False) if selected_client else {
        "name": "", "rut": "", "address": "", "telefono": "", "email": "",
    }

    form_data = {
        "numero_oc": "",
        "fecha_oc": now.strftime("%Y-%m-%d"),
        "fecha_entrega_comprometida": "",
        "forma_pago": "",
        "direccion_despacho": party.get("address") or "",
        "observaciones": "",
        "cliente_id": selected_client.id if selected_client else 0,
        "items": [],
        "totals": {"neto": 0, "iva": 0, "total": 0},
    }

    if request.method == "POST":
        form_data.update(
            {
                "numero_oc": (request.form.get("numero_oc") or "").strip(),
                "fecha_oc": (request.form.get("fecha_oc") or form_data["fecha_oc"]).strip(),
                "fecha_entrega_comprometida": (request.form.get("fecha_entrega_comprometida") or "").strip(),
                "forma_pago": (request.form.get("forma_pago") or "").strip(),
                "direccion_despacho": (request.form.get("direccion_despacho") or "").strip(),
                "observaciones": (request.form.get("observaciones") or "").strip(),
                "cliente_id": _safe_int(request.form.get("cliente_id")),
            }
        )
        items = _extract_items_from_form(request.form)
        form_data["items"] = items
        totals = calcular_totales_items(items)
        form_data["totals"] = totals

        errors = []
        numero_oc_raw = form_data["numero_oc"]
        if not numero_oc_raw:
            errors.append("El número de OC del cliente es obligatorio.")
        else:
            dup = buscar_oc_por_numero(numero_oc_raw)
            if dup:
                errors.append(
                    f"Ya existe una orden de compra con el número {numero_oc_raw} "
                    f"(registrada el {dup.fecha_oc.strftime('%d/%m/%Y') if dup.fecha_oc else '—'})."
                )
        if form_data["cliente_id"] <= 0 or _client_by_id(form_data["cliente_id"]) is None:
            errors.append("Debe seleccionar un cliente válido.")
        if not items:
            errors.append("Debe agregar al menos un ítem.")

        for err in errors:
            flash(err, "error")

        if not errors:
            try:
                cliente = _client_by_id(form_data["cliente_id"])
                oc = OrdenCompraCliente(
                    numero_oc=form_data["numero_oc"][:100],
                    cliente_id=cliente.id,
                    fecha_oc=_parse_date(form_data["fecha_oc"], now) or now,
                    fecha_entrega_comprometida=_parse_date(form_data["fecha_entrega_comprometida"]),
                    forma_pago=form_data["forma_pago"][:100] or None,
                    direccion_despacho=form_data["direccion_despacho"][:300] or None,
                    observaciones=form_data["observaciones"] or None,
                    estado="recibida",
                    neto=totals["neto"],
                    iva=totals["iva"],
                    total=totals["total"],
                    usuario=_current_user(),
                )
                db.session.add(oc)
                db.session.flush()
                for it in items:
                    oc.items.append(
                        OrdenCompraClienteItem(
                            codigo_producto=it["codigo_producto"],
                            descripcion=it["descripcion"],
                            marca=it["marca"] or None,
                            bodega=it["bodega"],
                            cantidad=it["cantidad"],
                            precio_unitario=it["precio_unitario"],
                            descuento_item=it["descuento_item"],
                            subtotal=it["subtotal"],
                            en_inventario=it["en_inventario"],
                        )
                    )
                db.session.commit()
                flash("Orden de compra del cliente registrada.", "success")
                return redirect(url_for("oc_clientes.detalle", oid=oc.id))
            except Exception as exc:
                db.session.rollback()
                flash(f"No se pudo guardar: {exc}", "error")

    elif selected_client:
        form_data["direccion_despacho"] = _full_address(selected_client) or party.get("address") or ""

    metodo_labels = METODO_PAGO_LABELS
    _partial = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    return render_template(
        "oc_clientes/form.html",
        form_data=form_data,
        party=party,
        selected_client=selected_client,
        metodo_pago_options=[(k, METODO_PAGO_LABELS.get(k, k)) for k in METODO_PAGO_OPTIONS if k != "saldo_favor"],
        url_producto=url_for("ventas.api_producto"),
        url_productos_search=url_for("ventas.api_productos_search"),
        url_clientes=url_for("ventas.api_clientes"),
        url_escanear_oc=url_for("oc_clientes.api_escanear_oc"),
        url_verificar_numero=url_for("oc_clientes.api_verificar_numero"),
        puede_modificar=_can_modify(),
        active_page="oc_clientes",
        _partial=_partial,
    )


@oc_clientes_bp.route("/<int:oid>")
@login_required
@permission_required("ver_oc_clientes")
def detalle(oid: int):
    oc = db.session.get(OrdenCompraCliente, oid)
    if oc is None:
        flash("Orden de compra no encontrada.", "error")
        return redirect(url_for("oc_clientes.lista"))

    cliente = db.session.get(Cliente, oc.cliente_id) if oc.cliente_id else None
    timeline = timeline_eventos(oc)
    _partial = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    return render_template(
        "oc_clientes/detalle.html",
        oc=oc,
        cliente=cliente,
        timeline=timeline,
        estado_label=_estado_label(oc.estado),
        badge=_estado_badge(oc.estado),
        metodo_labels=METODO_PAGO_LABELS,
        metodo_pago_options=[(k, METODO_PAGO_LABELS.get(k, k)) for k in METODO_PAGO_OPTIONS if k != "saldo_favor"],
        puede_modificar=_can_modify(),
        active_page="oc_clientes",
        _partial=_partial,
    )


@oc_clientes_bp.route("/<int:oid>/entregar", methods=["POST"])
@login_required
@permission_required("mod_oc_clientes")
def marcar_entregada(oid: int):
    oc = db.session.get(OrdenCompraCliente, oid)
    if oc is None:
        flash("Orden no encontrada.", "error")
        return redirect(url_for("oc_clientes.lista"))
    if (oc.estado or "") != "recibida":
        flash("Solo se puede marcar entregada una OC en estado recibida.", "error")
        return redirect(url_for("oc_clientes.detalle", oid=oid))

    fecha_raw = (request.form.get("fecha_entrega_real") or "").strip()
    fecha_entrega = _parse_date(fecha_raw, datetime.now()) or datetime.now()
    guia = (request.form.get("numero_guia_despacho") or "").strip()[:60]
    descontar = (request.form.get("descontar_stock") or "").strip().lower() in {"1", "on", "true", "yes"}

    try:
        if descontar:
            n_desc, n_omit, errors = descontar_stock_oc(oc, _current_user())
            if errors:
                raise ValueError("; ".join(errors))
            flash(
                f"Entrega registrada. Stock descontado en {n_desc} ítem(s); "
                f"{n_omit} ítem(s) omitidos (fuera de inventario).",
                "success",
            )
        else:
            flash("Entrega registrada sin descuento de stock.", "success")

        oc.estado = "entregada"
        oc.fecha_entrega_real = fecha_entrega
        oc.numero_guia_despacho = guia or None
        oc.updated_at = datetime.utcnow()
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        flash(f"No se pudo registrar la entrega: {exc}", "error")

    return redirect(url_for("oc_clientes.detalle", oid=oid))


@oc_clientes_bp.route("/<int:oid>/pago", methods=["POST"])
@login_required
@permission_required("mod_oc_clientes")
def registrar_pago(oid: int):
    oc = db.session.get(OrdenCompraCliente, oid)
    if oc is None:
        flash("Orden no encontrada.", "error")
        return redirect(url_for("oc_clientes.lista"))
    if (oc.estado or "") != "entregada":
        flash("Solo se puede registrar pago desde estado entregada.", "error")
        return redirect(url_for("oc_clientes.detalle", oid=oid))

    numero_factura = (request.form.get("numero_factura") or "").strip()
    fecha_pago = _parse_date((request.form.get("fecha_pago") or "").strip(), datetime.now()) or datetime.now()
    metodo = (request.form.get("metodo_pago") or "").strip().lower()

    if not numero_factura:
        flash("El número de factura es obligatorio para registrar el pago.", "error")
        return redirect(url_for("oc_clientes.detalle", oid=oid))
    if metodo not in METODO_PAGO_OPTIONS:
        flash("Método de pago inválido.", "error")
        return redirect(url_for("oc_clientes.detalle", oid=oid))

    oc.estado = "pagada"
    oc.numero_factura = numero_factura[:60]
    oc.fecha_pago = fecha_pago
    oc.metodo_pago = metodo
    oc.updated_at = datetime.utcnow()
    db.session.commit()
    flash("Pago registrado correctamente.", "success")
    return redirect(url_for("oc_clientes.detalle", oid=oid))


@oc_clientes_bp.route("/<int:oid>/anular", methods=["POST"])
@login_required
@permission_required("mod_oc_clientes")
def anular(oid: int):
    oc = db.session.get(OrdenCompraCliente, oid)
    if oc is None:
        flash("Orden no encontrada.", "error")
        return redirect(url_for("oc_clientes.lista"))
    if (oc.estado or "") != "recibida":
        flash(
            "Una OC entregada no puede anularse desde aquí; debe gestionarse como devolución.",
            "error",
        )
        return redirect(url_for("oc_clientes.detalle", oid=oid))

    oc.estado = "anulada"
    oc.updated_at = datetime.utcnow()
    db.session.commit()
    flash("Orden de compra anulada.", "success")
    return redirect(url_for("oc_clientes.detalle", oid=oid))


@oc_clientes_bp.route("/<int:oid>/imprimir")
@login_required
@permission_required("ver_oc_clientes")
def imprimir(oid: int):
    oc = db.session.get(OrdenCompraCliente, oid)
    if oc is None:
        flash("Orden no encontrada.", "error")
        return redirect(url_for("oc_clientes.lista"))
    cliente = db.session.get(Cliente, oc.cliente_id) if oc.cliente_id else None
    return render_template(
        "oc_clientes/imprimir.html",
        oc=oc,
        cliente=cliente,
        estado_label=_estado_label(oc.estado),
    )


_ALLOWED_SCAN_EXT = {".jpg", ".jpeg", ".png", ".pdf"}
_MAX_SCAN_BYTES = 12 * 1024 * 1024


@oc_clientes_bp.route("/api/verificar-numero")
@login_required
@permission_required("ver_oc_clientes")
def api_verificar_numero():
    """Indica si ya existe una OC con el mismo número."""
    q = (request.args.get("q") or request.args.get("numero_oc") or "").strip()
    if not q:
        return jsonify(ok=True, exists=False)
    existing = buscar_oc_por_numero(q)
    if existing is None:
        return jsonify(ok=True, exists=False)
    return jsonify(
        ok=True,
        exists=True,
        oc_id=existing.id,
        numero_oc=existing.numero_oc,
        estado=existing.estado,
        estado_label=_estado_label(existing.estado),
        fecha_oc=existing.fecha_oc.strftime("%d/%m/%Y") if existing.fecha_oc else None,
        detalle_url=url_for("oc_clientes.detalle", oid=existing.id),
    )


@oc_clientes_bp.route("/api/escanear", methods=["POST"])
@login_required
@permission_required("mod_oc_clientes")
def api_escanear_oc():
    """Escanea imagen o PDF de OC cliente y devuelve datos estructurados."""
    archivo = request.files.get("archivo") or request.files.get("file")
    if archivo is None or not (archivo.filename or "").strip():
        return jsonify(ok=False, error="Debe enviar un archivo (JPG, PNG o PDF)."), 400

    nombre = (archivo.filename or "").strip()
    ext = ("." + nombre.rsplit(".", 1)[-1].lower()) if "." in nombre else ""
    if ext not in _ALLOWED_SCAN_EXT:
        return jsonify(ok=False, error="Formato no soportado. Use JPG, PNG o PDF."), 400

    raw = archivo.read()
    if not raw:
        return jsonify(ok=False, error="El archivo está vacío."), 400
    if len(raw) > _MAX_SCAN_BYTES:
        return jsonify(ok=False, error="El archivo es demasiado grande (máx. 12 MB)."), 400

    try:
        data = escanear_oc(raw, nombre)
        return jsonify(ok=True, data=data)
    except ValueError as exc:
        msg = str(exc)
        if "credenciales" in msg.lower() or "vision" in msg.lower():
            return jsonify(
                ok=False,
                error=msg,
                hint="Verifique GOOGLE_VISION_CREDENTIALS y la cuenta de servicio de Google Cloud.",
            ), 503
        return jsonify(ok=False, error=msg), 400
    except Exception as exc:
        current_app.logger.exception("api_escanear_oc")
        return jsonify(ok=False, error=f"Error inesperado al escanear: {exc}"), 500
