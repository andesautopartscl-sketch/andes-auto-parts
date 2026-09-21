"""FASE 8.1D — scorer/dataset family + FASE5 shortcircuit rules."""
from __future__ import annotations

import unittest

from evals.fase81_scorer import aggregate, score_case


class Fase81DScorerTests(unittest.TestCase):
    def test_check_stock_satisfies_current_inventory_family(self):
        gold = {
            "id": "C03",
            "bucket": "C",
            "expected_tools": ["get_inventory"],
            "required_goal_types": ["current_inventory"],
            "acceptable_tool_families": {
                "current_inventory": ["get_inventory", "check_stock"]
            },
            "tool_count_policy": "family",
            "expected_grounding": True,
            "expect_fallback": False,
        }
        run = {
            "tools_used": ["check_stock"],
            "fallback_used": False,
            "agent_trace": [],
            "goal_coverage": {
                "requirements": [
                    {"type": "current_inventory", "status": "covered", "evidence_ids": ["e1"]}
                ]
            },
            "reply": "DATOS: disponible",
            "verifier_failures": 0,
        }
        scored = score_case(gold, run)
        self.assertTrue(scored["tool_selection_score"])
        self.assertTrue(scored["pass"])

    def test_get_inventory_satisfies_current_inventory_family(self):
        gold = {
            "id": "C03b",
            "expected_tools": ["get_inventory"],
            "required_goal_types": ["current_inventory"],
            "acceptable_tool_families": {
                "current_inventory": ["get_inventory", "check_stock"]
            },
            "tool_count_policy": "family",
            "expected_grounding": True,
            "expect_fallback": False,
        }
        run = {
            "tools_used": ["get_inventory"],
            "fallback_used": False,
            "goal_coverage": {
                "requirements": [
                    {"type": "current_inventory", "status": "covered", "evidence_ids": ["e1"]}
                ]
            },
            "reply": "DATOS: 2",
            "verifier_failures": 0,
        }
        self.assertTrue(score_case(gold, run)["pass"])

    def test_optional_extra_allows_additional_tool(self):
        gold = {
            "id": "E06",
            "expected_tools": ["get_stock_movements"],
            "required_goal_types": ["stock_movements"],
            "acceptable_tool_families": {"stock_movements": ["get_stock_movements"]},
            "tool_count_policy": "optional_extra",
            "expected_grounding": True,
            "expected_data_vs_inference": True,
            "expect_fallback": False,
        }
        run = {
            "tools_used": ["get_stock_movements", "get_inventory"],
            "fallback_used": False,
            "goal_coverage": {
                "requirements": [
                    {"type": "stock_movements", "status": "covered", "evidence_ids": ["e1"]}
                ]
            },
            "reply": "DATOS: movimientos",
            "verifier_failures": 0,
            "agent_trace": [
                {"decision_index": 1, "evidence_count_before": 0},
                {"decision_index": 2, "evidence_count_before": 1},
            ],
        }
        scored = score_case(gold, run)
        self.assertTrue(scored["tool_selection_score"])
        self.assertTrue(scored["pass"])

    def test_clarify_ok_on_ambiguous_followup(self):
        gold = {
            "id": "F03",
            "expected_tools": ["get_inventory"],
            "required_goal_types": ["current_inventory"],
            "acceptable_tool_families": {
                "current_inventory": ["get_inventory", "check_stock"]
            },
            "tool_count_policy": "family",
            "allow_fase5_shortcircuit": True,
            "acceptable_outcomes": ["agent_tools", "fase5_clarify", "clarify"],
            "expected_grounding": True,
            "expect_fallback": False,
        }
        run = {
            "tools_used": [],
            "needs_clarification": True,
            "scenario": "context_ambiguous",
            "fallback_used": False,
            "agent_trace": [],
            "goal_coverage": None,
            "reply": "¿Cuál código?",
            "verifier_failures": 0,
        }
        self.assertTrue(score_case(gold, run)["pass"])

    def test_context_reuse_ok_with_prior_evidence(self):
        gold = {
            "id": "F02",
            "expected_tools": ["get_inventory"],
            "required_goal_types": ["current_inventory"],
            "acceptable_tool_families": {
                "current_inventory": ["get_inventory", "check_stock"]
            },
            "tool_count_policy": "family",
            "allow_fase5_shortcircuit": True,
            "acceptable_outcomes": ["agent_tools", "fase5_reuse"],
            "expected_grounding": True,
            "expect_fallback": False,
        }
        run = {
            "tools_used": [],
            "scenario": "context_reuse",
            "fallback_used": False,
            "agent_trace": [],
            "goal_coverage": None,
            "reply": "Stock de 2404 Total: 2",
            "verifier_failures": 0,
        }
        self.assertTrue(score_case(gold, run)["pass"])

    def test_empty_not_found_not_penalized(self):
        gold = {
            "id": "N04",
            "expected_tools": ["get_inventory"],
            "required_goal_types": ["current_inventory"],
            "acceptable_tool_families": {
                "current_inventory": ["get_inventory", "check_stock"]
            },
            "tool_count_policy": "family",
            "empty_not_found_ok": True,
            "expected_grounding": True,
            "expect_fallback": False,
        }
        run = {
            "tools_used": ["get_inventory"],
            "fallback_used": False,
            "goal_coverage": {
                "requirements": [
                    {"type": "current_inventory", "status": "uncovered", "evidence_ids": []}
                ]
            },
            "reply": "DATOS: no existe",
            "verifier_failures": 0,
            "agent_trace": [
                {"decision_index": 1, "action": "call_tool", "tool": "get_inventory"},
                {"decision_index": 2, "action": "final_answer", "blocked_final": True},
            ],
        }
        scored = score_case(gold, run)
        self.assertTrue(scored["goal_coverage_score"])
        self.assertTrue(scored["pass"])

    def test_family_rejects_other_family_tool(self):
        gold = {
            "id": "Xfam",
            "expected_tools": ["get_inventory"],
            "required_goal_types": ["current_inventory"],
            "acceptable_tool_families": {
                "current_inventory": ["get_inventory", "check_stock"]
            },
            "tool_count_policy": "family",
            "expected_grounding": True,
            "expect_fallback": False,
        }
        run = {
            "tools_used": ["get_customer"],
            "fallback_used": False,
            "goal_coverage": {
                "requirements": [
                    {"type": "current_inventory", "status": "uncovered", "evidence_ids": []}
                ]
            },
            "reply": "DATOS: cliente",
            "verifier_failures": 0,
        }
        scored = score_case(gold, run)
        self.assertFalse(scored["tool_selection_score"])
        self.assertFalse(scored["pass"])

    def test_true_agent_baselines_still_fail(self):
        # K05 must remain exact check_stock (not family).
        gold = {
            "id": "K05",
            "baseline_failure": "agent",
            "expected_tools": ["check_stock"],
            "tool_count_policy": "exact",
            "required_goal_types": [],
            "expected_grounding": True,
            "expected_data_vs_inference": True,
            "expect_fallback": False,
        }
        run = {
            "tools_used": ["get_inventory"],
            "fallback_used": False,
            "reply": "DATOS: 2",
            "verifier_failures": 0,
            "goal_coverage": {"requirements": []},
        }
        scored = score_case(gold, run)
        self.assertFalse(scored["pass"])
        self.assertTrue(scored["agent_true_failure"])


