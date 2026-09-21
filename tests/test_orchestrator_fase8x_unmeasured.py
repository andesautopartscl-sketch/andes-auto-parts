"""FASE 8.x — una casilla vacia no es un fallo del producto.

Lo que paso el 2026-09-20, con tres corridas del benchmark seguidas:

    an0-pv0-pr0   74/76   llm_unavailable=0    latencia media 4587 ms   67 casos con tokens
    an1-pv0-pr0   18/76   llm_unavailable=59   latencia media 1402 ms   22 casos con tokens
    an0-pv0-pr1   17/76   llm_unavailable=59   latencia media 1063 ms   16 casos con tokens

El proveedor se agoto a mitad de sesion. El informe publico 18/76 y 17/76 como
mediciones, con ``product_failures`` 52 y 54, y con esas cifras cualquiera habria
concluido que la escalera analitica y la resolucion de periodos eran regresiones
catastroficas. No median nada: 59 de 76 llamadas nunca volvieron.

La firma esta en los propios datos y es inconfundible al reves de lo que uno
esperaria: la latencia **cayo**. Un modelo que lo hace peor tarda lo mismo o mas.
Uno al que no se llama responde al instante.

Este es el mismo defecto de clase que ya se corrigio en `null_not_zero` y en
`analysis.state`: una metrica que afirma algo distinto de lo que midio. Aqui era
el mas caro de los tres, porque la conclusion equivocada habria sido revertir dos
capacidades que funcionan.
"""
from __future__ import annotations

import unittest

from evals.fase81_scorer import aggregate, score_case


def _gold(case_id: str = "C02") -> dict:
    return {"id": case_id, "bucket": "C", "expected_tools": ["get_inventory"],
            "tool_count_policy": "exact", "expect_fallback": False,
            "expected_grounding": True}


def _sin_respuesta(case_id: str = "C02") -> dict:
    """Como queda un caso cuando el proveedor no contesta."""
    return {"id": case_id, "tools_used": [], "fallback_used": True,
            "fallback_reason": "llm_unavailable", "total_tokens": None,
            "latency_ms": 409,
            "reply": "No tengo evidencia de herramientas para responder."}


def _ok(case_id: str = "C02") -> dict:
    return {"id": case_id, "tools_used": ["get_inventory"], "fallback_used": False,
            "fallback_reason": None, "total_tokens": 4572, "latency_ms": 3599,
            "reply": "DATOS:\n- Stock 2 unidades."}


class AnEmptyCellIsNotAProductFailureTests(unittest.TestCase):

    def test_an_unanswered_case_is_marked_unmeasured(self):
        s = score_case(_gold(), _sin_respuesta())
        self.assertTrue(s["unmeasured"])
        self.assertFalse(s["pass"])
        self.assertFalse(s["product_failure"], "no se midio: no hay nada que atribuir")
        self.assertEqual(s["failure_kind"], "unmeasured")

    def test_it_is_not_misattributed_to_tool_selection(self):
        """El error exacto: 59 casillas vacias se contaron como 52 fallos de
        producto, la mayoria etiquetados 'tool_selection'."""
        s = score_case(_gold(), _sin_respuesta())
        self.assertNotEqual(s["failure_kind"], "tool_selection")

    def test_a_real_failure_is_still_a_product_failure(self):
        """Aflojar la clasificacion no puede tapar un fallo de verdad."""
        real = {"id": "C02", "tools_used": [], "fallback_used": False,
                "fallback_reason": None, "total_tokens": 3100,
                "reply": "Indicame el codigo."}
        s = score_case(_gold(), real)
        self.assertFalse(s["unmeasured"])
        self.assertTrue(s["product_failure"])
        self.assertEqual(s["failure_kind"], "tool_selection")


