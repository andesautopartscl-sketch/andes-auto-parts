"""FASE 7B.1 — memory feature flags (default OFF)."""
from __future__ import annotations

import os
from pathlib import Path


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def memory_enabled() -> bool:
    return _env_bool("ANDES_ASSISTANT_MEMORY_ENABLED", False)


def memory_derived_enabled() -> bool:
    """Derived writes are not used in 7B.1 chat path; flag reserved for later stages."""
    return _env_bool("ANDES_ASSISTANT_MEMORY_DERIVED", False)


def memory_soft_delete_grace_days() -> int:
    raw = (os.environ.get("ANDES_ASSISTANT_MEMORY_SOFT_DELETE_GRACE_DAYS") or "7").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 7


def memory_max_per_user() -> int:
    """Active (non-deleted, non-expired) slots per actor. LRU by updated_at when exceeded."""
    raw = (os.environ.get("ANDES_ASSISTANT_MEMORY_MAX_PER_USER") or "50").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 50


def memory_max_per_conversation() -> int:
    """Active conversation-scoped slots per (actor, conversation_id)."""
    raw = (os.environ.get("ANDES_ASSISTANT_MEMORY_MAX_PER_CONVERSATION") or "10").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 10


def memory_db_path() -> Path:
    raw = (os.environ.get("ANDES_ASSISTANT_MEMORY_DB") or "").strip()
    if raw:
        return Path(raw)
    # Same SQLite file as ERP / history by default
    hist = (os.environ.get("ANDES_ASSISTANT_HISTORY_DB") or "").strip()
    if hist:
        return Path(hist)
    base = Path(__file__).resolve().parents[3]
    return base / "data" / "andes.db"
