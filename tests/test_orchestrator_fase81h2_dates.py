"""FASE 8.1H.2 — semantic date grounding.

8.1H fixed substring number grounding but masked only the ISO date form, so
"31 de julio de 2026" and "31/07/2026" decayed into loose numbers and were
rejected even when the evidence held that exact date.

The contract here:
  - a date in a claim is grounded when it names a date the evidence contains,
    in ANY supported form, compared after normalization;
  - a date NEVER grounds its own day, month or year as independent figures;
  - the 8.1H number rule is untouched: "3 unidades" still needs 3 as an evidence
    token or a verified calculation.
"""
from __future__ import annotations

import unittest
from typing import Any

from app.assistant.orchestrator.answer_verifier import (
    _claim_grounded,
    _claim_number_tokens,
    _evidence_blob,
    grounded_dates,
    grounded_numbers,
    mask_dates,
    verify_agent_answer,
)
from app.assistant.orchestrator.evidence_store import EvidenceStore

ISO = "2026-07-31"


def _store(*payloads: dict[str, Any], tool: str = "get_stock_movements") -> EvidenceStore:
    store = EvidenceStore()
    for data in payloads:
        store.add_from_tool_result(
            tool=tool, arguments={},
            result={"ok": True, "empty": False, "data": data, "meta": {}},
        )
    return store


def _grounded(store: EvidenceStore, text: str, verified: list[Any] | None = None) -> bool:
    return _claim_grounded(
        text,
        _evidence_blob(store, verified),
        {str(i.tool or "").upper() for i in store.items},
        grounded_numbers(store, verified),
        grounded_dates(store),
    )


class CanonicalFormTests(unittest.TestCase):
    def test_every_supported_form_normalizes_to_the_same_date(self):
        for text in (
            "2026-07-31",
            "31/07/2026",
            "31-07-2026",
            "31.07.2026",
            "31/07/26",
            "31 de julio de 2026",
            "31 de Julio del 2026",
        ):
            self.assertEqual(mask_dates(text)[1], [ISO], text)

    def test_a_date_leaves_no_digits_behind(self):
        for text in ("2026-07-31", "31/07/2026", "31 de julio de 2026"):
            masked, found = mask_dates(text)
            self.assertEqual(found, [ISO], text)
            self.assertEqual(_claim_number_tokens(masked), [], text)

    def test_day_and_month_without_year_is_a_partial_reference(self):
        self.assertEqual(mask_dates("el 31 de julio")[1], ["??-07-31"])

    def test_invalid_dates_are_not_dates(self):
        for text in ("31/45/2026", "99 de julio de 2026", "2026-13-01"):
            self.assertEqual(mask_dates(text)[1], [], text)

    def test_september_spelling_variants(self):
        self.assertEqual(mask_dates("16 de septiembre de 2026")[1], ["2026-09-16"])
        self.assertEqual(mask_dates("16 de setiembre de 2026")[1], ["2026-09-16"])


class DateGroundingTests(unittest.TestCase):
    def _iso_evidence(self) -> EvidenceStore:
        return _store({"items": [{"fecha": ISO, "cantidad": 2}], "count": 1})

    def test_iso_evidence_iso_claim(self):
        self.assertTrue(_grounded(self._iso_evidence(), f"El ingreso fue el {ISO}."))

    def test_iso_evidence_dd_mm_yyyy_claim(self):
        self.assertTrue(_grounded(self._iso_evidence(), "El ingreso fue el 31/07/2026."))

    def test_iso_evidence_spanish_text_claim(self):
        store = self._iso_evidence()
        self.assertTrue(_grounded(store, "El ingreso fue el 31 de julio de 2026."))
        self.assertTrue(_grounded(store, "Se registraron 2 unidades el 31 de julio de 2026."))

    def test_text_evidence_iso_claim(self):
        store = _store({"items": [{"observacion": "recibido el 31 de julio de 2026"}]})
        self.assertTrue(_grounded(store, f"El ingreso fue el {ISO}."))

    def test_dmy_evidence_text_claim(self):
        store = _store({"items": [{"observacion": "recibido 31/07/2026"}]})
        self.assertTrue(_grounded(store, "El ingreso fue el 31 de julio de 2026."))

    def test_different_dates_do_not_match(self):
        store = self._iso_evidence()
        for claim in (
            "El ingreso fue el 2026-07-30.",
            "El ingreso fue el 30/07/2026.",
            "El ingreso fue el 31 de agosto de 2026.",
            "El ingreso fue el 31 de julio de 2025.",
        ):
            self.assertFalse(_grounded(store, claim), claim)

    def test_partial_reference_matches_only_a_real_day_and_month(self):
        store = self._iso_evidence()
        self.assertTrue(_grounded(store, "el movimiento del 31 de julio"))
        self.assertFalse(_grounded(store, "el movimiento del 30 de julio"))

    def test_no_evidence_dates_grounds_no_date(self):
        store = _store({"total_stock": 2})
        self.assertFalse(_grounded(store, f"El ingreso fue el {ISO}."))


