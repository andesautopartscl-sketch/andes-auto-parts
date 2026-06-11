from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import date, datetime
from types import SimpleNamespace

from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for, Response
from sqlalchemy import and_, func, or_

from app.bodega.models import IngresoDocumento, IngresoDocumentoItem
from app.extensions import db
from app.utils.decorators import login_required, permission_required
from app.utils.permissions import has_permission
from app.utils.rut_utils import clean_rut, format_rut
from .models import CuentaContable, EmisorContable, MovimientoContable, TIPOS_CUENTA
from .emisores_service import (
    backfill_emisores_desde_movimientos,
    emisor_form_data,
    emisor_to_form_dict,
    hydrate_emisor,
    resolve_emisor_por_rut,
    upsert_emisor_contable_desde_movimiento,
    validate_emisor_form,
)
from app.ventas.routes import _chile_regions, _load_chile_geo
from app.ventas.models import DocumentoVenta

contabilidad_bp = Blueprint(
    "contabilidad", __name__, url_prefix="/contabilidad",
    template_folder="../../templates"
)

finanzas_bp = Blueprint(
    "finanzas", __name__, url_prefix="/finanzas",
    template_folder="../../templates"
)


def _current_user() -> str:
    return session.get("user") or "sistema"


def _origenes_a_etiqueta(origins: set[str]) -> str:
    """Etiqueta de compra por líneas de ingreso (nacional / importación)."""
    nat = "nacional" in origins
    imp = "importacion" in origins
    if nat and imp:
        return "Mixto"
    if imp:
        return "Importación"
    return "Nacional"


def _totales_libro_compra_ingreso(doc: IngresoDocumento, neto_sum: float) -> tuple[float, float, float]:
    """Neto desde líneas; total/IVA desde factura física si existe, si no IVA 19 % sobre neto."""
    neto = float(neto_sum or 0)
    if doc.total_factura is not None:
        total = float(doc.total_factura)
        if doc.iva_factura is not None:
            iva = float(doc.iva_factura)
        else:
            iva = round(total - neto, 2)
    else:
        iva = round(neto * 0.19, 2)
        total = round(neto + iva, 2)
    return neto, iva, total


@contabilidad_bp.route("/")
@login_required
@permission_required("ver_finanzas")
def index():
    cuentas = CuentaContable.query.filter_by(activo=True).order_by(
        CuentaContable.tipo, CuentaContable.codigo
    ).all()
    return render_template(
        "contabilidad/index.html",
        cuentas=cuentas,
        tipos_cuenta=TIPOS_CUENTA,
        active_page="contabilidad",
    )


@contabilidad_bp.route("/cuentas/nueva", methods=["POST"])
@login_required
@permission_required("ver_finanzas")
def cuenta_nueva():
    if not has_permission(session.get("user"), session.get("rol"), "finanzas_gestion_cuentas"):
        flash("No tienes permiso para crear cuentas contables.", "error")
        return redirect(url_for("contabilidad.index"))
    codigo = request.form.get("codigo", "").strip().upper()
    nombre = request.form.get("nombre", "").strip()
    tipo = request.form.get("tipo", "").strip()
    descripcion = request.form.get("descripcion", "").strip()

    if not codigo or not nombre:
        flash("Código y nombre son obligatorios.", "error")
        return redirect(url_for("contabilidad.index"))
    if tipo not in TIPOS_CUENTA:
        flash("Tipo de cuenta inválido.", "error")
        return redirect(url_for("contabilidad.index"))
    existing = CuentaContable.query.filter_by(codigo=codigo).first()
    if existing:
        flash(f"Ya existe una cuenta con el código {codigo}.", "error")
        return redirect(url_for("contabilidad.index"))

    cuenta = CuentaContable(
        codigo=codigo, nombre=nombre, tipo=tipo, descripcion=descripcion
    )
    db.session.add(cuenta)
    db.session.commit()
    flash(f"Cuenta {codigo} creada correctamente.", "success")
    return redirect(url_for("contabilidad.index"))


