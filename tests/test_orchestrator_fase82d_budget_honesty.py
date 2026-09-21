"""FASE 8.2D — que los límites declarados y los medidos digan lo mismo.

La corrida 8.2C con LLM real dejó tres mentiras pequeñas, cada una inofensiva
por separado y juntas capaces de hacer que el sistema se mida a sí mismo mal:

- el gate de procedencia contaba como "sin medir" turnos que NUNCA pueden emitir
  claims, así que su denominador no podía alcanzarse y el gate no abría ni con
  datos perfectos;
- ``decisions_affordable`` asumía 4 caracteres por token y un pack de evidencia
  vacío, y dividía techo entre coste unitario ignorando que el guard se evalúa
  ANTES de cada decisión. Las tres suposiciones tiraban en la misma dirección:
  hacia una cota optimista contra la que cualquier MAX_AGENT_STEPS pasaba;
- el benchmark no tenía ningún caso de 3 herramientas ni ninguno que degradara
  evidencia con datos reales, de modo que el techo real de decisiones y la ruta
  de truncado podían romperse sin que nada lo notara.
"""
from __future__ import annotations

import json
import os
import unittest
from typing import Any
from unittest.mock import patch

from app.assistant.orchestrator.agent_config import (
    CHARS_PER_TOKEN,
    MAX_AGENT_STEPS,
    MAX_BLOCKED_FINALS_NO_PROGRESS,
    MAX_TOOL_RESULT_CHARS,
    TOKEN_BUDGET_FALLBACK,
    budget_snapshot,
    decisions_affordable,
)
from app.assistant.orchestrator.evidence_store import EvidenceStore
from app.assistant.orchestrator.llm.client import _extract_usage
from evals.fase81_scorer import _enforcement_ready, _provenance


def _score(prov: dict[str, Any]) -> dict[str, Any]:
    return {"provenance": prov}


def _run(vb: dict[str, Any] | None) -> dict[str, Any]:
    return {"verifier_breakdown": vb}


class ProvenanceGateDenominatorTests(unittest.TestCase):
    """El denominador son los casos que PUEDEN violar procedencia."""

    def test_a_turn_without_verifier_is_not_applicable(self):
        prov = _provenance(_run({}))
        self.assertFalse(prov["applicable"])
        self.assertFalse(prov["measured"])

    def test_a_turn_with_the_signal_is_applicable_and_measured(self):
        prov = _provenance(_run({"provenance_violations": 0, "failures": 0}))
        self.assertTrue(prov["applicable"])
        self.assertTrue(prov["measured"])

    def test_a_pre_82_breakdown_is_applicable_but_unmeasured(self):
        """Esta es la distinción que protege el gate: el verifier corrió, así que
        pudo haber violaciones, pero la señal no estaba. Eso NO es cero."""
        prov = _provenance(_run({"failures": 0, "dropped_claims": 0}))
        self.assertTrue(prov["applicable"])
        self.assertFalse(prov["measured"])

    def test_clean_measurable_cases_open_the_gate(self):
        scores = [_score(_provenance(_run({"provenance_violations": 0}))) for _ in range(50)]
        scores += [_score(_provenance(_run({}))) for _ in range(14)]
        self.assertEqual(_enforcement_ready(scores), "ready")

    def test_a_missing_signal_on_an_applicable_case_keeps_it_unknown(self):
        scores = [_score(_provenance(_run({"provenance_violations": 0}))) for _ in range(49)]
        scores.append(_score(_provenance(_run({"failures": 0}))))
        self.assertIn("unknown", _enforcement_ready(scores))

    def test_high_severity_still_blocks(self):
        scores = [_score(_provenance(_run(
            {"provenance_violations": 1, "provenance_severity": "high"})))]
        self.assertIn("blocked", _enforcement_ready(scores))

    def test_published_claims_at_risk_still_block(self):
        scores = [_score(_provenance(_run(
            {"provenance_violations": 2, "provenance_severity": "medium",
             "provenance_enforced": False})))]
        self.assertIn("blocked", _enforcement_ready(scores))

    def test_no_applicable_case_is_unknown_not_ready(self):
        self.assertIn("unknown", _enforcement_ready([_score(_provenance(_run({})))]))

    def test_an_empty_run_set_is_unknown(self):
        self.assertIn("unknown", _enforcement_ready([]))


