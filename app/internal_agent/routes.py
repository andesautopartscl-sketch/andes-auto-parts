"""Internal READ API for the Agent Gateway. Cookie sessions are rejected."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from app.internal_agent.catalog import search_catalog_page, validate_search_args
from app.internal_agent.check_stock import check_public_stock, validate_check_stock_args
from app.internal_agent.customer import get_public_customers, validate_customer_args
from app.internal_agent.equivalences import get_public_equivalences, validate_equivalence_args
from app.internal_agent.dashboard import get_public_dashboard_kpis, validate_dashboard_args
from app.internal_agent.ingresos import get_public_ingresos, validate_ingreso_args
from app.internal_agent.inventory import get_public_inventory, validate_inventory_args
from app.internal_agent.m2m import InternalAuthError, actor_username, authenticate_m2m, error_response
from app.internal_agent.movements import get_public_movements, validate_movement_args
from app.internal_agent.principal import (
    actor_can_view_finanzas,
    actor_can_view_oc_finance,
    actor_can_view_stock,
    require_mod_bodega,
    require_mod_dashboard,
    require_mod_productos,
    require_mod_ventas,
    require_ver_stock,
    resolve_actor,
)
from app.internal_agent.product import (
    get_public_product,
    reject_extra_product_request,
    validate_product_codigo,
)
from app.internal_agent.purchase_orders import get_public_purchase_orders, validate_purchase_order_args
from app.internal_agent.sales import get_public_sales, validate_sales_args
from app.internal_agent.supplier import get_public_suppliers, validate_supplier_args

internal_agent_bp = Blueprint("internal_agent", __name__, url_prefix="/internal/agent/v1")


@internal_agent_bp.route("/catalog/search", methods=["POST"])
def catalog_search():
    try:
        environment = authenticate_m2m(request)
        actor = actor_username(request)
        username, role_name = resolve_actor(actor)
        require_mod_productos(username, role_name)
        q, limit = validate_search_args(request.get_json(silent=True))
        items, truncated = search_catalog_page(q, limit)
        return jsonify(
            {
                "ok": True,
                "tool": "search_catalog",
                "classification": "INTERNAL",
                "data": {"items": items, "count": len(items)},
                "meta": {"limit": limit, "truncated": truncated, "environment": environment},
            }
        )
    except InternalAuthError as exc:
        return error_response(exc)


@internal_agent_bp.route("/catalog/product/<string:codigo>", methods=["GET"])
def catalog_product(codigo: str):
    try:
        environment = authenticate_m2m(request)
        actor = actor_username(request)
        username, role_name = resolve_actor(actor)
        require_mod_productos(username, role_name)
        reject_extra_product_request(request)
        code = validate_product_codigo(codigo)
        data = get_public_product(code)
        if data is None:
            raise InternalAuthError("not_found", "Producto no encontrado", status=404)
        return jsonify(
            {
                "ok": True,
                "tool": "get_product",
                "classification": "INTERNAL",
                "data": data,
                "meta": {"environment": environment},
            }
        )
    except InternalAuthError as exc:
        return error_response(exc)


@internal_agent_bp.route("/catalog/equivalences", methods=["POST"])
def catalog_equivalences():
    """FASE 8.8 — cruce OEM. Misma ACL que el resto del catalogo (mod_productos).

    Sin finanzas: el cruce es tecnico. No hay campos de precio que conceder ni
    que redactar, asi que no se pasa include_finance.
    """
    try:
        environment = authenticate_m2m(request)
        actor = actor_username(request)
        username, role_name = resolve_actor(actor)
        require_mod_productos(username, role_name)
        args = validate_equivalence_args(request.get_json(silent=True))
        data, truncated = get_public_equivalences(
            oem=args["oem"], codigo=args["codigo"],
            marca=args["marca"], modelo=args["modelo"], limit=args["limit"])
        return jsonify({
            "ok": True,
            "tool": "get_equivalences",
            "classification": "INTERNAL",
            "data": data,
            "meta": {"limit": args["limit"], "truncated": truncated,
                     "environment": environment},
        })
    except InternalAuthError as exc:
        return error_response(exc)


@internal_agent_bp.route("/inventory/stock", methods=["POST"])
def inventory_stock():
    try:
        environment = authenticate_m2m(request)
        actor = actor_username(request)
        username, role_name = resolve_actor(actor)
        require_ver_stock(username, role_name)
        codigo, marca, bodega = validate_inventory_args(request.get_json(silent=True))
        data = get_public_inventory(codigo, marca, bodega)
        if data is None:
            raise InternalAuthError("not_found", "Producto no encontrado", status=404)
        return jsonify(
            {
                "ok": True,
                "tool": "get_inventory",
                "classification": "INTERNAL",
                "data": data,
                "meta": {"environment": environment},
            }
        )
    except InternalAuthError as exc:
        return error_response(exc)


@internal_agent_bp.route("/inventory/check-stock", methods=["POST"])
def inventory_check_stock():
    try:
        environment = authenticate_m2m(request)
        actor = actor_username(request)
        username, role_name = resolve_actor(actor)
        require_ver_stock(username, role_name)
        items = validate_check_stock_args(request.get_json(silent=True))
        data = check_public_stock(items)
        return jsonify(
            {
                "ok": True,
                "tool": "check_stock",
                "classification": "INTERNAL",
                "data": data,
                "meta": {"environment": environment, "count": len(data["items"])},
            }
        )
    except InternalAuthError as exc:
        return error_response(exc)


@internal_agent_bp.route("/stock/movements", methods=["POST"])
def stock_movements():
    try:
        environment = authenticate_m2m(request)
        actor = actor_username(request)
        username, role_name = resolve_actor(actor)
        require_mod_bodega(username, role_name)
        codigo, fecha_desde, fecha_hasta, limit = validate_movement_args(request.get_json(silent=True))
        result = get_public_movements(codigo, fecha_desde, fecha_hasta, limit)
        if result is None:
            raise InternalAuthError("not_found", "Producto no encontrado", status=404)
        data, truncated = result
        return jsonify(
            {
                "ok": True,
                "tool": "get_stock_movements",
                "classification": "INTERNAL",
                "data": data,
                "meta": {"limit": limit, "truncated": truncated, "environment": environment},
            }
        )
    except InternalAuthError as exc:
        return error_response(exc)


@internal_agent_bp.route("/bodega/ingresos", methods=["POST"])
def bodega_ingresos():
    try:
        environment = authenticate_m2m(request)
        actor = actor_username(request)
        username, role_name = resolve_actor(actor)
        require_mod_bodega(username, role_name)
        args = validate_ingreso_args(request.get_json(silent=True))
        result = get_public_ingresos(
            codigo=args["codigo"],
            proveedor=args["proveedor"],
            numero_documento=args["numero_documento"],
            fecha_desde=args["fecha_desde"],
            fecha_hasta=args["fecha_hasta"],
            limit=args["limit"],
            include_finance=actor_can_view_finanzas(username, role_name),
        )
        if result is None:
            raise InternalAuthError("not_found", "Producto no encontrado", status=404)
        data, truncated = result
        return jsonify(
            {
                "ok": True,
                "tool": "get_ingresos",
                "classification": "CONFIDENTIAL",
                "data": data,
                "meta": {"limit": args["limit"], "truncated": truncated, "environment": environment},
            }
        )
    except InternalAuthError as exc:
        return error_response(exc)


@internal_agent_bp.route("/ventas/purchase-orders", methods=["POST"])
def ventas_purchase_orders():
    try:
        environment = authenticate_m2m(request)
        actor = actor_username(request)
        username, role_name = resolve_actor(actor)
        require_mod_ventas(username, role_name)
        args = validate_purchase_order_args(request.get_json(silent=True))
        result = get_public_purchase_orders(
            numero=args["numero"],
            proveedor=args["proveedor"],
            estado=args["estado"],
            codigo=args["codigo"],
            fecha_desde=args["fecha_desde"],
            fecha_hasta=args["fecha_hasta"],
            limit=args["limit"],
            include_finance=actor_can_view_oc_finance(username, role_name),
        )
        if result is None:
            raise InternalAuthError("not_found", "Orden de compra no encontrada", status=404)
        data, truncated = result
        return jsonify(
            {
                "ok": True,
                "tool": "get_purchase_orders",
                "classification": "CONFIDENTIAL",
                "data": data,
                "meta": {"limit": args["limit"], "truncated": truncated, "environment": environment},
            }
        )
    except InternalAuthError as exc:
        return error_response(exc)


@internal_agent_bp.route("/ventas/sales", methods=["POST"])
def ventas_sales():
    """FASE 8.6 — ventas agregadas. Misma ACL que el resto del modulo ventas.

    include_finance sigue la misma puerta que las OC: el ERP decide, el Gateway
    no concede visibilidad financiera por su cuenta.
    """
    try:
        environment = authenticate_m2m(request)
        actor = actor_username(request)
        username, role_name = resolve_actor(actor)
        require_mod_ventas(username, role_name)
        args = validate_sales_args(request.get_json(silent=True))
        data, truncated = get_public_sales(
            codigo=args["codigo"],
            cliente=args["cliente"],
            estado=args["estado"],
            tipos=args["tipos"],
            group_by=args["group_by"],
            fecha_desde=args["fecha_desde"],
            fecha_hasta=args["fecha_hasta"],
            limit=args["limit"],
            include_finance=actor_can_view_finanzas(username, role_name),
        )
        return jsonify(
            {
                "ok": True,
                "tool": "get_sales",
                "classification": "CONFIDENTIAL",
                "data": data,
                "meta": {"limit": args["limit"], "truncated": truncated,
                         "environment": environment},
            }
        )
    except InternalAuthError as exc:
        return error_response(exc)


@internal_agent_bp.route("/ventas/customers", methods=["POST"])
def ventas_customers():
    try:
        environment = authenticate_m2m(request)
        actor = actor_username(request)
        username, role_name = resolve_actor(actor)
        require_mod_ventas(username, role_name)
        args = validate_customer_args(request.get_json(silent=True))
        result = get_public_customers(
            q=args["q"],
            rut=args["rut"],
            customer_id=args["id"],
            limit=args["limit"],
            include_finance=actor_can_view_oc_finance(username, role_name),
        )
        if result is None:
            raise InternalAuthError("not_found", "Cliente no encontrado", status=404)
        data, truncated = result
        return jsonify(
            {
                "ok": True,
                "tool": "get_customer",
                "classification": "CONFIDENTIAL",
                "data": data,
                "meta": {"limit": args["limit"], "truncated": truncated, "environment": environment},
            }
        )
    except InternalAuthError as exc:
        return error_response(exc)


@internal_agent_bp.route("/ventas/suppliers", methods=["POST"])
def ventas_suppliers():
    try:
        environment = authenticate_m2m(request)
        actor = actor_username(request)
        username, role_name = resolve_actor(actor)
        require_mod_ventas(username, role_name)
        args = validate_supplier_args(request.get_json(silent=True))
        result = get_public_suppliers(
            q=args["q"],
            rut=args["rut"],
            rut_raw=args["rut_raw"],
            supplier_id=args["id"],
            limit=args["limit"],
        )
        if result is None:
            raise InternalAuthError("not_found", "Proveedor no encontrado", status=404)
        data, truncated = result
        return jsonify(
            {
                "ok": True,
                "tool": "get_supplier",
                "classification": "CONFIDENTIAL",
                "data": data,
                "meta": {"limit": args["limit"], "truncated": truncated, "environment": environment},
            }
        )
    except InternalAuthError as exc:
        return error_response(exc)


@internal_agent_bp.route("/dashboard/kpis", methods=["POST"])
def dashboard_kpis():
    try:
        environment = authenticate_m2m(request)
        actor = actor_username(request)
        username, role_name = resolve_actor(actor)
        require_mod_dashboard(username, role_name)
        args = validate_dashboard_args(request.get_json(silent=True))
        result = get_public_dashboard_kpis(
            args,
            include_finance=actor_can_view_finanzas(username, role_name),
            include_stock=actor_can_view_stock(username, role_name),
        )
        meta = dict(result["meta"])
        meta["environment"] = environment
        return jsonify(
            {
                "ok": True,
                "tool": "get_dashboard_kpis",
                "classification": "CONFIDENTIAL",
                "data": result["data"],
                "meta": meta,
            }
        )
    except InternalAuthError as exc:
        return error_response(exc)
