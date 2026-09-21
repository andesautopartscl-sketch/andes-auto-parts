"""FASE 8.9 — tres defectos que sólo aparecieron con LLM real.

El A/B con modelo real dio 70/76 en ambos brazos. Los seis fallos no eran seis
problemas: eran tres defectos de producto, dos golds obsoletos y una varianza.
Los tres de producto son míos, de 8.6 y 8.8, y ninguna prueba determinista los
veía porque los tres viven en la frontera entre lo que el sistema *declara* y lo
que *hace*.

1. **El contrato mentía.** ``get_equivalences`` exigía ``oem`` o ``codigo`` en
   código imperativo mientras el contrato decía ``required=none``. Y como
   ``inspect_arg_fields`` sólo mira ``required``, el aviso de reintento iba
   vacío. Medido: O01 y O04 agotaron los reintentos y cayeron a fallback **sin
   llegar a llamar la tool**. El modelo no podía acertar.

2. **El composer no sabía hablar de las tools nuevas.** Cuando el verifier
   descartó los claims de V02, el composer tomó el relevo y publicó
   ``"Campos en evidencia: count, detalle_parcial, documentos, ..."`` — los
   NOMBRES de los campos. Una tool no está entregada hasta que existen sus DOS
   renderizados.

3. **El prompt pedía al modelo vigilar lo que el verifier ya vigila.** El bloque
   analítico decía "basis='user_request' SOLO si el usuario escribió ese
   número". Eso le hacía escrutar los números del enunciado, y "2404" es un
   número: A01 volvió a pedir el código de producto de una pregunta que lo
   contenía.
"""
from __future__ import annotations

import unittest

from app.assistant.orchestrator.arg_schema import ArgSchemaError, validate_tool_args
from app.assistant.orchestrator.catalog import ALLOWED_TOOLS
from app.assistant.orchestrator.composer import compose_answer
from app.assistant.orchestrator.tool_contracts import (
    TOOL_CONTRACTS,
    format_contracts_for_prompt,
    inspect_arg_fields,
)


class ConditionalRequirementTests(unittest.TestCase):
    """Un requisito que se exige tiene que poder declararse."""

    def test_the_anchor_is_declared_in_the_contract(self):
        self.assertEqual(TOOL_CONTRACTS["get_equivalences"]["any_of"],
                         ("oem", "codigo"))

    def test_the_prompt_tells_the_model_about_it(self):
        """Si el contrato dice required=none y el validador exige un ancla, el
        modelo llama mal, lo rechazan, y no sabe por qué."""
        rendered = [l for l in format_contracts_for_prompt().splitlines()
                    if l.startswith("get_equivalences:")]
        self.assertTrue(rendered)
        self.assertIn("al menos uno de: oem|codigo", rendered[0])

    def test_the_retry_note_names_what_is_missing(self):
        """Medido: `fields: {}` dejaba al modelo reintentando a ciegas, y agotó
        los reintentos en O01 y O04."""
        fields = inspect_arg_fields("get_equivalences", {"marca": "BOSCH"})
        self.assertEqual(fields, {"oem|codigo": "one_required"})

    def test_a_valid_anchor_produces_no_complaint(self):
        self.assertEqual(inspect_arg_fields("get_equivalences",
                                            {"oem": "038-1701225"}), {})

    def test_the_validator_reads_the_declaration(self):
        """No se repite el ancla a mano: si cambia, cambia en un sitio y el
        prompt, el validador y el aviso siguen diciendo lo mismo."""
        with self.assertRaises(ArgSchemaError):
            validate_tool_args("get_equivalences", {"marca": "BOSCH"})
        self.assertEqual(
            validate_tool_args("get_equivalences", {"oem": "038-1701225"})["oem"],
            "038-1701225")

    def test_every_enforced_anchor_is_declared(self):
        """La invariante general: si una tool exige un ancla condicional, tiene
        que estar en `any_of`. Lo contrario es el defecto que costó O01/O04."""
        for tool in ALLOWED_TOOLS:
            spec = TOOL_CONTRACTS[tool]
            any_of = spec.get("any_of") or ()
            for key in any_of:
                with self.subTest(tool=tool, key=key):
                    self.assertIn(key, spec.get("optional") or {},
                                  "un ancla declarada tiene que ser un argumento real")


