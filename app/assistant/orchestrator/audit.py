"""Orchestrator audit records — never persist secrets."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

SECRET_KEYS = frozenset(
    {
        "password",
        "token",
        "authorization",
        "cookie",
        "cookies",
        "api_key",
        "apikey",
        "secret",
        "access_token",
        "bearer",
        "service_token",
        "csrf",
        "session",
    }
)

BEARER_RE = re.compile(r"bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE)
TOKENISH_RE = re.compile(r"\b(dev-token-local|erp-test-token|test-service-token)\b", re.IGNORECASE)


def _is_secret_key(key: str) -> bool:
    lowered = key.strip().lower()
    if lowered in SECRET_KEYS:
        return True
    return any(
        part in lowered
        for part in ("password", "token", "secret", "authorization", "cookie", "api_key", "csrf")
    )


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, inner in value.items():
            if _is_secret_key(str(key)):
                out[key] = "[redacted]"
            else:
                out[key] = redact(inner)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        text = value
        if text.lower().startswith("bearer "):
            return "Bearer [redacted]"
        text = BEARER_RE.sub("Bearer [redacted]", text)
        text = TOKENISH_RE.sub("[redacted]", text)
        return text
    return value


def message_hash(message: str) -> str:
    return hashlib.sha256((message or "").encode("utf-8")).hexdigest()[:16]


class OrchestratorAudit:
    def __init__(self, path: Path | None = None):
        default = Path(os.environ.get("ANDES_ORCH_AUDIT_PATH") or "data/orchestrator_audit.jsonl")
        self.path = path or default

    def write(self, record: dict[str, Any]) -> None:
        safe = redact(record)
        # Never persist raw message text — hash only if caller passed message
        if "message" in safe:
            safe["message"] = "[omitted]"
            safe.setdefault("message_hash", message_hash(str(record.get("message") or "")))
        safe.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(safe, ensure_ascii=False)
        # Final belt-and-suspenders
        line = TOKENISH_RE.sub("[redacted]", line)
        line = BEARER_RE.sub("Bearer [redacted]", line)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
