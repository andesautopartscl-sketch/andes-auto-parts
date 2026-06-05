from pathlib import Path

from flask import abort, current_app, jsonify, render_template, request, send_from_directory, session

from app.utils.decorators import login_required
from app.utils.finance_visibility import user_can_view_finanzas

from . import mobile_bp
from . import data as mobile_data
from . import scan as mobile_scan
from . import stock_ajuste as mobile_stock_ajuste
from . import venta_rapida as mobile_venta_rapida


def _puede_ver_finanzas() -> bool:
    return user_can_view_finanzas(session.get("user"), session.get("rol"))


def _nav_ctx(active: str, **extra) -> dict:
    return {"active_nav": active, "puede_ver_finanzas": _puede_ver_finanzas(), **extra}


@mobile_bp.route("/service-worker.js")
def service_worker():
    """SW con scope /m/ — exento del login wall vía login_wall.py."""
    root = Path(current_app.root_path) / "static" / "mobile"
    response = send_from_directory(root, "service-worker.js", mimetype="application/javascript")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@mobile_bp.route("/")
@login_required
def home():
    return render_template("mobile/home.html", **_nav_ctx("inicio"))


@mobile_bp.route("/dashboard")
@login_required
def dashboard():
    payload = mobile_data.dashboard_payload(_puede_ver_finanzas())
    return render_template("mobile/dashboard.html", data=payload, **_nav_ctx("mas"))


@mobile_bp.route("/buscar")
@login_required
def buscar():
    q = (request.args.get("q") or "").strip()
    resultados = mobile_data.buscar_productos(q, limit=30) if q else []
    return render_template(
        "mobile/buscar.html",
        q=q,
        resultados=resultados,
        **_nav_ctx("buscar"),
    )


@mobile_bp.route("/api/dashboard")
@login_required
def api_dashboard():
    payload = mobile_data.dashboard_payload(_puede_ver_finanzas())
    return jsonify(success=True, data=payload)


@mobile_bp.route("/api/catalogo")
@login_required
def api_catalogo():
    items = mobile_data.catalogo_completo()
    return jsonify(success=True, items=items, count=len(items), synced_at=mobile_data._catalog_sync_ts())


@mobile_bp.route("/api/buscar")
@login_required
def api_buscar():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify(success=True, items=[], count=0)
    items = mobile_data.buscar_productos(q, limit=30)
    return jsonify(success=True, items=items, count=len(items), query=q)


@mobile_bp.route("/producto/<codigo>")
@login_required
def producto(codigo):
    detalle = mobile_data.producto_detalle(codigo)
    if detalle is None:
        abort(404)
    return render_template("mobile/producto.html", p=detalle, **_nav_ctx("buscar"))


@mobile_bp.route("/ventas")
@login_required
def ventas():
    periodo = (request.args.get("periodo") or "hoy").strip().lower()
    if periodo not in {"hoy", "ayer", "semana"}:
        periodo = "hoy"
    lista = mobile_data.ventas_por_periodo(periodo)
    puede = _puede_ver_finanzas()
    for v in lista:
        v["total_fmt"] = mobile_data._fmt_monto(v["total"], puede)
    return render_template(
        "mobile/ventas.html",
        ventas=lista,
        periodo=periodo,
        **_nav_ctx("ventas"),
    )


@mobile_bp.route("/venta/<int:doc_id>")
@login_required
def venta(doc_id):
    detalle = mobile_data.venta_detalle(doc_id)
    if detalle is None:
        abort(404)
    puede = _puede_ver_finanzas()
    if not puede:
        detalle["totals"]["subtotal"] = None
        detalle["totals"]["iva"] = None
        detalle["totals"]["total"] = None
        detalle["totals"]["subtotal_fmt"] = "—"
        detalle["totals"]["iva_fmt"] = "—"
        detalle["totals"]["total_fmt"] = "—"
        for it in detalle.get("items") or []:
            it["precio"] = None
            it["subtotal"] = None
    return render_template("mobile/venta.html", v=detalle, **_nav_ctx("ventas"))


@mobile_bp.route("/ingresos")
@login_required
def ingresos():
    lista = mobile_data.ultimos_ingresos(20)
    puede = _puede_ver_finanzas()
    for i in lista:
        i["total_fmt"] = mobile_data._fmt_monto(i["total"], puede)
    return render_template(
        "mobile/ingresos.html",
        ingresos=lista,
        **_nav_ctx("mas"),
    )


