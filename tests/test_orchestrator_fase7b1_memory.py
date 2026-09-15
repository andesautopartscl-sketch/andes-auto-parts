"""FASE 7B.1 — memory store (no planner integration)."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app.assistant.orchestrator.history_store import HistoryStore
from app.assistant.orchestrator.memory_config import memory_enabled, memory_history_sqlite_aligned
from app.assistant.orchestrator.memory_schema import MemorySchemaError
from app.assistant.orchestrator.memory_sanitize import sanitize_memory_record
from app.assistant.orchestrator.memory_store import MemoryStore
from app.assistant.orchestrator.planner import FakePlanner
from app.assistant.orchestrator.service import run_orchestrator_chat
from app.assistant.orchestrator.turn_store import TurnStore


class Fase7B1MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db = Path(self._tmpdir.name) / "mem.db"
        self.store = MemoryStore(path=self.db)
        self.store.ensure_schema()
        self._env = patch.dict(
            os.environ,
            {
                "ANDES_ASSISTANT_MEMORY_ENABLED": "1",
                "ANDES_ASSISTANT_MEMORY_DERIVED": "0",
                "ANDES_ASSISTANT_HISTORY_ENABLED": "0",
                "ANDES_ASSISTANT_NL_ENABLED": "0",
                "ANDES_ORCH_PLANNER": "fake",
            },
            clear=False,
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmpdir.cleanup()

    def test_memory_flag_off_noop(self):
        with patch.dict(os.environ, {"ANDES_ASSISTANT_MEMORY_ENABLED": "0"}, clear=False):
            self.assertFalse(memory_enabled())
            out = self.store.upsert(
                actor_user="alice",
                scope="user",
                memory_type="preference",
                key="pref.answer_style",
                value={"answer_style": "brief"},
            )
            self.assertIsNone(out)
            self.assertEqual(self.store.list_slots("alice"), [])

    def test_create_user_scope(self):
        slot = self.store.upsert(
            actor_user="alice",
            scope="user",
            memory_type="preference",
            key="pref.answer_style",
            value={"answer_style": "brief"},
            source="explicit",
        )
        self.assertIsNotNone(slot)
        self.assertEqual(slot["scope"], "user")
        self.assertIsNone(slot["conversation_id"])
        self.assertEqual(slot["value"]["answer_style"], "brief")
        listed = self.store.list_slots("alice", scope="user")
        self.assertEqual(len(listed), 1)

    def test_create_conversation_scope(self):
        slot = self.store.upsert(
            actor_user="alice",
            scope="conversation",
            conversation_id="conv-abc",
            memory_type="pinned_entity",
            key="pin.codigo",
            value={"kind": "codigo", "value": "2404"},
            source="ui",
        )
        self.assertIsNotNone(slot)
        self.assertEqual(slot["conversation_id"], "conv-abc")
        other = self.store.list_slots("alice", conversation_id="conv-other")
        self.assertEqual(other, [])
        same = self.store.list_slots("alice", conversation_id="conv-abc")
        self.assertEqual(len(same), 1)

    def test_ownership_actor_isolation(self):
        slot = self.store.upsert(
            actor_user="alice",
            scope="user",
            memory_type="ui_pref",
            key="ui.compact",
            value={"compact": True},
            source="ui",
        )
        self.assertIsNone(self.store.get_slot("bob", slot["id"]))
        self.assertEqual(self.store.list_slots("bob"), [])
        self.assertFalse(self.store.soft_delete("bob", slot["id"]))
        self.assertIsNotNone(self.store.get_slot("alice", slot["id"]))

    def test_conversation_ownership_with_history(self):
        with patch.dict(os.environ, {"ANDES_ASSISTANT_HISTORY_ENABLED": "1"}, clear=False):
            hist = HistoryStore(path=self.db)
            hist.ensure_schema()
            conv = hist.ensure_conversation("alice", client_conversation_id="c1")
            cid = conv["id"]
            ok = self.store.upsert(
                actor_user="alice",
                scope="conversation",
                conversation_id=cid,
                memory_type="pinned_entity",
                key="pin.codigo",
                value={"kind": "codigo", "value": "2404"},
            )
            self.assertIsNotNone(ok)
            bad = self.store.upsert(
                actor_user="alice",
                scope="conversation",
                conversation_id="not-owned-uuid",
                memory_type="pinned_entity",
                key="pin.other",
                value={"kind": "codigo", "value": "9999"},
            )
            self.assertIsNone(bad)

    def test_split_memory_history_db_is_unsupported(self):
        mem = Path(self._tmpdir.name) / "mem_only.db"
        hist = Path(self._tmpdir.name) / "hist_only.db"
        with patch.dict(
            os.environ,
            {
                "ANDES_ASSISTANT_HISTORY_ENABLED": "1",
                "ANDES_ASSISTANT_MEMORY_DB": str(mem.resolve()),
                "ANDES_ASSISTANT_HISTORY_DB": str(hist.resolve()),
            },
            clear=False,
        ):
            self.assertFalse(memory_history_sqlite_aligned())
            hs = HistoryStore(path=hist)
            conv = hs.ensure_conversation("alice", client_conversation_id="c-split")
            self.assertIsNotNone(conv)
            store = MemoryStore(path=mem.resolve())
            slot = store.upsert(
                actor_user="alice",
                scope="conversation",
                conversation_id=conv["id"],
                memory_type="pinned_entity",
                key="pin.codigo",
                value={"kind": "codigo", "value": "2404"},
            )
            self.assertIsNone(slot)
            self.assertIn("memory_history_db_mismatch", store.last_error or "")

    def test_type_whitelist_and_schema(self):
        self.assertIsNone(
            self.store.upsert(
                actor_user="alice",
                scope="user",
                memory_type="hack_type",
                key="x",
                value={"foo": 1},
            )
        )
        with self.assertRaises(MemorySchemaError):
            sanitize_memory_record(
                actor_user="alice",
                scope="user",
                conversation_id=None,
                memory_type="preference",
                key="pref.answer_style",
                value={"answer_style": "telepathic", "role": "admin"},
            )

    def test_expired_memory_hidden(self):
        past = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        slot = self.store.upsert(
            actor_user="alice",
            scope="user",
            memory_type="preference",
            key="pref.answer_style",
            value={"answer_style": "brief"},
            expires_at=past,
        )
        self.assertIsNotNone(slot)
        self.assertEqual(self.store.list_slots("alice"), [])
        self.assertIsNone(self.store.get_slot("alice", slot["id"]))

    def test_soft_delete_by_id_and_all(self):
        a = self.store.upsert(
            actor_user="alice",
            scope="user",
            memory_type="preference",
            key="pref.answer_style",
            value={"answer_style": "brief"},
        )
        b = self.store.upsert(
            actor_user="alice",
            scope="user",
            memory_type="ui_pref",
            key="ui.compact",
            value={"compact": False},
            source="ui",
        )
        self.assertTrue(self.store.soft_delete("alice", a["id"]))
        self.assertIsNone(self.store.get_slot("alice", a["id"]))
        self.assertEqual(len(self.store.list_slots("alice")), 1)
        n = self.store.soft_delete_all("alice")
        self.assertGreaterEqual(n, 1)
        self.assertEqual(self.store.list_slots("alice"), [])
        # soft-deleted still in DB
        conn = sqlite3.connect(str(self.db))
        deleted = conn.execute(
            "SELECT COUNT(*) FROM assistant_memory_slot WHERE actor_user='alice' AND deleted_at IS NOT NULL"
        ).fetchone()[0]
        conn.close()
        self.assertGreaterEqual(deleted, 2)
        _ = b

    def test_cap_50_user_lru(self):
        with patch(
            "app.assistant.orchestrator.memory_store.memory_max_per_user",
            return_value=3,
        ):
            ids = []
            for i in range(4):
                slot = self.store.upsert(
                    actor_user="alice",
                    scope="user",
                    memory_type="frequent_entity",
                    key=f"entity.codigo.{i}",
                    value={"kind": "codigo", "value": f"C{i}", "hit_count": 1},
                    source="derived",
                )
                ids.append(slot["id"])
            active = self.store.list_slots("alice")
            self.assertEqual(len(active), 3)
            active_ids = {s["id"] for s in active}
            self.assertNotIn(ids[0], active_ids)

    def test_cap_10_conversation_lru(self):
        with patch(
            "app.assistant.orchestrator.memory_store.memory_max_per_conversation",
            return_value=2,
        ):
            for i in range(3):
                self.store.upsert(
                    actor_user="alice",
                    scope="conversation",
                    conversation_id="conv-x",
                    memory_type="pinned_entity",
                    key=f"pin.{i}",
                    value={"kind": "codigo", "value": f"A{i}"},
                    source="ui",
                )
            active = self.store.list_slots("alice", conversation_id="conv-x")
            self.assertEqual(len(active), 2)

    def test_sanitize_secrets_pii_financials(self):
        with self.assertRaises(MemorySchemaError):
            sanitize_memory_record(
                actor_user="alice",
                scope="user",
                conversation_id=None,
                memory_type="preference",
                key="pref.answer_style",
                value={"answer_style": "brief", "api_key": "sk-secret", "Authorization": "Bearer x"},
            )
        with self.assertRaises(MemorySchemaError):
            sanitize_memory_record(
                actor_user="alice",
                scope="user",
                conversation_id=None,
                memory_type="pinned_entity",
                key="pin.bad",
                value={"kind": "codigo", "value": "user@example.com"},
            )
        with self.assertRaises(MemorySchemaError):
            sanitize_memory_record(
                actor_user="alice",
                scope="user",
                conversation_id=None,
                memory_type="preference",
                key="pref.answer_style",
                value={"answer_style": "brief", "monto": 99999, "permisos": ["admin"]},
            )
        ok = sanitize_memory_record(
            actor_user="alice",
            scope="user",
            conversation_id=None,
            memory_type="conversation_summary",
            key="sum.1",
            value={"text": "Consulto stock 2404 email leak@x.com", "tools": ["get_inventory"]},
            source="derived",
        )
        self.assertIn("[redacted]", ok["value"]["text"])
        self.assertNotIn("leak@x.com", ok["value"]["text"])

    def test_sanitize_auth_markers_never_persist(self):
        """Auth scheme prefixes must not remain after scrub (E2E Bearer residual fix)."""
        samples = [
            "Bearer SECRET",
            "Bearer [redacted]",
            "Authorization: Bearer SECRET",
            "Basic SECRET",
            "Token SECRET",
            "api_key=SECRET",
            "cookie=SECRET",
            "csrf=SECRET",
            "m2m=SECRET",
        ]
        forbidden = (
            "bearer",
            "authorization",
            "basic",
            "token",
            "api_key",
            "apikey",
            "secret",
            "password",
            "cookie",
            "csrf",
            "m2m",
        )
        for raw in samples:
            with self.subTest(raw=raw):
                safe = sanitize_memory_record(
                    actor_user="alice",
                    scope="conversation",
                    conversation_id="conv-auth",
                    memory_type="conversation_summary",
                    key=f"sum.auth.{hash(raw) & 0xFFFF:x}",
                    value={"text": f"Consulta stock. {raw}", "tools": ["get_inventory"]},
                    source="derived",
                )
                text = safe["value"]["text"]
                lowered = text.lower()
                for marker in forbidden:
                    self.assertNotIn(marker, lowered, msg=f"marker {marker!r} left in {text!r}")
                self.assertNotIn("SECRET", text)
                slot = self.store.upsert(
                    actor_user="alice",
                    scope="conversation",
                    conversation_id="conv-auth",
                    memory_type="conversation_summary",
                    key=f"sum.auth.persist.{hash(raw) & 0xFFFF:x}",
                    value={"text": f"Consulta stock. {raw}", "tools": ["get_inventory"]},
                    source="derived",
                )
                self.assertIsNotNone(slot)
                persisted = (slot or {}).get("value", {}).get("text", "")
                pl = persisted.lower()
                for marker in forbidden:
                    self.assertNotIn(marker, pl, msg=f"persisted marker {marker!r} in {persisted!r}")
                self.assertNotIn("SECRET", persisted)

    def test_raw_input_never_persisted(self):
        self.store.upsert(
            actor_user="alice",
            scope="user",
            memory_type="preference",
            key="pref.answer_style",
            value={"answer_style": "brief"},
        )
        raw = self.db.read_bytes()
        self.assertNotIn(b"prefiero respuestas", raw)
        self.assertNotIn(b"system prompt", raw)
        self.assertNotIn(b"sk-", raw)

    def test_db_down_soft_fail(self):
        class Boom(MemoryStore):
            def ensure_schema(self):
                raise RuntimeError("db down")

        boom = Boom(path=self.db)
        self.assertIsNone(
            boom.upsert(
                actor_user="alice",
                scope="user",
                memory_type="preference",
                key="pref.answer_style",
                value={"answer_style": "brief"},
            )
        )
        self.assertEqual(boom.list_slots("alice"), [])

    def test_restart_hydrate(self):
        slot = self.store.upsert(
            actor_user="alice",
            scope="user",
            memory_type="preference",
            key="pref.answer_style",
            value={"answer_style": "detailed"},
        )
        fresh = MemoryStore(path=self.db)
        got = fresh.get_slot("alice", slot["id"])
        self.assertIsNotNone(got)
        self.assertEqual(got["value"]["answer_style"], "detailed")

    def test_invalid_conversation_scope_requires_id(self):
        self.assertIsNone(
            self.store.upsert(
                actor_user="alice",
                scope="conversation",
                conversation_id=None,
                memory_type="pinned_entity",
                key="pin.x",
                value={"kind": "codigo", "value": "2404"},
            )
        )

    def test_memory_cannot_store_permissions_or_write(self):
        with self.assertRaises(MemorySchemaError):
            sanitize_memory_record(
                actor_user="alice",
                scope="user",
                conversation_id=None,
                memory_type="preference",
                key="pref.answer_style",
                value={"answer_style": "brief", "role": "admin", "permiso": "ver_finanzas"},
            )
        # No WRITE memory type exists
        self.assertIsNone(
            self.store.upsert(
                actor_user="alice",
                scope="user",
                memory_type="write_intent",
                key="w",
                value={"action": "create_oc"},
            )
        )

    def test_history_on_memory_off_chat_intact(self):
        with patch.dict(
            os.environ,
            {"ANDES_ASSISTANT_HISTORY_ENABLED": "1", "ANDES_ASSISTANT_MEMORY_ENABLED": "0"},
            clear=False,
        ):
            hist = HistoryStore(path=self.db)
            hist.ensure_schema()
            turns = TurnStore()
            r = run_orchestrator_chat(
                message="stock 2404",
                actor_user="alice",
                conversation_id="conv-hm",
                invoke_fn=lambda p: (
                    200,
                    {
                        "ok": True,
                        "tool": "get_inventory",
                        "classification": "INTERNAL",
                        "write": False,
                        "data": {"codigo": "2404", "total_stock": 1, "items": []},
                        "meta": {},
                    },
                ),
                planner=FakePlanner(),
                turn_store=turns,
                history_store=hist,
                force_scenario="inventory_only",
            )
            self.assertTrue(r["ok"])
            self.assertEqual(self.store.list_slots("alice"), [])

    def test_history_off_memory_on(self):
        with patch.dict(
            os.environ,
            {"ANDES_ASSISTANT_HISTORY_ENABLED": "0", "ANDES_ASSISTANT_MEMORY_ENABLED": "1"},
            clear=False,
        ):
            slot = self.store.upsert(
                actor_user="alice",
                scope="user",
                memory_type="ui_pref",
                key="ui.compact",
                value={"compact": True},
                source="ui",
            )
            self.assertIsNotNone(slot)
            # Chat still works without reading memory (7B.1 has no planner wire)
            r = run_orchestrator_chat(
                message="hola",
                actor_user="alice",
                conversation_id="c-off",
                invoke_fn=lambda p: (500, {"ok": False}),
                planner=FakePlanner(),
                turn_store=TurnStore(),
                force_scenario="ambiguous",
            )
            self.assertTrue(r["ok"])
            self.assertTrue(r.get("needs_clarification"))

    def test_purge_expired(self):
        past = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        slot = self.store.upsert(
            actor_user="alice",
            scope="user",
            memory_type="preference",
            key="pref.answer_style",
            value={"answer_style": "brief"},
            expires_at=past,
        )
        result = self.store.purge_expired()
        self.assertGreaterEqual(result["slots"], 1)
        conn = sqlite3.connect(str(self.db))
        n = conn.execute(
            "SELECT COUNT(*) FROM assistant_memory_slot WHERE id=?", (slot["id"],)
        ).fetchone()[0]
        conn.close()
        self.assertEqual(n, 0)

    def test_delete_conversation_scoped(self):
        self.store.upsert(
            actor_user="alice",
            scope="conversation",
            conversation_id="conv-del",
            memory_type="pinned_entity",
            key="pin.codigo",
            value={"kind": "codigo", "value": "2404"},
            source="ui",
        )
        self.store.upsert(
            actor_user="alice",
            scope="user",
            memory_type="preference",
            key="pref.answer_style",
            value={"answer_style": "brief"},
        )
        n = self.store.soft_delete_conversation("alice", "conv-del")
        self.assertEqual(n, 1)
        self.assertEqual(self.store.list_slots("alice", conversation_id="conv-del"), [])
        self.assertEqual(len(self.store.list_slots("alice", scope="user")), 1)


if __name__ == "__main__":
    unittest.main()
