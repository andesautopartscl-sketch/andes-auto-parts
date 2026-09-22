"""Strict argument schemas. Extra fields and SQL-like payloads are rejected."""
from __future__ import annotations

import re
from typing import Any

FORBIDDEN_KEYS = frozenset({
    "sql",
    "query_raw",
    "raw_sql",
    "eval",
    "shell",
    "command",
    "cmd",
    "url",
    "headers",
    "order_by",
    "table",
    "column",
    "password",
    "token",
    "authorization",
    "cookie",
    "cookies",
    "api_key",
    "apikey",
    "secret",
    "access_token",
    "bearer",
})

SQL_VALUE_RE = re.compile(
    r"\b(SELECT|INSERT|UPDATE|DELETE|DROP|UNION|ALTER|EXEC|MERGE|TRUNCATE)\b",
    re.IGNORECASE,
)

_CODE_RE = re.compile(r"^[A-Z0-9._-]{1,64}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class SchemaError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = 400


def _as_object(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SchemaError("invalid_args", f"{label} must be an object")
    return value


def _reject_forbidden(payload: Any) -> None:
    if isinstance(payload, dict):
        for key, val in payload.items():
            lowered = str(key).strip().lower()
            if lowered in FORBIDDEN_KEYS:
                raise SchemaError("invalid_args", f"Forbidden argument '{key}'")
            _reject_forbidden(val)
    elif isinstance(payload, list):
        for item in payload:
            _reject_forbidden(item)
    elif isinstance(payload, str):
        if SQL_VALUE_RE.search(payload):
            raise SchemaError("invalid_args", "SQL is not allowed in arguments")


def _require_keys(data: dict[str, Any], allowed: set[str]) -> None:
    extra = set(data.keys()) - allowed
    if extra:
        raise SchemaError("invalid_args", f"Unknown argument(s): {', '.join(sorted(extra))}")


def _opt_int(data: dict[str, Any], key: str, minimum: int, maximum: int) -> int | None:
    if key not in data:
        return None
    raw = data[key]
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise SchemaError("invalid_args", f"'{key}' must be an integer")
    if raw < minimum or raw > maximum:
        raise SchemaError("invalid_args", f"'{key}' must be between {minimum} and {maximum}")
    return raw


def _req_str(data: dict[str, Any], key: str, *, min_len: int = 1, max_len: int = 80) -> str:
    if key not in data or not isinstance(data[key], str):
        raise SchemaError("invalid_args", f"'{key}' must be a string")
    value = data[key].strip()
    if len(value) < min_len or len(value) > max_len:
        raise SchemaError("invalid_args", f"'{key}' length is invalid")
    return value


def _opt_str(data: dict[str, Any], key: str, *, min_len: int = 1, max_len: int = 80) -> str | None:
    if key not in data:
        return None
    return _req_str(data, key, min_len=min_len, max_len=max_len)


def _codigo(value: str) -> str:
    code = value.strip().upper()
    if not _CODE_RE.match(code):
        raise SchemaError("invalid_args", "Invalid product code")
    return code


def validate_tool_arguments(tool: str, arguments: Any) -> dict[str, Any]:
    data = _as_object(arguments, "arguments")
    _reject_forbidden(data)
    validators = {
        "search_catalog": _search_catalog,
        "get_product": _get_product,
        "get_inventory": _get_inventory,
        "check_stock": _check_stock,
        "get_stock_movements": _get_stock_movements,
        "get_ingresos": _get_ingresos,
        "get_purchase_orders": _get_purchase_orders,
        "get_sales": _get_sales,
        "get_orders": _get_orders,
        "get_equivalences": _get_equivalences,
        "get_customer": _get_party,
        "get_supplier": _get_party,
        "get_dashboard_kpis": _get_dashboard_kpis,
    }
    fn = validators.get(tool)
    if fn is None:
        raise SchemaError("tool_not_allowed", "Tool no permitida")
    return fn(data)


def _clamp_int(data: dict[str, Any], key: str, minimum: int, maximum: int, default: int) -> int:
    if key not in data:
        return default
    raw = data[key]
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise SchemaError("invalid_args", f"'{key}' must be an integer")
    return max(minimum, min(maximum, raw))


def _search_catalog(data: dict[str, Any]) -> dict[str, Any]:
    _require_keys(data, {"q", "limit"})
    q = _req_str(data, "q", min_len=2, max_len=80)
    limit = _clamp_int(data, "limit", 1, 25, 10)
    return {"q": q, "limit": limit}


def _get_product(data: dict[str, Any]) -> dict[str, Any]:
    _require_keys(data, {"codigo"})
    return {"codigo": _codigo(_req_str(data, "codigo", max_len=64))}


def _get_inventory(data: dict[str, Any]) -> dict[str, Any]:
    _require_keys(data, {"codigo", "marca", "bodega"})
    out = {"codigo": _codigo(_req_str(data, "codigo", max_len=64))}
    marca = _opt_str(data, "marca", max_len=120)
    bodega = _opt_str(data, "bodega", max_len=120)
    if marca:
        out["marca"] = marca.upper()
    if bodega:
        out["bodega"] = bodega
    return out


def _check_stock(data: dict[str, Any]) -> dict[str, Any]:
    _require_keys(data, {"items"})
    items = data.get("items")
    if not isinstance(items, list) or not items or len(items) > 20:
        raise SchemaError("invalid_args", "'items' must be a non-empty list of at most 20 entries")
    clean = []
    for row in items:
        row = _as_object(row, "items[]")
        _require_keys(row, {"codigo", "cantidad", "marca", "bodega"})
        if not isinstance(row.get("cantidad"), int) or isinstance(row.get("cantidad"), bool) or row["cantidad"] < 1:
            raise SchemaError("invalid_args", "Each item.cantidad must be a positive integer")
        item = {"codigo": _codigo(str(row["codigo"])), "cantidad": row["cantidad"]}
        if "marca" in row:
            item["marca"] = _req_str(row, "marca", max_len=64).upper()
        if "bodega" in row:
            item["bodega"] = _req_str(row, "bodega", max_len=64)
        clean.append(item)
    return {"items": clean}


def _get_stock_movements(data: dict[str, Any]) -> dict[str, Any]:
    _require_keys(data, {"codigo", "fecha_desde", "fecha_hasta", "limit"})
    out = {"codigo": _codigo(_req_str(data, "codigo", max_len=64))}
    for key in ("fecha_desde", "fecha_hasta"):
        if key in data:
            value = _req_str(data, key, min_len=10, max_len=10)
            if not _DATE_RE.match(value):
                raise SchemaError("invalid_args", f"'{key}' must be YYYY-MM-DD")
            out[key] = value
    if "fecha_desde" in out and "fecha_hasta" in out and out["fecha_desde"] > out["fecha_hasta"]:
        raise SchemaError("invalid_args", "fecha_desde must be <= fecha_hasta")
    limit = _opt_int(data, "limit", 1, 50)
    out["limit"] = limit or 20
    return out


def _get_ingresos(data: dict[str, Any]) -> dict[str, Any]:
    _require_keys(data, {"codigo", "proveedor", "numero_documento", "fecha_desde", "fecha_hasta", "limit"})
    out: dict[str, Any] = {}
    codigo = _opt_str(data, "codigo", max_len=64)
    if codigo:
        out["codigo"] = _codigo(codigo)
    proveedor = _opt_str(data, "proveedor", max_len=120)
    if proveedor:
        out["proveedor"] = proveedor
    numero = _opt_str(data, "numero_documento", max_len=60)
    if numero:
        out["numero_documento"] = numero
    for key in ("fecha_desde", "fecha_hasta"):
        if key in data:
            value = _req_str(data, key, min_len=10, max_len=10)
            if not _DATE_RE.match(value):
                raise SchemaError("invalid_args", f"'{key}' must be YYYY-MM-DD")
            out[key] = value
    if "fecha_desde" in out and "fecha_hasta" in out and out["fecha_desde"] > out["fecha_hasta"]:
        raise SchemaError("invalid_args", "fecha_desde must be <= fecha_hasta")
    limit = _opt_int(data, "limit", 1, 20)
    out["limit"] = limit or 20
    return out


def _get_equivalences(data: dict[str, Any]) -> dict[str, Any]:
    """FASE 8.8 — cruce OEM. Exige ancla; sin ella no es una equivalencia."""
    _require_keys(data, {"oem", "codigo", "marca", "modelo", "limit"})
    out: dict[str, Any] = {}
    oem = _opt_str(data, "oem", max_len=64)
    if oem:
        out["oem"] = oem
    codigo = _opt_str(data, "codigo", max_len=64)
    if codigo:
        out["codigo"] = codigo.upper()
    for key in ("marca", "modelo"):
        value = _opt_str(data, key, max_len=64)
        if value:
            out[key] = value
    if not out.get("oem") and not out.get("codigo"):
        raise SchemaError("invalid_args", "get_equivalences requires 'oem' or 'codigo'")
    limit = data.get("limit")
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 20):
            raise SchemaError("invalid_args", "'limit' must be between 1 and 20")
        out["limit"] = limit
    return out


def _get_orders(data: dict[str, Any]) -> dict[str, Any]:
    """FASE 9.2 — argumentos de ordenes de cliente.

    `anulada` se admite solo si se nombra: excluirla por defecto no es una
    restriccion de permisos sino de correccion, igual que rechazar orden_compra
    en get_sales. Sumar una orden anulada a los ingresos invierte el signo de la
    realidad, y el payload declara que las excluyo.
    """
    _require_keys(data, {"codigo", "cliente", "estados", "group_by",
                         "fecha_desde", "fecha_hasta", "limit"})
    out: dict[str, Any] = {}
    codigo = _opt_str(data, "codigo", max_len=64)
    if codigo:
        out["codigo"] = codigo.upper()
    cliente = _opt_str(data, "cliente", max_len=120)
    if cliente:
        out["cliente"] = cliente
    group_by = _opt_str(data, "group_by", max_len=8)
    if group_by:
        group_by = group_by.lower()
        if group_by not in {"mes", "dia"}:
            raise SchemaError("invalid_args", "'group_by' must be mes|dia")
        out["group_by"] = group_by
    raw_estados = data.get("estados")
    if raw_estados not in (None, "", []):
        if not isinstance(raw_estados, list) or len(raw_estados) > 4:
            raise SchemaError("invalid_args", "'estados' must be a list of at most 4")
        estados = []
        for entry in raw_estados:
            if not isinstance(entry, str):
                raise SchemaError("invalid_args", "'estados' entries must be strings")
            text = entry.strip().lower()
            if text not in {"pagada", "recibida", "anulada"}:
                raise SchemaError("invalid_args", "'estados' has a value not allowed")
            estados.append(text)
        out["estados"] = estados
    for key in ("fecha_desde", "fecha_hasta"):
        value = _opt_str(data, key, max_len=10)
        if value:
            if not _DATE_RE.match(value):
                raise SchemaError("invalid_args", f"'{key}' must be YYYY-MM-DD")
            out[key] = value
    out["limit"] = _clamp_int(data, "limit", 1, 20, 10)
    return out


def _get_sales(data: dict[str, Any]) -> dict[str, Any]:
    """FASE 8.6 — argumentos de ventas. orden_compra se rechaza aqui mismo:
    es una COMPRA, y colarla por la tool de ventas invertiria el signo del
    resultado. No es una restriccion de permisos, es de correccion."""
    _require_keys(data, {"codigo", "cliente", "estado", "tipos", "group_by",
                         "fecha_desde", "fecha_hasta", "limit"})
    out: dict[str, Any] = {}
    codigo = _opt_str(data, "codigo", max_len=64)
    if codigo:
        out["codigo"] = codigo.upper()
    cliente = _opt_str(data, "cliente", max_len=120)
    if cliente:
        out["cliente"] = cliente
    estado = _opt_str(data, "estado", max_len=40)
    if estado:
        out["estado"] = estado.lower()
    group_by = _opt_str(data, "group_by", max_len=8)
    if group_by:
        group_by = group_by.lower()
        if group_by not in {"mes", "dia"}:
            raise SchemaError("invalid_args", "'group_by' must be mes|dia")
        out["group_by"] = group_by
    raw_tipos = data.get("tipos")
    if raw_tipos not in (None, "", []):
        if not isinstance(raw_tipos, list) or len(raw_tipos) > 8:
            raise SchemaError("invalid_args", "'tipos' must be a list of at most 8")
        tipos = []
        for entry in raw_tipos:
            if not isinstance(entry, str):
                raise SchemaError("invalid_args", "'tipos' entries must be strings")
            text = entry.strip().lower()
            if text == "orden_compra":
                raise SchemaError("invalid_args", "orden_compra is not a sale")
            if text not in {"factura", "boleta", "orden_venta", "cotizacion"}:
                raise SchemaError("invalid_args", "'tipos' has a value not allowed")
            tipos.append(text)
        out["tipos"] = tipos
    for key in ("fecha_desde", "fecha_hasta"):
        value = _opt_str(data, key, max_len=10)
        if value:
            out[key] = _date_str(value, key) if "_date_str" in globals() else value
    limit = data.get("limit")
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 20):
            raise SchemaError("invalid_args", "'limit' must be between 1 and 20")
        out["limit"] = limit
    return out


