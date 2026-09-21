"""FASE 8.1G — structural progress, admission and extraction generality.

Every test here asserts a GENERAL rule of the system. No test asserts a
benchmark case_id outcome; the T07/P03/E06 shapes appear only as the concrete
shapes that motivated each rule.

AGENT=0 must keep current behavior.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from app.assistant.orchestrator.agent_config import (
    MAX_EMPTY_FOLLOWUPS,
    MAX_NO_PROGRESS_STEPS,
    MAX_REJECTED_DECISIONS,
    MAX_SAME_CALL,
)
from app.assistant.orchestrator.agent_loop import QueueDecisionClient, continue_agent_loop
from app.assistant.orchestrator.agent_progress import (
    ADMIT_COVERS_UNCOVERED,
    ADMIT_NEW_TOOL,
    PROGRESS_COVERAGE,
    PROGRESS_EVIDENCE,
    PROGRESS_NONE,
    REJECT_EMPTY_NO_PROGRESS,
    REJECT_REPEAT_CALL,
    REJECT_TOOL_NO_PROGRESS,
    ProgressLedger,
    covers_uncovered_requirement,
)
from app.assistant.orchestrator.answer_verifier import verify_agent_answer
from app.assistant.orchestrator.audit import OrchestratorAudit
from app.assistant.orchestrator.evidence_store import EvidenceStore, canonical_content_key
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


def _result(
    tool: str,
    *,
    ok: bool = True,
    empty: bool = False,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "tool": tool,
        "empty": empty,
        "classification": "INTERNAL",
        "data": data if data is not None else {"items": [{"x": 1}]},
        "meta": {},
    }


def _t07_goal(supplier: str = "uncovered", purchase_orders: str = "uncovered") -> GoalCoverage:
    return GoalCoverage(
        extraction=EXTRACTION_DETECTED,
        requirements=[
            GoalRequirement(id="r1", type="supplier", status=supplier),
            GoalRequirement(id="r2", type="purchase_orders", status=purchase_orders),
        ],
    )


class ContentFingerprintTests(unittest.TestCase):
    def test_same_payload_same_key_regardless_of_arguments(self):
        store = EvidenceStore()
        a = store.add_from_tool_result(
            tool="get_dashboard_kpis",
            arguments={"periodo": "7d"},
            result=_result("get_dashboard_kpis", data={"ventas": None, "top": []}),
        )
        b = store.add_from_tool_result(
            tool="get_dashboard_kpis",
            arguments={"periodo": "7d", "top_limit": 5},
            result=_result("get_dashboard_kpis", data={"ventas": None, "top": []}),
        )
        self.assertNotEqual(a.call_key, b.call_key)
        self.assertEqual(a.content_key, b.content_key)

    def test_different_payload_different_key(self):
        k1 = canonical_content_key("get_inventory", ok=True, empty=False, data_view={"s": 2}, meta_view={})
        k2 = canonical_content_key("get_inventory", ok=True, empty=False, data_view={"s": 3}, meta_view={})
        self.assertNotEqual(k1, k2)

    def test_empty_flag_changes_the_fingerprint(self):
        k1 = canonical_content_key("get_supplier", ok=True, empty=False, data_view={}, meta_view={})
        k2 = canonical_content_key("get_supplier", ok=True, empty=True, data_view={}, meta_view={})
        self.assertNotEqual(k1, k2)


class AdmissionRuleTests(unittest.TestCase):
    """Admission rejects demonstrated repetition without progress — nothing else."""

    def test_same_canonical_call_is_rejected(self):
        ledger = ProgressLedger()
        store = EvidenceStore()
        item = store.add_from_tool_result(
            tool="get_inventory", arguments={"codigo": "2404"}, result=_result("get_inventory")
        )
        ledger.seed(store)
        self.assertEqual(ledger.call_count(item.call_key), MAX_SAME_CALL)
        out = ledger.admit("get_inventory", {"codigo": "2404"}, GoalCoverage())
        self.assertFalse(out.admitted)
        self.assertEqual(out.reason, REJECT_REPEAT_CALL)

    def test_different_tool_covering_uncovered_is_admitted_after_empty(self):
        """The T07 shape: an empty result must not block a different useful tool."""
        goal = _t07_goal(supplier="covered", purchase_orders="uncovered")
        store = EvidenceStore()
        store.add_from_tool_result(
            tool="get_supplier",
            arguments={"q": "BOSCH"},
            result=_result("get_supplier", empty=True, data={}),
        )
        ledger = ProgressLedger()
        ledger.seed(store)
        out = ledger.admit("get_purchase_orders", {"proveedor": "BOSCH"}, goal)
        self.assertTrue(out.admitted)
        self.assertEqual(out.reason, ADMIT_NEW_TOOL)

    def test_one_refined_retry_is_allowed_after_an_empty_result(self):
        """FASE 8.2 — refinar una consulta tras cero resultados es un uso legitimo.

        MAX_EMPTY_FOLLOWUPS=1 cerraba la tool en el primer vacio, asi que
        "search_catalog q=filtro" sin resultados impedia probar "filtro de aceite".
        """
        goal = _t07_goal(supplier="covered", purchase_orders="covered")
        store = EvidenceStore()
        store.add_from_tool_result(
            tool="get_supplier",
            arguments={"q": "BOSCH"},
            result=_result("get_supplier", empty=True, data={}),
        )
        ledger = ProgressLedger()
        ledger.seed(store)
        self.assertTrue(ledger.admit("get_supplier", {"q": "BOSCH SA"}, goal).admitted)

    def test_the_second_empty_closes_the_tool(self):
        """El refinamiento es UNO: si tambien vuelve vacio, no hay progreso."""
        goal = _t07_goal(supplier="covered", purchase_orders="covered")
        store = EvidenceStore()
        ledger = ProgressLedger()
        for q in ("BOSCH", "BOSCH SA"):
            item = store.add_from_tool_result(
                tool="get_supplier", arguments={"q": q},
                result=_result("get_supplier", empty=True, data={}),
            )
            ledger.record_execution("get_supplier", [item], resolved_before=2, resolved_after=2)
        out = ledger.admit("get_supplier", {"q": "BOSCH LTDA"}, goal)
        self.assertFalse(out.admitted)
        self.assertEqual(out.reason, REJECT_EMPTY_NO_PROGRESS)
        self.assertGreaterEqual(ledger.record("get_supplier").empty_results, MAX_EMPTY_FOLLOWUPS)

    def test_tool_that_returned_equivalent_evidence_is_rejected(self):
        """Productive once is not productive forever: the LAST step decides."""
        goal = GoalCoverage()
        ledger = ProgressLedger()
        store = EvidenceStore()
        first = store.add_from_tool_result(
            tool="get_dashboard_kpis", arguments={"periodo": "7d"}, result=_result("get_dashboard_kpis")
        )
        ledger.record_execution("get_dashboard_kpis", [first], resolved_before=0, resolved_after=0)
        self.assertTrue(ledger.admit("get_dashboard_kpis", {"top_limit": 5}, goal).admitted)
        second = store.add_from_tool_result(
            tool="get_dashboard_kpis", arguments={"top_limit": 5}, result=_result("get_dashboard_kpis")
        )
        ledger.record_execution("get_dashboard_kpis", [second], resolved_before=0, resolved_after=0)
        out = ledger.admit("get_dashboard_kpis", {"top_limit": 3}, goal)
        self.assertFalse(out.admitted)
        self.assertEqual(out.reason, REJECT_TOOL_NO_PROGRESS)

    def test_retry_allowed_while_the_requirement_it_covers_stays_uncovered(self):
        goal = _t07_goal(supplier="uncovered", purchase_orders="covered")
        store = EvidenceStore()
        store.add_from_tool_result(
            tool="get_supplier",
            arguments={"q": "BOSH"},
            result=_result("get_supplier", ok=False, empty=True, data={}),
        )
        ledger = ProgressLedger()
        ledger.seed(store)
        self.assertTrue(covers_uncovered_requirement(goal, "get_supplier"))
        out = ledger.admit("get_supplier", {"q": "BOSCH"}, goal)
        self.assertTrue(out.admitted)
        self.assertEqual(out.reason, ADMIT_COVERS_UNCOVERED)

    def test_productive_tool_is_capped_by_empty_followups(self):
        goal = GoalCoverage()
        ledger = ProgressLedger()
        store = EvidenceStore()
        good = store.add_from_tool_result(
            tool="get_purchase_orders",
            arguments={"proveedor": "BOSCH"},
            result=_result("get_purchase_orders", data={"items": [{"n": 1}]}),
        )
        ledger.record_execution(
            "get_purchase_orders", [good], resolved_before=0, resolved_after=0
        )
        for extra in ({"estado": "abierta"}, {"estado": "cerrada"}):
            empty = store.add_from_tool_result(
                tool="get_purchase_orders",
                arguments={"proveedor": "BOSCH", **extra},
                result=_result("get_purchase_orders", empty=True, data={}),
            )
            ledger.record_execution(
                "get_purchase_orders", [empty], resolved_before=0, resolved_after=0
            )
        self.assertGreaterEqual(
            ledger.record("get_purchase_orders").empty_results, MAX_EMPTY_FOLLOWUPS
        )
        out = ledger.admit("get_purchase_orders", {"proveedor": "BOSCH", "limit": 5}, goal)
        self.assertFalse(out.admitted)
        self.assertEqual(out.reason, REJECT_EMPTY_NO_PROGRESS)

    def test_covers_uncovered_ignores_impossible_requirements(self):
        goal = _t07_goal(supplier="covered", purchase_orders="impossible")
        self.assertFalse(covers_uncovered_requirement(goal, "get_purchase_orders"))


class ProgressClassificationTests(unittest.TestCase):
    def test_coverage_progress_wins(self):
        ledger = ProgressLedger()
        store = EvidenceStore()
        item = store.add_from_tool_result(
            tool="get_supplier", arguments={"q": "BOSCH"}, result=_result("get_supplier", empty=True, data={})
        )
        kind = ledger.record_execution("get_supplier", [item], resolved_before=0, resolved_after=1)
        self.assertEqual(kind, PROGRESS_COVERAGE)
        self.assertEqual(ledger.consecutive_no_progress, 0)

    def test_new_useful_evidence_is_progress(self):
        ledger = ProgressLedger()
        store = EvidenceStore()
        item = store.add_from_tool_result(
            tool="get_dashboard_kpis", arguments={}, result=_result("get_dashboard_kpis")
        )
        kind = ledger.record_execution("get_dashboard_kpis", [item], resolved_before=0, resolved_after=0)
        self.assertEqual(kind, PROGRESS_EVIDENCE)

    def test_equivalent_evidence_is_not_progress(self):
        """Same tool, different args, identical payload → no progress (P03 shape)."""
        ledger = ProgressLedger()
        store = EvidenceStore()
        first = store.add_from_tool_result(
            tool="get_dashboard_kpis", arguments={"periodo": "7d"}, result=_result("get_dashboard_kpis")
        )
        ledger.record_execution("get_dashboard_kpis", [first], resolved_before=0, resolved_after=0)
        second = store.add_from_tool_result(
            tool="get_dashboard_kpis",
            arguments={"periodo": "7d", "top_limit": 5},
            result=_result("get_dashboard_kpis"),
        )
        kind = ledger.record_execution(
            "get_dashboard_kpis", [second], resolved_before=0, resolved_after=0
        )
        self.assertEqual(kind, PROGRESS_NONE)
        self.assertEqual(ledger.consecutive_no_progress, 1)

    def test_empty_and_failed_results_are_not_evidence_progress(self):
        ledger = ProgressLedger()
        store = EvidenceStore()
        empty = store.add_from_tool_result(
            tool="get_supplier", arguments={"q": "X"}, result=_result("get_supplier", empty=True, data={})
        )
        self.assertEqual(
            ledger.record_execution("get_supplier", [empty], resolved_before=1, resolved_after=1),
            PROGRESS_NONE,
        )
        failed = store.add_from_tool_result(
            tool="get_customer", arguments={"q": "X"}, result=_result("get_customer", ok=False, data={})
        )
        self.assertEqual(
            ledger.record_execution("get_customer", [failed], resolved_before=1, resolved_after=1),
            PROGRESS_NONE,
        )
        self.assertTrue(ledger.no_progress_exhausted())

    def test_progress_resets_the_consecutive_counter(self):
        ledger = ProgressLedger()
        store = EvidenceStore()
        empty = store.add_from_tool_result(
            tool="get_supplier", arguments={"q": "X"}, result=_result("get_supplier", empty=True, data={})
        )
        ledger.record_execution("get_supplier", [empty], resolved_before=1, resolved_after=1)
        self.assertEqual(ledger.consecutive_no_progress, 1)
        good = store.add_from_tool_result(
            tool="get_purchase_orders", arguments={}, result=_result("get_purchase_orders")
        )
        ledger.record_execution("get_purchase_orders", [good], resolved_before=1, resolved_after=2)
        self.assertEqual(ledger.consecutive_no_progress, 0)
        self.assertFalse(ledger.no_progress_exhausted())

    def test_snapshot_is_counters_only(self):
        ledger = ProgressLedger()
        store = EvidenceStore()
        item = store.add_from_tool_result(
            tool="get_inventory",
            arguments={"codigo": "SECRET-CODE"},
            result=_result("get_inventory", data={"total_stock": 7}),
        )
        ledger.record_execution("get_inventory", [item], resolved_before=0, resolved_after=1)
        snap = ledger.safe_snapshot()
        blob = repr(snap)
        self.assertNotIn("SECRET-CODE", blob)
        self.assertNotIn("total_stock", blob)
        self.assertEqual(snap["progress_events"], 1)
        self.assertIn("get_inventory", snap["tools"])


def _invoke(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Supplier returns empty; purchase orders return rows."""
    tool = payload.get("tool")
    if tool == "get_supplier":
        return 200, {
            "ok": True,
            "tool": "get_supplier",
            "classification": "CONFIDENTIAL",
            "empty": True,
            "data": {"items": [], "count": 0},
            "meta": {},
        }
    if tool == "get_purchase_orders":
        return 200, {
            "ok": True,
            "tool": "get_purchase_orders",
            "classification": "CONFIDENTIAL",
            "data": {"items": [{"numero": "OC-77", "estado": "abierta"}], "count": 1},
            "meta": {},
        }
    if tool == "get_dashboard_kpis":
        return 200, {
            "ok": True,
            "tool": "get_dashboard_kpis",
            "classification": "CONFIDENTIAL",
            "data": {"periodo": "7d", "top_productos": [{"codigo": "2404"}]},
            "meta": {"periodo": "7d"},
        }
    return 200, {"ok": True, "tool": tool, "data": {}, "meta": {}, "empty": True}


