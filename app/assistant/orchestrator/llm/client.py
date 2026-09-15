"""OpenAI-compatible LLM client for the orchestrator (ERP only)."""
from __future__ import annotations

import json
import time
from typing import Any, Protocol

from app.assistant.orchestrator.llm.config import LlmSettings
from app.assistant.orchestrator.llm.plan_schema import plan_response_format


class LlmError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class LlmClient(Protocol):
    def complete_plan_json(self, *, system: str, user: str) -> str:
        """Return raw JSON string for a Plan. Raises LlmError on failure."""
        ...


def _redact_headers_for_capture(headers: dict[str, str]) -> dict[str, str]:
    out = {}
    for key, value in headers.items():
        if key.lower() in {"authorization", "api-key", "x-api-key"}:
            out[key] = "[redacted]"
        else:
            out[key] = value
    return out


class OpenAICompatibleClient:
    """Chat Completions client. Never sends Andes M2M token."""

    def __init__(self, settings: LlmSettings, *, http_client: Any | None = None):
        self.settings = settings
        self._http = http_client  # injectable for tests (openai.OpenAI instance or mock)
        self.last_request_capture: dict[str, Any] | None = None
        self.last_usage: dict[str, int] | None = None

    def _get_client(self):
        if self._http is not None:
            return self._http
        from openai import OpenAI

        return OpenAI(
            api_key=self.settings.api_key,
            base_url=self.settings.base_url,
            timeout=self.settings.timeout_seconds,
            max_retries=0,  # we handle retries ourselves
        )

    def complete_plan_json(self, *, system: str, user: str) -> str:
        self.last_usage = None
        # Defense: never allow M2M env leakage into prompts
        for forbidden in (
            "ANDES_AGENT_SERVICE_TOKEN",
            "dev-token-local",
            "Bearer ",
        ):
            if forbidden in system or forbidden in user:
                raise LlmError("invalid_args", "Prompt must not contain secrets")

        client = self._get_client()
        attempts = 1 + max(0, self.settings.max_retries)
        last_exc: Exception | None = None

        for attempt in range(attempts):
            try:
                headers_note = {
                    "Authorization": "Bearer [redacted-llm-key]",
                    "Content-Type": "application/json",
                }
                # Capture request shape for tests (no real secrets)
                self.last_request_capture = {
                    "base_url": self.settings.base_url,
                    "model": self.settings.planner_model,
                    "headers": _redact_headers_for_capture(headers_note),
                    "has_andes_m2m_header": False,
                    "body_keys": ["model", "messages", "temperature", "max_tokens", "response_format"],
                    "messages_roles": ["system", "user"],
                    "temperature": self.settings.temperature,
                    "max_tokens": self.settings.max_output_tokens,
                }
                # Ensure Andes M2M token never appears in capture or outbound body
                m2m = __import__("os").environ.get("ANDES_AGENT_SERVICE_TOKEN") or ""
                if m2m and m2m in (system + user):
                    raise LlmError("invalid_args", "M2M token must not be sent to LLM")

                response = client.chat.completions.create(
                    model=self.settings.planner_model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    temperature=self.settings.temperature,
                    max_tokens=self.settings.max_output_tokens,
                    response_format=plan_response_format(),
                )
                content = ""
                if response.choices:
                    content = (response.choices[0].message.content or "").strip()
                if not content:
                    raise LlmError("llm_invalid_json", "Empty model response")
                # Validate it parses as JSON object
                parsed = json.loads(content)
                if not isinstance(parsed, dict):
                    raise LlmError("llm_invalid_json", "Model response must be a JSON object")
                # Capture token usage only (never prompts/content)
                self.last_usage = _extract_usage(response)
                return content
            except LlmError:
                raise
            except json.JSONDecodeError as exc:
                raise LlmError("llm_invalid_json", f"Invalid JSON from model: {exc}") from exc
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
                name = type(exc).__name__.lower()
                message = str(exc).lower()
                # Quota exhaustion is a hard 429 — do not burn retries.
                hard_quota = (
                    "insufficient_quota" in message
                    or "credit_balance_exhausted" in message
                    or "no credits remaining" in message
                )
                retryable = False
                if not hard_quota and status in (429, 500, 502, 503, 504):
                    retryable = True
                if not hard_quota and (
                    "rate" in message or "429" in message or "timeout" in name or "timeout" in message
                ):
                    retryable = True
                if "api_connection" in name or "api_timeout" in name:
                    retryable = True
                if attempt + 1 < attempts and retryable:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                if "timeout" in name or "timeout" in message:
                    raise LlmError("llm_unavailable", "LLM request timed out") from exc
                if hard_quota:
                    raise LlmError("llm_unavailable", "LLM provider quota exhausted") from exc
                if status == 429:
                    raise LlmError("llm_unavailable", "LLM rate limited") from exc
                raise LlmError("llm_unavailable", "LLM provider unavailable") from exc

        raise LlmError("llm_unavailable", f"LLM provider unavailable: {last_exc}")


def _extract_usage(response: Any) -> dict[str, int] | None:
    """Pull prompt/completion/total tokens from provider response if present."""
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return None

    def _get(obj: Any, *names: str) -> int | None:
        for name in names:
            if isinstance(obj, dict) and name in obj and obj[name] is not None:
                try:
                    return int(obj[name])
                except (TypeError, ValueError):
                    return None
            val = getattr(obj, name, None)
            if val is not None:
                try:
                    return int(val)
                except (TypeError, ValueError):
                    return None
        return None

    prompt = _get(usage, "prompt_tokens", "input_tokens")
    completion = _get(usage, "completion_tokens", "output_tokens")
    total = _get(usage, "total_tokens")
    if total is None and prompt is not None and completion is not None:
        total = prompt + completion
    if prompt is None and completion is None and total is None:
        return None
    out: dict[str, int] = {}
    if prompt is not None:
        out["prompt_tokens"] = prompt
    if completion is not None:
        out["completion_tokens"] = completion
    if total is not None:
        out["total_tokens"] = total
    return out
