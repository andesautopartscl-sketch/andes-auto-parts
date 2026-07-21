import logging
from datetime import datetime

from flask import abort, current_app, jsonify, redirect, render_template, request, send_from_directory, session, url_for

logger = logging.getLogger(__name__)

from app.utils.decorators import login_required
from app.extensions import db
from app.oc_clientes.models import OC_ESTADOS, OC_ESTADO_LABELS
from app.seguridad.models import Usuario as UsuarioSistema
from app.utils.finance_visibility import user_can_view_finanzas

from .bootstrap import mobile_bp
from . import clientes as mobile_clientes
from . import data as mobile_data
from . import etiquetas as mobile_etiquetas
from . import importar_imagenes as mobile_importar_imagenes
from . import ingreso_rapido as mobile_ingreso_rapido
from . import proveedores as mobile_proveedores
from . import scan as mobile_scan
from . import stock_ajuste as mobile_stock_ajuste
from . import oc_clientes as mobile_oc_clientes
from . import productos_buscar as mobile_productos_buscar
from . import venta_rapida as mobile_venta_rapida


@mobile_bp.context_processor
def _mobile_permissions_ctx():
    user = session.get("user")
    rol = session.get("rol")
    return {
        "puede_ver_oc_clientes": mobile_oc_clientes.puede_ver(user, rol),
        "puede_mod_oc_clientes": mobile_oc_clientes.puede_modificar(user, rol),
        "puede_etiquetas": mobile_etiquetas.puede_imprimir_etiquetas(user, rol),
        "puede_importar_imagenes": mobile_importar_imagenes.puede_importar_imagenes(user, rol),
        "puede_ingreso_rapido": mobile_ingreso_rapido.puede_registrar_ingreso(user, rol),
        "puede_venta_rapida": mobile_venta_rapida.puede_registrar_venta(user, rol),
    }


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


def _fecha_hoy_legible() -> str:
    """Fecha corta en español para el home (sin depender de locale del SO)."""
    dias = (
        "Lunes",
        "Martes",
        "Miércoles",
        "Jueves",
        "Viernes",
        "Sábado",
        "Domingo",
    )
    meses = (
        "ene",
        "feb",
        "mar",
        "abr",
        "may",
        "jun",
        "jul",
        "ago",
        "sep",
        "oct",
        "nov",
        "dic",
    )
    now = datetime.now()
    # weekday(): lunes=0 … domingo=6
    return f"{dias[now.weekday()]} {now.day} {meses[now.month - 1]}"


def _nombre_presentable() -> str:
    """Primer nombre legible para saludo en Home (no el username técnico)."""
    uid = session.get("usuario_id")
    if uid:
        try:
            u = db.session.get(UsuarioSistema, int(uid))
            if u and (u.nombre or "").strip():
                return (u.nombre or "").strip().split()[0].capitalize()
        except (TypeError, ValueError):
            pass

    username = (session.get("user") or "").strip()
    if not username:
        return "Usuario"

    base = username.lower()
    for suffix in ("superadmin", "admin", "user", "usr"):
        if base.endswith(suffix) and len(base) > len(suffix) + 1:
            base = base[: -len(suffix)]
            break
    base = base.strip("._-")
    if base:
        return base.capitalize()
    return username.capitalize()


@mobile_bp.route("/service-worker.js")
def service_worker():
    """SW con scope /m/ — exento del login wall vía login_wall.py."""
    from app.utils.mobile_ui_paths import mobile_static_dir

    root = mobile_static_dir()
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
        nombre_presentable=_nombre_presentable(),
        fecha_hoy=_fecha_hoy_legible(),
        fecha_iso=datetime.now().date().isoformat(),
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
    puede_precio = _puede_ver_finanzas()
    resultados = (
        mobile_productos_buscar.buscar(q, puede_ver_precio=puede_precio, limit=50) if q else []
    )
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
    synced_at = mobile_data._catalog_sync_ts()
    limit_raw = request.args.get("limit", type=int)
    offset = max(0, request.args.get("offset", type=int) or 0)
    if limit_raw is not None and limit_raw > 0:
        items, total = mobile_data.catalogo_pagina(offset=offset, limit=limit_raw)
        return jsonify(
            success=True,
            items=items,
            count=len(items),
            total=total,
            offset=offset,
            limit=limit_raw,
            synced_at=synced_at,
        )
    items = mobile_data.catalogo_completo()
    return jsonify(
        success=True,
        items=items,
        count=len(items),
        total=len(items),
        offset=0,
        synced_at=synced_at,
    )