def _call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": "call_tool",
        "tool": tool,
        "arguments": arguments,
        "reason": "need",
        "claims": [],
        "calculations": [],
    }


def _final(text: str, eids: list[str]) -> dict[str, Any]:
    return {
        "action": "final_answer",
        "reason": "enough",
        "draft_reply": text,
        "claims": [{"kind": "dato", "text": text, "evidence_ids": eids}],
        "calculations": [],
    }


class LoopAdmissionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.audit = OrchestratorAudit(path=Path(self._tmp.name) / "a.jsonl")
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
        self._tmp.cleanup()

    def _loop(self, message: str, decisions: list[Any]):
        return continue_agent_loop(
            message=message,
            actor_user="albertadmin",
            conversation_id="c-81g",
            correlation_id="cid-81g",
            initial_plan=None,
            initial_evidence=None,
            invoke_fn=_invoke,
            decision_client=QueueDecisionClient(decisions),
        )

    def test_empty_result_does_not_block_a_different_covering_tool(self):
        """An empty tool result must not end the turn while a useful route is open."""
        out = self._loop(
            "Proveedor BOSCH y sus órdenes de compra.",
            [
                _call("get_supplier", {"q": "BOSCH"}),
                _call("get_purchase_orders", {"proveedor": "BOSCH"}),
                _final("OC-77 abierta", ["e1", "e2"]),
            ],
        )
        self.assertFalse(out.fallback_used, out.fallback_reason)
        tools = [i.tool for i in out.state.evidence.items]
        self.assertEqual(tools, ["get_supplier", "get_purchase_orders"])
        statuses = {r.type: r.status for r in out.state.goal.requirements}
        self.assertEqual(statuses.get("supplier"), "covered")
        self.assertEqual(statuses.get("purchase_orders"), "covered")

    def test_empty_result_then_blocked_finals_then_covering_tool(self):
        """Blocked finals must not consume the budget that reaches the right tool."""
        out = self._loop(
            "Proveedor BOSCH y sus órdenes de compra.",
            [
                _call("get_supplier", {"q": "BOSCH"}),
                _final("parcial", ["e1"]),
                _final("parcial otra vez", ["e1"]),
                _call("get_purchase_orders", {"proveedor": "BOSCH"}),
                _final("OC-77 abierta", ["e1", "e2"]),
            ],
        )
        self.assertFalse(out.fallback_used, out.fallback_reason)
        self.assertGreaterEqual(out.state.blocked_finals, 2)
        self.assertEqual(out.state.goal.uncovered(), [])

    def test_equivalent_repeat_is_rejected_without_ending_the_turn(self):
        """A no-progress repeat costs one decision, not the turn."""
        out = self._loop(
            "KPIs de 7 días.",
            [
                _call("get_dashboard_kpis", {"periodo": "7d"}),
                _call("get_dashboard_kpis", {"periodo": "7d", "top_limit": 5}),
                _call("get_dashboard_kpis", {"periodo": "7d", "top_limit": 3}),
                _final("periodo 7d", ["e1"]),
            ],
        )
        self.assertFalse(out.fallback_used, out.fallback_reason)
        self.assertEqual(len(out.state.evidence.items), 2)
        self.assertTrue(out.state.no_progress_detected)

    def test_persistent_no_progress_terminates_with_its_own_reason(self):
        out = self._loop(
            "KPIs de 7 días.",
            [
                _call("get_dashboard_kpis", {"periodo": "7d"}),
                _call("get_dashboard_kpis", {"top_limit": 5}),
                _call("get_dashboard_kpis", {"top_limit": 4}),
                _call("get_dashboard_kpis", {"top_limit": 3}),
                _call("get_dashboard_kpis", {"top_limit": 2}),
            ],
        )
        self.assertTrue(out.fallback_used)
        self.assertEqual(out.fallback_reason, "agent_no_progress")
        self.assertLessEqual(out.state.rejected_decisions, MAX_REJECTED_DECISIONS)

    def test_no_progress_budget_is_not_looser_than_the_tool_budget(self):
        self.assertLessEqual(MAX_NO_PROGRESS_STEPS, 2)
        self.assertLessEqual(MAX_REJECTED_DECISIONS, 3)

    def test_trace_records_admission_and_progress(self):
        out = self._loop(
            "Proveedor BOSCH y sus órdenes de compra.",
            [
                _call("get_supplier", {"q": "BOSCH"}),
                _call("get_purchase_orders", {"proveedor": "BOSCH"}),
                _final("OC-77 abierta", ["e1", "e2"]),
            ],
        )
        calls = [t for t in out.state.trace if t.get("action") == "call_tool"]
        self.assertTrue(all(t.get("admission") for t in calls))
        self.assertEqual(calls[0].get("progress"), PROGRESS_COVERAGE)

    @patch("app.assistant.orchestrator.service.agent_loop_allowed", return_value=True)
    def test_service_exposes_progress_snapshot(self, _allowed):
        out = run_orchestrator_chat(
            message="Proveedor BOSCH y sus órdenes de compra.",
            actor_user="albertadmin",
            conversation_id="c-81g-svc",
            invoke_fn=_invoke,
            planner=FakePlanner(),
            audit=self.audit,
            turn_store=TurnStore(),
            agent_decision_client=QueueDecisionClient(
                [
                    _call("get_supplier", {"q": "BOSCH"}),
                    _call("get_purchase_orders", {"proveedor": "BOSCH"}),
                    _final("OC-77 abierta", ["e1", "e2"]),
                ]
            ),
        )
        self.assertTrue(out.get("ok"))
        prog = out.get("agent_progress") or {}
        self.assertGreaterEqual(int(prog.get("progress_events") or 0), 1)
        self.assertIn("get_purchase_orders", prog.get("tools") or {})

    def test_agent_0_never_builds_a_ledger(self):
        out = run_orchestrator_chat(
            message="Proveedor BOSCH y sus órdenes de compra.",
            actor_user="albertadmin",
            conversation_id="c-81g-off",
            invoke_fn=_invoke,
            planner=FakePlanner(),
            audit=self.audit,
            turn_store=TurnStore(),
        )
        self.assertTrue(out.get("ok"))
        self.assertIsNone(out.get("agent_progress"))
        self.assertFalse(out.get("agent_enabled"))


