"""FASE 7A — persistent assistant conversation history (no 7B memory).

Dual-write companion to TurnStore. Flag ANDES_ASSISTANT_HISTORY_ENABLED=0 → no-ops.
Never stores secrets, full prompts, full user messages (hash only), or finance PII.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.assistant.orchestrator.audit import redact
from app.assistant.orchestrator.history_config import (
    history_db_path,
    history_enabled,
    history_retention_days,
    history_soft_delete_grace_days,
    store_message_excerpt,
)
from app.assistant.orchestrator.normalizer import _strip_pii
from app.assistant.orchestrator.turn_store import MAX_EVIDENCE_PER_TURN, MAX_REPLY_EXCERPT

logger = logging.getLogger(__name__)

STATUS_ACTIVE = "active"
STATUS_DELETED = "deleted"

# Extra keys stripped before DB persistence (beyond normalizer STRIP_PII_KEYS)
HISTORY_DROP_KEYS = frozenset(
    {
        "rut",
        "email",
        "correo",
        "telefono",
        "teléfono",
        "phone",
        "direccion",
        "dirección",
        "address",
        "password",
        "token",
        "authorization",
        "cookie",
        "cookies",
        "csrf",
        "api_key",
        "apikey",
        "secret",
        "access_token",
        "bearer",
        "service_token",
        "session",
        "connection_string",
        "database_url",
        "prompt",
        "system",
        "system_prompt",
        "user_prompt",
        "message",
        "reply",
        "monto",
        "precio",
        "costo",
        "total",
        "ventas_periodo",
        "ventas_hoy",
        "ventas_mes",
        "saldo",
    }
)


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


def _scrub_history_value(value: Any) -> Any:
    value = redact(value)
    if isinstance(value, dict):
        out = {}
        for key, inner in value.items():
            lk = str(key).strip().lower()
            if lk in HISTORY_DROP_KEYS or any(
                p in lk for p in ("password", "secret", "authorization", "cookie", "api_key", "csrf")
            ):
                continue
            if lk in {"prompt_tokens", "completion_tokens", "total_tokens", "cost_estimated_usd"}:
                # latency/token aggregates may appear in flags — tokens OK, cost not stored in history turns
                if lk == "cost_estimated_usd":
                    continue
                out[key] = inner
                continue
            if "token" in lk and lk not in {"prompt_tokens", "completion_tokens", "total_tokens"}:
                continue
            out[key] = _scrub_history_value(inner)
        return out
    if isinstance(value, list):
        return [_scrub_history_value(v) for v in value]
    if isinstance(value, str):
        if "@" in value and "." in value:
            return "[redacted]"
        return value
    return value


def sanitize_turn_for_history(turn: dict[str, Any]) -> dict[str, Any]:
    """Produce a persistence-safe turn payload (hash, redacted evidence, short excerpts)."""
    entities = turn.get("entities") if isinstance(turn.get("entities"), dict) else {}
    evidence_in = turn.get("evidence") if isinstance(turn.get("evidence"), list) else []
    evidence: list[dict[str, Any]] = []
    for item in evidence_in[:MAX_EVIDENCE_PER_TURN]:
        if not isinstance(item, dict):
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
        evidence.append(
            {
                "tool": str(item.get("tool") or "")[:80],
                "ok": bool(item.get("ok")),
                "empty": bool(item.get("empty")),
                "error_code": item.get("error_code"),
                "classification": item.get("classification") or "INTERNAL",
                "data": _scrub_history_value(_strip_pii(data)),
                "meta": _scrub_history_value(_strip_pii(meta)),
            }
        )
    tools = [str(t) for t in (turn.get("tools_used") or []) if t][:MAX_EVIDENCE_PER_TURN]
    reply = str(turn.get("reply_excerpt") or "")[:MAX_REPLY_EXCERPT]
    flags = turn.get("flags") if isinstance(turn.get("flags"), dict) else {}
    message_excerpt = None
    if store_message_excerpt():
        raw_ex = str(turn.get("message_excerpt") or "")[:80]
        message_excerpt = _scrub_history_value(raw_ex) if raw_ex else None
        if isinstance(message_excerpt, str) and not message_excerpt.strip():
            message_excerpt = None

    return {
        "message_hash": str(turn.get("message_hash") or "")[:32],
        "message_excerpt": message_excerpt,
        "reply_excerpt": _scrub_history_value(reply) if isinstance(reply, str) else "",
        "scenario": turn.get("scenario"),
        "classification": turn.get("classification"),
        "tools_used": tools,
        "entities": _scrub_history_value(_strip_pii(dict(entities))),
        "evidence": evidence,
        "flags": _scrub_history_value(dict(flags)),
        "llm_latency_ms": int(turn.get("llm_latency_ms") or 0),
        "total_latency_ms": int(turn.get("total_latency_ms") or 0),
        "planner_mode": str(turn.get("planner_mode") or "")[:32] or None,
        "correlation_id": str(turn.get("correlation_id") or "")[:80] or None,
    }


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS assistant_conversation (
    id TEXT PRIMARY KEY,
    client_conversation_id TEXT,
    actor_user TEXT NOT NULL,
    title TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_turn_at TEXT,
    turn_count INTEGER NOT NULL DEFAULT 0,
    retention_until TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS assistant_turn (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    actor_user TEXT NOT NULL,
    seq INTEGER NOT NULL,
    correlation_id TEXT,
    message_hash TEXT,
    message_excerpt TEXT,
    reply_excerpt TEXT,
    scenario TEXT,
    classification TEXT,
    tools_used_json TEXT,
    entities_json TEXT,
    evidence_json TEXT,
    flags_json TEXT,
    llm_latency_ms INTEGER,
    total_latency_ms INTEGER,
    planner_mode TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES assistant_conversation(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_asst_turn_conv_seq
    ON assistant_turn(conversation_id, seq);

CREATE INDEX IF NOT EXISTS ix_asst_conv_actor_updated
    ON assistant_conversation(actor_user, updated_at);

CREATE INDEX IF NOT EXISTS ix_asst_conv_retention
    ON assistant_conversation(retention_until);

CREATE INDEX IF NOT EXISTS ix_asst_turn_actor_created
    ON assistant_turn(actor_user, created_at);

CREATE INDEX IF NOT EXISTS ix_asst_turn_correlation
    ON assistant_turn(correlation_id);

CREATE UNIQUE INDEX IF NOT EXISTS ux_asst_conv_actor_client
    ON assistant_conversation(actor_user, client_conversation_id)
    WHERE deleted_at IS NULL
      AND client_conversation_id IS NOT NULL
      AND client_conversation_id != '';
"""


