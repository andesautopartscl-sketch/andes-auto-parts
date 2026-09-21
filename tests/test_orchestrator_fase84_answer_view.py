"""FASE 8.4 — la respuesta estructurada no puede ser una segunda verdad.

El riesgo de añadir tarjetas es evidente: si se pintan desde el payload crudo de
la herramienta —que es lo que ya hacen los comandos slash en el navegador— se
salta el verifier entero. Una tarjeta podría enseñar cifras que el verifier
tumbó, filas que el truncado descartó, o campos que el texto nunca expone.

Por eso la vista se proyecta del mismo ``EvidenceItem.data_view`` del que sale el
texto, con lista blanca de campos por herramienta derivada de lo que el composer
ya imprime. Estos tests fijan esa frontera.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from app.assistant.orchestrator.answer_view import (
    TOOL_VIEWS,
    build_answer_view,
)
from app.assistant.orchestrator.audit import OrchestratorAudit
from app.assistant.orchestrator.agent_loop import QueueDecisionClient
from app.assistant.orchestrator.catalog import ALLOWED_TOOLS
from app.assistant.orchestrator.evidence_store import EvidenceStore
from app.assistant.orchestrator.planner import FakePlanner
from app.assistant.orchestrator.service import run_orchestrator_chat
from app.assistant.orchestrator.turn_store import TurnStore
from tests.test_orchestrator_fase81_agent import _call, _final, _invoke_router


def _store(tool: str, data: Any, **kw: Any) -> EvidenceStore:
    store = EvidenceStore()
    store.add_from_tool_result(
        tool=tool, arguments=kw.pop("arguments", {"codigo": "2404"}),
        result={"ok": True, "empty": kw.pop("empty", False), "meta": {}, "data": data})
    return store


def _values(view_dict: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for block in view_dict["blocks"]:
        for f in block["summary"]:
            out.add(str(f["value"]))
        for card in block["cards"]:
            out.add(str(card["title"]))
            for f in card["fields"]:
                out.add(str(f["value"]))
    return out


class NothingBeyondTheEvidenceTests(unittest.TestCase):
    """Una tarjeta no puede mostrar algo que el texto tampoco podría afirmar."""

    def test_every_projected_value_exists_in_the_evidence(self):
        store = _store("get_inventory", {"codigo": "2404", "total_stock": 2,
                                         "items": [{"bodega": "Bodega 1", "stock": 2,
                                                    "marca": "BOSCH"}]})
        blob = json.dumps(store.items[0].data_view, ensure_ascii=False)
        for value in _values(build_answer_view(store).as_dict()):
            self.assertIn(value, blob, value)

    def test_a_field_outside_the_allowlist_is_never_projected(self):
        """Si el Gateway añade un campo mañana, no puede aparecer solo."""
        store = _store("get_supplier", {"items": [
            {"nombre": "ACME", "empresa": "ACME SA", "ciudad": "Santiago",
             "campo_nuevo_del_gateway": "FILTRAR-ESTO"}]})
        self.assertNotIn("FILTRAR-ESTO", json.dumps(build_answer_view(store).as_dict()))

    def test_an_unknown_tool_produces_no_block(self):
        """No se inventa una proyección para lo que no está declarado."""
        store = _store("get_customer", {"items": [{"nombre": "X"}]})
        store.items[0].tool = "herramienta_inexistente"
        self.assertTrue(build_answer_view(store).is_empty())

    def test_nested_structures_are_not_projected_as_values(self):
        store = _store("get_supplier", {"items": [
            {"nombre": "ACME", "empresa": {"oculto": "NO-DEBE-SALIR"},
             "ciudad": ["NO-DEBE-SALIR"]}]})
        self.assertNotIn("NO-DEBE-SALIR", json.dumps(build_answer_view(store).as_dict()))


class SecretsAndPiiTests(unittest.TestCase):
    def test_secrets_never_reach_the_view(self):
        store = _store("get_customer", {"items": [
            {"nombre": "ACME", "rut": "1-9", "token": "sk-abc",
             "password": "p", "api_key": "k"}]})
        blob = json.dumps(build_answer_view(store).as_dict(), ensure_ascii=False)
        for secret in ("sk-abc", "password", "api_key"):
            self.assertNotIn(secret, blob, secret)

    def test_contact_data_never_reaches_the_view(self):
        store = _store("get_customer", {"items": [
            {"nombre": "ACME", "email": "x@y.z", "telefono": "555123",
             "direccion": "Calle 1"}]})
        blob = json.dumps(build_answer_view(store).as_dict(), ensure_ascii=False)
        for pii in ("x@y.z", "555123", "Calle 1"):
            self.assertNotIn(pii, blob, pii)

    def test_a_personal_identifier_is_never_a_navigation_ref(self):
        """El RUT se imprime porque el texto lo imprime, pero no viaja en una URL."""
        store = _store("get_customer", {"items": [{"nombre": "ACME", "rut": "1-9"}]})
        for block in build_answer_view(store).as_dict()["blocks"]:
            for card in block["cards"]:
                self.assertNotIn("ref", card)

    def test_no_view_spec_declares_a_rut_ref(self):
        for tool, spec in TOOL_VIEWS.items():
            with self.subTest(tool=tool):
                self.assertNotEqual(spec.get("ref"), "rut")


class ProvenanceTests(unittest.TestCase):
    def test_every_card_carries_its_evidence_id(self):
        store = _store("get_supplier", {"items": [{"nombre": f"P{i}"} for i in range(4)]})
        view = build_answer_view(store).as_dict()
        for block in view["blocks"]:
            self.assertTrue(block["evidence_id"])
            for card in block["cards"]:
                self.assertEqual(card["evidence_id"], block["evidence_id"])

    def test_two_tools_produce_two_traceable_blocks(self):
        store = _store("get_product", {"codigo": "2404", "descripcion": "FILTRO"})
        store.add_from_tool_result(
            tool="get_inventory", arguments={"codigo": "2404"},
            result={"ok": True, "empty": False, "meta": {},
                    "data": {"codigo": "2404", "total_stock": 2, "items": []}})
        view = build_answer_view(store).as_dict()
        self.assertEqual(len(view["blocks"]), 2)
        self.assertEqual(view["evidence_ids"], ["e1", "e2"])

    def test_scope_restricts_what_is_shown(self):
        store = _store("get_product", {"codigo": "2404", "descripcion": "FILTRO"})
        store.add_from_tool_result(
            tool="get_supplier", arguments={"q": "a"},
            result={"ok": True, "empty": False, "meta": {},
                    "data": {"items": [{"nombre": "ACME"}]}})
        view = build_answer_view(store, scope={"e1"}).as_dict()
        self.assertEqual(view["evidence_ids"], ["e1"])


class TruncationHonestyTests(unittest.TestCase):
    """Una lista de tarjetas no puede insinuar completitud que no tiene."""

    def test_more_rows_than_cards_is_reported(self):
        store = _store("get_supplier", {"items": [{"nombre": f"P{i}"} for i in range(40)]})
        block = build_answer_view(store).as_dict()["blocks"][0]
        self.assertTrue(block["truncated"])
        self.assertLess(block["shown_rows"], block["total_rows"])

    def test_degraded_evidence_counts_the_omitted_rows(self):
        store = _store("get_supplier", {"items": [
            {"nombre": f"PROVEEDOR NUMERO {i}", "empresa": "E" * 120,
             "ciudad": "C" * 60} for i in range(80)]})
        block = build_answer_view(store).as_dict()["blocks"][0]
        self.assertTrue(block["truncated"])
        self.assertGreater(block["omitted_rows"], 0)

    def test_a_complete_small_list_is_not_marked_truncated(self):
        store = _store("get_supplier", {"items": [{"nombre": "ACME"}]})
        block = build_answer_view(store).as_dict()["blocks"][0]
        self.assertFalse(block["truncated"])
        self.assertEqual(block["shown_rows"], block["total_rows"])

    def test_an_empty_result_says_so(self):
        store = _store("get_supplier", {"items": []}, empty=True)
        block = build_answer_view(store).as_dict()["blocks"][0]
        self.assertTrue(block["empty"])
        self.assertEqual(block["cards"], [])


class ContractTests(unittest.TestCase):
    def test_the_view_is_json_serialisable(self):
        store = _store("get_inventory", {"codigo": "2404", "total_stock": 2,
                                         "items": [{"bodega": "B1", "stock": 2}]})
        json.dumps(build_answer_view(store).as_dict())

    def test_every_declared_tool_is_a_real_read_tool(self):
        """La vista no puede abrir una superficie nueva: sólo proyecta lo que el
        catálogo READ ya permite."""
        for tool in TOOL_VIEWS:
            self.assertIn(tool, ALLOWED_TOOLS, tool)

    def test_no_write_tool_has_a_projection(self):
        for tool in TOOL_VIEWS:
            for prefix in ("create_", "update_", "delete_", "insert_", "remove_", "write_"):
                self.assertFalse(tool.startswith(prefix), tool)

    def test_odd_payloads_never_raise(self):
        for data in ({}, {"items": None}, {"items": [None, 3, "x"]},
                     {"items": [{}]}, [], "texto", None):
            with self.subTest(data=data):
                build_answer_view(_store("get_supplier", data))


class ServiceIntegrationTests(unittest.TestCase):
    """La vista tiene que existir en la ruta por defecto, que es AGENT=0."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.audit = OrchestratorAudit(path=Path(self._tmpdir.name) / "audit.jsonl")
        self.store = TurnStore()
        self._env = patch.dict(os.environ, {
            "ANDES_ASSISTANT_AGENT_ENABLED": "0",
            "ANDES_ASSISTANT_NL_ENABLED": "0",
            "ANDES_ORCH_PLANNER": "fake",
            "ANDES_ASSISTANT_MEMORY_ENABLED": "0",
            "ANDES_ASSISTANT_HISTORY_ENABLED": "0",
        }, clear=False)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmpdir.cleanup()

    def _chat(self, message: str, **kwargs) -> dict[str, Any]:
        return run_orchestrator_chat(
            message=message, actor_user="albertadmin", conversation_id="c-84",
            invoke_fn=_invoke_router, planner=FakePlanner(),
            audit=self.audit, turn_store=self.store, **kwargs)

    def test_agent_zero_still_gets_a_view(self):
        out = self._chat("Stock del 2404")
        self.assertFalse(out.get("agent_enabled"))
        self.assertIsInstance(out.get("view"), dict)
        self.assertTrue(out["view"]["blocks"])

    def test_the_text_reply_is_unchanged_by_the_view(self):
        """8.4 es aditivo: el texto que ya se publicaba sigue siendo el mismo."""
        out = self._chat("Stock del 2404")
        self.assertTrue(out.get("reply"))
        self.assertTrue(out.get("grounded"))

    @patch("app.assistant.orchestrator.service.agent_loop_allowed", return_value=True)
    def test_the_agent_path_projects_its_own_store(self, _allowed):
        client = QueueDecisionClient([
            _call("get_inventory", {"codigo": "2404"}),
            _final("El stock es 2.", eids=["e1"]),
        ])
        out = self._chat("Stock del 2404", agent_decision_client=client)
        self.assertTrue(out.get("agent_enabled"))
        self.assertIsInstance(out.get("view"), dict)

    def test_the_live_store_never_leaks_into_the_response(self):
        out = self._chat("Stock del 2404")
        self.assertNotIn("evidence_store", out)
        json.dumps(out, default=str)

    def test_a_view_failure_never_breaks_the_turn(self):
        with patch("app.assistant.orchestrator.answer_view.build_answer_view",
                   side_effect=RuntimeError("boom")):
            out = self._chat("Stock del 2404")
        self.assertTrue(out.get("ok"))
        self.assertTrue(out.get("reply"))
        self.assertIsNone(out.get("view"))


class FrontendSafetyTests(unittest.TestCase):
    """El renderizador construye DOM; no puede escribir markup."""

    def _js(self) -> str:
        return Path("app/static/js/assistant.js").read_text(encoding="utf-8")

    def test_the_renderer_never_uses_inner_html(self):
        js = self._js()
        offenders = [m for m in re.findall(r"\.innerHTML\s*=\s*[^;]+", js)
                     if "''" not in m and '""' not in m]
        self.assertEqual(offenders, [], f"innerHTML con contenido: {offenders}")

    def test_the_renderer_uses_text_content_for_values(self):
        js = self._js()
        self.assertIn("value.textContent", js)
        self.assertIn("title.textContent", js)

    def test_the_card_click_does_not_invoke_anything(self):
        """Seleccionar un registro redacta una pregunta; no ejecuta acciones."""
        js = self._js()
        start = js.index("function pickRecord")
        body = js[start:start + 900]
        for forbidden in ("fetch(", "invoke", "service.", "XMLHttpRequest"):
            self.assertNotIn(forbidden, body, forbidden)


if __name__ == "__main__":
    unittest.main()
