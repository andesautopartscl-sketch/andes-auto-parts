"""Planner factory — FakePlanner by default; LlmPlanner only when soft-enabled."""
from __future__ import annotations

from typing import Any

from app.assistant.orchestrator.llm.client import OpenAICompatibleClient
from app.assistant.orchestrator.llm.config import LlmSettings, load_llm_settings
from app.assistant.orchestrator.planner import FakePlanner, Planner
from app.assistant.orchestrator.planner_llm import LlmPlanner


def build_planner(
    *,
    settings: LlmSettings | None = None,
    llm_client: Any | None = None,
) -> Planner:
    """Return FakePlanner unless NL+LLM soft-enable conditions are met.

    Defaults keep tests/CI on FakePlanner with zero provider calls.
    """
    settings = settings or load_llm_settings()
    if settings.planner_mode == "fake" or not settings.soft_llm_allowed:
        return FakePlanner()
    client = llm_client or OpenAICompatibleClient(settings)
    return LlmPlanner(client)
