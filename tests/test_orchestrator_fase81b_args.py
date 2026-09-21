"""FASE 8.1B — argument contract, normalize, structured invalid_args retry."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.assistant.orchestrator.agent_loop import QueueDecisionClient
from app.assistant.orchestrator.agent_schema import AgentDecisionError, validate_agent_decision
from app.assistant.orchestrator.audit import OrchestratorAudit
from app.assistant.orchestrator.catalog import ALLOWED_TOOLS
from app.assistant.orchestrator.planner import FakePlanner
from app.assistant.orchestrator.service import run_orchestrator_chat
from app.assistant.orchestrator.tool_contracts import (
    TOOL_CONTRACTS,
    normalize_agent_args,
    structured_invalid_args,
)
from app.assistant.orchestrator.turn_store import TurnStore
from evals.fase81_runner import load_cases
from evals.fase81_scorer import score_case
from tests.test_orchestrator_fase81_agent import _call, _final, _invoke_router

P2 = "Revisa los movimientos del 2404 y después dime cuánto stock hay."


class Fase81BArgsTests(unittest.TestCase):
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
            conversation_id="c-81b",
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

    def test_contracts_cover_every_allowlisted_tool(self):
        """El numero crece; lo que no puede pasar es que las listas divergan.

        8.6 anadio get_sales (primera capacidad de DEMANDA) y 8.8
        get_equivalences (cruce OEM, 72% de datos reales). La igualdad de
        conjuntos es el invariante; el conteo solo evita que alguien anada una
        tool sin darse cuenta."""
        self.assertEqual(set(TOOL_CONTRACTS), set(ALLOWED_TOOLS))
        self.assertGreaterEqual(len(TOOL_CONTRACTS), 12)

    def test_promote_q_code_to_codigo(self):
        out = normalize_agent_args(
            "get_stock_movements",
            {"q": "2404", "limit": 100, "fecha_desde": "2024-04-01", "fecha_hasta": "2024-04-30"},
            user_message=P2,
        )
        self.assertEqual(out.get("codigo"), "2404")
        self.assertNotIn("q", out)
        self.assertNotIn("limit", out)
        self.assertNotIn("fecha_desde", out)
        self.assertNotIn("fecha_hasta", out)

    def test_coerce_numeric_codigo_and_limit_string(self):
        out = normalize_agent_args(
            "get_inventory",
            {"codigo": 2404, "limit": "10"},
            user_message="Stock del 2404",
        )
        self.assertEqual(out.get("codigo"), "2404")
        dec = validate_agent_decision(
            {
                "action": "call_tool",
                "tool": "get_inventory",
                "arguments": {"codigo": 2404},
                "reason": "stock",
            },
            user_message="Stock del 2404",
        )
        self.assertEqual(dec["arguments"]["codigo"], "2404")
        lim = normalize_agent_args("search_catalog", {"q": "filtro", "limit": "10"})
        self.assertEqual(lim.get("limit"), 10)

    def test_do_not_invent_zero_from_unknown(self):
        out = normalize_agent_args(
            "get_stock_movements",
            {"codigo": "2404", "limit": "desconocido"},
            user_message=P2,
        )
        self.assertEqual(out.get("codigo"), "2404")
        self.assertNotIn("limit", out)

    def test_keep_date_only_if_user_wrote_it(self):
        msg = "Movimientos del 2404 desde 2026-03-01 hasta 2026-03-31"
        out = normalize_agent_args(
            "get_stock_movements",
            {"codigo": "2404", "fecha_desde": "2026-03-01", "fecha_hasta": "2026-03-31"},
            user_message=msg,
        )
        self.assertEqual(out.get("fecha_desde"), "2026-03-01")
        self.assertEqual(out.get("fecha_hasta"), "2026-03-31")

    def test_structured_invalid_args_missing_codigo(self):
        details = structured_invalid_args("get_stock_movements", {"q": "movimientos"})
        self.assertEqual(details["error"], "invalid_args")
        self.assertEqual(details["tool"], "get_stock_movements")
        self.assertEqual(details["fields"].get("codigo"), "required")
        with self.assertRaises(AgentDecisionError) as ctx:
            validate_agent_decision(
                {
                    "action": "call_tool",
                    "tool": "get_stock_movements",
                    "arguments": {"q": "no es un codigo"},
                    "reason": "mov",
                },
                user_message=P2,
            )
        self.assertEqual(ctx.exception.code, "invalid_args")
        self.assertEqual((ctx.exception.details or {}).get("fields", {}).get("codigo"), "required")

    @patch("app.assistant.orchestrator.service.agent_loop_allowed", return_value=True)
    def test_p2_union_bag_recovers_and_second_tool(self, _allowed):
        raw_union = {
            "action": "call_tool",
            "tool": "get_stock_movements",
            "arguments": {
                "q": "2404",
                "limit": 100,
                "codigo": None,
                "fecha_desde": "2024-04-01",
                "fecha_hasta": "2024-04-30",
            },
            "reason": "mov",
        }
        client = QueueDecisionClient(
            [
                raw_union,
                _call("get_inventory", {"codigo": "2404"}),
                _final("2404 tiene 25 unidades", eids=["e1", "e2"]),
            ]
        )
        out = self._chat(P2, agent_decision_client=client)
        self.assertTrue(out.get("ok"))
        self.assertEqual(out.get("tools_used"), ["get_stock_movements", "get_inventory"])
        self.assertFalse(out.get("fallback_used"))

    @patch("app.assistant.orchestrator.service.agent_loop_allowed", return_value=True)
    def test_structured_retry_then_fallback(self, _allowed):
        seen: list[str] = []

        class _Probe(QueueDecisionClient):
            def complete_decision(self, *, system: str, user: str):
                if '"error":"invalid_args"' in user.replace(" ", ""):
                    seen.append("structured")
                return super().complete_decision(system=system, user=user)

        client = _Probe(
            [
                {
                    "action": "call_tool",
                    "tool": "get_stock_movements",
                    "arguments": {"q": "no es un codigo"},
                    "reason": "bad",
                },
                {
                    "action": "call_tool",
                    "tool": "get_stock_movements",
                    "arguments": {"q": "sigue igual de mal"},
                    "reason": "bad2",
                },
            ]
        )
        out = self._chat(P2, agent_decision_client=client)
        self.assertTrue(out.get("ok"))
        self.assertTrue(out.get("fallback_used"))
        self.assertEqual(out.get("fallback_reason"), "invalid_args")
        self.assertTrue(seen)
        self.assertTrue(out.get("arg_errors"))
        err = (out.get("arg_errors") or [{}])[0]
        self.assertEqual(err.get("error"), "invalid_args")
        self.assertIn("codigo", err.get("fields") or {})
        blob = json.dumps(out.get("arg_errors"))
        self.assertNotIn("ANDES_LLM_API_KEY", blob)
        self.assertNotIn("Bearer", blob)

    def test_dataset_shape_is_pinned(self):
        """El dataset crece; lo que no puede cambiar en silencio es su forma.

        Fijar 64 obligaba a tocar el test cada vez que se anade un caso, que es
        justo el incentivo equivocado: invita a NO anadir cobertura. Se fija el
        minimo por bucket —nadie puede borrar casos sin que salte— y se exige que
        cada id sea unico y cada bucket conocido. 8.2D anadio T09 (3 herramientas,
        el unico caso que ejerce el techo real de decisiones) y G07 (busqueda
        amplia de proveedores, el unico que degrada evidencia con datos reales).
        """
        from pathlib import Path

        cases = load_cases(Path("evals/fase81_evidence_dataset.jsonl"))
        counts = {}
        for row in cases:
            counts[row["bucket"]] = counts.get(row["bucket"], 0) + 1
        # 8.5 anade el bucket A (analisis): ningun caso pedia proyectar, asi que
        # la escalera analitica no tenia donde ejercitarse ni con el flag on.
        floor = {"C": 6, "G": 7, "T": 9, "F": 6, "E": 4, "K": 6, "R": 4,
                 "N": 4, "Z": 4, "P": 4, "X": 4, "M": 4, "Q": 4, "A": 3, "V": 3, "O": 4}
        self.assertEqual(set(counts), set(floor), "bucket desconocido o desaparecido")
        for bucket, minimum in floor.items():
            self.assertGreaterEqual(counts[bucket], minimum, bucket)
        self.assertEqual(len(cases), sum(counts.values()))
        ids = [row["id"] for row in cases]
        self.assertEqual(len(ids), len(set(ids)), "ids duplicados en el dataset")
        self.assertGreaterEqual(len(cases), 76)

    def test_the_dataset_asks_for_a_projection(self):
        """Sin una sola pregunta de proyeccion, la escalera de 8.5 no podia
        medirse aunque estuviera habilitada: la corrida daria cero usos y eso
        pareceria un fallo del modelo cuando seria un hueco del benchmark."""
        from pathlib import Path

        cases = load_cases(Path("evals/fase81_evidence_dataset.jsonl"))
        analytic = [c for c in cases if c.get("analysis_expected")]
        self.assertGreaterEqual(len(analytic), 3)
        for case in analytic:
            self.assertGreaterEqual(len(case.get("expected_tools") or []), 2, case["id"])

    def test_the_dataset_exercises_the_real_decision_ceiling(self):
        """Ningun caso llegaba a 3 herramientas, asi que el techo de decisiones
        que paga el presupuesto de tokens nunca se ejercitaba: el benchmark podia
        estar al 98% sin haber tocado nunca el limite real del sistema."""
        from pathlib import Path

        cases = load_cases(Path("evals/fase81_evidence_dataset.jsonl"))
        widest = max(len(c.get("expected_tools") or []) for c in cases)
        self.assertGreaterEqual(widest, 3, "el dataset no ejerce ningun caso de 3 tools")
        scored = score_case(
            {
                "id": "T01",
                "bucket": "T",
                "expected_tools": ["get_stock_movements", "get_inventory"],
                "tool_count_policy": "exact",
                "required_goal_types": ["stock_movements", "current_inventory"],
                "acceptable_tool_families": {
                    "stock_movements": ["get_stock_movements"],
                    "current_inventory": ["get_inventory", "check_stock"],
                },
                "expected_grounding": True,
                "expected_data_vs_inference": True,
                "expected_null_not_zero": False,
                "expected_write": "none",
                "expect_fallback": False,
            },
            {
                "tools_used": ["get_stock_movements", "get_inventory"],
                "reply": "DATOS: 2404 tiene 25. INFERENCIA: estable.",
                "fallback_used": False,
                "agent_trace": [
                    {"decision_index": 1, "evidence_count_before": 0},
                    {"decision_index": 2, "evidence_count_before": 1},
                ],
                "goal_coverage": {
                    "requirements": [
                        {"type": "stock_movements", "status": "covered"},
                        {"type": "current_inventory", "status": "covered"},
                    ]
                },
                "arg_errors": [],
            },
        )
        self.assertTrue(scored["pass"])
        self.assertTrue(scored["tool_selection_score"])
        self.assertTrue(scored["goal_coverage_score"])


if __name__ == "__main__":
    unittest.main()
