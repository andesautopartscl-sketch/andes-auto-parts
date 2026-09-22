"""FASE 7B.4 — permission_epoch for memory invalidation.

Memory is NEVER an authorization source. The ERP remains the only authority.
permission_epoch only invalidates contextual memory when the actor's
authorization context changes materially (role / permission grants).

Source of truth (isolated, not business ACL itself):
  assistant_permission_epoch(actor_user, epoch) in the assistant memory DB.

Bump hooks run from seguridad when rol/permisos change; failures never
break seguridad or invent epochs from timestamps/hashes.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from app.assistant.orchestrator.memory_config import memory_db_path

logger = logging.getLogger(__name__)

EPOCH_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS assistant_permission_epoch (
    actor_user TEXT PRIMARY KEY,
    epoch INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class PermissionEpochResolution:
    """Result of resolving the actor's current permission epoch."""

    available: bool
    """True only when a robust epoch was obtained from the store."""

    epoch: int | None
    """Current epoch when available; None when unavailable."""


class PermissionEpochProvider(Protocol):
    def resolve(self, actor_user: str) -> PermissionEpochResolution:
        ...


class NeutralPermissionEpochProvider:
    """Fail-safe: no epoch source — contextual memory excluded on read (fail closed)."""

    def resolve(self, actor_user: str) -> PermissionEpochResolution:
        _ = (actor_user or "").strip()
        return PermissionEpochResolution(available=False, epoch=None)


