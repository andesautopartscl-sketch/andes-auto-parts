"""LlmPlanner — produces Plan JSON via OpenAI-compatible API; never executes tools."""
from __future__ import annotations

import json
from typing import Any

from app.assistant.orchestrator.input_guard import detect_write_intent
from app.assistant.orchestrator.llm.client import LlmClient, LlmError
from app.assistant.orchestrator.llm.prompts import build_system_prompt, build_user_prompt
from app.assistant.orchestrator.scenarios import SCENARIO_WRITE, scenario_fixtures


def _strip_nulls(value: Any) -> Any:
    """Remove nulls produced by OpenAI strict nullable fields before PlanValidator."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if item is None:
                continue
            cleaned = _strip_nulls(item)
            if cleaned is None:
                continue
            out[key] = cleaned
        return out
    if isinstance(value, list):
        return [_strip_nulls(item) for item in value if item is not None]
    return value


class LlmPlanner:
    def __init__(self, client: LlmClient):
        self.client = client

    def plan(self, message: str, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
        context = context or {}

        # Hard reject WRITE before spending tokens
        if detect_write_intent(message):
            return dict(scenario_fixtures()[SCENARIO_WRITE])

        replan_error = None
        if context.get("replan"):
            replan_error = str(context.get("validation_error") or "invalid plan")

        system = build_system_prompt()
        user = build_user_prompt(message, replan_error=replan_error)

        try:
            raw = self.client.complete_plan_json(system=system, user=user)
            data = json.loads(raw)
        except LlmError:
            raise
        except json.JSONDecodeError as exc:
            raise LlmError("llm_invalid_json", f"Invalid JSON: {exc}") from exc

        if not isinstance(data, dict):
            raise LlmError("llm_invalid_json", "Plan must be a JSON object")
        return _strip_nulls(data)
