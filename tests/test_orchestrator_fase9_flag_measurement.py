"""FASE 9 — el instrumento que cierra la deuda de medicion de las banderas.

EL PROBLEMA QUE RESUELVE

Tres capacidades apagadas sin una sola medicion con modelo. El diseno obvio
—factorial completo— cuesta 2^3 x 76 = 608 turnos, y el proveedor se agoto a
~411 en veinticinco minutos: la corrida no llegaria al final y produciria
exactamente el artefacto que 8.x enseno a desconfiar.

LA MEDICION QUE REDISENO EL EXPERIMENTO

    ANALYSIS   3/76 casos   el bloque solo entra si analytical_intent
    PERIOD     2/76 casos   dispara en 7, y 5 son KPI (tienen `periodo` propio)
    ORDERS    76/76 casos   anade una linea de contrato a TODOS los prompts

608 turnos -> ~192 dirigiendo el experimento.

POR QUE DIRIGIRLO NO ES PEREZA

Porque el radio se DEMUESTRA antes de usarlo. El verificador recorre las 76
preguntas con la bandera encendida y apagada y compara lo que el modelo
recibiria para las tools que cada caso llama de verdad. Si algo cambia fuera
del radio declarado, la medicion dirigida NO es valida y el runner se niega a
seguir.

Y el verificador ya se gano el sueldo: la primera version de la huella
normalizaba contra `get_sales` para todos los casos, y declaro el radio de
PERIOD invalido porque los cinco casos de KPI "cambiaban" — les inyectaba una
ventana en una tool que no usan. El radio era correcto y la huella medía otra
cosa. Se equivoco hacia el lado seguro: negarse a medir.
"""
from __future__ import annotations

import unittest

from evals.fase9_flag_measurement import FLAGS, verify_radius


def _cases():
    from evals.fase81g_closure import _load_cases

    return _load_cases()


class TheRadiusIsProvenNotAssumedTests(unittest.TestCase):

    def test_the_analysis_radius_is_exactly_the_analytical_cases(self):
        r = verify_radius("analysis", _cases())
        self.assertTrue(r["radius_is_sound"])
        self.assertEqual(r["radius"], ["A01", "A02", "A03"])
        self.assertEqual(r["outside_radius_that_change"], [])

    def test_the_period_radius_excludes_the_kpi_cases(self):
        """Los 5 casos de KPI nombran un periodo y NO reciben inyeccion: tienen
        vocabulario propio. Queda fuera por contrato, no por lista."""
        r = verify_radius("period", _cases())
        self.assertTrue(r["radius_is_sound"])
        self.assertEqual(r["radius"], ["E05", "V02"])
        for kpi in ("G03", "K04", "P02", "P04", "R04"):
            self.assertNotIn(kpi, r["radius"])
            self.assertNotIn(kpi, r["outside_radius_that_change"])

    def test_every_case_inside_the_radius_really_changes(self):
        """Un radio que incluya casos que NO cambian medirian ruido."""
        for flag in ("analysis", "period"):
            with self.subTest(flag=flag):
                r = verify_radius(flag, _cases())
                self.assertEqual(r["inside_radius_that_do_not_change"], [])

    def test_a_global_flag_refuses_to_be_targeted(self):
        r = verify_radius("orders", _cases())
        self.assertFalse(r["targeted"])
        self.assertIn("global", r["reason"])

    def test_the_radius_is_derived_at_runtime_not_hardcoded(self):
        """Si el detector cambia, la lista de casos le sigue sola."""
        for flag in ("analysis", "period"):
            self.assertTrue(callable(FLAGS[flag]["radius"]))


class TheTargetedDesignIsCheaperAndItMattersTests(unittest.TestCase):

    def test_the_targeted_cost_fits_where_the_factorial_does_not(self):
        """El proveedor se agoto a ~411 turnos. El factorial son 608."""
        casos = _cases()
        factorial = 2 ** len(FLAGS) * len(casos)
        dirigido = len(casos) * 2  # ORDERS, que es global
        for flag in ("analysis", "period"):
            dirigido += len(verify_radius(flag, casos)["radius"]) * 8 * 2
        self.assertGreater(factorial, 600)
        self.assertLess(dirigido, 411, "el diseno dirigido tiene que caber")
        self.assertLess(dirigido, factorial / 2)


class TheInstrumentRefusesToLieTests(unittest.TestCase):

    def test_an_unsound_radius_stops_the_measurement(self):
        """La condicion que legitima dirigir. Si se rompe, no se mide."""
        import evals.fase9_flag_measurement as fm

        original = fm.FLAGS["analysis"]["radius"]
        # Un radio mentiroso: dice tocar solo A02 cuando toca A01 y A03 tambien.
        fm.FLAGS["analysis"]["radius"] = lambda cases: ["A02"]
        try:
            r = verify_radius("analysis", _cases())
        finally:
            fm.FLAGS["analysis"]["radius"] = original
        self.assertFalse(r["radius_is_sound"])
        self.assertEqual(r["outside_radius_that_change"], ["A01", "A03"])

    def test_the_runner_stops_on_an_unsound_radius(self):
        import evals.fase9_flag_measurement as fm

        original = fm.FLAGS["analysis"]["radius"]
        fm.FLAGS["analysis"]["radius"] = lambda cases: ["A02"]
        try:
            codigo = fm.measure("analysis", runs=1)
        finally:
            fm.FLAGS["analysis"]["radius"] = original
        self.assertEqual(codigo, 1, "un radio falso tiene que abortar, no medir")

    def test_a_global_flag_sends_you_to_the_full_benchmark(self):
        import evals.fase9_flag_measurement as fm

        self.assertEqual(fm.measure("orders", runs=1), 2)

    def test_the_env_flag_is_restored_after_verifying(self):
        """El verificador manipula banderas de brazo: si no las devuelve,
        reetiqueta la corrida entera — el defecto que ya costo una en 8.x."""
        import os

        for flag, spec in FLAGS.items():
            if spec["radius"] is None:
                continue
            with self.subTest(flag=flag):
                previo = os.environ.get(spec["env"])
                verify_radius(flag, _cases())
                self.assertEqual(os.environ.get(spec["env"]), previo)


class EveryFlagDeclaresWhatItIsAskingTests(unittest.TestCase):
    """Una medicion sin pregunta es un numero sin lectura."""

    def test_the_three_flags_are_covered(self):
        self.assertEqual(set(FLAGS), {"analysis", "period", "orders"})

    def test_each_one_names_its_question_and_its_env_var(self):
        for flag, spec in FLAGS.items():
            with self.subTest(flag=flag):
                self.assertTrue(spec["question"])
                self.assertTrue(spec["env"].startswith("ANDES_ASSISTANT_"))

    def test_the_env_vars_match_the_arm_dimensions(self):
        """Las banderas que se miden tienen que ser las que el brazo distingue,
        o dos corridas distintas caerian en el mismo fichero."""
        from evals.fase81g_closure import ARM_FLAGS

        del_brazo = {env for _, env in ARM_FLAGS}
        for spec in FLAGS.values():
            self.assertIn(spec["env"], del_brazo)


if __name__ == "__main__":
    unittest.main()