def _get_purchase_orders(data: dict[str, Any]) -> dict[str, Any]:
    _require_keys(data, {"numero", "proveedor", "estado", "fecha_desde", "fecha_hasta", "codigo", "limit"})
    out: dict[str, Any] = {}
    numero = _opt_str(data, "numero", max_len=60)
    if numero:
        out["numero"] = numero.upper()
    proveedor = _opt_str(data, "proveedor", max_len=120)
    if proveedor:
        out["proveedor"] = proveedor
    estado = _opt_str(data, "estado", max_len=40)
    if estado:
        estado = estado.lower()
        if estado not in {"pendiente", "aprobada", "entregada", "anulada"}:
            raise SchemaError("invalid_args", "'estado' is not a valid OC status")
        out["estado"] = estado
    codigo = _opt_str(data, "codigo", max_len=64)
    if codigo:
        out["codigo"] = _codigo(codigo)
    for key in ("fecha_desde", "fecha_hasta"):
        if key in data:
            value = _req_str(data, key, min_len=10, max_len=10)
            if not _DATE_RE.match(value):
                raise SchemaError("invalid_args", f"'{key}' must be YYYY-MM-DD")
            out[key] = value
    if "fecha_desde" in out and "fecha_hasta" in out and out["fecha_desde"] > out["fecha_hasta"]:
        raise SchemaError("invalid_args", "fecha_desde must be <= fecha_hasta")
    limit = _opt_int(data, "limit", 1, 20)
    out["limit"] = limit or 20
    return out


