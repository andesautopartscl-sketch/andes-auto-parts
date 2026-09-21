"""get_sales — READ tool. Ventas agregadas + muestra de detalle.

Dos decisiones que no son de estilo:

1. **Agrega, no vuelca filas.** Medido en 8.2C: un payload de 19 filas se degrada
   a 9 y declara las omitidas. Un trimestre de ventas son miles de lineas. Si
   esta tool devolviera filas, el agente contaria sobre una lista truncada y
   publicaria un total falso con apariencia de estar grounded. Los agregados
   llegan calculados sobre el conjunto completo; el detalle viaja marcado como
   muestra (``detalle_parcial``).

2. **Finanzas las concede el ERP, no el Gateway.** Igual que get_ingresos y
   get_purchase_orders: los campos financieros se copian solo si el ERP los
   incluyo para ese actor.

Email, telefono, direccion y RUT del cliente NUNCA se copian, ni con permisos
financieros: el agente no necesita contactar a nadie.
"""
from __future__ import annotations

from typing import Any

from andes_agent.adapters.erp_http import AdapterError, ERPAdapter

NAME = "get_sales"
WRITE = False

PUBLIC_ITEM_FIELDS = (
    "fecha", "tipo", "numero", "estado", "cliente",
    "codigo", "descripcion", "marca", "bodega", "cantidad",
)
FINANCE_ITEM_FIELDS = ("precio_unitario", "subtotal")
PUBLIC_SCALARS = (
    "codigo", "cliente", "unidades", "documentos", "count",
    "neto_notas_credito", "incluye_cotizaciones", "detalle_parcial", "group_by",
)
FINANCE_SCALARS = ("ingresos",)
MAX_LIMIT = 20
MAX_SERIES = 24


def _error(code: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error_code": code,
        "message": message,
        "error": {"code": code, "message": message},
    }


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _public_item(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    item: dict[str, Any] = {}
    for key in PUBLIC_ITEM_FIELDS:
        value = row.get(key)
        item[key] = _as_int(value) if key == "cantidad" else str(value or "").strip()
    for key in FINANCE_ITEM_FIELDS:
        parsed = _as_float(row.get(key))
        if parsed is not None:
            item[key] = parsed
    return item


def _public_point(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    point = {
        "periodo": str(row.get("periodo") or "").strip(),
        "unidades": _as_int(row.get("unidades")),
        "documentos": _as_int(row.get("documentos")),
    }
    ingresos = _as_float(row.get("ingresos"))
    if ingresos is not None:
        point["ingresos"] = ingresos
    return point


def handle(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    context = context or {}
    actor = str(context.get("actor_user") or "").strip()
    if not actor:
        return _error("principal_required", "Human principal is required")

    adapter = context.get("adapter")
    if not isinstance(adapter, ERPAdapter):
        return _error("erp_unavailable", "ERP adapter is not configured")

    payload_args: dict[str, Any] = {}
    for key in ("codigo", "cliente", "estado", "group_by", "fecha_desde", "fecha_hasta"):
        value = arguments.get(key)
        if value:
            text = str(value).strip()
            payload_args[key] = text.upper() if key == "codigo" else text
    tipos = arguments.get("tipos")
    if isinstance(tipos, list) and tipos:
        payload_args["tipos"] = [str(t).strip().lower() for t in tipos[:8] if t]
    try:
        limit = int(arguments.get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    payload_args["limit"] = max(1, min(MAX_LIMIT, limit))

    try:
        payload = adapter.call("ventas/sales", payload_args, actor_user=actor, method="POST")
    except AdapterError as exc:
        message = exc.message or "Sales lookup failed"
        nested = exc.payload.get("error") if isinstance(exc.payload, dict) else None
        if isinstance(nested, dict) and nested.get("message"):
            message = str(nested.get("message") or message)
        return _error(exc.code, message)

    if not payload.get("ok"):
        code = str(payload.get("error_code")
                   or (payload.get("error") or {}).get("code") or "erp_error")
        message = str(payload.get("message")
                      or (payload.get("error") or {}).get("message")
                      or "Sales lookup failed")
        return _error(code, message)

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    out_data: dict[str, Any] = {}
    for key in PUBLIC_SCALARS:
        if key in data:
            out_data[key] = data.get(key)
    for key in FINANCE_SCALARS:
        parsed = _as_float(data.get(key))
        if parsed is not None:
            out_data[key] = parsed

    tipos_out = data.get("tipos")
    if isinstance(tipos_out, list):
        out_data["tipos"] = [str(t).strip().lower() for t in tipos_out if t]
    periodo = data.get("periodo")
    if isinstance(periodo, dict):
        out_data["periodo"] = {"desde": str(periodo.get("desde") or "") or None,
                               "hasta": str(periodo.get("hasta") or "") or None}
    nc = data.get("notas_credito")
    if isinstance(nc, dict):
        block = {"documentos": _as_int(nc.get("documentos")),
                 "unidades": _as_int(nc.get("unidades"))}
        monto = _as_float(nc.get("monto"))
        if monto is not None:
            block["monto"] = monto
        out_data["notas_credito"] = block

    series = []
    for row in (data.get("series") if isinstance(data.get("series"), list) else [])[:MAX_SERIES]:
        point = _public_point(row)
        if point and point["periodo"]:
            series.append(point)
    if series:
        out_data["series"] = series

    items = []
    for row in (data.get("items") if isinstance(data.get("items"), list) else []):
        item = _public_item(row)
        if item:
            items.append(item)
    limit_out = payload_args["limit"]
    if len(items) > limit_out:
        items = items[:limit_out]
    out_data["items"] = items
    out_data["count"] = len(items)

    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    return {
        "ok": True,
        "tool": NAME,
        "classification": "CONFIDENTIAL",
        "write": False,
        "data": out_data,
        "meta": {
            "limit": limit_out,
            "truncated": bool(meta.get("truncated")),
            "environment": str(context.get("environment") or meta.get("environment") or ""),
        },
    }
