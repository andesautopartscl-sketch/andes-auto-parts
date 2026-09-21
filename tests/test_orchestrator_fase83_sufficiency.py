"""FASE 8.3 — suficiencia: ¿la respuesta enseña los datos que fue a buscar?

El grounding es asimétrico por construcción: publicar una cifra equivocada cuesta
el claim entero, y no publicar ninguna no cuesta nada. Bajo ese incentivo la
jugada segura es describir la forma de los datos en vez de citarlos, y eso es lo
que se observó con LLM real sobre la MISMA pregunta y la MISMA evidencia.

Estos tests fijan tres cosas: que la métrica detecta el defecto, que NO puede
empujar a inventar, y —lo más importante— que no marca como pobres las
respuestas negativas correctas, que en un ERP son una clase grande y legítima.
"""
from __future__ import annotations

import json
import unittest
from typing import Any

from app.assistant.orchestrator.answer_sufficiency import (
    SufficiencyResult,
    analyze_sufficiency,
)
from app.assistant.orchestrator.evidence_store import EvidenceStore
from app.assistant.orchestrator.goal_coverage import GoalCoverage


def _store(tool: str, data: dict[str, Any]) -> EvidenceStore:
    store = EvidenceStore()
    store.add_from_tool_result(
        tool=tool, arguments={"codigo": "2404"},
        result={"ok": True, "empty": False, "meta": {}, "data": data})
    return store


def _movements(n: int = 3) -> dict[str, Any]:
    rows = [
        {"fecha": "2026-07-31", "tipo": "ingreso", "cantidad": 2,
         "marca": "BOSCH", "bodega": "Bodega 1"},
        {"fecha": "2026-07-31", "tipo": "ajuste", "cantidad": -1,
         "marca": "BOSCH", "bodega": "Bodega 1"},
        {"fecha": "2026-05-12", "tipo": "ingreso", "cantidad": 3,
         "marca": "BOSCH", "bodega": "Bodega 1"},
    ]
    return {"codigo": "2404", "descripcion": "FILTRO DIESEL", "items": rows[:n]}


def _analyze(store: EvidenceStore, question: str, reply: str) -> SufficiencyResult:
    goal = GoalCoverage.from_message(question)
    goal.refresh(store)
    return analyze_sufficiency(store=store, goal=goal, reply=reply, question=question)


Q_MOV = "Movimientos de stock del 2404"


class TheObservedDefectTests(unittest.TestCase):
    """Las respuestas reales que motivaron la fase, textuales."""

    def test_a_vague_summary_is_insufficient(self):
        """C07/E06/F06: describen la forma de los datos, no citan ninguno."""
        out = _analyze(_store("get_stock_movements", _movements()), Q_MOV,
                       "DATOS:\n- Movimientos de stock para el producto 2404 "
                       "incluyen ingresos y ajustes en Bodega 1 con marca BOSCH.")
        self.assertEqual(out.verdict, "insufficient")

    def test_a_concrete_answer_is_sufficient(self):
        """F01: misma evidencia, mismo turno, respuesta útil."""
        out = _analyze(_store("get_stock_movements", _movements()), Q_MOV,
                       "DATOS:\n- Ingreso de 2 unidades el 2026-07-31.\n"
                       "- Ajuste negativo de 1 unidad el 2026-07-31.")
        self.assertEqual(out.verdict, "sufficient")

    def test_citing_only_dates_is_enough(self):
        """K03: las fechas identifican las filas aunque falten las cantidades."""
        out = _analyze(_store("get_stock_movements", _movements()), Q_MOV,
                       "DATOS:\n- Movimientos en fechas 2026-07-31 y 2026-05-12.")
        self.assertEqual(out.verdict, "sufficient")

    def test_constant_labels_prove_nothing(self):
        """'Bodega 1' y 'BOSCH' son iguales en todas las filas: repetirlos no
        transmite nada que el usuario no pudiera suponer."""
        out = _analyze(_store("get_stock_movements", _movements()), Q_MOV,
                       "DATOS:\n- Hay movimientos en Bodega 1 con marca BOSCH.")
        self.assertEqual(out.verdict, "insufficient")

    def test_echoing_the_question_is_not_informing(self):
        out = _analyze(_store("get_stock_movements", _movements()), Q_MOV,
                       "DATOS:\n- Hay movimientos del producto 2404.")
        self.assertEqual(out.verdict, "insufficient")

    def test_a_directory_answered_without_names_is_insufficient(self):
        """G07: la información útil aquí es TEXTUAL. Una métrica sólo numérica
        era estructuralmente ciega a este caso."""
        store = _store("get_supplier", {"items": [
            {"nombre": "ACME REPUESTOS", "ciudad": "Santiago"},
            {"nombre": "AUTOPARTES DEL SUR", "ciudad": "Temuco"},
            {"nombre": "MAXUS CHILE", "ciudad": "Valparaiso"}]})
        out = _analyze(store, "Busca proveedores con la letra a.",
                       "DATOS:\n- Se encontraron proveedores con la letra 'a'.")
        self.assertEqual(out.verdict, "insufficient")

    def test_the_same_directory_answered_with_names_is_sufficient(self):
        store = _store("get_supplier", {"items": [
            {"nombre": "ACME REPUESTOS", "ciudad": "Santiago"},
            {"nombre": "AUTOPARTES DEL SUR", "ciudad": "Temuco"}]})
        out = _analyze(store, "Busca proveedores con la letra a.",
                       "DATOS:\n- ACME REPUESTOS (Santiago) y AUTOPARTES DEL SUR.")
        self.assertEqual(out.verdict, "sufficient")