@mobile_bp.route("/api/productos/buscar")
@login_required
def api_productos_buscar():
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify(success=True, items=[], count=0, query=q)
    puede_precio = _puede_ver_finanzas()
    items = mobile_productos_buscar.buscar(q, puede_ver_precio=puede_precio, limit=50)
    return jsonify(success=True, items=items, count=len(items), query=q)


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
    puede_precio = _puede_ver_finanzas()
    detalle = mobile_data.producto_detalle(codigo, puede_ver_precio=puede_precio)
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
    user = session.get("user")
    rol = session.get("rol")
    puede = mobile_ingreso_rapido.puede_registrar_ingreso(user, rol)
    logger.info("mobile ingreso-rapido: user=%s rol=%s puede_ingreso=%s", user, rol, puede)
    try:
        metodos = mobile_ingreso_rapido.metodos_pago_opciones()
    except Exception:
        logger.exception("mobile ingreso-rapido: error metodos_pago user=%s", user)
        metodos = []
    return render_template(
        "mobile/ingreso_rapido.html",
        puede_ingreso=puede,
        metodos_pago=metodos,
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
            mobile_etiquetas.guardar_pending(codigos_raw, print_mode)
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
    user = session.get("user")
    rol = session.get("rol")
    if not mobile_etiquetas.puede_imprimir_etiquetas(user, rol):
        abort(403)
    codigos_raw = (request.values.get("codigos") or "").strip()
    print_mode = (request.values.get("print_mode") or "").strip()
    valid_modes = {m["value"] for m in mobile_etiquetas.PRINT_MODES}
    used_session_fallback = False
    if not codigos_raw:
        codigos_raw, pending_mode = mobile_etiquetas.leer_pending()
        if codigos_raw:
            used_session_fallback = True
            if not print_mode or print_mode not in valid_modes:
                print_mode = pending_mode
    if not print_mode or print_mode not in valid_modes:
        print_mode = "a4"
    labels, missing = mobile_etiquetas.generar_etiquetas(codigos_raw)
    logger.info(
        "mobile etiquetas/imprimir: user=%s method=%s codigos_len=%s labels=%s missing=%s mode=%s session_fallback=%s",
        user,
        request.method,
        len(codigos_raw),
        len(labels),
        len(missing),
        print_mode,
        used_session_fallback,
    )
    if labels:
        mobile_etiquetas.registrar_impresion(labels)
    return render_template(
        "mobile/etiquetas_print.html",
        labels=labels,
        missing=missing,
        print_mode=print_mode,
        codigos_raw=codigos_raw,
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
        pwa_version="v2026.07.21-v32",
        **_nav_ctx("mas"),
    )


@mobile_bp.route("/oc-clientes")
@login_required
def oc_clientes_lista():
    if not mobile_oc_clientes.puede_ver(session.get("user"), session.get("rol")):
        abort(403)
    estado = (request.args.get("estado") or "").strip().lower()
    q = (request.args.get("q") or "").strip()
    filas = mobile_oc_clientes.listar_oc(estado=estado, q=q)
    return render_template(
        "mobile/oc_clientes_lista.html",
        filas=filas,
        estado=estado,
        q=q,
        estados=OC_ESTADOS,
        estado_labels=OC_ESTADO_LABELS,
        puede_modificar=mobile_oc_clientes.puede_modificar(session.get("user"), session.get("rol")),
        **_nav_ctx("mas"),
    )


@mobile_bp.route("/oc-clientes/nueva")
@login_required
def oc_clientes_nueva():
    if not mobile_oc_clientes.puede_ver(session.get("user"), session.get("rol")):
        abort(403)
    puede_mod = mobile_oc_clientes.puede_modificar(session.get("user"), session.get("rol"))
    return render_template(
        "mobile/oc_clientes_form.html",
        puede_modificar=puede_mod,
        metodo_pago_options=mobile_oc_clientes.metodos_pago_opciones(),
        url_escanear=url_for("oc_clientes.api_escanear_oc"),
        url_api_guardar=url_for("mobile.api_oc_clientes_crear"),
        url_api_buscar=url_for("mobile.api_buscar"),
        url_api_clientes=url_for("mobile.api_clientes"),
        **_nav_ctx("mas"),
    )


@mobile_bp.route("/oc-clientes/<int:oid>")
@login_required
def oc_clientes_detalle(oid: int):
    if not mobile_oc_clientes.puede_ver(session.get("user"), session.get("rol")):
        abort(403)
    detalle = mobile_oc_clientes.detalle_oc(oid)
    if detalle is None:
        abort(404)
    toast_map = {"entregada": "OC marcada como entregada", "pagada": "Pago registrado", "anulada": "OC anulada"}
    toast_key = (request.args.get("toast") or "").strip().lower()
    return render_template(
        "mobile/oc_clientes_detalle.html",
        oc=detalle,
        toast_msg=toast_map.get(toast_key, ""),
        puede_modificar=mobile_oc_clientes.puede_modificar(session.get("user"), session.get("rol")),
        metodo_pago_options=mobile_oc_clientes.metodos_pago_opciones(),
        url_entregar=url_for("mobile.api_oc_clientes_entregar", oid=oid),
        url_pago=url_for("mobile.api_oc_clientes_pago", oid=oid),
        url_anular=url_for("mobile.api_oc_clientes_anular", oid=oid),
        **_nav_ctx("mas"),
    )


@mobile_bp.route("/api/oc-clientes", methods=["POST"])
@login_required
def api_oc_clientes_crear():
    if not mobile_oc_clientes.puede_modificar(session.get("user"), session.get("rol")):
        return jsonify(ok=False, error="Sin permiso para crear OC."), 403
    payload = request.get_json(silent=True) or {}
    ok, oc_id, errors = mobile_oc_clientes.crear_oc(payload, session.get("user") or "sistema")
    if not ok:
        return jsonify(ok=False, errors=errors), 400
    return jsonify(ok=True, id=oc_id, redirect=url_for("mobile.oc_clientes_detalle", oid=oc_id))


@mobile_bp.route("/api/oc-clientes/<int:oid>/entregar", methods=["POST"])
@login_required
def api_oc_clientes_entregar(oid: int):
    if not mobile_oc_clientes.puede_modificar(session.get("user"), session.get("rol")):
        return jsonify(ok=False, error="Sin permiso."), 403
    payload = request.get_json(silent=True) or {}
    ok, msg = mobile_oc_clientes.marcar_entregada(
        oid,
        fecha_entrega_real=payload.get("fecha_entrega_real"),
        numero_guia_despacho=payload.get("numero_guia_despacho"),
        descontar_stock=bool(payload.get("descontar_stock")),
        usuario=session.get("user") or "sistema",
    )
    if not ok:
        return jsonify(ok=False, error=msg), 400
    return jsonify(ok=True, message=msg, redirect=url_for("mobile.oc_clientes_detalle", oid=oid, toast="entregada"))


@mobile_bp.route("/api/oc-clientes/<int:oid>/pago", methods=["POST"])
@login_required
def api_oc_clientes_pago(oid: int):
    if not mobile_oc_clientes.puede_modificar(session.get("user"), session.get("rol")):
        return jsonify(ok=False, error="Sin permiso."), 403
    payload = request.get_json(silent=True) or {}
    ok, msg = mobile_oc_clientes.registrar_pago(
        oid,
        numero_factura=payload.get("numero_factura") or "",
        fecha_pago=payload.get("fecha_pago"),
        metodo_pago=payload.get("metodo_pago") or "",
    )
    if not ok:
        return jsonify(ok=False, error=msg), 400
    return jsonify(ok=True, message=msg, redirect=url_for("mobile.oc_clientes_detalle", oid=oid, toast="pagada"))


@mobile_bp.route("/api/oc-clientes/<int:oid>/anular", methods=["POST"])
@login_required
def api_oc_clientes_anular(oid: int):
    if not mobile_oc_clientes.puede_modificar(session.get("user"), session.get("rol")):
        return jsonify(ok=False, error="Sin permiso."), 403
    payload = request.get_json(silent=True) or {}
    ok, msg = mobile_oc_clientes.anular_oc(
        oid,
        auth_user=payload.get("auth_user") or "",
        auth_password=payload.get("auth_password") or "",
    )
    if not ok:
        return jsonify(ok=False, error=msg), 400
    return jsonify(ok=True, message=msg, redirect=url_for("mobile.oc_clientes_detalle", oid=oid, toast="anulada"))
