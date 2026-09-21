"""FASE 8.5 — la escalera analítica, y por qué un supuesto no es una puerta trasera.

Medido antes de construir nada: una proyección se descartaba.

    claims=[{"kind": "dato", "text": "El stock actual es 7 unidades."},
            {"kind": "dato", "text": "Para cubrir dos meses necesitarías 24 unidades."}]
    -> dropped_claims: 1

Y con razón: 24 no está en la evidencia. Marcarla ``inferencia`` tampoco servía,
porque una inferencia con cifras se rechaza de plano. El resultado es que toda la
mitad analítica del negocio —cobertura, demanda, riesgo de quiebre, necesidad de
reposición— era inalcanzable por arquitectura, no por falta de modelo.

El riesgo de abrir esa puerta es evidente y estos tests son sobre eso: si el
modelo pudiera declarar supuestos libremente, bastaría "supongo que la demanda es
500/mes" para derivar cualquier cifra. La defensa es que un supuesto sólo puede
recoger un parámetro que el usuario pidió o uno que la casa declaró: nunca puede
introducir información nueva sobre el negocio.
"""
from __future__ import annotations

import unittest
from typing import Any

from app.assistant.orchestrator.agent_schema import (
    CALC_OPS,
    CLAIM_KINDS,
    AgentDecisionError,
    validate_agent_decision,
)
from app.assistant.orchestrator.analysis import (
    ASSUMPTION_DEFAULTS,
    ASSUMPTION_KINDS,
    AssumptionError,
    numbers_in_question,
    order_claims,
    validate_assumptions,
)
from app.assistant.orchestrator.answer_verifier import (
    recompute_calculation,
    verify_agent_answer,
)
from app.assistant.orchestrator.evidence_store import EvidenceStore

Q = "Con estas ventas, cuanto stock deberia tener para dos meses?"


def _store() -> EvidenceStore:
    store = EvidenceStore()
    store.add_from_tool_result(
        tool="get_inventory", arguments={"codigo": "2404"},
        result={"ok": True, "empty": False, "meta": {},
                "data": {"codigo": "2404", "total_stock": 7}})
    store.add_from_tool_result(
        tool="get_stock_movements", arguments={"codigo": "2404"},
        result={"ok": True, "empty": False, "meta": {},
                "data": {"items": [{"fecha": "2026-07-31", "tipo": "venta",
                                    "cantidad": 12}]}})
    return store


def _decision(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "action": "final_answer", "draft_reply": "",
        "assumptions": [{"id": "a1", "kind": "horizon_months", "value": 2,
                         "basis": "user_request"}],
        "calculations": [
            {"id": "c1", "op": "mul",
             "inputs": ["e2.data.items.0.cantidad", "a1.value"], "result": 24},
            {"id": "c2", "op": "diff",
             "inputs": ["c1", "e1.data.total_stock"], "result": 17}],
        "claims": [
            {"kind": "dato", "text": "El stock actual del 2404 es 7 unidades.",
             "evidence_ids": ["e1"]},
            {"kind": "supuesto", "text": "", "assumption_ids": ["a1"]},
            {"kind": "proyeccion",
             "text": "Para cubrir dos meses necesitarias 24 unidades.",
             "assumption_ids": ["a1"], "evidence_ids": ["e2"]},
            {"kind": "recomendacion",
             "text": "Podrias evaluar una reposicion cercana a 17 unidades.",
             "evidence_ids": ["e1", "e2"]}],
    }
    base.update(over)
    return base


def _verify(decision: dict[str, Any], question: str = Q):
    return verify_agent_answer(
        store=_store(), decision=validate_agent_decision(decision, user_message=question))