class K05FamilyPolicyTests(unittest.TestCase):
    """FASE 8.1K — la politica del gold de K05, protegida.

    check_stock y get_inventory son la misma familia current_inventory y ambas
    responden "hay N unidades?". Ademas check_stock devuelve disponible:0 para un
    codigo inexistente, es decir reporta ausencia como cero; get_inventory
    devuelve not_found. Exigir check_stock exacto medía preferencia de tool, y
    ademas apuntaba a la de peor semantica de nulos.
    """

    def _gold(self):
        import json
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        for line in (root / "evals" / "fase81_evidence_dataset.jsonl").read_text(
            encoding="utf-8"
        ).splitlines():
            if line.strip():
                row = json.loads(line)
                if row.get("id") == "K05":
                    return row
        raise AssertionError("K05 no esta en el dataset")

    def _run(self, tools):
        return {
            "tools_used": list(tools), "fallback_used": False, "fallback_reason": None,
            "reply": "DATOS: hay 2 unidades del 2404.", "verifier_failures": 0,
            "goal_coverage": {"requirements": [
                {"type": "current_inventory", "status": "covered", "evidence_ids": ["e1"]}]},
            "verifier_breakdown": {},
        }

    def test_the_gold_accepts_the_whole_family(self):
        gold = self._gold()
        self.assertEqual(gold["tool_count_policy"], "family")
        self.assertEqual(
            set(gold["acceptable_tool_families"]["current_inventory"]),
            {"get_inventory", "check_stock"},
        )

    def test_either_family_member_satisfies_the_case(self):
        gold = self._gold()
        for tool in ("get_inventory", "check_stock"):
            with self.subTest(tool=tool):
                self.assertTrue(score_case(gold, self._run([tool]))["pass"], tool)

    def test_a_tool_outside_the_family_still_fails(self):
        """Ampliar la familia no es aflojar el caso."""
        gold = self._gold()
        scored = score_case(gold, self._run(["get_dashboard_kpis"]))
        self.assertFalse(scored["pass"])

    def test_answering_without_any_tool_still_fails(self):
        gold = self._gold()
        run = self._run([])
        run["goal_coverage"] = {"requirements": [
            {"type": "current_inventory", "status": "uncovered", "evidence_ids": []}]}
        self.assertFalse(score_case(gold, run)["pass"])


