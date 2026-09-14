"""Natural-language orchestrator (FASE 2–3).

Default: FakePlanner. Soft-enable LlmPlanner only when NL=1 + ORCH=llm + API key
+ ANDES_ENV in {local, staging}. LLM runs in ERP only — never in the Gateway.
"""

from app.assistant.orchestrator.service import run_orchestrator_chat

__all__ = ["run_orchestrator_chat"]
