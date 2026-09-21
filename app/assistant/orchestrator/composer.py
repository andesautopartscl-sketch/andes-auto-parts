"""Grounded answer composer — uses only evidence; no LLM; never fabricates facts."""
from __future__ import annotations

import json
import re
from typing import Any

# Values that must never appear in replies unless present in evidence JSON.
PII_LABELS = ("email", "correo", "teléfono", "telefono", "dirección", "direccion", "password")

_MOVEMENT_OPTIONAL = ("usuario", "observacion", "observación", "documento", "doc")
_PII_ROW_KEYS = frozenset(
    {"email", "correo", "telefono", "teléfono", "password", "token", "direccion", "dirección"}
)


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
            # FASE 8.x — un vacio EXPLICADO es mejor que uno generico y es
            # igual de grounded: "ningun producto declara ese OEM" es un hecho
            # del catalogo, no una inferencia. Este cortocircuito se comia las
            # ramas `not_found`/`no_oem_declared` de get_equivalences antes de
            # que el formateador pudiera hablar, asi que eran codigo muerto en
            # produccion desde 8.8 — por las DOS vias, oem y codigo. Se descubrio
            # midiendo O04 contra el ERP real; la prueba unitaria no lo veia
            # porque construia la evidencia con empty=False, un estado que el
            # pipeline nunca produce para una lista vacia.
            #
            # La regla es general: una tool puede declarar que su vacio es un
            # hecho emitiendo una de estas banderas. Sin bandera, nada cambia.
            explained = _format_tool_evidence(tool, item) if _explains_empty(item) else []
            lines.extend(explained or [f"`{tool}` no devolvió resultados."])
            continue

        lines.extend(_format_tool_evidence(tool, item))

        if item.get("finance_redacted"):
            lines.append(
                "Montos financieros no disponibles por permisos (null; no se reportan como 0)."
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


# Banderas con las que una tool declara que su resultado vacio es un HECHO
# comprobado y no una simple ausencia. Quien la emite se compromete a que su
# formateador sepa decirlo con palabras; sin bandera, el composer usa la frase
# generica de siempre.
EXPLAINED_EMPTY_FLAGS = ("not_found", "no_oem_declared")


def _explains_empty(item: dict[str, Any]) -> bool:
    data = item.get("data")
    if not isinstance(data, dict):
        return False
    return any(data.get(flag) for flag in EXPLAINED_EMPTY_FLAGS)


def _fmt(value: Any) -> str:
    if value is None:
        return "no disponible"
    return str(value)


def _present(row: dict[str, Any], *keys: str) -> Any | None:
    """Return first present non-None value among keys (key must exist or alias)."""
    for key in keys:
        if key in row and row.get(key) is not None and str(row.get(key)).strip() != "":
            return row.get(key)
    return None


def _sort_by_fecha_desc(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Presentation-only sort; does not invent dates."""
    def key(row: dict[str, Any]) -> str:
        return str(row.get("fecha") or "")

    return sorted(rows, key=key, reverse=True)


def _format_tool_evidence(tool: str, item: dict[str, Any]) -> list[str]:
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
    out: list[str] = []

    if tool == "search_catalog":
        items = data.get("items") if isinstance(data.get("items"), list) else []
        count = data["count"] if "count" in data else len(items)
        out.append(f"Catálogo — {_fmt(count)} resultado(s):")
        for row in items[:5]:
            if not isinstance(row, dict):
                continue
            codigo = _fmt(row.get("codigo"))
            desc = _fmt(row.get("descripcion"))
            marca = row.get("marca")
            modelo = row.get("modelo")
            line = f"• {codigo} — {desc}"
            extras = []
            if marca is not None and str(marca).strip() != "":
                extras.append(str(marca))
            if modelo is not None and str(modelo).strip() != "":
                extras.append(str(modelo))
            if extras:
                line += f" ({' '.join(extras)})"
            out.append(line)
        return out

    if tool == "get_product":
        out.append(f"Producto {_fmt(data.get('codigo'))}")
        if "descripcion" in data:
            out.append(f"Descripción: {_fmt(data.get('descripcion'))}")
        if "marca" in data:
            out.append(f"Marca: {_fmt(data.get('marca'))}")
        if "modelo" in data and data.get("modelo") is not None and str(data.get("modelo")).strip() != "":
            out.append(f"Modelo: {_fmt(data.get('modelo'))}")
        return out

    if tool == "get_inventory":
        out.append(f"Stock de {_fmt(data.get('codigo'))}")
        if "total_stock" in data:
            out.append(f"Total: {_fmt(data.get('total_stock'))}")
        rows = [r for r in (data.get("items") if isinstance(data.get("items"), list) else []) if isinstance(r, dict)]
        if rows:
            out.append("Por bodega:")
            for row in rows[:10]:
                bodega = _fmt(row.get("bodega")) if "bodega" in row else "—"
                stock = _fmt(row.get("stock")) if "stock" in row else "—"
                line = f"• {bodega}: {stock}"
                if "marca" in row and row.get("marca") is not None and str(row.get("marca")).strip() != "":
                    line += f" ({_fmt(row.get('marca'))})"
                out.append(line)
        return out

    if tool == "check_stock":
        if "available" in data:
            out.append(f"Disponibilidad: {_fmt(data.get('available'))}")
        for row in (data.get("items") if isinstance(data.get("items"), list) else [])[:10]:
            if isinstance(row, dict):
                parts = [f"• {_fmt(row.get('codigo'))}"]
                if "cantidad" in row:
                    parts.append(f"pedido {_fmt(row.get('cantidad'))}")
                if "disponible" in row:
                    parts.append(f"disponible {_fmt(row.get('disponible'))}")
                if "ok" in row:
                    parts.append(f"ok={_fmt(row.get('ok'))}")
                out.append(" — ".join(parts))
        return out

    if tool == "get_stock_movements":
        rows = [
            r
            for r in (data.get("items") if isinstance(data.get("items"), list) else [])
            if isinstance(r, dict)
        ]
        count = data.get("count") if "count" in data else len(rows)
        out.append(f"Movimientos de {_fmt(data.get('codigo'))} — {_fmt(count)} registro(s):")
        if not rows:
            return out
        ordered = _sort_by_fecha_desc(rows)[:10]
        for idx, row in enumerate(ordered, start=1):
            fecha = _present(row, "fecha")
            tipo = _present(row, "tipo")
            header_bits = [f"{idx}."]
            if fecha is not None:
                header_bits.append(_fmt(fecha))
            if tipo is not None:
                header_bits.append("·")
                header_bits.append(_fmt(tipo))
            out.append(" ".join(header_bits))
            if "cantidad" in row and row.get("cantidad") is not None:
                out.append(f"   Cantidad: {_fmt(row.get('cantidad'))}")
            if "bodega" in row and row.get("bodega") is not None:
                out.append(f"   Bodega: {_fmt(row.get('bodega'))}")
            if "marca" in row and row.get("marca") is not None and str(row.get("marca")).strip() != "":
                out.append(f"   Marca: {_fmt(row.get('marca'))}")
            for opt in _MOVEMENT_OPTIONAL:
                if opt in row and row.get(opt) is not None and str(row.get(opt)).strip() != "":
                    label = "Observación" if "observ" in opt.lower() else opt.capitalize()
                    if opt == "documento" or opt == "doc":
                        label = "Documento"
                    if opt == "usuario":
                        label = "Usuario"
                    out.append(f"   {label}: {_fmt(row.get(opt))}")
            if idx < len(ordered):
                out.append("")  # blank line between movements
        return out

    if tool == "get_ingresos":
        count = data.get("count") if "count" in data else len(data.get("items") or [])
        out.append(f"Ingresos de {_fmt(data.get('codigo'))} — {_fmt(count)} registro(s).")
        rows = [r for r in (data.get("items") if isinstance(data.get("items"), list) else []) if isinstance(r, dict)]
        for row in _sort_by_fecha_desc(rows)[:5]:
            bits = []
            if "fecha" in row and row.get("fecha") is not None:
                bits.append(_fmt(row.get("fecha")))
            if "numero" in row and row.get("numero") is not None:
                bits.append(f"nº {_fmt(row.get('numero'))}")
            if "proveedor" in row and row.get("proveedor") is not None:
                bits.append(_fmt(row.get("proveedor")))
            if bits:
                out.append("• " + " — ".join(bits))
        return out

    if tool == "get_purchase_orders":
        rows = [r for r in (data.get("items") if isinstance(data.get("items"), list) else []) if isinstance(r, dict)]
        count = data.get("count") if "count" in data else len(rows)
        out.append(f"Órdenes de compra — {_fmt(count)} documento(s):")
        if not rows and data.get("numero"):
            # Single-doc shape
            rows = [data]
        for row in rows[:8]:
            numero = _present(row, "numero", "oc")
            line = f"• {_fmt(numero)}" if numero is not None else "• (sin número)"
            extras = []
            if "fecha" in row and row.get("fecha") is not None:
                extras.append(_fmt(row.get("fecha")))
            if "estado" in row and row.get("estado") is not None:
                extras.append(_fmt(row.get("estado")))
            if "proveedor" in row and row.get("proveedor") is not None:
                extras.append(_fmt(row.get("proveedor")))
            if extras:
                line += " — " + " · ".join(extras)
            out.append(line)
        return out

    if tool == "get_dashboard_kpis":
        periodo = meta.get("periodo") if "periodo" in meta else None
        rango = ""
        if "fecha_desde" in meta or "fecha_hasta" in meta:
            rango = f" ({_fmt(meta.get('fecha_desde'))} → {_fmt(meta.get('fecha_hasta'))})"
        out.append(f"KPIs — período {_fmt(periodo)}{rango}")
        if "docs_periodo" in data:
            out.append(f"Documentos: {_fmt(data.get('docs_periodo'))}")
        if "ventas_periodo" in data:
            ventas = data.get("ventas_periodo")
            if ventas is None:
                out.append("Ventas: no disponible por permisos")
            else:
                out.append(f"Ventas: {_fmt(ventas)}")
        if "ventas_hoy" in data:
            v = data.get("ventas_hoy")
            out.append(
                "Ventas hoy: no disponible por permisos"
                if v is None
                else f"Ventas hoy: {_fmt(v)}"
            )
        if "ventas_mes" in data:
            v = data.get("ventas_mes")
            out.append(
                "Ventas mes: no disponible por permisos"
                if v is None
                else f"Ventas mes: {_fmt(v)}"
            )
        if "stock_critico" in data and data.get("stock_critico") is None:
            out.append("Stock crítico: omitido")
        elif isinstance(data.get("stock_critico"), list):
            out.append(f"Stock crítico: {len(data['stock_critico'])} ítem(s)")
        return out

    if tool == "get_sales":
        # FASE 8.9 — sin esto el composer caia a su rama generica y publicaba
        # "Campos en evidencia: count, detalle_parcial, documentos, ...", es
        # decir los NOMBRES de los campos. Medido con LLM real en V02: el
        # verifier descarto los claims, el composer tomo el relevo y el usuario
        # recibio un volcado de metadatos. Una tool nueva no esta entregada
        # hasta que sus DOS renderizados existen.
        out.append(f"Ventas — {_fmt(data.get('unidades'))} unidad(es) en "
                   f"{_fmt(data.get('documentos'))} documento(s).")
        periodo = data.get("periodo") if isinstance(data.get("periodo"), dict) else {}
        if periodo.get("desde") or periodo.get("hasta"):
            out.append(f"Periodo: {_fmt(periodo.get('desde'))} a {_fmt(periodo.get('hasta'))}.")
        elif "periodo" in data:
            # Callar el alcance cuando no hubo filtro es lo que dejo pasar la
            # respuesta de V02: cifras de abril presentadas como el trimestre
            # preguntado. Decirlo no resuelve la pregunta, pero impide que la
            # respuesta finja haberla resuelto.
            out.append("Periodo: sin filtro de fecha — cubre todo el historial.")
        if "ingresos" in data:
            out.append(f"Ingresos: {_fmt(data.get('ingresos'))}")
        nc = data.get("notas_credito") if isinstance(data.get("notas_credito"), dict) else {}
        if nc.get("unidades"):
            # Decirlo siempre: una cifra neta sin mencionar la devolucion parece
            # una venta que no ocurrio.
            out.append(f"Neto de {_fmt(nc.get('unidades'))} unidad(es) "
                       f"devuelta(s) en {_fmt(nc.get('documentos'))} nota(s) de credito.")
        rows = [r for r in (data.get("items") if isinstance(data.get("items"), list) else [])
                if isinstance(r, dict)]
        for row in rows[:5]:
            line = f"• {_fmt(row.get('fecha'))} {_fmt(row.get('numero'))} — {_fmt(row.get('codigo'))}"
            if row.get("cantidad") is not None:
                line += f" x{_fmt(row.get('cantidad'))}"
            out.append(line)
        if data.get("detalle_parcial"):
            out.append("El detalle es una muestra; los totales de arriba son el dato.")
        return out

    if tool == "get_equivalences":
        items = [r for r in (data.get("items") if isinstance(data.get("items"), list) else [])
                 if isinstance(r, dict)]
        if data.get("not_found"):
            # El hecho depende de por donde se busco: "no existe ese codigo" es
            # falso cuando lo que no aparece es un OEM, y al reves. El payload ya
            # trae `matched_on`, asi que no hay que adivinarlo.
            consulta = (data.get("query") or {}) if isinstance(data.get("query"), dict) else {}
            if data.get("matched_on") == "oem":
                out.append(f"Ningún producto del catálogo declara el OEM "
                           f"{_fmt(consulta.get('oem'))}.")
            else:
                out.append("No existe ese código en el catálogo.")
            return out
        if data.get("no_oem_declared"):
            out.append("El producto existe pero no declara código OEM.")
            return out
        out.append(f"Equivalencias — {_fmt(data.get('count') or len(items))} resultado(s):")
        for row in items[:5]:
            line = f"• {_fmt(row.get('codigo'))} — {_fmt(row.get('descripcion'))}"
            oem = row.get("oem")
            if isinstance(oem, list) and oem:
                line += f" (OEM {', '.join(str(o) for o in oem[:3])})"
            out.append(line)
            apps = row.get("aplicaciones")
            if isinstance(apps, list) and apps:
                # Etiquetado aparte: son modelos de vehiculo, no piezas
                # equivalentes, y confundirlos ofreceria un coche por un repuesto.
                out.append(f"  Aplicaciones: {', '.join(str(a) for a in apps[:4])}")
        return out

    if tool == "get_customer":
        items = data.get("items") if isinstance(data.get("items"), list) else []
        count = data.get("count") if "count" in data else len(items)
        out.append(f"Clientes — {_fmt(count)} resultado(s):")
        for row in items[:5]:
            if isinstance(row, dict):
                # Only grounded directory fields — never email/phone/address
                nombre = _fmt(row.get("nombre")) if "nombre" in row else "—"
                if "rut" in row and row.get("rut") is not None:
                    out.append(f"• {nombre} — RUT {_fmt(row.get('rut'))}")
                else:
                    out.append(f"• {nombre}")
        out.append("Nota: email, teléfono y dirección no se exponen en esta tool.")
        return out

    if tool == "get_supplier":
        items = data.get("items") if isinstance(data.get("items"), list) else []
        count = data.get("count") if "count" in data else len(items)
        out.append(f"Proveedores — {_fmt(count)} resultado(s):")
        for row in items[:5]:
            if not isinstance(row, dict):
                continue
            nombre = row.get("nombre")
            empresa = row.get("empresa")
            if nombre is not None and str(nombre).strip() != "":
                out.append(f"• {_fmt(nombre)}")
            if empresa is not None and str(empresa).strip() != "" and str(empresa) != str(nombre):
                out.append(f"  Empresa: {_fmt(empresa)}")
            if "rut" in row and row.get("rut") is not None:
                out.append(f"  RUT: {_fmt(row.get('rut'))}")
            ciudad = _present(row, "ciudad", "comuna")
            if ciudad is not None:
                out.append(f"  Ciudad: {_fmt(ciudad)}")
        out.append("Nota: email, teléfono y dirección no se exponen en esta tool.")
        return out

    keys = sorted(
        str(k)
        for k in data.keys()
        if str(k).strip().lower() not in _PII_ROW_KEYS
    )
    out.append(f"`{tool}` OK. Campos en evidencia: {', '.join(keys) if keys else '(vacío)'}.")
    return out
