"""FASE 8.1H — numeric grounding. A figure is stated only if the evidence attests it.

The contract:
  1. the figure appears as a whole numeric TOKEN in this turn's evidence, or
  2. it is the result of a calculation the verifier recomputed and matched.
Never a substring.

Scope: the verifier's number check only. AgentLoop, GoalCoverage, ProgressLedger,
allow_partial, ToolRunner, Gateway, memory, history and the scorer are untouched.
"""
from __future__ import annotations

import unittest
from typing import Any

from app.assistant.orchestrator.answer_verifier import (
    _claim_grounded,
    _claim_number_tokens,
    _evidence_blob,
    _norm_number,
    grounded_dates,
    grounded_numbers,
    mask_dates,
    verify_agent_answer,
)
from app.assistant.orchestrator.evidence_store import EvidenceStore


def _store(*payloads: dict[str, Any], tool: str = "get_inventory") -> EvidenceStore:
    store = EvidenceStore()
    for data in payloads:
        store.add_from_tool_result(
            tool=tool,
            arguments={},
            result={"ok": True, "empty": False, "data": data, "meta": {}},
        )
    return store


def _grounded(store: EvidenceStore, text: str, verified: list[Any] | None = None) -> bool:
    numbers = grounded_numbers(store, verified)
    dates = grounded_dates(store)
    blob = _evidence_blob(store, verified)
    names = {str(i.tool or "").upper() for i in store.items}
    return _claim_grounded(text, blob, names, numbers, dates)


def _tokens(text: str) -> list[str]:
    """Figures a claim asserts, after dates are masked (the production order)."""
    return _claim_number_tokens(mask_dates(text)[0])


class NumberNormalizationTests(unittest.TestCase):
    def test_equivalent_forms_normalize_together(self):
        for a, b in ((2, "2"), (2.0, "2"), ("2,0", "2"), ("1,5", "1.5"), (1.50, "1.5")):
            self.assertEqual(_norm_number(a), _norm_number(b), f"{a!r} vs {b!r}")

    def test_non_numeric_is_none(self):
        for raw in (None, True, False, "", "abc", "2024-01-01", [], {}):
            self.assertIsNone(_norm_number(raw), repr(raw))

    def test_claim_tokenizer_masks_dates_and_codes(self):
        self.assertEqual(_tokens("El 2026-09-16 hubo 4 salidas"), ["4"])
        self.assertEqual(_tokens("El 16/09/2026 hubo 4 salidas"), ["4"])
        self.assertEqual(_tokens("El 16 de septiembre de 2026 hubo 4 salidas"), ["4"])
        self.assertEqual(_tokens("La OC-77 trae 5 items"), ["5"])
        self.assertEqual(_tokens("total 12345"), ["12345"])
        self.assertEqual(_tokens("son 2 y 1"), ["2", "1"])

    def test_tokenizer_does_not_split_a_larger_number(self):
        self.assertEqual(_tokens("12345"), ["12345"])
        self.assertNotIn("5", _tokens("12345"))

    def test_trailing_punctuation_does_not_hide_a_figure(self):
        self.assertEqual(_tokens("el stock es 2."), ["2"])


