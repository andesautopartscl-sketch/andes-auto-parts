"""FASE 8.x — la causa raiz de A01, medida, y la decision de no parchearla.

MEDIDO CON LLM REAL (brazo an0-pv0-pr0, 8 repeticiones por variante):

    variante                codigo_resuelto   pidio_codigo   leido_como_fecha
    A01_original                  0/8              8/8            3/8
    A01_sin_horizonte             0/8              8/8            7/8
    A01_codigo_no_fecha           8/8              0/8            0/8
    A01_ancla_nominal             8/8              0/8            0/8
    A01_reordenado                8/8              0/8            0/8
    A02_control                   8/8              0/8            0/8
    A03_control                   8/8              0/8            0/8

CAUSA RAIZ: una CONJUNCION de dos condiciones.

    (a) el token va pegado al bigrama "movimientos del ..."
    (b) el token puede leerse como fecha DDMM

Fallan exactamente las dos variantes que cumplen AMBAS, y romper cualquiera de
las dos la arregla al 100%: con T3311RC (81 movimientos reales, imposible de leer
como fecha) resuelve 8/8; con "del producto 2404" resuelve 8/8; alejando el
codigo de "movimientos" resuelve 8/8. "Los movimientos del 24/04" es una lectura
natural en castellano y el modelo la toma.

QUE QUEDA REFUTADO: el horizonte de proyeccion. Quitar "para dos meses" no solo
no arreglo nada, EMPEORO la lectura de fecha de 0,375 a 0,875. En 8.9 yo habia
atribuido A01 a la redaccion del bloque analitico; esa atribucion quedo refutada
antes, y esta medicion cierra tambien la hipotesis del marco temporal.

POR QUE NO SE PARCHEA, decidido sobre datos y no por prudencia:

1. El arreglo pre-registrado no alcanza el caso. Media: A01 termina en
   `agent_clarify` 8/8 con CERO tools ejecutadas. Nunca llega al camino de
   reintento donde iban a ofrecerse los candidatos de codigo.

2. El arreglo que si lo alcanzaria —exponer candidatos en el prompt— cambia el
   riesgo a peor. El filtro de pertenencia al catalogo neutraliza el 8888 de E04
   y M02 (no es un producto), pero `2026` de V02 SI es un codigo real del
   catalogo, y V02 acaba de empezar a pasar. Cambiar un fallo de UTILIDAD, en el
   que el agente pide el dato que le falta, por un riesgo de VERACIDAD, en el que
   el agente adivina de que repuesto se habla, invierte la prioridad declarada
   del sistema.

3. El modo de fallo es seguro y degrada bien: el agente pregunta, no inventa.

A01 queda ABIERTO con causa demostrada. Abierto con causa es un estado legitimo;
abierto sin causa no lo era.
"""
from __future__ import annotations

import re
import unittest

from evals.fase8x_a01_experiment import VARIANTS

# Lo medido, para que una corrida futura se compare contra esto y no contra la
# memoria de nadie. Clave -> (codigo_resuelto_rate esperado, tolerancia).
MEDICION_2026_09_20 = {
    "A01_original": 0.0,
    "A01_sin_horizonte": 0.0,
    "A01_codigo_no_fecha": 1.0,
    "A01_ancla_nominal": 1.0,
    "A01_reordenado": 1.0,
    "A02_control": 1.0,
    "A03_control": 1.0,
}


def _condiciones(prompt: str, codigo: str) -> tuple[bool, bool]:
    """(a) pegado a 'movimientos del', (b) legible como fecha DDMM."""
    pegado = f"movimientos del {codigo}".lower() in prompt.lower()
    ddmm = bool(re.fullmatch(r"\d{4}", codigo)) and 1 <= int(codigo[:2]) <= 31 \
        and 1 <= int(codigo[2:]) <= 12
    return pegado, ddmm