@contabilidad_bp.route("/cuentas/<int:cid>/toggle", methods=["POST"])
@login_required
@permission_required("ver_finanzas")
def cuenta_toggle(cid: int):
    if not has_permission(session.get("user"), session.get("rol"), "finanzas_gestion_cuentas"):
        flash("No tienes permiso para activar/inactivar cuentas.", "error")
        return redirect(url_for("contabilidad.index"))
    cuenta = db.session.get(CuentaContable, cid)
    if cuenta:
        cuenta.activo = not cuenta.activo
        db.session.commit()
    return redirect(url_for("contabilidad.index"))


@contabilidad_bp.route("/cuentas/<int:cid>/editar", methods=["POST"])
@login_required
@permission_required("ver_finanzas")
def cuenta_editar(cid: int):
    if not has_permission(session.get("user"), session.get("rol"), "finanzas_gestion_cuentas"):
        flash("No tienes permiso para editar cuentas contables.", "error")
        return redirect(url_for("contabilidad.index"))

    cuenta = db.session.get(CuentaContable, cid)
    if cuenta is None:
        flash("Cuenta contable no encontrada.", "error")
        return redirect(url_for("contabilidad.index"))

    codigo = request.form.get("codigo", "").strip().upper()
    nombre = request.form.get("nombre", "").strip()
    tipo = request.form.get("tipo", "").strip()
    descripcion = request.form.get("descripcion", "").strip()

    if not codigo or not nombre:
        flash("Código y nombre son obligatorios.", "error")
        return redirect(url_for("contabilidad.index"))
    if tipo not in TIPOS_CUENTA:
        flash("Tipo de cuenta inválido.", "error")
        return redirect(url_for("contabilidad.index"))

    duplicate = (
        CuentaContable.query
        .filter(CuentaContable.codigo == codigo, CuentaContable.id != cuenta.id)
        .first()
    )
    if duplicate:
        flash(f"Ya existe otra cuenta con el código {codigo}.", "error")
        return redirect(url_for("contabilidad.index"))

    cuenta.codigo = codigo
    cuenta.nombre = nombre
    cuenta.tipo = tipo
    cuenta.descripcion = descripcion
    db.session.commit()
    flash(f"Cuenta {codigo} actualizada correctamente.", "success")
    return redirect(url_for("contabilidad.index"))


@contabilidad_bp.route("/movimientos", methods=["GET"])
@login_required
@permission_required("ver_finanzas")
def movimientos():
    cuenta_id = request.args.get("cuenta_id", "").strip()
    q = MovimientoContable.query.order_by(MovimientoContable.fecha.desc(), MovimientoContable.id.desc())
    if cuenta_id and cuenta_id.isdigit():
        q = q.filter(MovimientoContable.cuenta_id == int(cuenta_id))
    movs = q.limit(200).all()
    cuentas = CuentaContable.query.filter_by(activo=True).order_by(CuentaContable.codigo).all()
    return render_template(
        "contabilidad/movimientos.html",
        movimientos=movs,
        cuentas=cuentas,
        cuenta_id_filter=cuenta_id,
        active_page="contabilidad_movimientos",
    )


