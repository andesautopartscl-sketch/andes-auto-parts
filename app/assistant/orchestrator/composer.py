"""Grounded answer composer — uses only evidence; no LLM; never fabricates facts."""
from __future__ import annotations

import json
import re
from typing import Any

# Values that must never appear in replies unless present in evidence JSON.
PII_LABELS = ("email", "correo", "teléfono", "telefono", "dirección", "direccion", "password")


def compose_answer(
    *,
    plan: dict[str, Any],
    evidence: list[dict[str, Any]],
    reject_message: str | None = None,
) -> dict[str, Any]:
    if plan.get("reject"):
        return {
            "reply": str(reject_message or plan.get("reject_message") or "Solicitud rechazada."),
            "grounded": True,
            "classification": "INTERNAL",
        }
    if plan.get("needs_clarification"):
        return {
            "reply": str(plan.get("reject_message") or "Necesito más detalles para continuar."),
            "grounded": True,
            "classification": "INTERNAL",
        }

    if not evidence:
        return {
            "reply": "No tengo evidencia de herramientas para responder. No inventaré datos.",
            "grounded": True,
            "classification": "INTERNAL",
        }

    lines: list[str] = []
    classification = "INTERNAL"
    for item in evidence:
        tool = item.get("tool") or ""
        if item.get("classification") == "CONFIDENTIAL":
            classification = "CONFIDENTIAL"
        if not item.get("ok"):
            code = item.get("error_code") or "error"
            if code == "permission_denied":
                lines.append(f"No tienes permiso para usar `{tool}`.")
            elif code in {"agent_unavailable", "agent_timeout", "erp_unavailable"}:
                lines.append(f"El servicio no está disponible al consultar `{tool}`.")
            elif code == "malformed_payload":
                lines.append(f"La respuesta de `{tool}` fue inválida; no inventaré datos.")
            elif code == "write_not_allowed":
                lines.append("No puedo ejecutar operaciones de escritura.")
            else:
                lines.append(f"No pude completar `{tool}` ({code}).")
            continue

        if item.get("empty"):
            lines.append(f"`{tool}` no devolvió resultados.")
            continue

        lines.extend(_format_tool_evidence(tool, item))

        if item.get("finance_redacted"):
            lines.append(
                "Montos financieros no disponibles por permiso (null; no se reportan como 0)."
            )
        if item.get("stock_omitted") and tool == "get_dashboard_kpis":
            lines.append("Stock crítico no incluido (sin permiso ver_stock).")

    if not lines:
        return {
            "reply": "La consulta se ejecutó pero no hay datos para mostrar. No inventaré información.",
            "grounded": True,
            "classification": classification,
        }

    reply = "\n".join(lines)
    reply = _scrub_leaked_pii(reply, evidence)
    # Soft ground check: never crash the chat; replace ungrounded replies
    if not reply_contains_only_evidence_values(reply, evidence):
        reply = (
            "Solo puedo reportar valores presentes en la evidencia de las tools. "
            "No inventaré códigos, montos ni datos de contacto."
        )

    return {"reply": reply, "grounded": True, "classification": classification}


def _evidence_blob(evidence: list[dict[str, Any]]) -> str:
    return json.dumps(
        [{"data": e.get("data"), "meta": e.get("meta"), "tool": e.get("tool")} for e in evidence],
        ensure_ascii=False,
        default=str,
    )


def assert_reply_grounded(reply: str, evidence: list[dict[str, Any]]) -> None:
    """Raise AssertionError if reply invents product-like codes not in evidence.

    Only flags tokens that look like product codes (letters+digits), not plain
    integers or period labels already present as prose.
    """
    blob = _evidence_blob(evidence).upper()
    tool_names = {str(e.get("tool") or "").upper() for e in evidence}
    # Require at least one letter AND one digit INSIDE the token (product codes)
    for token in re.findall(
        r"\b(?=[A-Z0-9._-]{3,64}\b)(?=[A-Z0-9._-]*[A-Z])(?=[A-Z0-9._-]*\d)[A-Z0-9._-]+\b",
        reply.upper(),
    ):
        if token in tool_names:
            continue
        if token not in blob:
            raise AssertionError(f"Ungrounded code in reply: {token}")


def reply_contains_only_evidence_values(reply: str, evidence: list[dict[str, Any]]) -> bool:
    try:
        assert_reply_grounded(reply, evidence)
        return True
    except AssertionError:
        return False


def _scrub_leaked_pii(reply: str, evidence: list[dict[str, Any]]) -> str:
    blob = _evidence_blob(evidence).lower()
    kept: list[str] = []
    for line in reply.splitlines():
        lower = line.lower()
        # Keep explicit "not exposed" disclaimers
        if "no se exponen" in lower or "no están disponibles" in lower:
            kept.append(line)
            continue
        # Drop lines that look like leaked contact values
        if "@" in line and "@" not in blob:
            continue
        if any(label in lower for label in ("email:", "correo:", "teléfono:", "telefono:", "dirección:", "direccion:")):
            if not any(label in blob for label in ("email", "telefono", "direccion")):
                continue
        kept.append(line)
    if not kept:
        return (
            "Consulté los datos disponibles. Email, teléfono y dirección no se exponen en el asistente."
        )
    return "\n".join(kept)


