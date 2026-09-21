"""FASE 8.1G.2 — harness parity: closure runner vs reference runner.

Deterministic. No LLM, no Gateway, no network: both runners are driven through a
stubbed ``run_orchestrator_chat`` that records every turn they request, so the
comparison is of the CALL SEQUENCE each runner issues, not of model output.

The bug this locks down: evals/fase81g_closure.py did not execute the dataset's
``setup_turns``. F01/F06/Q01/Q04 are anaphoric follow-ups ("Ahora dime los
movimientos") whose antecedent only exists if the prior turn ran in the same
conversation, so skipping it silently turned 4 passing cases into failures that
looked like production regressions.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "evals" / "fase81_evidence_dataset.jsonl"

# The four cases the missing setup turns broke, plus two controls:
# T07 has no setup turns, M01 is a plain single-turn case.
PARITY_IDS = ["F01", "F06", "Q01", "Q04"]
CONTROL_IDS = ["T07", "M01"]


def _load_gold() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for line in DATASET.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            out[str(row.get("id"))] = row
    return out


def _canned_result() -> dict[str, Any]:
    return {
        "ok": True,
        "reply": "DATOS:\n- stub",
        "tools_used": ["get_inventory"],
        "fallback_used": False,
        "fallback_reason": None,
        "needs_clarification": False,
        "scenario": "agent_loop",
        "correlation_id": "stub",
        "agent_trace": [],
        "goal_coverage": {"extraction": "unknown", "requirements": []},
        "arg_errors": [],
        "verifier_failures": 0,
        "verifier_breakdown": {},
        "agent_progress": {},
        "cost_est": None,
    }


class _Recorder:
    """Stands in for run_orchestrator_chat and records the turn sequence."""

    def __init__(self) -> None:
        self.turns: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.turns.append(
            {
                "message": str(kwargs.get("message") or ""),
                "actor_user": kwargs.get("actor_user"),
                "conversation_id": kwargs.get("conversation_id"),
                "store_id": id(kwargs.get("turn_store")),
                "planner_id": id(kwargs.get("planner")),
            }
        )
        return _canned_result()

    def shape(self) -> dict[str, Any]:
        """Identity-free description of what the runner did for one case."""
        return {
            "messages": [t["message"] for t in self.turns],
            "actors": sorted({t["actor_user"] for t in self.turns}),
            "distinct_conversation_ids": len({t["conversation_id"] for t in self.turns}),
            "distinct_stores": len({t["store_id"] for t in self.turns}),
            "distinct_planners": len({t["planner_id"] for t in self.turns}),
        }


class _StubPlanner:
    def plan(self, message: str, *, context: Any = None) -> dict[str, Any]:  # pragma: no cover
        return {"plan_id": "stub", "steps": []}


def _stub_invoke(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:  # pragma: no cover
    return 200, {"ok": True, "tool": payload.get("tool"), "data": {}, "meta": {}}


class HarnessParityTests(unittest.TestCase):
    """Both runners must request the same turns for the same case."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.gold = _load_gold()

    def _patches(self, recorder: _Recorder):
        """Patch at the SOURCE modules: both runners import these function-locally."""
        return (
            patch("app.assistant.orchestrator.service.run_orchestrator_chat", recorder),
            patch(
                "app.assistant.orchestrator.factory.build_planner",
                lambda *a, **k: _StubPlanner(),
            ),
            patch("app.assistant.routes.invoke_gateway", _stub_invoke),
            patch(
                "app.assistant.orchestrator.agent_config.agent_loop_allowed",
                lambda *a, **k: True,
            ),
        )

    def _reference_shapes(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        """Drive evals/fase81_runner.py — the harness of record — per case."""
        from evals import fase81_runner

        shapes: dict[str, dict[str, Any]] = {}
        for cid in ids:
            recorder = _Recorder()
            p1, p2, p3, p4 = self._patches(recorder)
            with tempfile.TemporaryDirectory() as tmp, p1, p2, p3, p4, patch.dict(
                os.environ,
                {
                    "ANDES_AGENT_SERVICE_TOKEN": "stub-token",
                    "ANDES_LLM_API_KEY": "stub-key",
                    "ANDES_AGENT_URL": "http://127.0.0.1:5055",
                    "ANDES_ENV": "local",
                },
                clear=False,
            ):
                rc = fase81_runner.main(["--ids", cid, "--out-dir", tmp])
            self.assertEqual(rc, 0, f"reference runner failed for {cid}")
            shapes[cid] = recorder.shape()
        return shapes

    def _closure_shapes(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        from evals import fase81g_closure

        shapes: dict[str, dict[str, Any]] = {}
        for cid in ids:
            recorder = _Recorder()
            p1, p2, p3, p4 = self._patches(recorder)
            with p1, p2, p3, p4:
                fase81g_closure._run_case(self.gold[cid], "parity")
            shapes[cid] = recorder.shape()
        return shapes

    def test_closure_runner_matches_reference_for_setup_turn_cases(self):
        ids = PARITY_IDS
        ref = self._reference_shapes(ids)
        got = self._closure_shapes(ids)
        for cid in ids:
            self.assertEqual(got[cid], ref[cid], f"harness divergence on {cid}")

    def test_controls_without_setup_turns_also_match(self):
        ids = CONTROL_IDS
        ref = self._reference_shapes(ids)
        got = self._closure_shapes(ids)
        for cid in ids:
            self.assertEqual(got[cid], ref[cid], f"harness divergence on {cid}")

    def test_setup_turns_run_before_the_scored_prompt_in_dataset_order(self):
        got = self._closure_shapes(PARITY_IDS)
        for cid in PARITY_IDS:
            gold = self.gold[cid]
            expected = [str(s.get("prompt") or "") for s in gold.get("setup_turns") or []]
            expected.append(str(gold.get("prompt") or ""))
            self.assertEqual(got[cid]["messages"], expected, cid)
            self.assertGreaterEqual(len(expected), 2, f"{cid} should have a setup turn")

    def test_one_store_one_planner_one_conversation_per_case(self):
        got = self._closure_shapes(PARITY_IDS + CONTROL_IDS)
        for cid, shape in got.items():
            self.assertEqual(shape["distinct_stores"], 1, f"{cid} must share one TurnStore")
            self.assertEqual(shape["distinct_planners"], 1, f"{cid} must share one planner")
            self.assertEqual(shape["distinct_conversation_ids"], 1, f"{cid} one conversation")
            self.assertEqual(len(shape["actors"]), 1, f"{cid} one actor")

    def test_every_dataset_setup_turn_is_executed(self):
        """No case may silently lose its setup turns again."""
        with_setup = [cid for cid, g in self.gold.items() if g.get("setup_turns")]
        self.assertTrue(with_setup, "dataset should contain setup_turns cases")
        got = self._closure_shapes(with_setup)
        for cid in with_setup:
            want = len(self.gold[cid]["setup_turns"]) + 1
            self.assertEqual(len(got[cid]["messages"]), want, f"{cid} turn count")

    def test_actor_comes_from_the_gold_case(self):
        from evals import fase81g_closure

        gold = dict(self.gold["P03"])
        self.assertTrue(gold.get("actor"))
        recorder = _Recorder()
        p1, p2, p3, p4 = self._patches(recorder)
        with p1, p2, p3, p4:
            fase81g_closure._run_case(gold, "actor")
        self.assertEqual(recorder.shape()["actors"], [gold["actor"]])

    def test_latency_covers_setup_turns(self):
        """The reference runner starts its timer before the setup turns; so must we."""
        import time as _time

        from evals import fase81g_closure

        recorder = _Recorder()
        slow = _Recorder()

        def _slow(**kwargs: Any) -> dict[str, Any]:
            recorder(**kwargs)
            _time.sleep(0.02)
            return _canned_result()

        p2, p3, p4 = self._patches(slow)[1:]
        with patch("app.assistant.orchestrator.service.run_orchestrator_chat", _slow), p2, p3, p4:
            rec = fase81g_closure._run_case(self.gold["F01"], "lat")
        # 1 setup + 1 scored turn, each ~20ms: a timer started after the setup turn
        # could not exceed ~20ms.
        self.assertEqual(rec["setup_turns"], 1)
        self.assertGreaterEqual(int(rec["latency_ms"]), 30)


class EnvRestorationTests(unittest.TestCase):
    def test_closure_restores_safe_flags(self):
        from evals import fase81g_closure

        with patch.dict(
            os.environ,
            {
                "ANDES_ASSISTANT_AGENT_ENABLED": "1",
                "ANDES_ASSISTANT_NL_ENABLED": "1",
                "ANDES_ORCH_PLANNER": "llm",
            },
            clear=False,
        ):
            fase81g_closure._restore_safe_env()
            self.assertEqual(os.environ["ANDES_ASSISTANT_AGENT_ENABLED"], "0")
            self.assertEqual(os.environ["ANDES_ASSISTANT_NL_ENABLED"], "0")
            self.assertEqual(os.environ["ANDES_ORCH_PLANNER"], "fake")


if __name__ == "__main__":
    unittest.main()