@mobile_bp.route("/ingreso/<int:doc_id>")
@login_required
def ingreso(doc_id):
    detalle = mobile_data.ingreso_detalle(doc_id)
    if detalle is None:
        abort(404)
    puede = _puede_ver_finanzas()
    detalle["total_fmt"] = mobile_data._fmt_monto(detalle["total"], puede)
    detalle["total_neto_fmt"] = mobile_data._fmt_monto(detalle["total_neto"], puede)
    if not puede:
        for it in detalle.get("items") or []:
            it["valor_neto"] = None
    return render_template("mobile/ingreso.html", i=detalle, **_nav_ctx("mas"))


@mobile_bp.route("/stock-critico")
@login_required
def stock_critico():
    lista = mobile_data.stock_critico_lista()
    return render_template(
        "mobile/stock_critico.html",
        items=lista,
        umbral=mobile_data._STOCK_CRITICO_UMBRAL,
        **_nav_ctx("mas"),
    )


@mobile_bp.route("/reportes")
@login_required
def reportes():
    payload = mobile_data.reportes_payload(_puede_ver_finanzas())
    return render_template("mobile/reportes.html", data=payload, **_nav_ctx("mas"))


@mobile_bp.route("/escaner")
@login_required
def escaner():
    modo = (request.args.get("modo") or "qr").strip().lower()
    if modo not in {"qr", "barcode", "venta"}:
        modo = "qr"
    return render_template(
        "mobile/escaner.html",
        modo_inicial=modo,
        **_nav_ctx("escaner", scanner_layout=True),
    )


@mobile_bp.route("/venta-rapida")
@login_required
def venta_rapida():
    return render_template(
        "mobile/venta_rapida.html",
        puede_vender=mobile_venta_rapida.puede_registrar_venta(
            session.get("user"), session.get("rol")
        ),
        **_nav_ctx("ventas"),
    )


@mobile_bp.route("/api/clientes")
@login_required
def api_clientes():
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify(success=True, items=[], count=0)
    items = mobile_venta_rapida.buscar_clientes(q, limit=30)
    return jsonify(success=True, items=items, count=len(items), query=q)


@mobile_bp.route("/api/venta-producto/<codigo>")
@login_required
def api_venta_producto(codigo):
    linea = mobile_venta_rapida.producto_linea_venta(codigo, cantidad=1)
    if linea is None:
        return jsonify(success=False, message="Producto no encontrado"), 404
    from app.utils.format_currency_cl import format_precio_publico_con_iva

    linea["precio_fmt"] = (
        format_precio_publico_con_iva(linea["precio"]) if linea["precio"] > 0 else "—"
    )
    return jsonify(success=True, producto=linea)


@mobile_bp.route("/api/venta-rapida", methods=["POST"])
@login_required
def api_venta_rapida():
    data = request.get_json(silent=True) or {}
    ok, result = mobile_venta_rapida.registrar_venta_rapida(data)
    if not ok:
        return jsonify(success=False, **result), 400
    return jsonify(success=True, **result)


@mobile_bp.route("/ajustar-stock/<codigo>")
@login_required
def ajustar_stock(codigo):
    ctx = mobile_stock_ajuste.stock_ajuste_contexto(codigo)
    if ctx is None:
        abort(404)
    return render_template(
        "mobile/ajustar_stock.html",
        s=ctx,
        tipos=mobile_stock_ajuste.TIPOS_MOVIMIENTO,
        **_nav_ctx("escaner"),
    )


@mobile_bp.route("/api/ajustar-stock", methods=["POST"])
@login_required
def api_ajustar_stock():
    data = request.get_json(silent=True) or {}
    ok, result = mobile_stock_ajuste.registrar_ajuste_stock(data)
    if not ok:
        return jsonify(success=False, **result), 400
    return jsonify(success=True, **result)


@mobile_bp.route("/api/producto/<codigo>")
@login_required
def api_producto(codigo):
    info = mobile_scan.producto_existe(codigo)
    return jsonify(success=True, **info)


@mobile_bp.route("/stock/<codigo>")
@login_required
def stock(codigo):
    vista = mobile_data.stock_vista_rapida(codigo)
    if vista is None:
        abort(404)
    return render_template("mobile/stock.html", s=vista, **_nav_ctx("escaner"))