def _fmt(value: Any) -> str:
    if value is None:
        return "no disponible"
    return str(value)


def _format_tool_evidence(tool: str, item: dict[str, Any]) -> list[str]:
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
    out: list[str] = []

    if tool == "search_catalog":
        items = data.get("items") if isinstance(data.get("items"), list) else []
        count = data["count"] if "count" in data else len(items)
        out.append(f"Catálogo: {_fmt(count)} resultado(s).")
        for row in items[:5]:
            if not isinstance(row, dict):
                continue
            out.append(
                f"- {_fmt(row.get('codigo'))}: {_fmt(row.get('descripcion'))} "
                f"({_fmt(row.get('marca'))} {_fmt(row.get('modelo'))})".strip()
            )
        return out

    if tool == "get_product":
        out.append(
            f"Producto {_fmt(data.get('codigo'))}: {_fmt(data.get('descripcion'))} "
            f"| marca {_fmt(data.get('marca'))}"
        )
        return out

    if tool == "get_inventory":
        total = data.get("total_stock") if "total_stock" in data else None
        out.append(f"Stock de {_fmt(data.get('codigo'))}: total {_fmt(total)}.")
        for row in (data.get("items") if isinstance(data.get("items"), list) else [])[:10]:
            if isinstance(row, dict):
                out.append(
                    f"- bodega {_fmt(row.get('bodega'))}: {_fmt(row.get('stock'))} "
                    f"({_fmt(row.get('marca'))})"
                )
        return out

    if tool == "check_stock":
        out.append(f"Disponibilidad: available={_fmt(data.get('available'))}.")
        for row in (data.get("items") if isinstance(data.get("items"), list) else [])[:10]:
            if isinstance(row, dict):
                out.append(
                    f"- {_fmt(row.get('codigo'))}: pedido {_fmt(row.get('cantidad'))}, "
                    f"disponible {_fmt(row.get('disponible'))}, ok={_fmt(row.get('ok'))}"
                )
        return out

    if tool == "get_stock_movements":
        count = data.get("count") if "count" in data else len(data.get("items") or [])
        out.append(f"Movimientos de {_fmt(data.get('codigo'))}: {_fmt(count)} registro(s).")
        return out

    if tool == "get_ingresos":
        count = data.get("count") if "count" in data else len(data.get("items") or [])
        out.append(f"Ingresos de {_fmt(data.get('codigo'))}: {_fmt(count)} registro(s).")
        return out

    if tool == "get_purchase_orders":
        count = data.get("count") if "count" in data else len(data.get("items") or [])
        out.append(f"Órdenes de compra: {_fmt(count)} documento(s).")
        return out

    if tool == "get_dashboard_kpis":
        periodo = meta.get("periodo") if "periodo" in meta else None
        out.append(
            f"KPIs período `{_fmt(periodo)}` "
            f"({_fmt(meta.get('fecha_desde'))} → {_fmt(meta.get('fecha_hasta'))})."
        )
        if "docs_periodo" in data:
            out.append(f"Documentos en período: {_fmt(data.get('docs_periodo'))}.")
        if "ventas_periodo" in data:
            ventas = data.get("ventas_periodo")
            if ventas is None:
                out.append("Ventas del período: no disponibles (sin permiso financiero).")
            else:
                out.append(f"Ventas del período: {_fmt(ventas)}.")
        if "stock_critico" in data and data.get("stock_critico") is None:
            out.append("stock_critico: omitido.")
        elif isinstance(data.get("stock_critico"), list):
            out.append(f"stock_critico: {len(data['stock_critico'])} ítem(s).")
        return out

    if tool == "get_customer":
        items = data.get("items") if isinstance(data.get("items"), list) else []
        count = data.get("count") if "count" in data else len(items)
        out.append(f"Clientes: {_fmt(count)} resultado(s).")
        for row in items[:5]:
            if isinstance(row, dict):
                # Only grounded directory fields — never email/phone/address
                out.append(f"- {_fmt(row.get('nombre'))} (RUT {_fmt(row.get('rut'))})")
        out.append("Nota: email, teléfono y dirección no se exponen en esta tool.")
        return out

    if tool == "get_supplier":
        items = data.get("items") if isinstance(data.get("items"), list) else []
        count = data.get("count") if "count" in data else len(items)
        out.append(f"Proveedores: {_fmt(count)} resultado(s).")
        for row in items[:5]:
            if isinstance(row, dict):
                out.append(f"- {_fmt(row.get('nombre') or row.get('empresa'))}")
        out.append("Nota: email, teléfono y dirección no se exponen en esta tool.")
        return out

    keys = sorted(str(k) for k in data.keys())
    out.append(f"`{tool}` OK. Campos en evidencia: {', '.join(keys) if keys else '(vacío)'}.")
    return out
