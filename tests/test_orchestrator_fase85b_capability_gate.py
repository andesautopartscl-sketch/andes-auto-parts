"""FASE 8.5B — la reja que hacia invisible la escalera.

La corrida con LLM real posterior a 8.5 dio CERO usos de la escalera analitica.
Parecia un fallo del modelo y no lo era: el structured output schema fijaba
``kind`` a dato|inferencia, ``op`` a las seis operaciones viejas, no declaraba
``assumptions`` y ponia additionalProperties=False. El modelo era FISICAMENTE
incapaz de emitir un peldano nuevo. La corrida no midio la capacidad, midio la
reja — y el benchmark tampoco tenia una sola pregunta de proyeccion.

Estos tests fijan las dos mitades: que con el flag apagado nada cambia (byte a
byte), y que encendido la capacidad existe de verdad de punta a punta.
"""
from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from app.assistant.orchestrator.agent_config import analysis_enabled
from app.assistant.orchestrator.llm.agent_prompts import build_agent_system_prompt
from app.assistant.orchestrator.llm.plan_schema import (
    AGENT_DECISION_JSON_SCHEMA,
    agent_decision_response_format,
)


def _schema(analytical: bool = True) -> dict:
    """8.6: la puerta tiene DOS dimensiones. El flag habilita la capacidad; el
    turno analitico decide si viaja. Cargarla siempre costo +17% de tokens y
    rompio dos preguntas inequivocas, asi que 'flag encendido' ya no basta."""
    return agent_decision_response_format(
        analytical=analytical)["json_schema"]["schema"]


class FlagOffChangesNothingTests(unittest.TestCase):
    """Apagado, lo que viaja al proveedor es exactamente lo de siempre."""

    def setUp(self):
        self._env = patch.dict(
            os.environ, {"ANDES_ASSISTANT_ANALYSIS_ENABLED": "0"}, clear=False)
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_the_flag_is_off_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANDES_ASSISTANT_ANALYSIS_ENABLED", None)
            self.assertFalse(analysis_enabled())

    def test_the_schema_is_the_untouched_one(self):
        self.assertIs(_schema(), AGENT_DECISION_JSON_SCHEMA)
        self.assertIs(_schema(analytical=False), AGENT_DECISION_JSON_SCHEMA)

    def test_the_claim_kinds_stay_closed(self):
        kinds = _schema()["properties"]["claims"]["items"]["properties"]["kind"]["enum"]
        self.assertEqual(kinds, ["dato", "inferencia"])

    def test_the_prompt_says_nothing_about_the_ladder(self):
        prompt = build_agent_system_prompt(analytical=True)
        for word in ("proyeccion", "supuesto", "assumptions", "mul"):
            self.assertNotIn(word, prompt, word)


class FlagOnOpensTheContractTests(unittest.TestCase):
    def setUp(self):
        self._env = patch.dict(
            os.environ, {"ANDES_ASSISTANT_ANALYSIS_ENABLED": "1"}, clear=False)
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_the_new_kinds_become_emittable(self):
        kinds = _schema()["properties"]["claims"]["items"]["properties"]["kind"]["enum"]
        for kind in ("calculo", "supuesto", "proyeccion", "recomendacion"):
            self.assertIn(kind, kinds, kind)

    def test_multiplication_becomes_emittable(self):
        ops = _schema()["properties"]["calculations"]["items"]["properties"]["op"]["enum"]
        self.assertIn("mul", ops)

    def test_assumptions_become_declarable(self):
        props = _schema()["properties"]
        self.assertIn("assumptions", props)
        kinds = props["assumptions"]["items"]["properties"]["kind"]["enum"]
        self.assertIn("horizon_months", kinds)

    def test_the_assumption_basis_stays_a_closed_set(self):
        """Si 'basis' fuera texto libre, el modelo podria justificar cualquier
        numero con una frase y el guard de analysis.py quedaria sin efecto."""
        props = _schema()["properties"]["assumptions"]["items"]["properties"]
        self.assertEqual(props["basis"]["enum"], ["user_request", "default"])

    def test_only_declared_assumption_kinds_are_offered(self):
        from app.assistant.orchestrator.analysis import ASSUMPTION_KINDS

        offered = _schema()["properties"]["assumptions"]["items"]["properties"]["kind"]["enum"]
        self.assertEqual(set(offered), set(ASSUMPTION_KINDS))

    def test_a_normal_turn_never_carries_the_ladder(self):
        """La regresion medida: T02 y A01 eran preguntas normales y el bloque las
        rompio. Con el flag encendido un turno no analitico no debe verlo."""
        self.assertIs(_schema(analytical=False), AGENT_DECISION_JSON_SCHEMA)
        self.assertNotIn("proyeccion", build_agent_system_prompt(analytical=False))

    def test_the_block_no_longer_names_temporal_units(self):
        """Repetir 'meses'/'dias' en cada prompt fue lo que cebo la lectura de
        '2404' como '24/04'. El schema enumera los tipos; el prompt no hace falta
        que los repita."""
        from app.assistant.orchestrator.llm.agent_prompts import ANALYSIS_BLOCK

        for word in ("horizon_months", "window_days", "lead_time_days"):
            self.assertNotIn(word, ANALYSIS_BLOCK, word)

    def test_the_prompt_documents_the_ladder(self):
        prompt = build_agent_system_prompt(analytical=True)
        for word in ("proyeccion", "supuesto", "calculo", "recomendacion", "mul"):
            self.assertIn(word, prompt, word)

    def test_the_prompt_does_not_police_what_the_verifier_polices(self):
        """8.9: el bloque pedia al modelo vigilar "solo si el usuario escribio
        ese numero". Medido con LLM real: A01 volvio a pedir el codigo de una
        pregunta que lo contenia, porque la instruccion le hacia escrutar los
        numeros del enunciado y "2404" es uno. validate_assumptions ya rechaza
        la decision entera si el origen no cuadra, asi que pedirselo al modelo
        no anadia garantia y si dano."""
        from app.assistant.orchestrator.llm.agent_prompts import ANALYSIS_BLOCK

        for word in ("escribio", "user_request", "basis", "default"):
            self.assertNotIn(word, ANALYSIS_BLOCK, word)

    def test_the_prompt_only_appends(self):
        """Encendido extiende; no reescribe. Una reescritura no medida ya costo
        T07 (9/10 -> 0/10) en 8.1I.2."""
        on = build_agent_system_prompt(analytical=True)
        with patch.dict(os.environ, {"ANDES_ASSISTANT_ANALYSIS_ENABLED": "0"},
                        clear=False):
            off = build_agent_system_prompt(analytical=True)
        self.assertTrue(on.startswith(off))

    def test_the_base_schema_is_never_mutated(self):
        """Se construye por copia: si se mutara, apagar el flag ya no volveria
        al contrato original y el A/B seria imposible."""
        _schema()
        base = AGENT_DECISION_JSON_SCHEMA["properties"]["claims"]["items"]
        self.assertEqual(base["properties"]["kind"]["enum"], ["dato", "inferencia"])

    def test_the_schema_stays_serialisable_and_strict(self):
        fmt = agent_decision_response_format()
        self.assertTrue(fmt["json_schema"]["strict"])
        json.dumps(fmt)