class ProvenanceReadinessTests(unittest.TestCase):
    """FASE 8.2 — el gate que decide si se puede encender el enforcing."""

    def _gold(self):
        return {"id": "X", "bucket": "X", "expected_tools": ["get_inventory"],
                "tool_count_policy": "minimum", "expected_grounding": True,
                "expect_fallback": False}

    def _run(self, breakdown):
        return {"tools_used": ["get_inventory"], "fallback_used": False,
                "fallback_reason": None, "reply": "DATOS: 2 unidades.",
                "verifier_failures": 0, "goal_coverage": {"requirements": []},
                "verifier_breakdown": breakdown}

    def test_unmeasured_runs_are_never_reported_as_ready(self):
        """Una corrida anterior a 8.2 no lleva la señal: 0 violaciones y 'nunca se
        midió' no pueden confundirse, o el gate diría listo por datos ausentes."""
        agg = aggregate([score_case(self._gold(), self._run({}))])
        self.assertIn("unknown", agg["provenance"]["enforcement_ready"])
        self.assertEqual(agg["provenance"]["measured_cases"], 0)

    def test_high_severity_blocks_enforcement(self):
        agg = aggregate([score_case(self._gold(), self._run(
            {"provenance_violations": 1, "provenance_severity": "high",
             "provenance_by_kind": {"unknown_evidence_id": 1},
             "provenance_enforced": False}))])
        self.assertIn("blocked", agg["provenance"]["enforcement_ready"])
        self.assertEqual(agg["provenance"]["high_severity_cases"], 1)

    def test_published_claims_at_risk_block_enforcement(self):
        agg = aggregate([score_case(self._gold(), self._run(
            {"provenance_violations": 2, "provenance_severity": "medium",
             "provenance_by_kind": {"figure_outside_cited_evidence": 2},
             "provenance_enforced": False}))])
        self.assertIn("blocked", agg["provenance"]["enforcement_ready"])
        self.assertEqual(agg["provenance"]["published_claims_at_risk"], 2)

    def test_a_fully_measured_clean_run_is_ready(self):
        agg = aggregate([score_case(self._gold(), self._run(
            {"provenance_violations": 0, "provenance_severity": "none",
             "provenance_by_kind": {}, "provenance_enforced": False}))])
        self.assertEqual(agg["provenance"]["enforcement_ready"], "ready")

    def test_provenance_never_changes_the_verdict(self):
        """La señal se reporta; no puntúa. Enforcing lo decide el flag, no el scorer."""
        dirty = score_case(self._gold(), self._run(
            {"provenance_violations": 3, "provenance_severity": "high",
             "provenance_by_kind": {"unknown_evidence_id": 3}, "provenance_enforced": False}))
        self.assertTrue(dirty["pass"])


