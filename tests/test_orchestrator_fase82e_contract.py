"""FASE 8.2E — el contrato de la respuesta y la supervivencia del informe.

Tres defectos encontrados al analizar la corrida con LLM real posterior a 8.2D,
los tres introducidos por mí en 8.2D:

1. ``evidence_summary`` ya era un campo PÚBLICO de la respuesta del orquestador
   —una lista de evidencias por herramienta— y reutilicé ese nombre para el
   resumen de degradación, que es un dict. Ambas claves caían en el MISMO
   literal, así que Python se quedó en silencio con la última y la lista pública
   desapareció en la ruta del agente: 57 de 66 turnos devolvieron la forma
   equivocada a cualquier consumidor.

2. El agregado del harness reventó sobre esa forma inesperada y una corrida
   completa de 66 casos con LLM real terminó sin escribir el informe. Los jsonl
   se salvaron sólo porque se escriben incrementalmente.

3. El eje de respuesta aceptaba contestaciones sin un solo dato concreto: el
   grounding castiga cifras FALSAS, nunca cifras AUSENTES, así que "se
   encontraron proveedores" pasaba todos los ejes.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from app.assistant.orchestrator.audit import OrchestratorAudit
from app.assistant.orchestrator.agent_loop import QueueDecisionClient
from app.assistant.orchestrator.planner import FakePlanner
from app.assistant.orchestrator.service import run_orchestrator_chat
from app.assistant.orchestrator.turn_store import TurnStore
from evals.fase81_scorer import _response_ok
from evals.fase81g_closure import _evidence_summary, _safely
from tests.test_orchestrator_fase81_agent import _call, _final, _invoke_router


class ResponseContractTests(unittest.TestCase):
    """evidence_summary es una lista. Siempre. Incluida la ruta del agente."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.audit = OrchestratorAudit(path=Path(self._tmpdir.name) / "audit.jsonl")
        self.store = TurnStore()
        self._env = patch.dict(os.environ, {
            "ANDES_ASSISTANT_AGENT_ENABLED": "0",
            "ANDES_ASSISTANT_NL_ENABLED": "0",
            "ANDES_ORCH_PLANNER": "fake",
            "ANDES_ASSISTANT_MEMORY_ENABLED": "0",
            "ANDES_ASSISTANT_HISTORY_ENABLED": "0",
        }, clear=False)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmpdir.cleanup()

    def _chat(self, message: str, **kwargs) -> dict[str, Any]:
        return run_orchestrator_chat(
            message=message,
            actor_user="albertadmin",
            conversation_id="c-82e",
            invoke_fn=_invoke_router,
            planner=FakePlanner(),
            audit=self.audit,
            turn_store=self.store,
            **kwargs,
        )

    def test_the_non_agent_path_returns_a_list(self):
        out = self._chat("Stock del 2404")
        self.assertIsInstance(out.get("evidence_summary"), list)

    @patch("app.assistant.orchestrator.service.agent_loop_allowed", return_value=True)
    def test_the_agent_path_also_returns_a_list(self, _allowed):
        """Este es el que se rompió. Un dict aquí cambia el contrato público."""
        client = QueueDecisionClient([
            _call("get_inventory", {"codigo": "2404"}),
            _final("El stock es 2.", eids=["e1"]),
        ])
        out = self._chat("Stock del 2404", agent_decision_client=client)
        self.assertTrue(out.get("agent_enabled"))
        self.assertIsInstance(out.get("evidence_summary"), list)

    @patch("app.assistant.orchestrator.service.agent_loop_allowed", return_value=True)
    def test_the_entries_keep_their_shape(self, _allowed):
        client = QueueDecisionClient([
            _call("get_inventory", {"codigo": "2404"}),
            _final("El stock es 2.", eids=["e1"]),
        ])
        out = self._chat("Stock del 2404", agent_decision_client=client)
        for entry in out.get("evidence_summary") or []:
            self.assertIsInstance(entry, dict)
            for key in ("tool", "ok", "empty", "error_code", "classification"):
                self.assertIn(key, entry, key)

    @patch("app.assistant.orchestrator.service.agent_loop_allowed", return_value=True)
    def test_degradation_travels_under_its_own_name(self, _allowed):
        client = QueueDecisionClient([
            _call("get_inventory", {"codigo": "2404"}),
            _final("El stock es 2.", eids=["e1"]),
        ])
        out = self._chat("Stock del 2404", agent_decision_client=client)
        degradation = out.get("evidence_degradation")
        self.assertIsInstance(degradation, dict)
        for key in ("items", "truncated_items", "omitted_rows_total", "degraded_tools"):
            self.assertIn(key, degradation, key)

    def test_the_two_keys_are_never_the_same_object(self):
        out = self._chat("Stock del 2404")
        self.assertNotEqual(type(out.get("evidence_summary")),
                            type(out.get("evidence_degradation")))

    def test_no_duplicate_keys_in_the_service_response_literals(self):
        """La causa raíz: una clave repetida dentro del mismo dict literal no es
        un error en Python, la última gana en silencio. Un linter no lo mira y un
        test de comportamiento sólo lo ve si mira justo ese campo."""
        import ast

        tree = ast.parse(Path("app/assistant/orchestrator/service.py").read_text(encoding="utf-8"))
        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            names = [k.value for k in node.keys
                     if isinstance(k, ast.Constant) and isinstance(k.value, str)]
            for name in set(names):
                if names.count(name) > 1:
                    offenders.append(f"{name} (linea {node.lineno})")
        self.assertEqual(offenders, [], f"claves duplicadas: {offenders}")


