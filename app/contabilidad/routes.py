from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for, Response
from sqlalchemy import and_, func, or_
from werkzeug.security import check_password_hash

from app.bodega.models import IngresoDocumento, IngresoDocumentoItem, MovimientoStock
from app.extensions import db
from app.seguridad.models import Usuario
from app.utils.datetime_utils import (
    chile_day_end_exclusive_utc,
    chile_day_start_utc,
    chile_today,
    format_utc_to_chile,
    now_chile,
    utc_to_chile,
)
from app.utils.decorators import login_required, permission_required
from app.utils.permissions import has_permission
from app.utils.rut_utils import clean_rut, format_rut
from app.ventas.models import DocumentoVenta, DocumentoVentaItem
from .models import CuentaContable, EmisorContable, MovimientoContable, TIPOS_CUENTA
from .emisores_service import (
    backfill_emisores_desde_movimientos,
    buscar_emisores,
    emisor_form_data,
    emisor_to_form_dict,
    hydrate_emisor,
    listar_descripciones_movimientos,
    resolve_emisor_por_rut,
    upsert_emisor_contable_desde_movimiento,
    validate_emisor_form,
)
from app.ventas.routes import _chile_regions, _load_chile_geo, METODO_PAGO_LABELS, METODO_PAGO_OPTIONS

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


def _costo_unitario_desde_ingresos(
    codigo: str,
    marca: str | None = None,
    bodega: str | None = None,
    *,
    cache: dict | None = None,
) -> float | None:
    """Costo neto unitario desde ingresos (promedio ponderado). Prioriza misma marca/bodega."""
    code = (codigo or "").strip().upper()
    if not code:
        return None
    marca_n = (marca or "").strip().upper()
    bodega_n = (bodega or "").strip()
    cache_key = (code, marca_n, bodega_n)
    if cache is not None and cache_key in cache:
        return cache[cache_key]

    q = (
        IngresoDocumentoItem.query.filter(
            func.upper(func.trim(IngresoDocumentoItem.codigo_producto)) == code,
            IngresoDocumentoItem.valor_neto.isnot(None),
            IngresoDocumentoItem.valor_neto > 0,
            IngresoDocumentoItem.cantidad > 0,
        )
        .order_by(IngresoDocumentoItem.id.desc())
        .limit(80)
    )
    items = q.all()
    if marca_n:
        con_marca = [
            it for it in items if (it.marca or "").strip().upper() == marca_n
        ]
        if con_marca:
            items = con_marca
    if bodega_n:
        con_bodega = [
            it for it in items if (it.bodega or "").strip() == bodega_n
        ]
        if con_bodega:
            items = con_bodega

    total_qty = 0
    total_val = 0.0
    for it in items:
        qty = int(it.cantidad or 0)
        vn = float(it.valor_neto or 0)
        if qty > 0 and vn > 0:
            total_qty += qty
            total_val += vn * qty
    result = round(total_val / total_qty, 2) if total_qty > 0 and total_val > 0 else None
    if cache is not None:
        cache[cache_key] = result
    return result


def _enrich_venta_operativa_row(m: MovimientoStock, *, costo_cache: dict | None = None) -> dict:
    """Arma fila de gestión: venta, costo y ganancia (neto)."""
    qty = abs(int(m.cantidad or 0))
    bruto = float(m.total_neto) if m.total_neto is not None else (
        qty * float(m.precio_venta_neto or 0)
    )
    is_dev = (m.motivo_codigo or "") == "devolucion_venta" or int(m.cantidad or 0) > 0
    sign = -1.0 if is_dev else 1.0
    venta_neto = round(sign * bruto, 2)
    costo_u = _costo_unitario_desde_ingresos(
        m.codigo_producto or "",
        m.marca,
        m.bodega,
        cache=costo_cache,
    )
    costo_total = round(sign * (float(costo_u) * qty), 2) if costo_u is not None and qty else None
    ganancia = round(venta_neto - costo_total, 2) if costo_total is not None else None
    margen_pct = None
    if ganancia is not None and abs(venta_neto) > 0.009:
        margen_pct = round(100.0 * ganancia / abs(venta_neto), 1)
    iva_rate = 0.19
    iva = round(venta_neto * iva_rate, 2)
    total = round(venta_neto + iva, 2)
    fecha_local = utc_to_chile(m.fecha) if m.fecha else None
    return {
        "mov": m,
        "fecha": m.fecha,
        "fecha_local": fecha_local,
        "fecha_short": fecha_local.strftime("%d/%m %H:%M") if fecha_local else None,
        "fecha_title": format_utc_to_chile(m.fecha, default="") if m.fecha else "",
        "is_dev": is_dev,
        "tipo": "DEVOLUCION_AJUSTE" if is_dev else "VENTA_AJUSTE",
        "documento": (m.ref_sii or f"AJ-{m.id}"),
        "codigo": m.codigo_producto or "",
        "marca": m.marca or "",
        "bodega": m.bodega or "",
        "cantidad": qty,
        "cantidad_signed": int(sign * qty),
        "precio_venta_neto": float(m.precio_venta_neto) if m.precio_venta_neto is not None else None,
        "venta_neto": venta_neto,
        "costo_unitario": costo_u,
        "costo_neto": costo_total,
        "ganancia_neta": ganancia,
        "margen_pct": margen_pct,
        "iva": iva,
        "total": total,
        "usuario": m.usuario or "",
        "observacion": m.observacion or "",
        "ref_sii": m.ref_sii or "",
        "detalle": f"{m.codigo_producto}"
        + (f" · {m.marca}" if m.marca else "")
        + (f" · {qty} u." if qty else ""),
    }


