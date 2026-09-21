"""Normalize Gateway tool results for composition and audit."""
from __future__ import annotations

from typing import Any

# Fields that must never reach the composer even if a buggy tool leaked them.
STRIP_PII_KEYS = frozenset(
    {
        "email",
        "correo",
        "telefono",
        "teléfono",
        "phone",
        "direccion",
        "dirección",
        "address",
        "password",
        "token",
        "authorization",
        "cookie",
    }
)


def _strip_pii(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, inner in value.items():
            if str(key).strip().lower() in STRIP_PII_KEYS:
                continue
            out[key] = _strip_pii(inner)
        return out
    if isinstance(value, list):
        return [_strip_pii(v) for v in value]
    return value


# FASE 8.7 — que herramientas devuelven dinero y por que campo se nota.
#
# El defecto que esto cierra: finance_redacted se calculaba SOLO para
# get_dashboard_kpis, asi que para ventas, ingresos u ordenes de compra el
# sistema omitia los montos sin decirlo. El composer tiene un aviso al usuario
# —"Montos financieros no disponibles por permisos (null; no se reportan como
# 0)"— que por eso nunca se emitia fuera del dashboard. Un usuario sin permiso
# veia una respuesta sin dinero y no tenia forma de distinguir "no te lo
# muestro" de "no hubo", que es exactamente la confusion null/cero que este
# sistema existe para impedir.
#
# Declarativo a proposito: anadir una tool financiera es anadir una fila aqui,
# no otro `if` por nombre.
FINANCE_TOOL_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    # tool: (campos financieros de nivel superior, campos financieros por fila)
    "get_sales": (("ingresos",), ("precio_unitario", "subtotal")),
    "get_ingresos": ((), ("costo_neto", "precio_venta_neto", "margen_pct")),
    "get_purchase_orders": ((), ("precio", "margen_pct", "subtotal")),
}


def _finance_withheld(tool: str, data: dict) -> bool:
    """True cuando la tool SI devuelve dinero y en esta respuesta no vino ninguno.

    Se exige que haya contenido: una respuesta vacia no tiene montos porque no
    tiene filas, no porque se hayan ocultado, y marcarla como redactada seria un
    falso positivo que acabaria en un aviso confuso al usuario.
    """
    scalars, row_fields = FINANCE_TOOL_FIELDS.get(tool, ((), ()))
    if any(key in data for key in scalars):
        return False
    rows = data.get("items") if isinstance(data.get("items"), list) else []
    rows = [r for r in rows if isinstance(r, dict)]
    if rows:
        if any(key in row for row in rows for key in row_fields):
            return False
        return bool(row_fields)
    # Sin filas y sin escalares no hay nada que ocultar.
    return False


def normalize_tool_result(status: int, body: Any) -> dict[str, Any]:
    """Normalize any Gateway response into a safe evidence envelope."""
    if body is None or not isinstance(body, dict):
        return {
            "ok": False,
            "status": int(status or 502),
            "tool": "",
            "classification": "INTERNAL",
            "write": False,
            "data": {},
            "meta": {},
            "empty": True,
            "error_code": "malformed_payload",
            "message": "Gateway returned a malformed payload",
            "finance_redacted": False,
            "stock_omitted": False,
        }

    # WRITE must never succeed through the orchestrator
    if body.get("write") is True:
        return {
            "ok": False,
            "status": 403,
            "tool": str(body.get("tool") or ""),
            "classification": "INTERNAL",
            "write": True,
            "data": {},
            "meta": {},
            "empty": True,
            "error_code": "write_not_allowed",
            "message": "WRITE responses are forbidden",
            "finance_redacted": False,
            "stock_omitted": False,
        }

    ok = bool(body.get("ok")) and 200 <= int(status) < 300
    error_code = None
    message = None
    if not ok:
        nested = body.get("error") if isinstance(body.get("error"), dict) else {}
        error_code = str(body.get("error_code") or nested.get("code") or "")
        message = str(body.get("message") or nested.get("message") or "Tool call failed")
        if int(status) == 403:
            error_code = error_code or "permission_denied"
        elif int(status) == 400:
            error_code = error_code or "invalid_args"
        elif int(status) in (502, 503, 504) or int(status) >= 500:
            error_code = error_code or "agent_unavailable"
        elif not error_code:
            error_code = "agent_error"

    raw_data = body.get("data")
    if raw_data is not None and not isinstance(raw_data, dict):
        return {
            "ok": False,
            "status": int(status or 502),
            "tool": str(body.get("tool") or ""),
            "classification": "INTERNAL",
            "write": False,
            "data": {},
            "meta": {},
            "empty": True,
            "error_code": "malformed_payload",
            "message": "Tool data must be an object",
            "finance_redacted": False,
            "stock_omitted": False,
        }

    data = _strip_pii(raw_data if isinstance(raw_data, dict) else {})
    meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}

    empty = False
    if ok:
        if "count" in data and int(data.get("count") or 0) == 0:
            empty = True
        items = data.get("items")
        if isinstance(items, list) and len(items) == 0 and "items" in data:
            empty = True

    finance_redacted = False
    stock_omitted = False
    tool_name = str(body.get("tool") or "")
    if ok and tool_name == "get_dashboard_kpis":
        # Preserve null ≠ 0 semantics
        for key in ("ventas_hoy", "ventas_mes", "ventas_periodo"):
            if key in data and data.get(key) is None:
                finance_redacted = True
        if data.get("stock_critico") is None or meta.get("stock_incluido") is False:
            stock_omitted = True
    elif ok and tool_name in FINANCE_TOOL_FIELDS:
        finance_redacted = _finance_withheld(tool_name, data)

    return {
        "ok": ok,
        "status": int(status),
        "tool": str(body.get("tool") or ""),
        "classification": str(body.get("classification") or "INTERNAL"),
        "write": False,
        "data": data,
        "meta": meta,
        "empty": empty,
        "error_code": error_code,
        "message": message,
        "finance_redacted": finance_redacted,
        "stock_omitted": stock_omitted,
    }
