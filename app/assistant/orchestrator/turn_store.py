"""In-memory short conversation turn store (FASE 5). No persistence, no secrets."""
from __future__ import annotations

import copy
import threading
import time
from typing import Any

from app.assistant.orchestrator.normalizer import _strip_pii

MAX_TURNS = 6
TTL_SECONDS = 1800
MAX_EVIDENCE_PER_TURN = 3
MAX_REPLY_EXCERPT = 200


def _now() -> float:
    return time.time()


class TurnStore:
    """Keyed by (actor_user, conversation_id). Empty conversation_id → no-op."""

    def __init__(
        self,
        *,
        max_turns: int = MAX_TURNS,
        ttl_seconds: int = TTL_SECONDS,
    ) -> None:
        self.max_turns = int(max_turns)
        self.ttl_seconds = int(ttl_seconds)
        self._lock = threading.Lock()
        self._data: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def _key(self, actor_user: str, conversation_id: str) -> tuple[str, str] | None:
        actor = (actor_user or "").strip()
        conv = (conversation_id or "").strip()[:80]
        if not actor or not conv:
            return None
        return (actor, conv)

    def prune_expired(self, *, now: float | None = None) -> None:
        now = _now() if now is None else now
        with self._lock:
            dead: list[tuple[str, str]] = []
            for key, turns in self._data.items():
                kept = [t for t in turns if now - float(t.get("ts") or 0) <= self.ttl_seconds]
                if kept:
                    self._data[key] = kept[-self.max_turns :]
                else:
                    dead.append(key)
            for key in dead:
                self._data.pop(key, None)

    def get(
        self,
        actor_user: str,
        conversation_id: str,
        *,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        key = self._key(actor_user, conversation_id)
        if key is None:
            return []
        now = _now() if now is None else now
        with self._lock:
            turns = self._data.get(key) or []
            kept = [t for t in turns if now - float(t.get("ts") or 0) <= self.ttl_seconds]
            if len(kept) != len(turns):
                if kept:
                    self._data[key] = kept[-self.max_turns :]
                else:
                    self._data.pop(key, None)
            return copy.deepcopy(kept[-self.max_turns :])

    def append(
        self,
        actor_user: str,
        conversation_id: str,
        turn: dict[str, Any],
        *,
        now: float | None = None,
    ) -> None:
        key = self._key(actor_user, conversation_id)
        if key is None:
            return
        now = _now() if now is None else now
        record = _sanitize_turn(turn, ts=now)
        with self._lock:
            turns = [
                t
                for t in (self._data.get(key) or [])
                if now - float(t.get("ts") or 0) <= self.ttl_seconds
            ]
            turns.append(record)
            self._data[key] = turns[-self.max_turns :]

    def clear(self, actor_user: str = "", conversation_id: str = "") -> None:
        with self._lock:
            if not actor_user and not conversation_id:
                self._data.clear()
                return
            key = self._key(actor_user, conversation_id)
            if key is not None:
                self._data.pop(key, None)


_DEFAULT_STORE: TurnStore | None = None
_DEFAULT_LOCK = threading.Lock()


def get_default_turn_store() -> TurnStore:
    global _DEFAULT_STORE
    with _DEFAULT_LOCK:
        if _DEFAULT_STORE is None:
            _DEFAULT_STORE = TurnStore()
        return _DEFAULT_STORE


def _sanitize_turn(turn: dict[str, Any], *, ts: float) -> dict[str, Any]:
    entities = turn.get("entities") if isinstance(turn.get("entities"), dict) else {}
    evidence_in = turn.get("evidence") if isinstance(turn.get("evidence"), list) else []
    evidence: list[dict[str, Any]] = []
    for item in evidence_in[:MAX_EVIDENCE_PER_TURN]:
        if not isinstance(item, dict):
            continue
        evidence.append(
            {
                "tool": str(item.get("tool") or ""),
                "ok": bool(item.get("ok")),
                "empty": bool(item.get("empty")),
                "error_code": item.get("error_code"),
                "classification": item.get("classification") or "INTERNAL",
                "data": _strip_pii(item.get("data") if isinstance(item.get("data"), dict) else {}),
                "meta": _strip_pii(item.get("meta") if isinstance(item.get("meta"), dict) else {}),
            }
        )
    reply = str(turn.get("reply_excerpt") or "")[:MAX_REPLY_EXCERPT]
    tools = [str(t) for t in (turn.get("tools_used") or []) if t][:MAX_EVIDENCE_PER_TURN]
    return {
        "ts": float(ts),
        "message_hash": str(turn.get("message_hash") or "")[:32],
        "tools_used": tools,
        "scenario": turn.get("scenario"),
        "entities": _strip_pii(dict(entities)),
        "evidence": evidence,
        "reply_excerpt": reply,
    }
