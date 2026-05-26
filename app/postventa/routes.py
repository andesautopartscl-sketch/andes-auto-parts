from __future__ import annotations

import re

from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import func

from app.extensions import db
from app.utils.decorators import login_required
from app.utils.permissions import has_permission
from app.utils.rut_utils import clean_rut, format_rut, is_valid_rut
from app.utils.stock_control import get_product_history
from app.ventas.models import Cliente, DocumentoVenta, DocumentoVentaItem, NotaCredito
from .models import Garantia, ESTADOS_GARANTIA

postventa_bp = Blueprint(
    "postventa", __name__, url_prefix="/postventa",
    template_folder="../../templates"
)

@postventa_bp.before_request
def _postventa_module_guard():
    if "user" not in session:
        return None
    if has_permission(session.get("user"), session.get("rol"), "mod_postventa"):
        return None
    is_ajax = request.is_json or (request.headers.get("X-Requested-With") or "").lower() == "xmlhttprequest"
    if is_ajax or request.path.startswith("/postventa/api/"):
        return jsonify({"ok": False, "error": "Permiso denegado para módulo Postventa"}), 403
    flash("No tienes permisos para acceder al módulo Postventa.", "error")
    return redirect(url_for("productos.buscar"))


def _current_user() -> str:
    return session.get("user") or "sistema"


def _lookup_documento_por_numero(raw: str) -> DocumentoVenta | None:
    """Resuelve factura/boleta/orden por número mostrado al cliente (texto libre en formulario)."""
    if not raw or not str(raw).strip():
        return None
    t = str(raw).strip().upper()
    return (
        DocumentoVenta.query.filter(
            DocumentoVenta.tipo.in_(["factura", "boleta", "orden_venta"]),
            func.upper(DocumentoVenta.numero) == t,
        )
        .order_by(DocumentoVenta.id.desc())
        .first()
    )


def _rut_norm_sql_expr():
    return func.upper(func.replace(func.replace(func.coalesce(Cliente.rut, ""), ".", ""), "-", ""))


def _find_cliente_por_rut(rut_raw: str) -> Cliente | None:
    cr = clean_rut(rut_raw)
    if not cr:
        return None
    return (
        Cliente.query.filter_by(activo=True)
        .filter(_rut_norm_sql_expr() == cr.upper())
        .first()
    )


def _crear_cliente_minimo_postventa(nombre: str, rut_raw: str) -> Cliente | None:
    """Alta rápida en maestro (Chile); completar datos en Ventas &gt; Clientes si hace falta."""
    n = (nombre or "").strip()
    cr = clean_rut(rut_raw)
    if not n or not cr or not is_valid_rut(cr):
        return None
    ya = _find_cliente_por_rut(rut_raw)
    if ya:
        return ya
    c = Cliente(
        nombre=n[:200],
        rut=cr,
        pais="Chile",
        region="",
        comuna="",
        ciudad="",
        giro="",
        direccion="",
        telefono="",
        email="",
        activo=True,
    )
    db.session.add(c)
    db.session.flush()
    return c


def _match_linea_venta(documento_id: int, codigo_producto: str) -> DocumentoVentaItem | None:
    if not codigo_producto or not str(codigo_producto).strip():
        return None
    c = str(codigo_producto).strip().upper()
    return (
        DocumentoVentaItem.query.filter(
            DocumentoVentaItem.documento_id == documento_id,
            func.upper(DocumentoVentaItem.codigo_producto) == c,
        )
        .order_by(DocumentoVentaItem.id.asc())
        .first()
    )


@postventa_bp.route("/api/cliente-por-rut", methods=["GET"])
@login_required
def api_cliente_por_rut():
    rut = (request.args.get("rut") or "").strip()
    if not rut:
        return jsonify({"ok": False, "error": "Indique RUT."}), 400
    if not is_valid_rut(rut):
        return jsonify({"ok": False, "error": "RUT no válido."}), 400
    c = _find_cliente_por_rut(rut)
    if c is None:
        return jsonify({"ok": True, "encontrado": False})
    return jsonify(
        {
            "ok": True,
            "encontrado": True,
            "cliente": {
                "id": c.id,
                "nombre": c.nombre or "",
                "rut": format_rut(c.rut) if c.rut else "",
            },
        }
    )


