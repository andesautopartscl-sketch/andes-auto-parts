"""Strip secrets before logging or persisting audit records."""
from __future__ import annotations

from typing import Any

SECRET_KEYS = frozenset({
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "cookies",
    "access_token",
    "refresh_token",
    "bearer",
    "service_token",
    ".env",
    "private_key",
})

REDACTED = "[redacted]"


def _is_secret_key(key: str) -> bool:
    lowered = key.strip().lower()
    if lowered in SECRET_KEYS:
        return True
    return any(part in lowered for part in ("password", "token", "secret", "authorization", "cookie", "api_key"))


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, inner in value.items():
            if _is_secret_key(str(key)):
                out[str(key)] = REDACTED
            else:
                out[str(key)] = redact(inner)
        return out
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str) and len(value) > 400:
        return value[:400] + "…"
    return value


def result_summary(payload: Any, *, max_len: int = 180) -> str:
    text = str(payload)
    if len(text) > max_len:
        return text[:max_len] + "…"
    return text
