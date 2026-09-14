"""Deterministic planner policy tests: A/B/C/D semantic routing (no LLM)."""
from __future__ import annotations

import unittest

from app.assistant.orchestrator.plan_validator import validate_plan
from app.assistant.orchestrator.llm.prompts import build_system_prompt
from app.assistant.orchestrator.planner import FakePlanner
from app.assistant.orchestrator.scenarios import (
    SCENARIO_AMBIGUOUS,
    SCENARIO_CATALOG_INV_INGRESOS,
    SCENARIO_CHECK_AND_PRODUCT,
    SCENARIO_INVENTORY_ONLY,
    SCENARIO_ONE_TOOL,
    SCENARIO_PRODUCT_ONLY,
    SCENARIO_TWO_TOOLS,
    detect_scenario,
)

class PlannerSemanticPolicyTests(unittest.TestCase):
    """Four categories: unequivocal code, textual search, ambiguity, multi-tool."""

    def test_a_unequivocal_code_direct_tools(self):
        cases = [
            ("Stock del 2404", SCENARIO_INVENTORY_ONLY, ["get_inventory"]),
            ("Que es el producto 2404?", SCENARIO_PRODUCT_ONLY, ["get_product"]),
            ("Disponibilidad 2404 x2 y ficha", SCENARIO_CHECK_AND_PRODUCT, ["check_stock", "get_product"]),
        ]
        planner = FakePlanner()
        for message, scenario, tools in cases:
            self.assertEqual(detect_scenario(message), scenario, message)
            plan = planner.plan(message)
            self.assertEqual(plan.get("scenario"), scenario, message)
            cleaned = validate_plan(plan)
            got = [s["tool"] for s in cleaned["steps"]]
            self.assertEqual(got, tools, message)
            self.assertNotIn("search_catalog", got, message)

    def test_b_textual_search_uses_search_catalog(self):
        cases = [
            ("Busca filtro 2404", SCENARIO_ONE_TOOL, "search_catalog"),
            ("Busca filtro", SCENARIO_ONE_TOOL, "search_catalog"),
            ("busca filtro aceite", SCENARIO_ONE_TOOL, "search_catalog"),
        ]
        planner = FakePlanner()
        for message, scenario, first_tool in cases:
            self.assertEqual(detect_scenario(message), scenario, message)
            plan = planner.plan(message)
            cleaned = validate_plan(plan)
            self.assertFalse(cleaned.get("needs_clarification"), message)
            self.assertEqual(cleaned["steps"][0]["tool"], first_tool, message)

    def test_c_ambiguity_clarify_zero_tools(self):
        cases = ["El filtro", "Stock", "Busca eso"]
        planner = FakePlanner()
        for message in cases:
            self.assertEqual(detect_scenario(message), SCENARIO_AMBIGUOUS, message)
            plan = planner.plan(message)
            self.assertTrue(plan.get("needs_clarification"), message)
            self.assertFalse(plan.get("steps"), message)
            cleaned = validate_plan(plan)
            self.assertTrue(cleaned.get("needs_clarification"), message)
            self.assertEqual(cleaned.get("steps") or [], [])

    def test_d_multi_tool_search_then_downstream(self):
        cases = [
            ("Busca 2404 y dime el stock", SCENARIO_TWO_TOOLS, ["search_catalog", "get_inventory"]),
            (
                "Catalogo, stock e ingresos del 2404",
                SCENARIO_CATALOG_INV_INGRESOS,
                ["search_catalog", "get_inventory", "get_ingresos"],
            ),
        ]
        planner = FakePlanner()
        for message, scenario, tools in cases:
            self.assertEqual(detect_scenario(message), scenario, message)
            plan = planner.plan(message)
            cleaned = validate_plan(plan)
            got = [s["tool"] for s in cleaned["steps"]]
            self.assertEqual(got, tools, message)

    def test_p01_busca_filtro_not_clarify(self):
        """P01 gold: search_catalog; must not clarify when term is present."""
        message = "Busca filtro"
        self.assertEqual(detect_scenario(message), SCENARIO_ONE_TOOL)
        plan = FakePlanner().plan(message)
        cleaned = validate_plan(plan)
        self.assertFalse(cleaned.get("needs_clarification"))
        self.assertEqual(cleaned["steps"][0]["tool"], "search_catalog")

    def test_system_prompt_encodes_abcd_policy(self):
        raw = build_system_prompt()
        self.assertIn("Identificador inequívoco", raw)
        self.assertIn("Búsqueda / descubrimiento", raw)
        self.assertIn("Ambigüedad real", raw)
        self.assertIn("mínima cadena SUFICIENTE", raw)
        self.assertIn("Busca filtro 2404", raw)
        self.assertIn("El filtro", raw)


if __name__ == "__main__":
    unittest.main()