@postventa_bp.route("/api/cliente-rapido", methods=["POST"])
@login_required
def api_cliente_rapido():
    if not has_permission(session.get("user"), session.get("rol"), "postventa_crear"):
        return jsonify({"ok": False, "error": "Sin permiso para crear clientes rápidos en Postventa."}), 403
    data = request.get_json(silent=True) or {}
    nombre = (data.get("nombre") or "").strip()
    rut = (data.get("rut") or "").strip()
    if not nombre:
        return jsonify({"ok": False, "error": "El nombre del cliente es obligatorio."}), 400
    if not rut or not is_valid_rut(rut):
        return jsonify({"ok": False, "error": "RUT no válido."}), 400
    exist = _find_cliente_por_rut(rut)
    if exist:
        return jsonify(
            {
                "ok": True,
                "creado": False,
                "cliente": {
                    "id": exist.id,
                    "nombre": exist.nombre or "",
                    "rut": format_rut(exist.rut) if exist.rut else "",
                },
            }
        )
    c = _crear_cliente_minimo_postventa(nombre, rut)
    if c is None:
        return jsonify({"ok": False, "error": "No se pudo crear el cliente."}), 400
    db.session.commit()
    return jsonify(
        {
            "ok": True,
            "creado": True,
            "cliente": {
                "id": c.id,
                "nombre": c.nombre or "",
                "rut": format_rut(c.rut) if c.rut else "",
            },
        }
    )


@postventa_bp.route("/api/buscar-documento", methods=["GET"])
@login_required
def api_buscar_documento():
    """Devuelve factura/boleta/orden y líneas con id para el formulario de garantía."""
    numero = (request.args.get("numero") or "").strip()
    if not numero:
        return jsonify({"ok": False, "error": "Indique el número de documento."}), 400
    doc = _lookup_documento_por_numero(numero)
    if doc is None:
        return jsonify({"ok": False, "error": "No se encontró un documento con ese número."}), 404
    items = []
    for it in sorted(doc.items or [], key=lambda x: x.id or 0):
        items.append(
            {
                "id": it.id,
                "codigo": (it.codigo_producto or "").strip().upper(),
                "descripcion": (it.descripcion or "").strip(),
                "cantidad": int(it.cantidad or 0),
                "marca": (it.marca or "").strip(),
                "bodega": (it.bodega or "").strip(),
            }
        )
    cr = doc.cliente_rut or ""
    return jsonify(
        {
            "ok": True,
            "documento": {
                "id": doc.id,
                "tipo": (doc.tipo or "").strip().lower(),
                "numero": (doc.numero or "").strip(),
                "cliente_id": doc.cliente_id,
                "cliente_nombre": (doc.cliente_nombre or "").strip(),
                "cliente_rut": format_rut(cr) if cr else "",
            },
            "items": items,
        }
    )


def _next_garantia_number() -> str:
    rows = db.session.query(Garantia.numero).filter(
        Garantia.numero.like("GR-%")
    ).all()
    nums = []
    for (num,) in rows:
        m = re.match(r"GR-(\d+)$", num or "")
        if m:
            nums.append(int(m.group(1)))
    next_num = max(nums, default=0) + 1
    return f"GR-{next_num:04d}"


@postventa_bp.route("/", methods=["GET"])
@login_required
def index():
    estado_filter = request.args.get("estado", "").strip()
    q = Garantia.query.order_by(Garantia.fecha.desc())
    if estado_filter and estado_filter in ESTADOS_GARANTIA:
        q = q.filter(Garantia.estado == estado_filter)
    garantias = q.all()
    return render_template(
        "postventa/index.html",
        garantias=garantias,
        estados=ESTADOS_GARANTIA,
        estado_filter=estado_filter,
        active_page="postventa",
    )


