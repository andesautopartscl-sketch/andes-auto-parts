"""FASE 7B.2 — select memory hints for the planner (read-only, auxiliary).

Pipeline:
  candidates → ownership → scope → TTL → permission_epoch → type whitelist
  → prohibited-content defense → rank → dedup → budget → memory_hints

Memory is NEVER authority for permissions or WRITE.

Ranking (deterministic, lower primary = higher priority):
  1. preference
  2. ui_pref
  3. pinned_entity (conversation)
  4. pinned_entity (user)
  5. conversation_summary
  6. frequent_entity
Within the same primary: conversation scope before user, then newer updated_at,
then higher confidence.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from app.assistant.orchestrator.memory_config import (
    memory_enabled,
    memory_selector_max_chars,
    memory_selector_max_slots,
)
from app.assistant.orchestrator.memory_epoch import (
    PermissionEpochProvider,
    PermissionEpochResolution,
    memory_passes_permission_epoch,
    resolve_actor_permission_epoch,
)
from app.assistant.orchestrator.memory_schema import MEMORY_TYPES
from app.assistant.orchestrator.memory_store import MemoryStore, get_default_memory_store

logger = logging.getLogger(__name__)

_TYPE_SCOPE_RANK: dict[tuple[str, str], int] = {
    ("preference", "user"): 1,
    ("preference", "conversation"): 1,
    ("ui_pref", "user"): 2,
    ("ui_pref", "conversation"): 2,
    ("pinned_entity", "conversation"): 3,
    ("pinned_entity", "user"): 4,
    ("conversation_summary", "conversation"): 5,
    ("conversation_summary", "user"): 5,
    ("frequent_entity", "user"): 6,
    ("frequent_entity", "conversation"): 6,
}

_PROHIBITED_MARKER_RE = re.compile(
    r"\b(?:Bearer|Authorization|Basic|Token|api[_-]?key|apikey|secret|"
    r"password|passwd|cookie|cookies|csrf|m2m|ver_finanzas|write_not_allowed)\b",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_RUT_RE = re.compile(r"\b\d{1,2}\.?\d{3}\.?\d{3}-[\dkK]\b")


@dataclass
class MemorySelectionResult:
    hints: list[dict[str, Any]] = field(default_factory=list)
    candidates_count: int = 0
    selected_count: int = 0
    budget_chars: int = 0
    selected_types: list[str] = field(default_factory=list)
    dropped_prohibited: int = 0
    permission_epoch_read: int | None = None
    permission_epoch_available: bool = False
    permission_epoch_error: bool = False
    memory_contextual_invalidated: int = 0
    memory_contextual_selected: int = 0
    error: str | None = None


def _dedup_key(slot: dict[str, Any]) -> str:
    scope = str(slot.get("scope") or "").strip().lower()
    mt = str(slot.get("memory_type") or "").strip().lower()
    key = str(slot.get("key") or "").strip()
    return f"{scope}|{mt}|{key}"


def _invert_iso(value: str) -> str:
    """Key that sorts ISO-ish timestamps descending under ascending sort."""
    if not value:
        return "~"
    return "".join(chr(0x10FFFF - ord(ch)) for ch in value)


def sort_memory_candidates(slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deterministic ranking for selection."""

    def key_fn(s: dict[str, Any]) -> tuple[Any, ...]:
        mt = str(s.get("memory_type") or "").strip().lower()
        scope = str(s.get("scope") or "").strip().lower()
        primary = _TYPE_SCOPE_RANK.get((mt, scope), 99)
        scope_tie = 0 if scope == "conversation" else 1
        updated = str(s.get("updated_at") or "")
        try:
            conf = float(s.get("confidence") if s.get("confidence") is not None else 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        return (primary, scope_tie, _invert_iso(updated), -conf)

    return sorted(slots, key=key_fn)


def hint_from_slot(slot: dict[str, Any]) -> dict[str, Any] | None:
    """Minimal planner-facing representation (no internal/DB fields)."""
    mt = str(slot.get("memory_type") or "").strip().lower()
    if mt not in MEMORY_TYPES:
        return None
    key = str(slot.get("key") or "").strip()
    if not key:
        return None
    value = slot.get("value")
    if not isinstance(value, dict):
        return None
    return {"type": mt, "key": key, "value": value}


def hints_char_budget(hints: list[dict[str, Any]]) -> int:
    """Character count of the exact JSON representation sent to the planner."""
    return len(json.dumps(hints, ensure_ascii=False, separators=(",", ":")))


def hint_contains_prohibited(hint: dict[str, Any]) -> bool:
    try:
        blob = json.dumps(hint, ensure_ascii=False)
    except (TypeError, ValueError):
        return True
    if _PROHIBITED_MARKER_RE.search(blob):
        return True
    if _EMAIL_RE.search(blob) or _RUT_RE.search(blob):
        return True
    low = blob.lower()
    for needle in (
        "ver_finanzas",
        "mod_finanzas",
        "write=true",
        "crear factura",
        "authorization",
    ):
        if needle in low:
            return True
    return False


def _scope_ok(slot: dict[str, Any], *, actor: str, conversation_id: str) -> bool:
    if str(slot.get("actor_user") or "") != actor:
        return False
    scope = str(slot.get("scope") or "").strip().lower()
    cid = slot.get("conversation_id")
    if scope == "user":
        return cid in (None, "")
    if scope == "conversation":
        if not conversation_id:
            return False
        return str(cid or "") == conversation_id
    return False


def select_memory_hints(
    *,
    actor_user: str,
    conversation_id: str = "",
    store: MemoryStore | None = None,
    epoch_provider: PermissionEpochProvider | None = None,
    max_slots: int | None = None,
    max_chars: int | None = None,
    list_fn: Callable[..., list[dict[str, Any]]] | None = None,
) -> MemorySelectionResult:
    """Select planner memory_hints. Soft-fails to empty on any error / MEMORY=0."""
    out = MemorySelectionResult()
    if not memory_enabled():
        return out

    actor = (actor_user or "").strip()
    if not actor:
        return out

    cid = (conversation_id or "").strip()[:80]
    cap = max_slots if max_slots is not None else memory_selector_max_slots()
    budget = max_chars if max_chars is not None else memory_selector_max_chars()
    cap = max(1, min(int(cap), 50))
    budget = max(64, min(int(budget), 4000))

    try:
        mem = store if store is not None else get_default_memory_store()
        if list_fn is not None:
            user_slots = list_fn(actor, scope="user", conversation_id=None, limit=100)
            conv_slots = (
                list_fn(actor, scope="conversation", conversation_id=cid, limit=100)
                if cid
                else []
            )
        else:
            user_slots = mem.list_slots(actor, scope="user", limit=100)
            conv_slots = (
                mem.list_slots(actor, scope="conversation", conversation_id=cid, limit=100)
                if cid
                else []
            )
    except Exception as exc:  # noqa: BLE001
        out.error = "list_failed"
        logger.warning("assistant_memory select list failed: %s", type(exc).__name__)
        return out

    candidates = list(user_slots or []) + list(conv_slots or [])
    out.candidates_count = len(candidates)

    epoch_res: PermissionEpochResolution = resolve_actor_permission_epoch(
        actor, provider=epoch_provider
    )
    out.permission_epoch_available = bool(epoch_res.available)
    out.permission_epoch_read = int(epoch_res.epoch) if epoch_res.available and epoch_res.epoch is not None else None
    out.permission_epoch_error = not bool(epoch_res.available)

    filtered: list[dict[str, Any]] = []
    for slot in candidates:
        if not isinstance(slot, dict):
            continue
        mt = str(slot.get("memory_type") or "").strip().lower()
        if mt not in MEMORY_TYPES:
            continue
        if not _scope_ok(slot, actor=actor, conversation_id=cid):
            continue
        if slot.get("deleted_at"):
            continue
        sens = str(slot.get("sensitivity") or "benign").strip().lower()
        if not memory_passes_permission_epoch(slot, epoch_res):
            if sens != "benign":
                out.memory_contextual_invalidated += 1
            continue
        filtered.append(slot)

    ranked = sort_memory_candidates(filtered)

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for slot in ranked:
        dk = _dedup_key(slot)
        if dk in seen:
            continue
        seen.add(dk)
        unique.append(slot)

    hints: list[dict[str, Any]] = []
    for slot in unique:
        if len(hints) >= cap:
            break
        hint = hint_from_slot(slot)
        if hint is None:
            continue
        if hint_contains_prohibited(hint):
            out.dropped_prohibited += 1
            logger.warning(
                "assistant_memory hint dropped: prohibited_content type=%s",
                hint.get("type"),
            )
            continue
        trial = hints + [hint]
        chars = hints_char_budget(trial)
        if chars > budget:
            continue
        hints = trial
        if str(slot.get("sensitivity") or "").strip().lower() != "benign":
            out.memory_contextual_selected += 1

    out.hints = hints
    out.selected_count = len(hints)
    out.budget_chars = hints_char_budget(hints) if hints else 0
    out.selected_types = [str(h.get("type")) for h in hints]
    return out