@contabilidad_bp.route("/movimientos/nuevo", methods=["POST"])
@login_required
@permission_required("ver_finanzas")
def movimiento_nuevo():
    if not has_permission(session.get("user"), session.get("rol"), "finanzas_registrar_movimientos"):
        flash("No tienes permiso para registrar movimientos contables.", "error")
        return redirect(url_for("contabilidad.movimientos"))
    cuenta_id = request.form.get("cuenta_id", "").strip()
    tipo = request.form.get("tipo", "").strip().lower()
    monto_raw = request.form.get("monto", "0").strip().replace(",", ".")
    descripcion = request.form.get("descripcion", "").strip()
    documento_ref = request.form.get("documento_ref", "").strip()
    emisor_nombre = (request.form.get("emisor_nombre") or "").strip().upper()[:200]
    emisor_rut_raw = (request.form.get("emisor_rut") or "").strip()[:24]
    emisor_rut_cr = clean_rut(emisor_rut_raw)
    emisor_rut = (format_rut(emisor_rut_cr) or emisor_rut_cr)[:24] if emisor_rut_cr else ""
    fecha_str = request.form.get("fecha", "").strip()

    if not cuenta_id or not cuenta_id.isdigit():
        flash("Cuenta inválida.", "error")
        return redirect(url_for("contabilidad.movimientos"))
    if tipo not in ("debe", "haber"):
        flash("Tipo debe ser 'debe' o 'haber'.", "error")
        return redirect(url_for("contabilidad.movimientos"))
    try:
        monto = float(monto_raw)
        if monto <= 0:
            raise ValueError
    except ValueError:
        flash("Monto inválido.", "error")
        return redirect(url_for("contabilidad.movimientos"))

    cuenta = db.session.get(CuentaContable, int(cuenta_id))
    if cuenta is None:
        flash("Cuenta no encontrada.", "error")
        return redirect(url_for("contabilidad.movimientos"))

    fecha = date.today()
    if fecha_str:
        try:
            fecha = date.fromisoformat(fecha_str)
        except ValueError:
            pass

    mov = MovimientoContable(
        fecha=fecha,
        cuenta_id=int(cuenta_id),
        tipo=tipo,
        monto=monto,
        descripcion=descripcion,
        documento_ref=documento_ref,
        emisor_nombre=emisor_nombre,
        emisor_rut=emisor_rut,
        usuario=_current_user(),
    )
    db.session.add(mov)
    upsert_emisor_contable_desde_movimiento(emisor_rut, emisor_nombre)
    db.session.commit()
    flash("Movimiento registrado.", "success")
    return redirect(url_for("contabilidad.movimientos"))


@contabilidad_bp.route("/api/libro_diario")
@login_required
@permission_required("ver_finanzas")
def api_libro_diario():
    desde_str = request.args.get("desde", "").strip()
    hasta_str = request.args.get("hasta", "").strip()
    q = MovimientoContable.query.order_by(MovimientoContable.fecha, MovimientoContable.id)
    if desde_str:
        try:
            q = q.filter(MovimientoContable.fecha >= date.fromisoformat(desde_str))
        except ValueError:
            pass
    if hasta_str:
        try:
            q = q.filter(MovimientoContable.fecha <= date.fromisoformat(hasta_str))
        except ValueError:
            pass
    movs = q.all()
    return jsonify([{
        "id": m.id,
        "fecha": m.fecha.isoformat(),
        "cuenta_codigo": m.cuenta.codigo if m.cuenta else "",
        "cuenta_nombre": m.cuenta.nombre if m.cuenta else "",
        "tipo": m.tipo,
        "monto": m.monto,
        "descripcion": m.descripcion,
        "documento_ref": m.documento_ref,
        "emisor_nombre": m.emisor_nombre or "",
        "emisor_rut": m.emisor_rut or "",
        "usuario": m.usuario,
    } for m in movs])


@contabilidad_bp.route("/api/emisor-por-rut", methods=["GET"])
@login_required
@permission_required("ver_finanzas")
def api_emisor_por_rut():
    """Autocompletar emisor: directorio contable o último movimiento del libro diario."""
    rut = (request.args.get("rut") or "").strip()
    if not rut:
        return jsonify({"ok": False, "error": "Indique RUT."}), 400
    cr = clean_rut(rut)
    if len(cr) < 7:
        return jsonify({"ok": True, "encontrado": False})
    data = resolve_emisor_por_rut(rut)
    if data is None:
        return jsonify({"ok": True, "encontrado": False})
    return jsonify({"ok": True, "encontrado": True, **data})


@contabilidad_bp.route("/emisores", methods=["GET"])
@login_required
@permission_required("ver_finanzas")
def emisores_lista():
    q_raw = (request.args.get("q") or "").strip()
    q = EmisorContable.query.filter_by(activo=True).order_by(EmisorContable.nombre)
    if q_raw:
        term = f"%{q_raw}%"
        cr = clean_rut(q_raw)
        filtros = [
            EmisorContable.nombre.ilike(term),
            EmisorContable.email.ilike(term),
            EmisorContable.comuna.ilike(term),
        ]
        if cr:
            filtros.append(EmisorContable.rut.ilike(f"%{cr}%"))
        q = q.filter(or_(*filtros))
    emisores = q.limit(300).all()
    puede_registrar_mov = has_permission(
        session.get("user"), session.get("rol"), "finanzas_registrar_movimientos"
    )
    return render_template(
        "contabilidad/emisores.html",
        emisores=emisores,
        q=q_raw,
        puede_registrar_mov=puede_registrar_mov,
        active_page="contabilidad_emisores",
    )