@postventa_bp.route("/nueva", methods=["POST"])
@login_required
def nueva():
    if not has_permission(session.get("user"), session.get("rol"), "postventa_crear"):
        flash("Sin permiso para crear garantías.", "error")
        return redirect(url_for("postventa.index"))
    estado = request.form.get("estado", "Pendiente").strip()
    if estado not in ESTADOS_GARANTIA:
        estado = "Pendiente"

    doc_num = request.form.get("documento_numero", "").strip()
    prod_cod = request.form.get("producto_codigo", "").strip().upper()
    prod_desc = request.form.get("producto_descripcion", "").strip()
    rut_snap = request.form.get("cliente_rut", "").strip()
    nombre_cli = request.form.get("cliente_nombre", "").strip()
    cliente_id_raw = request.form.get("cliente_id", "").strip()
    item_id_raw = request.form.get("documento_item_id", "").strip()

    cliente: Cliente | None = None
    if rut_snap and is_valid_rut(rut_snap):
        cliente = _find_cliente_por_rut(rut_snap)
    if cliente is None and cliente_id_raw.isdigit():
        cliente = db.session.get(Cliente, int(cliente_id_raw))
    if cliente is None and rut_snap and nombre_cli and is_valid_rut(rut_snap):
        cliente = _crear_cliente_minimo_postventa(nombre_cli, rut_snap)

    rut_guardado = ""
    if cliente:
        rut_guardado = format_rut(cliente.rut) if cliente.rut else ""
    elif rut_snap:
        rut_guardado = format_rut(rut_snap) or rut_snap.strip()

    doc = _lookup_documento_por_numero(doc_num) if doc_num else None
    linea: DocumentoVentaItem | None = None
    if item_id_raw.isdigit():
        cand = db.session.get(DocumentoVentaItem, int(item_id_raw))
        if cand is not None:
            if doc is not None and cand.documento_id != doc.id:
                cand = None
            elif doc is None:
                doc = db.session.get(DocumentoVenta, cand.documento_id)
            linea = cand

    nombre_final = nombre_cli or (cliente.nombre if cliente else "")
    garantia = Garantia(
        numero=_next_garantia_number(),
        cliente_id=cliente.id if cliente else None,
        cliente_nombre=nombre_final,
        cliente_rut=rut_guardado,
        producto_codigo=prod_cod,
        producto_descripcion=prod_desc,
        documento_numero=doc_num.upper() if doc_num else "",
        motivo=request.form.get("motivo", "").strip(),
        estado=estado,
        usuario=_current_user(),
    )

    if doc:
        garantia.documento_id = doc.id
        garantia.documento_numero = (doc.numero or "").strip().upper() or garantia.documento_numero
        if not (garantia.cliente_nombre or "").strip() and doc.cliente_nombre:
            garantia.cliente_nombre = (doc.cliente_nombre or "").strip()
        if not (garantia.cliente_rut or "").strip() and doc.cliente_rut:
            garantia.cliente_rut = format_rut(doc.cliente_rut) or ""
        if not garantia.cliente_id and doc.cliente_id:
            garantia.cliente_id = doc.cliente_id

    if linea:
        garantia.documento_item_id = linea.id
        garantia.producto_codigo = (linea.codigo_producto or "").strip().upper()
        garantia.producto_descripcion = (linea.descripcion or "").strip()
    elif doc and prod_cod:
        m = _match_linea_venta(doc.id, prod_cod)
        if m:
            garantia.documento_item_id = m.id

    db.session.add(garantia)
    db.session.commit()
    flash(f"Garantía {garantia.numero} registrada correctamente.", "success")
    return redirect(url_for("postventa.index"))


