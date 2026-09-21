"""FASE 8.1A — goal coverage. AGENT=0 path must stay unchanged."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from app.assistant.orchestrator.agent_loop import (
    QueueDecisionClient,
    allow_partial_final,
    continue_agent_loop,
    empty_agent_plan,
    has_unused_covering_for_uncovered,
)
from app.assistant.orchestrator.agent_schema import validate_agent_decision
from app.assistant.orchestrator.audit import OrchestratorAudit
from app.assistant.orchestrator.evidence_store import EvidenceStore
from app.assistant.orchestrator.goal_coverage import (
    EXTRACTION_DETECTED,
    EXTRACTION_UNKNOWN,
    GoalCoverage,
    GoalRequirement,
    extract_requirement_types,
)
from app.assistant.orchestrator.planner import FakePlanner
from app.assistant.orchestrator.service import run_orchestrator_chat
from app.assistant.orchestrator.turn_store import TurnStore

from tests.test_orchestrator_fase81_agent import (
    _call,
    _final,
    _invoke_router,
)

P1 = "Muéstrame los movimientos del 2404 y dime cuánto stock queda actualmente."
EQUIV = [
    P1,
    "Revisa los movimientos del 2404 y después dime cuánto stock hay.",
    "Quiero los movimientos del 2404 más el stock actual.",
    "Consulta movimientos y stock actual del 2404.",
    "¿Qué movimientos ha tenido el 2404 y cuántas unidades quedan?",
]


class Fase81AGoalTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.audit = OrchestratorAudit(path=Path(self._tmpdir.name) / "audit.jsonl")
        self.store = TurnStore()
        self._env = patch.dict(
            os.environ,
            {
                "ANDES_ASSISTANT_AGENT_ENABLED": "0",
                "ANDES_ASSISTANT_NL_ENABLED": "0",
                "ANDES_ORCH_PLANNER": "fake",
                "ANDES_ASSISTANT_MEMORY_ENABLED": "0",
                "ANDES_ASSISTANT_HISTORY_ENABLED": "0",
            },
            clear=False,
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmpdir.cleanup()

    def _chat(self, message: str, **kwargs):
        return run_orchestrator_chat(
            message=message,
            actor_user="albertadmin",
            conversation_id="c-81a",
            invoke_fn=kwargs.pop("invoke_fn", _invoke_router),
            planner=kwargs.pop("planner", FakePlanner()),
            audit=self.audit,
            turn_store=self.store,
            **kwargs,
        )

    def test_agent_0_intact(self):
        client = QueueDecisionClient([_final()])
        out = self._chat("Stock del 2404", agent_decision_client=client)
        self.assertTrue(out.get("ok"))
        self.assertFalse(out.get("agent_enabled"))
        self.assertEqual(client.calls, 0)
        self.assertIn("get_inventory", out.get("tools_used") or [])
        self.assertIsNone(out.get("goal_coverage"))

    def test_extract_equivalent_prompts_need_both(self):
        for text in EQUIV:
            extraction, types = extract_requirement_types(text)
            self.assertEqual(extraction, EXTRACTION_DETECTED, text)
            self.assertEqual(set(types), {"stock_movements", "current_inventory"}, text)

    def test_extract_single_and_unknown(self):
        ext, types = extract_requirement_types("Stock del 2404")
        self.assertEqual(ext, EXTRACTION_DETECTED)
        self.assertEqual(types, ["current_inventory"])
        ext2, types2 = extract_requirement_types("hola, qué puedes hacer?")
        self.assertEqual(ext2, EXTRACTION_UNKNOWN)
        self.assertEqual(types2, [])

    def test_extract_stock_simple_is_current_inventory_only(self):
        for text in (
            "Stock del 2404",
            "¿Cuánto inventario queda del 2404?",
            "stock critico del filtro",
        ):
            ext, types = extract_requirement_types(text)
            self.assertEqual(ext, EXTRACTION_DETECTED, text)
            self.assertEqual(types, ["current_inventory"], text)
            self.assertNotIn("dashboard_kpis", types, text)

    def test_extract_dashboard_kpi_intent(self):
        cases = [
            ("KPIs de los últimos 7 días.", ["dashboard_kpis"]),
            ("Dashboard del mes", ["dashboard_kpis"]),
            ("Indicadores de la semana", ["dashboard_kpis"]),
            ("Ranking de ventas del mes.", ["dashboard_kpis"]),
            ("Top productos de 7 días.", ["dashboard_kpis"]),
            ("Resumen gerencial 7d", ["dashboard_kpis"]),
        ]
        for text, expected in cases:
            ext, types = extract_requirement_types(text)
            self.assertEqual(ext, EXTRACTION_DETECTED, text)
            self.assertEqual(types, expected, text)
            self.assertNotIn("current_inventory", types, text)

    def test_extract_dashboard_with_stock_critico_facet(self):
        # P03/R03-shaped: KPI + "stock crítico" as dashboard facet → dashboard only.
        text = "KPIs de 7 días con stock crítico."
        ext, types = extract_requirement_types(text)
        self.assertEqual(ext, EXTRACTION_DETECTED)
        self.assertEqual(types, ["dashboard_kpis"])
        cov = GoalCoverage.from_message(text)
        self.assertEqual([r.type for r in cov.requirements], ["dashboard_kpis"])
        store = EvidenceStore()
        store.add_from_tool_result(
            tool="get_dashboard_kpis",
            arguments={"periodo": "7d"},
            result={"ok": True, "data": {"periodo": "7d", "documentos": 0}, "meta": {}},
        )
        cov.refresh(store)
        self.assertEqual(cov.by_type("dashboard_kpis").status, "covered")
        self.assertFalse(cov.blocks_final())

    def test_extract_dashboard_plus_real_inventory_intent(self):
        text = "KPIs de 7 días y stock actual del 2404"
        ext, types = extract_requirement_types(text)
        self.assertEqual(ext, EXTRACTION_DETECTED)
        self.assertEqual(set(types), {"dashboard_kpis", "current_inventory"})

    def test_extract_bare_stock_does_not_become_dashboard(self):
        for text in (
            "stock",
            "Hay stock del 2404?",
            "stock critico",
            "revisa el stock en bodega",
        ):
            _ext, types = extract_requirement_types(text)
            self.assertNotIn("dashboard_kpis", types, text)

    def test_extract_supplier_only(self):
        for text in ("Proveedor BOSCH", "muéstrame proveedores", "Supplier ACME"):
            ext, types = extract_requirement_types(text)
            self.assertEqual(ext, EXTRACTION_DETECTED, text)
            self.assertEqual(types, ["supplier"], text)

    def test_extract_purchase_orders_only(self):
        text = "muéstrame órdenes de compra pendientes"
        ext, types = extract_requirement_types(text)
        self.assertEqual(ext, EXTRACTION_DETECTED)
        self.assertEqual(types, ["purchase_orders"])
        self.assertNotIn("supplier", types)

    def test_extract_ordenes_de_compra_with_name_without_proveedor_word(self):
        # Name alone is not a supplier stem; only purchase_orders.
        text = "Órdenes de compra de BOSCH"
        ext, types = extract_requirement_types(text)
        self.assertEqual(ext, EXTRACTION_DETECTED)
        self.assertEqual(types, ["purchase_orders"])

    def test_extract_ordenes_de_compra_with_explicit_proveedor(self):
        text = "Órdenes de compra del proveedor BOSCH"
        ext, types = extract_requirement_types(text)
        self.assertEqual(ext, EXTRACTION_DETECTED)
        self.assertEqual(set(types), {"purchase_orders", "supplier"})

    def test_extract_t07_supplier_and_purchase_orders(self):
        text = "Proveedor BOSCH y sus órdenes de compra."
        ext, types = extract_requirement_types(text)
        self.assertEqual(ext, EXTRACTION_DETECTED)
        self.assertEqual(set(types), {"supplier", "purchase_orders"})

    def test_extract_compra_ventas_do_not_spuriously_add_supplier_or_po(self):
        for text in (
            "muéstrame las compras del mes",
            "Ventas del día",
            "quiero comprar el filtro 2404",
            "pedidos pendientes de clientes",
        ):
            _ext, types = extract_requirement_types(text)
            self.assertNotIn("supplier", types, text)
            self.assertNotIn("purchase_orders", types, text)

    def test_goal_coverage_supplier_and_purchase_orders_mapping(self):
        from app.assistant.orchestrator.goal_coverage import GoalRequirement

        cov = GoalCoverage(
            extraction=EXTRACTION_DETECTED,
            requirements=[
                GoalRequirement(id="r1", type="supplier"),
                GoalRequirement(id="r2", type="purchase_orders"),
            ],
        )
        store = EvidenceStore()
        store.add_from_tool_result(
            tool="get_purchase_orders",
            arguments={"proveedor": "BOSCH"},
            result={"ok": True, "data": {"items": []}, "meta": {}},
        )
        cov.refresh(store)
        self.assertEqual(cov.by_type("purchase_orders").status, "covered")
        self.assertEqual(cov.by_type("supplier").status, "uncovered")
        self.assertTrue(cov.blocks_final())
        store.add_from_tool_result(
            tool="get_supplier",
            arguments={"q": "BOSCH"},
            result={"ok": True, "data": {"nombre": "BOSCH"}, "meta": {}},
        )
        cov.refresh(store)
        self.assertEqual(cov.by_type("supplier").status, "covered")
        self.assertEqual(cov.by_type("purchase_orders").status, "covered")
        self.assertFalse(cov.blocks_final())

    def test_requirement_uncovered_then_covered(self):
        cov = GoalCoverage.from_message(P1)
        store = EvidenceStore()
        cov.refresh(store)
        self.assertTrue(cov.blocks_final())
        self.assertEqual({r.type for r in cov.uncovered()}, {"stock_movements", "current_inventory"})
        store.add_from_tool_result(
            tool="get_inventory",
            arguments={"codigo": "2404"},
            result={"ok": True, "data": {"codigo": "2404", "total_stock": 25}, "meta": {}},
        )
        cov.refresh(store)
        inv = cov.by_type("current_inventory")
        mov = cov.by_type("stock_movements")
        self.assertIsNotNone(inv)
        self.assertIsNotNone(mov)
        self.assertEqual(inv.status, "covered")
        self.assertEqual(inv.evidence_ids, ["e1"])
        self.assertEqual(mov.status, "uncovered")
        self.assertTrue(cov.blocks_final())
        store.add_from_tool_result(
            tool="get_stock_movements",
            arguments={"codigo": "2404"},
            result={"ok": True, "data": {"items": []}, "meta": {}},
        )
        cov.refresh(store)
        self.assertEqual(mov.status, "covered")
        self.assertEqual(mov.evidence_ids, ["e2"])
        self.assertFalse(cov.blocks_final())

    def test_evidence_mapping_is_this_turn_only(self):
        cov = GoalCoverage.from_message(P1)
        store = EvidenceStore()
        store.add_from_tool_result(
            tool="get_stock_movements",
            arguments={"codigo": "2404"},
            result={"ok": True, "data": {"items": [{"tipo": "salida"}]}, "meta": {}},
        )
        cov.refresh(store)
        self.assertEqual(cov.by_type("stock_movements").evidence_ids, ["e1"])
        self.assertEqual(cov.by_type("current_inventory").evidence_ids, [])

    def test_memory_is_not_evidence_for_coverage(self):
        cov = GoalCoverage.from_message(P1)
        store = EvidenceStore()
        cov.refresh(store)
        self.assertTrue(cov.blocks_final())
        self.assertEqual(cov.by_type("current_inventory").status, "uncovered")

    @patch("app.assistant.orchestrator.service.agent_loop_allowed", return_value=True)
    def test_final_blocked_while_required_missing(self, _allowed):
        client = QueueDecisionClient(
            [
                _call("get_inventory", {"codigo": "2404"}),
                _final(),
                _call("get_stock_movements", {"codigo": "2404"}),
                _final("2404 tiene 25 unidades", eids=["e1", "e2"]),
            ]
        )
        planner = FakePlanner()
        planner_calls: list[str] = []
        orig = planner.plan

        def spy(message: str, *, context=None):
            planner_calls.append(message)
            return orig(message, context=context)

        planner.plan = spy  # type: ignore[method-assign]
        out = self._chat(P1, planner=planner, agent_decision_client=client)
        self.assertTrue(out.get("ok"))
        self.assertEqual(planner_calls, [])
        self.assertEqual(out.get("tools_used"), ["get_inventory", "get_stock_movements"])
        trace = out.get("agent_trace") or []
        blocked = [t for t in trace if t.get("blocked_final")]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0].get("action"), "final_answer")
        self.assertGreaterEqual(blocked[0].get("goal_uncovered_count") or 0, 1)
        finals = [t for t in trace if t.get("action") == "final_answer" and not t.get("blocked_final")]
        self.assertTrue(finals)
        self.assertGreaterEqual(finals[-1].get("evidence_count_before") or 0, 2)
        snap = out.get("goal_coverage") or {}
        types_ok = {r["type"]: r["status"] for r in snap.get("requirements") or []}
        self.assertEqual(types_ok.get("stock_movements"), "covered")
        self.assertEqual(types_ok.get("current_inventory"), "covered")
        self.assertFalse(out.get("fallback_used"))

    @patch("app.assistant.orchestrator.service.agent_loop_allowed", return_value=True)
    def test_final_allowed_when_all_covered(self, _allowed):
        client = QueueDecisionClient(
            [
                _call("get_stock_movements", {"codigo": "2404"}),
                _call("get_inventory", {"codigo": "2404"}),
                _final("2404 tiene 25 unidades", eids=["e1", "e2"]),
            ]
        )
        out = self._chat(P1, agent_decision_client=client)
        self.assertTrue(out.get("ok"))
        self.assertFalse(out.get("fallback_used"))
        self.assertEqual(out.get("tools_used"), ["get_stock_movements", "get_inventory"])
        trace = out.get("agent_trace") or []
        self.assertFalse(any(t.get("blocked_final") for t in trace))
        self.assertEqual(trace[1].get("evidence_count_before"), 1)
        snap = out.get("goal_coverage") or {}
        self.assertEqual(snap.get("extraction"), EXTRACTION_DETECTED)
        self.assertTrue(all(r.get("status") == "covered" for r in snap.get("requirements") or []))

    @patch("app.assistant.orchestrator.service.agent_loop_allowed", return_value=True)
    def test_requirement_impossible_allows_final(self, _allowed):
        client = QueueDecisionClient(
            [
                {
                    "action": "call_tool",
                    "tool": "get_product",
                    "arguments": {"codigo": "2404"},
                    "reason": "ficha",
                    "proposed_requirements": [{"id": "r1", "type": "product_detail"}],
                },
                {
                    "action": "final_answer",
                    "draft_reply": "No tienes permiso para la ficha",
                    "claims": [],
                    "calculations": [],
                    "unresolved": [
                        {"id": "u1", "type": "product_detail", "reason": "permission"}
                    ],
                },
            ]
        )
        out = self._chat("hola, necesito una ficha", agent_decision_client=client)
        self.assertTrue(out.get("ok"))
        snap = out.get("goal_coverage") or {}
        statuses = {r["type"]: r["status"] for r in snap.get("requirements") or []}
        self.assertEqual(statuses.get("product_detail"), "impossible")
        self.assertFalse(any(t.get("blocked_final") for t in (out.get("agent_trace") or [])))

    @patch("app.assistant.orchestrator.service.agent_loop_allowed", return_value=True)
    def test_impossible_requires_attempt_or_no_budget(self, _allowed):
        seen_notes: list[str] = []

        class _Probe(QueueDecisionClient):
            def complete_decision(self, *, system: str, user: str) -> dict[str, Any]:
                # FASE 8.1J — the blocked-final hint must NAME the tool that is
                # still missing, not only the internal requirement type.
                if "final_answer bloqueado" in user and "get_stock_movements" in user:
                    seen_notes.append("blocked")
                return super().complete_decision(system=system, user=user)

        client = _Probe(
            [
                _call("get_inventory", {"codigo": "2404"}),
                {
                    "action": "final_answer",
                    "draft_reply": "2404 tiene 25 unidades",
                    "claims": [
                        {"kind": "dato", "text": "2404 tiene 25 unidades", "evidence_ids": ["e1"]}
                    ],
                    "calculations": [],
                    "unresolved": [
                        {"id": "u1", "type": "stock_movements", "reason": "cannot_resolve"}
                    ],
                },
                _call("get_stock_movements", {"codigo": "2404"}),
                _final("2404 tiene 25 unidades", eids=["e1", "e2"]),
            ]
        )
        out = self._chat(P1, agent_decision_client=client)
        self.assertTrue(out.get("ok"))
        self.assertIn("get_stock_movements", out.get("tools_used") or [])
        self.assertTrue(seen_notes)

    @patch("app.assistant.orchestrator.service.agent_loop_allowed", return_value=True)
    def test_partial_answer_after_second_final(self, _allowed):
        """FASE 8.1F.6 — second premature final stays blocked while covering tool unused.

        Formerly accepted partial via blocked_finals>=1; now requires covering tool
        (or remaining/impossible). Queue supplies the covering call after two blocks.
        """
        client = QueueDecisionClient(
            [
                _call("get_inventory", {"codigo": "2404"}),
                _final(),
                _final(),
                _call("get_stock_movements", {"codigo": "2404"}),
                _final("2404 tiene 25 unidades", eids=["e1", "e2"]),
            ]
        )
        out = self._chat(P1, agent_decision_client=client)
        self.assertTrue(out.get("ok"))
        self.assertFalse(out.get("fallback_used"))
        self.assertEqual(
            set(out.get("tools_used") or []),
            {"get_inventory", "get_stock_movements"},
        )
        blocked = [t for t in (out.get("agent_trace") or []) if t.get("blocked_final")]
        self.assertGreaterEqual(len(blocked), 2)
        snap = out.get("goal_coverage") or {}
        statuses = {r["type"]: r["status"] for r in snap.get("requirements") or []}
        self.assertEqual(statuses.get("current_inventory"), "covered")
        self.assertEqual(statuses.get("stock_movements"), "covered")

    @patch("app.assistant.orchestrator.service.agent_loop_allowed", return_value=True)
    def test_unknown_extraction_does_not_block(self, _allowed):
        client = QueueDecisionClient(
            [
                _call("search_catalog", {"q": "filtro"}),
                _final("hay 1 resultado", eids=["e1"]),
            ]
        )
        out = self._chat("hola, qué puedes hacer?", agent_decision_client=client)
        self.assertTrue(out.get("ok"))
        snap = out.get("goal_coverage") or {}
        self.assertEqual(snap.get("extraction"), EXTRACTION_UNKNOWN)
        self.assertFalse(any(t.get("blocked_final") for t in (out.get("agent_trace") or [])))

    @patch("app.assistant.orchestrator.service.agent_loop_allowed", return_value=True)
    def test_fase81f4_residual_final_still_blocked_when_uncovered(self, _allowed):
        """F: schema accepts residual-arg final_answer; GoalCoverage still blocks if uncovered."""
        residual_final = {
            "action": "final_answer",
            "tool": None,
            "arguments": {"q": "BOSCH", "limit": 10},
            "draft_reply": "Proveedor listo",
            "claims": [{"kind": "dato", "text": "BOSCH", "evidence_ids": ["e1"]}],
            "calculations": [],
        }
        parsed = validate_agent_decision(residual_final)
        self.assertEqual(parsed["arguments"], {})
        self.assertFalse(parsed.get("tool"))

        client = QueueDecisionClient(
            [
                _call("get_supplier", {"q": "BOSCH"}),
                residual_final,
                _call("get_purchase_orders", {"proveedor": "BOSCH"}),
                {
                    "action": "final_answer",
                    "arguments": {"q": "BOSCH", "limit": 10},
                    "draft_reply": "Proveedor BOSCH y sus OC.",
                    "claims": [
                        {"kind": "dato", "text": "BOSCH", "evidence_ids": ["e1"]},
                        {"kind": "dato", "text": "OC", "evidence_ids": ["e2"]},
                    ],
                    "calculations": [],
                },
            ]
        )
        out = self._chat(
            "Proveedor BOSCH y sus órdenes de compra.",
            agent_decision_client=client,
        )
        self.assertTrue(out.get("ok"))
        self.assertFalse(out.get("fallback_used"))
        self.assertNotEqual(out.get("fallback_reason"), "invalid_decision")
        tools = out.get("tools_used") or []
        self.assertIn("get_supplier", tools)
        self.assertIn("get_purchase_orders", tools)
        blocked = [t for t in (out.get("agent_trace") or []) if t.get("blocked_final")]
        self.assertGreaterEqual(len(blocked), 1)
        snap = out.get("goal_coverage") or {}
        types_ok = {r["type"]: r["status"] for r in snap.get("requirements") or []}
        self.assertEqual(types_ok.get("supplier"), "covered")
        self.assertEqual(types_ok.get("purchase_orders"), "covered")

    @patch("app.assistant.orchestrator.service.agent_loop_allowed", return_value=True)
    def test_fase81f4_residual_final_allowed_when_all_covered(self, _allowed):
        """G: residual-arg final_answer allowed once all requirements are covered."""
        client = QueueDecisionClient(
            [
                _call("get_supplier", {"q": "BOSCH"}),
                _call("get_purchase_orders", {"proveedor": "BOSCH"}),
                {
                    "action": "final_answer",
                    "arguments": {"q": "BOSCH", "limit": 10},
                    "draft_reply": "Proveedor BOSCH y OC listos.",
                    "claims": [
                        {"kind": "dato", "text": "BOSCH", "evidence_ids": ["e1"]},
                        {"kind": "dato", "text": "OC", "evidence_ids": ["e2"]},
                    ],
                    "calculations": [],
                },
            ]
        )
        out = self._chat(
            "Proveedor BOSCH y sus órdenes de compra.",
            agent_decision_client=client,
        )
        self.assertTrue(out.get("ok"))
        self.assertFalse(out.get("fallback_used"))
        self.assertEqual(
            set(out.get("tools_used") or []),
            {"get_supplier", "get_purchase_orders"},
        )
        self.assertFalse(any(t.get("blocked_final") for t in (out.get("agent_trace") or [])))
        snap = out.get("goal_coverage") or {}
        self.assertTrue(all(r.get("status") == "covered" for r in snap.get("requirements") or []))

    def test_proposed_requirements_ignored_when_invalid_or_detected(self):
        cov = GoalCoverage.from_message(P1)
        cov.merge_proposed([{"type": "catalog_search"}, {"type": "write_invoice"}])
        self.assertIsNone(cov.by_type("catalog_search"))
        unknown = GoalCoverage.from_message("hola")
        unknown.merge_proposed(
            [{"type": "current_inventory"}, {"type": "invented"}, {"type": "write_invoice"}]
        )
        self.assertIsNotNone(unknown.by_type("current_inventory"))
        self.assertIsNone(unknown.by_type("invented"))
        parsed = validate_agent_decision(
            {
                "action": "final_answer",
                "draft_reply": "ok",
                "claims": [{"kind": "dato", "text": "n", "evidence_ids": ["e1"]}],
                "proposed_requirements": [{"id": "x", "type": "current_inventory"}],
                "unresolved": [{"id": "u", "type": "stock_movements", "reason": "empty"}],
            }
        )
        self.assertEqual(parsed["proposed_requirements"][0]["type"], "current_inventory")
        self.assertEqual(parsed["unresolved"][0]["reason"], "empty")

    @patch("app.assistant.orchestrator.service.agent_loop_allowed", return_value=True)
    def test_multi_tool_second_sees_e1(self, _allowed):
        seen_before: list[int] = []

        class _Probe(QueueDecisionClient):
            def complete_decision(self, *, system: str, user: str) -> dict[str, Any]:
                pack = user.split("<evidence>", 1)[-1].split("</evidence>", 1)[0]
                seen_before.append(pack.count('"evidence_id"'))
                if "<goal_coverage>" not in user:
                    raise AssertionError("goal_coverage missing from prompt")
                return super().complete_decision(system=system, user=user)

        client = _Probe(
            [
                _call("get_stock_movements", {"codigo": "2404"}),
                _call("get_inventory", {"codigo": "2404"}),
                _final("2404 tiene 25 unidades", eids=["e1", "e2"]),
            ]
        )
        out = self._chat(P1, agent_decision_client=client)
        self.assertEqual(seen_before, [0, 1, 2])
        self.assertGreaterEqual((out.get("agent_trace") or [None, {}])[1].get("evidence_count_before"), 1)

    @patch("app.assistant.orchestrator.service.agent_loop_allowed", return_value=True)
    def test_memory_hints_do_not_cover_requirements(self, _allowed):
        class _Probe(QueueDecisionClient):
            def complete_decision(self, *, system: str, user: str) -> dict[str, Any]:
                if self.calls == 0:
                    if "uncovered" not in user or "stock_movements" not in user:
                        raise AssertionError("uncovered stock_movements missing from first prompt")
                ev = user.split("<evidence>", 1)[-1].split("</evidence>", 1)[0]
                if "stock=25" in ev:
                    raise AssertionError("memory stock leaked into evidence")
                return super().complete_decision(system=system, user=user)

        client = _Probe(
            [
                _call("get_stock_movements", {"codigo": "2404"}),
                _call("get_inventory", {"codigo": "2404"}),
                _final("2404 tiene 25 unidades", eids=["e1", "e2"]),
            ]
        )
        result = continue_agent_loop(
            message=P1,
            actor_user="albertadmin",
            conversation_id="c",
            correlation_id="cid",
            initial_plan=empty_agent_plan(),
            initial_evidence=[],
            invoke_fn=_invoke_router,
            decision_client=client,
            memory_hints=[{"type": "frequent_entity", "codigo": "2404", "stock": 25}],
        )
        self.assertEqual([e.get("tool") for e in result.raw_evidence], ["get_stock_movements", "get_inventory"])
        self.assertFalse(result.fallback_used)
        self.assertEqual(result.state.goal.by_type("current_inventory").status, "covered")

    def test_reuse_prior_is_not_fresh_evidence(self):
        client = QueueDecisionClient([_final()])
        first = self._chat("Stock del 2404", agent_decision_client=client)
        self.assertTrue(first.get("ok"))
        with patch("app.assistant.orchestrator.service.agent_loop_allowed", return_value=True):
            follow = self._chat("cuáles son", agent_decision_client=client)
        self.assertTrue(follow.get("ok"))
        self.assertTrue(follow.get("reuse_prior_evidence") or follow.get("scenario") == "context_reuse")
        self.assertEqual(client.calls, 0)
        self.assertFalse(follow.get("agent_enabled"))
        self.assertIsNone(follow.get("goal_coverage"))


class Fase81F6AllowPartialTests(unittest.TestCase):
    """FASE 8.1F.6 — allow_partial only when uncovered is no longer resolvable."""

    def _t07_goal(self, *, supplier="covered", purchase_orders="uncovered") -> GoalCoverage:
        return GoalCoverage(
            extraction=EXTRACTION_DETECTED,
            requirements=[
                GoalRequirement(id="r1", type="supplier", status=supplier),
                GoalRequirement(id="r2", type="purchase_orders", status=purchase_orders),
            ],
        )

    def test_a_blocked_final_with_unused_covering_disallows_partial(self):
        goal = self._t07_goal()
        store = EvidenceStore()
        store.add_from_tool_result(
            tool="get_supplier",
            arguments={"q": "BOSCH"},
            result={"ok": True, "data": {"nombre": "BOSCH"}, "meta": {}},
        )
        self.assertTrue(has_unused_covering_for_uncovered(goal, store))
        # blocked_finals=1 is irrelevant under the new rule
        self.assertFalse(allow_partial_final(goal, store, remaining=4))

    def test_b_remaining_zero_allows_partial(self):
        goal = self._t07_goal()
        store = EvidenceStore()
        store.add_from_tool_result(
            tool="get_supplier",
            arguments={"q": "BOSCH"},
            result={"ok": True, "data": {"nombre": "BOSCH"}, "meta": {}},
        )
        self.assertTrue(allow_partial_final(goal, store, remaining=0))

    def test_c_impossible_does_not_block_via_uncovered(self):
        goal = GoalCoverage(
            extraction=EXTRACTION_DETECTED,
            requirements=[
                GoalRequirement(id="r1", type="supplier", status="covered"),
                GoalRequirement(id="r2", type="purchase_orders", status="impossible"),
            ],
        )
        store = EvidenceStore()
        self.assertFalse(goal.blocks_final())
        self.assertTrue(allow_partial_final(goal, store, remaining=3))

    def test_d_no_covering_tool_allows_partial(self):
        goal = GoalCoverage(
            extraction=EXTRACTION_DETECTED,
            requirements=[
                GoalRequirement(id="r1", type="invented_type_without_tools", status="uncovered"),
            ],
        )
        # invented type: covering_tools returns empty → not resolvable
        store = EvidenceStore()
        self.assertFalse(has_unused_covering_for_uncovered(goal, store))
        self.assertTrue(allow_partial_final(goal, store, remaining=3))

    def test_e_covering_tool_already_tried_allows_partial(self):
        goal = self._t07_goal(supplier="covered", purchase_orders="uncovered")
        store = EvidenceStore()
        store.add_from_tool_result(
            tool="get_supplier",
            arguments={"q": "BOSCH"},
            result={"ok": True, "data": {"nombre": "BOSCH"}, "meta": {}},
        )
        store.add_from_tool_result(
            tool="get_purchase_orders",
            arguments={"proveedor": "BOSCH"},
            result={"ok": True, "data": {"items": []}, "meta": {}},
        )
        # PO tool attempted but requirement left uncovered (e.g. mapping lag / empty)
        # — progress protection permits partial.
        self.assertFalse(has_unused_covering_for_uncovered(goal, store))
        self.assertTrue(allow_partial_final(goal, store, remaining=2))

    def test_f_t07_shape_disallows_partial(self):
        goal = self._t07_goal()
        store = EvidenceStore()
        store.add_from_tool_result(
            tool="get_supplier",
            arguments={"q": "BOSCH"},
            result={"ok": True, "data": {"nombre": "BOSCH"}, "meta": {}},
        )
        self.assertEqual([r.type for r in goal.uncovered()], ["purchase_orders"])
        self.assertFalse(allow_partial_final(goal, store, remaining=4))

    def test_g_all_covered_final_allowed(self):
        goal = self._t07_goal(supplier="covered", purchase_orders="covered")
        store = EvidenceStore()
        self.assertFalse(goal.blocks_final())
        self.assertTrue(allow_partial_final(goal, store, remaining=4))

    @patch("app.assistant.orchestrator.service.agent_loop_allowed", return_value=True)
    def test_double_final_still_blocked_until_po_tool(self, _allowed):
        """Integration: second premature final must not accept; PO tool then final."""
        audit = OrchestratorAudit(path=Path(tempfile.mkdtemp()) / "a.jsonl")
        client = QueueDecisionClient(
            [
                _call("get_supplier", {"q": "BOSCH"}),
                _final("parcial BOSCH", eids=["e1"]),
                _final("parcial otra vez", eids=["e1"]),
                _call("get_purchase_orders", {"proveedor": "BOSCH"}),
                _final("BOSCH y OC", eids=["e1", "e2"]),
            ]
        )
        out = run_orchestrator_chat(
            message="Proveedor BOSCH y sus órdenes de compra.",
            actor_user="albertadmin",
            conversation_id="c-81f6",
            invoke_fn=_invoke_router,
            planner=FakePlanner(),
            audit=audit,
            turn_store=TurnStore(),
            agent_decision_client=client,
        )
        self.assertTrue(out.get("ok"))
        self.assertFalse(out.get("fallback_used"))
        tools = out.get("tools_used") or []
        self.assertIn("get_supplier", tools)
        self.assertIn("get_purchase_orders", tools)
        blocked = [t for t in (out.get("agent_trace") or []) if t.get("blocked_final")]
        self.assertGreaterEqual(len(blocked), 2)
        snap = out.get("goal_coverage") or {}
        types_ok = {r["type"]: r["status"] for r in snap.get("requirements") or []}
        self.assertEqual(types_ok.get("supplier"), "covered")
        self.assertEqual(types_ok.get("purchase_orders"), "covered")


if __name__ == "__main__":
    unittest.main()