class HarnessSurvivalTests(unittest.TestCase):
    """Una métrica accesoria no puede costar el informe de una corrida con LLM."""

    def test_a_throwing_metric_yields_an_error_instead_of_propagating(self):
        out = _safely(lambda _runs: 1 / 0, [], default={})
        self.assertIsInstance(out, dict)
        self.assertIn("ZeroDivisionError", out.get("error", ""))

    def test_a_throwing_metric_with_a_text_default_stays_text(self):
        out = _safely(lambda _runs: 1 / 0, [], default="no medido")
        self.assertIsInstance(out, str)
        self.assertIn("no medido", out)

    def test_a_working_metric_passes_through_untouched(self):
        self.assertEqual(_safely(lambda runs: len(runs), [1, 2, 3], default={}), 3)

    def test_the_aggregate_reads_the_degradation_key_not_the_public_one(self):
        """Si vuelve a leer evidence_summary, recibe una lista y revienta."""
        runs = [{"evidence_summary": [{"tool": "get_inventory", "ok": True}],
                 "evidence_degradation": {"items": 1, "truncated_items": 1,
                                          "omitted_rows_total": 7,
                                          "degraded_tools": ["get_supplier"]}}]
        out = _evidence_summary(runs)
        self.assertEqual(out["cases_degraded"], 1)
        self.assertEqual(out["omitted_rows_total"], 7)
        self.assertEqual(out["degraded_tools"], ["get_supplier"])

    def test_a_run_carrying_the_old_shape_does_not_crash_the_aggregate(self):
        runs = [{"evidence_summary": [{"tool": "get_inventory"}]},
                {"evidence_degradation": None},
                {}]
        out = _evidence_summary(runs)
        self.assertEqual(out["cases_degraded"], 0)


class SubstantiveAnswerAxisTests(unittest.TestCase):
    """8.2E anadio `expect_figures`; 8.3 lo retiro. Se deja fijado por que.

    Era un parche por caso: habia que marcar a mano cada gold, asi que sólo podia
    detectar el defecto en los casos donde alguien ya sabia que estaba. Un
    mecanismo que exige conocer de antemano el fallo que busca no sirve de red.
    La senal general vive ahora en answer_sufficiency, medida contra la evidencia
    que el agente recupero de verdad y sin marcas por caso.
    """

    def test_the_per_case_flag_is_gone(self):
        from pathlib import Path as _P

        src = _P("evals/fase81_scorer.py").read_text(encoding="utf-8")
        self.assertNotIn('gold.get("expect_figures")', src)

    def test_no_gold_carries_the_retired_flag(self):
        from evals.fase81_runner import load_cases

        cases = load_cases(Path("evals/fase81_evidence_dataset.jsonl"))
        self.assertEqual([c["id"] for c in cases if "expect_figures" in c], [])

    def test_the_response_axis_is_back_to_its_original_contract(self):
        gold = {"expected_data_vs_inference": True}
        ok, _r = _response_ok(gold, {"reply": "DATOS:\n- Hay proveedores.",
                                     "fallback_used": False}, ["get_supplier"])
        self.assertTrue(ok)

    def test_the_datos_check_still_holds(self):
        gold = {"expected_data_vs_inference": True}
        ok, reasons = _response_ok(gold, {"reply": "Hay proveedores.",
                                          "fallback_used": False}, ["get_supplier"])
        self.assertFalse(ok)
        self.assertIn("missing_datos", reasons)


class DatasetIntentTests(unittest.TestCase):
    """G07 no puede ejercer el truncado y el dataset no debe fingir que si."""

    def _cases(self) -> list[dict[str, Any]]:
        from evals.fase81_runner import load_cases

        return load_cases(Path("evals/fase81_evidence_dataset.jsonl"))

    def test_g07_is_labelled_for_what_it_actually_validates(self):
        g07 = [c for c in self._cases() if c["id"] == "G07"]
        self.assertTrue(g07)
        self.assertEqual(g07[0].get("expected_behavior"), "supplier_broad_search")

    def test_real_truncation_is_owned_by_the_deterministic_probe(self):
        """El agente elige los argumentos, asi que el tamano del payload no es
        controlable desde el benchmark: en la corrida real el modelo paso un
        limit y no degrado nada. La cobertura de truncado real vive en
        probe_real_truncation, que usa 5226 caracteres del Gateway."""
        from evals.fase81g_closure import probe_real_truncation

        self.assertTrue(callable(probe_real_truncation))


if __name__ == "__main__":
    unittest.main()
