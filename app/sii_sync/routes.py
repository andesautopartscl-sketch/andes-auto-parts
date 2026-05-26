"""Rutas del módulo SII Sync — consulta y sincronización de DTE emitidos."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import func

from app.extensions import db
from app.sii_sync.models import SIIDocumento
from app.sii_sync.sii_service import (
    ContribuyenteNotFoundError,
    SIIService,
    SIIServiceError,
)
from app.sii_sync.sync_logic import sincronizar_periodo
from app.utils.decorators import login_required, permission_required
from app.utils.permissions import has_permission
from app.utils.rut_utils import clean_rut, is_valid_rut
from app.ventas.models import DocumentoVenta

sii_sync_bp = Blueprint(
    "sii_sync",
    __name__,
    url_prefix="/sii",
    template_folder="../../templates",
)


@sii_sync_bp.before_request
def _sii_module_guard():
    if "user" not in session:
        return None
    if request.endpoint == "sii_sync.api_contribuyente":
        return None
    if has_permission(session.get("user"), session.get("rol"), "mod_sii_sync"):
        return None
    is_ajax = request.is_json or (request.headers.get("X-Requested-With") or "").lower() == "xmlhttprequest"
    if is_ajax or request.path.startswith("/sii/api/"):
        return jsonify({"ok": False, "error": "Permiso denegado para módulo SII Sync"}), 403
    flash("No tienes permisos para acceder al módulo SII Sync.", "error")
    return redirect(url_for("productos.buscar"))


def _periodo_default() -> str:
    today = date.today()
    return f"{today.year:04d}-{today.month:02d}"


def _parse_periodo_form() -> str:
    mes = (request.args.get("mes") or request.form.get("mes") or "").strip()
    anio = (request.args.get("anio") or request.form.get("anio") or "").strip()
    if mes and anio:
        try:
            m = int(mes)
            y = int(anio)
            if 1 <= m <= 12 and 2000 <= y <= 2100:
                return f"{y:04d}-{m:02d}"
        except ValueError:
            pass
    p = (request.args.get("periodo") or "").strip()
    if p:
        return p
    return _periodo_default()


def _estado_badge_class(estado: str) -> str:
    e = (estado or "").upper()
    if e == "ACEPTADO":
        return "sii-badge sii-badge--ok"
    if e == "RECHAZADO":
        return "sii-badge sii-badge--err"
    return "sii-badge sii-badge--warn"


def _candidatos_documento_interno(doc: SIIDocumento, limit: int = 15) -> list[DocumentoVenta]:
    if not doc.fecha_emision and not doc.rut_receptor and not doc.monto_total:
        return []
    q = DocumentoVenta.query.filter(DocumentoVenta.tipo.in_(["factura", "boleta"]))
    rut_norm = clean_rut(doc.rut_receptor or "")
    if rut_norm:
        rut_expr = func.upper(
            func.replace(func.replace(func.coalesce(DocumentoVenta.cliente_rut, ""), ".", ""), "-", "")
        )
        q = q.filter(rut_expr == rut_norm.upper())
    if doc.fecha_emision:
        desde = doc.fecha_emision - timedelta(days=5)
        hasta = doc.fecha_emision + timedelta(days=5)
        q = q.filter(
            func.date(DocumentoVenta.fecha_documento) >= desde,
            func.date(DocumentoVenta.fecha_documento) <= hasta,
        )
    total = float(doc.monto_total or 0)
    if total > 0:
        tol = max(500.0, total * 0.02)
        q = q.filter(
            DocumentoVenta.total >= total - tol,
            DocumentoVenta.total <= total + tol,
        )
    return (
        q.order_by(DocumentoVenta.fecha_documento.desc(), DocumentoVenta.id.desc())
        .limit(limit)
        .all()
    )


@sii_sync_bp.route("/documentos", methods=["GET"])
@login_required
@permission_required("sii_ver")
def documentos():
    periodo = _parse_periodo_form()
    try:
        y, m = periodo.split("-")
        mes_sel = int(m)
        anio_sel = int(y)
    except ValueError:
        mes_sel = date.today().month
        anio_sel = date.today().year
        periodo = _periodo_default()

    tipo_f = (request.args.get("tipo_dte") or "").strip()
    estado_f = (request.args.get("estado_sii") or "").strip().upper()
    conc_f = (request.args.get("conciliado") or "").strip().lower()

    q = SIIDocumento.query.filter(SIIDocumento.periodo == periodo)
    if tipo_f:
        q = q.filter(SIIDocumento.tipo_dte == tipo_f)
    if estado_f:
        q = q.filter(SIIDocumento.estado_sii == estado_f)
    if conc_f == "si":
        q = q.filter(SIIDocumento.documento_venta_id.isnot(None))
    elif conc_f == "no":
        q = q.filter(SIIDocumento.documento_venta_id.is_(None))

    registros = (
        q.order_by(SIIDocumento.fecha_emision.desc(), SIIDocumento.folio.desc())
        .limit(500)
        .all()
    )

    tipos_disponibles = [
        row[0]
        for row in db.session.query(SIIDocumento.tipo_dte)
        .filter(SIIDocumento.periodo == periodo)
        .distinct()
        .order_by(SIIDocumento.tipo_dte)
        .all()
    ]

    puede_sincronizar = has_permission(session.get("user"), session.get("rol"), "sii_sincronizar")

    return render_template(
        "sii_sync/documentos.html",
        registros=registros,
        periodo=periodo,
        mes_sel=mes_sel,
        anio_sel=anio_sel,
        filtros={
            "tipo_dte": tipo_f,
            "estado_sii": estado_f,
            "conciliado": conc_f,
        },
        tipos_disponibles=tipos_disponibles,
        puede_sincronizar=puede_sincronizar,
        estado_badge_class=_estado_badge_class,
        active_page="sii_sync_documentos",
    )


@sii_sync_bp.route("/documentos/<int:doc_id>", methods=["GET"])
@login_required
@permission_required("sii_ver")
def documento_detalle(doc_id: int):
    doc = db.session.get(SIIDocumento, doc_id)
    if not doc:
        flash("Documento SII no encontrado.", "error")
        return redirect(url_for("sii_sync.documentos"))

    candidatos = []
    if not doc.documento_venta_id:
        candidatos = _candidatos_documento_interno(doc)

    doc_interno = None
    if doc.documento_venta_id:
        doc_interno = db.session.get(DocumentoVenta, doc.documento_venta_id)

    return render_template(
        "sii_sync/detalle.html",
        doc=doc,
        doc_interno=doc_interno,
        candidatos=candidatos,
        estado_badge_class=_estado_badge_class,
        active_page="sii_sync_documentos",
    )


@sii_sync_bp.route("/documentos/<int:doc_id>/vincular/<int:venta_id>", methods=["POST"])
@login_required
@permission_required("sii_ver")
def vincular_documento(doc_id: int, venta_id: int):
    doc = db.session.get(SIIDocumento, doc_id)
    venta = db.session.get(DocumentoVenta, venta_id)
    if not doc or not venta:
        flash("No se pudo vincular: registro no encontrado.", "error")
    else:
        doc.documento_venta_id = venta.id
        db.session.commit()
        flash("Documento interno vinculado correctamente.", "success")
    return redirect(url_for("sii_sync.documento_detalle", doc_id=doc_id))


def _contribuyente_lookup_allowed() -> bool:
    user = session.get("user")
    rol = session.get("rol")
    return (
        has_permission(user, rol, "sii_ver")
        or has_permission(user, rol, "mod_ventas")
        or has_permission(user, rol, "ventas_guardar_documento")
    )


@sii_sync_bp.route("/api/contribuyente", methods=["GET"])
@login_required
def api_contribuyente():
    """Autocompletar datos tributarios desde SII (BaseAPI) para formularios de terceros."""
    if not _contribuyente_lookup_allowed():
        return jsonify({"error": "Permiso denegado"}), 403

    rut = (request.args.get("rut") or "").strip()
    if not rut:
        return jsonify({"error": "RUT requerido"}), 400
    if not is_valid_rut(rut):
        return jsonify({"error": "RUT inválido"}), 400

    svc = SIIService()
    if not svc.contribuyente_lookup_ready():
        return jsonify({"error": "SII no configurado"}), 503

    try:
        data = svc.consultar_contribuyente(rut)
        resp = jsonify({**data, "cached": bool(svc._contribuyente_cache_hit)})
        if svc._contribuyente_cache_hit:
            resp.headers["X-SII-Cache"] = "HIT"
        return resp
    except ContribuyenteNotFoundError:
        return jsonify({"error": "RUT no encontrado"}), 404
    except SIIServiceError as exc:
        msg = str(exc)
        if "inválido" in msg.lower():
            return jsonify({"error": msg}), 400
        if "no encontrado" in msg.lower() or "no existe" in msg.lower():
            return jsonify({"error": "RUT no encontrado"}), 404
        return jsonify({"error": msg}), 502


@sii_sync_bp.route("/api/sincronizar", methods=["POST"])
@login_required
@permission_required("sii_sincronizar")
def api_sincronizar():
    data = request.get_json(silent=True) or {}
    periodo = (data.get("periodo") or request.form.get("periodo") or "").strip()
    if not periodo:
        periodo = _parse_periodo_form()
    resultado = sincronizar_periodo(periodo)
    ok = resultado.get("errores", 0) == 0 or (
        resultado.get("nuevos", 0) + resultado.get("actualizados", 0) > 0
    )
    return jsonify(
        {
            "ok": ok,
            "nuevos": resultado.get("nuevos", 0),
            "actualizados": resultado.get("actualizados", 0),
            "errores": resultado.get("errores", 0),
            "mensaje": resultado.get("mensaje", ""),
            "periodo": periodo,
        }
    ), (200 if ok else 400)
