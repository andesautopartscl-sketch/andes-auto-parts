"""Conversational reference resolution and redacted summaries (FASE 5)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.assistant.orchestrator.normalizer import STRIP_PII_KEYS, _strip_pii

KIND_NONE = "none"
KIND_CLARIFY = "clarify"
KIND_REUSE = "reuse_evidence"
KIND_PLAN_HINTS = "plan_hints"

# Anaphora / follow-up signals (Spanish)
_ANAPHORA_RE = re.compile(
    r"\b("
    r"ese|esa|esos|esas|esto|esta|estos|estas|"
    r"el\s+primero|la\s+primera|el\s+segundo|"
    r"los\s+anteriores|las\s+anteriores|"
    r"cu[aá]les\s+son|"
    r"los\s+[uú]ltimos\s+movimientos|y\s+los\s+movimientos|"
    r"y\s+cu[aá]nto\s+queda|cu[aá]nto\s+queda|"
    r"qu[eé]\s+empresa|y\s+qu[eé]\s+empresa|"
    r"bodega|ciudad|comuna"
    r")\b",
    re.IGNORECASE,
)

_REUSE_LIST_RE = re.compile(
    r"\b(cu[aá]les\s+son|mu[eé]strame\s+los\s+anteriores|los\s+anteriores|"
    r"detalle|det[aá]llalos|detalle\s+de\s+esos)\b",
    re.IGNORECASE,
)
_REUSE_SUPPLIER_RE = re.compile(
    r"\b(qu[eé]\s+empresa|empresa\s+es|c[oó]mo\s+se\s+llama)\b",
    re.IGNORECASE,
)
_CIUDAD_RE = re.compile(
    r"\b(ciudad|comuna|de\s+qu[eé]\s+ciudad|en\s+qu[eé]\s+ciudad)\b",
    re.IGNORECASE,
)
_MOVEMENTS_RE = re.compile(
    r"\b(movimientos?|[uú]ltimos\s+movimientos)\b",
    re.IGNORECASE,
)
_STOCK_RE = re.compile(
    r"\b(stock|inventario|cu[aá]nto\s+queda|queda|cu[aá]nto\s+hay)\b",
    re.IGNORECASE,
)
_BODEGA_RE = re.compile(
    r"\b(bodega|en\s+qu[eé]\s+bodega|qu[eé]\s+bodega|d[oó]nde\s+est[aá])\b",
    re.IGNORECASE,
)
_SHOW_MOVEMENTS_BARE_RE = re.compile(
    r"^\s*(mu[eé]strame\s+)?(los\s+)?movimientos\.?\s*$",
    re.IGNORECASE,
)
_FIRST_RE = re.compile(r"\b(el\s+primero|la\s+primera|el\s+1(?:er)?)\b", re.IGNORECASE)
_ORDINAL_AMBIGUOUS_RE = re.compile(
    r"\b(ese\s+producto|esa\s+pieza|el\s+producto|esa\s+oc|ese\s+proveedor)\b",
    re.IGNORECASE,
)


@dataclass
class ResolveResult:
    kind: str = KIND_NONE
    intent_hint: str | None = None
    entities: dict[str, Any] = field(default_factory=dict)
    prior_evidence: list[dict[str, Any]] = field(default_factory=list)
    clarify_message: str | None = None
    tool_filter: str | None = None


def extract_entities_from_evidence(evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """Pull allowlisted entity ids from normalized tool evidence."""
    entities: dict[str, Any] = {
        "codigo": None,
        "codigos": [],
        "proveedor_q": None,
        "proveedor_nombre": None,
        "proveedor_empresa": None,
        "proveedor_ciudad": None,
        "oc_numero": None,
        "customer_q": None,
        "oem": None,
        # FASE 8.7 — el periodo es el ancla que necesitan las secuencias de
        # ventas: "muestrame las ventas del 2404" -> "comparame con el trimestre
        # anterior" no es resoluble sin saber sobre que ventana se respondio.
        # Se extrae de la evidencia, no del texto: es un hecho del turno, no una
        # interpretacion, y por eso no puede desviarse de lo que se consulto.
        "periodo_desde": None,
        "periodo_hasta": None,
        "last_tools": [],
    }
    codigos: list[str] = []

    for item in evidence or []:
        if not isinstance(item, dict):
            continue
        tool = str(item.get("tool") or "")
        if tool:
            entities["last_tools"].append(tool)
        if not item.get("ok") or item.get("empty"):
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}

        if tool == "search_catalog":
            for row in data.get("items") or []:
                if isinstance(row, dict) and row.get("codigo"):
                    code = str(row["codigo"]).strip().upper()
                    if code and code not in codigos:
                        codigos.append(code)
            if codigos:
                entities["codigo"] = codigos[0]

        elif tool in {"get_inventory", "get_product", "get_stock_movements", "get_ingresos"}:
            code = str(data.get("codigo") or "").strip().upper()
            if code:
                entities["codigo"] = code
                if code not in codigos:
                    codigos.insert(0, code)

        elif tool == "check_stock":
            for row in data.get("items") or []:
                if isinstance(row, dict) and row.get("codigo"):
                    code = str(row["codigo"]).strip().upper()
                    if code and code not in codigos:
                        codigos.append(code)
            if codigos and not entities["codigo"]:
                entities["codigo"] = codigos[0]

        elif tool == "get_supplier":
            items = data.get("items") if isinstance(data.get("items"), list) else []
            if items and isinstance(items[0], dict):
                row = items[0]
                entities["proveedor_nombre"] = str(row.get("nombre") or "").strip() or None
                entities["proveedor_empresa"] = str(row.get("empresa") or "").strip() or None
                ciudad = str(row.get("ciudad") or row.get("comuna") or "").strip()
                entities["proveedor_ciudad"] = ciudad or None
            meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
            if meta.get("q"):
                entities["proveedor_q"] = str(meta.get("q")).strip()

        elif tool == "get_equivalences":
            # El OEM consultado es el ancla de "¿y cual me sirve para el Chery?".
            query = data.get("query") if isinstance(data.get("query"), dict) else {}
            oem = str(query.get("oem") or "").strip()
            if oem:
                entities["oem"] = oem
            items = data.get("items") if isinstance(data.get("items"), list) else []
            for row in items:
                if isinstance(row, dict) and row.get("codigo"):
                    code = str(row["codigo"]).strip().upper()
                    if code and code not in codigos:
                        codigos.append(code)
            if codigos and not entities["codigo"]:
                entities["codigo"] = codigos[0]

        elif tool == "get_sales":
            periodo = data.get("periodo") if isinstance(data.get("periodo"), dict) else {}
            desde = str(periodo.get("desde") or "").strip()
            hasta = str(periodo.get("hasta") or "").strip()
            if desde:
                entities["periodo_desde"] = desde
            if hasta:
                entities["periodo_hasta"] = hasta
            codigo = str(data.get("codigo") or "").strip().upper()
            if codigo:
                entities["codigo"] = codigo
                if codigo not in codigos:
                    codigos.insert(0, codigo)

        elif tool == "get_customer":
            meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
            if meta.get("q"):
                entities["customer_q"] = str(meta.get("q")).strip()

        elif tool == "get_purchase_orders":
            items = data.get("items") if isinstance(data.get("items"), list) else []
            for row in items:
                if isinstance(row, dict) and (row.get("numero") or row.get("oc")):
                    entities["oc_numero"] = str(row.get("numero") or row.get("oc")).strip()
                    break
            if not entities["oc_numero"] and data.get("numero"):
                entities["oc_numero"] = str(data.get("numero")).strip()

    entities["codigos"] = codigos
    if not entities["codigo"] and codigos:
        entities["codigo"] = codigos[0]
    return _strip_pii(entities)


def merge_entities(*parts: dict[str, Any] | None) -> dict[str, Any]:
    """Merge entity maps; later non-empty values win for scalars; lists accumulate."""
    merged: dict[str, Any] = {
        "codigo": None,
        "codigos": [],
        "proveedor_q": None,
        "proveedor_nombre": None,
        "proveedor_empresa": None,
        "proveedor_ciudad": None,
        "oc_numero": None,
        "customer_q": None,
        # FASE 8.7 — el periodo es el ancla que necesitan las secuencias de
        # ventas: "muestrame las ventas del 2404" -> "comparame con el trimestre
        # anterior" no es resoluble sin saber sobre que ventana se respondio.
        # Se extrae de la evidencia, no del texto: es un hecho del turno, no una
        # interpretacion, y por eso no puede desviarse de lo que se consulto.
        "periodo_desde": None,
        "periodo_hasta": None,
        "last_tools": [],
    }
    for ents in parts:
        if not isinstance(ents, dict):
            continue
        for key in (
            "codigo",
            "proveedor_q",
            "proveedor_nombre",
            "proveedor_empresa",
            "proveedor_ciudad",
            "oc_numero",
            "customer_q",
        ):
            if ents.get(key):
                merged[key] = ents[key]
        for code in ents.get("codigos") or []:
            c = str(code).strip().upper()
            if c and c not in merged["codigos"]:
                merged["codigos"].append(c)
        for tool in ents.get("last_tools") or []:
            if tool and tool not in merged["last_tools"]:
                merged["last_tools"].append(tool)
    if not merged["codigo"] and merged["codigos"]:
        merged["codigo"] = merged["codigos"][0]
    return merged


def build_redacted_summary(turns: list[dict[str, Any]]) -> str:
    """Short multi-turn summary for the LLM — no PII keys, no secrets."""
    if not turns:
        return ""
    lines: list[str] = []
    for idx, turn in enumerate(turns[-6:], start=1):
        ents = turn.get("entities") if isinstance(turn.get("entities"), dict) else {}
        tools = turn.get("tools_used") or []
        parts = [f"turno {idx}"]
        if tools:
            parts.append("tools=" + ",".join(str(t) for t in tools[:3]))
        evid_tools = [
            str(e.get("tool"))
            for e in (turn.get("evidence") or [])
            if isinstance(e, dict) and e.get("tool") and e.get("ok") and not e.get("empty")
        ]
        if evid_tools:
            parts.append("evidence=" + ",".join(evid_tools[:4]))
        code = ents.get("codigo")
        if code:
            parts.append(f"codigo={code}")
        codes = ents.get("codigos") or []
        if isinstance(codes, list) and len(codes) > 1:
            parts.append("codigos=" + ",".join(str(c) for c in codes[:5]))
        if ents.get("proveedor_nombre") or ents.get("proveedor_empresa"):
            parts.append(
                "proveedor="
                + str(ents.get("proveedor_nombre") or ents.get("proveedor_empresa") or "")
            )
        if ents.get("proveedor_ciudad"):
            parts.append(f"ciudad={ents.get('proveedor_ciudad')}")
        if ents.get("oc_numero"):
            parts.append(f"oc={ents.get('oc_numero')}")
        excerpt = _scrub_summary_text(str(turn.get("reply_excerpt") or ""))
        if excerpt:
            parts.append(f"reply={excerpt[:120]}")
        lines.append("; ".join(parts))
    return "\n".join(lines)


def _scrub_summary_text(text: str) -> str:
    if "@" in text or any(k in text.lower() for k in STRIP_PII_KEYS):
        text = re.sub(r"\S+@\S+", "[redacted]", text)
        text = re.sub(
            r"(?i)\b(email|correo|tel[eé]fono|telefono|direcci[oó]n|direccion|password|token)\b\s*[:=]?\s*\S+",
            r"\1=[redacted]",
            text,
        )
    return text


def _merged_entities(turns: list[dict[str, Any]]) -> dict[str, Any]:
    merged = merge_entities(
        *[
            turn.get("entities") if isinstance(turn.get("entities"), dict) else None
            for turn in turns
        ]
    )
    for turn in turns:
        for tool in turn.get("tools_used") or []:
            if tool and tool not in merged["last_tools"]:
                merged["last_tools"].append(tool)
    return merged


def evidence_for_tool(
    turns: list[dict[str, Any]],
    tool: str,
    *,
    codigo: str | None = None,
) -> list[dict[str, Any]]:
    """Find most recent usable evidence for a tool, optionally filtered by codigo."""
    codigo_u = (codigo or "").strip().upper() or None
    for turn in reversed(turns):
        evidence = turn.get("evidence") if isinstance(turn.get("evidence"), list) else []
        matched: list[dict[str, Any]] = []
        for e in evidence:
            if not isinstance(e, dict):
                continue
            if e.get("tool") != tool or not e.get("ok") or e.get("empty"):
                continue
            data = e.get("data") if isinstance(e.get("data"), dict) else {}
            if codigo_u:
                ev_code = str(data.get("codigo") or "").strip().upper()
                if ev_code and ev_code != codigo_u:
                    continue
            matched.append(e)
        if matched:
            return matched
    return []


def _any_usable_evidence(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for turn in reversed(turns):
        evidence = turn.get("evidence") if isinstance(turn.get("evidence"), list) else []
        matched = [
            e for e in evidence if isinstance(e, dict) and e.get("ok") and not e.get("empty")
        ]
        if matched:
            return matched
    return []


def looks_like_anaphora(message: str) -> bool:
    text = (message or "").strip()
    if not text:
        return False
    if _ANAPHORA_RE.search(text):
        return True
    if _SHOW_MOVEMENTS_BARE_RE.match(text):
        return True
    if _REUSE_LIST_RE.search(text) or _REUSE_SUPPLIER_RE.search(text):
        return True
    if _BODEGA_RE.search(text) or _CIUDAD_RE.search(text):
        return True
    if re.match(r"^\s*[¿?]?\s*y\b", text, re.IGNORECASE):
        return True
    return False


def _ambiguous_multi_codigo(entities: dict[str, Any], text: str) -> bool:
    codes = [str(c).upper() for c in (entities.get("codigos") or []) if c]
    if len(codes) <= 1:
        return False
    if _FIRST_RE.search(text):
        return False
    # Explicit code in message disambiguates
    upper = text.upper()
    if any(c in upper for c in codes):
        return False
    if _ORDINAL_AMBIGUOUS_RE.search(text) or re.search(
        r"\b(ese|esa|el producto|la pieza)\b", text, re.IGNORECASE
    ):
        return True
    return False


class ConversationResolver:
    """Deterministic reference resolution against recent turns."""

    def resolve(self, message: str, turns: list[dict[str, Any]]) -> ResolveResult:
        text = (message or "").strip()
        if not text:
            return ResolveResult(kind=KIND_NONE)

        if not looks_like_anaphora(text):
            return ResolveResult(kind=KIND_NONE)

        entities = _merged_entities(turns)
        has_turns = bool(turns)
        codigo = str(entities.get("codigo") or "").strip().upper() or None

        # Case 5 / bare "muéstrame los movimientos" without prior movements/codigo
        if _SHOW_MOVEMENTS_BARE_RE.match(text):
            prior_mov = evidence_for_tool(turns, "get_stock_movements", codigo=codigo)
            if prior_mov:
                return ResolveResult(
                    kind=KIND_REUSE,
                    intent_hint="reuse_movements",
                    entities=entities,
                    prior_evidence=prior_mov,
                    tool_filter="get_stock_movements",
                )
            if not codigo:
                return ResolveResult(
                    kind=KIND_CLARIFY,
                    clarify_message=(
                        "¿De qué producto o código quieres ver los movimientos?"
                    ),
                )
            return ResolveResult(
                kind=KIND_PLAN_HINTS,
                intent_hint="movements",
                entities={"codigo": codigo, "codigos": entities.get("codigos") or []},
            )

        # Reuse list / "cuáles son"
        if _REUSE_LIST_RE.search(text):
            prior_mov = evidence_for_tool(turns, "get_stock_movements", codigo=codigo)
            if prior_mov:
                return ResolveResult(
                    kind=KIND_REUSE,
                    intent_hint="reuse_movements",
                    entities=entities,
                    prior_evidence=prior_mov,
                    tool_filter="get_stock_movements",
                )
            prior = _any_usable_evidence(turns)
            if prior:
                return ResolveResult(
                    kind=KIND_REUSE,
                    intent_hint="reuse_last",
                    entities=entities,
                    prior_evidence=prior,
                )
            return ResolveResult(
                kind=KIND_CLARIFY,
                clarify_message="No tengo un resultado anterior para detallar. ¿Qué quieres consultar?",
            )

        # Supplier company follow-up
        if _REUSE_SUPPLIER_RE.search(text):
            prior_sup = evidence_for_tool(turns, "get_supplier")
            if prior_sup:
                return ResolveResult(
                    kind=KIND_REUSE,
                    intent_hint="reuse_supplier",
                    entities=entities,
                    prior_evidence=prior_sup,
                    tool_filter="get_supplier",
                )
            if entities.get("proveedor_nombre") or entities.get("proveedor_empresa"):
                return ResolveResult(
                    kind=KIND_CLARIFY,
                    clarify_message="No tengo el detalle del proveedor en evidencia. ¿Quieres buscarlo de nuevo?",
                )
            return ResolveResult(
                kind=KIND_CLARIFY,
                clarify_message="¿De qué proveedor hablas? Indica nombre o RUT.",
            )

        # Ciudad/comuna from prior supplier evidence
        if _CIUDAD_RE.search(text):
            prior_sup = evidence_for_tool(turns, "get_supplier")
            if prior_sup:
                return ResolveResult(
                    kind=KIND_REUSE,
                    intent_hint="reuse_supplier_city",
                    entities=entities,
                    prior_evidence=prior_sup,
                    tool_filter="get_supplier",
                )
            return ResolveResult(
                kind=KIND_CLARIFY,
                clarify_message="¿De qué proveedor quieres la ciudad?",
            )

        # Multi-entity ambiguity
        if _ambiguous_multi_codigo(entities, text):
            return ResolveResult(
                kind=KIND_CLARIFY,
                clarify_message=(
                    "Hay varios productos en contexto ("
                    + ", ".join(str(c) for c in (entities.get("codigos") or [])[:5])
                    + "). ¿Cuál exactamente?"
                ),
            )

        # "El primero" → first catalog codigo + stock intent
        if _FIRST_RE.search(text):
            codes = entities.get("codigos") or []
            if not codes:
                return ResolveResult(
                    kind=KIND_CLARIFY,
                    clarify_message="No tengo una lista previa. ¿Qué producto buscas?",
                )
            first = str(codes[0]).strip().upper()
            if _STOCK_RE.search(text) or "cuánto" in text.lower() or "cuanto" in text.lower():
                prior_inv = evidence_for_tool(turns, "get_inventory", codigo=first)
                if prior_inv:
                    return ResolveResult(
                        kind=KIND_REUSE,
                        intent_hint="reuse_inventory",
                        entities={"codigo": first, "codigos": codes, "item_index": 0},
                        prior_evidence=prior_inv,
                        tool_filter="get_inventory",
                    )
                return ResolveResult(
                    kind=KIND_PLAN_HINTS,
                    intent_hint="inventory",
                    entities={"codigo": first, "codigos": codes, "item_index": 0},
                )
            return ResolveResult(
                kind=KIND_PLAN_HINTS,
                intent_hint="product",
                entities={"codigo": first, "codigos": codes, "item_index": 0},
            )

        # Movements follow-up with known codigo → new tool (not reuse count-only)
        if _MOVEMENTS_RE.search(text):
            if codigo:
                return ResolveResult(
                    kind=KIND_PLAN_HINTS,
                    intent_hint="movements",
                    entities={
                        "codigo": codigo,
                        "codigos": entities.get("codigos") or [],
                    },
                )
            return ResolveResult(
                kind=KIND_CLARIFY,
                clarify_message="¿De qué código quieres los movimientos?",
            )

        # Stock remaining OR bodega → prefer prior inventory evidence across turns
        if _STOCK_RE.search(text) or _BODEGA_RE.search(text):
            prior_inv = evidence_for_tool(turns, "get_inventory", codigo=codigo)
            if prior_inv:
                return ResolveResult(
                    kind=KIND_REUSE,
                    intent_hint="reuse_inventory",
                    entities=entities,
                    prior_evidence=prior_inv,
                    tool_filter="get_inventory",
                )
            if codigo:
                return ResolveResult(
                    kind=KIND_PLAN_HINTS,
                    intent_hint="inventory",
                    entities={
                        "codigo": codigo,
                        "codigos": entities.get("codigos") or [],
                    },
                )
            return ResolveResult(
                kind=KIND_CLARIFY,
                clarify_message="¿De qué producto o código quieres el stock?",
            )

        # Generic "ese producto" / "esa OC" without enough binding
        if _ORDINAL_AMBIGUOUS_RE.search(text):
            if "oc" in text.lower() and entities.get("oc_numero"):
                return ResolveResult(
                    kind=KIND_PLAN_HINTS,
                    intent_hint="purchase_orders",
                    entities={"oc_numero": entities["oc_numero"]},
                )
            if "proveedor" in text.lower() and (
                entities.get("proveedor_nombre") or entities.get("proveedor_q")
            ):
                return ResolveResult(
                    kind=KIND_PLAN_HINTS,
                    intent_hint="supplier",
                    entities={
                        "proveedor_q": entities.get("proveedor_q")
                        or entities.get("proveedor_nombre"),
                        "proveedor_nombre": entities.get("proveedor_nombre"),
                    },
                )
            if codigo:
                return ResolveResult(
                    kind=KIND_PLAN_HINTS,
                    intent_hint="product",
                    entities={
                        "codigo": codigo,
                        "codigos": entities.get("codigos") or [],
                    },
                )
            return ResolveResult(
                kind=KIND_CLARIFY,
                clarify_message="¿A qué te refieres? Indica código, proveedor u OC.",
            )

        if not has_turns or (
            not codigo
            and not entities.get("codigos")
            and not entities.get("proveedor_nombre")
            and not entities.get("oc_numero")
        ):
            return ResolveResult(
                kind=KIND_CLARIFY,
                clarify_message="Necesito más detalles (código, producto o proveedor) para continuar.",
            )

        return ResolveResult(
            kind=KIND_CLARIFY,
            clarify_message="¿Qué quieres saber de eso (stock, movimientos, ficha…)?",
        )


# Back-compat alias used by older tests/imports
def _last_evidence_for_tool(turns: list[dict[str, Any]], tool: str) -> list[dict[str, Any]]:
    return evidence_for_tool(turns, tool)
