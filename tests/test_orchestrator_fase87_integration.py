"""FASE 8.7 — lo que sólo aparece cuando la cadena corre de verdad.

Validar get_sales por HTTP real —orquestador → Gateway → ERP → BD— destapó dos
cosas que ninguna prueba en proceso podía ver:

1. **Dos servicios corrían código de ayer.** El Gateway (:5055) y el ERP (:5000)
   son procesos aparte sin reloader. `get_sales` existía en disco y respondía
   `tool_not_allowed` por HTTP. Una capacidad nueva no está entregada hasta que
   los procesos que la sirven se reinician.

2. **`finance_redacted` nunca se emitía fuera del dashboard.** El campo viaja
   hasta el EvidenceStore y el audit, y el composer tiene un aviso al usuario
   —"Montos financieros no disponibles por permisos (null; no se reportan como
   0)"— que sólo se disparaba para get_dashboard_kpis. Para ventas, ingresos u
   órdenes de compra el sistema omitía el dinero EN SILENCIO, que es justo la
   confusión null/cero que existe para impedir. Medido tras el arreglo:
   get_purchase_orders devuelve True para albertadmin.
"""
from __future__ import annotations

import json
import unittest

from app.assistant.orchestrator.conversation_context import extract_entities_from_evidence
from app.assistant.orchestrator.llm.agent_prompts import build_agent_context_note
from app.assistant.orchestrator.normalizer import FINANCE_TOOL_FIELDS, _finance_withheld


class FinanceRedactionTests(unittest.TestCase):
    """El aviso existía; la señal que lo dispara, no."""

    def test_money_present_is_not_redacted(self):
        self.assertFalse(_finance_withheld(
            "get_sales", {"ingresos": 0.0, "items": [{"subtotal": 1}]}))

    def test_money_missing_with_rows_is_redacted(self):
        """El caso real: hay ventas y no hay montos -> se ocultaron."""
        self.assertTrue(_finance_withheld("get_sales", {"items": [{"cantidad": 2}]}))

    def test_an_empty_result_is_never_redacted(self):
        """Sin filas no hay montos porque no hay nada, no porque se oculten.
        Marcarlo daría un aviso confuso sobre permisos que el usuario sí tiene."""
        self.assertFalse(_finance_withheld("get_sales", {"items": []}))
        self.assertFalse(_finance_withheld("get_sales", {}))

    def test_a_scalar_total_counts_as_money(self):
        self.assertFalse(_finance_withheld("get_sales", {"ingresos": 1234.0}))

    def test_every_financial_tool_is_declared(self):
        """Declarativo: añadir una tool financiera es añadir una fila, no otro
        `if` por nombre. get_dashboard_kpis conserva su regla propia porque su
        señal es null-en-escalar, no ausencia-de-campo."""
        for tool in ("get_sales", "get_ingresos", "get_purchase_orders"):
            self.assertIn(tool, FINANCE_TOOL_FIELDS, tool)

    def test_a_non_financial_tool_is_never_redacted(self):
        self.assertFalse(_finance_withheld("get_inventory", {"items": [{"stock": 2}]}))

    def test_the_normalizer_sets_the_flag(self):
        from app.assistant.orchestrator.normalizer import normalize_tool_result

        out = normalize_tool_result(200, {
            "ok": True, "tool": "get_sales", "classification": "CONFIDENTIAL",
            "data": {"items": [{"cantidad": 2, "codigo": "2404"}], "count": 1},
            "meta": {}})
        self.assertTrue(out["finance_redacted"])

    def test_the_composer_warns_when_money_was_withheld(self):
        """Sin este aviso el usuario lee 'sin montos' como 'sin dinero'."""
        from app.assistant.orchestrator.composer import compose_answer

        out = compose_answer(
            plan={"steps": [{"step": 1, "tool": "get_sales"}]},
            evidence=[{"tool": "get_sales", "ok": True, "empty": False,
                       "finance_redacted": True, "data": {"items": [], "count": 0},
                       "meta": {}}])
        self.assertIn("permisos", out["reply"].lower())


class SalesContextTests(unittest.TestCase):
    """El periodo es el ancla de las secuencias de ventas."""

    def _entities(self, data: dict) -> dict:
        return extract_entities_from_evidence(
            [{"tool": "get_sales", "ok": True, "data": data, "meta": {}}])

    def test_the_queried_window_is_remembered(self):
        ents = self._entities({"codigo": "2417", "unidades": 0,
                               "periodo": {"desde": "2026-01-01",
                                           "hasta": "2026-03-31"}})
        self.assertEqual(ents["periodo_desde"], "2026-01-01")
        self.assertEqual(ents["periodo_hasta"], "2026-03-31")

    def test_the_code_is_remembered_too(self):
        ents = self._entities({"codigo": "2417", "unidades": 0})
        self.assertEqual(ents["codigo"], "2417")
        self.assertIn("2417", ents["codigos"])

    def test_no_window_means_no_invented_window(self):
        """Si no se consultó un periodo, no se inventa uno: el contexto refleja
        lo que se hizo, no lo que podría haberse hecho."""
        ents = self._entities({"codigo": "2417", "unidades": 0})
        self.assertIsNone(ents["periodo_desde"])
        self.assertIsNone(ents["periodo_hasta"])

    def test_the_window_reaches_the_model(self):
        """Sin esto, "compárame con el trimestre anterior" no tiene contra qué
        compararse y el modelo inventaría un periodo o pediría aclaración por
        algo que ya estaba resuelto."""
        note = build_agent_context_note({"resolved_entities": self._entities(
            {"codigo": "2417", "periodo": {"desde": "2026-01-01",
                                           "hasta": "2026-03-31"}})})
        self.assertIn("periodo_desde", note)
        self.assertIn("2026-03-31", note)

    def test_the_context_note_carries_no_money_or_pii(self):
        note = build_agent_context_note({"resolved_entities": self._entities(
            {"codigo": "2417", "ingresos": 132400.0, "cliente": "ACME",
             "periodo": {"desde": "2026-01-01", "hasta": "2026-03-31"}})})
        for leaked in ("132400", "ACME", "ingresos"):
            self.assertNotIn(leaked, note, leaked)

    def test_context_is_never_evidence(self):
        """El contexto orienta; no puede fundamentar una cifra. Se comprueba que
        el prompt lo sigue diciendo, porque es la frontera que impide citar
        stock desde la memoria de conversación."""
        from app.assistant.orchestrator.llm.agent_prompts import SYSTEM_AGENT

        self.assertIn("NUNCA evidencia ERP", SYSTEM_AGENT)