class TheAggregateRefusesToPublishAnInvalidRateTests(unittest.TestCase):

    def test_a_complete_run_is_valid(self):
        agg = aggregate([score_case(_gold(f"C{i:02d}"), _ok(f"C{i:02d}"))
                         for i in range(5)])
        self.assertTrue(agg["measurement_valid"])
        self.assertEqual(agg["cases_unmeasured"], 0)
        self.assertEqual(agg["measurement_note"], "corrida completa")
        self.assertEqual(agg["pass_rate"], agg["pass_rate_measured"])

    def test_a_single_empty_cell_invalidates_the_rate(self):
        """Estricto a proposito: con una casilla vacia la cifra ya no es
        comparable con la de otro brazo, y elegir una tolerancia arbitraria fue
        lo que dejo pasar 59."""
        scores = [score_case(_gold(f"C{i:02d}"), _ok(f"C{i:02d}")) for i in range(4)]
        scores.append(score_case(_gold("C99"), _sin_respuesta("C99")))
        agg = aggregate(scores)
        self.assertFalse(agg["measurement_valid"])
        self.assertEqual(agg["cases_unmeasured"], 1)
        self.assertEqual(agg["unmeasured_ids"], ["C99"])
        self.assertIn("NO COMPARABLE", agg["measurement_note"])

    def test_the_partial_data_is_not_thrown_away(self):
        """Invalidar no es borrar: los casos que si respondieron siguen
        contando, en su propia cifra y bien etiquetada."""
        scores = [score_case(_gold(f"C{i:02d}"), _ok(f"C{i:02d}")) for i in range(4)]
        scores += [score_case(_gold(f"D{i:02d}"), _sin_respuesta(f"D{i:02d}"))
                   for i in range(6)]
        agg = aggregate(scores)
        self.assertEqual(agg["cases_measured"], 4)
        self.assertEqual(agg["pass_rate_measured"], 1.0)
        self.assertEqual(agg["pass_rate"], 0.4, "la cifra bruta castiga las vacias")
        self.assertEqual(agg["product_failures"], 0)
        self.assertEqual(agg["failure_kinds"], {"unmeasured": 6})

    def test_the_reproduction_of_the_real_incident(self):
        """59 de 76 sin respuesta: la cifra bruta dice 18/76 y los fallos de
        producto reales eran 2, no 52."""
        scores = [score_case(_gold(f"C{i:02d}"), _ok(f"C{i:02d}")) for i in range(17)]
        scores += [score_case(_gold(f"D{i:02d}"), _sin_respuesta(f"D{i:02d}"))
                   for i in range(59)]
        agg = aggregate(scores)
        self.assertEqual(agg["n"], 76)
        self.assertFalse(agg["measurement_valid"])
        self.assertEqual(agg["cases_measured"], 17)
        self.assertEqual(agg["product_failures"], 0)


class TheArmRefusesToCompleteAnABTests(unittest.TestCase):

    def test_an_invalid_arm_does_not_declare_the_ab_complete(self):
        """Comparar un brazo valido contra uno vacio produce una regresion
        inventada. Es exactamente lo que habria pasado con 74/76 contra 18/76."""
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from evals.fase81g_closure import _arm_report, arm_id

        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            # Un hermano en disco para que `siblings` no este vacio.
            otro = "an1-pv1-pr1" if arm_id() != "an1-pv1-pr1" else "an0-pv0-pr0"
            (out / f"fase81_final_report.{otro}.json").write_text("{}", encoding="utf-8")

            valido = _arm_report(out, measurement_valid=True)
            invalido = _arm_report(out, measurement_valid=False)

        self.assertTrue(valido["ab_complete"])
        self.assertFalse(invalido["ab_complete"])
        self.assertIn("no midio", invalido["note"])
        self.assertFalse(invalido["measurement_valid"])



