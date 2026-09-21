"""FASE 8.1A — turn-scoped goal coverage. Not a planner. Not memory. Not permissions."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from app.assistant.orchestrator.evidence_store import EvidenceStore

# Closed catalog. Agent may propose these types; never authorizes tools or ACL.
REQUIREMENT_TYPES: dict[str, dict[str, Any]] = {
    "stock_movements": {
        "tools": frozenset({"get_stock_movements"}),
        "label": "movimientos de stock",
    },
    "current_inventory": {
        "tools": frozenset({"get_inventory", "check_stock"}),
        "label": "stock actual",
    },
    "catalog_search": {
        "tools": frozenset({"search_catalog"}),
        "label": "búsqueda de catálogo",
    },
    "product_detail": {
        "tools": frozenset({"get_product"}),
        "label": "ficha de producto",
    },
    "ingresos": {
        "tools": frozenset({"get_ingresos"}),
        "label": "ingresos",
    },
    "purchase_orders": {
        "tools": frozenset({"get_purchase_orders"}),
        "label": "órdenes de compra",
    },
    "dashboard_kpis": {
        "tools": frozenset({"get_dashboard_kpis"}),
        "label": "indicadores",
    },
    "customer": {
        "tools": frozenset({"get_customer"}),
        "label": "cliente",
    },
    "supplier": {
        "tools": frozenset({"get_supplier"}),
        "label": "proveedor",
    },
    "sales": {
        "tools": frozenset({"get_sales"}),
        "label": "ventas",
    },
    "equivalences": {
        "tools": frozenset({"get_equivalences"}),
        "label": "equivalencias",
    },
}

# Closed stems/phrases only — not a regex zoo, not the sole extractor.
# dashboard_kpis: explicit managerial/KPI intent (never bare "stock").
# current_inventory: stock/existencia intent; may co-occur with dashboard_kpis.
_AUTO_SIGNALS: dict[str, tuple[str, ...]] = {
    "stock_movements": ("movimientos", "movimiento", "kardex"),
    "dashboard_kpis": (
        "kpis",
        "kpi",
        "dashboard",
        "indicadores",
        "indicador",
        "ranking",
        "top productos",
        "resumen gerencial",
        "resumen ejecutivo",
    ),
    "current_inventory": (
        "stock",
        "inventario",
        "unidades",
        "queda",
        "quedan",
        "existencia",
        "existencias",
    ),
    # 8.8 — el cruce OEM. Frases inequivocas: "oem" suelto no aparece en lenguaje
    # de negocio para otra cosa, y "equivalente/homologado/alternativo" son
    # terminos del oficio, no palabras genericas.
    "equivalences": (
        "oem",
        "equivalente",
        "equivalentes",
        "equivalencia",
        "equivalencias",
        "homologado",
        "homologados",
        "alternativo",
        "alternativos",
        "alternativa",
        "alternativas",
        "reemplazo",
        "sustituto",
        "sustitutos",
    ),
    "supplier": (
        "proveedor",
        "proveedores",
        "supplier",
        "suministrador",
    ),
    # Only unambiguous multi-word phrases here. Bare "orden"/"ordenes" is handled
    # below and requires a compra/proveedor qualifier: on its own it is a generic
    # word ("en orden alfabetico", "orden de trabajo") and would be a false positive.
    "purchase_orders": (
        "ordenes de compra",
        "orden de compra",
        "purchase orders",
        "purchase order",
    ),
}

# Bare purchase-order stems, admitted only next to an explicit buying qualifier.
_PO_BARE_STEMS = ("ordenes", "orden")
_PO_QUALIFIERS = ("compra", "compras", "proveedor", "proveedores")

# 8.6 — "ventas" suelto NO es un requisito. Es una palabra de negocio genérica
# ("buenas ventas", "el area de ventas") y, sobre todo, "ventas del dia" es justo
# lo que responde el dashboard: crear un requisito de sales ahi obligaria a
# consultar DOS tools para una pregunta que una sola cubre, y bloquearia el final
# si el agente resuelve con el dashboard. Sigue el mismo patron que orden/ordenes:
# cuenta solo junto a un calificador que la vuelve especifica.
CODE_LIKE_RE = re.compile(chr(92) + "b" + chr(92) + "d{3,}" + chr(92) + "b")

_SALES_STEMS = ("ventas", "venta", "vendido", "vendidos", "vendimos",
                "facturado", "facturacion")
_SALES_QUALIFIERS = (
    "codigo", "producto", "cliente", "clientes", "sku",
    "periodo", "mes", "meses", "trimestre", "semestre", "ano", "anio",
    "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
    "agosto", "septiembre", "octubre", "noviembre", "diciembre",
    "unidades", "boleta", "boletas", "factura", "facturas",
    "historico", "historicas", "demanda", "rotacion",
)
# El dashboard YA agrega ventas ("Ranking de ventas del mes" se responde con el
# dashboard). La absorcion se decide contra el requisito dashboard_kpis DETECTADO,
# no contra una lista paralela de palabras: una lista propia se desincroniza en
# cuanto alguien anada una senal nueva al dashboard, y el sintoma seria una
# cobertura imposible de satisfacer.

# A facet phrase mentions "stock" as a qualifier of ANOTHER intent, not as a
# separate current_inventory goal ("KPIs con stock critico", "movimientos de
# stock"). Real dual intent ("stock y movimientos", "KPIs y stock actual") keeps
# both requirements because an inventory signal survives outside the facet.
_INVENTORY_FACET_PHRASES = (
    "stock critico",
    "movimientos de stock",
    "movimiento de stock",
)

IMPOSSIBLE_REASONS = frozenset(
    {"empty", "permission", "not_found", "tool_error", "cannot_resolve"}
)

# FASE 8.1G.3 — definitive failures the system may derive from evidence itself.
# CLOSED ALLOWLIST: anything not listed here is NOT definitive. An unknown code or
# a transport failure must never turn a requirement into "impossible" — the door
# stays open so the agent can retry or try a sibling tool.
SCOPE_ENTITY = "entity"
SCOPE_TOOL = "tool"
DEFINITIVE_ERRORS: dict[str, dict[str, str]] = {
    # The entity does not exist. This is a fact about the entity, not about the
    # tool, so no sibling covering tool can discover otherwise — every covering
    # tool of a requirement asks about the same entity. Pushing the agent to a
    # sibling is also unsafe: check_stock answers "disponible: 0" for a code that
    # does not exist, which would report absence as zero.
    "not_found": {"reason": "not_found", "scope": SCOPE_ENTITY},
    # ACL is enforced per tool, so a sibling tool may still be permitted. Only
    # definitive once every covering tool has been attempted and denied.
    "permission_denied": {"reason": "permission", "scope": SCOPE_TOOL},
}
# Not consulted at runtime (the allowlist above governs); kept so the contract is
# explicit and testable: these never make a requirement impossible.
NON_DEFINITIVE_ERRORS = frozenset(
    {
        "agent_unavailable",
        "erp_unavailable",
        "timeout",
        "network_error",
        "transport_error",
        "agent_error",
        "malformed_payload",
        "invalid_args",
        "dependency_failed",
        "dependency_empty",
        "binding_error",
    }
)
STATUSES = frozenset({"uncovered", "covered", "impossible"})
EXTRACTION_DETECTED = "detected"
EXTRACTION_UNKNOWN = "unknown"


def _fold(text: str) -> str:
    raw = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(ch for ch in raw if not unicodedata.combining(ch))


def _has_phrase(folded: str, phrase: str) -> bool:
    p = _fold(phrase)
    if not p:
        return False
    if " " in p:
        return p in folded
    return bool(re.search(rf"\b{re.escape(p)}\b", folded))


def _inventory_only_from_facet(folded: str) -> bool:
    """True when every inventory signal came from a facet of another intent.

    ``folded`` is already accent-stripped (crítico → critico). The facet phrases
    are removed and the SAME closed current_inventory signals are re-tested on the
    remainder: if nothing survives, there is no independent inventory goal.
    """
    stripped = folded
    matched = False
    for phrase in _INVENTORY_FACET_PHRASES:
        folded_phrase = _fold(phrase)
        if folded_phrase and folded_phrase in stripped:
            matched = True
            stripped = stripped.replace(folded_phrase, " ")
    if not matched:
        return False
    return not any(
        _has_phrase(stripped, signal) for signal in _AUTO_SIGNALS["current_inventory"]
    )


def _sales_intent(folded: str) -> bool:
    """'ventas' cuenta solo si la pregunta la vuelve especifica y no es un KPI."""
    if not any(_has_phrase(folded, stem) for stem in _SALES_STEMS):
        return False
    if any(_has_phrase(folded, q) for q in _SALES_QUALIFIERS):
        return True
    # Un codigo en la pregunta ("ventas del 2404") la vuelve especifica sin
    # necesidad de una palabra calificadora.
    return bool(re.search(CODE_LIKE_RE, folded))


def _bare_purchase_order_intent(folded: str) -> bool:
    """'ordenes'/'orden' only count next to an explicit compra/proveedor qualifier."""
    if not any(_has_phrase(folded, stem) for stem in _PO_BARE_STEMS):
        return False
    return any(_has_phrase(folded, q) for q in _PO_QUALIFIERS)


def extract_requirement_types(message: str) -> tuple[str, list[str]]:
    """Deterministic complement detector. Unknown if no closed signal fires."""
    folded = _fold(message)
    found: list[str] = []
    for rtype, phrases in _AUTO_SIGNALS.items():
        if any(_has_phrase(folded, p) for p in phrases):
            found.append(rtype)
    if "purchase_orders" not in found and _bare_purchase_order_intent(folded):
        found.append("purchase_orders")
    # Absorcion: si la pregunta ya es de indicadores, el dashboard cubre las
    # ventas y pedir get_sales ademas obligaria a dos tools para una pregunta
    # que una sola responde.
    if ("sales" not in found and "dashboard_kpis" not in found
            and _sales_intent(folded)):
        found.append("sales")
    # "KPIs … con stock critico" / "movimientos de stock" → the word "stock" is a
    # facet of the other intent, not a separate inventory goal. Only applies when
    # another requirement owns the facet; a lone facet keeps its requirement.
    if (
        "current_inventory" in found
        and len(found) > 1
        and _inventory_only_from_facet(folded)
    ):
        found = [t for t in found if t != "current_inventory"]
    if not found:
        return EXTRACTION_UNKNOWN, []
    return EXTRACTION_DETECTED, found


def derive_impossible_reason(
    items: list[Any],
    covering: frozenset[str],
) -> str | None:
    """Reason a requirement is unresolvable, derived from THIS turn's evidence.

    Returns a closed ``IMPOSSIBLE_REASONS`` value, or None to leave it uncovered.
    Never invents: only the definitive codes in ``DEFINITIVE_ERRORS`` qualify, and
    a single non-definitive failure among the attempts keeps the requirement open.
    """
    if not items:
        return None
    if any(getattr(item, "ok", False) for item in items):
        return None
    entity_reasons: set[str] = set()
    tool_reasons: dict[str, str] = {}
    for item in items:
        code = str(getattr(item, "error_code", "") or "").strip().lower()
        spec = DEFINITIVE_ERRORS.get(code)
        if spec is None:
            # Transport, unknown, or retryable failure: a sibling tool or a retry
            # could still resolve this requirement.
            return None
        if spec["scope"] == SCOPE_ENTITY:
            entity_reasons.add(spec["reason"])
        else:
            tool_reasons[str(getattr(item, "tool", ""))] = spec["reason"]
    if entity_reasons:
        return sorted(entity_reasons)[0]
    if covering and set(tool_reasons) >= set(covering):
        return sorted(set(tool_reasons.values()))[0]
    return None


def covering_tools(requirement_type: str) -> frozenset[str]:
    spec = REQUIREMENT_TYPES.get(requirement_type) or {}
    tools = spec.get("tools") or frozenset()
    return tools if isinstance(tools, frozenset) else frozenset(tools)


def type_label(requirement_type: str) -> str:
    spec = REQUIREMENT_TYPES.get(requirement_type) or {}
    return str(spec.get("label") or requirement_type)


@dataclass
class GoalRequirement:
    id: str
    type: str
    status: str = "uncovered"
    evidence_ids: list[str] = field(default_factory=list)
    reason: str | None = None

    def as_safe_dict(self) -> dict[str, Any]:
        """FASE 8.1J — the covering tools travel with the requirement.

        The system always knew which tool answers a requirement; it only ever
        told the model the requirement TYPE, an internal vocabulary that is not
        one-to-one with tool names. Naming the tools is informational: the
        allowlist still lives in agent_schema, ToolRunner and the Gateway ACL,
        and a named tool can still be refused by any of them.
        """
        return {
            "id": self.id,
            "type": self.type,
            "status": self.status,
            "evidence_ids": list(self.evidence_ids),
            "tools": sorted(covering_tools(self.type)),
            "reason": self.reason,
        }


@dataclass
class GoalCoverage:
    """Required facts for this turn. Memory and prior turns never cover a requirement."""

    extraction: str = EXTRACTION_UNKNOWN
    requirements: list[GoalRequirement] = field(default_factory=list)

    @classmethod
    def from_message(cls, message: str) -> GoalCoverage:
        extraction, types = extract_requirement_types(message)
        reqs = [
            GoalRequirement(id=f"r{i}", type=rtype)
            for i, rtype in enumerate(types, start=1)
        ]
        return cls(extraction=extraction, requirements=reqs)

    def by_type(self, rtype: str) -> GoalRequirement | None:
        for req in self.requirements:
            if req.type == rtype:
                return req
        return None

    def merge_proposed(self, items: list[Any] | None) -> None:
        """Agent may propose types. Invalid types are ignored. Never grants tools."""
        if self.extraction == EXTRACTION_DETECTED:
            # Authority stays with the deterministic set; proposals cannot add extras.
            return
        for raw in items or []:
            if not isinstance(raw, dict):
                continue
            rtype = str(raw.get("type") or "").strip()
            if rtype not in REQUIREMENT_TYPES:
                continue
            if self.by_type(rtype):
                continue
            self.requirements.append(
                GoalRequirement(id=f"r{len(self.requirements) + 1}", type=rtype)
            )
        if self.requirements and self.extraction == EXTRACTION_UNKNOWN:
            self.extraction = EXTRACTION_DETECTED

    def refresh(self, store: EvidenceStore) -> GoalCoverage:
        """Map THIS-TURN evidence tools → requirements. Memory is not accepted.

        FASE 8.1G.3 — a requirement whose covering tools all failed with a
        definitive error is derived ``impossible`` here instead of staying
        ``uncovered`` forever. Without this, "the entity does not exist" looked
        identical to "not asked yet", so the loop kept blocking final_answer while
        hunting for a sibling tool that cannot change the answer.
        """
        for req in self.requirements:
            if req.status == "impossible":
                continue
            tools = covering_tools(req.type)
            items = [item for item in store.items if item.tool in tools]
            eids = [item.evidence_id for item in items if item.ok]
            if eids:
                req.status = "covered"
                req.evidence_ids = eids
                req.reason = None
                continue
            reason = derive_impossible_reason(items, tools)
            if reason:
                req.status = "impossible"
                req.reason = reason
                req.evidence_ids = []
            else:
                req.status = "uncovered"
                req.evidence_ids = []
        return self

    def coverage_signature(self) -> str:
        """Stable fingerprint of the coverage state. Changes only when a
        requirement actually moves; used to tell progress from repetition."""
        return ";".join(f"{r.type}={r.status}" for r in sorted(self.requirements, key=lambda x: x.type))

    def apply_unresolved(
        self,
        items: list[Any] | None,
        *,
        store: EvidenceStore,
        remaining_calls: int,
    ) -> None:
        for raw in items or []:
            if not isinstance(raw, dict):
                continue
            rtype = str(raw.get("type") or "").strip()
            reason = str(raw.get("reason") or "").strip()
            req = self.by_type(rtype)
            if req is None or req.status == "covered":
                continue
            if reason not in IMPOSSIBLE_REASONS:
                continue
            if not _may_declare_impossible(req.type, store, remaining_calls):
                continue
            req.status = "impossible"
            req.reason = reason

    def uncovered(self) -> list[GoalRequirement]:
        return [r for r in self.requirements if r.status == "uncovered"]

    def covered(self) -> list[GoalRequirement]:
        return [r for r in self.requirements if r.status == "covered"]

    def impossible(self) -> list[GoalRequirement]:
        return [r for r in self.requirements if r.status == "impossible"]

    def blocks_final(self) -> bool:
        if self.extraction != EXTRACTION_DETECTED:
            return False
        return bool(self.uncovered())

    def prompt_pack(self) -> str:
        import json

        return json.dumps(self.safe_snapshot(), ensure_ascii=False, separators=(",", ":"))

    def safe_snapshot(self) -> dict[str, Any]:
        return {
            "extraction": self.extraction,
            "requirements": [r.as_safe_dict() for r in self.requirements],
        }

    def blocked_note(self, store: EvidenceStore | None = None) -> str:
        """Say WHICH tool is missing, not just which requirement.

        When the store is given, tools already executed this turn are dropped so
        the hint points at a route that can still produce something.
        """
        used = {str(i.tool) for i in store.items if i.tool} if store is not None else set()
        parts: list[str] = []
        actionable: list[str] = []
        for req in self.uncovered():
            pending = sorted(covering_tools(req.type) - used)
            parts.append(f"{req.type}({'|'.join(pending) if pending else 'sin tool disponible'})")
            actionable.extend(pending)
        missing = ", ".join(parts) or "none"
        if actionable:
            how = f"Llama UNA de estas tools: {', '.join(sorted(set(actionable)))}."
        else:
            how = "No queda tool por intentar: declara unresolved con reason cerrado."
        return (
            f"final_answer bloqueado. Falta cubrir: {missing}. {how} "
            "Si no es posible, declara unresolved con reason cerrado."
        )

    def unresolved_user_note(self) -> str:
        labels = [type_label(r.type) for r in self.uncovered() + self.impossible()]
        if not labels:
            return ""
        return "No pude resolver: " + ", ".join(labels) + "."


def _may_declare_impossible(rtype: str, store: EvidenceStore, remaining_calls: int) -> bool:
    if remaining_calls <= 0:
        return True
    tools = covering_tools(rtype)
    return any(item.tool in tools for item in store.items)


def parse_proposed_requirements(raw: Any) -> list[dict[str, str]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        rtype = str(item.get("type") or "").strip()
        if rtype not in REQUIREMENT_TYPES:
            continue
        cid = str(item.get("id") or "").strip() or f"p{len(out) + 1}"
        out.append({"id": cid[:16], "type": rtype})
    return out[:12]


def parse_unresolved(raw: Any) -> list[dict[str, str]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        rtype = str(item.get("type") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if rtype not in REQUIREMENT_TYPES or reason not in IMPOSSIBLE_REASONS:
            continue
        cid = str(item.get("id") or "").strip() or f"u{len(out) + 1}"
        out.append({"id": cid[:16], "type": rtype, "reason": reason})
    return out[:12]
