from __future__ import annotations

from datetime import date, datetime

from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for

from app.extensions import db
from app.utils.decorators import login_required
from app.ventas.models import Cliente
from .models import Oportunidad, ESTADOS_OPORTUNIDAD

oportunidades_bp = Blueprint(
    "oportunidades", __name__, url_prefix="/oportunidades",
    template_folder="../../templates"
)


def _current_user() -> str:
    return session.get("user") or "sistema"


def _safe_float(raw) -> float:
    try:
        v = float(str(raw or "0").strip().replace(",", "."))
        return v if v >= 0 else 0.0
    except (ValueError, TypeError):
        return 0.0


@oportunidades_bp.route("/", methods=["GET"])
@login_required
def index():
    estado_filter = request.args.get("estado", "").strip()
    q = Oportunidad.query.order_by(Oportunidad.created_at.desc())
    if estado_filter and estado_filter in ESTADOS_OPORTUNIDAD:
        q = q.filter(Oportunidad.estado == estado_filter)
    oportunidades = q.all()

    # Group by estado for kanban display
    kanban: dict[str, list[Oportunidad]] = {e: [] for e in ESTADOS_OPORTUNIDAD}
    for op in oportunidades:
        kanban.setdefault(op.estado, []).append(op)

    clientes = Cliente.query.filter_by(activo=True).order_by(Cliente.nombre).all()
    return render_template(
        "oportunidades/index.html",
        oportunidades=oportunidades,
        kanban=kanban,
        clientes=clientes,
        estados=ESTADOS_OPORTUNIDAD,
        estado_filter=estado_filter,
        active_page="oportunidades",
    )


@oportunidades_bp.route("/nueva", methods=["POST"])
@login_required
def nueva():
    cliente_id = request.form.get("cliente_id", "").strip()
    descripcion = request.form.get("descripcion", "").strip()
    monto_str = request.form.get("monto_estimado", "0").strip()
    estado = request.form.get("estado", "Nueva").strip()
    fecha_str = request.form.get("fecha_seguimiento", "").strip()

    cliente = None
    if cliente_id and cliente_id.isdigit():
        cliente = db.session.get(Cliente, int(cliente_id))

    if estado not in ESTADOS_OPORTUNIDAD:
        estado = "Nueva"

    fecha_seguimiento = None
    if fecha_str:
        try:
            fecha_seguimiento = date.fromisoformat(fecha_str)
        except ValueError:
            pass

    op = Oportunidad(
        cliente_id=cliente.id if cliente else None,
        cliente_nombre=cliente.nombre if cliente else request.form.get("cliente_nombre_manual", "").strip(),
        descripcion=descripcion,
        monto_estimado=_safe_float(monto_str),
        estado=estado,
        fecha_seguimiento=fecha_seguimiento,
        usuario=_current_user(),
    )
    db.session.add(op)
    db.session.commit()
    flash("Oportunidad creada correctamente.", "success")
    return redirect(url_for("oportunidades.index"))


@oportunidades_bp.route("/<int:oid>/editar", methods=["POST"])
@login_required
def editar(oid: int):
    op = db.session.get(Oportunidad, oid)
    if op is None:
        flash("Oportunidad no encontrada.", "error")
        return redirect(url_for("oportunidades.index"))

    cliente_id = request.form.get("cliente_id", "").strip()
    estado = request.form.get("estado", op.estado).strip()
    if estado not in ESTADOS_OPORTUNIDAD:
        estado = op.estado

    op.descripcion = request.form.get("descripcion", op.descripcion).strip()
    op.monto_estimado = _safe_float(request.form.get("monto_estimado", str(op.monto_estimado)))
    op.estado = estado
    fecha_str = request.form.get("fecha_seguimiento", "").strip()
    if fecha_str:
        try:
            op.fecha_seguimiento = date.fromisoformat(fecha_str)
        except ValueError:
            pass
    if cliente_id and cliente_id.isdigit():
        c = db.session.get(Cliente, int(cliente_id))
        if c:
            op.cliente_id = c.id
            op.cliente_nombre = c.nombre
    op.updated_at = datetime.utcnow()
    db.session.commit()
    flash("Oportunidad actualizada.", "success")
    return redirect(url_for("oportunidades.index"))


@oportunidades_bp.route("/<int:oid>/estado", methods=["POST"])
@login_required
def cambiar_estado(oid: int):
    op = db.session.get(Oportunidad, oid)
    if op is None:
        return jsonify({"ok": False, "error": "No encontrada"}), 404
    nuevo_estado = (request.get_json(force=True) or {}).get("estado", "").strip()
    if nuevo_estado not in ESTADOS_OPORTUNIDAD:
        return jsonify({"ok": False, "error": "Estado inválido"}), 400
    op.estado = nuevo_estado
    op.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"ok": True, "estado": op.estado})


@oportunidades_bp.route("/<int:oid>/eliminar", methods=["POST"])
@login_required
def eliminar(oid: int):
    op = db.session.get(Oportunidad, oid)
    if op:
        db.session.delete(op)
        db.session.commit()
        flash("Oportunidad eliminada.", "success")
    return redirect(url_for("oportunidades.index"))


@oportunidades_bp.route("/api/lista", methods=["GET"])
@login_required
def api_lista():
    ops = Oportunidad.query.order_by(Oportunidad.created_at.desc()).all()
    return jsonify([{
        "id": op.id,
        "cliente_nombre": op.cliente_nombre or (op.cliente.nombre if op.cliente else ""),
        "descripcion": op.descripcion,
        "monto_estimado": op.monto_estimado,
        "estado": op.estado,
        "fecha_seguimiento": op.fecha_seguimiento.isoformat() if op.fecha_seguimiento else "",
    } for op in ops])