def _validar_autorizacion_edicion_movimiento(
    username: str, password: str
) -> tuple[bool, str, Usuario | None]:
    """Usuario + clave de quien autoriza editar un movimiento del libro diario."""
    user_name = (username or "").strip()
    raw_pass = password or ""
    if not user_name or not raw_pass:
        return False, "Debe ingresar usuario y contraseña para autorizar la edición.", None

    u = Usuario.query.filter_by(usuario=user_name).first()
    if u is None:
        return False, "Usuario de autorización no válido.", None
    if not bool(u.activo):
        return False, "El usuario de autorización está inactivo.", None
    if bool(getattr(u, "bloqueado_seguridad", False)):
        return False, "El usuario de autorización está bloqueado.", None

    try:
        ok = check_password_hash(u.password_hash or "", raw_pass)
    except Exception:
        ok = False
    if not ok:
        return False, "Contraseña de autorización incorrecta.", None

    rol_name = (u.rol.nombre if getattr(u, "rol", None) and u.rol.nombre else "") or ""
    if not has_permission(u.usuario, rol_name, "finanzas_registrar_movimientos"):
        return False, "El usuario no tiene permiso para editar movimientos contables.", None
    return True, "", u


def _parse_movimiento_form(form) -> tuple[dict | None, str | None]:
    """Parsea y valida el formulario de movimiento. Retorna (data, error)."""
    cuenta_id = (form.get("cuenta_id") or "").strip()
    tipo = (form.get("tipo") or "").strip().lower()
    monto_raw = (form.get("monto") or "0").strip().replace(",", ".")
    descripcion = (form.get("descripcion") or "").strip()[:300]
    documento_ref = (form.get("documento_ref") or "").strip()[:60]
    emisor_nombre = (form.get("emisor_nombre") or "").strip().upper()[:200]
    emisor_rut_raw = (form.get("emisor_rut") or "").strip()[:24]
    emisor_rut_cr = clean_rut(emisor_rut_raw)
    emisor_rut = (format_rut(emisor_rut_cr) or emisor_rut_cr)[:24] if emisor_rut_cr else ""
    fecha_str = (form.get("fecha") or "").strip()
    hora_str = (form.get("hora") or "").strip()

    if not cuenta_id or not cuenta_id.isdigit():
        return None, "Cuenta inválida."
    if tipo not in ("debe", "haber"):
        return None, "Tipo debe ser 'debe' o 'haber'."
    try:
        monto = float(monto_raw)
        if monto <= 0:
            raise ValueError
    except ValueError:
        return None, "Monto inválido."

    cuenta = db.session.get(CuentaContable, int(cuenta_id))
    if cuenta is None:
        return None, "Cuenta no encontrada."

    now = now_chile().replace(tzinfo=None)
    dia = chile_today()
    if fecha_str:
        try:
            dia = date.fromisoformat(fecha_str[:10])
        except ValueError:
            pass

    hh, mm, ss = now.hour, now.minute, now.second
    if hora_str:
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                t = datetime.strptime(hora_str, fmt).time()
                hh, mm, ss = t.hour, t.minute, t.second
                break
            except ValueError:
                continue

    fecha = datetime(dia.year, dia.month, dia.day, hh, mm, ss, microsecond=0)

    return {
        "fecha": fecha,
        "cuenta_id": int(cuenta_id),
        "tipo": tipo,
        "monto": monto,
        "descripcion": descripcion,
        "documento_ref": documento_ref,
        "emisor_nombre": emisor_nombre,
        "emisor_rut": emisor_rut,
    }, None


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
    puede_registrar_mov = has_permission(
        session.get("user"), session.get("rol"), "finanzas_registrar_movimientos"
    )
    return render_template(
        "contabilidad/movimientos.html",
        movimientos=movs,
        cuentas=cuentas,
        cuenta_id_filter=cuenta_id,
        descripciones_opciones=listar_descripciones_movimientos(),
        puede_registrar_mov=puede_registrar_mov,
        active_page="contabilidad_movimientos",
    )


