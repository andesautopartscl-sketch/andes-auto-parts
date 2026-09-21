"""FASE 8.2 — claim provenance: ¿la cifra viene de la evidencia que el claim cita?

``evidence_ids`` se parseaba y se ignoraba. Un claim podia citar e1 mientras cada
cifra suya venia de e2, o citar un id inexistente, y el verifier lo aceptaba
porque medía contra el blob global.

Arranca en modo OBSERVACION: la violacion se cuenta y se reporta, el claim no se
descarta. ANDES_ASSISTANT_PROVENANCE_ENFORCE=1 lo vuelve efectivo. Asi se puede
medir la tasa real antes de endurecer, sin romper respuestas correctas mal citadas.
"""
from __future__ import annotations

import os
import unittest
from typing import Any
from unittest.mock import patch

from app.assistant.orchestrator.agent_config import provenance_enforced
from app.assistant.orchestrator.answer_verifier import (
    evidence_ids_in_paths,
    grounded_dates,
    grounded_numbers,
    verify_agent_answer,
)
from app.assistant.orchestrator.evidence_store import EvidenceStore


def _two_tools() -> EvidenceStore:
    """e1 = ingresos (2 y 1 unidades), e2 = inventario (total 7)."""
    store = EvidenceStore()
    store.add_from_tool_result(
        tool="get_ingresos", arguments={"codigo": "2404"},
        result={"ok": True, "empty": False, "meta": {},
                "data": {"items": [{"cantidad": 2, "fecha": "2026-07-31"},
                                   {"cantidad": 1, "fecha": "2026-05-12"}]}},
    )
    store.add_from_tool_result(
        tool="get_inventory", arguments={"codigo": "2404"},
        result={"ok": True, "empty": False, "meta": {},
                "data": {"total_stock": 7, "items": [{"bodega": "Bodega 9", "stock": 7}]}},
    )
    return store


def _verify(store: EvidenceStore, claims: list[dict[str, Any]],
            calcs: list[dict[str, Any]] | None = None):
    return verify_agent_answer(
        store=store,
        decision={"action": "final_answer", "draft_reply": "",
                  "claims": claims, "calculations": calcs or []},
    )


def _claim(text: str, eids: list[str], kind: str = "dato") -> dict[str, Any]:
    return {"kind": kind, "text": text, "evidence_ids": eids}


class ScopePrimitiveTests(unittest.TestCase):
    def test_scope_restricts_the_citable_figures(self):
        store = _two_tools()
        self.assertEqual(grounded_numbers(store, scope={"e1"}) & {"7"}, set())
        self.assertIn("7", grounded_numbers(store, scope={"e2"}))
        self.assertIn("2", grounded_numbers(store, scope={"e1"}))

    def test_scope_restricts_the_citable_dates(self):
        store = _two_tools()
        self.assertIn("2026-07-31", grounded_dates(store, scope={"e1"}))
        self.assertEqual(grounded_dates(store, scope={"e2"}), set())

    def test_no_scope_means_everything(self):
        store = _two_tools()
        every = grounded_numbers(store)
        self.assertTrue({"2", "1", "7"} <= every)

    def test_evidence_ids_are_read_from_calculation_paths(self):
        self.assertEqual(
            evidence_ids_in_paths(["e1.data.items.0.cantidad", "e1.data.items.1.cantidad"]),
            {"e1"},
        )
        self.assertEqual(evidence_ids_in_paths(["e1.data.a", "e2.data.b"]), {"e1", "e2"})
        self.assertEqual(evidence_ids_in_paths([]), set())


class ObservationModeTests(unittest.TestCase):
    """Por defecto: se cuenta, no se descarta."""

    def setUp(self):
        self._env = patch.dict(os.environ, {"ANDES_ASSISTANT_PROVENANCE_ENFORCE": "0"},
                               clear=False)
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_the_flag_is_off_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANDES_ASSISTANT_PROVENANCE_ENFORCE", None)
            self.assertFalse(provenance_enforced())

    def test_a_figure_from_another_evidence_item_is_flagged_not_dropped(self):
        out = _verify(_two_tools(), [_claim("El stock total es 7 unidades.", ["e1"])])
        self.assertEqual(out.provenance_violations, 1)
        self.assertEqual(out.provenance_kinds, ["figure_outside_cited_evidence"])
        self.assertEqual(out.dropped_claims, 0)
        self.assertIn("7", out.reply)
        self.assertFalse(out.provenance_enforced)

    def test_an_unknown_evidence_id_is_flagged(self):
        out = _verify(_two_tools(), [_claim("El stock total es 7 unidades.", ["e9"])])
        self.assertEqual(out.provenance_kinds, ["unknown_evidence_id"])
        self.assertEqual(out.dropped_claims, 0)

    def test_a_correctly_cited_claim_has_no_violation(self):
        out = _verify(_two_tools(), [_claim("El stock total es 7 unidades.", ["e2"])])
        self.assertEqual(out.provenance_violations, 0)
        self.assertEqual(out.failures, 0)

    def test_citing_both_items_covers_figures_from_both(self):
        out = _verify(
            _two_tools(),
            [_claim("Ingresaron 2 unidades y quedan 7.", ["e1", "e2"])],
        )
        self.assertEqual(out.provenance_violations, 0)

    def test_a_claim_without_citation_is_not_checked(self):
        """Sin evidence_ids no hay afirmacion de procedencia que validar."""
        out = _verify(_two_tools(), [_claim("El stock total es 7 unidades.", [])])
        self.assertEqual(out.provenance_violations, 0)

    def test_a_date_from_another_item_is_flagged_as_its_own_kind(self):
        """Severidad por clase: una fecha mal atribuida duele menos que un
        derivado, porque la fecha sí existe en el turno."""
        out = _verify(_two_tools(), [_claim("El 31 de julio de 2026 hubo movimiento.", ["e2"])])
        self.assertEqual(out.provenance_kinds, ["date_outside_cited_evidence"])
        self.assertEqual(out.provenance_severity, "medium")

    def test_an_unknown_evidence_id_is_high_severity(self):
        out = _verify(_two_tools(), [_claim("El stock total es 7 unidades.", ["e9"])])
        self.assertEqual(out.provenance_severity, "high")

    def test_a_misattributed_derived_figure_is_high_severity(self):
        out = _verify(
            _two_tools(),
            [_claim("Los ingresos suman 3 unidades.", ["e2"])],
            [{"id": "c1", "op": "sum",
              "inputs": ["e1.data.items.0.cantidad", "e1.data.items.1.cantidad"],
              "result": 3}],
        )
        self.assertEqual(out.provenance_kinds, ["derived_figure_outside_cited_evidence"])
        self.assertEqual(out.provenance_severity, "high")

    def test_a_clean_answer_has_no_severity(self):
        out = _verify(_two_tools(), [_claim("El stock total es 7 unidades.", ["e2"])])
        self.assertEqual(out.provenance_severity, "none")
        self.assertEqual(out.provenance_by_kind, {})


