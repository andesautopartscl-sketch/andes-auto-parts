"""FASE 7B.5 — technical pre-threshold counters (NOT user memory).

Stored in the assistant memory SQLite file as assistant_derived_freq_counter.
Never listed by GET /assistant/api/memory, never selected into memory_hints.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.assistant.orchestrator.memory_config import memory_db_path

logger = logging.getLogger(__name__)

WINDOW_DAYS = 14
MIN_GAP_SECONDS = 120
MIN_QUALIFIED_HITS = 5
MIN_DISTINCT_CONVERSATIONS = 3

COUNTER_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS assistant_derived_freq_counter (
    actor_user TEXT NOT NULL,
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    hits_json TEXT NOT NULL,
    PRIMARY KEY (actor_user, kind, value)
);
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(raw: str) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class DerivedCounterStore:
    """Internal frequency counters. Soft-fails; never raises into chat."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else memory_db_path()
        self._lock = threading.RLock()
        self.last_error: str | None = None

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def ensure_schema(self) -> None:
        with self._lock:
            conn = self.connect()
            try:
                conn.executescript(COUNTER_SCHEMA_SQL)
            finally:
                conn.close()

    def record_hit(
        self,
        *,
        actor_user: str,
        kind: str,
        value: str,
        conversation_id: str,
        now: datetime | None = None,
        window_days: int = WINDOW_DAYS,
        min_gap_seconds: int = MIN_GAP_SECONDS,
    ) -> dict[str, Any] | None:
        """Record at most one qualified hit per call. Returns snapshot or None."""
        actor = (actor_user or "").strip()[:80]
        kind_s = (kind or "").strip().lower()
        val = (value or "").strip()[:40]
        cid = (conversation_id or "").strip()[:80]
        if not actor or not kind_s or not val or not cid:
            return None
        now_dt = now or _utc_now()
        now_s = _iso(now_dt)
        window_start = now_dt - timedelta(days=max(1, int(window_days)))
        try:
            self.ensure_schema()
            with self._lock:
                conn = self.connect()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    row = conn.execute(
                        """
                        SELECT hits_json, first_seen_at, last_seen_at
                        FROM assistant_derived_freq_counter
                        WHERE actor_user = ? AND kind = ? AND value = ?
                        """,
                        (actor, kind_s, val),
                    ).fetchone()
                    hits: list[dict[str, str]] = []
                    if row is not None:
                        try:
                            raw = json.loads(row["hits_json"] or "[]")
                            if isinstance(raw, list):
                                hits = [h for h in raw if isinstance(h, dict)]
                        except (TypeError, json.JSONDecodeError):
                            hits = []
                    kept: list[dict[str, str]] = []
                    for hit in hits:
                        at = _parse_iso(str(hit.get("at") or ""))
                        if at is None or at < window_start:
                            continue
                        kept.append(
                            {
                                "at": _iso(at),
                                "conversation_id": str(hit.get("conversation_id") or "")[:80],
                            }
                        )
                    if kept:
                        last_at = _parse_iso(kept[-1]["at"])
                        if last_at is not None:
                            gap = (now_dt - last_at).total_seconds()
                            if gap < float(min_gap_seconds):
                                conn.execute("COMMIT")
                                return self._snapshot(kept, now_s, accepted=False, reason="gap")
                    kept.append({"at": now_s, "conversation_id": cid})
                    kept = kept[-32:]
                    first = kept[0]["at"]
                    last = kept[-1]["at"]
                    payload = json.dumps(kept, ensure_ascii=False, separators=(",", ":"))
                    conn.execute(
                        """
                        INSERT INTO assistant_derived_freq_counter (
                            actor_user, kind, value, first_seen_at, last_seen_at, hits_json
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(actor_user, kind, value) DO UPDATE SET
                            first_seen_at = excluded.first_seen_at,
                            last_seen_at = excluded.last_seen_at,
                            hits_json = excluded.hits_json
                        """,
                        (actor, kind_s, val, first, last, payload),
                    )
                    conn.execute("COMMIT")
                    return self._snapshot(kept, now_s, accepted=True, reason=None)
                except Exception:
                    try:
                        conn.execute("ROLLBACK")
                    except Exception:
                        pass
                    raise
                finally:
                    conn.close()
        except Exception as exc:  # noqa: BLE001
            self.last_error = type(exc).__name__
            logger.warning("assistant_memory derived counter failed: %s", type(exc).__name__)
            return None

    def snapshot(
        self,
        *,
        actor_user: str,
        kind: str,
        value: str,
        now: datetime | None = None,
        window_days: int = WINDOW_DAYS,
    ) -> dict[str, Any] | None:
        actor = (actor_user or "").strip()[:80]
        kind_s = (kind or "").strip().lower()
        val = (value or "").strip()[:40]
        if not actor or not kind_s or not val:
            return None
        now_dt = now or _utc_now()
        window_start = now_dt - timedelta(days=max(1, int(window_days)))
        try:
            self.ensure_schema()
            conn = self.connect()
            try:
                row = conn.execute(
                    """
                    SELECT hits_json FROM assistant_derived_freq_counter
                    WHERE actor_user = ? AND kind = ? AND value = ?
                    """,
                    (actor, kind_s, val),
                ).fetchone()
            finally:
                conn.close()
            if row is None:
                return None
            try:
                raw = json.loads(row["hits_json"] or "[]")
            except (TypeError, json.JSONDecodeError):
                return None
            kept: list[dict[str, str]] = []
            if isinstance(raw, list):
                for hit in raw:
                    if not isinstance(hit, dict):
                        continue
                    at = _parse_iso(str(hit.get("at") or ""))
                    if at is None or at < window_start:
                        continue
                    kept.append(
                        {
                            "at": _iso(at),
                            "conversation_id": str(hit.get("conversation_id") or "")[:80],
                        }
                    )
            return self._snapshot(kept, _iso(now_dt), accepted=False, reason=None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("assistant_memory derived counter snapshot failed: %s", type(exc).__name__)
            return None

    @staticmethod
    def _snapshot(
        hits: list[dict[str, str]],
        now_s: str,
        *,
        accepted: bool,
        reason: str | None,
    ) -> dict[str, Any]:
        convs = {h.get("conversation_id") or "" for h in hits if h.get("conversation_id")}
        return {
            "qualified_hits": len(hits),
            "distinct_conversations": len(convs),
            "first_seen_at": hits[0]["at"] if hits else None,
            "last_seen_at": hits[-1]["at"] if hits else None,
            "hit_accepted": accepted,
            "reason": reason,
            "as_of": now_s,
            "meets_threshold": (
                len(hits) >= MIN_QUALIFIED_HITS and len(convs) >= MIN_DISTINCT_CONVERSATIONS
            ),
        }


_DEFAULT: DerivedCounterStore | None = None
_LOCK = threading.Lock()


def get_default_derived_counter_store() -> DerivedCounterStore:
    global _DEFAULT
    with _LOCK:
        if _DEFAULT is None:
            _DEFAULT = DerivedCounterStore()
        return _DEFAULT


def reset_default_derived_counter_store_for_tests(
    path: Path | str | None = None,
) -> DerivedCounterStore:
    global _DEFAULT
    with _LOCK:
        _DEFAULT = DerivedCounterStore(path) if path is not None else DerivedCounterStore()
        return _DEFAULT