class HistoryStore:
    """SQLite-backed conversation history with ownership and soft-delete."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else history_db_path()
        self._lock = threading.RLock()
        self.last_error: str | None = None
        self._ensure_parent()

    def _ensure_parent(self) -> None:
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

    def _retention_until(self, from_dt: datetime | None = None) -> str:
        base = from_dt or _utc_now()
        return _iso(base + timedelta(days=history_retention_days()))

    def ensure_conversation(
        self,
        actor_user: str,
        *,
        conversation_id: str | None = None,
        client_conversation_id: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any] | None:
        """Resolve or create a conversation owned by actor. Returns public dict or None on hard failure."""
        actor = (actor_user or "").strip()
        if not actor:
            return None
        client_id = (client_conversation_id or "").strip()[:80] or None
        wanted_id = (conversation_id or "").strip()[:80] or None

        try:
            self.ensure_schema()
            with self._lock:
                conn = self.connect()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    row = None
                    if wanted_id:
                        row = conn.execute(
                            """
                            SELECT * FROM assistant_conversation
                            WHERE id = ? AND actor_user = ? AND deleted_at IS NULL
                            """,
                            (wanted_id, actor),
                        ).fetchone()
                    if row is None and client_id:
                        row = conn.execute(
                            """
                            SELECT * FROM assistant_conversation
                            WHERE actor_user = ? AND client_conversation_id = ?
                              AND deleted_at IS NULL
                            """,
                            (actor, client_id),
                        ).fetchone()
                    if row is not None:
                        conn.execute("COMMIT")
                        return _conv_public(dict(row))

                    new_id = wanted_id or str(uuid.uuid4())
                    now = _utc_now()
                    now_s = _iso(now)
                    conn.execute(
                        """
                        INSERT INTO assistant_conversation (
                            id, client_conversation_id, actor_user, title, status,
                            created_at, updated_at, last_turn_at, turn_count,
                            retention_until, deleted_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, NULL)
                        """,
                        (
                            new_id,
                            client_id,
                            actor,
                            (title or "Conversación")[:120],
                            STATUS_ACTIVE,
                            now_s,
                            now_s,
                            None,
                            self._retention_until(now),
                        ),
                    )
                    conn.execute("COMMIT")
                    return {
                        "id": new_id,
                        "client_conversation_id": client_id,
                        "actor_user": actor,
                        "title": (title or "Conversación")[:120],
                        "status": STATUS_ACTIVE,
                        "created_at": now_s,
                        "updated_at": now_s,
                        "last_turn_at": None,
                        "turn_count": 0,
                        "retention_until": self._retention_until(now),
                    }
                except Exception:
                    try:
                        conn.execute("ROLLBACK")
                    except Exception:
                        pass
                    raise
                finally:
                    conn.close()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"ensure_conversation: {exc}"
            logger.warning("assistant_history ensure_conversation failed: %s", exc)
            return None

    def append_turn(
        self,
        actor_user: str,
        conversation_id: str,
        turn: dict[str, Any],
    ) -> int | None:
        """Append a sanitized turn with monotonic seq. Returns seq or None on failure."""
        actor = (actor_user or "").strip()
        conv_id = (conversation_id or "").strip()[:80]
        if not actor or not conv_id:
            return None
        safe = sanitize_turn_for_history(turn)
        try:
            self.ensure_schema()
            with self._lock:
                conn = self.connect()
                try:
                    for attempt in range(3):
                        try:
                            conn.execute("BEGIN IMMEDIATE")
                            conv = conn.execute(
                                """
                                SELECT id, turn_count FROM assistant_conversation
                                WHERE id = ? AND actor_user = ? AND deleted_at IS NULL
                                """,
                                (conv_id, actor),
                            ).fetchone()
                            if conv is None:
                                conn.execute("ROLLBACK")
                                self.last_error = "append_turn: conversation not found or not owned"
                                return None
                            row_max = conn.execute(
                                "SELECT COALESCE(MAX(seq), 0) AS m FROM assistant_turn WHERE conversation_id = ?",
                                (conv_id,),
                            ).fetchone()
                            next_seq = int(row_max["m"] if row_max else 0) + 1
                            now_s = _iso(_utc_now())
                            turn_id = str(uuid.uuid4())
                            conn.execute(
                                """
                                INSERT INTO assistant_turn (
                                    id, conversation_id, actor_user, seq, correlation_id,
                                    message_hash, message_excerpt, reply_excerpt, scenario,
                                    classification, tools_used_json, entities_json, evidence_json,
                                    flags_json, llm_latency_ms, total_latency_ms, planner_mode,
                                    created_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    turn_id,
                                    conv_id,
                                    actor,
                                    next_seq,
                                    safe.get("correlation_id"),
                                    safe.get("message_hash"),
                                    safe.get("message_excerpt"),
                                    safe.get("reply_excerpt"),
                                    safe.get("scenario"),
                                    safe.get("classification"),
                                    json.dumps(safe.get("tools_used") or [], ensure_ascii=False),
                                    json.dumps(safe.get("entities") or {}, ensure_ascii=False),
                                    json.dumps(safe.get("evidence") or [], ensure_ascii=False),
                                    json.dumps(safe.get("flags") or {}, ensure_ascii=False),
                                    safe.get("llm_latency_ms"),
                                    safe.get("total_latency_ms"),
                                    safe.get("planner_mode"),
                                    now_s,
                                ),
                            )
                            conn.execute(
                                """
                                UPDATE assistant_conversation
                                SET turn_count = ?,
                                    updated_at = ?,
                                    last_turn_at = ?,
                                    retention_until = ?,
                                    title = CASE
                                        WHEN turn_count = 0 AND ? IS NOT NULL AND ? != ''
                                        THEN substr(?, 1, 120)
                                        ELSE title
                                    END
                                WHERE id = ? AND actor_user = ?
                                """,
                                (
                                    next_seq,
                                    now_s,
                                    now_s,
                                    self._retention_until(_utc_now()),
                                    safe.get("scenario"),
                                    safe.get("scenario"),
                                    str(safe.get("scenario") or ""),
                                    conv_id,
                                    actor,
                                ),
                            )
                            conn.execute("COMMIT")
                            return next_seq
                        except sqlite3.IntegrityError:
                            try:
                                conn.execute("ROLLBACK")
                            except Exception:
                                pass
                            if attempt + 1 >= 3:
                                raise
                            time.sleep(0.01 * (attempt + 1))
                    return None
                finally:
                    conn.close()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"append_turn: {exc}"
            logger.warning("assistant_history append_turn failed: %s", exc)
            return None

    def get_conversation(self, actor_user: str, conversation_id: str) -> dict[str, Any] | None:
        actor = (actor_user or "").strip()
        conv_id = (conversation_id or "").strip()[:80]
        if not actor or not conv_id:
            return None
        try:
            self.ensure_schema()
            conn = self.connect()
            try:
                row = conn.execute(
                    """
                    SELECT * FROM assistant_conversation
                    WHERE id = ? AND actor_user = ? AND deleted_at IS NULL
                    """,
                    (conv_id, actor),
                ).fetchone()
                return _conv_public(dict(row)) if row else None
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"get_conversation: {exc}"
            return None

    def list_conversations(
        self,
        actor_user: str,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        actor = (actor_user or "").strip()
        limit = max(1, min(int(limit or 20), 100))
        if not actor:
            return {"items": [], "next_cursor": None}
        cursor_updated, cursor_id = _decode_cursor(cursor)
        try:
            self.ensure_schema()
            conn = self.connect()
            try:
                if cursor_updated and cursor_id:
                    rows = conn.execute(
                        """
                        SELECT * FROM assistant_conversation
                        WHERE actor_user = ? AND deleted_at IS NULL
                          AND (
                            updated_at < ?
                            OR (updated_at = ? AND id < ?)
                          )
                        ORDER BY updated_at DESC, id DESC
                        LIMIT ?
                        """,
                        (actor, cursor_updated, cursor_updated, cursor_id, limit + 1),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT * FROM assistant_conversation
                        WHERE actor_user = ? AND deleted_at IS NULL
                        ORDER BY updated_at DESC, id DESC
                        LIMIT ?
                        """,
                        (actor, limit + 1),
                    ).fetchall()
                items = [_conv_public(dict(r)) for r in rows[:limit]]
                next_cursor = None
                if len(rows) > limit:
                    last = items[-1]
                    next_cursor = _encode_cursor(str(last.get("updated_at") or ""), str(last.get("id") or ""))
                return {"items": items, "next_cursor": next_cursor}
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"list_conversations: {exc}"
            return {"items": [], "next_cursor": None}

    def list_turns(
        self,
        actor_user: str,
        conversation_id: str,
        *,
        limit: int = 20,
        before_seq: int | None = None,
        after_seq: int | None = None,
    ) -> dict[str, Any]:
        actor = (actor_user or "").strip()
        conv_id = (conversation_id or "").strip()[:80]
        limit = max(1, min(int(limit or 20), 100))
        if not actor or not conv_id:
            return {"items": [], "next_before_seq": None}
        if self.get_conversation(actor, conv_id) is None:
            return {"items": [], "next_before_seq": None, "error_code": "not_found"}
        try:
            self.ensure_schema()
            conn = self.connect()
            try:
                if after_seq is not None:
                    rows = conn.execute(
                        """
                        SELECT * FROM assistant_turn
                        WHERE conversation_id = ? AND actor_user = ? AND seq > ?
                        ORDER BY seq ASC
                        LIMIT ?
                        """,
                        (conv_id, actor, int(after_seq), limit + 1),
                    ).fetchall()
                    items = [_turn_public(dict(r)) for r in rows[:limit]]
                    next_before = None
                    return {"items": items, "next_before_seq": next_before}
                if before_seq is not None:
                    rows = conn.execute(
                        """
                        SELECT * FROM assistant_turn
                        WHERE conversation_id = ? AND actor_user = ? AND seq < ?
                        ORDER BY seq DESC
                        LIMIT ?
                        """,
                        (conv_id, actor, int(before_seq), limit + 1),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT * FROM assistant_turn
                        WHERE conversation_id = ? AND actor_user = ?
                        ORDER BY seq DESC
                        LIMIT ?
                        """,
                        (conv_id, actor, limit + 1),
                    ).fetchall()
                page = list(rows[:limit])
                items = [_turn_public(dict(r)) for r in reversed(page)]
                next_before = None
                if len(rows) > limit and items:
                    next_before = int(items[0]["seq"])
                return {"items": items, "next_before_seq": next_before}
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"list_turns: {exc}"
            return {"items": [], "next_before_seq": None}

    def recent_turns_for_resolver(
        self,
        actor_user: str,
        conversation_id: str,
        *,
        limit: int = 6,
    ) -> list[dict[str, Any]]:
        """Return TurnStore-shaped turns (oldest→newest) for FASE 5 hydration."""
        result = self.list_turns(actor_user, conversation_id, limit=limit)
        items = result.get("items") or []
        out: list[dict[str, Any]] = []
        for item in items:
            created = _parse_iso(item.get("created_at"))
            ts = created.timestamp() if created else time.time()
            out.append(
                {
                    "ts": ts,
                    "message_hash": item.get("message_hash") or "",
                    "tools_used": item.get("tools_used") or [],
                    "scenario": item.get("scenario"),
                    "entities": item.get("entities") or {},
                    "evidence": item.get("evidence") or [],
                    "reply_excerpt": item.get("reply_excerpt") or "",
                }
            )
        return out

    def soft_delete_conversation(self, actor_user: str, conversation_id: str) -> bool:
        actor = (actor_user or "").strip()
        conv_id = (conversation_id or "").strip()[:80]
        if not actor or not conv_id:
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
                        UPDATE assistant_conversation
                        SET status = ?, deleted_at = ?, updated_at = ?
                        WHERE id = ? AND actor_user = ? AND deleted_at IS NULL
                        """,
                        (STATUS_DELETED, now_s, now_s, conv_id, actor),
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
            logger.warning("assistant_history soft_delete failed: %s", exc)
            return False

    def purge_expired(self, *, now: datetime | None = None) -> dict[str, int]:
        """Hard-delete past retention and soft-deleted past grace."""
        now = now or _utc_now()
        now_s = _iso(now)
        grace_cutoff = _iso(now - timedelta(days=history_soft_delete_grace_days()))
        deleted_convs = 0
        deleted_turns = 0
        try:
            self.ensure_schema()
            with self._lock:
                conn = self.connect()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    # Soft-deleted past grace
                    soft_ids = [
                        r["id"]
                        for r in conn.execute(
                            """
                            SELECT id FROM assistant_conversation
                            WHERE deleted_at IS NOT NULL AND deleted_at < ?
                            """,
                            (grace_cutoff,),
                        ).fetchall()
                    ]
                    # Retention expired (active or not)
                    ret_ids = [
                        r["id"]
                        for r in conn.execute(
                            """
                            SELECT id FROM assistant_conversation
                            WHERE retention_until < ?
                            """,
                            (now_s,),
                        ).fetchall()
                    ]
                    ids = list({*soft_ids, *ret_ids})
                    for cid in ids:
                        tcur = conn.execute(
                            "DELETE FROM assistant_turn WHERE conversation_id = ?", (cid,)
                        )
                        deleted_turns += int(tcur.rowcount or 0)
                        ccur = conn.execute(
                            "DELETE FROM assistant_conversation WHERE id = ?", (cid,)
                        )
                        deleted_convs += int(ccur.rowcount or 0)
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
            logger.warning("assistant_history purge failed: %s", exc)
        return {"conversations": deleted_convs, "turns": deleted_turns}


def _conv_public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "client_conversation_id": row.get("client_conversation_id"),
        "actor_user": row.get("actor_user"),
        "title": row.get("title"),
        "status": row.get("status"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "last_turn_at": row.get("last_turn_at"),
        "turn_count": int(row.get("turn_count") or 0),
        "retention_until": row.get("retention_until"),
    }


def _turn_public(row: dict[str, Any]) -> dict[str, Any]:
    def _loads(raw: Any, default: Any) -> Any:
        if raw is None or raw == "":
            return deepcopy(default)
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return deepcopy(default)

    return {
        "id": row.get("id"),
        "conversation_id": row.get("conversation_id"),
        "seq": int(row.get("seq") or 0),
        "correlation_id": row.get("correlation_id"),
        "message_hash": row.get("message_hash"),
        "message_excerpt": row.get("message_excerpt"),
        "reply_excerpt": row.get("reply_excerpt"),
        "scenario": row.get("scenario"),
        "classification": row.get("classification"),
        "tools_used": _loads(row.get("tools_used_json"), []),
        "entities": _loads(row.get("entities_json"), {}),
        "evidence": _loads(row.get("evidence_json"), []),
        "flags": _loads(row.get("flags_json"), {}),
        "llm_latency_ms": row.get("llm_latency_ms"),
        "total_latency_ms": row.get("total_latency_ms"),
        "planner_mode": row.get("planner_mode"),
        "created_at": row.get("created_at"),
    }


def _encode_cursor(updated_at: str, conv_id: str) -> str:
    return f"{updated_at}|{conv_id}"


def _decode_cursor(cursor: str | None) -> tuple[str | None, str | None]:
    if not cursor or "|" not in cursor:
        return None, None
    updated_at, conv_id = cursor.split("|", 1)
    updated_at = updated_at.strip()
    conv_id = conv_id.strip()
    if not updated_at or not conv_id:
        return None, None
    return updated_at, conv_id


_DEFAULT_HISTORY: HistoryStore | None = None
_DEFAULT_HISTORY_LOCK = threading.Lock()


def get_default_history_store() -> HistoryStore:
    global _DEFAULT_HISTORY
    with _DEFAULT_HISTORY_LOCK:
        if _DEFAULT_HISTORY is None:
            _DEFAULT_HISTORY = HistoryStore()
        return _DEFAULT_HISTORY


def reset_default_history_store_for_tests() -> None:
    global _DEFAULT_HISTORY
    with _DEFAULT_HISTORY_LOCK:
        _DEFAULT_HISTORY = None