class EnforcementModeTests(unittest.TestCase):
    """Con el flag encendido la violacion descarta el claim."""

    def setUp(self):
        self._env = patch.dict(os.environ, {"ANDES_ASSISTANT_PROVENANCE_ENFORCE": "1"},
                               clear=False)
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_the_miscited_claim_is_dropped(self):
        out = _verify(
            _two_tools(),
            [_claim("Ingresaron 2 unidades.", ["e1"]),
             _claim("El stock total es 7 unidades.", ["e1"])],
        )
        self.assertTrue(out.provenance_enforced)
        self.assertEqual(out.provenance_violations, 1)
        self.assertEqual(out.dropped_claims, 1)
        self.assertIn("2 unidades", out.reply)
        self.assertNotIn("7 unidades", out.reply)

    def test_a_correctly_cited_claim_survives_enforcement(self):
        out = _verify(_two_tools(), [_claim("El stock total es 7 unidades.", ["e2"])])
        self.assertEqual(out.dropped_claims, 0)
        self.assertIn("7", out.reply)


class CalculationProvenanceTests(unittest.TestCase):
    """Una calculation solo respalda al claim que cita la evidencia que lee."""

    def setUp(self):
        self._env = patch.dict(os.environ, {"ANDES_ASSISTANT_PROVENANCE_ENFORCE": "1"},
                               clear=False)
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_a_sum_over_e1_does_not_ground_a_claim_citing_only_e2(self):
        out = _verify(
            _two_tools(),
            [_claim("Los ingresos suman 3 unidades.", ["e2"])],
            [{"id": "c1", "op": "sum",
              "inputs": ["e1.data.items.0.cantidad", "e1.data.items.1.cantidad"],
              "result": 3}],
        )
        self.assertEqual(out.calc_mismatch, 0)
        self.assertEqual(out.provenance_violations, 1)
        self.assertNotIn("suman 3", out.reply)

    def test_the_same_sum_grounds_the_claim_that_cites_e1(self):
        out = _verify(
            _two_tools(),
            [_claim("Los ingresos suman 3 unidades.", ["e1"])],
            [{"id": "c1", "op": "sum",
              "inputs": ["e1.data.items.0.cantidad", "e1.data.items.1.cantidad"],
              "result": 3}],
        )
        self.assertEqual(out.provenance_violations, 0)
        self.assertEqual(out.failures, 0)
        self.assertIn("3", out.reply)

    def test_a_count_over_the_wrong_collection_is_caught_by_provenance(self):
        """count prueba cardinalidad, no pertenencia: la procedencia es lo que
        impide respaldar un claim con la lista equivocada."""
        store = _two_tools()
        out = _verify(
            store,
            [_claim("Hay 2 bodegas con stock.", ["e2"])],
            [{"id": "c1", "op": "count", "inputs": ["e1.data.items"], "result": 2}],
        )
        self.assertEqual(out.provenance_violations, 1)
        self.assertNotIn("2 bodegas", out.reply)


class GuardrailsUnchangedTests(unittest.TestCase):
    """Nada de 8.1 se relaja por añadir procedencia."""

    def test_an_ungrounded_figure_is_still_dropped_in_observation_mode(self):
        with patch.dict(os.environ, {"ANDES_ASSISTANT_PROVENANCE_ENFORCE": "0"}, clear=False):
            out = _verify(_two_tools(), [_claim("El stock es 8888.", ["e2"])])
        self.assertEqual(out.dropped_claims, 1)
        self.assertNotIn("8888", out.reply)

    def test_an_unresolvable_calculation_is_still_rejected(self):
        with patch.dict(os.environ, {"ANDES_ASSISTANT_PROVENANCE_ENFORCE": "0"}, clear=False):
            out = _verify(_two_tools(), [_claim("Suman 3 unidades.", ["e1"])],
                          [{"id": "c1", "op": "sum", "inputs": ["e1.data.total"], "result": 3}])
        self.assertEqual(out.calc_unresolved, 1)
        self.assertNotIn("Suman 3", out.reply)

    def test_the_breakdown_carries_the_new_signal(self):
        out = _verify(_two_tools(), [_claim("El stock total es 7 unidades.", ["e1"])])
        bd = out.breakdown()
        for key in ("provenance_violations", "provenance_kinds", "provenance_enforced"):
            self.assertIn(key, bd)


if __name__ == "__main__":
    unittest.main()
