"""FASE 9 — preflight del proveedor: que una corrida larga falle rapido y claro.

LO QUE PASO, MEDIDO

El 2026-09-21 una sesion exporto la cadena literal ``TU_KEY_YA_EXISTENTE`` como
clave. Tres defectos encadenados:

1. ``_llm_ready()`` solo comprobaba que la variable no estuviera vacia, asi que
   una plantilla de 19 caracteres paso por credencial valida.
2. El cliente colapsaba el 401 en ``llm_unavailable``, el mismo codigo que un
   timeout o una caida, asi que el artefacto no podia distinguir "repite la
   corrida" de "arregla tu credencial".
3. El arnes ejecuto **354 turnos** y los marco "sin medir". Correctamente — pero
   nadie dijo nunca por que.

Y un cuarto, mio y del mismo dia: el instrumento de medicion concluia "la
escalera NO se uso" habiendo medido CERO corridas. Una lectura sobre la nada.

LO QUE CIERRA ESTO

Una llamada minima antes de gastar turnos, y una clasificacion que separa lo que
se arregla esperando de lo que no. La clave nunca se imprime: solo su forma.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from evals.fase9_llm_preflight import (
    BLOCKING,
    ENV_VAR,
    key_shape,
    preflight,
)


class _Env:
    """Pone y devuelve la variable, siempre."""

    def __init__(self, value):
        self.value = value

    def __enter__(self):
        self._prev = os.environ.get(ENV_VAR)
        if self.value is None:
            os.environ.pop(ENV_VAR, None)
        else:
            os.environ[ENV_VAR] = self.value
        return self

    def __exit__(self, *_exc):
        if self._prev is None:
            os.environ.pop(ENV_VAR, None)
        else:
            os.environ[ENV_VAR] = self._prev


class TheExactStringThatBurned354TurnsIsCaughtTests(unittest.TestCase):

    def test_the_literal_placeholder_from_the_log_is_refused(self):
        with _Env("TU_KEY_YA_EXISTENTE"):
            forma = key_shape()
        self.assertTrue(forma["present"])
        self.assertTrue(forma["looks_like_placeholder"])
        self.assertEqual(forma["verdict"], "placeholder")

    def test_a_placeholder_costs_zero_calls(self):
        """Lo caro no era la llamada: eran los 354 turnos que vinieron despues."""
        with _Env("TU_KEY_YA_EXISTENTE"):
            with patch("app.assistant.orchestrator.llm.client."
                       "OpenAICompatibleClient") as cliente:
                r = preflight()
        self.assertEqual(r["verdict"], "placeholder")
        cliente.assert_not_called()

    def test_other_common_placeholders_are_caught_too(self):
        for muestra in ("YOUR_API_KEY_HERE", "sk-xxxxxxxxxxxxxxxxxxxx",
                        "<pon-tu-key-aqui>", "changeme", "dummy-key-value"):
            with self.subTest(muestra=muestra):
                with _Env(muestra):
                    self.assertTrue(key_shape()["looks_like_placeholder"], muestra)

    def test_a_plausible_shape_is_not_refused_on_shape_alone(self):
        """El preflight no valida claves: filtra despistes obvios. Una cadena
        con forma creible se prueba de verdad contra el proveedor."""
        with _Env("sk-proj-" + "a1b2c3d4e5" * 5):
            self.assertFalse(key_shape()["looks_like_placeholder"])

    def test_a_missing_variable_is_its_own_verdict(self):
        with _Env(None):
            self.assertEqual(key_shape()["verdict"], "sin_clave")
            self.assertFalse(key_shape()["present"])


class TheKeyIsNeverExposedTests(unittest.TestCase):
    """Diagnosticar no puede significar filtrar."""

    SECRETO = "sk-proj-SUPERSECRETO0123456789abcdefghij"

    def test_the_shape_report_carries_no_fragment_of_the_key(self):
        with _Env(self.SECRETO):
            forma = key_shape()
        rendido = repr(forma)
        self.assertNotIn(self.SECRETO, rendido)
        self.assertNotIn(self.SECRETO[:8], rendido)
        self.assertNotIn("SUPER", rendido)

    def test_the_shape_reports_only_length_whitespace_and_pattern(self):
        with _Env(self.SECRETO):
            forma = key_shape()
        self.assertEqual(set(forma), {"present", "length", "has_whitespace",
                                      "looks_like_placeholder", "verdict"})
        self.assertEqual(forma["length"], len(self.SECRETO))

    def test_whitespace_is_reported_because_it_breaks_headers(self):
        with _Env("sk-proj-" + "a1b2c3d4e5" * 5 + " "):
            self.assertTrue(key_shape()["has_whitespace"])


class TheProviderFailuresAreDistinguishableTests(unittest.TestCase):
    """Timeout, cuota, 429 y 401 dejan la casilla vacia igual — pero la accion
    que exigen es distinta, y por eso el codigo tiene que serlo."""

    def test_the_client_has_a_code_for_each(self):
        from app.assistant.orchestrator.llm.client import LlmError

        for code in ("llm_auth_invalid", "llm_quota_exhausted",
                     "llm_rate_limited", "llm_unavailable"):
            with self.subTest(code=code):
                self.assertEqual(LlmError(code, "x").code, code)

    def test_the_scorer_treats_all_of_them_as_unmeasured(self):
        """Ninguno es un fallo del producto: los cuatro dejan la casilla vacia."""
        from evals.fase81_scorer import PROVIDER_FAILURE_CODES, score_case

        gold = {"id": "X", "bucket": "X", "expected_tools": ["get_inventory"],
                "tool_count_policy": "exact", "expect_fallback": False}
        for code in PROVIDER_FAILURE_CODES:
            with self.subTest(code=code):
                s = score_case(gold, {"id": "X", "tools_used": [],
                                      "fallback_used": True,
                                      "fallback_reason": code, "reply": ""})
                self.assertTrue(s["unmeasured"])
                self.assertFalse(s["product_failure"])

    def test_auth_and_quota_block_a_long_run_but_rate_limit_does_not(self):
        """Lo que no se arregla esperando aborta; lo transitorio solo avisa."""
        self.assertIn("auth_invalid", BLOCKING)
        self.assertIn("quota_exhausted", BLOCKING)
        self.assertIn("placeholder", BLOCKING)
        self.assertIn("sin_clave", BLOCKING)
        self.assertNotIn("rate_limited", BLOCKING)
        self.assertNotIn("ok", BLOCKING)


class TheGateStopsLongRunsTests(unittest.TestCase):

    def test_the_flag_measurement_aborts_before_spending_turns(self):
        """El parche va en el origen, no en el modulo que lo importa dentro de
        la funcion: ahi es donde se gastaria el turno."""
        import evals.fase81g_closure as closure
        import evals.fase9_flag_measurement as fm

        with _Env("TU_KEY_YA_EXISTENTE"):
            with patch.object(closure, "_run_case") as corrida:
                codigo = fm.measure("analysis", runs=8)
        self.assertEqual(codigo, 2)
        corrida.assert_not_called()


class NoMeasurementMeansNoReadingTests(unittest.TestCase):
    """El cuarto defecto del dia, y mio: el instrumento concluia "la escalera
    NO se uso" habiendo medido cero corridas."""

    def _render(self, filas):
        import io
        from contextlib import redirect_stdout

        import evals.fase9_flag_measurement as fm

        datos = {"flag": "analysis", "runs": len(filas), "radius": ["A02"],
                 "arms": {"1": {"arm": "x", "cases": {"A02": filas}}}}
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            fm.render(datos)
        return buffer.getvalue()

    def test_zero_measured_runs_produces_no_conclusion(self):
        salida = self._render([{"unmeasured": True, "pass": False}] * 8)
        self.assertIn("SIN LECTURA", salida)
        self.assertNotIn("el modelo no la elige", salida)

    def test_measured_runs_without_the_ladder_do_produce_one(self):
        salida = self._render([{"unmeasured": False, "pass": True,
                                "used_ladder": False}] * 8)
        self.assertIn("no la elige", salida)
        self.assertNotIn("SIN LECTURA", salida)

    def test_measured_runs_with_the_ladder_say_so(self):
        salida = self._render([{"unmeasured": False, "pass": True,
                                "used_ladder": True}] * 4)
        self.assertIn("se uso en 4", salida)


if __name__ == "__main__":
    unittest.main()
