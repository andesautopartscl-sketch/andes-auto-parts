from datetime import datetime
from pathlib import Path

from flask import abort, current_app, jsonify, redirect, render_template, request, send_from_directory, session, url_for

from app.utils.decorators import login_required
from app.extensions import db
from app.seguridad.models import Usuario as UsuarioSistema
from app.utils.finance_visibility import user_can_view_finanzas

from . import mobile_bp
from . import clientes as mobile_clientes
from . import data as mobile_data
from . import etiquetas as mobile_etiquetas
from . import importar_imagenes as mobile_importar_imagenes
from . import ingreso_rapido as mobile_ingreso_rapido
from . import proveedores as mobile_proveedores
from . import scan as mobile_scan
from . import stock_ajuste as mobile_stock_ajuste
from . import venta_rapida as mobile_venta_rapida


def _puede_ver_finanzas() -> bool:
    return user_can_view_finanzas(session.get("user"), session.get("rol"))


def _nav_ctx(active: str, **extra) -> dict:
    return {"active_nav": active, "puede_ver_finanzas": _puede_ver_finanzas(), **extra}


def _saludo_hora() -> str:
    h = datetime.now().hour
    if 5 <= h < 12:
        return "Buenos días"
    if 12 <= h < 20:
        return "Buenas tardes"
    return "Buenas noches"


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
    kpis = mobile_data.dashboard_payload(_puede_ver_finanzas())
    return render_template(
        "mobile/home.html",
        kpis=kpis,
        saludo=_saludo_hora(),
        **_nav_ctx("inicio"),
    )


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
    if modo not in {"qr", "barcode", "venta", "ingreso"}:
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


@mobile_bp.route("/clientes")
@login_required
def clientes():
    q = (request.args.get("q") or "").strip()
    lista = mobile_clientes.listar_clientes(q)
    toast_map = {"creado": "Cliente creado", "actualizado": "Cliente actualizado", "eliminado": "Cliente desactivado"}
    toast_key = (request.args.get("toast") or "").strip().lower()
    return render_template(
        "mobile/clientes.html",
        clientes=lista,
        q=q,
        toast_msg=toast_map.get(toast_key, ""),
        puede_gestionar=mobile_clientes.puede_gestionar_clientes(session.get("user"), session.get("rol")),
        **_nav_ctx("mas"),
    )


@mobile_bp.route("/cliente/nuevo", methods=["GET", "POST"])
@login_required
def cliente_nuevo():
    if not mobile_clientes.puede_gestionar_clientes(session.get("user"), session.get("rol")):
        abort(403)
    validation_errors = []
    cliente = {}
    if request.method == "POST":
        ok, result = mobile_clientes.guardar_cliente_nuevo(request.form)
        if ok:
            return redirect(url_for("mobile.clientes", toast="creado"))
        validation_errors = result.get("errors") or []
        cliente = result.get("cliente") or {}
    return render_template(
        "mobile/cliente_form.html",
        form_title="Nuevo cliente",
        submit_label="Crear cliente",
        cliente=cliente,
        validation_errors=validation_errors,
        back_url=url_for("mobile.clientes"),
        **_nav_ctx("mas"),
    )


@mobile_bp.route("/cliente/<int:cid>")
@login_required
def cliente_detalle(cid):
    detalle = mobile_clientes.cliente_detalle(cid)
    if detalle is None:
        abort(404)
    return render_template(
        "mobile/cliente_detalle.html",
        c=detalle,
        puede_gestionar=mobile_clientes.puede_gestionar_clientes(session.get("user"), session.get("rol")),
        **_nav_ctx("mas"),
    )


@mobile_bp.route("/cliente/<int:cid>/editar", methods=["GET", "POST"])
@login_required
def cliente_editar(cid):
    if not mobile_clientes.puede_gestionar_clientes(session.get("user"), session.get("rol")):
        abort(403)
    validation_errors = []
    cliente = {}
    if request.method == "POST":
        ok, result = mobile_clientes.guardar_cliente_editar(cid, request.form)
        if ok:
            return redirect(url_for("mobile.cliente_detalle", cid=cid, toast="actualizado"))
        validation_errors = result.get("errors") or []
        cliente = result.get("cliente") or {}
    else:
        detalle = mobile_clientes.cliente_detalle(cid)
        if detalle is None:
            abort(404)
        cliente = detalle
    return render_template(
        "mobile/cliente_form.html",
        form_title="Editar cliente",
        submit_label="Guardar cambios",
        cliente=cliente,
        validation_errors=validation_errors,
        back_url=url_for("mobile.cliente_detalle", cid=cid),
        **_nav_ctx("mas"),
    )