@contabilidad_bp.route("/movimientos/nuevo", methods=["POST"])
@login_required
@permission_required("ver_finanzas")
def movimiento_nuevo():
    if not has_permission(session.get("user"), session.get("rol"), "finanzas_registrar_movimientos"):
        flash("No tienes permiso para registrar movimientos contables.", "error")
        return redirect(url_for("contabilidad.movimientos"))

    data, err = _parse_movimiento_form(request.form)
    if err:
        flash(err, "error")
        return redirect(url_for("contabilidad.movimientos"))

    mov = MovimientoContable(
        fecha=data["fecha"],
        cuenta_id=data["cuenta_id"],
        tipo=data["tipo"],
        monto=data["monto"],
        descripcion=data["descripcion"],
        documento_ref=data["documento_ref"],
        emisor_nombre=data["emisor_nombre"],
        emisor_rut=data["emisor_rut"],
        usuario=_current_user(),
    )
    db.session.add(mov)
    upsert_emisor_contable_desde_movimiento(data["emisor_rut"], data["emisor_nombre"])
    db.session.commit()
    flash("Movimiento registrado.", "success")
    return redirect(url_for("contabilidad.movimientos"))


@contabilidad_bp.route("/api/movimientos/autorizar-edicion", methods=["POST"])
@login_required
@permission_required("ver_finanzas")
def api_autorizar_edicion_movimiento():
    """Valida usuario/clave antes de abrir el formulario de edición."""
    if not has_permission(session.get("user"), session.get("rol"), "finanzas_registrar_movimientos"):
        return jsonify(ok=False, error="No tienes permiso para editar movimientos."), 403
    payload = request.get_json(silent=True) or {}
    auth_user = (payload.get("auth_user") or request.form.get("auth_user") or "").strip()
    auth_pass = payload.get("auth_password") or request.form.get("auth_password") or ""
    auth_ok, auth_err, actor = _validar_autorizacion_edicion_movimiento(auth_user, auth_pass)
    if not auth_ok:
        return jsonify(ok=False, error=auth_err), 403
    return jsonify(ok=True, actor=(actor.usuario if actor else auth_user))