@contabilidad_bp.route("/emisores/nuevo", methods=["GET", "POST"])
@login_required
@permission_required("ver_finanzas")
def emisor_nuevo():
    if request.method == "POST" and not has_permission(
        session.get("user"), session.get("rol"), "finanzas_registrar_movimientos"
    ):
        flash("No tienes permiso para crear emisores contables.", "error")
        return redirect(url_for("contabilidad.emisores_lista"))
    form_data = emisor_form_data(request.form if request.method == "POST" else None)
    errors: list[str] = []
    if request.method == "POST":
        errors = validate_emisor_form(form_data)
        if not errors:
            emisor = hydrate_emisor(EmisorContable(), form_data)
            db.session.add(emisor)
            db.session.commit()
            flash("Emisor contable creado.", "success")
            return redirect(url_for("contabilidad.emisores_lista"))
        for err in errors:
            flash(err, "error")
    return render_template(
        "contabilidad/emisor_form.html",
        form_title="Nuevo emisor contable",
        submit_label="Crear emisor",
        emisor=form_data,
        validation_errors=errors,
        chile_geo=_load_chile_geo(),
        chile_regions=_chile_regions(_load_chile_geo()),
        active_page="contabilidad_emisores",
    )


@contabilidad_bp.route("/emisores/<int:eid>/editar", methods=["GET", "POST"])
@login_required
@permission_required("ver_finanzas")
def emisor_editar(eid: int):
    if request.method == "POST" and not has_permission(
        session.get("user"), session.get("rol"), "finanzas_registrar_movimientos"
    ):
        flash("No tienes permiso para editar emisores contables.", "error")
        return redirect(url_for("contabilidad.emisores_lista"))
    emisor = db.session.get(EmisorContable, eid)
    if emisor is None or not emisor.activo:
        flash("Emisor no encontrado.", "error")
        return redirect(url_for("contabilidad.emisores_lista"))
    errors: list[str] = []
    if request.method == "POST":
        form_data = emisor_form_data(request.form)
        errors = validate_emisor_form(form_data, emisor_id=eid)
        if not errors:
            hydrate_emisor(emisor, form_data)
            db.session.commit()
            flash("Emisor actualizado.", "success")
            return redirect(url_for("contabilidad.emisores_lista"))
        for err in errors:
            flash(err, "error")
    else:
        form_data = emisor_to_form_dict(emisor)
    chile_geo = _load_chile_geo()
    return render_template(
        "contabilidad/emisor_form.html",
        form_title="Editar emisor contable",
        submit_label="Guardar cambios",
        emisor=form_data,
        emisor_id=eid,
        validation_errors=errors,
        chile_geo=chile_geo,
        chile_regions=_chile_regions(chile_geo),
        active_page="contabilidad_emisores",
    )


@contabilidad_bp.route("/asientos", methods=["GET"])
@login_required
@permission_required("ver_finanzas")
def asientos():
    cuenta_id = request.args.get("cuenta_id", "").strip()
    q = MovimientoContable.query.order_by(MovimientoContable.fecha.desc(), MovimientoContable.id.desc())
    if cuenta_id and cuenta_id.isdigit():
        q = q.filter(MovimientoContable.cuenta_id == int(cuenta_id))
    movs = q.limit(300).all()
    cuentas = CuentaContable.query.filter_by(activo=True).order_by(CuentaContable.codigo).all()
    return render_template(
        "contabilidad/asientos.html",
        asientos=movs,
        cuentas=cuentas,
        cuenta_id_filter=cuenta_id,
        active_page="contabilidad_asientos",
    )


