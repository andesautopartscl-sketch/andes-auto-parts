"""FASE 8.2C — la cadena de evidencia: tool result → data_view → store → pack → verifier.

El truncado anterior sustituía un payload grande por
``{"truncated": True, "preview": "<json crudo>"}``. Eso rompía tres cosas a la vez:

- todos los paths morían, así que cualquier calculation sobre esa evidencia era
  irresoluble;
- el preview es un STRING y el tokenizador numérico lo minaba, convirtiendo texto
  serializado —incluidos números cortados a mitad— en cifras citables que nunca
  fueron valores;
- los valores que no cabían dejaban de ser citables.

Medido antes del arreglo sobre un payload de 80 filas: 73 tokens grounded salidos
del preview y 44 de 80 valores legítimos perdidos.
"""
from __future__ import annotations

import json
import unittest
from typing import Any

from app.assistant.orchestrator.agent_config import MAX_TOOL_RESULT_CHARS
from app.assistant.orchestrator.answer_verifier import (
    CollectionRequired,
    CollectionTruncated,
    grounded_numbers,
    recompute_calculation,
    verify_agent_answer,
)
from app.assistant.orchestrator.evidence_store import EvidenceStore


def _rows(n: int, pad: int = 60) -> dict[str, Any]:
    return {"items": [{"codigo": f"P{i:04d}", "cantidad": 1000 + i, "obs": "Z" * pad}
                      for i in range(n)]}


def _store(data: dict[str, Any], tool: str = "get_supplier") -> EvidenceStore:
    store = EvidenceStore()
    store.add_from_tool_result(tool=tool, arguments={"q": "a"},
                               result={"ok": True, "empty": False, "data": data, "meta": {}})
    return store


class ShapePreservationTests(unittest.TestCase):
    def test_a_small_payload_is_untouched(self):
        store = _store(_rows(3))
        item = store.items[0]
        self.assertFalse(item.truncated)
        self.assertEqual(item.omitted_rows, {})
        self.assertEqual(len(item.data_view["items"]), 3)

    def test_an_oversized_payload_keeps_its_shape(self):
        item = _store(_rows(80)).items[0]
        self.assertTrue(item.truncated)
        self.assertIsInstance(item.data_view, dict)
        self.assertIn("items", item.data_view)
        self.assertIsInstance(item.data_view["items"], list)
        self.assertNotIn("preview", item.data_view)

    def test_paths_survive_degradation(self):
        """Antes cualquier calculation sobre evidencia grande daba calc_unresolved."""
        store = _store(_rows(80))
        self.assertEqual(store.resolve_path("e1.data.items.0.cantidad"), 1000)
        self.assertIsInstance(store.resolve_path("e1.data.items"), list)

    def test_arithmetic_over_the_retained_rows_still_verifies(self):
        store = _store(_rows(80))
        value = recompute_calculation(store, {
            "id": "c1", "op": "sum",
            "inputs": ["e1.data.items.0.cantidad", "e1.data.items.1.cantidad"], "result": 0})
        self.assertEqual(value, 2001.0)

    def test_the_view_respects_the_size_cap(self):
        item = _store(_rows(300)).items[0]
        self.assertLessEqual(
            len(json.dumps(item.data_view, ensure_ascii=False)), MAX_TOOL_RESULT_CHARS)

    def test_dropped_rows_are_recorded_by_path(self):
        item = _store(_rows(80)).items[0]
        self.assertIn("data.items", item.omitted_rows)
        self.assertGreater(item.omitted_rows["data.items"], 0)
        self.assertEqual(
            item.omitted_rows["data.items"] + len(item.data_view["items"]), 80)

    def test_the_biggest_collection_is_shrunk_first(self):
        """Una lista enorme no debe costarle sus filas a las demás."""
        data = {"grande": [{"x": "Y" * 80} for _ in range(120)],
                "chica": [{"n": i} for i in range(3)]}
        item = _store(data).items[0]
        self.assertEqual(len(item.data_view["chica"]), 3)
        self.assertLess(len(item.data_view["grande"]), 120)


class GroundingIntegrityTests(unittest.TestCase):
    """El truncado no puede inventar cifras ni borrar las que conserva."""

    def test_no_figure_is_mined_from_serialized_text(self):
        data = _rows(80)
        store = _store(data)
        numbers = grounded_numbers(store)
        kept = {str(r["cantidad"]) for r in store.items[0].data_view["items"]}
        dropped = {str(r["cantidad"]) for r in data["items"]} - kept
        self.assertTrue(kept <= numbers, "los valores retenidos deben ser citables")
        self.assertFalse(dropped & numbers, "un valor descartado no puede quedar citable")

    def test_retained_values_stay_citable(self):
        store = _store(_rows(80))
        item = store.items[0]
        numbers = grounded_numbers(store)
        self.assertIn(str(item.data_view["items"][0]["cantidad"]), numbers)

    def test_an_oversized_scalar_payload_emits_no_minable_text(self):
        store = _store({"blob": "9" * (MAX_TOOL_RESULT_CHARS * 2)})
        item = store.items[0]
        self.assertTrue(item.truncated)
        blob = json.dumps(item.data_view, ensure_ascii=False)
        self.assertNotIn("9" * 50, blob)