class ComposerCoversEveryToolTests(unittest.TestCase):
    """El composer es el segundo renderizado; sin él, el fallback publica basura."""

    def _reply(self, tool: str, data: dict) -> str:
        """FASE 8.x — la evidencia se arma con el normalizador, no a mano.

        Esta ayuda fijaba `empty=False` siempre, y el normalizador marca
        `empty=True` en cuanto `count==0` o `items==[]`. Con el estado inventado,
        dos de estas pruebas validaban ramas del composer que en produccion no se
        alcanzaban nunca, porque `compose_answer` cortocircuita antes en `empty`.
        El estado imposible escondio el defecto hasta que O04 se midio contra el
        ERP real.
        """
        from app.assistant.orchestrator.normalizer import normalize_tool_result

        item = normalize_tool_result(200, {"ok": True, "tool": tool,
                                           "classification": "INTERNAL",
                                           "data": data, "meta": {}})
        item["tool"] = tool
        return compose_answer(plan={"steps": [{"step": 1, "tool": tool}]},
                              evidence=[item])["reply"]

    def test_sales_renders_as_prose_not_field_names(self):
        reply = self._reply("get_sales", {
            "unidades": 0, "documentos": 2, "ingresos": 0.0,
            "notas_credito": {"documentos": 2, "unidades": 5},
            "items": [{"fecha": "2026-04-07", "numero": "FA-0001",
                       "codigo": "2417", "cantidad": 1}]})
        self.assertIn("unidad(es)", reply)
        self.assertNotIn("detalle_parcial", reply)
        self.assertNotIn("Campos en evidencia", reply)

    def test_sales_always_states_the_returns(self):
        """Una cifra neta sin mencionar la devolución parece una venta que no
        ocurrió. Medido en la base: 5 unidades vendidas, 5 devueltas, neto 0.

        FASE 8.x — el payload de esta prueba llevaba ``items: []`` con
        ``documentos: 2``, forma que el ERP real no produce: o hay documentos y
        hay filas, o no hay ni lo uno ni lo otro. Con la evidencia armada por el
        normalizador, ``items: []`` marca el resultado vacío y el composer no
        llega a hablar. Se sustituye por el payload REAL medido contra el ERP.
        """
        reply = self._reply("get_sales", {
            "unidades": 0, "documentos": 2, "count": 2, "ingresos": 0.0,
            "notas_credito": {"documentos": 2, "unidades": 5, "monto": 132400.0},
            "items": [{"fecha": "2026-04-07", "numero": "FA-0001",
                       "codigo": "2417", "cantidad": 1}]})
        self.assertIn("devuelta", reply.lower())

    def test_equivalences_render_codes_and_applications_apart(self):
        reply = self._reply("get_equivalences", {
            "count": 1, "items": [{"codigo": "FK1264", "descripcion": "ANILLO",
                                   "oem": ["038-1701225"],
                                   "aplicaciones": ["RICH 6 2.5"]}]})
        self.assertIn("FK1264", reply)
        self.assertIn("OEM 038-1701225", reply)
        self.assertIn("Aplicaciones:", reply)

    def test_a_missing_code_is_stated_as_a_fact(self):
        reply = self._reply("get_equivalences", {"not_found": True, "items": [],
                                                 "count": 0})
        self.assertIn("No existe", reply)

    def test_a_product_without_oem_is_not_the_same_as_not_found(self):
        reply = self._reply("get_equivalences", {"no_oem_declared": True,
                                                 "items": [], "count": 0})
        self.assertIn("no declara", reply)

    def test_every_allowlisted_tool_has_a_text_renderer(self):
        """La invariante que faltaba: answer_view tenía proyección para las tools
        nuevas y el composer no. Una capacidad necesita las dos."""
        from pathlib import Path

        src = Path("app/assistant/orchestrator/composer.py").read_text(encoding="utf-8")
        missing = [t for t in ALLOWED_TOOLS if f'tool == "{t}"' not in src]
        self.assertEqual(missing, [], f"sin formateador de texto: {missing}")


class PromptDoesNotDuplicateEnforcementTests(unittest.TestCase):
    """No se le pide al modelo que vigile lo que el verifier ya vigila."""

    def test_the_block_says_nothing_about_where_a_number_came_from(self):
        from app.assistant.orchestrator.llm.agent_prompts import ANALYSIS_BLOCK

        for word in ("escribio", "user_request", "basis", "default"):
            self.assertNotIn(word, ANALYSIS_BLOCK, word)

    def test_the_server_still_enforces_it(self):
        """Quitarlo del prompt no afloja nada: el guard sigue entero."""
        from app.assistant.orchestrator.analysis import (
            AssumptionError, validate_assumptions)

        question = "stock para dos meses"
        with self.assertRaises(AssumptionError):
            validate_assumptions([{"id": "a1", "kind": "horizon_months",
                                   "value": 7, "basis": "user_request"}],
                                 question=question)
        with self.assertRaises(AssumptionError):
            validate_assumptions([{"id": "a1", "kind": "horizon_months",
                                   "value": 5, "basis": "default"}],
                                 question=question)

    def test_the_block_still_states_the_rule_the_model_must_know(self):
        """Lo que sí necesita saber: que una proyección lleva assumption_ids.
        Eso no lo puede deducir del schema."""
        from app.assistant.orchestrator.llm.agent_prompts import ANALYSIS_BLOCK

        self.assertIn("assumption_ids", ANALYSIS_BLOCK)
        self.assertIn("proyeccion", ANALYSIS_BLOCK)

    def test_the_block_got_smaller(self):
        from app.assistant.orchestrator.llm.agent_prompts import ANALYSIS_BLOCK

        self.assertLess(len(ANALYSIS_BLOCK), 682)


if __name__ == "__main__":
    unittest.main()