@contabilidad_bp.route("/libro-mayor", methods=["GET"])
@login_required
@permission_required("ver_finanzas")
def libro_mayor():
    cuentas = CuentaContable.query.order_by(CuentaContable.codigo).all()
    movs = MovimientoContable.query.order_by(MovimientoContable.fecha.asc(), MovimientoContable.id.asc()).all()

    resumen = {
        c.id: {
            "cuenta": c,
            "debe": 0.0,
            "haber": 0.0,
        }
        for c in cuentas
    }
    for m in movs:
        if m.cuenta_id not in resumen:
            continue
        if m.tipo == "debe":
            resumen[m.cuenta_id]["debe"] += float(m.monto or 0)
        else:
            resumen[m.cuenta_id]["haber"] += float(m.monto or 0)

    rows = []
    for c in cuentas:
        item = resumen[c.id]
        saldo = item["debe"] - item["haber"]
        rows.append(
            {
                "cuenta": c,
                "debe": item["debe"],
                "haber": item["haber"],
                "saldo": saldo,
            }
        )

    return render_template(
        "contabilidad/libro_mayor.html",
        rows=rows,
        active_page="contabilidad_libro_mayor",
    )


@contabilidad_bp.route("/cuentas-por-pagar", methods=["GET"])
@login_required
@permission_required("ver_finanzas")
def cuentas_por_pagar():
    docs = (
        DocumentoVenta.query
        .filter(
            and_(
                DocumentoVenta.tipo.in_(["orden_compra", "factura"]),
                DocumentoVenta.estado_pago != "pagado",
            )
        )
        .order_by(DocumentoVenta.fecha_documento.desc(), DocumentoVenta.id.desc())
        .limit(300)
        .all()
    )
    return render_template(
        "contabilidad/cuentas_por_pagar.html",
        documentos=docs,
        active_page="contabilidad_cxp",
    )


@contabilidad_bp.route("/cuentas-por-cobrar", methods=["GET"])
@login_required
@permission_required("ver_finanzas")
def cuentas_por_cobrar():
    docs = (
        DocumentoVenta.query
        .filter(
            and_(
                DocumentoVenta.tipo.in_(["factura", "boleta", "orden_venta"]),
                DocumentoVenta.estado_pago != "pagado",
            )
        )
        .order_by(DocumentoVenta.fecha_documento.desc(), DocumentoVenta.id.desc())
        .limit(300)
        .all()
    )
    return render_template(
        "contabilidad/cuentas_por_cobrar.html",
        documentos=docs,
        active_page="contabilidad_cxc",
    )


@contabilidad_bp.route("/libro-ventas", methods=["GET"])
@login_required
@permission_required("ver_finanzas")
def libro_ventas():
    docs = (
        DocumentoVenta.query
        .filter(DocumentoVenta.tipo.in_(["factura", "boleta", "orden_venta"]))
        .order_by(DocumentoVenta.fecha_documento.desc(), DocumentoVenta.id.desc())
        .limit(300)
        .all()
    )
    return render_template(
        "contabilidad/libro_ventas.html",
        documentos=docs,
        active_page="contabilidad_libro_ventas",
    )


