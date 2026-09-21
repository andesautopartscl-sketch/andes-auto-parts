"""FASE 8.1G.3 — definitive-failure derivation (N04) and blocked-final progress (T07).

Every test states a general rule. The N04/T07 shapes appear only as the concrete
shapes that motivated each rule; no assertion is keyed to a benchmark case_id.

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
    MAX_BLOCKED_FINALS_NO_PROGRESS,
    MAX_TOOL_CALLS,
)
from app.assistant.orchestrator.agent_config import TOKEN_BUDGET_FALLBACK
from app.assistant.orchestrator.agent_loop import (
    QueueDecisionClient,
    allow_partial_final,
    continue_agent_loop,
)
from app.assistant.orchestrator.agent_progress import ProgressLedger
from app.assistant.orchestrator.audit import OrchestratorAudit
from app.assistant.orchestrator.evidence_store import EvidenceStore
from app.assistant.orchestrator.goal_coverage import (
    DEFINITIVE_ERRORS,
    EXTRACTION_DETECTED,
    IMPOSSIBLE_REASONS,
    NON_DEFINITIVE_ERRORS,
    GoalCoverage,
    GoalRequirement,
    covering_tools,
    derive_impossible_reason,
)
from app.assistant.orchestrator.planner import FakePlanner
from app.assistant.orchestrator.service import run_orchestrator_chat
from app.assistant.orchestrator.turn_store import TurnStore


def _fail(tool: str, code: str) -> dict[str, Any]:
    return {
        "ok": False,
        "tool": tool,
        "empty": True,
        "error_code": code,
        "classification": "INTERNAL",
        "data": {},
        "meta": {},
    }


def _ok(tool: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "ok": True,
        "tool": tool,
        "empty": False,
        "classification": "INTERNAL",
        "data": data if data is not None else {"items": [{"x": 1}]},
        "meta": {},
    }


def _goal(rtype: str) -> GoalCoverage:
    return GoalCoverage(
        extraction=EXTRACTION_DETECTED,
        requirements=[GoalRequirement(id="r1", type=rtype)],
    )


class DefinitiveFailureContractTests(unittest.TestCase):
    def test_definitive_codes_are_a_closed_allowlist(self):
        self.assertEqual(set(DEFINITIVE_ERRORS), {"not_found", "permission_denied"})
        for spec in DEFINITIVE_ERRORS.values():
            self.assertIn(spec["reason"], IMPOSSIBLE_REASONS)
            self.assertIn(spec["scope"], {"entity", "tool"})

    def test_transport_and_unknown_codes_are_never_definitive(self):
        for code in NON_DEFINITIVE_ERRORS | {"who_knows", "", "418_teapot"}:
            self.assertNotIn(code, DEFINITIVE_ERRORS, code)
            store = EvidenceStore()
            store.add_from_tool_result(
                tool="get_inventory", arguments={"codigo": "Z"}, result=_fail("get_inventory", code)
            )
            self.assertIsNone(
                derive_impossible_reason(store.items, covering_tools("current_inventory")),
                code,
            )


class NFourDerivationTests(unittest.TestCase):
    """A. B. C. D. E. — requested cases."""

    def _refresh(self, rtype: str, results: list[tuple[str, dict[str, Any]]]) -> GoalCoverage:
        goal = _goal(rtype)
        store = EvidenceStore()
        for tool, res in results:
            store.add_from_tool_result(tool=tool, arguments={"codigo": "Z"}, result=res)
        return goal.refresh(store)

    def test_a_get_inventory_not_found_makes_the_requirement_impossible(self):
        goal = self._refresh("current_inventory", [("get_inventory", _fail("get_inventory", "not_found"))])
        req = goal.by_type("current_inventory")
        self.assertEqual(req.status, "impossible")
        self.assertEqual(req.reason, "not_found")
        self.assertFalse(goal.blocks_final())

    def test_b_check_stock_not_found_makes_the_requirement_impossible(self):
        goal = self._refresh("current_inventory", [("check_stock", _fail("check_stock", "not_found"))])
        self.assertEqual(goal.by_type("current_inventory").status, "impossible")
        self.assertFalse(goal.blocks_final())

    def test_c_timeout_stays_uncovered(self):
        for code in ("timeout", "agent_unavailable", "erp_unavailable"):
            goal = self._refresh("current_inventory", [("get_inventory", _fail("get_inventory", code))])
            req = goal.by_type("current_inventory")
            self.assertEqual(req.status, "uncovered", code)
            self.assertIsNone(req.reason, code)
            self.assertTrue(goal.blocks_final(), code)

    def test_d_tool_scoped_denial_keeps_the_requirement_open_while_a_sibling_is_untried(self):
        """permission_denied is per tool: a sibling may still be permitted."""
        goal = self._refresh(
            "current_inventory", [("get_inventory", _fail("get_inventory", "permission_denied"))]
        )
        self.assertEqual(goal.by_type("current_inventory").status, "uncovered")
        self.assertTrue(goal.blocks_final())

    def test_d2_entity_scoped_not_found_does_not_wait_for_the_sibling(self):
        """not_found is a fact about the ENTITY, not the tool.

        The sibling cannot discover the entity exists, and check_stock answers
        "disponible: 0" for a code that does not exist — reporting absence as zero.
        So an untried sibling is not a real route here.
        """
        goal = self._refresh("current_inventory", [("get_inventory", _fail("get_inventory", "not_found"))])
        self.assertEqual(goal.by_type("current_inventory").status, "impossible")
        self.assertNotIn("check_stock", {i.tool for i in EvidenceStore().items})

    def test_e_permission_denied_on_every_covering_tool_is_impossible(self):
        goal = self._refresh(
            "current_inventory",
            [
                ("get_inventory", _fail("get_inventory", "permission_denied")),
                ("check_stock", _fail("check_stock", "permission_denied")),
            ],
        )
        req = goal.by_type("current_inventory")
        self.assertEqual(req.status, "impossible")
        self.assertEqual(req.reason, "permission")

    def test_single_tool_requirement_is_impossible_on_first_denial(self):
        goal = self._refresh(
            "stock_movements", [("get_stock_movements", _fail("get_stock_movements", "permission_denied"))]
        )
        self.assertEqual(goal.by_type("stock_movements").status, "impossible")

    def test_one_non_definitive_failure_keeps_the_door_open(self):
        goal = self._refresh(
            "current_inventory",
            [
                ("get_inventory", _fail("get_inventory", "not_found")),
                ("check_stock", _fail("check_stock", "timeout")),
            ],
        )
        self.assertEqual(goal.by_type("current_inventory").status, "uncovered")

    def test_success_always_wins_over_a_definitive_failure(self):
        goal = self._refresh(
            "current_inventory",
            [
                ("get_inventory", _fail("get_inventory", "not_found")),
                ("check_stock", _ok("check_stock")),
            ],
        )
        self.assertEqual(goal.by_type("current_inventory").status, "covered")

    def test_derivation_does_not_change_empty_but_ok_semantics(self):
        goal = self._refresh(
            "supplier", [("get_supplier", {**_ok("get_supplier", {}), "empty": True})]
        )
        self.assertEqual(goal.by_type("supplier").status, "covered")


def _invoke_not_found(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    tool = payload.get("tool")
    if tool == "get_inventory":
        return 404, {"ok": False, "tool": "get_inventory", "error_code": "not_found",
                     "message": "producto no encontrado"}
    return 200, {"ok": True, "tool": tool, "data": {}, "meta": {}, "empty": True}


def _call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"action": "call_tool", "tool": tool, "arguments": arguments,
            "reason": "need", "claims": [], "calculations": []}


def _final(text: str, eids: list[str]) -> dict[str, Any]:
    return {"action": "final_answer", "reason": "enough", "draft_reply": text,
            "claims": [{"kind": "dato", "text": text, "evidence_ids": eids}],
            "calculations": []}


class NFourEndToEndTests(unittest.TestCase):
    """F. — a nonexistent entity must end as a grounded negative, not a fallback."""

    def test_f_not_found_entity_closes_without_loop_or_fallback(self):
        out = continue_agent_loop(
            message="Stock del codigo ZZZZNOEXISTE",
            actor_user="albertadmin",
            conversation_id="c",
            correlation_id="cid",
            initial_plan=None,
            initial_evidence=None,
            invoke_fn=_invoke_not_found,
            decision_client=QueueDecisionClient(
                [
                    _call("get_inventory", {"codigo": "ZZZZNOEXISTE"}),
                    _final("No hay registro del codigo consultado.", ["e1"]),
                ]
            ),
        )
        self.assertFalse(out.fallback_used, out.fallback_reason)
        self.assertEqual(out.state.blocked_finals, 0)
        req = out.state.goal.by_type("current_inventory")
        self.assertEqual(req.status, "impossible")
        self.assertEqual(req.reason, "not_found")
        self.assertIn("No pude resolver", out.reply)
        self.assertLessEqual(out.state.step_index, 2)

    def test_f2_no_extra_invokes_are_spent_hunting_a_sibling_tool(self):
        out = continue_agent_loop(
            message="Stock del codigo ZZZZNOEXISTE",
            actor_user="albertadmin",
            conversation_id="c",
            correlation_id="cid",
            initial_plan=None,
            initial_evidence=None,
            invoke_fn=_invoke_not_found,
            decision_client=QueueDecisionClient(
                [
                    _call("get_inventory", {"codigo": "ZZZZNOEXISTE"}),
                    _final("No hay registro del codigo consultado.", ["e1"]),
                ]
            ),
        )
        self.assertEqual(out.state.invoke_count, 1)
        self.assertLess(out.state.invoke_count, MAX_TOOL_CALLS)


def _invoke_supplier_then_po(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    tool = payload.get("tool")
    if tool == "get_supplier":
        return 200, {"ok": True, "tool": "get_supplier", "classification": "CONFIDENTIAL",
                     "data": {"items": [], "count": 0}, "meta": {}}
    if tool == "get_purchase_orders":
        return 200, {"ok": True, "tool": "get_purchase_orders", "classification": "CONFIDENTIAL",
                     "data": {"items": [{"numero": "OC-77"}], "count": 1}, "meta": {}}
    return 200, {"ok": True, "tool": tool, "data": {}, "meta": {}, "empty": True}


T07_MSG = "Proveedor BOSCH y sus órdenes de compra."


class BlockedFinalProgressTests(unittest.TestCase):
    """A. B. C. D. E. F. G. — requested cases."""

    def _loop(self, decisions: list[Any], message: str = T07_MSG, **kw):
        return continue_agent_loop(
            message=message,
            actor_user="albertadmin",
            conversation_id="c",
            correlation_id="cid",
            initial_plan=None,
            initial_evidence=None,
            invoke_fn=kw.pop("invoke_fn", _invoke_supplier_then_po),
            decision_client=QueueDecisionClient(decisions),
            **kw,
        )

    def test_a_one_blocked_final_continues(self):
        out = self._loop(
            [
                _call("get_supplier", {"q": "BOSCH"}),
                _final("parcial", ["e1"]),
                _call("get_purchase_orders", {"proveedor": "BOSCH"}),
                _final("OC-77", ["e1", "e2"]),
            ]
        )
        self.assertEqual(out.state.blocked_finals, 1)
        self.assertFalse(out.state.blocked_final_released)
        self.assertEqual(out.state.goal.uncovered(), [])
        self.assertFalse(out.fallback_used)

    def test_b_two_blocked_finals_still_continue(self):
        """Measured on T07: the runs that succeed do so after exactly 2 blocks."""
        out = self._loop(
            [
                _call("get_supplier", {"q": "BOSCH"}),
                _final("parcial", ["e1"]),
                _final("parcial otra vez", ["e1"]),
                _call("get_purchase_orders", {"proveedor": "BOSCH"}),
                _final("OC-77", ["e1", "e2"]),
            ]
        )
        self.assertEqual(out.state.blocked_finals, 2)
        self.assertFalse(out.state.blocked_final_released)
        self.assertIn("get_purchase_orders", [i.tool for i in out.state.evidence.items])
        self.assertEqual(out.state.goal.uncovered(), [])
        self.assertFalse(out.fallback_used)

    def test_c_third_consecutive_block_releases_a_partial_not_a_fallback(self):
        out = self._loop(
            [
                _call("get_supplier", {"q": "BOSCH"}),
                # No digits: an ungrounded figure would be dropped by the verifier
                # and would mask what this test is about.
                _final("parcial uno", ["e1"]),
                _final("parcial dos", ["e1"]),
                _final("parcial tres", ["e1"]),
            ]
        )
        self.assertEqual(out.state.blocked_finals, MAX_BLOCKED_FINALS_NO_PROGRESS)
        self.assertTrue(out.state.blocked_final_released)
        self.assertFalse(out.fallback_used, out.fallback_reason)
        self.assertIn("No pude resolver", out.reply)
        self.assertEqual([r.type for r in out.state.goal.uncovered()], ["purchase_orders"])

    def test_c2_threshold_is_at_least_three(self):
        self.assertGreaterEqual(MAX_BLOCKED_FINALS_NO_PROGRESS, 3)

    def test_d_blocked_then_covering_tool_is_allowed(self):
        out = self._loop(
            [
                _call("get_supplier", {"q": "BOSCH"}),
                _final("parcial", ["e1"]),
                _call("get_purchase_orders", {"proveedor": "BOSCH"}),
                _final("OC-77", ["e1", "e2"]),
            ]
        )
        trace = [t for t in out.state.trace if t.get("action") == "call_tool"]
        self.assertEqual([t["tool"] for t in trace], ["get_supplier", "get_purchase_orders"])
        self.assertTrue(all(t.get("admission", "").startswith("admit") for t in trace))

    def test_d2_coverage_change_resets_the_block_streak(self):
        ledger = ProgressLedger()
        self.assertFalse(ledger.record_blocked_final("a=uncovered"))
        self.assertFalse(ledger.record_blocked_final("a=uncovered"))
        self.assertEqual(ledger.consecutive_blocked_finals, 2)
        ledger.note_coverage("a=covered")
        self.assertEqual(ledger.consecutive_blocked_finals, 0)
        self.assertFalse(ledger.record_blocked_final("a=covered"))
        self.assertEqual(ledger.consecutive_blocked_finals, 1)
        self.assertEqual(ledger.blocked_finals, 3)

    def test_e_all_covered_final_is_allowed_immediately(self):
        out = self._loop(
            [
                _call("get_supplier", {"q": "BOSCH"}),
                _call("get_purchase_orders", {"proveedor": "BOSCH"}),
                _final("OC-77", ["e1", "e2"]),
            ]
        )
        self.assertEqual(out.state.blocked_finals, 0)
        self.assertFalse(out.fallback_used)
        self.assertNotIn("No pude resolver", out.reply)

    def test_f_impossible_requirement_allows_a_valid_partial(self):
        def _denied(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            if payload.get("tool") == "get_purchase_orders":
                return 403, {"ok": False, "tool": "get_purchase_orders",
                             "error_code": "permission_denied"}
            return _invoke_supplier_then_po(payload)

        out = self._loop(
            [
                _call("get_supplier", {"q": "BOSCH"}),
                _call("get_purchase_orders", {"proveedor": "BOSCH"}),
                _final("Proveedor sin registros.", ["e1"]),
            ],
            invoke_fn=_denied,
        )
        self.assertEqual(out.state.goal.by_type("purchase_orders").status, "impossible")
        self.assertEqual(out.state.blocked_finals, 0)
        self.assertIn("No pude resolver", out.reply)

    def test_g_exhausted_invoke_budget_allows_a_partial(self):
        goal = GoalCoverage(
            extraction=EXTRACTION_DETECTED,
            requirements=[GoalRequirement(id="r1", type="purchase_orders")],
        )
        self.assertTrue(allow_partial_final(goal, EvidenceStore(), remaining=0))
        out = self._loop(
            [
                _call("get_supplier", {"q": "BOSCH"}),
                _final("solo proveedor", ["e1"]),
            ],
            max_tool_calls=1,
        )
        self.assertFalse(out.fallback_used, out.fallback_reason)
        self.assertEqual(out.state.blocked_finals, 0)
        self.assertIn("No pude resolver", out.reply)

    def test_release_keeps_loop_and_cost_protection(self):
        """Releasing a partial must not weaken MAX_SAME_CALL or the tool budget."""
        out = self._loop(
            [
                _call("get_supplier", {"q": "BOSCH"}),
                _final("p1", ["e1"]),
                _final("p2", ["e1"]),
                _final("p3", ["e1"]),
            ]
        )
        self.assertTrue(out.state.blocked_final_released)
        self.assertEqual(out.state.invoke_count, 1)
        self.assertLessEqual(out.state.step_index, 4)


class FallbackReasonTests(unittest.TestCase):
    def test_token_budget_has_its_own_reason(self):
        out = continue_agent_loop(
            message="Stock del 2404",
            actor_user="albertadmin",
            conversation_id="c",
            correlation_id="cid",
            initial_plan=None,
            initial_evidence=None,
            invoke_fn=_invoke_supplier_then_po,
            decision_client=QueueDecisionClient([_final("x", ["e1"])]),
            usage_totals={"prompt_tokens": TOKEN_BUDGET_FALLBACK,
                          "completion_tokens": 0},
        )
        self.assertTrue(out.fallback_used)
        self.assertEqual(out.fallback_reason, "agent_token_budget")

    def test_invoke_budget_keeps_agent_limit(self):
        out = continue_agent_loop(
            message="Proveedor BOSCH y sus órdenes de compra.",
            actor_user="albertadmin",
            conversation_id="c",
            correlation_id="cid",
            initial_plan=None,
            initial_evidence=None,
            invoke_fn=_invoke_supplier_then_po,
            decision_client=QueueDecisionClient(
                [
                    _call("get_supplier", {"q": "BOSCH"}),
                    _call("get_purchase_orders", {"proveedor": "BOSCH"}),
                ]
            ),
            max_tool_calls=1,
        )
        self.assertTrue(out.fallback_used)
        self.assertEqual(out.fallback_reason, "agent_limit")


class HardeningTests(unittest.TestCase):
    """FASE 8.1K — deuda convertida en invariante."""

    def test_no_dead_state_on_the_turn(self):
        """empty_followups se escribia y nadie lo leia desde 8.1G."""
        from dataclasses import fields

        from app.assistant.orchestrator.agent_loop import TurnAgentState

        names = {f.name for f in fields(TurnAgentState)}
        self.assertNotIn("empty_followups", names)

    def test_the_hint_does_not_survive_the_call_that_resolves_it(self):
        """Un hint de un final bloqueado describe una situacion que la siguiente
        llamada cambia; dejarlo pegado contamina decisiones posteriores."""
        seen: list[str | None] = []

        class _Probe(QueueDecisionClient):
            def complete_decision(self, *, system: str, user: str):
                seen.append("final_answer bloqueado" in user)
                return super().complete_decision(system=system, user=user)

        out = continue_agent_loop(
            message="Proveedor BOSCH y sus órdenes de compra.",
            actor_user="albertadmin", conversation_id="c", correlation_id="cid",
            initial_plan=None, initial_evidence=None,
            invoke_fn=_invoke_supplier_then_po,
            decision_client=_Probe([
                _call("get_supplier", {"q": "BOSCH"}),
                _final("parcial", ["e1"]),
                _call("get_purchase_orders", {"proveedor": "BOSCH"}),
                _final("OC-77", ["e1", "e2"]),
            ]),
        )
        self.assertFalse(out.fallback_used, out.fallback_reason)
        # d3 lleva el hint (viene del bloqueo); d4 ya no, porque d3 lo resolvio.
        self.assertTrue(seen[2], "la decision posterior al bloqueo debe recibir el hint")
        self.assertFalse(seen[3], "el hint no debe sobrevivir a la llamada que lo resuelve")
        self.assertIsNone(out.state.coverage_hint)

    def test_a_transport_error_still_counts_as_a_call(self):
        """Sin registrarla, MAX_SAME_CALL no ve la llamada fallida y el modelo puede
        repetirla identica hasta agotar el tope de errores."""
        from app.assistant.orchestrator.tool_runner import ToolRunnerError

        calls: list[str] = []

        def _boom(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            calls.append(str(payload.get("tool")))
            raise ToolRunnerError("agent_unavailable", "transport down")

        out = continue_agent_loop(
            message="Proveedor BOSCH y sus órdenes de compra.",
            actor_user="albertadmin", conversation_id="c", correlation_id="cid",
            initial_plan=None, initial_evidence=None,
            invoke_fn=_boom,
            decision_client=QueueDecisionClient([
                _call("get_supplier", {"q": "BOSCH"}),
                _call("get_supplier", {"q": "BOSCH"}),
                _final("sin datos", ["e1"]),
            ]),
        )
        # la repeticion identica la corta el ledger, no el tope de errores
        self.assertTrue(out.state.loop_detected)
        self.assertEqual(len(calls), 1)


class AuditObservabilityTests(unittest.TestCase):
    """FASE 8.1K — la traza debe permitir reconstruir el turno sin el modelo."""

    @patch("app.assistant.orchestrator.service.agent_loop_allowed", return_value=True)
    def test_the_audit_record_answers_the_operational_questions(self, _allowed):
        import json

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.jsonl"
            out = run_orchestrator_chat(
                message="Proveedor BOSCH y sus órdenes de compra.",
                actor_user="albertadmin", conversation_id="c-audit",
                invoke_fn=_invoke_supplier_then_po, planner=FakePlanner(),
                audit=OrchestratorAudit(path=path), turn_store=TurnStore(),
                agent_decision_client=QueueDecisionClient([
                    _call("get_supplier", {"q": "BOSCH"}),
                    _final("parcial", ["e1"]),
                    _call("get_purchase_orders", {"proveedor": "BOSCH"}),
                    _final("OC-77", ["e1", "e2"]),
                ]),
            )
            self.assertTrue(out.get("ok"))
            rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        agent_rows = [r for r in rows if r.get("agent_trace")]
        self.assertTrue(agent_rows, "el audit debe registrar la traza del agente")
        rec = agent_rows[-1]
        for field in ("agent_enabled", "agent_steps", "fallback_used", "fallback_reason",
                      "agent_trace", "goal_coverage", "verifier_breakdown", "agent_progress",
                      "arg_errors"):
            self.assertIn(field, rec, field)
        trace = rec["agent_trace"]
        # que quiso hacer, que tool, por que era valida, que progreso hizo
        for key in ("action", "tool", "admission", "progress", "blocked_final"):
            self.assertIn(key, trace[0], key)
        # que requisito intentaba cubrir, y con que tool
        reqs = (rec["goal_coverage"] or {}).get("requirements") or []
        self.assertTrue(reqs)
        self.assertIn("tools", reqs[0])

    def test_the_audit_never_persists_the_message_or_secrets(self):
        import json

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.jsonl"
            OrchestratorAudit(path=path).write(
                {"message": "stock del 2404", "Authorization": "Bearer abc123",
                 "api_key": "sk-secret", "nested": {"token": "t0ken"}}
            )
            raw = path.read_text(encoding="utf-8")
        rec = json.loads(raw.splitlines()[0])
        self.assertEqual(rec["message"], "[omitted]")
        self.assertIn("message_hash", rec)
        for secret in ("abc123", "sk-secret", "t0ken"):
            self.assertNotIn(secret, raw, secret)


class AgentZeroTests(unittest.TestCase):
    def test_agent_0_path_unchanged(self):
        with patch.dict(
            os.environ,
            {
                "ANDES_ASSISTANT_AGENT_ENABLED": "0",
                "ANDES_ASSISTANT_NL_ENABLED": "0",
                "ANDES_ORCH_PLANNER": "fake",
                "ANDES_ASSISTANT_MEMORY_ENABLED": "0",
                "ANDES_ASSISTANT_HISTORY_ENABLED": "0",
            },
            clear=False,
        ):
            with tempfile.TemporaryDirectory() as tmp:
                out = run_orchestrator_chat(
                    message="Stock del codigo ZZZZNOEXISTE",
                    actor_user="albertadmin",
                    conversation_id="c-a0-g3",
                    invoke_fn=_invoke_not_found,
                    planner=FakePlanner(),
                    audit=OrchestratorAudit(path=Path(tmp) / "a.jsonl"),
                    turn_store=TurnStore(),
                )
        self.assertTrue(out.get("ok"))
        self.assertFalse(out.get("agent_enabled"))
        self.assertIsNone(out.get("goal_coverage"))
        self.assertIsNone(out.get("agent_progress"))


if __name__ == "__main__":
    unittest.main()
