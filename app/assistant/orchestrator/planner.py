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
        plan = fixtures.get(scenario) or fixtures[detect_scenario("ambiguous")]
        return dict(plan)
