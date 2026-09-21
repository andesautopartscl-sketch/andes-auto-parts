"""FASE 8.x — tres metricas del harness que decian algo distinto de lo que median.

Ninguna causo un fallo. Las tres distorsionaban la lectura de la tercera A/B
real, que es peor: una metrica que miente no rompe nada, hace que alguien
concluya mal. Las tres se encontraron leyendo el informe contra las trazas.

1. `benchmark.analysis.state` afirmaba el estado de una variable de entorno sin
   leerla nunca: literal fijo emitido siempre que la escalera no se usara. Los
   DOS brazos reportaron "ANALYSIS_ENABLED=0", incluido el que corrio encendido.
2. `null_not_zero` era un alias del gate de grounding entero, asi que V02/OFF
   salio marcado como nulo-publicado-como-cero por haber caido a fallback, sin
   que hubiera ningun nulo.
3. `agent_true_failures` cuenta solo casos pre-etiquetados en el dataset. Salio 0
   mientras un defecto de schema mataba O01 y O04.

Las pruebas de `state` viven con el resto de la puerta de capacidad, en
test_orchestrator_fase85b_capability_gate.
"""
from __future__ import annotations

import unittest

from evals.fase81_scorer import aggregate, score_case


def _gold(**kw) -> dict:
    gold = {"id": "V02", "bucket": "V", "expected_tools": ["get_sales"],
            "tool_policy": "exact", "expect_fallback": False,
            "expected_grounding": True, "expected_null_not_zero": True}
    gold.update(kw)
    return gold


def _run(**kw) -> dict:
    run = {"id": "V02", "tools_used": ["get_sales"], "fallback_used": False,
           "reply": "Ventas — 0 unidad(es) en 2 documento(s). Ingresos: 0.0"}
    run.update(kw)
    return run


class NullNotZeroSaysOnlyWhatItMeasuredTests(unittest.TestCase):

    def test_a_real_zero_is_not_flagged_as_a_null(self):
        """Medido en V02: las dos unicas ventas del periodo fueron devueltas
        integras, asi que 0.0 es el neto verdadero. `_fmt(None)` habria escrito
        "no disponible"."""
        scored = score_case(_gold(), _run())
        self.assertTrue(scored["null_not_zero"])

    def test_a_fallback_no_longer_trips_the_null_gate(self):
        """El caso exacto: V02/OFF fallo por `answer_replaced`, y el unico
        `null_not_zero=False` de los 76 casos apuntaba a un defecto de dinero
        que no existia."""
        scored = score_case(_gold(), _run(
            fallback_used=True, fallback_reason="agent_verifier_failed"))
        self.assertFalse(scored["pass"], "el caso sigue fallando")
        self.assertTrue(scored["null_not_zero"], "pero no por nulos")
        self.assertIn("fallback_instead_of_grounded_answer=agent_verifier_failed",
                      scored["reasons"])
        self.assertNotIn("null_as_zero", scored["reasons"])

    def test_a_genuine_null_as_zero_is_still_caught(self):
        """Aflojar el nombre no puede aflojar la regla."""
        scored = score_case(_gold(), _run(reply="Ventas del periodo: 0"))
        self.assertFalse(scored["null_not_zero"])
        self.assertIn("null_as_zero", scored["reasons"])
        self.assertFalse(scored["pass"])

    def test_the_gate_is_vacuously_true_when_the_case_does_not_ask_for_it(self):
        scored = score_case(_gold(expected_null_not_zero=False),
                            _run(reply="Ventas del periodo: 0"))
        self.assertTrue(scored["null_not_zero"])


class ProductFailuresAreNotHiddenTests(unittest.TestCase):

    def _equivalences(self, **kw) -> dict:
        run = {"id": "O01", "tools_used": [], "fallback_used": True,
               "fallback_reason": "invalid_args",
               "arg_errors": [{"error": "invalid_args", "tool": "get_equivalences",
                               "fields": {"oem|codigo": "one_required"}}],
               "reply": "No tengo evidencia de herramientas para responder."}
        run.update(kw)
        return run

    def _o01_gold(self) -> dict:
        return {"id": "O01", "bucket": "O", "expected_tools": ["get_equivalences"],
                "tool_policy": "exact", "expect_fallback": False,
                "expected_grounding": True}

    def test_an_argument_contract_failure_is_named_as_such(self):
        """O01/O04: el modelo eligio bien la tool y no pudo expresar el ancla.
        Eso es un defecto de la cadena contrato/schema/normalizador/validador,
        no ruido del modelo, y la metrica tiene que poder decirlo."""
        scored = score_case(self._o01_gold(), self._equivalences())
        self.assertFalse(scored["pass"])
        self.assertTrue(scored["product_failure"])
        self.assertEqual(scored["failure_kind"], "argument_contract")

    def test_the_aggregate_surfaces_them_even_when_the_dataset_labels_nothing(self):
        """El sintoma exacto de la tercera A/B: agent_true_failures=0 con dos
        casos rotos por un defecto de schema."""
        scores = [score_case(self._o01_gold(), self._equivalences()),
                  score_case(self._o01_gold(), self._equivalences(id="O04"))]
        agg = aggregate(scores)
        self.assertEqual(agg["agent_true_failures"], 0)
        self.assertEqual(agg["product_failures"], 2)
        self.assertEqual(agg["failure_kinds"], {"argument_contract": 2})
        self.assertIn("product_failures", agg["agent_true_failures_note"])

    def test_a_passing_case_has_no_failure_kind(self):
        scored = score_case(_gold(), _run())
        self.assertTrue(scored["pass"])
        self.assertFalse(scored["product_failure"])
        self.assertIsNone(scored["failure_kind"])

    def test_an_evaluation_artifact_is_not_counted_as_a_product_failure(self):
        """Un gold obsoleto es un defecto de la evaluacion, no del producto.
        Distinguirlos es el motivo por el que la etiqueta del dataset existe."""
        gold = self._o01_gold()
        gold["baseline_failure"] = "eval"
        scored = score_case(gold, self._equivalences())
        self.assertFalse(scored["pass"])
        self.assertFalse(scored["product_failure"])
        self.assertEqual(scored["failure_kind"], "evaluation_artifact")
        self.assertEqual(aggregate([scored])["product_failures"], 0)

    def test_a_replaced_answer_gets_its_own_kind(self):
        scored = score_case(_gold(), _run(
            fallback_used=True, fallback_reason="agent_verifier_failed"))
        self.assertEqual(scored["failure_kind"], "verifier_replaced_answer")

    def test_the_kinds_partition_the_failures(self):
        """Todo fallo tiene exactamente un tipo, y ningun aprobado tiene ninguno."""
        scores = [score_case(self._o01_gold(), self._equivalences()),
                  score_case(_gold(), _run()),
                  score_case(_gold(), _run(fallback_used=True,
                                           fallback_reason="agent_verifier_failed"))]
        agg = aggregate(scores)
        failed = [s for s in scores if not s["pass"]]
        self.assertEqual(sum(agg["failure_kinds"].values()), len(failed))
        self.assertTrue(all(s["failure_kind"] for s in failed))


if __name__ == "__main__":
    unittest.main()
