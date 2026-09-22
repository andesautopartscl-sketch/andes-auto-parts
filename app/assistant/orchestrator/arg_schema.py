"""Lightweight argument validation mirroring Gateway contracts (defense in depth)."""
from __future__ import annotations

import re
from typing import Any

FORBIDDEN_KEYS = frozenset(
    {
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
        "endpoint",
        "tool",
    }
)

SQL_VALUE_RE = re.compile(
    r"\b(SELECT|INSERT|UPDATE|DELETE|DROP|UNION|ALTER|EXEC|MERGE|TRUNCATE)\b",
    re.IGNORECASE,
)
CODE_RE = re.compile(r"^[A-Z0-9._-]{1,64}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ArgSchemaError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _reject_forbidden(payload: Any) -> None:
    if isinstance(payload, dict):
        for key, val in payload.items():
            if str(key).strip().lower() in FORBIDDEN_KEYS:
                raise ArgSchemaError("invalid_args", f"Forbidden argument '{key}'")
            _reject_forbidden(val)
    elif isinstance(payload, list):
        for item in payload:
            _reject_forbidden(item)
    elif isinstance(payload, str):
        if SQL_VALUE_RE.search(payload):
            raise ArgSchemaError("invalid_args", "SQL is not allowed in arguments")
        if payload.strip().startswith("$steps."):
            return  # bindings validated separately after resolution


def _require_only(data: dict[str, Any], allowed: set[str]) -> None:
    extra = set(data.keys()) - allowed
    if extra:
        raise ArgSchemaError("invalid_args", f"Unknown argument(s): {', '.join(sorted(extra))}")


def _codigo(value: Any) -> str:
    if not isinstance(value, str):
        raise ArgSchemaError("invalid_args", "codigo must be a string")
    code = value.strip().upper()
    if not CODE_RE.match(code):
        raise ArgSchemaError("invalid_args", "Invalid product code")
    return code


def validate_tool_args(tool: str, arguments: Any) -> dict[str, Any]:
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ArgSchemaError("invalid_args", "arguments must be an object")
    _reject_forbidden(arguments)
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
        raise ArgSchemaError("tool_not_allowed", f"Unknown tool '{tool}'")
    return fn(arguments)


def _search_catalog(data: dict[str, Any]) -> dict[str, Any]:
    _require_only(data, {"q", "limit"})
    if "q" not in data or not isinstance(data["q"], str):
        raise ArgSchemaError("invalid_args", "'q' must be a string")
    q = data["q"].strip()
    if len(q) < 2 or len(q) > 80:
        raise ArgSchemaError("invalid_args", "'q' length is invalid")
    out: dict[str, Any] = {"q": q}
    if "limit" in data:
        lim = data["limit"]
        if isinstance(lim, bool) or not isinstance(lim, int) or lim < 1 or lim > 25:
            raise ArgSchemaError("invalid_args", "'limit' must be between 1 and 25")
        out["limit"] = lim
    else:
        out["limit"] = 10
    return out


def _get_product(data: dict[str, Any]) -> dict[str, Any]:
    _require_only(data, {"codigo"})
    if "codigo" not in data:
        raise ArgSchemaError("invalid_args", "'codigo' is required")
    return {"codigo": _codigo(data["codigo"])}


def _get_inventory(data: dict[str, Any]) -> dict[str, Any]:
    _require_only(data, {"codigo", "marca", "bodega"})
    if "codigo" not in data:
        raise ArgSchemaError("invalid_args", "'codigo' is required")
    out: dict[str, Any] = {"codigo": _codigo(data["codigo"]) if not str(data["codigo"]).startswith("$steps.") else data["codigo"]}
    if isinstance(data.get("codigo"), str) and data["codigo"].startswith("$steps."):
        out["codigo"] = data["codigo"]
    for key in ("marca", "bodega"):
        if key in data:
            if not isinstance(data[key], str):
                raise ArgSchemaError("invalid_args", f"'{key}' must be a string")
            out[key] = data[key].strip()
    return out


def _check_stock(data: dict[str, Any]) -> dict[str, Any]:
    _require_only(data, {"items"})
    items = data.get("items")
    if not isinstance(items, list) or not items or len(items) > 20:
        raise ArgSchemaError("invalid_args", "'items' must be a non-empty list of at most 20")
    clean = []
    for row in items:
        if not isinstance(row, dict):
            raise ArgSchemaError("invalid_args", "items[] must be objects")
        _require_only(row, {"codigo", "cantidad", "marca", "bodega"})
        if "codigo" not in row or "cantidad" not in row:
            raise ArgSchemaError("invalid_args", "items require codigo and cantidad")
        cantidad = row["cantidad"]
        if isinstance(cantidad, bool) or not isinstance(cantidad, int) or cantidad < 1:
            raise ArgSchemaError("invalid_args", "cantidad must be a positive integer")
        item = {"codigo": row["codigo"] if str(row["codigo"]).startswith("$steps.") else _codigo(row["codigo"]), "cantidad": cantidad}
        clean.append(item)
    return {"items": clean}


def _get_stock_movements(data: dict[str, Any]) -> dict[str, Any]:
    return _codigo_dated(data, {"codigo", "fecha_desde", "fecha_hasta", "limit"})


# FASE 8.8 — el maximo se declaraba DOS veces y no coincidian. Medido: el
# orquestador admitia limit=50 en get_customer/get_ingresos/get_purchase_orders/
# get_supplier y el Gateway los rechazaba con 400. Un plan valido aqui moria en
# la frontera exterior, y invalid_args es uno de los ejes que mide el benchmark.
# Gana el mas estrecho: es el que de verdad se puede ejecutar.
def _get_ingresos(data: dict[str, Any]) -> dict[str, Any]:
    # get_ingresos: el Gateway acota a 20. Declarar 50 aqui producia planes
    # validos que morian en la frontera exterior con invalid_args.
    return _codigo_dated(data, {"codigo", "fecha_desde", "fecha_hasta", "limit", "proveedor", "numero"},
                         max_limit=20)


def _codigo_dated(data: dict[str, Any], allowed: set[str], *, max_limit: int = 50) -> dict[str, Any]:
    _require_only(data, allowed)
    if "codigo" not in data:
        raise ArgSchemaError("invalid_args", "'codigo' is required")
    raw_code = data["codigo"]
    out: dict[str, Any] = {
        "codigo": raw_code if isinstance(raw_code, str) and raw_code.startswith("$steps.") else _codigo(raw_code)
    }
    for key in ("fecha_desde", "fecha_hasta"):
        if key in data:
            if not isinstance(data[key], str) or not DATE_RE.match(data[key].strip()):
                raise ArgSchemaError("invalid_args", f"'{key}' must be YYYY-MM-DD")
            out[key] = data[key].strip()
    if "limit" in data:
        lim = data["limit"]
        if isinstance(lim, bool) or not isinstance(lim, int) or lim < 1 or lim > max_limit:
            raise ArgSchemaError(
                "invalid_args", f"'limit' must be between 1 and {max_limit}")
        out["limit"] = lim
    return out


def _get_purchase_orders(data: dict[str, Any]) -> dict[str, Any]:
    _require_only(data, {"numero", "codigo", "estado", "proveedor", "fecha_desde", "fecha_hasta", "limit"})
    out: dict[str, Any] = {}
    for key in ("numero", "codigo", "estado", "proveedor", "fecha_desde", "fecha_hasta"):
        if key in data:
            if not isinstance(data[key], str):
                raise ArgSchemaError("invalid_args", f"'{key}' must be a string")
            out[key] = data[key].strip()
    if "limit" in data:
        lim = data["limit"]
        if isinstance(lim, bool) or not isinstance(lim, int) or lim < 1 or lim > 20:
            raise ArgSchemaError("invalid_args", "'limit' must be between 1 and 20")
        out["limit"] = lim
    return out


def _get_equivalences(data: dict[str, Any]) -> dict[str, Any]:
    """FASE 8.8. Se exige ancla (oem o codigo): sin ella la consulta devolveria
    un recorte arbitrario del catalogo que el agente presentaria como
    'equivalencias', y eso seria falso."""
    _require_only(data, {"oem", "codigo", "marca", "modelo", "limit"})
    out: dict[str, Any] = {}
    for key in ("oem", "codigo", "marca", "modelo"):
        if key in data:
            if not isinstance(data[key], str):
                raise ArgSchemaError("invalid_args", f"'{key}' must be a string")
            value = data[key].strip()
            if value:
                out[key] = value.upper() if key == "codigo" else value
    # Se lee del contrato: si un dia cambia el ancla, cambia en UN sitio y el
    # prompt, el validador y el aviso de reintento siguen diciendo lo mismo.
    from app.assistant.orchestrator.tool_contracts import TOOL_CONTRACTS

    any_of = (TOOL_CONTRACTS.get("get_equivalences") or {}).get("any_of") or ()
    if any_of and not any(out.get(k) for k in any_of):
        raise ArgSchemaError(
            "invalid_args",
            "get_equivalences requires one of: " + "|".join(any_of))
    if "limit" in data:
        lim = data["limit"]
        if isinstance(lim, bool) or not isinstance(lim, int) or lim < 1 or lim > 20:
            raise ArgSchemaError("invalid_args", "'limit' must be between 1 and 20")
        out["limit"] = lim
    return out


def _get_sales(data: dict[str, Any]) -> dict[str, Any]:
    """FASE 8.6. 'tipos' se acepta pero el ERP lo valida contra su lista cerrada:
    orden_compra se rechaza alli porque es una COMPRA y contarla como venta
    cambiaria el signo del resultado."""
    _require_only(data, {"codigo", "cliente", "estado", "tipos", "group_by",
                         "fecha_desde", "fecha_hasta", "limit"})
    out: dict[str, Any] = {}
    for key in ("codigo", "cliente", "estado", "group_by", "fecha_desde", "fecha_hasta"):
        if key in data:
            if not isinstance(data[key], str):
                raise ArgSchemaError("invalid_args", f"'{key}' must be a string")
            out[key] = data[key].strip()
    if "tipos" in data:
        raw = data["tipos"]
        if not isinstance(raw, list) or len(raw) > 8:
            raise ArgSchemaError("invalid_args", "'tipos' must be a list of at most 8")
        tipos = []
        for entry in raw:
            if not isinstance(entry, str):
                raise ArgSchemaError("invalid_args", "'tipos' entries must be strings")
            tipos.append(entry.strip().lower())
        out["tipos"] = tipos
    if "limit" in data:
        lim = data["limit"]
        if isinstance(lim, bool) or not isinstance(lim, int) or lim < 1 or lim > 20:
            raise ArgSchemaError("invalid_args", "'limit' must be between 1 and 20")
        out["limit"] = lim
    return out


def _get_orders(data: dict[str, Any]) -> dict[str, Any]:
    """FASE 9.2. `estados` se acepta pero NO se expone al modelo en el contrato:
    decidir que una orden anulada cuenta como venta es del sistema, no del
    modelo — es la misma trampa que `tipos` en get_sales, donde admitir
    orden_compra invertiria el signo del resultado."""
    _require_only(data, {"codigo", "cliente", "estados", "group_by",
                         "fecha_desde", "fecha_hasta", "limit"})
    out: dict[str, Any] = {}
    for key in ("codigo", "cliente", "group_by", "fecha_desde", "fecha_hasta"):
        if key in data:
            if not isinstance(data[key], str):
                raise ArgSchemaError("invalid_args", f"'{key}' must be a string")
            value = data[key].strip()
            if value:
                out[key] = value.upper() if key == "codigo" else value
    if "estados" in data:
        raw = data["estados"]
        if not isinstance(raw, list) or len(raw) > 4:
            raise ArgSchemaError("invalid_args", "'estados' must be a list of at most 4")
        estados = []
        for entry in raw:
            if not isinstance(entry, str):
                raise ArgSchemaError("invalid_args", "'estados' entries must be strings")
            estados.append(entry.strip().lower())
        out["estados"] = estados
    if "limit" in data:
        lim = data["limit"]
        if isinstance(lim, bool) or not isinstance(lim, int) or lim < 1 or lim > 20:
            raise ArgSchemaError("invalid_args", "'limit' must be between 1 and 20")
        out["limit"] = lim
    return out


def _get_party(data: dict[str, Any]) -> dict[str, Any]:
    _require_only(data, {"q", "rut", "id", "limit"})
    out: dict[str, Any] = {}
    for key in ("q", "rut"):
        if key in data:
            if not isinstance(data[key], str):
                raise ArgSchemaError("invalid_args", f"'{key}' must be a string")
            out[key] = data[key].strip()
    if "id" in data:
        raw = data["id"]
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
            raise ArgSchemaError("invalid_args", "'id' must be a positive integer")
        out["id"] = raw
    if "limit" in data:
        lim = data["limit"]
        if isinstance(lim, bool) or not isinstance(lim, int) or lim < 1 or lim > 20:
            raise ArgSchemaError("invalid_args", "'limit' must be between 1 and 20")
        out["limit"] = lim
    if not out.get("q") and not out.get("rut") and "id" not in out:
        raise ArgSchemaError("invalid_args", "Provide q, rut or id")
    return out


def _get_dashboard_kpis(data: dict[str, Any]) -> dict[str, Any]:
    _require_only(data, {"periodo", "fecha_desde", "fecha_hasta", "top_limit", "stock_threshold", "stock_limit"})
    out: dict[str, Any] = {}
    if "periodo" in data:
        if not isinstance(data["periodo"], str):
            raise ArgSchemaError("invalid_args", "'periodo' must be a string")
        periodo = data["periodo"].strip().lower()
        if periodo not in {"snapshot", "hoy", "mes", "7d", "30d", "custom"}:
            raise ArgSchemaError("invalid_args", "'periodo' is not valid")
        out["periodo"] = periodo
    for key in ("fecha_desde", "fecha_hasta"):
        if key in data:
            if not isinstance(data[key], str) or not DATE_RE.match(data[key].strip()):
                raise ArgSchemaError("invalid_args", f"'{key}' must be YYYY-MM-DD")
            out[key] = data[key].strip()
    for key, lo, hi in (("top_limit", 1, 10), ("stock_threshold", 0, 10), ("stock_limit", 1, 20)):
        if key in data:
            raw = data[key]
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < lo or raw > hi:
                raise ArgSchemaError("invalid_args", f"'{key}' out of range")
            out[key] = raw
    return out