class CardinalityAfterDegradationTests(unittest.TestCase):
    """Contar una colección degradada publicaría una cardinalidad falsa."""

    def test_count_refuses_a_degraded_collection(self):
        store = _store(_rows(80))
        with self.assertRaises(CollectionTruncated):
            recompute_calculation(store, {"id": "c1", "op": "count",
                                          "inputs": ["e1.data.items"], "result": 20})

    def test_count_still_works_on_an_intact_collection(self):
        store = _store(_rows(3))
        self.assertEqual(
            recompute_calculation(store, {"id": "c1", "op": "count",
                                          "inputs": ["e1.data.items"], "result": 3}), 3.0)

    def test_a_refused_count_does_not_publish_the_figure(self):
        store = _store(_rows(80))
        out = verify_agent_answer(store=store, decision={
            "action": "final_answer", "draft_reply": "",
            "claims": [{"kind": "dato", "text": "Hay 20 registros.", "evidence_ids": ["e1"]}],
            "calculations": [{"id": "c1", "op": "count", "inputs": ["e1.data.items"],
                              "result": 20}]})
        self.assertEqual(out.calc_error, 1)
        self.assertNotIn("20 registros", out.reply)

    def test_count_still_refuses_a_non_collection(self):
        store = _store({"total": 5})
        with self.assertRaises(CollectionRequired):
            recompute_calculation(store, {"id": "c1", "op": "count",
                                          "inputs": ["e1.data.total"], "result": 5})


class EvidencePackSignalTests(unittest.TestCase):
    def test_the_pack_tells_the_model_what_was_dropped(self):
        store = _store(_rows(80))
        entry = json.loads(store.prompt_pack())[0]
        self.assertTrue(entry["truncated"])
        self.assertIn("data.items", entry["omitted_rows"])


class EvidenceChainSecurityTests(unittest.TestCase):
    """Invariantes de seguridad de la cadena completa."""

    def test_stores_do_not_leak_across_turns(self):
        a, b = EvidenceStore(), EvidenceStore()
        a.add_from_tool_result(tool="get_inventory", arguments={},
                               result={"ok": True, "empty": False,
                                       "data": {"total_stock": 999}, "meta": {}})
        self.assertEqual(b.items, [])
        self.assertEqual(grounded_numbers(b), set())

    def test_a_spoofed_evidence_id_cannot_ground_a_figure(self):
        store = _store({"total_stock": 2}, tool="get_inventory")
        out = verify_agent_answer(store=store, decision={
            "action": "final_answer", "draft_reply": "",
            "claims": [{"kind": "dato", "text": "El stock es 999.",
                        "evidence_ids": ["e1", "e2", "e99"]}],
            "calculations": []})
        self.assertEqual(out.dropped_claims, 1)
        self.assertNotIn("999", out.reply)

    def test_paths_cannot_escape_the_evidence_structure(self):
        store = _store({"total_stock": 2}, tool="get_inventory")
        for path in ("e99.data.x", "../../etc/passwd", "__class__", "e1.__dict__",
                     "e1.arguments.__class__"):
            with self.subTest(path=path):
                with self.assertRaises((KeyError, CollectionRequired, CollectionTruncated)):
                    recompute_calculation(store, {"id": "c", "op": "count",
                                                  "inputs": [path], "result": 1})

    def test_pii_and_secrets_never_reach_the_degraded_view(self):
        store = _store({"items": [
            {"nombre": f"C{i}", "email": f"x{i}@y.z", "telefono": f"555{i}",
             "token": "sk-abc", "password": "p", "direccion": "Calle", "obs": "W" * 80}
            for i in range(60)]}, tool="get_customer")
        blob = json.dumps(store.items[0].data_view, ensure_ascii=False)
        for secret in ("@y.z", "555", "sk-abc", "password", "direccion"):
            self.assertNotIn(secret, blob, secret)

    def test_the_prompt_pack_carries_no_secrets_either(self):
        store = _store({"items": [{"token": "sk-abc", "password": "p", "n": i}
                                  for i in range(40)]}, tool="get_customer")
        pack = store.prompt_pack()
        for secret in ("sk-abc", "password"):
            self.assertNotIn(secret, pack, secret)


if __name__ == "__main__":
    unittest.main()