@contabilidad_bp.route("/libro-compras", methods=["GET"])
@login_required
@permission_required("ver_finanzas")
def libro_compras():
    """
    Libro de compras: solo documentos de **ingreso de bodega** (compra a proveedor con stock).
    No incluye facturas de venta (FA-… en ventas); el número mostrado es el N° de factura/guía del proveedor.
    """
    rut_q = request.args.get("rut", "").strip()
    proveedor_q = request.args.get("proveedor", "").strip()
    numero_q = request.args.get("numero", "").strip()
    tipo_q = request.args.get("tipo", "").strip().lower()
    estado_pago_q = request.args.get("estado_pago", "").strip().lower()
    desde_str = request.args.get("desde", "").strip()
    hasta_str = request.args.get("hasta", "").strip()
    limit_str = request.args.get("limit", "300").strip()

    q = IngresoDocumento.query.filter(or_(IngresoDocumento.anulado.is_(False), IngresoDocumento.anulado.is_(None)))

    if rut_q:
        q = q.filter(IngresoDocumento.proveedor_rut.ilike(f"%{rut_q}%"))
    if proveedor_q:
        q = q.filter(IngresoDocumento.proveedor_nombre.ilike(f"%{proveedor_q}%"))
    if numero_q:
        q = q.filter(IngresoDocumento.numero_documento.ilike(f"%{numero_q}%"))

    if tipo_q in {"nacional", "importacion"}:
        q = q.filter(
            IngresoDocumento.id.in_(
                db.session.query(IngresoDocumentoItem.ingreso_documento_id).filter(
                    IngresoDocumentoItem.origen_compra == tipo_q
                )
            )
        )

    if estado_pago_q == "pagado":
        q = q.filter(
            and_(IngresoDocumento.metodo_pago.isnot(None), func.trim(IngresoDocumento.metodo_pago) != "")
        )
    elif estado_pago_q == "pendiente":
        q = q.filter(
            or_(IngresoDocumento.metodo_pago.is_(None), func.trim(IngresoDocumento.metodo_pago) == "")
        )

    if desde_str:
        try:
            d0 = date.fromisoformat(desde_str[:10])
            q = q.filter(IngresoDocumento.fecha_documento >= d0)
        except ValueError:
            pass
    if hasta_str:
        try:
            d1 = date.fromisoformat(hasta_str[:10])
            q = q.filter(IngresoDocumento.fecha_documento <= d1)
        except ValueError:
            pass

    try:
        limit = max(50, min(2000, int(limit_str or "300")))
    except ValueError:
        limit = 300
    export = request.args.get("export", "").strip().lower()

    raw_docs = (
        q.order_by(IngresoDocumento.fecha_documento.desc(), IngresoDocumento.id.desc())
        .limit(limit)
        .all()
    )

    doc_ids = [d.id for d in raw_docs]
    neto_por_doc: dict[int, float] = {}
    origenes_por_doc: dict[int, set[str]] = defaultdict(set)
    if doc_ids:
        for rid, neto_sum in (
            db.session.query(
                IngresoDocumentoItem.ingreso_documento_id,
                func.coalesce(func.sum(IngresoDocumentoItem.valor_neto), 0.0),
            )
            .filter(IngresoDocumentoItem.ingreso_documento_id.in_(doc_ids))
            .group_by(IngresoDocumentoItem.ingreso_documento_id)
            .all()
        ):
            neto_por_doc[int(rid)] = float(neto_sum or 0)
        for rid, orig in (
            db.session.query(IngresoDocumentoItem.ingreso_documento_id, IngresoDocumentoItem.origen_compra)
            .filter(IngresoDocumentoItem.ingreso_documento_id.in_(doc_ids))
            .all()
        ):
            origenes_por_doc[int(rid)].add((orig or "nacional").strip().lower() or "nacional")

    def _fila_namespace(doc: IngresoDocumento) -> SimpleNamespace:
        neto = neto_por_doc.get(doc.id, 0.0)
        neto_v, iva_v, total_v = _totales_libro_compra_ingreso(doc, neto)
        lbl = _origenes_a_etiqueta(origenes_por_doc.get(doc.id, {"nacional"}))
        mp = (doc.metodo_pago or "").strip()
        ep = "pagado" if mp else "pendiente"
        return SimpleNamespace(
            fecha_documento=doc.fecha_documento,
            tipo=lbl,
            tipo_origen=lbl,
            numero=doc.numero_documento,
            cliente_nombre=doc.proveedor_nombre,
            estado_pago=ep,
            subtotal=neto_v,
            impuesto=iva_v,
            total=total_v,
            metodo_pago=mp or "—",
        )

    docs = [_fila_namespace(d) for d in raw_docs]

    if export in {"csv", "excel"}:
        if export == "csv":
            sio = io.StringIO()
            writer = csv.writer(sio)
            writer.writerow(
                ["Fecha", "Origen (compra)", "N° documento proveedor", "Proveedor", "Estado pago", "Neto", "IVA", "Total"]
            )
            for d in docs:
                writer.writerow(
                    [
                        d.fecha_documento.strftime("%d/%m/%Y") if d.fecha_documento else "",
                        d.tipo_origen or "",
                        d.numero or "",
                        d.cliente_nombre or "",
                        (d.estado_pago or "pendiente"),
                        float(d.subtotal or 0),
                        float(d.impuesto or 0),
                        float(d.total or 0),
                    ]
                )
            content = sio.getvalue()
            filename = f"libro_compras_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            return Response(
                content,
                mimetype="text/csv; charset=utf-8",
                headers={"Content-Disposition": f"attachment; filename={filename}"},
            )

        lines = ["Fecha\tOrigen (compra)\tN° documento proveedor\tProveedor\tEstado pago\tNeto\tIVA\tTotal"]
        for d in docs:
            lines.append(
                "\t".join(
                    [
                        d.fecha_documento.strftime("%d/%m/%Y") if d.fecha_documento else "",
                        str(d.tipo_origen or ""),
                        str(d.numero or ""),
                        str(d.cliente_nombre or ""),
                        str(d.estado_pago or "pendiente"),
                        str(float(d.subtotal or 0)),
                        str(float(d.impuesto or 0)),
                        str(float(d.total or 0)),
                    ]
                )
            )
        content = "\n".join(lines)
        filename = f"libro_compras_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xls"
        return Response(
            content,
            mimetype="application/vnd.ms-excel; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    resumen = {
        "cantidad": len(docs),
        "neto": sum(float(d.subtotal or 0) for d in docs),
        "iva": sum(float(d.impuesto or 0) for d in docs),
        "total": sum(float(d.total or 0) for d in docs),
        "pendientes": sum(1 for d in docs if (d.estado_pago or "").lower() != "pagado"),
        "pagadas": sum(1 for d in docs if (d.estado_pago or "").lower() == "pagado"),
    }

    return render_template(
        "contabilidad/libro_compras.html",
        documentos=docs,
        resumen=resumen,
        filtros={
            "rut": rut_q,
            "proveedor": proveedor_q,
            "numero": numero_q,
            "tipo": tipo_q,
            "estado_pago": estado_pago_q,
            "desde": desde_str,
            "hasta": hasta_str,
            "limit": limit,
        },
        active_page="contabilidad_libro_compras",
    )


