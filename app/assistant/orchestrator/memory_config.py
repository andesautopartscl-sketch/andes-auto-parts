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
    """Derived writes are not used in chat path; flag reserved for later stages."""
    return _env_bool("ANDES_ASSISTANT_MEMORY_DERIVED", False)


def memory_explicit_enabled() -> bool:
    """FASE 7B.3 — allow NL explicit-memory signals in chat (requires MEMORY=1)."""
    return _env_bool("ANDES_ASSISTANT_MEMORY_EXPLICIT", False)


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


def memory_selector_max_slots() -> int:
    """FASE 7B.2 — max hints sent to planner per turn."""
    raw = (os.environ.get("ANDES_ASSISTANT_MEMORY_SELECTOR_MAX_SLOTS") or "12").strip()
    try:
        return max(1, min(int(raw), 50))
    except ValueError:
        return 12


def memory_selector_max_chars() -> int:
    """FASE 7B.2 — max JSON chars of memory_hints list for planner."""
    raw = (os.environ.get("ANDES_ASSISTANT_MEMORY_SELECTOR_MAX_CHARS") or "800").strip()
    try:
        return max(64, min(int(raw), 4000))
    except ValueError:
        return 800


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