@contabilidad_bp.route("/movimientos/<int:mid>/editar", methods=["POST"])
@login_required
@permission_required("ver_finanzas")
def movimiento_editar(mid: int):
    if not has_permission(session.get("user"), session.get("rol"), "finanzas_registrar_movimientos"):
        flash("No tienes permiso para editar movimientos contables.", "error")
        return redirect(url_for("contabilidad.movimientos"))

    auth_user = (request.form.get("auth_user") or "").strip()
    auth_pass = request.form.get("auth_password") or ""
    auth_ok, auth_err, _actor = _validar_autorizacion_edicion_movimiento(auth_user, auth_pass)
    if not auth_ok:
        flash(auth_err, "error")
        return redirect(url_for("contabilidad.movimientos"))

    mov = db.session.get(MovimientoContable, mid)
    if mov is None:
        flash("Movimiento no encontrado.", "error")
        return redirect(url_for("contabilidad.movimientos"))

    data, err = _parse_movimiento_form(request.form)
    if err:
        flash(err, "error")
        return redirect(url_for("contabilidad.movimientos"))

    mov.fecha = data["fecha"]
    mov.cuenta_id = data["cuenta_id"]
    mov.tipo = data["tipo"]
    mov.monto = data["monto"]
    mov.descripcion = data["descripcion"]
    mov.documento_ref = data["documento_ref"]
    mov.emisor_nombre = data["emisor_nombre"]
    mov.emisor_rut = data["emisor_rut"]
    upsert_emisor_contable_desde_movimiento(data["emisor_rut"], data["emisor_nombre"])
    db.session.commit()
    flash("Movimiento actualizado.", "success")
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
            d0 = date.fromisoformat(desde_str[:10])
            q = q.filter(MovimientoContable.fecha >= datetime.combine(d0, time.min))
        except ValueError:
            pass
    if hasta_str:
        try:
            d1 = date.fromisoformat(hasta_str[:10])
            q = q.filter(
                MovimientoContable.fecha < datetime.combine(d1, time.min) + timedelta(days=1)
            )
        except ValueError:
            pass
    movs = q.all()
    return jsonify([{
        "id": m.id,
        "fecha": m.fecha.isoformat() if m.fecha else None,
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


@contabilidad_bp.route("/api/emisores-buscar", methods=["GET"])
@login_required
@permission_required("ver_finanzas")
def api_emisores_buscar():
    """Buscar emisores por RUT o razón social (lupa del libro diario)."""
    q = (request.args.get("q") or "").strip()
    try:
        limit = int(request.args.get("limit") or 15)
    except (TypeError, ValueError):
        limit = 15
    items = buscar_emisores(q, limit=limit)
    return jsonify({"ok": True, "items": items, "q": q})


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


def _dias_desde_fecha_documento(fecha: datetime | None) -> int:
    if fecha is None:
        return 0
    ref = fecha.date() if isinstance(fecha, datetime) else fecha
    return max(0, (date.today() - ref).days)


def _plazo_credito_dias(metodo_pago: str) -> int | None:
    mp = (metodo_pago or "").strip().lower()
    return {"credito_30": 30, "credito_60": 60, "credito_90": 90}.get(mp)


def _metodo_pago_cxc_label(metodo_pago: str) -> str:
    mp = (metodo_pago or "").strip().lower()
    if mp in {"credito_30", "credito_60", "credito_90"}:
        return METODO_PAGO_LABELS.get(mp, mp)
    if mp:
        return METODO_PAGO_LABELS.get(mp, mp.replace("_", " ").title())
    return "—"


def _documento_cxc_vencido(doc: DocumentoVenta, dias: int) -> bool:
    plazo = _plazo_credito_dias(doc.metodo_pago or "")
    if plazo is None:
        return False
    return dias > plazo


@contabilidad_bp.route("/cuentas-por-cobrar", methods=["GET"])
@login_required
@permission_required("ver_finanzas")
def cuentas_por_cobrar():
    cliente_q = (request.args.get("cliente") or "").strip()
    tipo_q = (request.args.get("tipo") or "").strip().lower()

    query = DocumentoVenta.query.filter(
        and_(
            DocumentoVenta.tipo.in_(["factura", "boleta", "orden_venta"]),
            DocumentoVenta.estado_pago != "pagado",
        )
    )
    if cliente_q:
        query = query.filter(DocumentoVenta.cliente_nombre.ilike(f"%{cliente_q}%"))
    if tipo_q in {"factura", "boleta", "orden_venta"}:
        query = query.filter(DocumentoVenta.tipo == tipo_q)

    docs = (
        query.order_by(DocumentoVenta.fecha_documento.asc(), DocumentoVenta.id.asc())
        .limit(500)
        .all()
    )

    filas = []
    resumen_por_cliente: dict[str, dict] = {}
    for d in docs:
        dias = _dias_desde_fecha_documento(d.fecha_documento)
        vencido = _documento_cxc_vencido(d, dias)
        cliente = (d.cliente_nombre or "—").strip() or "—"
        total = float(d.total or 0)
        filas.append(
            SimpleNamespace(
                id=d.id,
                fecha_documento=d.fecha_documento,
                tipo=d.tipo,
                numero=d.numero,
                numero_oc_cliente=(getattr(d, "numero_oc_cliente", None) or "").strip() or "—",
                cliente_nombre=cliente,
                total=total,
                dias=dias,
                metodo_pago_label=_metodo_pago_cxc_label(d.metodo_pago or ""),
                vencido=vencido,
                estado_pago=d.estado_pago or "pendiente",
            )
        )
        bucket = resumen_por_cliente.setdefault(
            cliente,
            {"cliente": cliente, "total": 0.0, "deuda_antigua_dias": 0},
        )
        bucket["total"] += total
        bucket["deuda_antigua_dias"] = max(bucket["deuda_antigua_dias"], dias)

    resumen_clientes = sorted(
        resumen_por_cliente.values(),
        key=lambda x: (-x["total"], -x["deuda_antigua_dias"], x["cliente"]),
    )

    puede_registrar_pago = has_permission(
        session.get("user"), session.get("rol"), "mod_finanzas"
    )

    return render_template(
        "contabilidad/cuentas_por_cobrar.html",
        documentos=filas,
        resumen_clientes=resumen_clientes,
        filtros={"cliente": cliente_q, "tipo": tipo_q},
        metodo_pago_options=[
            {"value": k, "label": METODO_PAGO_LABELS[k]}
            for k in METODO_PAGO_OPTIONS
            if k != "saldo_favor"
        ],
        puede_registrar_pago=puede_registrar_pago,
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

    iva_rate = 0.19
    ops_raw = (
        MovimientoStock.query.filter(
            MovimientoStock.es_venta_operativa.is_(True),
            MovimientoStock.tipo == "ajuste",
        )
        .order_by(MovimientoStock.fecha.desc(), MovimientoStock.id.desc())
        .limit(400)
        .all()
    )
    costo_cache: dict = {}
    operativas = []
    op_neto = 0.0
    op_costo = 0.0
    op_ganancia = 0.0
    for m in ops_raw:
        row = _enrich_venta_operativa_row(m, costo_cache=costo_cache)
        op_neto += float(row["venta_neto"] or 0)
        if row["costo_neto"] is not None:
            op_costo += float(row["costo_neto"])
        if row["ganancia_neta"] is not None:
            op_ganancia += float(row["ganancia_neta"])
        operativas.append(row)
    op_iva = round(op_neto * iva_rate, 2)
    op_total = round(op_neto + op_iva, 2)
    op_margen_pct = round(100.0 * op_ganancia / abs(op_neto), 1) if abs(op_neto) > 0.009 else None

    return render_template(
        "contabilidad/libro_ventas.html",
        documentos=docs,
        operativas=operativas,
        op_neto=op_neto,
        op_iva=op_iva,
        op_total=op_total,
        op_costo=op_costo,
        op_ganancia=op_ganancia,
        op_margen_pct=op_margen_pct,
        active_page="contabilidad_libro_ventas",
    )


def _parse_date_arg(raw: str, fallback: date | None = None) -> date | None:
    text = (raw or "").strip()
    if not text:
        return fallback
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return fallback


def _match_docs_sii_por_refs(refs: list[str]) -> dict[str, list[dict]]:
    """Cruce liviano: ref_sii ↔ número de DocumentoVenta (ERP), si existe."""
    cleaned = sorted({(r or "").strip() for r in refs if (r or "").strip()})
    if not cleaned:
        return {}
    out: dict[str, list[dict]] = {r: [] for r in cleaned}
    # Buscar coincidencias parciales por número
    q = DocumentoVenta.query.filter(
        DocumentoVenta.numero.isnot(None),
        DocumentoVenta.numero != "",
    )
    # Limitar: solo docs cuyo número aparece en alguna ref
    clauses = []
    for ref in cleaned[:80]:
        clauses.append(DocumentoVenta.numero.ilike(f"%{ref}%"))
        if hasattr(DocumentoVenta, "numero_oc_cliente"):
            clauses.append(DocumentoVenta.numero_oc_cliente.ilike(f"%{ref}%"))
    if not clauses:
        return out
    docs = q.filter(or_(*clauses)).order_by(DocumentoVenta.fecha_documento.desc()).limit(200).all()
    for d in docs:
        num = (d.numero or "").strip().upper()
        oc = (getattr(d, "numero_oc_cliente", None) or "").strip().upper()
        for ref in cleaned:
            ru = ref.upper()
            if (num and (ru in num or num in ru)) or (oc and (ru in oc or oc in ru)):
                out[ref].append(
                    {
                        "id": d.id,
                        "tipo": d.tipo,
                        "numero": d.numero,
                        "fecha": d.fecha_documento.isoformat() if d.fecha_documento else None,
                        "total": float(d.total or 0),
                    }
                )
    return out


@contabilidad_bp.route("/ventas-operativas", methods=["GET"])
@login_required
@permission_required("ver_finanzas")
def ventas_operativas():
    """Informe de gestión: ajustes marcados como venta (no sustituye libro SII)."""
    today = chile_today()
    desde = _parse_date_arg(request.args.get("desde", ""), today.replace(day=1))
    hasta = _parse_date_arg(request.args.get("hasta", ""), today)
    codigo = (request.args.get("codigo") or "").strip().upper()
    marca = (request.args.get("marca") or "").strip()
    bodega = (request.args.get("bodega") or "").strip()
    usuario = (request.args.get("usuario") or "").strip()
    export = (request.args.get("export") or "").strip().lower()

    q = MovimientoStock.query.filter(
        MovimientoStock.es_venta_operativa.is_(True),
        MovimientoStock.tipo == "ajuste",
    )
    # fecha en DB es UTC; filtrar por día civil Chile (evita perder ventas nocturnas).
    if desde:
        q = q.filter(MovimientoStock.fecha >= chile_day_start_utc(desde))
    if hasta:
        q = q.filter(MovimientoStock.fecha < chile_day_end_exclusive_utc(hasta))
    if codigo:
        q = q.filter(MovimientoStock.codigo_producto.ilike(f"%{codigo}%"))
    if marca:
        q = q.filter(MovimientoStock.marca.ilike(f"%{marca}%"))
    if bodega:
        q = q.filter(MovimientoStock.bodega.ilike(f"%{bodega}%"))
    if usuario:
        q = q.filter(MovimientoStock.usuario.ilike(f"%{usuario}%"))

    rows_raw = q.order_by(MovimientoStock.fecha.desc(), MovimientoStock.id.desc()).limit(2000).all()

    costo_cache: dict = {}
    rows = [_enrich_venta_operativa_row(r, costo_cache=costo_cache) for r in rows_raw]

    unidades_venta = 0
    unidades_dev = 0
    total_ventas = 0.0
    total_devs = 0.0
    total_costo = 0.0
    total_ganancia = 0.0
    n_con_costo = 0
    for row in rows:
        qty = int(row["cantidad"] or 0)
        if row["is_dev"]:
            unidades_dev += qty
            total_devs += abs(float(row["venta_neto"] or 0))
        else:
            unidades_venta += qty
            total_ventas += abs(float(row["venta_neto"] or 0))
        if row["costo_neto"] is not None:
            total_costo += float(row["costo_neto"])
            n_con_costo += 1
        if row["ganancia_neta"] is not None:
            total_ganancia += float(row["ganancia_neta"])
    unidades = unidades_venta - unidades_dev
    total_neto = total_ventas - total_devs
    n_mov = len(rows)
    ticket_prom = (total_ventas / max(1, sum(1 for r in rows if not r["is_dev"]))) if rows else 0.0
    iva_rate = 0.19
    total_iva = round(total_neto * iva_rate, 2)
    total_con_iva = round(total_neto + total_iva, 2)
    margen_pct = round(100.0 * total_ganancia / abs(total_neto), 1) if abs(total_neto) > 0.009 else None

    refs = [r["ref_sii"] for r in rows if (r["ref_sii"] or "").strip()]
    matches_sii = _match_docs_sii_por_refs(refs)

    timeline = []
    if codigo and len(codigo) >= 2:
        tl_q = (
            MovimientoStock.query.filter(MovimientoStock.codigo_producto == codigo)
            .order_by(MovimientoStock.fecha.desc(), MovimientoStock.id.desc())
            .limit(120)
            .all()
        )
        for m in tl_q:
            kind = "otro"
            if m.es_venta_operativa and (m.motivo_codigo or "") == "devolucion_venta":
                kind = "devolucion"
            elif m.es_venta_operativa and int(m.cantidad or 0) < 0:
                kind = "venta"
            elif m.es_venta_operativa and int(m.cantidad or 0) > 0:
                kind = "devolucion"
            elif (m.tipo or "") == "ingreso" or int(m.cantidad or 0) > 0:
                kind = "ingreso"
            elif (m.tipo or "") == "ajuste":
                kind = "ajuste"
            timeline.append({"mov": m, "kind": kind})

    if export == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=";")
        writer.writerow(
            [
                "fecha",
                "tipo",
                "codigo",
                "marca",
                "bodega",
                "cantidad",
                "precio_neto_u",
                "venta_neto",
                "costo_unitario",
                "costo_neto",
                "ganancia_neta",
                "margen_pct",
                "ref_sii",
                "usuario",
                "observacion",
            ]
        )
        for r in rows:
            writer.writerow(
                [
                    format_utc_to_chile(r["fecha"], default="") if r["fecha"] else "",
                    "devolucion" if r["is_dev"] else "venta",
                    r["codigo"],
                    r["marca"],
                    r["bodega"],
                    r["cantidad_signed"],
                    r["precio_venta_neto"] if r["precio_venta_neto"] is not None else "",
                    r["venta_neto"],
                    r["costo_unitario"] if r["costo_unitario"] is not None else "",
                    r["costo_neto"] if r["costo_neto"] is not None else "",
                    r["ganancia_neta"] if r["ganancia_neta"] is not None else "",
                    r["margen_pct"] if r["margen_pct"] is not None else "",
                    r["ref_sii"],
                    r["usuario"],
                    r["observacion"],
                ]
            )
        resp = Response(buf.getvalue(), mimetype="text/csv; charset=utf-8")
        resp.headers["Content-Disposition"] = "attachment; filename=ventas_operativas.csv"
        return resp

    return render_template(
        "contabilidad/ventas_operativas.html",
        rows=rows,
        timeline=timeline,
        matches_sii=matches_sii,
        unidades=unidades,
        unidades_venta=unidades_venta,
        unidades_dev=unidades_dev,
        total_neto=total_neto,
        total_ventas=total_ventas,
        total_devs=total_devs,
        total_iva=total_iva,
        total_con_iva=total_con_iva,
        total_costo=total_costo,
        total_ganancia=total_ganancia,
        margen_pct=margen_pct,
        n_con_costo=n_con_costo,
        n_mov=n_mov,
        ticket_prom=ticket_prom,
        filtros={
            "desde": desde.isoformat() if desde else "",
            "hasta": hasta.isoformat() if hasta else "",
            "codigo": codigo,
            "marca": marca,
            "bodega": bodega,
            "usuario": usuario,
        },
        active_page="contabilidad_ventas_operativas",
    )


@contabilidad_bp.route("/cierre-mensual", methods=["GET"])
@login_required
@permission_required("ver_finanzas")
def cierre_mensual():
    """Resumen de gestión: compras vs ventas ERP vs ventas operativas (solo lectura)."""
    today = chile_today()
    desde = _parse_date_arg(request.args.get("desde", ""), today.replace(day=1))
    hasta = _parse_date_arg(request.args.get("hasta", ""), today)
    if not desde:
        desde = today.replace(day=1)
    if not hasta:
        hasta = today

    # ── Compras (ingresos no anulados) ──
    compras_q = IngresoDocumento.query.filter(
        or_(IngresoDocumento.anulado.is_(False), IngresoDocumento.anulado.is_(None)),
        IngresoDocumento.fecha_documento >= desde,
        IngresoDocumento.fecha_documento <= hasta,
    )
    compras_docs = compras_q.order_by(
        IngresoDocumento.fecha_documento.desc(), IngresoDocumento.id.desc()
    ).limit(2000).all()
    compra_ids = [d.id for d in compras_docs]
    neto_por_compra: dict[int, float] = defaultdict(float)
    if compra_ids:
        # valor_neto en ingreso es unitario → costo doc = sum(vn × cantidad)
        for rid, vn, qty in (
            db.session.query(
                IngresoDocumentoItem.ingreso_documento_id,
                IngresoDocumentoItem.valor_neto,
                IngresoDocumentoItem.cantidad,
            )
            .filter(IngresoDocumentoItem.ingreso_documento_id.in_(compra_ids))
            .all()
        ):
            try:
                neto_por_compra[int(rid)] += float(vn or 0) * max(0, int(qty or 0))
            except (TypeError, ValueError):
                continue

    compras_neto = 0.0
    compras_iva = 0.0
    compras_total = 0.0
    compras_lista = []
    for doc in compras_docs:
        neto_v, iva_v, total_v = _totales_libro_compra_ingreso(
            doc, float(neto_por_compra.get(doc.id, 0.0))
        )
        compras_neto += neto_v
        compras_iva += iva_v
        compras_total += total_v
        if len(compras_lista) < 12:
            compras_lista.append(
                {
                    "fecha": doc.fecha_documento,
                    "numero": doc.numero_documento or f"ING-{doc.id}",
                    "proveedor": doc.proveedor_nombre or "—",
                    "neto": neto_v,
                    "iva": iva_v,
                    "total": total_v,
                }
            )

    # ── Ventas ERP (factura / boleta) ──
    erp_docs = (
        DocumentoVenta.query.filter(
            DocumentoVenta.tipo.in_(("factura", "boleta")),
            DocumentoVenta.status != "anulada",
            func.date(DocumentoVenta.fecha_documento) >= desde,
            func.date(DocumentoVenta.fecha_documento) <= hasta,
        )
        .order_by(DocumentoVenta.fecha_documento.desc(), DocumentoVenta.id.desc())
        .limit(2000)
        .all()
    )
    erp_neto = sum(float(d.subtotal or 0) for d in erp_docs)
    erp_iva = sum(float(d.impuesto or 0) for d in erp_docs)
    erp_total = sum(float(d.total or 0) for d in erp_docs)
    erp_lista = [
        {
            "fecha": d.fecha_documento,
            "tipo": d.tipo or "",
            "numero": d.numero or f"DOC-{d.id}",
            "cliente": d.cliente_nombre or "—",
            "neto": float(d.subtotal or 0),
            "iva": float(d.impuesto or 0),
            "total": float(d.total or 0),
        }
        for d in erp_docs[:12]
    ]

    # Costo estimado ERP (por líneas del período)
    erp_doc_ids = [d.id for d in erp_docs]
    costo_cache: dict = {}
    erp_costo = 0.0
    erp_costo_cubierto = 0.0
    if erp_doc_ids:
        lineas = (
            db.session.query(
                DocumentoVentaItem.codigo_producto,
                DocumentoVentaItem.cantidad,
                DocumentoVentaItem.subtotal,
            )
            .filter(DocumentoVentaItem.documento_id.in_(erp_doc_ids))
            .all()
        )
        for codigo, qty_raw, _sub in lineas:
            qty = abs(int(qty_raw or 0))
            if qty <= 0:
                continue
            cu = _costo_unitario_desde_ingresos(codigo or "", cache=costo_cache)
            if cu is not None:
                erp_costo += float(cu) * qty
                erp_costo_cubierto += 1
    erp_costo = round(erp_costo, 2)
    erp_ganancia = round(erp_neto - erp_costo, 2) if erp_costo_cubierto > 0 else None
    erp_margen_pct = (
        round(100.0 * erp_ganancia / abs(erp_neto), 1)
        if erp_ganancia is not None and abs(erp_neto) > 0.009
        else None
    )

    # ── Ventas operativas (ajustes) ──
    ops_raw = (
        MovimientoStock.query.filter(
            MovimientoStock.es_venta_operativa.is_(True),
            MovimientoStock.tipo == "ajuste",
            MovimientoStock.fecha >= chile_day_start_utc(desde),
            MovimientoStock.fecha < chile_day_end_exclusive_utc(hasta),
        )
        .order_by(MovimientoStock.fecha.desc(), MovimientoStock.id.desc())
        .limit(2000)
        .all()
    )
    ops_rows = [_enrich_venta_operativa_row(m, costo_cache=costo_cache) for m in ops_raw]
    ops_venta = sum(float(r["venta_neto"] or 0) for r in ops_rows)
    ops_costo = sum(float(r["costo_neto"] or 0) for r in ops_rows if r["costo_neto"] is not None)
    ops_ganancia = sum(
        float(r["ganancia_neta"] or 0) for r in ops_rows if r["ganancia_neta"] is not None
    )
    ops_margen_pct = (
        round(100.0 * ops_ganancia / abs(ops_venta), 1) if abs(ops_venta) > 0.009 else None
    )
    ops_lista = ops_rows[:12]

    # ── Resumen combinado ──
    venta_neta_total = round(erp_neto + ops_venta, 2)
    costo_vendido = round(erp_costo + ops_costo, 2)
    ganancia_total = None
    if erp_ganancia is not None or any(r["ganancia_neta"] is not None for r in ops_rows):
        g_erp = erp_ganancia if erp_ganancia is not None else 0.0
        ganancia_total = round(g_erp + ops_ganancia, 2)
    margen_total_pct = (
        round(100.0 * ganancia_total / abs(venta_neta_total), 1)
        if ganancia_total is not None and abs(venta_neta_total) > 0.009
        else None
    )

    return render_template(
        "contabilidad/cierre_mensual.html",
        filtros={
            "desde": desde.isoformat(),
            "hasta": hasta.isoformat(),
        },
        compras={
            "n": len(compras_docs),
            "neto": round(compras_neto, 2),
            "iva": round(compras_iva, 2),
            "total": round(compras_total, 2),
            "lista": compras_lista,
        },
        erp={
            "n": len(erp_docs),
            "neto": round(erp_neto, 2),
            "iva": round(erp_iva, 2),
            "total": round(erp_total, 2),
            "costo": erp_costo,
            "ganancia": erp_ganancia,
            "margen_pct": erp_margen_pct,
            "lista": erp_lista,
        },
        ops={
            "n": len(ops_rows),
            "venta": round(ops_venta, 2),
            "costo": round(ops_costo, 2),
            "ganancia": round(ops_ganancia, 2),
            "margen_pct": ops_margen_pct,
            "lista": ops_lista,
        },
        resumen={
            "venta_neta": venta_neta_total,
            "costo_vendido": costo_vendido,
            "ganancia": ganancia_total,
            "margen_pct": margen_total_pct,
            "compras_neto": round(compras_neto, 2),
        },
        active_page="contabilidad_cierre_mensual",
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
            proveedor_rut=format_rut(doc.proveedor_rut) or (doc.proveedor_rut or ""),
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
                ["Fecha", "Origen (compra)", "N° documento proveedor", "Proveedor", "RUT", "Estado pago", "Neto", "IVA", "Total"]
            )
            for d in docs:
                writer.writerow(
                    [
                        d.fecha_documento.strftime("%d/%m/%Y") if d.fecha_documento else "",
                        d.tipo_origen or "",
                        d.numero or "",
                        d.cliente_nombre or "",
                        d.proveedor_rut or "",
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

        lines = ["Fecha\tOrigen (compra)\tN° documento proveedor\tProveedor\tRUT\tEstado pago\tNeto\tIVA\tTotal"]
        for d in docs:
            lines.append(
                "\t".join(
                    [
                        d.fecha_documento.strftime("%d/%m/%Y") if d.fecha_documento else "",
                        str(d.tipo_origen or ""),
                        str(d.numero or ""),
                        str(d.cliente_nombre or ""),
                        str(d.proveedor_rut or ""),
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
