from __future__ import annotations

from datetime import date, datetime

from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import and_

from app.extensions import db
from app.utils.decorators import login_required, permission_required
from .models import CuentaContable, MovimientoContable, TIPOS_CUENTA
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
    cuenta = db.session.get(CuentaContable, cid)
    if cuenta:
        cuenta.activo = not cuenta.activo
        db.session.commit()
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
    cuenta_id = request.form.get("cuenta_id", "").strip()
    tipo = request.form.get("tipo", "").strip().lower()
    monto_raw = request.form.get("monto", "0").strip().replace(",", ".")
    descripcion = request.form.get("descripcion", "").strip()
    documento_ref = request.form.get("documento_ref", "").strip()
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
        usuario=_current_user(),
    )
    db.session.add(mov)
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
        "usuario": m.usuario,
    } for m in movs])


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
    docs = (
        DocumentoVenta.query
        .filter(DocumentoVenta.tipo.in_(["orden_compra", "factura"]))
        .order_by(DocumentoVenta.fecha_documento.desc(), DocumentoVenta.id.desc())
        .limit(300)
        .all()
    )
    return render_template(
        "contabilidad/libro_compras.html",
        documentos=docs,
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
finanzas_bp.add_url_rule("/libro_mayor", endpoint="libro_mayor", view_func=libro_mayor)
finanzas_bp.add_url_rule("/asientos", endpoint="asientos", view_func=asientos)
finanzas_bp.add_url_rule("/cxp", endpoint="cxp", view_func=cuentas_por_pagar)
finanzas_bp.add_url_rule("/cxc", endpoint="cxc", view_func=cuentas_por_cobrar)
finanzas_bp.add_url_rule("/balance", endpoint="balance", view_func=balance_general)
finanzas_bp.add_url_rule("/resultados", endpoint="resultados", view_func=estado_resultados)