class ExtractionGeneralityTests(unittest.TestCase):
    """The facet rule is one rule, not one rule per phrase."""

    def test_stock_as_a_facet_of_another_intent_is_not_an_inventory_goal(self):
        for text, expected in (
            ("KPIs de 7 días con stock crítico.", ["dashboard_kpis"]),
            ("Movimientos de stock del 2404", ["stock_movements"]),
            ("Movimiento de stock del 2404", ["stock_movements"]),
        ):
            ext, types = extract_requirement_types(text)
            self.assertEqual(ext, EXTRACTION_DETECTED, text)
            self.assertEqual(types, expected, text)

    def test_real_dual_intent_keeps_both_requirements(self):
        for text in (
            "Stock y movimientos del 2404.",
            "Consulta movimientos y stock actual del 2404.",
            "Movimientos de stock y stock actual del 2404",
            "KPIs y stock actual del 2404",
            "Muéstrame los movimientos del 2404 y dime cuánto stock queda",
        ):
            _ext, types = extract_requirement_types(text)
            self.assertIn("current_inventory", types, text)

    def test_a_lone_facet_keeps_its_own_requirement(self):
        _ext, types = extract_requirement_types("productos con stock crítico")
        self.assertEqual(types, ["current_inventory"])

    def test_bare_order_words_need_a_buying_qualifier(self):
        for text in (
            "en orden alfabético",
            "ordena los productos por precio",
            "orden de trabajo 55",
            "po",
        ):
            _ext, types = extract_requirement_types(text)
            self.assertNotIn("purchase_orders", types, text)

    def test_qualified_order_words_still_detect_purchase_orders(self):
        for text, expected in (
            ("órdenes de compra pendientes", {"purchase_orders"}),
            ("órdenes del proveedor BOSCH", {"purchase_orders", "supplier"}),
            ("Proveedor BOSCH y sus órdenes de compra.", {"purchase_orders", "supplier"}),
        ):
            ext, types = extract_requirement_types(text)
            self.assertEqual(ext, EXTRACTION_DETECTED, text)
            self.assertEqual(set(types), expected, text)

    def test_generic_business_words_never_become_requirements(self):
        for text in (
            "muéstrame las compras del mes",
            "Ventas del día",
            "quiero comprar el filtro 2404",
            "pedidos pendientes de clientes",
        ):
            ext, types = extract_requirement_types(text)
            self.assertEqual(ext, EXTRACTION_UNKNOWN, f"{text} -> {types}")

    def test_every_allowlisted_tool_belongs_to_exactly_one_requirement_type(self):
        from app.assistant.orchestrator.catalog import ALLOWED_TOOLS
        from app.assistant.orchestrator.goal_coverage import REQUIREMENT_TYPES

        mapped: dict[str, list[str]] = {}
        for rtype, spec in REQUIREMENT_TYPES.items():
            for tool in spec["tools"]:
                mapped.setdefault(tool, []).append(rtype)
        self.assertEqual(set(mapped) , set(ALLOWED_TOOLS))
        for tool, types in mapped.items():
            self.assertEqual(len(types), 1, f"{tool} mapped to {types}")


