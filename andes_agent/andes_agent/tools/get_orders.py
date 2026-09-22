"""get_orders — READ tool. Ordenes de cliente: agregados + muestra de detalle.

Por que existe, medido en la base real: ``oc_clientes`` tiene 71 ordenes con 93
lineas y precio unitario al 100%, y ``ventas_documentos`` —la fuente de
``get_sales``— tiene 11 documentos. Donde el negocio transacta de verdad no
miraba ninguna tool.

Tres reglas heredadas de get_sales, por las mismas razones medidas:

1. **Agrega, no vuelca filas.** Los agregados llegan calculados por el ERP sobre
   el conjunto completo; el detalle viaja marcado con ``detalle_parcial``. Si la
   tool devolviera filas, el agente contaria sobre una lista truncada y
   publicaria un total falso con apariencia de estar grounded.

2. **Las finanzas las concede el ERP, no el Gateway.** Los campos de dinero se
   copian solo si el ERP los incluyo para ese actor.

3. **Allowlist de salida.** Lo que no esta enumerado aqui no se copia. La
   identidad del cliente, la direccion de despacho, el vendedor, el usuario, las
   referencias de pago y las observaciones no salen de la frontera interna, ni
   con permisos financieros.
"""
from __future__ import annotations

from typing import Any

from andes_agent.adapters.erp_http import AdapterError, ERPAdapter

NAME = "get_orders"
WRITE = False

PUBLIC_ITEM_FIELDS = ("numero_oc", "fecha", "estado", "codigo", "descripcion",
                      "marca", "cantidad")
FINANCE_ITEM_FIELDS = ("precio_unitario", "subtotal")
PUBLIC_SCALARS = (
    "ordenes", "lineas", "unidades", "count", "codigo",
    "estados_consultados", "anuladas_excluidas", "detalle_parcial", "group_by",
)
FINANCE_SCALARS = ("monto_lineas",)
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
    return {
        "periodo": str(row.get("periodo") or "").strip(),
        "unidades": _as_int(row.get("unidades")),
        "lineas": _as_int(row.get("lineas")),
    }


def _counts(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    return {str(k).strip().lower(): _as_int(v) for k, v in raw.items() if k}


def handle(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    context = context or {}
    actor = str(context.get("actor_user") or "").strip()
    if not actor:
        return _error("principal_required", "Human principal is required")

    adapter = context.get("adapter")
    if not isinstance(adapter, ERPAdapter):
        return _error("erp_unavailable", "ERP adapter is not configured")

    payload_args: dict[str, Any] = {}
    for key in ("codigo", "cliente", "group_by", "fecha_desde", "fecha_hasta"):
        value = arguments.get(key)
        if value:
            text = str(value).strip()
            payload_args[key] = text.upper() if key == "codigo" else text
    estados = arguments.get("estados")
    if isinstance(estados, list) and estados:
        payload_args["estados"] = [str(e).strip().lower() for e in estados[:4] if e]
    try:
        limit = int(arguments.get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    payload_args["limit"] = max(1, min(MAX_LIMIT, limit))

    try:
        payload = adapter.call("ventas/orders", payload_args, actor_user=actor, method="POST")
    except AdapterError as exc:
        message = exc.message or "Orders lookup failed"
        nested = exc.payload.get("error") if isinstance(exc.payload, dict) else None
        if isinstance(nested, dict) and nested.get("message"):
            message = str(nested.get("message") or message)
        return _error(exc.code, message)

    if not payload.get("ok"):
        code = str(payload.get("error_code")
                   or (payload.get("error") or {}).get("code") or "erp_error")
        message = str(payload.get("message")
                      or (payload.get("error") or {}).get("message")
                      or "Orders lookup failed")
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

    for key in ("lineas_por_estado", "ordenes_por_estado"):
        counts = _counts(data.get(key))
        if counts:
            out_data[key] = counts

    # El alcance viaja SIEMPRE, tambien cuando no hubo filtro: una cifra
    # agregada sin su ventana no es verificable (medido en V02, 8.x).
    periodo = data.get("periodo")
    if isinstance(periodo, dict):
        out_data["periodo"] = {"desde": str(periodo.get("desde") or "") or None,
                               "hasta": str(periodo.get("hasta") or "") or None}

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
    out_data["items"] = items

    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    return {
        "ok": True,
        "tool": NAME,
        "write": False,
        "classification": str(payload.get("classification") or "CONFIDENTIAL"),
        "data": out_data,
        "meta": {"limit": payload_args["limit"],
                 "truncated": bool(meta.get("truncated"))},
    }
