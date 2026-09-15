"""FASE 7A — persistent conversation history (no 7B memory)."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app.assistant.orchestrator.audit import OrchestratorAudit
from app.assistant.orchestrator.history_store import (
    HistoryStore,
    sanitize_turn_for_history,
)
from app.assistant.orchestrator.planner import FakePlanner
from app.assistant.orchestrator.service import run_orchestrator_chat
from app.assistant.orchestrator.turn_store import TurnStore


def _inv_ok(codigo: str = "2404"):
    return (
        200,
        {
            "ok": True,
            "tool": "get_inventory",
            "classification": "INTERNAL",
            "write": False,
            "data": {
                "codigo": codigo,
                "total_stock": 2,
                "items": [{"bodega": "B1", "stock": 2}],
                "email": "secret@example.com",
                "rut": "12.345.678-9",
                "monto": 99999,
                "Authorization": "Bearer sk-secret",
            },
            "meta": {"api_key": "should-not-persist"},
        },
    )


class Fase7AHistoryStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db = Path(self._tmpdir.name) / "hist.db"
        self.store = HistoryStore(path=self.db)
        self.store.ensure_schema()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_create_conversation_server_id(self):
        conv = self.store.ensure_conversation("alice", client_conversation_id="conv-client-1")
        self.assertIsNotNone(conv)
        self.assertTrue(conv["id"])
        self.assertNotEqual(conv["id"], "conv-client-1")
        self.assertEqual(conv["client_conversation_id"], "conv-client-1")
        self.assertEqual(conv["actor_user"], "alice")
        again = self.store.ensure_conversation("alice", client_conversation_id="conv-client-1")
        self.assertEqual(again["id"], conv["id"])

    def test_append_and_retrieve_turns(self):
        conv = self.store.ensure_conversation("alice")
        cid = conv["id"]
        s1 = self.store.append_turn(
            "alice",
            cid,
            {
                "message_hash": "abc1",
                "tools_used": ["get_inventory"],
                "scenario": "inventory_only",
                "entities": {"codigo": "2404"},
                "evidence": [{"tool": "get_inventory", "ok": True, "data": {"codigo": "2404"}}],
                "reply_excerpt": "Stock 2",
                "classification": "INTERNAL",
            },
        )
        s2 = self.store.append_turn(
            "alice",
            cid,
            {
                "message_hash": "abc2",
                "tools_used": ["get_stock_movements"],
                "scenario": "movements_only",
                "entities": {"codigo": "2404"},
                "evidence": [],
                "reply_excerpt": "Movimientos",
            },
        )
        self.assertEqual(s1, 1)
        self.assertEqual(s2, 2)
        got = self.store.get_conversation("alice", cid)
        self.assertEqual(got["turn_count"], 2)
        turns = self.store.list_turns("alice", cid, limit=10)
        self.assertEqual(len(turns["items"]), 2)
        self.assertEqual(turns["items"][0]["seq"], 1)
        self.assertEqual(turns["items"][1]["seq"], 2)

    def test_pagination_cursor_conversations_and_turns(self):
        ids = []
        for i in range(5):
            c = self.store.ensure_conversation("alice", client_conversation_id=f"c-{i}")
            ids.append(c["id"])
            self.store.append_turn(
                "alice",
                c["id"],
                {"message_hash": f"h{i}", "tools_used": [], "reply_excerpt": f"r{i}"},
            )
        page1 = self.store.list_conversations("alice", limit=2)
        self.assertEqual(len(page1["items"]), 2)
        self.assertIsNotNone(page1["next_cursor"])
        page2 = self.store.list_conversations("alice", limit=2, cursor=page1["next_cursor"])
        self.assertEqual(len(page2["items"]), 2)
        # turns pagination
        cid = ids[0]
        for i in range(5):
            self.store.append_turn(
                "alice", cid, {"message_hash": f"t{i}", "tools_used": [], "reply_excerpt": "x"}
            )
        tpage = self.store.list_turns("alice", cid, limit=2)
        self.assertEqual(len(tpage["items"]), 2)
        self.assertIsNotNone(tpage["next_before_seq"])
        older = self.store.list_turns("alice", cid, limit=2, before_seq=tpage["next_before_seq"])
        self.assertTrue(len(older["items"]) >= 1)

    def test_ownership_isolation(self):
        conv = self.store.ensure_conversation("alice", client_conversation_id="shared-name")
        self.store.append_turn(
            "alice", conv["id"], {"message_hash": "h", "tools_used": ["get_inventory"], "reply_excerpt": "ok"}
        )
        self.assertIsNone(self.store.get_conversation("bob", conv["id"]))
        bob_list = self.store.list_conversations("bob")
        self.assertEqual(bob_list["items"], [])
        turns = self.store.list_turns("bob", conv["id"])
        self.assertEqual(turns.get("error_code"), "not_found")
        self.assertFalse(self.store.soft_delete_conversation("bob", conv["id"]))
        self.assertIsNotNone(self.store.get_conversation("alice", conv["id"]))

    def test_soft_delete(self):
        conv = self.store.ensure_conversation("alice")
        cid = conv["id"]
        self.store.append_turn("alice", cid, {"message_hash": "h", "reply_excerpt": "x"})
        self.assertTrue(self.store.soft_delete_conversation("alice", cid))
        self.assertIsNone(self.store.get_conversation("alice", cid))
        listed = self.store.list_conversations("alice")
        self.assertEqual(listed["items"], [])

    def test_retention_purge(self):
        conv = self.store.ensure_conversation("alice")
        cid = conv["id"]
        self.store.append_turn("alice", cid, {"message_hash": "h", "reply_excerpt": "x"})
        # Force retention_until in the past
        import sqlite3

        past = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = sqlite3.connect(str(self.db))
        conn.execute(
            "UPDATE assistant_conversation SET retention_until = ? WHERE id = ?",
            (past, cid),
        )
        conn.commit()
        conn.close()
        result = self.store.purge_expired()
        self.assertGreaterEqual(result["conversations"], 1)
        self.assertIsNone(self.store.get_conversation("alice", cid))

    def test_soft_delete_grace_purge(self):
        conv = self.store.ensure_conversation("alice")
        cid = conv["id"]
        self.store.soft_delete_conversation("alice", cid)
        import sqlite3

        old = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = sqlite3.connect(str(self.db))
        conn.execute(
            "UPDATE assistant_conversation SET deleted_at = ? WHERE id = ?",
            (old, cid),
        )
        conn.commit()
        conn.close()
        with patch(
            "app.assistant.orchestrator.history_store.history_soft_delete_grace_days",
            return_value=7,
        ):
            result = self.store.purge_expired()
        self.assertGreaterEqual(result["conversations"], 1)

    def test_seq_concurrent(self):
        conv = self.store.ensure_conversation("alice")
        cid = conv["id"]
        seqs: list[int] = []
        lock = threading.Lock()

        def worker():
            s = self.store.append_turn(
                "alice", cid, {"message_hash": "h", "tools_used": [], "reply_excerpt": "x"}
            )
            with lock:
                if s is not None:
                    seqs.append(s)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(seqs), 8)
        self.assertEqual(sorted(seqs), list(range(1, 9)))

    def test_sanitize_strips_pii_and_secrets(self):
        safe = sanitize_turn_for_history(
            {
                "message_hash": "mh",
                "message_excerpt": "should stay only if enabled",
                "reply_excerpt": "Stock ok email leak@x.com",
                "tools_used": ["get_inventory"],
                "entities": {"codigo": "2404", "rut": "1-9", "email": "a@b.com"},
                "evidence": [
                    {
                        "tool": "get_inventory",
                        "ok": True,
                        "data": {
                            "codigo": "2404",
                            "rut": "12.345.678-9",
                            "email": "x@y.com",
                            "monto": 5000,
                            "Authorization": "Bearer secret",
                            "api_key": "sk-live",
                        },
                        "meta": {"cookie": "sid=1", "password": "x"},
                    }
                ],
                "flags": {"prompt": "SYSTEM FULL", "ok": True},
            }
        )
        blob = json.dumps(safe)
        self.assertIsNone(safe.get("message_excerpt"))  # default OFF
        self.assertNotIn("12.345.678-9", blob)
        self.assertNotIn("x@y.com", blob)
        self.assertNotIn("sk-live", blob)
        self.assertNotIn("Bearer secret", blob)
        self.assertNotIn("sid=1", blob)
        self.assertNotIn("5000", blob)
        self.assertNotIn("SYSTEM FULL", blob)
        self.assertEqual(safe["entities"].get("codigo"), "2404")
        self.assertNotIn("rut", safe["entities"])
        self.assertIn("[redacted]", safe["reply_excerpt"])


class Fase7AOrchestratorHistoryTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db = Path(self._tmpdir.name) / "orch.db"
        self.history = HistoryStore(path=self.db)
        self.history.ensure_schema()
        self.turns = TurnStore()
        self.audit = OrchestratorAudit(path=Path(self._tmpdir.name) / "a.jsonl")
        self.planner = FakePlanner()
        self._env = patch.dict(
            os.environ,
            {
                "ANDES_ASSISTANT_HISTORY_ENABLED": "1",
                "ANDES_ASSISTANT_NL_ENABLED": "0",
                "ANDES_ORCH_PLANNER": "fake",
            },
            clear=False,
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmpdir.cleanup()

    def _chat(self, message: str, *, conversation_id: str = "", history=None, turn_store=None):
        return run_orchestrator_chat(
            message=message,
            actor_user="alice",
            conversation_id=conversation_id,
            invoke_fn=lambda p: _inv_ok(str((p.get("arguments") or {}).get("codigo") or "2404")),
            planner=self.planner,
            audit=self.audit,
            turn_store=turn_store if turn_store is not None else self.turns,
            history_store=history if history is not None else self.history,
            force_scenario="inventory_only",
        )

    def test_flag_on_persists_and_returns_server_conversation_id(self):
        r = self._chat("stock 2404", conversation_id="conv-widget-1")
        self.assertTrue(r["ok"])
        cid = r["conversation_id"]
        self.assertTrue(cid)
        self.assertNotEqual(cid, "conv-widget-1")
        conv = self.history.get_conversation("alice", cid)
        self.assertIsNotNone(conv)
        self.assertEqual(conv["client_conversation_id"], "conv-widget-1")
        self.assertGreaterEqual(conv["turn_count"], 1)
        turns = self.history.list_turns("alice", cid)
        self.assertGreaterEqual(len(turns["items"]), 1)
        blob = json.dumps(turns)
        self.assertNotIn("secret@example.com", blob)
        self.assertNotIn("12.345.678-9", blob)
        self.assertNotIn("sk-secret", blob)
        self.assertNotIn("99999", blob)

    def test_flag_off_behavior_no_persist(self):
        with patch.dict(os.environ, {"ANDES_ASSISTANT_HISTORY_ENABLED": "0"}, clear=False):
            r = self._chat("stock 2404", conversation_id="conv-off-1")
            self.assertTrue(r["ok"])
            self.assertEqual(r["conversation_id"], "conv-off-1")
        # Nothing persisted for that client id as server row with turns under alice
        listed = self.history.list_conversations("alice")
        self.assertEqual(listed["items"], [])

    def test_hydrate_after_restart(self):
        r1 = self._chat("stock 2404", conversation_id="conv-hyd")
        cid = r1["conversation_id"]
        # New RAM store + new HistoryStore handle (same DB) simulates ERP restart
        fresh_turns = TurnStore()
        fresh_history = HistoryStore(path=self.db)
        r2 = run_orchestrator_chat(
            message="¿Y cuánto queda?",
            actor_user="alice",
            conversation_id=cid,
            invoke_fn=lambda p: _inv_ok(),
            planner=self.planner,
            audit=self.audit,
            turn_store=fresh_turns,
            history_store=fresh_history,
        )
        self.assertTrue(r2["ok"])
        self.assertTrue(r2.get("reuse_prior_evidence"))
        self.assertEqual(r2["conversation_id"], cid)

    def test_db_failure_chat_continues(self):
        class Boom(HistoryStore):
            def ensure_conversation(self, *a, **k):
                raise RuntimeError("db down")

            def append_turn(self, *a, **k):
                raise RuntimeError("db down")

            def get_conversation(self, *a, **k):
                raise RuntimeError("db down")

            def recent_turns_for_resolver(self, *a, **k):
                raise RuntimeError("db down")

        r = self._chat("stock 2404", conversation_id="conv-boom", history=Boom(path=self.db))
        self.assertTrue(r["ok"])
        self.assertIn("reply", r)

    def test_user_b_cannot_read_user_a(self):
        r = self._chat("stock 2404", conversation_id="conv-own")
        cid = r["conversation_id"]
        self.assertIsNone(self.history.get_conversation("bob", cid))
        turns = self.history.list_turns("bob", cid)
        self.assertEqual(turns.get("error_code"), "not_found")

    def test_fase5_compat_reuse_still_works_with_history(self):
        r1 = self._chat("¿Cuánto stock del 2404?")
        self.assertEqual(r1["tools_used"], ["get_inventory"])
        r2 = self._chat("¿Y cuánto queda?", conversation_id=r1["conversation_id"])
        self.assertTrue(r2.get("reuse_prior_evidence"))

    def test_no_secrets_in_sqlite_bytes(self):
        r = self._chat("stock 2404", conversation_id="conv-sec")
        self.assertTrue(r["ok"])
        raw = self.db.read_bytes()
        self.assertNotIn(b"sk-secret", raw)
        self.assertNotIn(b"secret@example.com", raw)
        self.assertNotIn(b"12.345.678-9", raw)
        self.assertNotIn(b"should-not-persist", raw)


if __name__ == "__main__":
    unittest.main()