class SalesViewTests(unittest.TestCase):
    """La UI de ventas sale de la misma evidencia verificada que el texto."""

    def _view(self, data: dict) -> dict:
        from app.assistant.orchestrator.answer_view import build_answer_view
        from app.assistant.orchestrator.evidence_store import EvidenceStore

        store = EvidenceStore()
        store.add_from_tool_result(
            tool="get_sales", arguments={"codigo": "2417"},
            result={"ok": True, "empty": False, "meta": {}, "data": data})
        return build_answer_view(store).as_dict()

    def test_aggregates_render_as_summary(self):
        block = self._view({"unidades": 5, "documentos": 2, "ingresos": 500.0,
                            "items": []})["blocks"][0]
        names = {f["name"] for f in block["summary"]}
        self.assertTrue({"unidades", "documentos", "ingresos"} <= names)

    def test_revenue_is_absent_when_the_erp_withheld_it(self):
        """La tarjeta no puede enseñar lo que el ERP no entregó a este actor."""
        block = self._view({"unidades": 5, "documentos": 2, "items": []})["blocks"][0]
        self.assertNotIn("ingresos", {f["name"] for f in block["summary"]})

    def test_the_detail_sample_never_poses_as_the_total(self):
        block = self._view({"unidades": 500, "documentos": 90, "detalle_parcial": True,
                            "items": [{"numero": f"FA-{i}", "fecha": "2026-04-07",
                                       "codigo": "2417", "cantidad": 1}
                                      for i in range(40)]})["blocks"][0]
        self.assertTrue(block["truncated"])
        self.assertLess(block["shown_rows"], block["total_rows"])

    def test_no_contact_data_can_reach_a_card(self):
        blob = json.dumps(self._view({
            "unidades": 1, "items": [{"numero": "FA-1", "cliente": "ACME",
                                      "cliente_email": "x@y.z",
                                      "cliente_telefono": "555",
                                      "cliente_rut": "1-9"}]}), ensure_ascii=False)
        for leaked in ("x@y.z", "555", "1-9"):
            self.assertNotIn(leaked, blob, leaked)


class ForecastingReadinessTests(unittest.TestCase):
    """8.5 sigue sin validar, y el motivo es de DATOS, no de mecanismo.

    Medido contra la base real: las dos únicas ventas fueron devueltas íntegras
    (bruto 5 unidades / 132 400, notas de crédito 5 / 132 400 → neto CERO). No
    hay demanda realizada positiva sobre la que proyectar nada.
    """

    def test_the_ladder_mechanism_is_ready(self):
        from app.assistant.orchestrator.analysis import (
            ASSUMPTION_KINDS, analytical_intent)

        self.assertTrue(analytical_intent("cuanto deberia comprar para dos meses"))
        self.assertIn("horizon_months", ASSUMPTION_KINDS)

    def test_a_projection_still_needs_a_declared_assumption(self):
        """Que falte demanda no afloja el guard: una proyección sin supuesto
        sigue siendo una cifra sin condicional."""
        from app.assistant.orchestrator.answer_verifier import verify_agent_answer
        from app.assistant.orchestrator.evidence_store import EvidenceStore

        store = EvidenceStore()
        store.add_from_tool_result(
            tool="get_sales", arguments={"codigo": "2417"},
            result={"ok": True, "empty": False, "meta": {},
                    "data": {"unidades": 0, "items": []}})
        out = verify_agent_answer(store=store, decision={
            "action": "final_answer", "draft_reply": "",
            "claims": [{"kind": "proyeccion", "text": "Necesitarias 24 unidades.",
                        "evidence_ids": ["e1"], "assumption_ids": []}],
            "calculations": [], "assumptions": []})
        self.assertGreaterEqual(out.dropped_claims, 1)
        self.assertNotIn("24", out.reply)

    def test_committed_orders_are_not_silently_treated_as_demand(self):
        """orden_venta es demanda COMPROMETIDA y puede ser una señal legítima,
        pero convertirla en demanda por defecto seria una decisión de negocio
        tomada por el código. Hay que pedirla explícitamente."""
        from app.internal_agent.sales import SALE_TIPOS, validate_sales_args

        self.assertNotIn("orden_venta", SALE_TIPOS)
        self.assertEqual(validate_sales_args({})["tipos"], list(SALE_TIPOS))
        self.assertIn("orden_venta",
                      validate_sales_args({"tipos": ["orden_venta"]})["tipos"])


if __name__ == "__main__":
    unittest.main()