class FixedPermissionEpochProvider:
    """Test/E2E harness — fixed available epoch."""

    def __init__(self, epoch: int):
        self.epoch = int(epoch)

    def resolve(self, actor_user: str) -> PermissionEpochResolution:
        _ = (actor_user or "").strip()
        return PermissionEpochResolution(available=True, epoch=self.epoch)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class SqlitePermissionEpochStore:
    """Atomic per-actor epoch store (BEGIN IMMEDIATE)."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else memory_db_path()
        self._lock = threading.RLock()

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def ensure_schema(self) -> None:
        with self._lock:
            conn = self.connect()
            try:
                conn.executescript(EPOCH_SCHEMA_SQL)
            finally:
                conn.close()

    def get_or_init(self, actor_user: str) -> int:
        """Return current epoch; create row at 1 if missing. Raises on DB failure."""
        actor = (actor_user or "").strip()
        if not actor:
            raise ValueError("actor_user required")
        self.ensure_schema()
        with self._lock:
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT epoch FROM assistant_permission_epoch WHERE actor_user = ?",
                    (actor[:80],),
                ).fetchone()
                if row is None:
                    conn.execute(
                        """
                        INSERT INTO assistant_permission_epoch (actor_user, epoch, updated_at)
                        VALUES (?, 1, ?)
                        """,
                        (actor[:80], _utc_iso()),
                    )
                    conn.execute("COMMIT")
                    return 1
                epoch = int(row["epoch"])
                conn.execute("COMMIT")
                return epoch
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                conn.close()

    def bump(self, actor_user: str) -> int:
        """Atomically increment epoch; create at 2 if missing was 1 after init.

        Two concurrent bumps cannot silently lose an increment (BEGIN IMMEDIATE).
        Returns the new epoch.
        """
        actor = (actor_user or "").strip()
        if not actor:
            raise ValueError("actor_user required")
        self.ensure_schema()
        with self._lock:
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT epoch FROM assistant_permission_epoch WHERE actor_user = ?",
                    (actor[:80],),
                ).fetchone()
                now = _utc_iso()
                if row is None:
                    # First material change: start at 2 so prior contextual stamped at
                    # implicit init(1) (if any) would already be invalidated — but if
                    # no prior row, use 1 then bump → 2 only when we know init was 1.
                    # Spec: initial epoch exists; first bump after init → 2.
                    conn.execute(
                        """
                        INSERT INTO assistant_permission_epoch (actor_user, epoch, updated_at)
                        VALUES (?, 2, ?)
                        """,
                        (actor[:80], now),
                    )
                    new_epoch = 2
                else:
                    new_epoch = int(row["epoch"]) + 1
                    conn.execute(
                        """
                        UPDATE assistant_permission_epoch
                        SET epoch = ?, updated_at = ?
                        WHERE actor_user = ?
                        """,
                        (new_epoch, now, actor[:80]),
                    )
                conn.execute("COMMIT")
                return new_epoch
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                conn.close()


class SqlitePermissionEpochProvider:
    """Real provider: reads integer epoch from assistant_permission_epoch."""

    def __init__(self, store: SqlitePermissionEpochStore | None = None):
        self._store = store or SqlitePermissionEpochStore()

    def resolve(self, actor_user: str) -> PermissionEpochResolution:
        actor = (actor_user or "").strip()
        if not actor:
            return PermissionEpochResolution(available=False, epoch=None)
        try:
            epoch = self._store.get_or_init(actor)
            return PermissionEpochResolution(available=True, epoch=int(epoch))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "assistant_memory permission_epoch resolve failed: %s",
                type(exc).__name__,
            )
            return PermissionEpochResolution(available=False, epoch=None)


_DEFAULT_STORE: SqlitePermissionEpochStore | None = None
_DEFAULT_PROVIDER: PermissionEpochProvider = NeutralPermissionEpochProvider()
_PROVIDER_LOCK = threading.Lock()


def _ensure_default_store_locked() -> SqlitePermissionEpochStore:
    """Create/return the process default store. Caller MUST hold _PROVIDER_LOCK."""
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = SqlitePermissionEpochStore()
    return _DEFAULT_STORE


def get_default_permission_epoch_store() -> SqlitePermissionEpochStore:
    with _PROVIDER_LOCK:
        return _ensure_default_store_locked()


def get_default_permission_epoch_provider() -> PermissionEpochProvider:
    return _DEFAULT_PROVIDER


def configure_permission_epoch_provider(provider: PermissionEpochProvider | None = None) -> PermissionEpochProvider:
    """Wire the real Sqlite provider (call from create_app). None → Sqlite default store."""
    global _DEFAULT_PROVIDER
    with _PROVIDER_LOCK:
        if provider is not None:
            _DEFAULT_PROVIDER = provider
        else:
            # Do not call get_default_permission_epoch_store() here: it also takes
            # _PROVIDER_LOCK (a non-reentrant Lock) and would deadlock create_app().
            _DEFAULT_PROVIDER = SqlitePermissionEpochProvider(_ensure_default_store_locked())
        return _DEFAULT_PROVIDER


def set_permission_epoch_provider_for_tests(provider: PermissionEpochProvider | None) -> None:
    """Tests only — inject a fake provider; None restores Neutral (no side effects)."""
    global _DEFAULT_PROVIDER
    with _PROVIDER_LOCK:
        _DEFAULT_PROVIDER = provider if provider is not None else NeutralPermissionEpochProvider()


def reset_permission_epoch_store_for_tests(path: Path | str | None = None) -> SqlitePermissionEpochStore:
    """Tests only — point default store/provider at a temp DB path."""
    global _DEFAULT_STORE, _DEFAULT_PROVIDER
    with _PROVIDER_LOCK:
        _DEFAULT_STORE = SqlitePermissionEpochStore(path) if path is not None else SqlitePermissionEpochStore()
        _DEFAULT_PROVIDER = SqlitePermissionEpochProvider(_DEFAULT_STORE)
        return _DEFAULT_STORE


def resolve_actor_permission_epoch(
    actor_user: str,
    *,
    provider: PermissionEpochProvider | None = None,
) -> PermissionEpochResolution:
    prov = provider or get_default_permission_epoch_provider()
    try:
        return prov.resolve(actor_user)
    except Exception:
        return PermissionEpochResolution(available=False, epoch=None)


def bump_actor_permission_epoch(actor_user: str, *, store: SqlitePermissionEpochStore | None = None) -> int | None:
    """Increment epoch for actor. Returns new epoch or None on soft failure."""
    actor = (actor_user or "").strip()
    if not actor:
        return None
    try:
        st = store or get_default_permission_epoch_store()
        return st.bump(actor)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "assistant_memory permission_epoch bump failed: %s",
            type(exc).__name__,
        )
        return None


def notify_permission_context_changed(actor_user: str) -> int | None:
    """Safe hook for seguridad: bump epoch; never raise into business flows.

    FASE 10.3.1 — devuelve el epoch nuevo, o None si el bump no ocurrio. El
    valor no cambia el comportamiento de nadie: existe para que quien llama
    pueda AUDITAR el resultado. Un bump que falla en silencio deja una
    autorizacion previa viva mas tiempo del debido, y eso tiene que dejar
    rastro en vez de perderse en un warning.
    """
    try:
        return bump_actor_permission_epoch(actor_user)
    except Exception:  # noqa: BLE001
        logger.warning("assistant_memory permission_epoch notify soft-failed")
        return None


def memory_passes_permission_epoch(
    slot: dict[str, Any],
    resolution: PermissionEpochResolution,
) -> bool:
    """Enforce read-time epoch rules without mutating permissions.

    FAIL CLOSED for contextual when epoch is unavailable:
    - sensitivity=benign → keep (may survive epoch changes)
    - sensitivity=contextual + unavailable → exclude
    - sensitivity=contextual + available → require matching permission_epoch
    """
    sens = str(slot.get("sensitivity") or "benign").strip().lower()
    if sens == "benign":
        return True

    # contextual (and any non-benign)
    if not resolution.available or resolution.epoch is None:
        return False

    raw = slot.get("permission_epoch")
    if raw is None:
        return False
    try:
        return int(raw) == int(resolution.epoch)
    except (TypeError, ValueError):
        return False


def sensitivity_for_memory_type(memory_type: str) -> str:
    """Server-derived sensitivity; clients cannot override."""
    from app.assistant.orchestrator.memory_schema import DEFAULT_SENSITIVITY

    mt = (memory_type or "").strip().lower()
    return DEFAULT_SENSITIVITY.get(mt, "contextual")