@mobile_bp.route("/cliente/<int:cid>/eliminar", methods=["POST"])
@login_required
def cliente_eliminar(cid):
    if not mobile_clientes.puede_gestionar_clientes(session.get("user"), session.get("rol")):
        abort(403)
    if mobile_clientes.desactivar_cliente(cid):
        return redirect(url_for("mobile.clientes", toast="eliminado"))
    abort(404)


@mobile_bp.route("/proveedores")
@login_required
def proveedores():
    q = (request.args.get("q") or "").strip()
    lista = mobile_proveedores.listar_proveedores(q)
    toast_map = {"creado": "Proveedor creado", "actualizado": "Proveedor actualizado", "eliminado": "Proveedor desactivado"}
    toast_key = (request.args.get("toast") or "").strip().lower()
    return render_template(
        "mobile/proveedores.html",
        proveedores=lista,
        q=q,
        toast_msg=toast_map.get(toast_key, ""),
        puede_gestionar=mobile_proveedores.puede_gestionar_proveedores(session.get("user"), session.get("rol")),
        **_nav_ctx("mas"),
    )


@mobile_bp.route("/proveedor/nuevo", methods=["GET", "POST"])
@login_required
def proveedor_nuevo():
    if not mobile_proveedores.puede_gestionar_proveedores(session.get("user"), session.get("rol")):
        abort(403)
    validation_errors = []
    proveedor = {}
    if request.method == "POST":
        ok, result = mobile_proveedores.guardar_proveedor_nuevo(request.form)
        if ok:
            return redirect(url_for("mobile.proveedores", toast="creado"))
        validation_errors = result.get("errors") or []
        proveedor = result.get("proveedor") or {}
    return render_template(
        "mobile/proveedor_form.html",
        form_title="Nuevo proveedor",
        submit_label="Crear proveedor",
        proveedor=proveedor,
        validation_errors=validation_errors,
        back_url=url_for("mobile.proveedores"),
        **_nav_ctx("mas"),
    )


@mobile_bp.route("/proveedor/<int:pid>")
@login_required
def proveedor_detalle(pid):
    detalle = mobile_proveedores.proveedor_detalle(pid)
    if detalle is None:
        abort(404)
    return render_template(
        "mobile/proveedor_detalle.html",
        p=detalle,
        puede_gestionar=mobile_proveedores.puede_gestionar_proveedores(session.get("user"), session.get("rol")),
        **_nav_ctx("mas"),
    )


@mobile_bp.route("/proveedor/<int:pid>/editar", methods=["GET", "POST"])
@login_required
def proveedor_editar(pid):
    if not mobile_proveedores.puede_gestionar_proveedores(session.get("user"), session.get("rol")):
        abort(403)
    validation_errors = []
    proveedor = {}
    if request.method == "POST":
        ok, result = mobile_proveedores.guardar_proveedor_editar(pid, request.form)
        if ok:
            return redirect(url_for("mobile.proveedor_detalle", pid=pid, toast="actualizado"))
        validation_errors = result.get("errors") or []
        proveedor = result.get("proveedor") or {}
    else:
        detalle = mobile_proveedores.proveedor_detalle(pid)
        if detalle is None:
            abort(404)
        proveedor = detalle
    return render_template(
        "mobile/proveedor_form.html",
        form_title="Editar proveedor",
        submit_label="Guardar cambios",
        proveedor=proveedor,
        validation_errors=validation_errors,
        back_url=url_for("mobile.proveedor_detalle", pid=pid),
        **_nav_ctx("mas"),
    )


@mobile_bp.route("/proveedor/<int:pid>/eliminar", methods=["POST"])
@login_required
def proveedor_eliminar(pid):
    if not mobile_proveedores.puede_gestionar_proveedores(session.get("user"), session.get("rol")):
        abort(403)
    if mobile_proveedores.desactivar_proveedor(pid):
        return redirect(url_for("mobile.proveedores", toast="eliminado"))
    abort(404)


@mobile_bp.route("/api/proveedores")
@login_required
def api_proveedores():
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify(success=True, items=[], count=0)
    items = mobile_proveedores.buscar_proveedores(q, limit=30)
    return jsonify(success=True, items=items, count=len(items), query=q)


@mobile_bp.route("/ingreso-rapido")
@login_required
def ingreso_rapido():
    return render_template(
        "mobile/ingreso_rapido.html",
        puede_ingreso=mobile_ingreso_rapido.puede_registrar_ingreso(session.get("user"), session.get("rol")),
        metodos_pago=mobile_ingreso_rapido.metodos_pago_opciones(),
        **_nav_ctx("mas"),
    )


@mobile_bp.route("/api/ingreso-producto/<codigo>")
@login_required
def api_ingreso_producto(codigo):
    linea = mobile_ingreso_rapido.producto_linea_ingreso(codigo)
    if linea is None:
        return jsonify(success=False, message="Producto no encontrado"), 404
    return jsonify(success=True, producto=linea)


