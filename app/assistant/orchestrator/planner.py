"""Planner interface + deterministic FakePlanner (no LLM)."""
from __future__ import annotations

from typing import Any, Protocol

from app.assistant.orchestrator.input_guard import detect_write_intent
from app.assistant.orchestrator.scenarios import (
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
            # force write reject even if keyword mix
            fixtures = scenario_fixtures()
            return dict(fixtures[SCENARIO_WRITE])

        scenario = detect_scenario(message)
        if context.get("force_scenario"):
            scenario = str(context["force_scenario"])
        fixtures = scenario_fixtures()
        plan = dict(fixtures.get(scenario) or fixtures[detect_scenario("ambiguous")])
        return _adapt_search_query(plan, message)


def _adapt_search_query(plan: dict[str, Any], message: str) -> dict[str, Any]:
    """Fill search_catalog.q from the user message when fixture uses a placeholder."""
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        return plan
    first = steps[0] if isinstance(steps[0], dict) else None
    if not first or first.get("tool") != "search_catalog":
        return plan
    q = (message or "").strip()
    lower = q.lower()
    for prefix in ("busca ", "buscar ", "encuentra ", "listar "):
        if lower.startswith(prefix):
            q = q[len(prefix) :].strip()
            break
    if "catalogo" in lower or "catálogo" in lower:
        # keep code token if present
        if "2404" in lower:
            q = "2404"
    elif "2404" in lower and ("stock" in lower or "dime" in lower or "ingresos" in lower):
        q = "2404"
    args = dict(first.get("arguments") or {})
    if q:
        args["q"] = q
    new_first = dict(first, arguments=args)
    return dict(plan, steps=[new_first, *steps[1:]])
