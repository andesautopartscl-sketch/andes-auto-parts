"""FASE 8.1C — deterministic failure classifier tests. No LLM."""
from __future__ import annotations

import unittest

from evals.fase81c_failure_diagnosis import classify_failure, diagnose_case


def _gold(**kwargs):
    base = {
        "id": "T00",
        "bucket": "T",
        "tools_expected": ["get_stock_movements", "get_inventory"],
        "tool_count": "exact",
        "goal_types": ["stock_movements", "current_inventory"],
        "expect_fallback": False,
    }
    base.update(kwargs)
    return base


def _run(**kwargs):
    base = {
        "id": "T00",
        "tools_used": [],
        "fallback_used": False,
        "fallback_reason": None,
        "verifier_failures": 0,
        "agent_trace": [],
        "goal_coverage": None,
        "scenario": "agent_loop",
        "needs_clarification": False,
        "latency_ms": 100,
    }
    base.update(kwargs)
    return base


class Fase81CFailureDiagnosisTests(unittest.TestCase):
    def test_premature_final_uncovered_is_b(self):
        gold = _gold()
        run = _run(
            tools_used=["get_inventory"],
            goal_coverage={
                "extraction": "detected",
                "requirements": [
                    {"id": "r1", "type": "stock_movements", "status": "uncovered", "evidence_ids": []},
                    {"id": "r2", "type": "current_inventory", "status": "covered", "evidence_ids": ["e1"]},
                ],
            },
            agent_trace=[
                {
                    "decision_index": 1,
                    "action": "call_tool",
                    "tool": "get_inventory",
                    "blocked_final": False,
                    "evidence_count_before": 0,
                    "evidence_count_after": 1,
                },
                {
                    "decision_index": 2,
                    "action": "final_answer",
                    "tool": None,
                    "blocked_final": False,
                    "evidence_count_before": 1,
                    "evidence_count_after": 1,
                },
            ],
        )
        self.assertEqual(classify_failure(gold, run), "B")

    def test_blocked_final_alone_is_not_b(self):
        gold = _gold(tools_expected=["get_inventory"], tool_count="exact", goal_types=["current_inventory"])
        run = _run(
            tools_used=["get_inventory"],
            goal_coverage={
                "extraction": "detected",
                "requirements": [
                    {
                        "id": "r1",
                        "type": "current_inventory",
                        "status": "uncovered",
                        "evidence_ids": [],
                    }
                ],
            },
            agent_trace=[
                {
                    "decision_index": 1,
                    "action": "call_tool",
                    "tool": "get_inventory",
                    "blocked_final": False,
                    "evidence_count_before": 0,
                    "evidence_count_after": 1,
                },
                {
                    "decision_index": 2,
                    "action": "final_answer",
                    "tool": None,
                    "blocked_final": True,
                    "evidence_count_before": 1,
                    "evidence_count_after": 1,
                },
            ],
        )
        # Last act is blocked_final → not B. Covering tool already used → not C.
        # Gold tools match → falls through; empty-not_found style → F.
        self.assertNotEqual(classify_failure(gold, run), "B")

    def test_verifier_failure_is_d(self):
        gold = _gold(tools_expected=["get_inventory"], goal_types=["current_inventory"])
        run = _run(
            tools_used=["get_inventory"],
            verifier_failures=1,
            goal_coverage={
                "requirements": [
                    {"type": "current_inventory", "status": "covered", "evidence_ids": ["e1"]}
                ]
            },
            agent_trace=[
                {"decision_index": 1, "action": "call_tool", "tool": "get_inventory"},
                {"decision_index": 2, "action": "final_answer", "blocked_final": False},
            ],
        )
        self.assertEqual(classify_failure(gold, run), "D")

    def test_agent_limit_is_e(self):
        gold = _gold(tools_expected=["get_dashboard_kpis"], tool_count="exact", goal_types=[])
        run = _run(
            tools_used=["get_dashboard_kpis", "get_dashboard_kpis"],
            fallback_used=True,
            fallback_reason="agent_limit",
            agent_trace=[{"decision_index": 1, "action": "fallback", "tool": None}],
        )
        self.assertEqual(classify_failure(gold, run), "E")

    def test_wrong_tool_is_a(self):
        gold = _gold(
            tools_expected=["check_stock"],
            tool_count="exact",
            goal_types=[],
        )
        run = _run(
            tools_used=["get_inventory"],
            goal_coverage={"extraction": "unknown", "requirements": []},
            agent_trace=[
                {"decision_index": 1, "action": "call_tool", "tool": "get_inventory"},
                {"decision_index": 2, "action": "final_answer", "blocked_final": False},
            ],
        )
        self.assertEqual(classify_failure(gold, run), "A")

    def test_uncovered_with_final_prefers_b_over_c(self):
        gold = _gold(
            tools_expected=["get_supplier", "get_purchase_orders"],
            tool_count="min",
            goal_types=[],
        )
        run = _run(
            tools_used=["get_purchase_orders"],
            goal_coverage={
                "extraction": "detected",
                "requirements": [
                    {"id": "r1", "type": "supplier", "status": "uncovered", "evidence_ids": []},
                    {"id": "r2", "type": "purchase_orders", "status": "covered", "evidence_ids": ["e1"]},
                ],
            },
            agent_trace=[
                {"decision_index": 1, "action": "call_tool", "tool": "get_purchase_orders"},
                {"decision_index": 2, "action": "final_answer", "blocked_final": False},
            ],
        )
        self.assertEqual(classify_failure(gold, run), "B")

    def test_uncovered_available_tool_without_premature_is_c(self):
        """Clarify/stop with uncovered requirement and covering tool never called → C."""
        gold = _gold(
            tools_expected=["get_stock_movements"],
            tool_count="min",
            goal_types=["stock_movements"],
        )
        run = _run(
            tools_used=[],
            goal_coverage={
                "extraction": "detected",
                "requirements": [
                    {"id": "r1", "type": "stock_movements", "status": "uncovered", "evidence_ids": []}
                ],
            },
            agent_trace=[
                {"decision_index": 1, "action": "clarify", "tool": None, "blocked_final": False},
            ],
        )
        self.assertEqual(classify_failure(gold, run), "C")

    def test_dataset_expected_tools_mismatch_is_f(self):
        gold = _gold(
            tools_expected=["check_stock", "get_inventory"],
            tool_count="min",
            goal_types=["current_inventory"],
        )
        run = _run(
            tools_used=["get_inventory"],
            goal_coverage={
                "extraction": "detected",
                "requirements": [
                    {"id": "r1", "type": "current_inventory", "status": "covered", "evidence_ids": ["e1"]}
                ],
            },
            agent_trace=[
                {"decision_index": 1, "action": "call_tool", "tool": "get_inventory"},
                {"decision_index": 2, "action": "final_answer", "blocked_final": False},
            ],
        )
        self.assertEqual(classify_failure(gold, run), "F")

    def test_resolver_short_circuit_is_f(self):
        gold = _gold(tools_expected=["get_inventory"], tool_count="exact", goal_types=["current_inventory"])
        run = _run(
            tools_used=[],
            agent_trace=[],
            scenario="context_ambiguous",
            needs_clarification=True,
            goal_coverage=None,
        )
        self.assertEqual(classify_failure(gold, run), "F")

    def test_diagnose_case_safe_fields(self):
        gold = _gold()
        run = _run(
            tools_used=["get_inventory"],
            fallback_reason=None,
            verifier_failures=0,
            latency_ms=1234,
            goal_coverage={
                "requirements": [
                    {"type": "stock_movements", "status": "uncovered", "evidence_ids": []},
                    {"type": "current_inventory", "status": "covered", "evidence_ids": ["e1"]},
                ]
            },
            agent_trace=[
                {"decision_index": 1, "action": "call_tool", "tool": "get_inventory"},
                {"decision_index": 2, "action": "final_answer", "blocked_final": False},
            ],
        )
        row = diagnose_case(gold, run, {"pass": False, "reasons": ["tools_got"]})
        self.assertEqual(row["case_id"], "T00")
        self.assertEqual(row["failure_class"], "B")
        self.assertEqual(row["actual_tools"], ["get_inventory"])
        self.assertIn("decisions", row)
        self.assertNotIn("reply", row)
        self.assertNotIn("prompt", row)
        blob = str(row)
        self.assertNotIn("Authorization", blob)
        self.assertNotIn("ANDES_LLM_API_KEY", blob)


if __name__ == "__main__":
    unittest.main()