class BudgetModelTests(unittest.TestCase):
    """La cota tiene que predecir lo que el proveedor facturó de verdad."""

    def test_the_model_matches_the_measured_turn(self):
        """K06 con LLM real: 16 654 caracteres de prompt en 3 decisiones y 6 180
        tokens facturados. Si el ratio se desvía, toda cota construida encima
        miente, que es exactamente lo que pasaba con el 4 teórico."""
        self.assertAlmostEqual(16654 / CHARS_PER_TOKEN, 6180, delta=6180 * 0.03)

    def test_four_chars_per_token_would_underestimate(self):
        self.assertLess(16654 / 4, 6180 * 0.75)

    def test_the_bounds_bracket_the_observed_maximum(self):
        """Maximo observado con LLM real: 4 decisiones (T09, 3 tools + final)."""
        worst, best = decisions_affordable()
        self.assertLessEqual(worst, 4)
        self.assertGreaterEqual(best, 4)

    def test_the_bound_is_derived_not_asserted(self):
        """La cota vale por como se calcula, no por el numero que da.

        La version original asumia pack vacio y 4 caracteres por token; las dos
        eran falsas. Lo que se fija aqui es que sigue usando el ratio medido y
        la semantica real del guard, no una constante comoda."""
        from app.assistant.orchestrator.agent_config import _decisions_under_budget

        with_pack = _decisions_under_budget(MAX_TOOL_RESULT_CHARS, 800)
        without = _decisions_under_budget(0, 200)
        self.assertLess(with_pack, without,
                        "un pack que crece TIENE que costar decisiones")

    def test_the_worst_case_is_never_zero(self):
        worst, _best = decisions_affordable()
        self.assertGreaterEqual(worst, 1)

    def test_the_snapshot_exposes_the_calibration_and_the_mismatch(self):
        snap = budget_snapshot()
        self.assertEqual(snap["chars_per_token"], CHARS_PER_TOKEN)
        self.assertIn("steps_exceed_budget", snap)
        self.assertIsInstance(snap["steps_exceed_budget"], bool)

    def test_the_declared_steps_satisfy_the_lower_bound(self):
        """El camino más largo por diseño: 3 finales bloqueados + la tool que los
        resuelve + el final."""
        self.assertGreaterEqual(MAX_AGENT_STEPS, MAX_BLOCKED_FINALS_NO_PROGRESS + 2)

    def test_the_budget_funds_the_longest_designed_path(self):
        """DEUDA CERRADA en 8.6. Este test decia lo contrario y llevaba la nota
        "si esto deja de cumplirse, la deuda se cerro": se cerro.

        La medida que lo forzo: T09 cubrio sus tres requirements y aun asi cayo
        con agent_token_budget. El techo estaba matando trabajo correcto. Si el
        sistema declara que existe un camino de N decisiones, el presupuesto
        tiene que pagarlo; si no, el limite declarado es decorativo.

        Ahora falla si alguien sube MAX_AGENT_STEPS sin subir el techo, o encoge
        el techo sin bajar los pasos."""
        _worst, best = decisions_affordable()
        needed = MAX_BLOCKED_FINALS_NO_PROGRESS + 2
        self.assertEqual(MAX_AGENT_STEPS, needed)
        self.assertGreaterEqual(best, needed,
                                "el presupuesto ya no paga el camino declarado")
        self.assertFalse(budget_snapshot()["steps_exceed_budget"])

    def test_the_effective_ceiling_covers_what_was_actually_spent(self):
        """Medido con LLM real: T09 cerro en 8614 tokens con el techo declarado
        en 8000, y era correcto. El guard mira el acumulado ANTES de decidir, asi
        que concede una decision mas mientras lo gastado quepa. Publicar solo el
        techo declarado hacia parecer un desbordo lo que es el diseno."""
        snap = budget_snapshot()
        self.assertGreater(snap["effective_token_ceiling"], TOKEN_BUDGET_FALLBACK)
        # Envolvente observada con LLM real: 9858 tokens (T09).
        self.assertGreaterEqual(snap["effective_token_ceiling"], 9858)

    def test_the_effective_ceiling_is_derived_not_guessed(self):
        from app.assistant.orchestrator.agent_config import (
            MAX_EVIDENCE_PROMPT_CHARS, MAX_OUTPUT_TOKENS, _max_decision_cost)

        self.assertGreater(_max_decision_cost(), MAX_OUTPUT_TOKENS)
        self.assertGreater(_max_decision_cost(),
                           int(MAX_EVIDENCE_PROMPT_CHARS / CHARS_PER_TOKEN))

    def test_raising_the_budget_would_buy_decisions(self):
        """Comprueba que el modelo es monótono: sirve para decidir con evidencia
        cuánto habría que subir el techo, en vez de tantear."""
        from app.assistant.orchestrator import agent_config as cfg

        base = cfg._decisions_under_budget(0, 200)
        original = cfg.TOKEN_BUDGET_FALLBACK
        try:
            cfg.TOKEN_BUDGET_FALLBACK = original * 2
            self.assertGreater(cfg._decisions_under_budget(0, 200), base)
        finally:
            cfg.TOKEN_BUDGET_FALLBACK = original


