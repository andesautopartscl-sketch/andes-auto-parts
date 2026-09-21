"""FASE 8.x — un vacio explicado, y el alcance de una consulta.

Tres defectos encontrados midiendo O04 y V02 contra el ERP real. Los tres
comparten la forma: el sistema CONOCIA el hecho y no lo declaraba, asi que nadie
aguas abajo podia usarlo ni contradecirlo.

1. **La rama `not_found` del composer era codigo muerto.** `compose_answer`
   cortocircuita en `item["empty"]` antes de llamar al formateador de la tool,
   y el normalizador marca `empty=True` en cuanto `count==0` o `items==[]`. Asi
   que "No existe ese codigo en el catalogo" no se publico nunca — ni por la via
   `oem` ni por la via `codigo`. Las pruebas de 8.9 no lo veian porque
   construian la evidencia con `empty=False`, un estado que el pipeline no
   produce para una lista vacia. Por eso estas pruebas arman la evidencia con
   `normalize_tool_result`: un test que inventa un estado imposible valida una
   rama imposible.

2. **El ERP solo declaraba `not_found` por la via `codigo`.** Un OEM inexistente
   volvia como `{items: [], count: 0}`, indistinguible de "no busque nada".

3. **`get_sales` callaba su alcance cuando no habia filtro de fecha.** Medido en
   V02: "las ventas de enero a marzo de 2026" devuelve CERO, pero el sistema no
   puede expresar un periodo en lenguaje natural — `normalize_agent_args`
   descarta toda fecha que no aparezca como YYYY-MM-DD en la pregunta, que es la
   regla anti-alucinacion correcta — asi que la llamada sale sin filtro y trae 2
   documentos de 2026-04-07. Abril. Todas las cifras estaban grounded y la
   respuesta era falsa igualmente. Declarar el alcance no resuelve la pregunta;
   impide que la respuesta finja haberla resuelto.
"""
from __future__ import annotations

import unittest

from app.assistant.orchestrator.composer import (
    EXPLAINED_EMPTY_FLAGS,
    compose_answer,
)
from app.assistant.orchestrator.normalizer import normalize_tool_result


def _evidence(tool: str, data: dict) -> dict:
    """Evidencia como la produce el pipeline, no como conviene al test."""
    item = normalize_tool_result(200, {"ok": True, "tool": tool,
                                       "classification": "INTERNAL",
                                       "data": data, "meta": {}})
    item["tool"] = tool
    return item


def _reply(tool: str, data: dict) -> str:
    return compose_answer(plan={"steps": [{"step": 1, "tool": tool}]},
                          evidence=[_evidence(tool, data)])["reply"]