@postventa_bp.route("/<int:gid>/estado", methods=["POST"])
@login_required
def cambiar_estado(gid: int):
    if not has_permission(session.get("user"), session.get("rol"), "postventa_editar_estado"):
        return jsonify({"ok": False, "error": "Sin permiso para cambiar estado."}), 403
    g = db.session.get(Garantia, gid)
    if g is None:
        return jsonify({"ok": False, "error": "No encontrada"}), 404
    nuevo_estado = (request.get_json(force=True) or {}).get("estado", "").strip()
    if nuevo_estado not in ESTADOS_GARANTIA:
        return jsonify({"ok": False, "error": "Estado inválido"}), 400
    g.estado = nuevo_estado
    db.session.commit()
    return jsonify({"ok": True, "estado": g.estado})


@postventa_bp.route("/garantia/<int:gid>")
@login_required
def garantia_detalle(gid: int):
    g = db.session.get(Garantia, gid)
    if g is None:
        flash("Garantía no encontrada.", "error")
        return redirect(url_for("postventa.index"))

    documento = None
    linea = None
    notas_credito = []
    if g.documento_id:
        documento = db.session.get(DocumentoVenta, g.documento_id)
        notas_credito = (
            NotaCredito.query.filter_by(documento_venta_id=g.documento_id)
            .order_by(NotaCredito.fecha_documento.desc(), NotaCredito.id.desc())
            .all()
        )
    if g.documento_item_id:
        linea = db.session.get(DocumentoVentaItem, g.documento_item_id)
    elif g.documento_id and g.producto_codigo:
        linea = _match_linea_venta(g.documento_id, g.producto_codigo)

    historial_producto = None
    if (g.producto_codigo or "").strip():
        try:
            historial_producto = get_product_history(g.producto_codigo.strip(), limit=25)
        except Exception:
            historial_producto = None

    _partial = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    return render_template(
        "postventa/garantia_detalle.html",
        garantia=g,
        documento=documento,
        linea=linea,
        notas_credito=notas_credito,
        historial_producto=historial_producto,
        active_page="postventa",
        _partial=_partial,
    )


@postventa_bp.route("/garantia/<int:gid>/vincular-nc", methods=["POST"])
@login_required
def garantia_vincular_nc(gid: int):
    if not has_permission(session.get("user"), session.get("rol"), "postventa_vincular_nc"):
        flash("Sin permiso para vincular nota de crédito.", "error")
        return redirect(url_for("postventa.garantia_detalle", gid=gid))
    g = db.session.get(Garantia, gid)
    if g is None:
        flash("Garantía no encontrada.", "error")
        return redirect(url_for("postventa.index"))

    raw = (request.form.get("nota_credito_id") or "").strip()
    if not raw:
        g.nota_credito_id = None
        db.session.commit()
        flash("Se quitó la vinculación con la nota de crédito.", "success")
        return redirect(url_for("postventa.garantia_detalle", gid=gid))

    if not raw.isdigit():
        flash("Identificador de nota de crédito inválido.", "error")
        return redirect(url_for("postventa.garantia_detalle", gid=gid))

    nc_id = int(raw)
    nc = db.session.get(NotaCredito, nc_id)
    if nc is None:
        flash("Nota de crédito no encontrada.", "error")
        return redirect(url_for("postventa.garantia_detalle", gid=gid))

    if not g.documento_id or nc.documento_venta_id != g.documento_id:
        flash(
            "Esa nota de crédito no corresponde al mismo documento de venta vinculado a la garantía.",
            "error",
        )
        return redirect(url_for("postventa.garantia_detalle", gid=gid))

    g.nota_credito_id = nc_id
    db.session.commit()
    flash("Nota de crédito vinculada a la garantía.", "success")
    return redirect(url_for("postventa.garantia_detalle", gid=gid))


@postventa_bp.route("/<int:gid>/eliminar", methods=["POST"])
@login_required
def eliminar(gid: int):
    if not has_permission(session.get("user"), session.get("rol"), "postventa_eliminar"):
        flash("Sin permiso para eliminar garantías.", "error")
        return redirect(url_for("postventa.index"))
    g = db.session.get(Garantia, gid)
    if g:
        db.session.delete(g)
        db.session.commit()
        flash("Garantía eliminada.", "success")
    return redirect(url_for("postventa.index"))