class TheLadderVerdictDistinguishesUnusedFromUnmeasuredTests(unittest.TestCase):
    """Segunda instancia del mismo defecto, encontrada leyendo la misma corrida.

    En an1-pv0-pr0 los TRES casos analiticos (A01, A02, A03) volvieron con
    llm_unavailable, y el resumen seguia afirmando "habilitada y NO usada: el
    modelo no emitio ningun peldano". No emitio nada porque no se le pregunto.
    """

    def _scores(self, unmeasured: bool):
        return [{"id": cid, "pass": False, "unmeasured": unmeasured,
                 "analysis": {"expected": True, "used_ladder": False,
                              "rungs_used": []}}
                for cid in ("A01", "A02", "A03")]

    def test_unmeasured_analytic_cases_say_so(self):
        agg = aggregate(self._scores(True), analysis_enabled=True)
        state = agg["analysis"]["state"]
        self.assertIn("SIN MEDIR", state)
        self.assertIn("A01", state)
        self.assertNotIn("NO usada", state)
        self.assertEqual(agg["analysis"]["analytic_cases_unmeasured"],
                         ["A01", "A02", "A03"])

    def test_measured_and_unused_still_says_unused(self):
        """Quitar el falso negativo no puede tapar el hallazgo verdadero: con la
        capacidad encendida y los casos medidos, cero usos SI mide al modelo."""
        agg = aggregate(self._scores(False), analysis_enabled=True)
        state = agg["analysis"]["state"]
        self.assertIn("NO usada", state)
        self.assertNotIn("SIN MEDIR", state)
        self.assertEqual(agg["analysis"]["analytic_cases_unmeasured"], [])

    def test_a_partially_measured_run_reports_both(self):
        scores = self._scores(False)[:2] + [
            {"id": "A03", "pass": False, "unmeasured": True,
             "analysis": {"expected": True, "used_ladder": False, "rungs_used": []}}]
        state = aggregate(scores, analysis_enabled=True)["analysis"]["state"]
        self.assertIn("2 caso(s) analitico(s) medido(s)", state)
        self.assertIn("1 sin medir", state)

class TheWatchListCannotBePoisonedByAnInvalidRunTests(unittest.TestCase):
    """Un agotamiento del proveedor no puede convertirse en 265 turnos de
    vigilancia sobre casillas vacias, y menos en la cuenta que acaba de
    quedarse sin cuota."""

    def _write(self, out, arm, failed, valid):
        import json
        (out / f"fase81_final_report.{arm}.json").write_text(
            json.dumps({"benchmark": {"failed": failed,
                                      "measurement_valid": valid}}),
            encoding="utf-8")

    def test_an_invalid_report_yields_the_seed_not_its_failures(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from evals.fase81g_closure import (
            STABILITY_WATCH_SEED, arm_id, stability_watch_ids)

        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            self._write(out, arm_id(), [f"X{i:02d}" for i in range(58)], False)
            self.assertEqual(stability_watch_ids(out), STABILITY_WATCH_SEED)

    def test_a_valid_report_drives_the_watch_list(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from evals.fase81g_closure import arm_id, stability_watch_ids

        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            self._write(out, arm_id(), ["A01", "V02"], True)
            self.assertEqual(stability_watch_ids(out), ("A01", "V02"))

    def test_the_cap_holds_even_if_validity_detection_ever_failed(self):
        """Defensa estructural: el tope no depende de que la deteccion acierte."""
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from evals.fase81g_closure import (
            MAX_WATCH_IDS, arm_id, stability_watch_ids)

        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            self._write(out, arm_id(), [f"X{i:02d}" for i in range(58)], True)
            self.assertEqual(len(stability_watch_ids(out)), MAX_WATCH_IDS)

    def test_core_cases_are_never_duplicated_into_the_watch_list(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from evals.fase81g_closure import (
            STABILITY_CORE_IDS, arm_id, stability_watch_ids)

        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            self._write(out, arm_id(), ["T07", "P03", "A01"], True)
            watch = stability_watch_ids(out)
            self.assertEqual(watch, ("A01",))
            self.assertFalse(set(watch) & set(STABILITY_CORE_IDS))


if __name__ == "__main__":
    unittest.main()