class TheEmptyShortCircuitWasEatingTheFactsTests(unittest.TestCase):

    def test_an_empty_list_really_does_mark_the_evidence_empty(self):
        """La premisa del defecto. Si esto cambiara, las pruebas de abajo
        dejarian de medir lo que creen medir."""
        item = _evidence("get_equivalences",
                         {"items": [], "count": 0, "matched_on": "oem",
                          "not_found": True})
        self.assertTrue(item["empty"])

    def test_a_missing_oem_is_stated_as_a_fact_through_the_real_state(self):
        reply = _reply("get_equivalences",
                       {"items": [], "count": 0, "matched_on": "oem",
                        "query": {"oem": "ZZZNOEXISTE999"}, "not_found": True})
        self.assertIn("ZZZNOEXISTE999", reply)
        self.assertIn("declara el OEM", reply)
        self.assertNotIn("no devolvió resultados", reply)

    def test_a_missing_code_is_stated_as_a_fact_through_the_real_state(self):
        """Esta rama tampoco se alcanzaba, y esa via existia desde 8.8."""
        reply = _reply("get_equivalences",
                       {"items": [], "count": 0, "matched_on": "codigo",
                        "query": {"codigo": "NOEXISTE1"}, "not_found": True})
        self.assertIn("No existe ese código", reply)
        self.assertNotIn("no devolvió resultados", reply)

    def test_a_product_without_oem_is_not_the_same_as_not_found(self):
        reply = _reply("get_equivalences",
                       {"items": [], "count": 0, "matched_on": "codigo",
                        "query": {"codigo": "2404"}, "no_oem_declared": True})
        self.assertIn("no declara", reply)
        self.assertNotIn("No existe", reply)

    def test_the_wording_follows_matched_on_not_a_guess(self):
        """"No existe ese codigo" es falso cuando lo que falta es un OEM."""
        by_oem = _reply("get_equivalences",
                        {"items": [], "count": 0, "matched_on": "oem",
                         "query": {"oem": "X999"}, "not_found": True})
        by_code = _reply("get_equivalences",
                         {"items": [], "count": 0, "matched_on": "codigo",
                          "query": {"codigo": "X999"}, "not_found": True})
        self.assertNotEqual(by_oem, by_code)
        self.assertIn("OEM", by_oem)
        self.assertNotIn("OEM", by_code)

    def test_an_unexplained_empty_keeps_the_generic_sentence(self):
        """El cambio es una excepcion declarada, no un cambio de regla."""
        reply = _reply("get_equivalences",
                       {"items": [], "count": 0, "matched_on": "oem",
                        "query": {"oem": "X999", "marca": "BOSCH"}})
        self.assertIn("no devolvió resultados", reply)

    def test_the_opt_in_is_an_explicit_short_list(self):
        self.assertEqual(EXPLAINED_EMPTY_FLAGS, ("not_found", "no_oem_declared"))

    def test_no_other_tool_changes_behaviour(self):
        """Ninguna otra tool emite las banderas, asi que ninguna cambia."""
        for tool, data in (("get_inventory", {"items": [], "count": 0}),
                           ("get_sales", {"items": [], "count": 0}),
                           ("get_purchase_orders", {"items": [], "count": 0})):
            with self.subTest(tool=tool):
                self.assertIn("no devolvió resultados", _reply(tool, data))


class AnAmbiguousEmptyIsNotAFactTests(unittest.TestCase):
    """Con marca o modelo filtrando, el OEM podria existir y no estar en esa
    marca. Afirmar que no existe seria falso, y el ERP no lo afirma."""

    def test_the_erp_rule_is_anchored_and_unfiltered_only(self):
        from app.internal_agent import equivalences

        src = equivalences.__file__
        with open(src, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn('matched_on == "oem" and not items and not marca and not modelo',
                      text)


class SalesAlwaysDeclaresItsScopeTests(unittest.TestCase):

    def _sales(self, **extra) -> str:
        data = {"unidades": 0, "documentos": 2, "count": 2, "ingresos": 0.0,
                "notas_credito": {"documentos": 2, "unidades": 5},
                "items": [{"fecha": "2026-04-07", "numero": "FA-0001",
                           "codigo": "2417", "cantidad": 1}]}
        data.update(extra)
        return _reply("get_sales", data)

    def test_an_unfiltered_window_says_so(self):
        reply = self._sales(periodo={"desde": None, "hasta": None})
        self.assertIn("sin filtro de fecha", reply)
        self.assertIn("todo el historial", reply)

    def test_a_filtered_window_still_prints_its_dates(self):
        reply = self._sales(periodo={"desde": "2026-01-01", "hasta": "2026-03-31"})
        self.assertIn("2026-01-01", reply)
        self.assertIn("2026-03-31", reply)
        self.assertNotIn("sin filtro", reply)

    def test_a_payload_without_the_field_does_not_invent_a_scope(self):
        """Compatibilidad hacia atras: sin el campo, no se afirma nada."""
        reply = self._sales()
        self.assertNotIn("Periodo:", reply)

    def test_the_returns_are_still_always_stated(self):
        """No perder la invariante de 8.9 al tocar la misma funcion."""
        self.assertIn("devuelta", self._sales(
            periodo={"desde": None, "hasta": None}).lower())

    def test_the_erp_emits_the_field_unconditionally(self):
        from app.internal_agent import sales

        with open(sales.__file__, encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn('if fecha_desde or fecha_hasta:\n        data["periodo"]', text)
        self.assertIn('data["periodo"] = {', text)


if __name__ == "__main__":
    unittest.main()