class CachedTokenTests(unittest.TestCase):
    """El prefijo reenviado se observa; todavía no puntúa."""

    def test_openai_shape_is_read(self):
        usage = {"prompt_tokens": 1700, "completion_tokens": 150, "total_tokens": 1850,
                 "prompt_tokens_details": {"cached_tokens": 1024}}
        self.assertEqual(_extract_usage({"usage": usage})["cached_tokens"], 1024)

    def test_anthropic_shape_is_read(self):
        usage = {"input_tokens": 1700, "output_tokens": 150,
                 "cache_read_input_tokens": 1500}
        self.assertEqual(_extract_usage({"usage": usage})["cached_tokens"], 1500)

    def test_absence_is_not_zero(self):
        """Un proveedor que no reporta cache no puede parecer un 0% de cache."""
        out = _extract_usage({"usage": {"prompt_tokens": 10, "completion_tokens": 2}})
        self.assertNotIn("cached_tokens", out)

    def test_cached_tokens_do_not_change_the_guard(self):
        from app.assistant.orchestrator.agent_config import budget_exceeded

        self.assertTrue(budget_exceeded(prompt_tokens=TOKEN_BUDGET_FALLBACK,
                                        completion_tokens=0, cost_est=None))


class CostGuardTests(unittest.TestCase):
    """El guard de coste tiene que poder activarse alguna vez.

    cost_is_priced() preguntaba por LlmSettings.cost_per_1k_in, un campo que no
    existe en el dataclass. El hasattr devolvia False pase lo que pase, asi que
    MAX_COST_PER_TURN era un limite declarado que el snapshot nunca reconocia
    como activo. Peor que no tenerlo: el audit afirmaba que mandaba el techo de
    tokens aunque el operador hubiera configurado tarifas.
    """

    def setUp(self):
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()
        for key in ("ANDES_LLM_COST_INPUT_PER_1K", "ANDES_LLM_COST_OUTPUT_PER_1K"):
            os.environ.pop(key, None)

    def tearDown(self):
        self._env.stop()

    def test_unpriced_by_default(self):
        from app.assistant.orchestrator.agent_config import cost_is_priced

        self.assertFalse(cost_is_priced())
        self.assertEqual(budget_snapshot()["active_guard"], "token_budget")

    def test_configuring_an_input_rate_makes_cost_the_guard(self):
        from app.assistant.orchestrator.agent_config import cost_is_priced

        os.environ["ANDES_LLM_COST_INPUT_PER_1K"] = "0.003"
        self.assertTrue(cost_is_priced())
        self.assertEqual(budget_snapshot()["active_guard"], "cost_per_turn")

    def test_an_output_rate_alone_also_counts(self):
        from app.assistant.orchestrator.agent_config import cost_is_priced

        os.environ["ANDES_LLM_COST_OUTPUT_PER_1K"] = "0.015"
        self.assertTrue(cost_is_priced())

    def test_a_blank_rate_is_not_a_rate(self):
        from app.assistant.orchestrator.agent_config import cost_is_priced

        os.environ["ANDES_LLM_COST_INPUT_PER_1K"] = "   "
        self.assertFalse(cost_is_priced())

    def test_it_reads_the_same_source_as_the_estimator(self):
        """Si divergen, el snapshot dice un guard y budget_exceeded aplica otro."""
        from app.assistant.orchestrator.agent_config import cost_is_priced
        from app.assistant.orchestrator.metrics import estimate_cost_usd

        os.environ["ANDES_LLM_COST_INPUT_PER_1K"] = "0.003"
        self.assertEqual(cost_is_priced(), estimate_cost_usd(1000, 0) is not None)