class CannotRewardInventionTests(unittest.TestCase):
    """La utilidad nunca puede comprarse mintiendo."""

    def test_an_invented_figure_does_not_count(self):
        """8888 no está en la evidencia, así que no suma aquí — y el verifier lo
        tumba aparte. Utilidad y veracidad no compiten."""
        out = _analyze(_store("get_stock_movements", _movements()), Q_MOV,
                       "DATOS:\n- Hubo 8888 movimientos.")
        self.assertEqual(out.verdict, "insufficient")

    def test_only_retrieved_values_can_raise_the_score(self):
        store = _store("get_stock_movements", _movements())
        invented = _analyze(store, Q_MOV, "DATOS:\n- 9999 el 1999-01-01.")
        real = _analyze(store, Q_MOV, "DATOS:\n- 2 unidades el 2026-07-31.")
        self.assertEqual(invented.verdict, "insufficient")
        self.assertEqual(real.verdict, "sufficient")

    def test_empty_evidence_demands_nothing(self):
        """Si la herramienta volvió vacía, 'no hay movimientos' es LA respuesta.
        Sin esta regla la métrica presionaría por rellenar."""
        store = EvidenceStore()
        store.add_from_tool_result(
            tool="get_stock_movements", arguments={"codigo": "2404"},
            result={"ok": True, "empty": True, "meta": {}, "data": {"items": []}})
        out = _analyze(store, Q_MOV, "DATOS:\n- No hay movimientos registrados.")
        self.assertIn(out.verdict, ("not_applicable", "sufficient"))


class CorrectNegativesTests(unittest.TestCase):
    """El peor error posible sería confundir 'no hubo ventas' con una respuesta
    vacua. Las dos citan cero cifras; sólo una es un defecto."""

    def _kpis(self) -> EvidenceStore:
        return _store("get_dashboard_kpis", {
            "ventas_7d": 0, "documentos_7d": 0,
            "serie": [{"fecha": f"2026-08-{d:02d}", "ventas": 0, "documentos": 0}
                      for d in range(1, 15)]})

    def test_stating_absence_in_words_is_sufficient(self):
        """K04/G04: 'no hubo ventas' dice lo mismo que 'ventas: 0' y es mejor
        castellano. La métrica tiene que reconocerlo."""
        out = _analyze(self._kpis(), "KPIs de los ultimos 7 dias.",
                       "DATOS:\n- No hubo ventas ni documentos en los ultimos 7 dias.")
        self.assertEqual(out.verdict, "sufficient")

    def test_citing_the_zero_is_also_sufficient(self):
        out = _analyze(self._kpis(), "KPIs de los ultimos 7 dias.",
                       "DATOS:\n- Ventas: 0. Documentos: 0.")
        self.assertEqual(out.verdict, "sufficient")

    def test_a_daily_series_of_zeros_creates_no_obligation(self):
        """Una fecha dice CUÁNDO, no QUÉ: un bucket vacío no deja de estarlo por
        llevar fecha. Exigir que se recite la serie marcaría como pobre justo la
        respuesta correcta."""
        out = _analyze(self._kpis(), "KPIs de los ultimos 7 dias.",
                       "DATOS:\n- No se registraron ventas.")
        self.assertEqual(out.verdict, "sufficient")

    def test_absence_wording_does_not_excuse_a_payload_with_real_data(self):
        """La negación sólo vale cuando de verdad no hay nada. Si los datos
        existen, decir 'no hay' no los sustituye."""
        out = _analyze(_store("get_stock_movements", _movements()), Q_MOV,
                       "DATOS:\n- No hay nada que destacar.")
        self.assertEqual(out.verdict, "insufficient")


