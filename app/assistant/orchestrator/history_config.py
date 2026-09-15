"""FASE 7A — persistent history feature flags and paths."""
from __future__ import annotations

import os
from pathlib import Path


def history_enabled() -> bool:
    raw = (os.environ.get("ANDES_ASSISTANT_HISTORY_ENABLED") or "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def history_retention_days() -> int:
    raw = (os.environ.get("ANDES_ASSISTANT_HISTORY_RETENTION_DAYS") or "90").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 90


def history_soft_delete_grace_days() -> int:
    raw = (os.environ.get("ANDES_ASSISTANT_HISTORY_SOFT_DELETE_GRACE_DAYS") or "7").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 7


def store_message_excerpt() -> bool:
    """Off by default — never persist user message text unless explicitly enabled."""
    raw = (os.environ.get("ANDES_ASSISTANT_STORE_MESSAGE_EXCERPT") or "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def history_db_path() -> Path:
    raw = (os.environ.get("ANDES_ASSISTANT_HISTORY_DB") or "").strip()
    if raw:
        return Path(raw)
    # Same SQLite file as ERP by default
    base = Path(__file__).resolve().parents[3]  # repo root (app/assistant/orchestrator → ..)
    # parents: orchestrator=0, assistant=1, app=2, root=3
    return base / "data" / "andes.db"
