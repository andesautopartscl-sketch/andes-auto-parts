"""Planner interface + deterministic FakePlanner (no LLM)."""
from __future__ import annotations

from typing import Any, Protocol

from app.assistant.orchestrator.input_guard import detect_write_intent
from app.assistant.orchestrator.scenarios import (
    SCENARIO_INVENTORY_ONLY,
    SCENARIO_MOVEMENTS_ONLY,
    SCENARIO_PRODUCT_ONLY,
    SCENARIO_SUPPLIER,
    SCENARIO_WRITE,
    detect_scenario,
    scenario_fixtures,
)


class Planner(Protocol):
    def plan(self, message: str, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
        ...


class FakePlanner:
    """Maps NL messages to fixed fixtures. Never calls an LLM."""

    def plan(self, message: str, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
        context = context or {}
        if context.get("replan"):
            err = str(context.get("validation_error") or "plan inválido")
            return {
                "plan_id": "replan-clarify",
                "needs_clarification": True,
                "reject_message": f"No pude armar un plan válido ({err}). ¿Puedes reformular?",
                "user_intent": "replan",
                "scenario": "ambiguous",
            }
        if detect_write_intent(message) and detect_scenario(message) != SCENARIO_WRITE:
            fixtures = scenario_fixtures()
            return dict(fixtures[SCENARIO_WRITE])

        # FASE 5: follow-ups with resolved entities from conversation context
        resolved = context.get("resolved_entities") if isinstance(context.get("resolved_entities"), dict) else {}
        intent = str(context.get("intent_hint") or "").strip().lower()
        if resolved or intent:
            follow = _plan_from_context_hints(message, resolved, intent)
            if follow is not None:
                return follow

        scenario = detect_scenario(message)
        if context.get("force_scenario"):
            scenario = str(context["force_scenario"])
        fixtures = scenario_fixtures()
        plan = dict(fixtures.get(scenario) or fixtures[detect_scenario("ambiguous")])
        return _adapt_search_query(plan, message)


def _plan_from_context_hints(
    message: str,
    resolved: dict[str, Any],
    intent: str,
) -> dict[str, Any] | None:
    fixtures = scenario_fixtures()
    codigo = str(resolved.get("codigo") or "").strip().upper()
    proveedor_q = str(
        resolved.get("proveedor_q") or resolved.get("proveedor_nombre") or ""
    ).strip()

    if intent == "movements" and codigo:
        plan = dict(fixtures[SCENARIO_MOVEMENTS_ONLY])
        return _with_codigo(plan, codigo)
    if intent == "inventory" and codigo:
        plan = dict(fixtures[SCENARIO_INVENTORY_ONLY])
        return _with_codigo(plan, codigo)
    if intent == "product" and codigo:
        plan = dict(fixtures[SCENARIO_PRODUCT_ONLY])
        return _with_codigo(plan, codigo)
    if intent == "supplier" and proveedor_q:
        plan = dict(fixtures[SCENARIO_SUPPLIER])
        return _with_supplier_q(plan, proveedor_q)
    if intent == "purchase_orders" and resolved.get("oc_numero"):
        return {
            "plan_id": "ctx-oc",
            "scenario": "purchase_orders_ctx",
            "user_intent": "Consultar OC del contexto",
            "answer_style": "operational",
            "steps": [
                {
                    "step": 1,
                    "tool": "get_purchase_orders",
                    "arguments": {"numero": str(resolved["oc_numero"]), "limit": 5},
                    "reason": "OC del contexto conversacional",
                }
            ],
        }
    return None


def _with_codigo(plan: dict[str, Any], codigo: str) -> dict[str, Any]:
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        return plan
    new_steps = []
    for step in steps:
        if not isinstance(step, dict):
            new_steps.append(step)
            continue
        args = dict(step.get("arguments") or {})
        if "codigo" in args or step.get("tool") in {
            "get_inventory",
            "get_product",
            "get_stock_movements",
            "get_ingresos",
        }:
            args["codigo"] = codigo
        if step.get("tool") == "check_stock":
            items = args.get("items")
            if isinstance(items, list) and items and isinstance(items[0], dict):
                items = [dict(items[0], codigo=codigo), *items[1:]]
                args["items"] = items
        new_steps.append(dict(step, arguments=args))
    return dict(plan, steps=new_steps, user_intent=f"{plan.get('user_intent')} ({codigo})")


def _with_supplier_q(plan: dict[str, Any], q: str) -> dict[str, Any]:
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        return plan
    first = dict(steps[0])
    args = dict(first.get("arguments") or {})
    args["q"] = q
    first["arguments"] = args
    return dict(plan, steps=[first, *steps[1:]])


def _adapt_search_query(plan: dict[str, Any], message: str) -> dict[str, Any]:
    """Fill search_catalog.q / get_supplier.q from the user message when needed."""
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        return plan
    first = steps[0] if isinstance(steps[0], dict) else None
    if not first:
        return plan
    tool = first.get("tool")
    q = (message or "").strip()
    lower = q.lower()
    for prefix in ("busca ", "buscar ", "encuentra ", "listar "):
        if lower.startswith(prefix):
            q = q[len(prefix) :].strip()
            break
    if tool == "get_supplier":
        for prefix in ("el proveedor ", "proveedor ", "al proveedor "):
            if q.lower().startswith(prefix):
                q = q[len(prefix) :].strip()
                break
        args = dict(first.get("arguments") or {})
        if q:
            args["q"] = q
        new_first = dict(first, arguments=args)
        return dict(plan, steps=[new_first, *steps[1:]])
    if tool != "search_catalog":
        return plan
    if "catalogo" in lower or "catálogo" in lower:
        if "2404" in lower:
            q = "2404"
    elif "2404" in lower and ("stock" in lower or "dime" in lower or "ingresos" in lower):
        q = "2404"
    args = dict(first.get("arguments") or {})
    if q:
        args["q"] = q
    new_first = dict(first, arguments=args)
    return dict(plan, steps=[new_first, *steps[1:]])