class PerRequirementTests(unittest.TestCase):
    """Se mide requisito a requisito: una mitad buena no tapa la otra."""

    def _both(self) -> EvidenceStore:
        store = _store("get_stock_movements", _movements())
        store.add_from_tool_result(
            tool="get_inventory", arguments={"codigo": "2404"},
            result={"ok": True, "empty": False, "meta": {},
                    "data": {"total_stock": 7}})
        return store

    def test_two_requirements_are_judged_separately(self):
        out = _analyze(self._both(),
                       "Movimientos del 2404 y su stock actual.",
                       "DATOS:\n- El stock actual es 7 unidades.")
        types = {r.requirement_type: r for r in out.requirements}
        self.assertIn("current_inventory", types)
        self.assertTrue(types["current_inventory"].sufficient)

    def test_an_uncovered_requirement_is_not_judged(self):
        """GoalCoverage y el verifier ya gobiernan eso; pedirle datos aquí sería
        pedir lo que no existe."""
        store = EvidenceStore()
        goal = GoalCoverage.from_message("Movimientos del 2404 y su stock actual.")
        goal.refresh(store)
        out = analyze_sufficiency(store=store, goal=goal, reply="", question="x")
        self.assertEqual(out.verdict, "not_applicable")
        self.assertTrue(all(r.sufficient for r in out.requirements))


class SafetyAndShapeTests(unittest.TestCase):
    def test_the_snapshot_carries_no_content(self):
        store = _store("get_supplier", {"items": [
            {"nombre": "ACME", "email": "x@y.z", "token": "sk-abc"}]})
        out = _analyze(store, "Busca proveedores.", "DATOS:\n- ACME.")
        blob = json.dumps(out.safe_snapshot(), ensure_ascii=False)
        for secret in ("sk-abc", "@y.z", "ACME"):
            self.assertNotIn(secret, blob, secret)

    def test_the_snapshot_is_json_serialisable(self):
        out = _analyze(_store("get_stock_movements", _movements()), Q_MOV, "DATOS:\n- 2.")
        json.dumps(out.safe_snapshot())

    def test_it_never_raises_on_odd_shapes(self):
        for data in ({}, {"items": []}, {"items": [[]]}, {"a": {"b": {"c": 1}}},
                     {"items": [{"x": None}, {"x": True}]}):
            with self.subTest(data=data):
                analyze_sufficiency(store=_store("get_inventory", data),
                                    goal=GoalCoverage.from_message("Stock del 2404"),
                                    reply="DATOS:\n- algo", question="Stock del 2404")

    def test_an_empty_reply_is_never_sufficient_when_data_existed(self):
        out = _analyze(_store("get_stock_movements", _movements()), Q_MOV, "")
        self.assertEqual(out.verdict, "insufficient")


class GuardrailsUnchangedTests(unittest.TestCase):
    """8.3 no toca nada de 8.1/8.2. La suficiencia observa; no altera respuestas."""

    def test_the_analysis_is_pure(self):
        store = _store("get_stock_movements", _movements())
        before = json.dumps(store.items[0].data_view, ensure_ascii=False)
        _analyze(store, Q_MOV, "DATOS:\n- 2 unidades.")
        self.assertEqual(json.dumps(store.items[0].data_view, ensure_ascii=False), before)

    def test_the_verifier_still_drops_ungrounded_figures(self):
        from app.assistant.orchestrator.answer_verifier import verify_agent_answer

        store = _store("get_inventory", {"total_stock": 2})
        out = verify_agent_answer(store=store, decision={
            "action": "final_answer", "draft_reply": "",
            "claims": [{"kind": "dato", "text": "El stock es 8888.",
                        "evidence_ids": ["e1"]}],
            "calculations": []})
        self.assertEqual(out.dropped_claims, 1)
        self.assertNotIn("8888", out.reply)

    def test_sufficiency_is_not_an_enforcement_path(self):
        """No existe flag de enforcing para esto, y es deliberado: la métrica
        tiene falsos negativos conocidos y no se ha medido a escala. Añadir el
        camino de activación antes de tener la distribución invitaría a usarlo."""
        import app.assistant.orchestrator.answer_sufficiency as mod

        src = mod.__file__
        with open(src, encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn("os.environ", text)
        self.assertNotIn("ENFORCE", text)


if __name__ == "__main__":
    unittest.main()