class EvidenceSummaryTests(unittest.TestCase):
    """Saber que se degradó, sin filtrar qué."""

    def _big(self) -> EvidenceStore:
        store = EvidenceStore()
        store.add_from_tool_result(
            tool="get_customer", arguments={"q": "a"},
            result={"ok": True, "empty": False, "meta": {}, "data": {"items": [
                {"nombre": f"C{i}", "email": f"x{i}@y.z", "token": "sk-abc",
                 "obs": "W" * 80} for i in range(80)]}})
        return store

    def test_a_small_turn_reports_no_degradation(self):
        store = EvidenceStore()
        store.add_from_tool_result(tool="get_inventory", arguments={"codigo": "2404"},
                                   result={"ok": True, "empty": False, "meta": {},
                                           "data": {"total_stock": 2}})
        summary = store.safe_summary()
        self.assertEqual(summary["truncated_items"], 0)
        self.assertEqual(summary["degraded_tools"], [])

    def test_degradation_is_reported_with_counts(self):
        summary = self._big().safe_summary()
        self.assertEqual(summary["truncated_items"], 1)
        self.assertGreater(summary["omitted_rows_total"], 0)
        self.assertEqual(summary["degraded_tools"], ["get_customer"])

    def test_the_summary_carries_no_content(self):
        blob = json.dumps(self._big().safe_summary(), ensure_ascii=False)
        for secret in ("sk-abc", "@y.z", "nombre", "WWW"):
            self.assertNotIn(secret, blob, secret)

    def test_the_summary_is_json_serialisable(self):
        json.dumps(self._big().safe_summary())


class DatasetCoverageTests(unittest.TestCase):
    """Los dos huecos que impedían medir los límites reales."""

    def _cases(self) -> list[dict[str, Any]]:
        from pathlib import Path

        from evals.fase81_runner import load_cases

        return load_cases(Path("evals/fase81_evidence_dataset.jsonl"))

    def test_a_three_tool_case_exists(self):
        widest = max(len(c.get("expected_tools") or []) for c in self._cases())
        self.assertGreaterEqual(widest, 3)

    def test_the_budget_covers_the_widest_case_with_room_for_a_blocked_final(self):
        """Medido: T09 (3 tools) tomo un final bloqueado y necesito 5 decisiones.
        Con margen cero ese camino cae en fallback aunque el trabajo sea
        correcto, que es exactamente lo que paso."""
        _worst, best = decisions_affordable()
        widest = max(len(c.get("expected_tools") or []) for c in self._cases())
        self.assertGreaterEqual(best, widest + 2)

    def test_a_case_can_degrade_evidence_with_real_sizes(self):
        """get_supplier con q amplia devuelve 5226 caracteres reales contra el
        Gateway. El dataset tiene que tener al menos un caso que pueda pedirlo."""
        supplier_cases = [c for c in self._cases()
                          if "get_supplier" in (c.get("expected_tools") or [])]
        self.assertTrue(supplier_cases)
        self.assertGreater(5226, MAX_TOOL_RESULT_CHARS)

    def test_every_case_has_a_unique_id(self):
        ids = [c["id"] for c in self._cases()]
        self.assertEqual(len(ids), len(set(ids)))


class GroundedLabelTests(unittest.TestCase):
    """La razón del fallo tiene que nombrar lo que se midió."""

    def _grounded(self, run: dict[str, Any]) -> tuple[bool, list[str]]:
        from evals.fase81_scorer import _grounded_ok

        return _grounded_ok({"expected_grounding": True, "expect_fallback": False}, run)

    def test_a_fallback_still_fails_the_axis(self):
        ok, _reasons = self._grounded(
            {"reply": "Ingresos de 2404 - 2 registros.", "fallback_used": True,
             "fallback_reason": "agent_timeout"})
        self.assertFalse(ok)

    def test_the_reason_no_longer_claims_the_text_was_ungrounded(self):
        """C04 publicó datos reales del Gateway y el scorer lo etiquetaba
        'ungrounded_fallback'. El veredicto era correcto; la razón, falsa."""
        _ok, reasons = self._grounded(
            {"reply": "Ingresos de 2404 - 2 registros.", "fallback_used": True,
             "fallback_reason": "agent_timeout"})
        self.assertNotIn("ungrounded_fallback", reasons)
        self.assertTrue(any("fallback_instead_of_grounded_answer" in r for r in reasons))
        self.assertTrue(any("agent_timeout" in r for r in reasons))

    def test_an_invented_figure_is_named_as_such_even_in_a_fallback(self):
        _ok, reasons = self._grounded(
            {"reply": "El stock es 8888.", "fallback_used": True,
             "fallback_reason": "agent_timeout"})
        self.assertIn("invented_number", reasons)

    def test_a_grounded_answer_without_fallback_passes(self):
        ok, _r = self._grounded({"reply": "El stock es 2.", "fallback_used": False})
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
