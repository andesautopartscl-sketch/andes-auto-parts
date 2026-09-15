"""Natural-language orchestrator (FASE 2–3).

Default: FakePlanner. Soft-enable LlmPlanner only when NL=1 + ORCH=llm + API key
+ ANDES_ENV in {local, staging}. LLM runs in ERP only — never in the Gateway.
"""

from __future__ import annotations

from typing import Any

__all__ = ["run_orchestrator_chat"]


def run_orchestrator_chat(*args: Any, **kwargs: Any):
    """Lazy re-export to avoid circular imports at package import time."""
    from app.assistant.orchestrator.service import run_orchestrator_chat as _run

    return _run(*args, **kwargs)