class FalsePositiveTests(unittest.TestCase):
    """The bug: substring matching accepted digits that were never stated."""

    def test_digit_substring_of_a_larger_integer_is_not_grounded(self):
        store = _store({"total_stock": 12345})
        self.assertTrue(_grounded(store, "hay 12345 unidades"))
        self.assertFalse(_grounded(store, "hay 5 unidades"))
        self.assertFalse(_grounded(store, "hay 234 unidades"))

    def test_digit_inside_a_date_is_not_grounded(self):
        store = _store({"items": [{"fecha": "2026-09-16", "tipo": "salida"}]})
        for claim in ("hubo 9 salidas", "hubo 16 salidas", "hubo 2026 salidas"):
            self.assertFalse(_grounded(store, claim), claim)

    def test_a_date_itself_is_still_accepted(self):
        store = _store({"items": [{"fecha": "2026-09-16", "cantidad": 4}]})
        self.assertTrue(_grounded(store, "el 2026-09-16 salieron 4 unidades"))

    def test_date_in_claim_absent_from_evidence_is_rejected(self):
        store = _store({"items": [{"fecha": "2026-09-16", "cantidad": 4}]})
        self.assertFalse(_grounded(store, "el 2025-01-01 salieron 4 unidades"))

    def _c04_store(self) -> EvidenceStore:
        """The real get_ingresos payload shape for 2404.

        No field equals 3, but "3" occurs inside numero_documento "340273", inside
        margen_pct 62.38 and inside the dates — which is exactly why the old
        substring check accepted "suman 3 unidades".
        """
        return _store(
            {
                "items": [
                    {"fecha": "2026-07-31", "numero_documento": "340273",
                     "proveedor": "FITALIA", "codigo": "2404", "cantidad": 2,
                     "costo_neto": 7900.0, "margen_pct": 62.38},
                    {"fecha": "2026-05-11", "numero_documento": "333210",
                     "proveedor": "FITALIA", "codigo": "2404", "cantidad": 1,
                     "costo_neto": 7900.0, "margen_pct": 62.38},
                ],
                "count": 2,
            },
            tool="get_ingresos",
        )

    def test_c04_shape_unverified_sum_is_not_grounded(self):
        """C04: "suman 3 unidades (2 + 1)" while the calculation did not verify."""
        store = self._c04_store()
        blob = _evidence_blob(store, None)
        self.assertIn("3", blob, "the false positive needs a 3 somewhere in the blob")
        self.assertNotIn("3", grounded_numbers(store))
        self.assertFalse(_grounded(store, "suman 3 unidades (2 + 1)"))
        self.assertTrue(_grounded(store, "hay 2 y 1 unidades"))

    def test_c04_shape_becomes_grounded_once_the_sum_is_verified(self):
        store = self._c04_store()
        self.assertTrue(_grounded(store, "suman 3 unidades (2 + 1)", verified=[3.0]))

    def test_c04_document_number_still_groundable_as_itself(self):
        store = self._c04_store()
        self.assertTrue(_grounded(store, "el documento 340273 trae 2 unidades"))


class TruePositiveTests(unittest.TestCase):
    def test_exact_integer_is_grounded(self):
        store = _store({"total_stock": 3})
        self.assertTrue(_grounded(store, "hay 3 unidades"))

    def test_decimals_match_across_separators(self):
        store = _store({"precio": 1.5})
        self.assertTrue(_grounded(store, "el precio es 1.5"))
        self.assertTrue(_grounded(store, "el precio es 1,5"))
        self.assertFalse(_grounded(store, "el precio es 15"))

    def test_string_encoded_numbers_count(self):
        store = _store({"codigo": "2404", "stock": "7"})
        self.assertTrue(_grounded(store, "el 2404 tiene 7 unidades"))

    def test_numbers_inside_free_text_descriptions_count(self):
        store = _store({"items": [{"descripcion": "FILTRO 2404 X2"}]})
        self.assertTrue(_grounded(store, "el 2404 aparece 2 veces"))

    def test_negative_numbers(self):
        store = _store({"items": [{"cantidad": -5}]})
        self.assertTrue(_grounded(store, "el movimiento fue -5 unidades"))
        self.assertTrue(_grounded(store, "bajo 5 unidades"))
        self.assertFalse(_grounded(store, "bajo 4 unidades"))

    def test_arguments_and_meta_also_ground_figures(self):
        store = EvidenceStore()
        store.add_from_tool_result(
            tool="get_stock_movements",
            arguments={"codigo": "2404", "limit": 20},
            result={"ok": True, "empty": False, "data": {}, "meta": {"count": 9}},
        )
        self.assertTrue(_grounded(store, "se pidieron 20 filas del 2404"))
        self.assertTrue(_grounded(store, "hay 9 registros"))
        self.assertFalse(_grounded(store, "hay 8 registros"))

    def test_mixed_evidence_from_several_tools(self):
        store = EvidenceStore()
        store.add_from_tool_result(
            tool="get_ingresos", arguments={},
            result={"ok": True, "empty": False, "data": {"items": [{"cantidad": 2}]}, "meta": {}},
        )
        store.add_from_tool_result(
            tool="get_inventory", arguments={},
            result={"ok": True, "empty": False, "data": {"total_stock": 7}, "meta": {}},
        )
        self.assertTrue(_grounded(store, "ingresaron 2 y quedan 7"))
        self.assertFalse(_grounded(store, "ingresaron 2 y quedan 9"))