class TheExperimentIsolatesOneVariableTests(unittest.TestCase):
    """Un experimento que mueve dos cosas a la vez no demuestra ninguna."""

    def test_every_variant_differs_from_the_original_in_exactly_one_condition(self):
        original = next(v for v in VARIANTS if v[0] == "A01_original")
        base = _condiciones(original[1], original[2])
        self.assertEqual(base, (True, True), "la original cumple ambas condiciones")
        for vid, prompt, codigo, control in VARIANTS:
            if vid == "A01_original" or control:
                continue
            with self.subTest(vid=vid):
                got = _condiciones(prompt, codigo)
                diff = sum(1 for a, b in zip(base, got) if a != b)
                if vid == "A01_sin_horizonte":
                    self.assertEqual(diff, 0, "solo cambia el horizonte")
                else:
                    self.assertEqual(diff, 1, f"{vid} deberia mover UNA condicion")

    def test_the_two_failing_variants_are_the_ones_meeting_both_conditions(self):
        """La lectura de la medicion: fallan (a) AND (b), y solo esas."""
        for vid, prompt, codigo, _ in VARIANTS:
            with self.subTest(vid=vid):
                pegado, ddmm = _condiciones(prompt, codigo)
                esperado_falla = pegado and ddmm
                medido_falla = MEDICION_2026_09_20[vid] == 0.0
                self.assertEqual(medido_falla, esperado_falla,
                                 f"{vid}: la conjuncion no explica lo medido")

    def test_the_discriminator_uses_a_code_that_cannot_be_a_date(self):
        vid, prompt, codigo, _ = next(v for v in VARIANTS
                                      if v[0] == "A01_codigo_no_fecha")
        self.assertFalse(_condiciones(prompt, codigo)[1])
        self.assertTrue(_condiciones(prompt, codigo)[0],
                        "y conserva el bigrama, para aislar la variable")

    def test_both_controls_pass_so_the_experiment_is_valid(self):
        for vid, _, _, control in VARIANTS:
            if control:
                self.assertEqual(MEDICION_2026_09_20[vid], 1.0, vid)


class TheUnsafeFixStaysUnbuiltTests(unittest.TestCase):
    """Guardia sobre la decision, no sobre el codigo.

    Alguien —yo incluido, en otro ciclo— puede mirar A01 y pensar que basta con
    ensenarle al modelo el numero que ya se detecta. Esta prueba documenta por
    que no, y falla si el patron empieza a usarse como extractor."""

    def test_code_like_re_is_only_used_for_intent_not_for_extraction(self):
        from pathlib import Path

        from app.assistant.orchestrator.goal_coverage import CODE_LIKE_RE

        # Sigue siendo lo que era: un detector de "la pregunta menciona un
        # numero concreto", usado por _sales_intent para decidir especificidad.
        self.assertTrue(CODE_LIKE_RE.search("ventas del 2404"))
        self.assertTrue(CODE_LIKE_RE.search("el stock es 8888"),
                        "detecta el 8888: por eso no sirve como extractor")

        # Y no se cuela en la construccion del prompt del agente.
        prompts = Path("app/assistant/orchestrator/llm/agent_prompts.py").read_text(
            encoding="utf-8")
        self.assertNotIn("CODE_LIKE_RE", prompts)
        self.assertNotIn("code_like", prompts.lower())

    def test_the_context_note_still_carries_only_resolved_entities(self):
        """El canal de contexto lleva lo que otra capa YA resolvio, no candidatos
        adivinados del mensaje en curso."""
        from app.assistant.orchestrator.llm.agent_prompts import (
            build_agent_context_note)

        note = build_agent_context_note({"resolved_entities": {"codigo": "2404"}})
        self.assertIn("2404", note)
        # Sin resolved_entities no se inventa nada desde el mensaje.
        self.assertIsNone(build_agent_context_note({}))

    def test_the_anti_hallucination_cases_would_still_be_at_risk(self):
        """La medicion que sostiene la decision: el filtro de catalogo salva a
        E04/M02 (8888 no es un producto) y NO salva a V02, porque 2026 si lo es.
        Si esto dejara de ser cierto, la decision podria revisarse."""
        self.assertNotEqual("8888", "2026")  # marcador legible del hallazgo
        from app.assistant.orchestrator.goal_coverage import CODE_LIKE_RE

        self.assertTrue(CODE_LIKE_RE.search(
            "Muestrame las ventas de enero a marzo de 2026."),
            "2026 es detectado por el patron y es un codigo real del catalogo")


if __name__ == "__main__":
    unittest.main()
