"""LLM configuration from environment (ERP only — never Gateway)."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class LlmSettings:
    nl_enabled: bool
    planner_mode: str  # fake | llm
    provider: str
    api_key: str
    base_url: str
    planner_model: str
    timeout_seconds: float
    max_retries: int
    max_output_tokens: int
    temperature: float
    environment: str

    @property
    def soft_llm_allowed(self) -> bool:
        """LLM planner only when NL flag on, explicit local env, key present, mode=llm.

        ANDES_ENV must be exactly ``local`` — unset/empty does not soft-enable.
        """
        if self.planner_mode != "llm":
            return False
        if not self.nl_enabled:
            return False
        if self.environment != "local":
            return False
        if not self.api_key:
            return False
        if self.provider != "openai_compatible":
            return False
        return True


def load_llm_settings() -> LlmSettings:
    return LlmSettings(
        nl_enabled=_env_bool("ANDES_ASSISTANT_NL_ENABLED", False),
        planner_mode=(os.environ.get("ANDES_ORCH_PLANNER") or "fake").strip().lower() or "fake",
        provider=(os.environ.get("ANDES_LLM_PROVIDER") or "openai_compatible").strip().lower(),
        api_key=(os.environ.get("ANDES_LLM_API_KEY") or "").strip(),
        base_url=(os.environ.get("ANDES_LLM_BASE_URL") or "https://api.openai.com/v1").strip().rstrip("/"),
        planner_model=(os.environ.get("ANDES_LLM_PLANNER_MODEL") or "gpt-4.1-mini").strip(),
        timeout_seconds=float(_env_float("ANDES_LLM_TIMEOUT_SECONDS", 20.0)),
        max_retries=max(0, min(2, _env_int("ANDES_LLM_MAX_RETRIES", 2))),
        max_output_tokens=max(100, min(800, _env_int("ANDES_LLM_MAX_OUTPUT_TOKENS", 800))),
        temperature=float(_env_float("ANDES_LLM_TEMPERATURE", 0.0)),
        # Empty if unset — soft-enable requires explicit ANDES_ENV=local
        environment=(os.environ.get("ANDES_ENV") or "").strip().lower(),
    )
