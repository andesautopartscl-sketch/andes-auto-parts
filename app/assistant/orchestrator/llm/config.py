"""LLM configuration from environment (ERP only — never Gateway)."""
from __future__ import annotations

import os
from dataclasses import dataclass

# Soft-enable allowlist — production / unset / empty never qualifies.
SOFT_LLM_ENVS = frozenset({"local", "staging"})


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
        """LLM planner only when NL flag on, allowlisted env, key present, mode=llm.

        ANDES_ENV must be exactly ``local`` or ``staging`` — unset/empty/production do not soft-enable.
        """
        if self.planner_mode != "llm":
            return False
        if not self.nl_enabled:
            return False
        if self.environment not in SOFT_LLM_ENVS:
            return False
        if not self.api_key:
            return False
        if self.provider != "openai_compatible":
            return False
        return True

    def public_capabilities(self) -> dict:
        """Safe capability snapshot for UI/ops — never includes secrets."""
        from app.assistant.orchestrator.input_guard import MAX_MESSAGE_LEN

        return {
            "nl_enabled": bool(self.nl_enabled),
            "planner_mode": self.planner_mode,
            "soft_llm_ready": bool(self.soft_llm_allowed),
            "provider": self.provider if self.soft_llm_allowed else None,
            "model": self.planner_model if self.soft_llm_allowed else None,
            "max_message_len": int(MAX_MESSAGE_LEN),
            "environment": self.environment or None,
        }


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
        # Empty if unset — soft-enable requires explicit ANDES_ENV=local|staging
        environment=(os.environ.get("ANDES_ENV") or "").strip().lower(),
    )


def assistant_config_log_line(settings: LlmSettings | None = None) -> str:
    """One-line startup summary — never includes API keys or tokens."""
    s = settings or load_llm_settings()
    return (
        "assistant_config "
        f"nl={'1' if s.nl_enabled else '0'} "
        f"planner={s.planner_mode} "
        f"soft_llm={'yes' if s.soft_llm_allowed else 'no'} "
        f"provider={s.provider} "
        f"model={s.planner_model} "
        f"env={s.environment or '(unset)'}"
    )