@contabilidad_bp.route("/iva", methods=["GET"])
@login_required
@permission_required("ver_finanzas")
def iva():
    docs = (
        DocumentoVenta.query
        .filter(DocumentoVenta.tipo.in_(["factura", "boleta", "orden_venta", "orden_compra"]))
        .order_by(DocumentoVenta.fecha_documento.desc(), DocumentoVenta.id.desc())
        .limit(400)
        .all()
    )
    total_debito = sum(float(d.impuesto or 0) for d in docs if d.tipo in {"factura", "boleta", "orden_venta"})
    total_credito = sum(float(d.impuesto or 0) for d in docs if d.tipo in {"orden_compra"})

    return render_template(
        "contabilidad/iva.html",
        documentos=docs,
        total_debito=total_debito,
        total_credito=total_credito,
        saldo_iva=(total_debito - total_credito),
        active_page="contabilidad_iva",
    )


@contabilidad_bp.route("/reportes", methods=["GET"])
@login_required
@permission_required("ver_finanzas")
def reportes_financieros():
    cuentas = CuentaContable.query.filter_by(activo=True).all()
    movs = MovimientoContable.query.all()

    total_debe = sum(float(m.monto or 0) for m in movs if m.tipo == "debe")
    total_haber = sum(float(m.monto or 0) for m in movs if m.tipo == "haber")

    activos = sum(1 for c in cuentas if (c.tipo or "").lower() == "activo")
    pasivos = sum(1 for c in cuentas if (c.tipo or "").lower() == "pasivo")
    ingresos = sum(1 for c in cuentas if (c.tipo or "").lower() == "ingreso")
    egresos = sum(1 for c in cuentas if (c.tipo or "").lower() in {"egreso", "costo"})

    return render_template(
        "contabilidad/reportes_financieros.html",
        total_debe=total_debe,
        total_haber=total_haber,
        diferencia=(total_debe - total_haber),
        activos=activos,
        pasivos=pasivos,
        ingresos=ingresos,
        egresos=egresos,
        active_page="contabilidad_reportes",
    )