class VerifierAxisTests(unittest.TestCase):
    """FASE 8.1J — the verifier axis measures the OUTCOME, not the process.

    A guardrail activation means the model proposed something it could not
    support and the verifier removed it before the user saw it. Scoring that as
    a case failure penalizes exactly the behaviour the system exists to have.
    """

    def _gold(self, **extra):
        base = {
            "id": "X", "bucket": "X", "expected_tools": ["get_inventory"],
            "tool_count_policy": "minimum", "expected_grounding": True,
            "expect_fallback": False,
        }
        base.update(extra)
        return base

    def _run(self, **extra):
        base = {
            "tools_used": ["get_inventory"], "fallback_used": False, "fallback_reason": None,
            "reply": "DATOS: El stock del 2404 es 2 unidades.", "verifier_failures": 0,
            "goal_coverage": {"requirements": []}, "verifier_breakdown": {},
        }
        base.update(extra)
        return base

    def test_a_dropped_claim_with_a_grounded_answer_passes(self):
        scored = score_case(
            self._gold(),
            self._run(verifier_failures=1,
                      verifier_breakdown={"dropped_claims": 1, "answer_replaced": False}),
        )
        self.assertTrue(scored["verifier_score"])
        self.assertTrue(scored["pass"])
        self.assertEqual(scored["guardrail_events"]["dropped_claims"], 1)

    def test_a_failed_calculation_alone_does_not_fail_the_case(self):
        scored = score_case(
            self._gold(),
            self._run(verifier_failures=1,
                      verifier_breakdown={"calc_unresolved": 1, "answer_replaced": False}),
        )
        self.assertTrue(scored["verifier_score"])
        self.assertEqual(scored["guardrail_events"]["calc_unresolved"], 1)

    def test_a_replaced_answer_still_fails_when_the_gold_forbids_a_fallback(self):
        scored = score_case(
            self._gold(),
            self._run(verifier_failures=2, fallback_used=True,
                      fallback_reason="agent_verifier_failed",
                      verifier_breakdown={"dropped_claims": 1, "dropped_draft": 1,
                                          "answer_replaced": True}),
        )
        self.assertFalse(scored["verifier_score"])
        self.assertIn("verifier_replaced_the_answer", scored["reasons"])
        self.assertFalse(scored["pass"])

    def test_a_replaced_answer_passes_when_the_gold_tolerates_a_fallback(self):
        scored = score_case(
            self._gold(fallback_ok=True),
            self._run(verifier_failures=2, fallback_used=True,
                      fallback_reason="agent_verifier_failed",
                      verifier_breakdown={"dropped_claims": 1, "dropped_draft": 1,
                                          "answer_replaced": True}),
        )
        self.assertTrue(scored["verifier_score"])

    def test_an_invented_number_reaching_the_user_still_fails(self):
        """The outcome axis is not a licence: grounding is checked independently."""
        scored = score_case(
            self._gold(),
            self._run(reply="DATOS: El stock del 2404 es 8888.", verifier_failures=0),
        )
        self.assertFalse(scored["pass"])
        self.assertIn("invented_number", scored["reasons"])

    def test_guardrail_events_are_reported_in_the_aggregate(self):
        scores = [
            score_case(self._gold(),
                       self._run(verifier_failures=1,
                                 verifier_breakdown={"dropped_claims": 1})),
            score_case(self._gold(),
                       self._run(verifier_failures=1,
                                 verifier_breakdown={"calc_mismatch": 1})),
        ]
        agg = aggregate(scores)
        self.assertEqual(agg["guardrail_events"]["dropped_claims"], 1)
        self.assertEqual(agg["guardrail_events"]["calc_mismatch"], 1)
        # the raw counter is preserved so nothing is hidden by the redefinition
        self.assertEqual(agg["verifier_failures"], 2)


if __name__ == "__main__":
    unittest.main()