class AssumptionsCannotInventDataTests(unittest.TestCase):
    """Si un supuesto pudiera traer un número propio, todo lo demás sobra."""

    def test_a_value_the_user_never_wrote_is_rejected(self):
        with self.assertRaises(AssumptionError):
            validate_assumptions(
                [{"id": "a1", "kind": "horizon_months", "value": 7,
                  "basis": "user_request"}], question=Q)

    def test_a_default_that_is_not_the_declared_default_is_rejected(self):
        with self.assertRaises(AssumptionError):
            validate_assumptions(
                [{"id": "a1", "kind": "horizon_months", "value": 5,
                  "basis": "default"}], question=Q)

    def test_an_invented_assumption_kind_is_rejected(self):
        """Éste es el vector peligroso: 'supongo una demanda de 500/mes'."""
        with self.assertRaises(AssumptionError):
            validate_assumptions(
                [{"id": "a1", "kind": "demanda_mensual", "value": 500,
                  "basis": "default"}], question=Q)

    def test_an_out_of_range_value_is_rejected(self):
        with self.assertRaises(AssumptionError):
            validate_assumptions(
                [{"id": "a1", "kind": "horizon_months", "value": 9000,
                  "basis": "default"}], question=Q)

    def test_an_invented_basis_is_rejected(self):
        with self.assertRaises(AssumptionError):
            validate_assumptions(
                [{"id": "a1", "kind": "horizon_months", "value": 2,
                  "basis": "me_parece_razonable"}], question=Q)

    def test_a_bad_assumption_invalidates_the_whole_decision(self):
        """Descartarlo en silencio publicaría la proyección sin su condicional."""
        with self.assertRaises(AgentDecisionError):
            validate_agent_decision(
                _decision(assumptions=[{"id": "a1", "kind": "horizon_months",
                                        "value": 9, "basis": "user_request"}]),
                user_message=Q)

    def test_every_declared_kind_has_a_declared_default(self):
        self.assertEqual(set(ASSUMPTION_KINDS), set(ASSUMPTION_DEFAULTS))

    def test_defaults_sit_inside_their_own_range(self):
        for kind, value in ASSUMPTION_DEFAULTS.items():
            spec = ASSUMPTION_KINDS[kind]
            self.assertGreaterEqual(value, spec["min"], kind)
            self.assertLessEqual(value, spec["max"], kind)

    def test_a_spelled_out_number_counts_as_asked(self):
        """'para dos meses' y 'para 2 meses' son la misma petición."""
        self.assertIn(2.0, numbers_in_question("stock para dos meses"))
        self.assertIn(3.0, numbers_in_question("hazlo para el trimestre"))

    def test_too_many_assumptions_are_rejected(self):
        with self.assertRaises(AssumptionError):
            validate_assumptions(
                [{"id": f"a{i}", "kind": "horizon_months", "value": 2,
                  "basis": "default"} for i in range(1, 20)], question=Q)


class ProjectionsRequireTheirConditionTests(unittest.TestCase):
    def test_the_full_ladder_publishes(self):
        out = _verify(_decision())
        self.assertEqual(out.failures, 0)
        self.assertEqual(out.dropped_claims, 0)
        for label in ("DATOS:", "SUPUESTOS:", "PROYECCI", "RECOMENDACI"):
            self.assertIn(label, out.reply)

    def test_a_projection_without_an_assumption_is_dropped(self):
        """Sin supuesto no es una proyección: es una cifra sin condicional."""
        decision = _decision()
        for claim in decision["claims"]:
            if claim["kind"] == "proyeccion":
                claim["assumption_ids"] = []
        out = _verify(decision)
        self.assertGreaterEqual(out.dropped_claims, 1)
        self.assertNotIn("24 unidades", out.reply)

    def test_a_projection_citing_an_unknown_assumption_is_dropped(self):
        decision = _decision()
        for claim in decision["claims"]:
            if claim["kind"] == "proyeccion":
                claim["assumption_ids"] = ["a9"]
        self.assertNotIn("24 unidades", _verify(decision).reply)

    def test_a_projection_whose_figure_is_not_recomputable_is_dropped(self):
        decision = _decision(calculations=[])
        self.assertNotIn("24 unidades", _verify(decision).reply)

    def test_the_labels_are_what_keeps_a_projection_from_reading_as_a_fact(self):
        reply = _verify(_decision()).reply
        self.assertLess(reply.index("DATOS:"), reply.index("SUPUESTOS:"))
        self.assertLess(reply.index("SUPUESTOS:"), reply.index("PROYECCI"))

    def test_the_assumption_sentence_is_not_written_by_the_model(self):
        """El modelo no puede editorializar un supuesto ni colar una cifra ahí."""
        decision = _decision()
        for claim in decision["claims"]:
            if claim["kind"] == "supuesto":
                claim["text"] = "Supongo, razonablemente, una demanda de 500/mes."
        reply = _verify(decision).reply
        self.assertNotIn("500", reply)
        self.assertIn("horizonte de cobertura: 2 meses", reply)