class DatesDoNotBecomeNumbersTests(unittest.TestCase):
    """The 8.1H guarantee must survive: a date is not three loose figures."""

    def test_date_components_are_not_grounded_numbers(self):
        store = _store({"items": [{"fecha": ISO}]})
        numbers = grounded_numbers(store)
        for part in ("7", "31", "2026", "26", "6"):
            self.assertNotIn(part, numbers, part)

    def test_claims_citing_date_components_as_figures_are_rejected(self):
        store = _store({"items": [{"fecha": ISO}]})
        for claim in ("hubo 7 salidas", "hubo 31 salidas", "hubo 2026 salidas"):
            self.assertFalse(_grounded(store, claim), claim)

    def test_a_real_quantity_next_to_a_date_still_needs_its_own_grounding(self):
        store = _store({"items": [{"fecha": ISO, "cantidad": 2}]})
        self.assertTrue(_grounded(store, "el 31 de julio ingresaron 2 unidades"))
        self.assertFalse(_grounded(store, "el 31 de julio ingresaron 9 unidades"))

    def test_textual_date_in_evidence_does_not_leak_numbers_either(self):
        store = _store({"items": [{"observacion": "recibido el 31 de julio de 2026"}]})
        numbers = grounded_numbers(store)
        for part in ("31", "2026", "7"):
            self.assertNotIn(part, numbers, part)


class NumberRuleUnchangedTests(unittest.TestCase):
    """8.1H regression guards, re-asserted after the date change."""

    def test_digit_substring_false_positive_still_rejected(self):
        store = _store({"total_stock": 12345}, tool="get_inventory")
        self.assertTrue(_grounded(store, "hay 12345 unidades"))
        self.assertFalse(_grounded(store, "hay 5 unidades"))
        self.assertFalse(_grounded(store, "hay 234 unidades"))

    def _c04_store(self) -> EvidenceStore:
        return _store(
            {
                "items": [
                    {"fecha": "2026-07-31", "numero_documento": "340273", "cantidad": 2},
                    {"fecha": "2026-05-11", "numero_documento": "333210", "cantidad": 1},
                ],
                "count": 2,
            },
            tool="get_ingresos",
        )

    def test_c04_unresolved_calculation_still_rejects_the_three(self):
        store = self._c04_store()
        self.assertNotIn("3", grounded_numbers(store))
        self.assertFalse(_grounded(store, "suman 3 unidades (2 + 1)"))
        out = verify_agent_answer(
            store=store,
            decision={
                "action": "final_answer", "draft_reply": "",
                "claims": [{"kind": "dato", "text": "los ingresos suman 3 unidades",
                            "evidence_ids": ["e1"]}],
                "calculations": [{"id": "c1", "op": "sum", "inputs": ["e1.data.total"], "result": 3}],
            },
        )
        self.assertEqual(out.calc_unresolved, 1)
        self.assertNotIn("suman 3", out.reply)

    def test_c04_valid_calculation_still_passes(self):
        store = self._c04_store()
        self.assertTrue(_grounded(store, "suman 3 unidades (2 + 1)", verified=[3.0]))
        out = verify_agent_answer(
            store=store,
            decision={
                "action": "final_answer", "draft_reply": "",
                "claims": [{"kind": "dato", "text": "los ingresos suman 3 unidades",
                            "evidence_ids": ["e1"]}],
                "calculations": [{"id": "c1", "op": "sum",
                                  "inputs": ["e1.data.items.0.cantidad", "e1.data.items.1.cantidad"],
                                  "result": 3}],
            },
        )
        self.assertEqual(out.failures, 0)
        self.assertIn("3", out.reply)

    def test_movements_answer_with_a_date_survives_end_to_end(self):
        """The T05/T08 shape: a movements claim that cites a real date."""
        store = self._c04_store()
        out = verify_agent_answer(
            store=store,
            decision={
                "action": "final_answer", "draft_reply": "",
                "claims": [
                    {"kind": "dato",
                     "text": "El 31 de julio de 2026 ingresaron 2 unidades.", "evidence_ids": ["e1"]},
                    {"kind": "dato",
                     "text": "El 11 de mayo de 2026 ingreso 1 unidad.", "evidence_ids": ["e1"]},
                ],
                "calculations": [],
            },
        )
        self.assertEqual(out.failures, 0)
        self.assertEqual(out.dropped_claims, 0)
        self.assertFalse(out.answer_replaced)
        self.assertIn("31 de julio", out.reply)
        self.assertIn("11 de mayo", out.reply)


class GuardrailUnchangedTests(unittest.TestCase):
    """M02 / M04 must behave exactly as before."""

    def _inventory(self) -> EvidenceStore:
        return _store({"codigo": "2404", "total_stock": 2,
                       "items": [{"bodega": "Bodega 1", "stock": 2}]}, tool="get_inventory")

    def test_m02_shape_still_replaces_the_answer(self):
        out = verify_agent_answer(
            store=self._inventory(),
            decision={
                "action": "final_answer", "draft_reply": "El stock del 2404 es 8888",
                "claims": [{"kind": "dato", "text": "El stock del 2404 es 8888",
                            "evidence_ids": ["e1"]}],
                "calculations": [],
            },
        )
        self.assertTrue(out.answer_replaced)
        self.assertEqual(out.replaced_reason, "no_grounded_claim")
        self.assertNotIn("8888", out.reply)

    def test_m04_shape_still_drops_only_the_remembered_number(self):
        out = verify_agent_answer(
            store=self._inventory(),
            decision={
                "action": "final_answer", "draft_reply": "",
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


if __name__ == "__main__":
    unittest.main()