class LadderUsageSignalTests(unittest.TestCase):
    """Hay que poder distinguir 'no sabe usarla' de 'no puede usarla'."""

    def _analysis(self, gold: dict, run: dict) -> dict:
        from evals.fase81_scorer import _analysis

        return _analysis(gold, run)

    def test_a_plain_answer_reports_no_ladder(self):
        out = self._analysis({}, {"reply": "DATOS:\n- El stock es 2."})
        self.assertFalse(out["used_ladder"])
        self.assertEqual(out["rungs_used"], [])

    def test_a_full_ladder_is_detected(self):
        reply = ("DATOS:\n- a\nCÁLCULOS:\n- b\nSUPUESTOS:\n- c\n"
                 "PROYECCIÓN:\n- d\nRECOMENDACIÓN:\n- e")
        out = self._analysis({"analysis_expected": True}, {"reply": reply})
        self.assertTrue(out["used_ladder"])
        self.assertEqual(out["rungs_used"],
                         ["calculo", "supuesto", "proyeccion", "recomendacion"])

    def test_the_expectation_is_carried_from_the_gold(self):
        out = self._analysis({"analysis_expected": True}, {"reply": ""})
        self.assertTrue(out["expected"])

    def _summary(self, **kw):
        from evals.fase81_scorer import _analysis_summary

        return _analysis_summary(
            [{"id": "A01", "analysis": {"expected": True, "used_ladder": False,
                                        "rungs_used": []}}], **kw)

    def test_the_summary_names_the_disabled_state(self):
        summary = self._summary(analysis_enabled=False)
        self.assertIn("no habilitada", summary["state"])
        self.assertEqual(summary["cases_expecting_analysis"], 1)
        self.assertEqual(summary["cases_using_ladder"], 0)

    def test_zero_usage_with_the_flag_on_is_not_reported_as_disabled(self):
        """FASE 8.x — `state` era un literal fijo emitido siempre que la escalera
        no se usara, y NO leia la variable de entorno. La tercera A/B reporto
        "ANDES_ASSISTANT_ANALYSIS_ENABLED=0" en los dos brazos, incluido el que
        corrio encendido, tapando el hallazgo real: el modelo no emitio ni un
        peldano en 76 casos con la capacidad disponible."""
        summary = self._summary(analysis_enabled=True)
        self.assertNotIn("no habilitada", summary["state"])
        self.assertNotIn("ANALYSIS_ENABLED=0", summary["state"])
        self.assertIn("NO usada", summary["state"])
        self.assertIs(summary["analysis_enabled"], True)

    def test_an_uninformed_arm_says_so_instead_of_guessing(self):
        summary = self._summary()
        self.assertIn("no informado", summary["state"])
        self.assertIsNone(summary["analysis_enabled"])

    def test_the_flag_reaches_the_summary_through_aggregate(self):
        from evals.fase81_scorer import aggregate

        agg = aggregate([{"id": "A01", "pass": True,
                          "analysis": {"expected": True, "used_ladder": False,
                                       "rungs_used": []}}],
                        analysis_enabled=True)
        self.assertIs(agg["analysis"]["analysis_enabled"], True)


if __name__ == "__main__":
    unittest.main()