def _get_party(data: dict[str, Any]) -> dict[str, Any]:
    _require_keys(data, {"q", "rut", "id", "limit"})
    out: dict[str, Any] = {}
    q = _opt_str(data, "q", max_len=80)
    rut = _opt_str(data, "rut", max_len=20)
    if q:
        out["q"] = q
    if rut:
        out["rut"] = rut
    if "id" in data:
        ident = data["id"]
        if isinstance(ident, bool) or not isinstance(ident, int) or ident < 1:
            raise SchemaError("invalid_args", "'id' must be a positive integer")
        out["id"] = ident
    if not out:
        raise SchemaError("invalid_args", "Provide q, rut or id")
    limit = _opt_int(data, "limit", 1, 20)
    out["limit"] = limit or 20
    return out


def _get_dashboard_kpis(data: dict[str, Any]) -> dict[str, Any]:
    from datetime import datetime

    _require_keys(data, {"periodo", "fecha_desde", "fecha_hasta", "top_limit", "stock_threshold", "stock_limit"})
    out: dict[str, Any] = {}
    if "periodo" in data:
        periodo = _req_str(data, "periodo", min_len=1, max_len=20).lower()
        if periodo not in {"snapshot", "hoy", "mes", "7d", "30d", "custom"}:
            raise SchemaError("invalid_args", "'periodo' is not valid")
        out["periodo"] = periodo
    for key in ("fecha_desde", "fecha_hasta"):
        if key in data:
            value = _req_str(data, key, min_len=10, max_len=10)
            if not _DATE_RE.match(value):
                raise SchemaError("invalid_args", f"'{key}' must be YYYY-MM-DD")
            out[key] = value
    if ("fecha_desde" in out) ^ ("fecha_hasta" in out):
        raise SchemaError("invalid_args", "Provide both fecha_desde and fecha_hasta")
    if "fecha_desde" in out and "fecha_hasta" in out:
        if out["fecha_desde"] > out["fecha_hasta"]:
            raise SchemaError("invalid_args", "fecha_desde must be <= fecha_hasta")
        start = datetime.strptime(out["fecha_desde"], "%Y-%m-%d").date()
        end = datetime.strptime(out["fecha_hasta"], "%Y-%m-%d").date()
        if (end - start).days + 1 > 90:
            raise SchemaError("invalid_args", "Date range must be at most 90 days")
        out["periodo"] = out.get("periodo") or "custom"
    top_limit = _opt_int(data, "top_limit", 1, 10)
    if top_limit is not None:
        out["top_limit"] = top_limit
    stock_threshold = _opt_int(data, "stock_threshold", 0, 10)
    if stock_threshold is not None:
        out["stock_threshold"] = stock_threshold
    stock_limit = _opt_int(data, "stock_limit", 1, 20)
    if stock_limit is not None:
        out["stock_limit"] = stock_limit
    return out