class ChainedDerivationTests(unittest.TestCase):
    def test_a_calculation_can_build_on_a_verified_one(self):
        out = _verify(_decision())
        self.assertIn("17 unidades", out.reply)

    def test_a_forward_reference_does_not_resolve(self):
        """Sólo se apoya en lo ya demostrado: sin esto habría ciclos."""
        decision = _decision(calculations=[
            {"id": "c1", "op": "diff", "inputs": ["c2", "e1.data.total_stock"],
             "result": 17},
            {"id": "c2", "op": "mul",
             "inputs": ["e2.data.items.0.cantidad", "a1.value"], "result": 24}])
        self.assertGreater(_verify(decision).calc_unresolved, 0)

    def test_mul_recomputes(self):
        self.assertEqual(
            recompute_calculation(_store(), {"id": "c1", "op": "mul",
                                             "inputs": ["e1.data.total_stock",
                                                        "a1.value"],
                                             "result": 14},
                                  {"a1.value": 2.0}), 14.0)

    def test_an_unknown_assumption_path_raises(self):
        with self.assertRaises(KeyError):
            recompute_calculation(_store(), {"id": "c", "op": "mul",
                                             "inputs": ["a9.value",
                                                        "e1.data.total_stock"],
                                             "result": 1}, {})

    def test_mul_needs_two_inputs(self):
        with self.assertRaises(ValueError):
            recompute_calculation(_store(), {"id": "c", "op": "mul",
                                             "inputs": ["e1.data.total_stock"],
                                             "result": 7}, {})


class NothingFromEightOneIsRelaxedTests(unittest.TestCase):
    """La escalera añade peldaños; no afloja ninguno de los existentes."""

    def test_an_ungrounded_dato_is_still_dropped(self):
        out = _verify(_decision(claims=[
            {"kind": "dato", "text": "El stock es 8888.", "evidence_ids": ["e1"]}],
            calculations=[], assumptions=[]))
        self.assertEqual(out.dropped_claims, 1)
        self.assertNotIn("8888", out.reply)

    def test_an_inference_with_figures_is_still_bounded(self):
        out = _verify(_decision(claims=[
            {"kind": "inferencia", "text": "Se venderan 999 unidades.",
             "evidence_ids": ["e1"]}], calculations=[], assumptions=[]))
        self.assertNotIn("999", out.reply)

    def test_a_calculation_that_does_not_recompute_is_still_rejected(self):
        out = _verify(_decision(calculations=[
            {"id": "c1", "op": "mul",
             "inputs": ["e2.data.items.0.cantidad", "a1.value"], "result": 999}]))
        self.assertGreater(out.calc_mismatch, 0)
        self.assertNotIn("999", out.reply)

    def test_a_decision_without_the_ladder_behaves_exactly_as_before(self):
        """8.5 es aditivo: sin supuestos ni peldaños nuevos, nada cambia."""
        out = _verify({"action": "final_answer", "draft_reply": "",
                       "claims": [{"kind": "dato",
                                   "text": "El stock actual del 2404 es 7 unidades.",
                                   "evidence_ids": ["e1"]}],
                       "calculations": []}, question="Stock del 2404")
        self.assertEqual(out.failures, 0)
        self.assertEqual(out.reply, "DATOS:\n- El stock actual del 2404 es 7 unidades.")

    def test_the_claim_kinds_are_a_closed_set(self):
        self.assertEqual(CLAIM_KINDS, {"dato", "inferencia", "calculo",
                                       "supuesto", "proyeccion", "recomendacion"})

    def test_the_op_set_stays_closed(self):
        self.assertEqual(CALC_OPS, {"min", "max", "sum", "diff", "ratio",
                                    "count", "mul"})

    def test_no_write_capability_is_introduced(self):
        from pathlib import Path

        src = Path("app/assistant/orchestrator/analysis.py").read_text(encoding="utf-8")
        for forbidden in ("invoke_gateway", "execute", "commit", "INSERT", "UPDATE"):
            self.assertNotIn(forbidden, src, forbidden)


class LadderOrderTests(unittest.TestCase):
    def test_claims_are_ordered_by_rung(self):
        ordered = order_claims([
            {"kind": "recomendacion", "text": "r"}, {"kind": "dato", "text": "d"},
            {"kind": "proyeccion", "text": "p"}, {"kind": "supuesto", "text": "s"}])
        self.assertEqual([c["kind"] for c in ordered],
                         ["dato", "supuesto", "proyeccion", "recomendacion"])

    def test_order_is_stable_inside_a_rung(self):
        ordered = order_claims([{"kind": "dato", "text": "primero"},
                                {"kind": "dato", "text": "segundo"}])
        self.assertEqual([c["text"] for c in ordered], ["primero", "segundo"])


if __name__ == "__main__":
    unittest.main()