@mobile_bp.route("/api/ingreso-rapido", methods=["POST"])
@login_required
def api_ingreso_rapido():
    data = request.get_json(silent=True) or {}
    ok, result = mobile_ingreso_rapido.registrar_ingreso_rapido(data)
    if not ok:
        return jsonify(success=False, **result), 400
    return jsonify(success=True, **result)


@mobile_bp.route("/etiquetas", methods=["GET", "POST"])
@login_required
def etiquetas():
    if not mobile_etiquetas.puede_imprimir_etiquetas(session.get("user"), session.get("rol")):
        abort(403)
    codigos_raw = (request.values.get("codigos") or "").strip()
    labels = []
    missing = []
    message = ""
    print_mode = (request.values.get("print_mode") or "a4").strip()
    if request.method == "POST" and codigos_raw:
        labels, missing = mobile_etiquetas.generar_etiquetas(codigos_raw)
        if labels:
            mobile_etiquetas.registrar_impresion(labels)
        if missing:
            message = f"No encontrados: {', '.join(missing[:5])}"
    return render_template(
        "mobile/etiquetas.html",
        codigos_raw=codigos_raw,
        labels=labels,
        missing=missing,
        message=message,
        print_mode=print_mode,
        print_modes=mobile_etiquetas.PRINT_MODES,
        **_nav_ctx("mas"),
    )


@mobile_bp.route("/etiquetas/imprimir", methods=["GET", "POST"])
@login_required
def etiquetas_imprimir():
    if not mobile_etiquetas.puede_imprimir_etiquetas(session.get("user"), session.get("rol")):
        abort(403)
    codigos_raw = (request.values.get("codigos") or "").strip()
    print_mode = (request.values.get("print_mode") or "a4").strip()
    labels, missing = mobile_etiquetas.generar_etiquetas(codigos_raw)
    if labels:
        mobile_etiquetas.registrar_impresion(labels)
    return render_template(
        "mobile/etiquetas_print.html",
        labels=labels,
        missing=missing,
        print_mode=print_mode,
    )


@mobile_bp.route("/importar-imagenes")
@login_required
def importar_imagenes():
    if not mobile_importar_imagenes.puede_importar_imagenes(session.get("user"), session.get("rol")):
        abort(403)
    return render_template(
        "mobile/importar_imagenes.html",
        cloudinary_ok=mobile_importar_imagenes.cloudinary_is_configured(),
        tipos_imagen=mobile_importar_imagenes.TIPO_OPCIONES,
        **_nav_ctx("mas"),
    )


@mobile_bp.route("/api/importar-imagenes/buscar")
@login_required
def api_importar_imagenes_buscar():
    if not mobile_importar_imagenes.puede_importar_imagenes(session.get("user"), session.get("rol")):
        return mobile_importar_imagenes._deny_json()
    q = (request.args.get("q") or "").strip()
    items = mobile_importar_imagenes.buscar_productos(q)
    return jsonify(success=True, items=items, count=len(items))


@mobile_bp.route("/api/importar-imagenes/resolver")
@login_required
def api_importar_imagenes_resolver():
    if not mobile_importar_imagenes.puede_importar_imagenes(session.get("user"), session.get("rol")):
        return mobile_importar_imagenes._deny_json()
    codigo = (request.args.get("codigo") or "").strip()
    return jsonify(mobile_importar_imagenes.resolver_codigo(codigo))


@mobile_bp.route("/api/importar-imagenes/subir", methods=["POST"])
@login_required
def api_importar_imagenes_subir():
    if not mobile_importar_imagenes.puede_importar_imagenes(session.get("user"), session.get("rol")):
        return mobile_importar_imagenes._deny_json()
    file_obj = request.files.get("imagen") or request.files.get("file")
    codigo = (request.form.get("codigo") or "").strip().upper()
    archivo_nombre = (request.form.get("archivo_nombre") or "").strip()
    tipo_imagen = (request.form.get("tipo_imagen") or "producto").strip()
    if not file_obj:
        return jsonify(ok=False, success=False, error="Falta archivo", estado="error"), 400
    if not codigo:
        return jsonify(ok=False, success=False, error="Falta código", estado="error"), 400
    result = mobile_importar_imagenes.subir_imagen(
        file_obj,
        codigo=codigo,
        archivo_nombre=archivo_nombre,
        tipo_imagen=tipo_imagen,
    )
    status = 200 if result.get("ok") else 500
    return jsonify(result), status


@mobile_bp.route("/ajustes")
@login_required
def ajustes():
    email = ""
    uid = session.get("usuario_id")
    if uid:
        try:
            u = db.session.get(UsuarioSistema, int(uid))
            if u and u.correo:
                email = (u.correo or "").strip()
        except (TypeError, ValueError):
            pass
    return render_template(
        "mobile/ajustes.html",
        usuario_email=email,
        pwa_version="v2026.06.05-v16",
        **_nav_ctx("mas"),
    )