@contabilidad_bp.route("/balance-general", methods=["GET"])
@login_required
@permission_required("ver_finanzas")
def balance_general():
    cuentas = CuentaContable.query.filter_by(activo=True).all()
    movs = MovimientoContable.query.all()

    by_cuenta = {}
    for c in cuentas:
        by_cuenta[c.id] = {"cuenta": c, "debe": 0.0, "haber": 0.0}

    for m in movs:
        bucket = by_cuenta.get(m.cuenta_id)
        if not bucket:
            continue
        if m.tipo == "debe":
            bucket["debe"] += float(m.monto or 0)
        else:
            bucket["haber"] += float(m.monto or 0)

    activos = []
    pasivos = []
    patrimonio = []
    for item in by_cuenta.values():
        c = item["cuenta"]
        saldo = item["debe"] - item["haber"]
        row = {"cuenta": c, "saldo": saldo}
        tipo = (c.tipo or "").lower()
        if tipo == "activo":
            activos.append(row)
        elif tipo == "pasivo":
            pasivos.append(row)
        elif tipo == "patrimonio":
            patrimonio.append(row)

    total_activos = sum(r["saldo"] for r in activos)
    total_pasivos = sum(r["saldo"] for r in pasivos)
    total_patrimonio = sum(r["saldo"] for r in patrimonio)

    return render_template(
        "contabilidad/balance_general.html",
        activos=activos,
        pasivos=pasivos,
        patrimonio=patrimonio,
        total_activos=total_activos,
        total_pasivos=total_pasivos,
        total_patrimonio=total_patrimonio,
        active_page="contabilidad_balance_general",
    )


@contabilidad_bp.route("/estado-resultados", methods=["GET"])
@login_required
@permission_required("ver_finanzas")
def estado_resultados():
    cuentas = CuentaContable.query.filter_by(activo=True).all()
    movs = MovimientoContable.query.all()

    by_cuenta = {}
    for c in cuentas:
        by_cuenta[c.id] = {"cuenta": c, "debe": 0.0, "haber": 0.0}

    for m in movs:
        bucket = by_cuenta.get(m.cuenta_id)
        if not bucket:
            continue
        if m.tipo == "debe":
            bucket["debe"] += float(m.monto or 0)
        else:
            bucket["haber"] += float(m.monto or 0)

    ingresos = []
    egresos = []
    for item in by_cuenta.values():
        c = item["cuenta"]
        tipo = (c.tipo or "").lower()
        if tipo == "ingreso":
            monto = item["haber"] - item["debe"]
            ingresos.append({"cuenta": c, "monto": monto})
        elif tipo in {"egreso", "costo"}:
            monto = item["debe"] - item["haber"]
            egresos.append({"cuenta": c, "monto": monto})

    total_ingresos = sum(r["monto"] for r in ingresos)
    total_egresos = sum(r["monto"] for r in egresos)
    utilidad = total_ingresos - total_egresos

    return render_template(
        "contabilidad/estado_resultados.html",
        ingresos=ingresos,
        egresos=egresos,
        total_ingresos=total_ingresos,
        total_egresos=total_egresos,
        utilidad=utilidad,
        active_page="contabilidad_estado_resultados",
    )


@finanzas_bp.route("/")
@login_required
@permission_required("ver_finanzas")
def finanzas_home():
    return redirect(url_for("finanzas.plan_cuentas"))


finanzas_bp.add_url_rule("/plan_cuentas", endpoint="plan_cuentas", view_func=index)
finanzas_bp.add_url_rule("/libro_diario", endpoint="libro_diario", view_func=movimientos)
finanzas_bp.add_url_rule("/emisores", endpoint="emisores", view_func=emisores_lista)
finanzas_bp.add_url_rule("/emisores/nuevo", endpoint="emisor_nuevo", view_func=emisor_nuevo)
finanzas_bp.add_url_rule("/emisores/<int:eid>/editar", endpoint="emisor_editar", view_func=emisor_editar)
finanzas_bp.add_url_rule("/libro_mayor", endpoint="libro_mayor", view_func=libro_mayor)
finanzas_bp.add_url_rule("/asientos", endpoint="asientos", view_func=asientos)
finanzas_bp.add_url_rule("/cxp", endpoint="cxp", view_func=cuentas_por_pagar)
finanzas_bp.add_url_rule("/cxc", endpoint="cxc", view_func=cuentas_por_cobrar)
finanzas_bp.add_url_rule("/balance", endpoint="balance", view_func=balance_general)
finanzas_bp.add_url_rule("/resultados", endpoint="resultados", view_func=estado_resultados)
