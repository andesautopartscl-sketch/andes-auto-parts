"""Gateway audit log (JSONL). Never persist secrets."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from andes_agent.redaction import redact, result_summary


@dataclass
class AuditRecord:
    agent_id: str
    conversation_id: str
    actor_user: str
    tool_name: str
    arguments_redacted: dict[str, Any]
    timestamp: str
    duration_ms: int
    success: bool
    error_code: str | None
    environment: str
    result_summary: str


class AuditLogger:
    def __init__(self, path: Path):
        self.path = path

    def write(self, record: AuditRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(record)
        payload["arguments_redacted"] = redact(payload.get("arguments_redacted") or {})
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def summarize(payload: Any) -> str:
    return result_summary(redact(payload))