class VerifierBreakdownTests(unittest.TestCase):
    """The verifier is unchanged; only its reporting is now separable.

    This is the M02/M04 mechanism: a number supplied by the user is not evidence,
    so a claim citing it is discarded. Discarding it and still publishing a
    grounded answer is the guardrail working, not a degraded turn.
    """

    def _store(self) -> EvidenceStore:
        store = EvidenceStore()
        store.add_from_tool_result(
            tool="get_inventory",
            arguments={"codigo": "2404"},
            result={
                "ok": True,
                "empty": False,
                "data": {"codigo": "2404", "total_stock": 2, "items": [{"bodega": "Bodega 1", "stock": 2}]},
                "meta": {},
            },
        )
        return store

    def test_user_supplied_number_is_dropped_and_grounded_answer_survives(self):
        out = verify_agent_answer(
            store=self._store(),
            decision={
                "action": "final_answer",
                "draft_reply": "",
                "claims": [
                    {"kind": "dato", "text": "El stock del 2404 es 2 unidades", "evidence_ids": ["e1"]},
                    {"kind": "inferencia", "text": "Bajó desde 25 unidades", "evidence_ids": ["e1"]},
                ],
                "calculations": [],
            },
        )
        self.assertFalse(out.answer_replaced)
        self.assertFalse(out.used_composer_fallback)
        self.assertEqual(out.dropped_claims, 1)
        self.assertIn("2 unidades", out.reply)
        self.assertNotIn("25", out.reply)
        # failures keeps its original meaning and value
        self.assertEqual(out.failures, 1)

    def test_every_claim_ungrounded_is_a_replaced_answer(self):
        out = verify_agent_answer(
            store=self._store(),
            decision={
                "action": "final_answer",
                "draft_reply": "",
                "claims": [
                    {"kind": "dato", "text": "El stock del 2404 es 8888", "evidence_ids": ["e1"]},
                ],
                "calculations": [],
            },
        )
        self.assertTrue(out.answer_replaced)
        self.assertEqual(out.replaced_reason, "no_grounded_claim")
        self.assertEqual(out.dropped_claims, 1)
        self.assertNotIn("8888", out.reply)

    def test_breakdown_separates_calculation_failure_kinds(self):
        bad_path = verify_agent_answer(
            store=self._store(),
            decision={
                "action": "final_answer",
                "draft_reply": "",
                "claims": [{"kind": "dato", "text": "stock 2", "evidence_ids": ["e1"]}],
                "calculations": [
                    {"id": "c1", "op": "sum", "inputs": ["e9.data.total_stock"], "result": 2}
                ],
            },
        )
        self.assertEqual(bad_path.calc_unresolved, 1)
        self.assertEqual(bad_path.calc_mismatch, 0)

        wrong_value = verify_agent_answer(
            store=self._store(),
            decision={
                "action": "final_answer",
                "draft_reply": "",
                "claims": [{"kind": "dato", "text": "stock 2", "evidence_ids": ["e1"]}],
                "calculations": [
                    {"id": "c1", "op": "sum", "inputs": ["e1.data.total_stock"], "result": 99}
                ],
            },
        )
        self.assertEqual(wrong_value.calc_mismatch, 1)
        self.assertEqual(wrong_value.calc_unresolved, 0)

    def test_clean_answer_has_an_empty_breakdown(self):
        out = verify_agent_answer(
            store=self._store(),
            decision={
                "action": "final_answer",
                "draft_reply": "",
                "claims": [{"kind": "dato", "text": "El 2404 tiene 2 unidades", "evidence_ids": ["e1"]}],
                "calculations": [],
            },
        )
        self.assertTrue(out.ok)
        self.assertEqual(out.failures, 0)
        self.assertEqual(out.dropped_claims, 0)
        self.assertFalse(out.answer_replaced)


if __name__ == "__main__":
    unittest.main()
