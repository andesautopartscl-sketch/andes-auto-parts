"""FASE 5 — conversational context and reference resolution (deterministic)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.assistant.orchestrator.audit import OrchestratorAudit
from app.assistant.orchestrator.conversation_context import (
    ConversationResolver,
    build_redacted_summary,
    extract_entities_from_evidence,
)
from app.assistant.orchestrator.llm.prompts import build_user_prompt
from app.assistant.orchestrator.planner import FakePlanner
from app.assistant.orchestrator.service import run_orchestrator_chat
from app.assistant.orchestrator.turn_store import TurnStore


def _inv_ok(codigo: str = "2404", total: int = 2):
    return (
        200,
        {
            "ok": True,
            "tool": "get_inventory",
            "classification": "INTERNAL",
            "write": False,
            "data": {
                "codigo": codigo,
                "total_stock": total,
                "items": [{"bodega": "Bodega 1", "stock": total, "marca": "BOSCH"}],
            },
            "meta": {},
        },
    )


def _mov_ok(codigo: str = "2404", with_items: bool = True):
    items = []
    if with_items:
        items = [
            {"fecha": "2026-09-10", "tipo": "INGRESO", "cantidad": 5, "bodega": "Bodega 1"},
            {"fecha": "2026-09-12", "tipo": "SALIDA", "cantidad": 3, "bodega": "Bodega 1"},
            {"fecha": "2026-09-13", "tipo": "SALIDA", "cantidad": 1, "bodega": "Bodega 1"},
        ]
    return (
        200,
        {
            "ok": True,
            "tool": "get_stock_movements",
            "classification": "INTERNAL",
            "write": False,
            "data": {"codigo": codigo, "count": len(items), "items": items},
            "meta": {},
        },
    )


def _catalog_ok():
    return (
        200,
        {
            "ok": True,
            "tool": "search_catalog",
            "classification": "INTERNAL",
            "write": False,
            "data": {
                "count": 2,
                "items": [
                    {
                        "codigo": "2404",
                        "descripcion": "FILTRO DIESEL",
                        "marca": "BOSCH",
                        "modelo": "T60",
                    },
                    {
                        "codigo": "2404PL",
                        "descripcion": "FILTRO DIESEL GEN",
                        "marca": "JAC",
                        "modelo": "SUNRAY",
                    },
                ],
            },
            "meta": {},
        },
    )


def _supplier_ok():
    return (
        200,
        {
            "ok": True,
            "tool": "get_supplier",
            "classification": "CONFIDENTIAL",
            "write": False,
            "data": {
                "count": 1,
                "items": [
                    {
                        "nombre": "ANDES SPA",
                        "empresa": "ANDES AUTO PARTS LTDA",
                        "rut": "76.000.000-0",
                        "ciudad": "SANTIAGO",
                        "comuna": "PROVIDENCIA",
                        "email": "secret@example.com",
                    }
                ],
            },
            "meta": {"q": "ANDES"},
        },
    )


def _oc_ok():
    return (
        200,
        {
            "ok": True,
            "tool": "get_purchase_orders",
            "classification": "INTERNAL",
            "write": False,
            "data": {"count": 1, "items": [{"numero": "OC-100", "estado": "abierta"}]},
            "meta": {},
        },
    )


class Fase5ContextTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.audit = OrchestratorAudit(path=Path(self._tmpdir.name) / "audit.jsonl")
        self.store = TurnStore()
        self.planner = FakePlanner()
        self.invokes: list[dict] = []

    def tearDown(self):
        self._tmpdir.cleanup()

    def _chat(self, message: str, *, conversation_id: str = "conv-fase5", invoke_map=None):
        invoke_map = invoke_map or {}

        def invoke_fn(payload: dict):
            self.invokes.append(payload)
            tool = payload.get("tool")
            if tool in invoke_map:
                return invoke_map[tool]
            if tool == "get_inventory":
                return _inv_ok(str((payload.get("arguments") or {}).get("codigo") or "2404"))
            if tool == "get_stock_movements":
                return _mov_ok(str((payload.get("arguments") or {}).get("codigo") or "2404"))
            if tool == "search_catalog":
                return _catalog_ok()
            if tool == "get_supplier":
                return _supplier_ok()
            if tool == "get_purchase_orders":
                return _oc_ok()
            if tool == "get_product":
                return (
                    200,
                    {
                        "ok": True,
                        "tool": "get_product",
                        "classification": "INTERNAL",
                        "write": False,
                        "data": {"codigo": "2404", "descripcion": "FILTRO", "marca": "BOSCH"},
                        "meta": {},
                    },
                )
            return (500, {"ok": False, "error_code": "agent_error", "message": f"unexpected {tool}"})

        return run_orchestrator_chat(
            message=message,
            actor_user="albertadmin",
            conversation_id=conversation_id,
            invoke_fn=invoke_fn,
            planner=self.planner,
            audit=self.audit,
            turn_store=self.store,
        )

    def test_01_entity_followup_movements_without_repeating_codigo(self):
        r1 = self._chat("¿Cuánto stock tenemos del 2404?")
        self.assertTrue(r1["ok"])
        self.assertEqual(r1["tools_used"], ["get_inventory"])
        n1 = len(self.invokes)

        r2 = self._chat("¿Y los últimos movimientos?")
        self.assertTrue(r2["ok"])
        self.assertEqual(r2["tools_used"], ["get_stock_movements"])
        self.assertEqual(len(self.invokes), n1 + 1)
        args = self.invokes[-1]["arguments"]
        self.assertEqual(str(args.get("codigo")).upper(), "2404")
        self.assertNotIn("needs_clarification", r2.get("needs_clarification") and {} or {})
        self.assertFalse(r2.get("needs_clarification"))

    def test_02_reuse_prior_movements_list(self):
        r1 = self._chat("¿Cuántos movimientos tiene el 2404?")
        self.assertTrue(r1["ok"])
        self.assertEqual(r1["tools_used"], ["get_stock_movements"])
        self.assertIn("registro", r1["reply"].lower())
        n1 = len(self.invokes)

        r2 = self._chat("Dime cuáles son.")
        self.assertTrue(r2["ok"])
        self.assertEqual(r2.get("tools_used"), [])
        self.assertTrue(r2.get("reuse_prior_evidence"))
        self.assertEqual(len(self.invokes), n1)  # no new gateway invokes
        self.assertIn("INGRESO", r2["reply"])
        self.assertIn("SALIDA", r2["reply"])

    def test_03_followup_without_repeating_code_assert_args(self):
        self._chat("Stock del 2404")
        self._chat("¿Y cuánto queda?")
        self.assertEqual(self.invokes[-1]["tool"], "get_inventory")
        self.assertEqual(str(self.invokes[-1]["arguments"].get("codigo")).upper(), "2404")

    def test_04_ambiguous_followup_needs_clarification(self):
        r = self._chat("Muéstrame los movimientos.")
        self.assertTrue(r["ok"])
        self.assertTrue(r.get("needs_clarification"))
        self.assertEqual(r.get("tools_used"), [])
        self.assertEqual(self.invokes, [])

    def test_05_empty_prior_evidence_clarifies(self):
        # Seed a turn with empty movements evidence
        self.store.append(
            "albertadmin",
            "conv-fase5",
            {
                "message_hash": "x",
                "tools_used": ["get_stock_movements"],
                "scenario": "movements_only",
                "entities": {"codigo": "2404", "codigos": ["2404"]},
                "evidence": [
                    {
                        "tool": "get_stock_movements",
                        "ok": True,
                        "empty": True,
                        "data": {"codigo": "2404", "count": 0, "items": []},
                        "meta": {},
                    }
                ],
                "reply_excerpt": "sin movimientos",
            },
        )
        r = self._chat("Dime cuáles son.")
        self.assertTrue(r["ok"])
        self.assertTrue(r.get("needs_clarification"))
        self.assertEqual(r.get("tools_used"), [])

    def test_06_permission_denied_on_followup(self):
        self._chat("¿Cuánto stock tenemos del 2404?")

        def invoke_fn(payload: dict):
            self.invokes.append(payload)
            if payload.get("tool") == "get_stock_movements":
                return (
                    403,
                    {
                        "ok": False,
                        "tool": "get_stock_movements",
                        "error_code": "permission_denied",
                        "message": "sin permiso",
                    },
                )
            return _inv_ok()

        r = run_orchestrator_chat(
            message="¿Y los últimos movimientos?",
            actor_user="albertadmin",
            conversation_id="conv-fase5",
            invoke_fn=invoke_fn,
            planner=self.planner,
            audit=self.audit,
            turn_store=self.store,
        )
        self.assertTrue(r["ok"])
        self.assertIn("permiso", r["reply"].lower())
        # Still invoked as same actor — context did not elevate
        self.assertEqual(self.invokes[-1].get("actor_user"), "albertadmin")

    def test_07_write_via_reference_rejected(self):
        self._chat("Muéstrame las órdenes de compra")
        n = len(self.invokes)
        r = self._chat("Anula esa OC")
        self.assertTrue(r["ok"])
        self.assertEqual(r.get("scenario"), "write_reject")
        self.assertEqual(r.get("tools_used"), [])
        self.assertEqual(len(self.invokes), n)
        self.assertIn("no puedo", r["reply"].lower())

    def test_08_no_pii_leak_from_context(self):
        # Seed supplier evidence that originally contained email (store must strip)
        self.store.append(
            "albertadmin",
            "conv-fase5",
            {
                "message_hash": "p",
                "tools_used": ["get_supplier"],
                "scenario": "supplier_search",
                "entities": {
                    "proveedor_nombre": "ANDES SPA",
                    "proveedor_empresa": "ANDES AUTO PARTS LTDA",
                    "proveedor_q": "ANDES",
                },
                "evidence": [
                    {
                        "tool": "get_supplier",
                        "ok": True,
                        "empty": False,
                        "data": {
                            "count": 1,
                            "items": [
                                {
                                    "nombre": "ANDES SPA",
                                    "empresa": "ANDES AUTO PARTS LTDA",
                                    "email": "leak@evil.test",
                                }
                            ],
                        },
                        "meta": {"q": "ANDES"},
                    }
                ],
                "reply_excerpt": "Proveedores: 1",
            },
        )
        turns = self.store.get("albertadmin", "conv-fase5")
        summary = build_redacted_summary(turns)
        self.assertNotIn("leak@evil.test", summary)
        self.assertNotIn("@", summary)

        prompt = build_user_prompt("¿Y qué empresa es?", conversation_context=summary)
        self.assertNotIn("leak@evil.test", prompt)

        r = self._chat("¿Y qué empresa es?")
        self.assertTrue(r["ok"])
        self.assertTrue(r.get("reuse_prior_evidence"))
        self.assertNotIn("leak@evil.test", r["reply"])
        self.assertNotIn("secret@example.com", r["reply"])
        self.assertIn("ANDES", r["reply"])

    def test_09_max_turns_and_ttl_and_empty_conversation_id(self):
        store = TurnStore(max_turns=3, ttl_seconds=10)
        for i in range(5):
            store.append(
                "u",
                "c1",
                {
                    "message_hash": str(i),
                    "tools_used": ["get_inventory"],
                    "entities": {"codigo": f"C{i}", "codigos": [f"C{i}"]},
                    "evidence": [],
                    "reply_excerpt": f"r{i}",
                },
                now=1000.0 + i,
            )
        turns = store.get("u", "c1", now=1004.0)
        self.assertEqual(len(turns), 3)
        self.assertEqual(turns[0]["entities"]["codigo"], "C2")

        # TTL expiry
        expired = store.get("u", "c1", now=1000.0 + 5 + 100)
        self.assertEqual(expired, [])

        # Empty conversation_id does not persist / cross turns
        store2 = TurnStore()
        store2.append(
            "albertadmin",
            "",
            {
                "tools_used": ["get_inventory"],
                "entities": {"codigo": "2404", "codigos": ["2404"]},
                "evidence": [
                    {
                        "tool": "get_inventory",
                        "ok": True,
                        "empty": False,
                        "data": {"codigo": "2404", "total_stock": 1, "items": []},
                        "meta": {},
                    }
                ],
                "reply_excerpt": "stock",
            },
        )
        self.assertEqual(store2.get("albertadmin", ""), [])
        r = self._chat("¿Y los últimos movimientos?", conversation_id="")
        self.assertTrue(r.get("needs_clarification"))

    def test_10_case3_supplier_company_from_prior_evidence(self):
        r1 = self._chat("Busca el proveedor ANDES.")
        self.assertTrue(r1["ok"])
        self.assertEqual(r1["tools_used"], ["get_supplier"])
        n = len(self.invokes)
        r2 = self._chat("¿Y qué empresa es?")
        self.assertTrue(r2["ok"])
        self.assertTrue(r2.get("reuse_prior_evidence"))
        self.assertEqual(len(self.invokes), n)
        self.assertIn("ANDES AUTO PARTS", r2["reply"])

    def test_11_case4_first_item_stock(self):
        r1 = self._chat("Busca filtro diesel.")
        self.assertTrue(r1["ok"])
        self.assertEqual(r1["tools_used"], ["search_catalog"])
        r2 = self._chat("El primero, ¿cuánto stock tiene?")
        self.assertTrue(r2["ok"])
        self.assertEqual(r2["tools_used"], ["get_inventory"])
        self.assertEqual(str(self.invokes[-1]["arguments"].get("codigo")).upper(), "2404")

    def test_extract_entities_and_resolver_unit(self):
        evidence = [
            {
                "tool": "search_catalog",
                "ok": True,
                "empty": False,
                "data": {
                    "items": [
                        {"codigo": "A1", "descripcion": "x"},
                        {"codigo": "B2", "descripcion": "y"},
                    ],
                    "count": 2,
                },
                "meta": {},
            }
        ]
        ents = extract_entities_from_evidence(evidence)
        self.assertEqual(ents["codigo"], "A1")
        self.assertEqual(ents["codigos"], ["A1", "B2"])

        store_turns = [
            {
                "tools_used": ["search_catalog"],
                "entities": ents,
                "evidence": evidence,
                "reply_excerpt": "cat",
            }
        ]
        resolved = ConversationResolver().resolve("El primero, ¿cuánto stock tiene?", store_turns)
        self.assertEqual(resolved.kind, "plan_hints")
        self.assertEqual(resolved.intent_hint, "inventory")
        self.assertEqual(resolved.entities.get("codigo"), "A1")

    def test_12_stock_movements_detail_then_remaining_stock(self):
        """stock → movimientos → detalle → stock restante (reuse inventory)."""
        r1 = self._chat("¿Cuánto stock tenemos del 2404?")
        self.assertEqual(r1["tools_used"], ["get_inventory"])
        r2 = self._chat("¿Y los últimos movimientos?")
        self.assertEqual(r2["tools_used"], ["get_stock_movements"])
        n = len(self.invokes)
        r3 = self._chat("Dime cuáles son.")
        self.assertTrue(r3.get("reuse_prior_evidence"))
        self.assertEqual(len(self.invokes), n)
        r4 = self._chat("¿Y cuánto queda?")
        self.assertTrue(r4["ok"])
        self.assertTrue(r4.get("reuse_prior_evidence"))
        self.assertEqual(r4.get("tools_used"), [])
        self.assertEqual(len(self.invokes), n)  # no new invoke
        self.assertIn("2404", r4["reply"])
        self.assertIn("2", r4["reply"])
        self.assertFalse(r4.get("needs_clarification"))

    def test_13_stock_movements_then_bodega(self):
        self._chat("¿Cuánto stock tenemos del 2404?")
        self._chat("¿Y los últimos movimientos?")
        n = len(self.invokes)
        r = self._chat("¿Y en qué bodega?")
        self.assertTrue(r.get("reuse_prior_evidence"))
        self.assertEqual(len(self.invokes), n)
        self.assertIn("Bodega 1", r["reply"])
        self.assertFalse(r.get("needs_clarification"))

    def test_14_supplier_empresa_ciudad(self):
        self._chat("Busca el proveedor ANDES.")
        n = len(self.invokes)
        r2 = self._chat("¿Y qué empresa es?")
        self.assertTrue(r2.get("reuse_prior_evidence"))
        self.assertIn("ANDES AUTO PARTS", r2["reply"])
        r3 = self._chat("¿Y de qué ciudad es?")
        self.assertTrue(r3.get("reuse_prior_evidence"))
        self.assertEqual(len(self.invokes), n)
        self.assertIn("SANTIAGO", r3["reply"])

    def test_15_product_stock_movements_chain(self):
        r1 = self._chat("Qué es el producto 2404?")
        self.assertEqual(r1["tools_used"], ["get_product"])
        r2 = self._chat("¿Y cuánto stock tiene?")
        self.assertEqual(r2["tools_used"], ["get_inventory"])
        self.assertEqual(str(self.invokes[-1]["arguments"].get("codigo")).upper(), "2404")
        r3 = self._chat("¿Y los últimos movimientos?")
        self.assertEqual(r3["tools_used"], ["get_stock_movements"])
        self.assertEqual(str(self.invokes[-1]["arguments"].get("codigo")).upper(), "2404")

    def test_16_reuse_then_next_turn_keeps_inventory(self):
        self._chat("¿Cuánto stock tenemos del 2404?")
        self._chat("¿Y los últimos movimientos?")
        self._chat("Dime cuáles son.")
        r = self._chat("¿Y en qué bodega?")
        self.assertTrue(r.get("reuse_prior_evidence"))
        self.assertIn("Bodega", r["reply"])

    def test_17_multi_entity_disambiguation(self):
        self.store.append(
            "albertadmin",
            "conv-fase5",
            {
                "tools_used": ["search_catalog"],
                "entities": {"codigo": "2404", "codigos": ["2404", "9999"]},
                "evidence": [
                    {
                        "tool": "search_catalog",
                        "ok": True,
                        "empty": False,
                        "data": {
                            "items": [
                                {"codigo": "2404", "descripcion": "A"},
                                {"codigo": "9999", "descripcion": "B"},
                            ],
                            "count": 2,
                        },
                        "meta": {},
                    }
                ],
                "reply_excerpt": "cat",
            },
        )
        r = self._chat("¿Y ese producto?")
        self.assertTrue(r.get("needs_clarification"))
        self.assertIn("2404", r["reply"])
        self.assertIn("9999", r["reply"])

    def test_18_empty_context_clarify(self):
        r = self._chat("¿Y cuánto queda?", conversation_id="conv-empty-brand-new")
        self.assertTrue(r.get("needs_clarification"))
        self.assertEqual(r.get("tools_used"), [])

    def test_19_expired_context(self):
        store = TurnStore(max_turns=6, ttl_seconds=1)
        store.append(
            "albertadmin",
            "conv-exp",
            {
                "tools_used": ["get_inventory"],
                "entities": {"codigo": "2404", "codigos": ["2404"]},
                "evidence": [
                    {
                        "tool": "get_inventory",
                        "ok": True,
                        "empty": False,
                        "data": {
                            "codigo": "2404",
                            "total_stock": 2,
                            "items": [{"bodega": "Bodega 1", "stock": 2}],
                        },
                        "meta": {},
                    }
                ],
                "reply_excerpt": "stock",
            },
            now=1000.0,
        )
        # Expire
        turns = store.get("albertadmin", "conv-exp", now=1000.0 + 100)
        self.assertEqual(turns, [])
        r = run_orchestrator_chat(
            message="¿Y cuánto queda?",
            actor_user="albertadmin",
            conversation_id="conv-exp",
            invoke_fn=lambda p: _inv_ok(),
            planner=self.planner,
            audit=self.audit,
            turn_store=store,
        )
        self.assertTrue(r.get("needs_clarification"))

    def test_20_permission_denied_followup(self):
        self._chat("¿Cuánto stock tenemos del 2404?")

        def invoke_fn(payload: dict):
            self.invokes.append(payload)
            if payload.get("tool") == "get_stock_movements":
                return (
                    403,
                    {
                        "ok": False,
                        "tool": "get_stock_movements",
                        "error_code": "permission_denied",
                        "message": "sin permiso",
                    },
                )
            return _inv_ok()

        r = run_orchestrator_chat(
            message="¿Y los últimos movimientos?",
            actor_user="albertadmin",
            conversation_id="conv-fase5",
            invoke_fn=invoke_fn,
            planner=self.planner,
            audit=self.audit,
            turn_store=self.store,
        )
        self.assertIn("permiso", r["reply"].lower())
        self.assertEqual(self.invokes[-1].get("actor_user"), "albertadmin")

    def test_21_followup_needs_new_tool(self):
        """After inventory, movements is a NEW tool call (not reuse)."""
        self._chat("¿Cuánto stock tenemos del 2404?")
        n = len(self.invokes)
        r = self._chat("¿Y los últimos movimientos?")
        self.assertEqual(r["tools_used"], ["get_stock_movements"])
        self.assertEqual(len(self.invokes), n + 1)
        self.assertFalse(r.get("reuse_prior_evidence"))


if __name__ == "__main__":
    unittest.main()
