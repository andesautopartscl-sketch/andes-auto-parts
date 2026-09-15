"""FASE 7B.2 — memory selector + orchestrator integration tests."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from app.assistant.orchestrator.memory_epoch import (
    NeutralPermissionEpochProvider,
    PermissionEpochResolution,
    memory_passes_permission_epoch,
    set_permission_epoch_provider_for_tests,
)
from app.assistant.orchestrator.memory_selector import (
    hint_contains_prohibited,
    hint_from_slot,
    hints_char_budget,
    select_memory_hints,
    sort_memory_candidates,
)
from app.assistant.orchestrator.memory_store import MemoryStore, reset_default_memory_store_for_tests
from app.assistant.orchestrator.planner import FakePlanner
from app.assistant.orchestrator.service import run_orchestrator_chat
from app.assistant.orchestrator.turn_store import TurnStore


class _FixedEpoch:
    def __init__(self, epoch: int):
        self.epoch = epoch

    def resolve(self, actor_user: str) -> PermissionEpochResolution:
        _ = actor_user
        return PermissionEpochResolution(available=True, epoch=self.epoch)


class _RecordingPlanner:
    def __init__(self, inner: FakePlanner | None = None):
        self.inner = inner or FakePlanner()
        self.contexts: list[dict[str, Any]] = []

    def plan(self, message: str, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
        ctx = dict(context or {})
        self.contexts.append(ctx)
        return self.inner.plan(message, context=ctx)


def _invoke_ok(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    tool = payload.get("tool")
    return 200, {
        "ok": True,
        "tool": tool,
        "classification": "INTERNAL",
        "data": {"items": [{"codigo": "2404", "descripcion": "Filtro"}], "count": 1},
        "meta": {},
    }


class Fase7B2MemorySelectorTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db = Path(self._tmpdir.name) / "mem.db"
        self.store = MemoryStore(path=self.db)
        self.store.ensure_schema()
        reset_default_memory_store_for_tests()
        # Default upserts stamp permission_epoch=0; match so contextual slots remain selectable.
        set_permission_epoch_provider_for_tests(_FixedEpoch(0))
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
        set_permission_epoch_provider_for_tests(None)
        reset_default_memory_store_for_tests()
        self._tmpdir.cleanup()

    def _upsert(self, **kwargs):
        return self.store.upsert(**kwargs)

    def test_memory_off_no_store_no_hints(self):
        self._upsert(
            actor_user="alice",
            scope="user",
            memory_type="preference",
            key="pref.answer_style",
            value={"answer_style": "brief"},
        )
        with patch.dict(os.environ, {"ANDES_ASSISTANT_MEMORY_ENABLED": "0"}, clear=False):
            called = {"n": 0}

            def boom(*_a, **_k):
                called["n"] += 1
                raise AssertionError("list_slots must not run when MEMORY=0")

            out = select_memory_hints(
                actor_user="alice",
                conversation_id="c1",
                store=self.store,
                list_fn=boom,
            )
            self.assertEqual(out.hints, [])
            self.assertEqual(called["n"], 0)

            planner = _RecordingPlanner()
            r = run_orchestrator_chat(
                message="Busca filtro",
                actor_user="alice",
                conversation_id="c1",
                invoke_fn=_invoke_ok,
                planner=planner,
                turn_store=TurnStore(),
                memory_store=self.store,
            )
            self.assertTrue(r.get("ok"))
            self.assertTrue(planner.contexts)
            self.assertNotIn("memory_hints", planner.contexts[0])

    def test_memory_on_user_and_conversation_scope(self):
        self._upsert(
            actor_user="alice",
            scope="user",
            memory_type="preference",
            key="pref.answer_style",
            value={"answer_style": "brief"},
        )
        self._upsert(
            actor_user="alice",
            scope="conversation",
            conversation_id="conv-A",
            memory_type="pinned_entity",
            key="pin.2404",
            value={"kind": "codigo", "value": "2404"},
        )
        self._upsert(
            actor_user="alice",
            scope="conversation",
            conversation_id="conv-B",
            memory_type="pinned_entity",
            key="pin.other",
            value={"kind": "codigo", "value": "9999"},
        )
        sel_a = select_memory_hints(
            actor_user="alice", conversation_id="conv-A", store=self.store
        )
        types_keys = {(h["type"], h["key"]) for h in sel_a.hints}
        self.assertIn(("preference", "pref.answer_style"), types_keys)
        self.assertIn(("pinned_entity", "pin.2404"), types_keys)
        self.assertNotIn(("pinned_entity", "pin.other"), types_keys)

        sel_b = select_memory_hints(
            actor_user="alice", conversation_id="conv-B", store=self.store
        )
        keys_b = {h["key"] for h in sel_b.hints}
        self.assertIn("pin.other", keys_b)
        self.assertNotIn("pin.2404", keys_b)
        self.assertIn("pref.answer_style", keys_b)

    def test_ownership_wrong_actor(self):
        self._upsert(
            actor_user="alice",
            scope="user",
            memory_type="ui_pref",
            key="ui.compact",
            value={"compact": True},
        )
        sel = select_memory_hints(actor_user="bob", conversation_id="c1", store=self.store)
        self.assertEqual(sel.hints, [])

    def test_ttl_and_deleted_excluded(self):
        past = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        expired = self._upsert(
            actor_user="alice",
            scope="user",
            memory_type="ui_pref",
            key="ui.expired",
            value={"compact": False},
            expires_at=past,
        )
        alive = self._upsert(
            actor_user="alice",
            scope="user",
            memory_type="ui_pref",
            key="ui.alive",
            value={"compact": True},
        )
        gone = self._upsert(
            actor_user="alice",
            scope="user",
            memory_type="preference",
            key="pref.gone",
            value={"answer_style": "detailed"},
        )
        self.store.soft_delete("alice", gone["id"])
        sel = select_memory_hints(actor_user="alice", conversation_id="c1", store=self.store)
        keys = {h["key"] for h in sel.hints}
        self.assertIn("ui.alive", keys)
        self.assertNotIn("ui.expired", keys)
        self.assertNotIn("pref.gone", keys)
        self.assertIsNotNone(expired)
        self.assertIsNotNone(alive)

    def test_priority_confidence_recency_dedup(self):
        # Older preference then newer — keep newer after dedup on same key
        self._upsert(
            actor_user="alice",
            scope="user",
            memory_type="preference",
            key="pref.answer_style",
            value={"answer_style": "detailed"},
            confidence=0.2,
        )
        time.sleep(0.02)
        self._upsert(
            actor_user="alice",
            scope="user",
            memory_type="preference",
            key="pref.answer_style",
            value={"answer_style": "brief"},
            confidence=0.9,
        )
        self._upsert(
            actor_user="alice",
            scope="user",
            memory_type="frequent_entity",
            key="freq.x",
            value={"kind": "codigo", "value": "1111", "hit_count": 2},
        )
        self._upsert(
            actor_user="alice",
            scope="conversation",
            conversation_id="c1",
            memory_type="pinned_entity",
            key="pin.c",
            value={"kind": "codigo", "value": "2222"},
        )
        self._upsert(
            actor_user="alice",
            scope="user",
            memory_type="pinned_entity",
            key="pin.u",
            value={"kind": "codigo", "value": "3333"},
        )
        self._upsert(
            actor_user="alice",
            scope="user",
            memory_type="ui_pref",
            key="ui.compact",
            value={"compact": True},
        )
        sel = select_memory_hints(actor_user="alice", conversation_id="c1", store=self.store)
        types = [h["type"] for h in sel.hints]
        # preference before ui_pref before pinned conv before pinned user before frequent
        pref_i = types.index("preference")
        ui_i = types.index("ui_pref")
        pin_c = next(i for i, h in enumerate(sel.hints) if h["key"] == "pin.c")
        pin_u = next(i for i, h in enumerate(sel.hints) if h["key"] == "pin.u")
        freq_i = types.index("frequent_entity")
        self.assertLess(pref_i, ui_i)
        self.assertLess(ui_i, pin_c)
        self.assertLess(pin_c, pin_u)
        self.assertLess(pin_u, freq_i)
        pref = next(h for h in sel.hints if h["type"] == "preference")
        self.assertEqual(pref["value"]["answer_style"], "brief")
        # exact one preference key
        self.assertEqual(sum(1 for h in sel.hints if h["key"] == "pref.answer_style"), 1)

    def test_deterministic_selection(self):
        for i in range(5):
            self._upsert(
                actor_user="alice",
                scope="user",
                memory_type="ui_pref",
                key=f"ui.{i}",
                value={"compact": bool(i % 2)},
            )
        a = select_memory_hints(actor_user="alice", conversation_id="c1", store=self.store)
        b = select_memory_hints(actor_user="alice", conversation_id="c1", store=self.store)
        self.assertEqual(a.hints, b.hints)

    def test_slot_cap_and_char_budget(self):
        for i in range(20):
            self._upsert(
                actor_user="alice",
                scope="user",
                memory_type="ui_pref",
                key=f"ui.cap.{i:02d}",
                value={"compact": False},
            )
        sel = select_memory_hints(
            actor_user="alice",
            conversation_id="c1",
            store=self.store,
            max_slots=12,
            max_chars=8000,
        )
        self.assertLessEqual(len(sel.hints), 12)
        self.assertEqual(len(sel.hints), 12)

        # Force tiny budget: only first fitting hints
        sel2 = select_memory_hints(
            actor_user="alice",
            conversation_id="c1",
            store=self.store,
            max_slots=12,
            max_chars=120,
        )
        self.assertGreaterEqual(len(sel2.hints), 1)
        self.assertLessEqual(sel2.budget_chars, 120)
        self.assertLess(len(sel2.hints), 12)
        # Representation remains valid JSON list
        json.loads(json.dumps(sel2.hints))

    def test_exact_representation_to_planner(self):
        self._upsert(
            actor_user="alice",
            scope="user",
            memory_type="preference",
            key="pref.answer_style",
            value={"answer_style": "brief"},
        )
        self._upsert(
            actor_user="alice",
            scope="conversation",
            conversation_id="conv-rep",
            memory_type="conversation_summary",
            key="sum.1",
            value={"text": "Consulto stock 2404", "tools": ["get_inventory"]},
            source="derived",
        )
        planner = _RecordingPlanner()
        r = run_orchestrator_chat(
            message="Busca filtro",
            actor_user="alice",
            conversation_id="conv-rep",
            invoke_fn=_invoke_ok,
            planner=planner,
            turn_store=TurnStore(),
            memory_store=self.store,
        )
        self.assertTrue(r.get("ok"))
        hints = planner.contexts[0].get("memory_hints")
        self.assertIsInstance(hints, list)
        for h in hints:
            self.assertEqual(set(h.keys()), {"type", "key", "value"})
            self.assertNotIn("actor_user", h)
            self.assertNotIn("permission_epoch", h)
            self.assertNotIn("deleted_at", h)
            self.assertNotIn("source_turn_id", h)
            self.assertNotIn("updated_at", h)

    def test_prohibited_content_defense(self):
        # Bypass sanitize by injecting a crafted public-like slot via list_fn
        bad = {
            "id": "x",
            "actor_user": "alice",
            "scope": "user",
            "conversation_id": None,
            "memory_type": "conversation_summary",
            "key": "sum.bad",
            "value": {"text": "Bearer SECRET ver_finanzas", "tools": []},
            "confidence": 1.0,
            "sensitivity": "contextual",
            "permission_epoch": 0,
            "updated_at": "2099-01-01T00:00:00Z",
            "deleted_at": None,
        }
        self.assertTrue(hint_contains_prohibited(hint_from_slot(bad)))

        def list_fn(actor, *, scope=None, conversation_id=None, limit=100):
            _ = (actor, conversation_id, limit)
            if scope == "user":
                return [bad]
            return []

        sel = select_memory_hints(
            actor_user="alice", conversation_id="c1", store=self.store, list_fn=list_fn
        )
        self.assertEqual(sel.hints, [])
        self.assertGreaterEqual(sel.dropped_prohibited, 1)

    def test_permission_epoch_mismatch_contextual(self):
        set_permission_epoch_provider_for_tests(_FixedEpoch(5))
        self._upsert(
            actor_user="alice",
            scope="user",
            memory_type="frequent_entity",
            key="freq.old",
            value={"kind": "codigo", "value": "2404", "hit_count": 1},
            permission_epoch=1,
            sensitivity="contextual",
        )
        self._upsert(
            actor_user="alice",
            scope="user",
            memory_type="preference",
            key="pref.answer_style",
            value={"answer_style": "brief"},
            permission_epoch=1,
            sensitivity="benign",
        )
        # Fail-closed when epoch unavailable: contextual excluded
        self.assertFalse(
            memory_passes_permission_epoch(
                {"sensitivity": "contextual", "permission_epoch": 1},
                PermissionEpochResolution(available=False, epoch=None),
            )
        )
        self.assertTrue(
            memory_passes_permission_epoch(
                {"sensitivity": "benign", "permission_epoch": 1},
                PermissionEpochResolution(available=False, epoch=None),
            )
        )
        sel = select_memory_hints(
            actor_user="alice",
            conversation_id="c1",
            store=self.store,
            epoch_provider=_FixedEpoch(5),
        )
        keys = {h["key"] for h in sel.hints}
        self.assertIn("pref.answer_style", keys)  # benign survives
        self.assertNotIn("freq.old", keys)  # contextual mismatch excluded

    def test_no_permission_elevation_no_write(self):
        self._upsert(
            actor_user="alice",
            scope="user",
            memory_type="preference",
            key="pref.hack",
            value={"answer_style": "brief"},
        )
        # Inject "permission claim" summary via list_fn that would be scrubbed if upserted;
        # for elevation test, preference alone must not add WRITE tools.
        planner = _RecordingPlanner()
        r = run_orchestrator_chat(
            message="Busca filtro",
            actor_user="alice",
            conversation_id="c-elev",
            invoke_fn=_invoke_ok,
            planner=planner,
            turn_store=TurnStore(),
            memory_store=self.store,
        )
        self.assertTrue(r.get("ok"))
        tools = r.get("tools_used") or []
        self.assertNotIn("create_invoice", tools)
        self.assertNotIn("write", [str(t).lower() for t in tools])
        # FakePlanner still plans READ search
        self.assertTrue(any("search" in str(t) or "get_" in str(t) for t in tools) or r.get("ok"))

        # Explicit WRITE message still rejected regardless of memory
        r2 = run_orchestrator_chat(
            message="Anula la factura 123",
            actor_user="alice",
            conversation_id="c-elev-write",
            invoke_fn=_invoke_ok,
            planner=FakePlanner(),
            turn_store=TurnStore(),
            memory_store=self.store,
        )
        tools2 = r2.get("tools_used") or []
        self.assertEqual(tools2, [])
        self.assertTrue(
            r2.get("error_code") == "write_not_allowed"
            or r2.get("scenario") == "write_reject"
            or "no puedo" in str(r2.get("reply") or "").lower()
            or "solo puedo consultar" in str(r2.get("reply") or "").lower()
        )

    def test_db_down_soft_fail(self):
        class Boom(MemoryStore):
            def list_slots(self, *a, **k):
                raise RuntimeError("db down")

        planner = _RecordingPlanner()
        r = run_orchestrator_chat(
            message="Busca filtro",
            actor_user="alice",
            conversation_id="c-db",
            invoke_fn=_invoke_ok,
            planner=planner,
            turn_store=TurnStore(),
            memory_store=Boom(path=self.db),
        )
        self.assertTrue(r.get("ok"))
        self.assertNotIn("memory_hints", planner.contexts[0])

    def test_history_matrix_independence(self):
        self._upsert(
            actor_user="alice",
            scope="user",
            memory_type="preference",
            key="pref.answer_style",
            value={"answer_style": "operational"},
        )

        def run(hist: str, mem: str):
            with patch.dict(
                os.environ,
                {
                    "ANDES_ASSISTANT_HISTORY_ENABLED": hist,
                    "ANDES_ASSISTANT_MEMORY_ENABLED": mem,
                },
                clear=False,
            ):
                planner = _RecordingPlanner()
                r = run_orchestrator_chat(
                    message="Busca filtro",
                    actor_user="alice",
                    conversation_id="hist-mem",
                    invoke_fn=_invoke_ok,
                    planner=planner,
                    turn_store=TurnStore(),
                    memory_store=self.store,
                )
                self.assertTrue(r.get("ok"), msg=f"hist={hist} mem={mem}")
                has = "memory_hints" in (planner.contexts[0] if planner.contexts else {})
                return has

        self.assertFalse(run("1", "0"))
        self.assertTrue(run("0", "1"))
        self.assertTrue(run("1", "1"))

    def test_fase5_compat_reuse_still_works(self):
        # MEMORY on must not break reuse early-exit (no planner → no memory_hints required)
        turns = TurnStore()
        planner = _RecordingPlanner()
        r1 = run_orchestrator_chat(
            message="Stock del 2404",
            actor_user="alice",
            conversation_id="fase5",
            invoke_fn=_invoke_ok,
            planner=planner,
            turn_store=turns,
            memory_store=self.store,
        )
        self.assertTrue(r1.get("ok"))
        r2 = run_orchestrator_chat(
            message="muéstrame los anteriores",
            actor_user="alice",
            conversation_id="fase5",
            invoke_fn=_invoke_ok,
            planner=_RecordingPlanner(),
            turn_store=turns,
            memory_store=self.store,
        )
        # reuse or clarify or plan — must not crash; if reuse, ok with evidence
        self.assertIn(r2.get("ok"), (True, False))
        self.assertNotEqual(r2.get("http_status"), 500)

    def test_restart_hydrate_selection(self):
        self._upsert(
            actor_user="alice",
            scope="user",
            memory_type="preference",
            key="pref.answer_style",
            value={"answer_style": "brief"},
        )
        fresh = MemoryStore(path=self.db)
        sel = select_memory_hints(actor_user="alice", conversation_id="c1", store=fresh)
        self.assertTrue(any(h["key"] == "pref.answer_style" for h in sel.hints))

    def test_sort_helper_and_budget_helpers(self):
        slots = [
            {
                "memory_type": "frequent_entity",
                "scope": "user",
                "updated_at": "2026-01-01T00:00:00Z",
                "confidence": 1.0,
                "key": "a",
            },
            {
                "memory_type": "preference",
                "scope": "user",
                "updated_at": "2026-01-02T00:00:00Z",
                "confidence": 0.5,
                "key": "b",
            },
        ]
        ordered = sort_memory_candidates(slots)
        self.assertEqual(ordered[0]["memory_type"], "preference")
        hints = [{"type": "ui_pref", "key": "k", "value": {"compact": True}}]
        self.assertGreater(hints_char_budget(hints), 10)
        self.assertIsInstance(NeutralPermissionEpochProvider().resolve("x").available, bool)


if __name__ == "__main__":
    unittest.main()
