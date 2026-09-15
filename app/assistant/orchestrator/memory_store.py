"""FASE 7B.1 — persistent memory slot store (no planner integration).

Flag ANDES_ASSISTANT_MEMORY_ENABLED=0 → no-ops / empty reads.
Never stores raw messages, prompts, secrets, PII, or financial amounts.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.assistant.orchestrator.history_store import HistoryStore
from app.assistant.orchestrator.memory_config import (
    memory_db_path,
    memory_enabled,
    memory_max_per_conversation,
    memory_max_per_user,
    memory_soft_delete_grace_days,
)
from app.assistant.orchestrator.memory_schema import (
    DEFAULT_TTL_DAYS,
    MemorySchemaError,
)
from app.assistant.orchestrator.memory_sanitize import sanitize_memory_record

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS assistant_memory_slot (
    id TEXT PRIMARY KEY,
    actor_user TEXT NOT NULL,
    scope TEXT NOT NULL,
    conversation_id TEXT,
    memory_type TEXT NOT NULL,
    key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    confidence REAL,
    source TEXT NOT NULL,
    permission_epoch INTEGER,
    sensitivity TEXT NOT NULL DEFAULT 'benign',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT,
    deleted_at TEXT,
    source_turn_id TEXT,
    meta_json TEXT
);

CREATE INDEX IF NOT EXISTS ix_asst_mem_actor_updated
    ON assistant_memory_slot(actor_user, updated_at);

CREATE INDEX IF NOT EXISTS ix_asst_mem_actor_scope_conv
    ON assistant_memory_slot(actor_user, scope, conversation_id);

CREATE INDEX IF NOT EXISTS ix_asst_mem_expires
    ON assistant_memory_slot(expires_at);

CREATE UNIQUE INDEX IF NOT EXISTS ux_asst_mem_identity
    ON assistant_memory_slot(
        actor_user,
        scope,
        IFNULL(conversation_id, ''),
        memory_type,
        key
    )
    WHERE deleted_at IS NULL;
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = str(raw).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


class MemoryStore:
    """SQLite-backed typed memory slots with ownership, TTL, and soft-delete."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else memory_db_path()
        self._lock = threading.RLock()
        self.last_error: str | None = None
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def ensure_schema(self) -> None:
        with self._lock:
            conn = self.connect()
            try:
                conn.executescript(SCHEMA_SQL)
            finally:
                conn.close()

    def upsert(
        self,
        *,
        actor_user: str,
        scope: str,
        memory_type: str,
        key: str,
        value: dict[str, Any],
        conversation_id: str | None = None,
        source: str = "explicit",
        confidence: float | None = 1.0,
        permission_epoch: int | None = 0,
        sensitivity: str | None = None,
        source_turn_id: str | None = None,
        meta: dict[str, Any] | None = None,
        expires_at: str | None = None,
        verify_conversation: bool = True,
    ) -> dict[str, Any] | None:
        """Create/update a sanitized slot. Returns public dict or None on soft failure."""
        if not memory_enabled():
            return None
        try:
            safe = sanitize_memory_record(
                actor_user=actor_user,
                scope=scope,
                conversation_id=conversation_id,
                memory_type=memory_type,
                key=key,
                value=value,
                source=source,
                confidence=confidence,
                permission_epoch=permission_epoch,
                sensitivity=sensitivity,
                source_turn_id=source_turn_id,
                meta=meta,
            )
        except MemorySchemaError as exc:
            self.last_error = f"upsert_schema: {exc.code}: {exc.message}"
            logger.warning("assistant_memory upsert rejected: %s", exc.message)
            return None

        if safe["scope"] == "conversation" and verify_conversation:
            if not self._conversation_owned(safe["actor_user"], safe["conversation_id"]):
                self.last_error = "upsert: conversation not found or not owned"
                return None

        try:
            self.ensure_schema()
            with self._lock:
                conn = self.connect()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    now = _utc_now()
                    now_s = _iso(now)
                    exp = expires_at
                    if exp is None:
                        ttl = DEFAULT_TTL_DAYS.get(safe["memory_type"])
                        exp = _iso(now + timedelta(days=ttl)) if ttl else None

                    existing = conn.execute(
                        """
                        SELECT id FROM assistant_memory_slot
                        WHERE actor_user = ?
                          AND scope = ?
                          AND IFNULL(conversation_id, '') = IFNULL(?, '')
                          AND memory_type = ?
                          AND key = ?
                          AND deleted_at IS NULL
                        """,
                        (
                            safe["actor_user"],
                            safe["scope"],
                            safe["conversation_id"],
                            safe["memory_type"],
                            safe["key"],
                        ),
                    ).fetchone()

                    if existing:
                        slot_id = existing["id"]
                        conn.execute(
                            """
                            UPDATE assistant_memory_slot
                            SET value_json = ?, confidence = ?, source = ?,
                                permission_epoch = ?, sensitivity = ?,
                                updated_at = ?, expires_at = ?,
                                source_turn_id = COALESCE(?, source_turn_id),
                                meta_json = ?
                            WHERE id = ? AND actor_user = ? AND deleted_at IS NULL
                            """,
                            (
                                json.dumps(safe["value"], ensure_ascii=False),
                                safe["confidence"],
                                safe["source"],
                                safe["permission_epoch"],
                                safe["sensitivity"],
                                now_s,
                                exp,
                                safe["source_turn_id"],
                                json.dumps(safe["meta"], ensure_ascii=False),
                                slot_id,
                                safe["actor_user"],
                            ),
                        )
                    else:
                        slot_id = str(uuid.uuid4())
                        conn.execute(
                            """
                            INSERT INTO assistant_memory_slot (
                                id, actor_user, scope, conversation_id, memory_type, key,
                                value_json, confidence, source, permission_epoch, sensitivity,
                                created_at, updated_at, expires_at, deleted_at,
                                source_turn_id, meta_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                            """,
                            (
                                slot_id,
                                safe["actor_user"],
                                safe["scope"],
                                safe["conversation_id"],
                                safe["memory_type"],
                                safe["key"],
                                json.dumps(safe["value"], ensure_ascii=False),
                                safe["confidence"],
                                safe["source"],
                                safe["permission_epoch"],
                                safe["sensitivity"],
                                now_s,
                                now_s,
                                exp,
                                safe["source_turn_id"],
                                json.dumps(safe["meta"], ensure_ascii=False),
                            ),
                        )

                    self._enforce_caps_locked(conn, safe["actor_user"], safe["conversation_id"])
                    conn.execute("COMMIT")
                    row = conn.execute(
                        "SELECT * FROM assistant_memory_slot WHERE id = ? AND actor_user = ?",
                        (slot_id, safe["actor_user"]),
                    ).fetchone()
                    return _public(dict(row)) if row else None
                except Exception:
                    try:
                        conn.execute("ROLLBACK")
                    except Exception:
                        pass
                    raise
                finally:
                    conn.close()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"upsert: {exc}"
            logger.warning("assistant_memory upsert failed: %s", exc)
            return None

    def list_slots(
        self,
        actor_user: str,
        *,
        scope: str | None = None,
        conversation_id: str | None = None,
        include_expired: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if not memory_enabled():
            return []
        actor = (actor_user or "").strip()
        if not actor:
            return []
        limit = max(1, min(int(limit or 100), 200))
        now_s = _iso(_utc_now())
        try:
            self.ensure_schema()
            conn = self.connect()
            try:
                sql = """
                    SELECT * FROM assistant_memory_slot
                    WHERE actor_user = ? AND deleted_at IS NULL
                """
                params: list[Any] = [actor]
                if not include_expired:
                    sql += " AND (expires_at IS NULL OR expires_at > ?)"
                    params.append(now_s)
                if scope:
                    sql += " AND scope = ?"
                    params.append(scope.strip().lower())
                if conversation_id is not None:
                    cid = (conversation_id or "").strip()[:80]
                    if cid:
                        # Conversation-scoped reads must not leak other convs;
                        # optionally include user-scoped when listing "for chat" —
                        # 7B.1 list API filters explicitly via query params.
                        sql += " AND conversation_id = ?"
                        params.append(cid)
                sql += " ORDER BY updated_at DESC LIMIT ?"
                params.append(limit)
                rows = conn.execute(sql, params).fetchall()
                return [_public(dict(r)) for r in rows]
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"list_slots: {exc}"
            logger.warning("assistant_memory list failed: %s", exc)
            return []

    def get_slot(self, actor_user: str, slot_id: str) -> dict[str, Any] | None:
        if not memory_enabled():
            return None
        actor = (actor_user or "").strip()
        sid = (slot_id or "").strip()[:80]
        if not actor or not sid:
            return None
        now_s = _iso(_utc_now())
        try:
            self.ensure_schema()
            conn = self.connect()
            try:
                row = conn.execute(
                    """
                    SELECT * FROM assistant_memory_slot
                    WHERE id = ? AND actor_user = ? AND deleted_at IS NULL
                      AND (expires_at IS NULL OR expires_at > ?)
                    """,
                    (sid, actor, now_s),
                ).fetchone()
                return _public(dict(row)) if row else None
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"get_slot: {exc}"
            return None

    def soft_delete(self, actor_user: str, slot_id: str) -> bool:
        if not memory_enabled():
            return False
        actor = (actor_user or "").strip()
        sid = (slot_id or "").strip()[:80]
        if not actor or not sid:
            return False
        try:
            self.ensure_schema()
            with self._lock:
                conn = self.connect()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    now_s = _iso(_utc_now())
                    cur = conn.execute(
                        """
                        UPDATE assistant_memory_slot
                        SET deleted_at = ?, updated_at = ?
                        WHERE id = ? AND actor_user = ? AND deleted_at IS NULL
                        """,
                        (now_s, now_s, sid, actor),
                    )
                    conn.execute("COMMIT")
                    return cur.rowcount > 0
                except Exception:
                    try:
                        conn.execute("ROLLBACK")
                    except Exception:
                        pass
                    raise
                finally:
                    conn.close()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"soft_delete: {exc}"
            logger.warning("assistant_memory soft_delete failed: %s", exc)
            return False

    def soft_delete_all(self, actor_user: str) -> int:
        if not memory_enabled():
            return 0
        actor = (actor_user or "").strip()
        if not actor:
            return 0
        try:
            self.ensure_schema()
            with self._lock:
                conn = self.connect()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    now_s = _iso(_utc_now())
                    cur = conn.execute(
                        """
                        UPDATE assistant_memory_slot
                        SET deleted_at = ?, updated_at = ?
                        WHERE actor_user = ? AND deleted_at IS NULL
                        """,
                        (now_s, now_s, actor),
                    )
                    conn.execute("COMMIT")
                    return int(cur.rowcount or 0)
                except Exception:
                    try:
                        conn.execute("ROLLBACK")
                    except Exception:
                        pass
                    raise
                finally:
                    conn.close()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"soft_delete_all: {exc}"
            logger.warning("assistant_memory soft_delete_all failed: %s", exc)
            return 0

    def soft_delete_conversation(self, actor_user: str, conversation_id: str) -> int:
        if not memory_enabled():
            return 0
        actor = (actor_user or "").strip()
        cid = (conversation_id or "").strip()[:80]
        if not actor or not cid:
            return 0
        try:
            self.ensure_schema()
            with self._lock:
                conn = self.connect()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    now_s = _iso(_utc_now())
                    cur = conn.execute(
                        """
                        UPDATE assistant_memory_slot
                        SET deleted_at = ?, updated_at = ?
                        WHERE actor_user = ?
                          AND scope = 'conversation'
                          AND conversation_id = ?
                          AND deleted_at IS NULL
                        """,
                        (now_s, now_s, actor, cid),
                    )
                    conn.execute("COMMIT")
                    return int(cur.rowcount or 0)
                except Exception:
                    try:
                        conn.execute("ROLLBACK")
                    except Exception:
                        pass
                    raise
                finally:
                    conn.close()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"soft_delete_conversation: {exc}"
            return 0

    def purge_expired(self, *, now: datetime | None = None) -> dict[str, int]:
        """Hard-delete expired and soft-deleted past grace."""
        now = now or _utc_now()
        now_s = _iso(now)
        grace_cutoff = _iso(now - timedelta(days=memory_soft_delete_grace_days()))
        deleted = 0
        try:
            self.ensure_schema()
            with self._lock:
                conn = self.connect()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    cur1 = conn.execute(
                        """
                        DELETE FROM assistant_memory_slot
                        WHERE deleted_at IS NOT NULL AND deleted_at < ?
                        """,
                        (grace_cutoff,),
                    )
                    cur2 = conn.execute(
                        """
                        DELETE FROM assistant_memory_slot
                        WHERE deleted_at IS NULL
                          AND expires_at IS NOT NULL
                          AND expires_at < ?
                        """,
                        (now_s,),
                    )
                    deleted = int(cur1.rowcount or 0) + int(cur2.rowcount or 0)
                    conn.execute("COMMIT")
                except Exception:
                    try:
                        conn.execute("ROLLBACK")
                    except Exception:
                        pass
                    raise
                finally:
                    conn.close()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"purge_expired: {exc}"
            logger.warning("assistant_memory purge failed: %s", exc)
        return {"slots": deleted}

    def _conversation_owned(self, actor: str, conversation_id: str | None) -> bool:
        if not conversation_id:
            return False
        try:
            from app.assistant.orchestrator.history_config import history_enabled

            if not history_enabled():
                # HISTORY off: cannot verify ownership in HistoryStore; accept opaque id
                # for scope isolation only (still keyed by actor_user).
                return True
            hist = HistoryStore(path=self.path)
            hist.ensure_schema()
            return hist.get_conversation(actor, conversation_id) is not None
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"conversation_owned: {exc}"
            return False

    def _enforce_caps_locked(self, conn: sqlite3.Connection, actor: str, conversation_id: str | None) -> None:
        """Soft-delete LRU overflow. Active = deleted_at IS NULL AND not expired."""
        now_s = _iso(_utc_now())
        max_user = memory_max_per_user()
        # Count all active slots for user (any scope)
        rows = conn.execute(
            """
            SELECT id FROM assistant_memory_slot
            WHERE actor_user = ? AND deleted_at IS NULL
              AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY updated_at ASC
            """,
            (actor, now_s),
        ).fetchall()
        if len(rows) > max_user:
            overflow = len(rows) - max_user
            for r in rows[:overflow]:
                conn.execute(
                    """
                    UPDATE assistant_memory_slot
                    SET deleted_at = ?, updated_at = ?
                    WHERE id = ? AND actor_user = ?
                    """,
                    (now_s, now_s, r["id"], actor),
                )

        if conversation_id:
            max_conv = memory_max_per_conversation()
            crows = conn.execute(
                """
                SELECT id FROM assistant_memory_slot
                WHERE actor_user = ?
                  AND scope = 'conversation'
                  AND conversation_id = ?
                  AND deleted_at IS NULL
                  AND (expires_at IS NULL OR expires_at > ?)
                ORDER BY updated_at ASC
                """,
                (actor, conversation_id, now_s),
            ).fetchall()
            if len(crows) > max_conv:
                overflow = len(crows) - max_conv
                for r in crows[:overflow]:
                    conn.execute(
                        """
                        UPDATE assistant_memory_slot
                        SET deleted_at = ?, updated_at = ?
                        WHERE id = ? AND actor_user = ?
                        """,
                        (now_s, now_s, r["id"], actor),
                    )


def _public(row: dict[str, Any]) -> dict[str, Any]:
    def _loads(raw: Any, default: Any) -> Any:
        if raw is None or raw == "":
            return deepcopy(default)
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return deepcopy(default)

    return {
        "id": row.get("id"),
        "actor_user": row.get("actor_user"),
        "scope": row.get("scope"),
        "conversation_id": row.get("conversation_id"),
        "memory_type": row.get("memory_type"),
        "key": row.get("key"),
        "value": _loads(row.get("value_json"), {}),
        "confidence": row.get("confidence"),
        "source": row.get("source"),
        "permission_epoch": row.get("permission_epoch"),
        "sensitivity": row.get("sensitivity"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "expires_at": row.get("expires_at"),
        "source_turn_id": row.get("source_turn_id"),
        "meta": _loads(row.get("meta_json"), {}),
    }


_DEFAULT_MEMORY: MemoryStore | None = None
_DEFAULT_MEMORY_LOCK = threading.Lock()


def get_default_memory_store() -> MemoryStore:
    global _DEFAULT_MEMORY
    with _DEFAULT_MEMORY_LOCK:
        if _DEFAULT_MEMORY is None:
            _DEFAULT_MEMORY = MemoryStore()
        return _DEFAULT_MEMORY


def reset_default_memory_store_for_tests() -> None:
    global _DEFAULT_MEMORY
    with _DEFAULT_MEMORY_LOCK:
        _DEFAULT_MEMORY = None