class CalculationGroundingTests(unittest.TestCase):
    def _store(self) -> EvidenceStore:
        return _store({"items": [{"cantidad": 2}, {"cantidad": 1}], "total_stock": 10})

    def _verify(self, calc: dict[str, Any], text: str) -> Any:
        return verify_agent_answer(
            store=self._store(),
            decision={
                "action": "final_answer",
                "draft_reply": "",
                "claims": [{"kind": "dato", "text": text, "evidence_ids": ["e1"]}],
                "calculations": [calc],
            },
        )

    def test_verified_calculation_grounds_its_result(self):
        out = self._verify(
            {"id": "c1", "op": "sum",
             "inputs": ["e1.data.items.0.cantidad", "e1.data.items.1.cantidad"], "result": 3},
            "los ingresos suman 3 unidades",
        )
        self.assertEqual(out.failures, 0)
        self.assertEqual(out.calc_mismatch, 0)
        self.assertFalse(out.answer_replaced)
        self.assertIn("3", out.reply)

    def test_calculation_mismatch_does_not_ground_the_result(self):
        out = self._verify(
            {"id": "c1", "op": "sum",
             "inputs": ["e1.data.items.0.cantidad", "e1.data.items.1.cantidad"], "result": 99},
            "los ingresos suman 99 unidades",
        )
        self.assertEqual(out.calc_mismatch, 1)
        self.assertNotIn("99", out.reply)

    def test_calculation_unresolved_does_not_ground_the_result(self):
        out = self._verify(
            {"id": "c1", "op": "sum", "inputs": ["e9.data.total"], "result": 3},
            "los ingresos suman 3 unidades",
        )
        self.assertEqual(out.calc_unresolved, 1)
        self.assertNotIn("3 unidades", out.reply)

    def test_a_grounded_claim_survives_next_to_a_failed_calculation(self):
        out = verify_agent_answer(
            store=self._store(),
            decision={
                "action": "final_answer",
                "draft_reply": "",
                "claims": [
                    {"kind": "dato", "text": "el stock total es 10", "evidence_ids": ["e1"]},
                    {"kind": "dato", "text": "los ingresos suman 3 unidades", "evidence_ids": ["e1"]},
                ],
                "calculations": [{"id": "c1", "op": "sum", "inputs": ["e9.data.x"], "result": 3}],
            },
        )
        self.assertIn("10", out.reply)
        self.assertNotIn("suman 3", out.reply)
        self.assertFalse(out.answer_replaced)
        self.assertEqual(out.dropped_claims, 1)


class GuardrailNotWeakenedTests(unittest.TestCase):
    """M02 / M04: a user-supplied number is still not evidence."""

    def _inventory(self) -> EvidenceStore:
        return _store({"codigo": "2404", "total_stock": 2,
                       "items": [{"bodega": "Bodega 1", "stock": 2}]})

    def test_m04_shape_drops_the_remembered_number_and_publishes_the_rest(self):
        out = verify_agent_answer(
            store=self._inventory(),
            decision={
                "action": "final_answer",
                "draft_reply": "",
                "claims": [
                    {"kind": "dato", "text": "El stock del 2404 es 2 unidades", "evidence_ids": ["e1"]},
                    {"kind": "inferencia", "text": "Bajo desde las 25 unidades", "evidence_ids": ["e1"]},
                ],
                "calculations": [],
            },
        )
        self.assertEqual(out.dropped_claims, 1)
        self.assertFalse(out.answer_replaced)
        self.assertIn("2 unidades", out.reply)
        self.assertNotIn("25", out.reply)

    def test_m02_shape_replaces_the_answer_when_nothing_is_grounded(self):
        out = verify_agent_answer(
            store=self._inventory(),
            decision={
                "action": "final_answer",
                "draft_reply": "El stock del 2404 es 8888",
                "claims": [{"kind": "dato", "text": "El stock del 2404 es 8888", "evidence_ids": ["e1"]}],
                "calculations": [],
            },
        )
        self.assertTrue(out.answer_replaced)
        self.assertEqual(out.replaced_reason, "no_grounded_claim")
        self.assertNotIn("8888", out.reply)

    def test_clean_answer_still_passes_untouched(self):
        out = verify_agent_answer(
            store=self._inventory(),
            decision={
                "action": "final_answer",
                "draft_reply": "",
                "claims": [{"kind": "dato",
                            "text": "El 2404 tiene 2 unidades en Bodega 1", "evidence_ids": ["e1"]}],
                "calculations": [],
            },
        )
        self.assertTrue(out.ok)
        self.assertEqual(out.failures, 0)
        self.assertEqual(out.dropped_claims, 0)
        self.assertIn("Bodega 1", out.reply)

    def test_percentage_needs_a_grounded_numeric_part(self):
        store = _store({"variacion_pct": 15})
        self.assertTrue(_grounded(store, "subio 15%"))
        self.assertFalse(_grounded(store, "subio 42%"))

    def test_empty_evidence_grounds_nothing(self):
        store = EvidenceStore()
        self.assertEqual(grounded_numbers(store), set())
        self.assertFalse(_grounded(store, "hay 1 unidad"))
        self.assertTrue(_grounded(store, "sin resultados"))


if __name__ == "__main__":
    unittest.main()
