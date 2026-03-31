from __future__ import annotations

import re
from datetime import datetime

from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for

from app.extensions import db
from app.utils.decorators import login_required
from app.ventas.models import Cliente
from .models import Garantia, ESTADOS_GARANTIA

postventa_bp = Blueprint(
    "postventa", __name__, url_prefix="/postventa",
    template_folder="../../templates"
)


def _current_user() -> str:
    return session.get("user") or "sistema"


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
    clientes = Cliente.query.filter_by(activo=True).order_by(Cliente.nombre).all()
    return render_template(
        "postventa/index.html",
        garantias=garantias,
        clientes=clientes,
        estados=ESTADOS_GARANTIA,
        estado_filter=estado_filter,
        active_page="postventa",
    )


@postventa_bp.route("/nueva", methods=["POST"])
@login_required
def nueva():
    cliente_id_raw = request.form.get("cliente_id", "").strip()
    cliente = None
    if cliente_id_raw and cliente_id_raw.isdigit():
        cliente = db.session.get(Cliente, int(cliente_id_raw))

    estado = request.form.get("estado", "Pendiente").strip()
    if estado not in ESTADOS_GARANTIA:
        estado = "Pendiente"

    garantia = Garantia(
        numero=_next_garantia_number(),
        cliente_id=cliente.id if cliente else None,
        cliente_nombre=cliente.nombre if cliente else request.form.get("cliente_nombre_manual", "").strip(),
        producto_codigo=request.form.get("producto_codigo", "").strip().upper(),
        producto_descripcion=request.form.get("producto_descripcion", "").strip(),
        documento_numero=request.form.get("documento_numero", "").strip().upper(),
        motivo=request.form.get("motivo", "").strip(),
        estado=estado,
        usuario=_current_user(),
    )
    db.session.add(garantia)
    db.session.commit()
    flash(f"Garantía {garantia.numero} registrada correctamente.", "success")
    return redirect(url_for("postventa.index"))


@postventa_bp.route("/<int:gid>/estado", methods=["POST"])
@login_required
def cambiar_estado(gid: int):
    g = db.session.get(Garantia, gid)
    if g is None:
        return jsonify({"ok": False, "error": "No encontrada"}), 404
    nuevo_estado = (request.get_json(force=True) or {}).get("estado", "").strip()
    if nuevo_estado not in ESTADOS_GARANTIA:
        return jsonify({"ok": False, "error": "Estado inválido"}), 400
    g.estado = nuevo_estado
    db.session.commit()
    return jsonify({"ok": True, "estado": g.estado})


@postventa_bp.route("/<int:gid>/eliminar", methods=["POST"])
@login_required
def eliminar(gid: int):
    g = db.session.get(Garantia, gid)
    if g:
        db.session.delete(g)
        db.session.commit()
        flash("Garantía eliminada.", "success")
    return redirect(url_for("postventa.index"))
