"""FASE 8.6 — get_sales, análisis condicional y el A/B que no se pisa.

Cuatro problemas medidos, no supuestos:

1. **No había demanda.** A03 respondió correctamente "no hay datos suficientes
   para estimar la demanda": los movimientos del 2404 son 2 ingresos y 1 ajuste.
   Sin una fuente de ventas, ninguna proyección era legítima y la escalera de 8.5
   no podía validarse.

2. **Las ventas viven con las compras.** ventas_documentos guarda ambas,
   separadas por `tipo`. Medido en la base real: boleta(1), factura(1),
   orden_venta(4), cotizacion(4), orden_compra(1). Contar cotizaciones infla las
   ventas con intentos; contar orden_compra invierte el signo.

3. **Las devoluciones anulan la cifra.** También medido: bruto 5 unidades /
   132 400, notas de crédito 5 unidades / 132 400 → neto CERO. Una tool que las
   ignorase diría "vendiste 5 unidades" cuando todo fue devuelto.

4. **Cargar análisis en toda pregunta rompía preguntas normales.** +17% de tokens
   por turno, T09 contra el techo, y "2404" leído como "24/04".
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# El Gateway es un paquete aparte (andes_agent/). Se monta al importar, igual que
# en test_orchestrator_gateway_contracts: hacerlo dentro de un metodo depende del
# orden de ejecucion y rompe segun que test corra primero.
_ROOT = Path(__file__).resolve().parents[1]
# Al FINAL: andes_agent/ tiene su propio paquete `tests` y anteponerlo
# eclipsaria el tests/ del proyecto.
if str(_ROOT / "andes_agent") not in sys.path:
    sys.path.append(str(_ROOT / "andes_agent"))

from app.assistant.orchestrator.analysis import analytical_intent
from app.assistant.orchestrator.arg_schema import ArgSchemaError, validate_tool_args
from app.assistant.orchestrator.catalog import ALLOWED_TOOLS
from app.assistant.orchestrator.goal_coverage import REQUIREMENT_TYPES, GoalCoverage
from app.assistant.orchestrator.tool_contracts import TOOL_CONTRACTS


class ToolSurfaceAlignmentTests(unittest.TestCase):
    """La superficie de tools está defendida por CINCO allowlists independientes.

    Es buen diseño de seguridad —una tool tiene que estar en las cinco para
    ejecutarse— y también significa que añadir una toca las cinco y nada
    comprobaba que coincidieran. Este test es ese control.
    """

    def _gateway_registry(self):
        from andes_agent.tools.registry import ALLOWED_TOOLS as GW

        return GW

    def test_the_five_allowlists_agree(self):
        from app.assistant.orchestrator import arg_schema

        gw = self._gateway_registry()
        self.assertEqual(set(ALLOWED_TOOLS), set(TOOL_CONTRACTS))
        self.assertEqual(set(ALLOWED_TOOLS), set(gw))
        for tool in ALLOWED_TOOLS:
            with self.subTest(tool=tool):
                # arg_schema rechaza lo que no conoce: si falta, la tool existe
                # en el catálogo pero ningún plan puede usarla.
                try:
                    validate_tool_args(tool, {})
                except ArgSchemaError as exc:
                    self.assertNotIn("Unknown tool", str(exc), tool)

    def test_no_write_tool_is_registered_anywhere(self):
        gw = self._gateway_registry()
        self.assertEqual([n for n, s in gw.items() if s.write], [])
        for tool in ALLOWED_TOOLS:
            for prefix in ("create_", "update_", "delete_", "insert_", "remove_", "write_"):
                self.assertFalse(tool.startswith(prefix), tool)

    def test_get_sales_is_present_everywhere(self):
        self.assertIn("get_sales", ALLOWED_TOOLS)
        self.assertIn("get_sales", TOOL_CONTRACTS)
        self.assertIn("get_sales", self._gateway_registry())
        self.assertIn("sales", REQUIREMENT_TYPES)


class SalesArgumentContractTests(unittest.TestCase):
    def test_a_purchase_order_is_not_a_sale(self):
        """No es una restricción de permisos: es de corrección. Contar una compra
        como venta invierte el signo del resultado."""
        from andes_agent.schemas import SchemaError, validate_tool_arguments

        with self.assertRaises(SchemaError):
            validate_tool_arguments("get_sales", {"tipos": ["orden_compra"]})

    def test_an_unknown_document_type_is_rejected(self):
        from andes_agent.schemas import SchemaError, validate_tool_arguments

        with self.assertRaises(SchemaError):
            validate_tool_arguments("get_sales", {"tipos": ["inventado"]})

    def test_the_orchestrator_accepts_the_declared_arguments(self):
        out = validate_tool_args("get_sales", {"codigo": "2404", "group_by": "mes",
                                               "limit": 5})
        self.assertEqual(out["codigo"], "2404")
        self.assertEqual(out["group_by"], "mes")

    def test_an_undeclared_argument_is_rejected(self):
        with self.assertRaises(ArgSchemaError):
            validate_tool_args("get_sales", {"sql": "SELECT 1"})

    def test_group_by_is_a_closed_set(self):
        from andes_agent.schemas import SchemaError, validate_tool_arguments

        with self.assertRaises(SchemaError):
            validate_tool_arguments("get_sales", {"group_by": "semana"})


class SalesServiceTests(unittest.TestCase):
    """Sobre la lógica pura: la base real se prueba en el probe del closure."""

    def _validate(self, payload):
        from app.internal_agent.sales import validate_sales_args

        return validate_sales_args(payload)

    def test_the_default_scope_is_realised_sales(self):
        """"Ventas" sin calificar significa facturado, no cotizado."""
        from app.internal_agent.sales import SALE_TIPOS

        self.assertEqual(self._validate({})["tipos"], list(SALE_TIPOS))
        self.assertNotIn("cotizacion", SALE_TIPOS)
        self.assertNotIn("orden_compra", SALE_TIPOS)

    def test_a_purchase_type_is_refused_by_the_erp_too(self):
        from app.internal_agent.m2m import InternalAuthError

        with self.assertRaises(InternalAuthError):
            self._validate({"tipos": ["orden_compra"]})

    def test_a_reversed_window_is_rejected(self):
        from app.internal_agent.m2m import InternalAuthError

        with self.assertRaises(InternalAuthError):
            self._validate({"fecha_desde": "2026-03-01", "fecha_hasta": "2026-01-01"})

    def test_an_unbounded_window_is_rejected(self):
        from app.internal_agent.m2m import InternalAuthError

        with self.assertRaises(InternalAuthError):
            self._validate({"fecha_desde": "2000-01-01", "fecha_hasta": "2026-01-01"})

    def test_sql_in_a_text_argument_is_rejected(self):
        from app.internal_agent.m2m import InternalAuthError

        with self.assertRaises(InternalAuthError):
            self._validate({"cliente": "x'; DROP TABLE ventas_documentos; --"})

    def test_contact_fields_are_blocked_by_name(self):
        """Nunca salen, ni con permisos financieros: el agente no contacta a nadie."""
        from app.internal_agent.sales import BLOCKED_FIELDS

        for field in ("cliente_email", "cliente_telefono", "cliente_direccion"):
            self.assertIn(field, BLOCKED_FIELDS)

    def test_the_limit_is_bounded(self):
        self.assertLessEqual(self._validate({"limit": 9999})["limit"], 20)


class SalesProjectionTests(unittest.TestCase):
    def test_the_view_projects_aggregates_and_a_sample(self):
        from app.assistant.orchestrator.answer_view import build_answer_view
        from app.assistant.orchestrator.evidence_store import EvidenceStore

        store = EvidenceStore()
        store.add_from_tool_result(
            tool="get_sales", arguments={"codigo": "2404"},
            result={"ok": True, "empty": False, "meta": {}, "data": {
                "unidades": 5, "documentos": 2, "ingresos": 500.0,
                "neto_notas_credito": True, "detalle_parcial": True,
                "items": [{"numero": "FA-0001", "fecha": "2026-04-07",
                           "codigo": "2404", "cantidad": 5,
                           "cliente": "ACME", "estado": "aprobada"}]}})
        block = build_answer_view(store).as_dict()["blocks"][0]
        names = {f["name"] for f in block["summary"]}
        self.assertTrue({"unidades", "documentos"} <= names)
        self.assertEqual(block["cards"][0]["title"], "FA-0001")

    def test_no_contact_field_can_be_projected(self):
        from app.assistant.orchestrator.answer_view import TOOL_VIEWS

        spec = TOOL_VIEWS["get_sales"]
        allowed = set(spec["fields"]) | set(spec.get("summary") or ())
        for blocked in ("cliente_email", "cliente_telefono", "cliente_direccion",
                        "rut", "cliente_rut"):
            self.assertNotIn(blocked, allowed, blocked)


class SalesRequirementTests(unittest.TestCase):
    """'ventas' suelto NO es un requisito; con calificador sí."""

    def _types(self, message: str) -> list[str]:
        snap = GoalCoverage.from_message(message).safe_snapshot()
        return [r["type"] for r in snap["requirements"]]

    def test_a_bare_business_word_creates_no_requirement(self):
        self.assertEqual(self._types("Ventas del dia"), [])

    def test_a_product_code_makes_it_specific(self):
        self.assertIn("sales", self._types("Ventas del 2404"))

    def test_a_period_makes_it_specific(self):
        self.assertIn("sales", self._types("Ventas de enero a marzo"))

    def test_a_kpi_question_absorbs_sales(self):
        """El dashboard YA agrega ventas. Pedir get_sales además obligaría a dos
        tools para una pregunta que una sola responde, y bloquearía el final."""
        self.assertEqual(self._types("Ranking de ventas del mes."), ["dashboard_kpis"])
        self.assertEqual(self._types("KPIs de los ultimos 7 dias"), ["dashboard_kpis"])

    def test_absorption_reads_the_detected_requirement_not_a_word_list(self):
        """Una lista paralela de palabras del dashboard se desincroniza en cuanto
        alguien añada una señal nueva, y el síntoma sería una cobertura
        imposible de satisfacer."""
        import app.assistant.orchestrator.goal_coverage as gc

        self.assertFalse(hasattr(gc, "_SALES_ABSORBED_BY"))


class ConditionalAnalysisTests(unittest.TestCase):
    """La regresión medida: el bloque analítico rompió preguntas normales."""

    def setUp(self):
        self._env = patch.dict(
            os.environ, {"ANDES_ASSISTANT_ANALYSIS_ENABLED": "1"}, clear=False)
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_the_two_regressions_had_two_different_causes(self):
        """T02 y A01 fallaron igual y por motivos distintos. Importa separarlos.

        T02 ("Revisa los movimientos del 2404 y despues dime cuanto stock hay")
        NO es analitica: su arreglo es la inyeccion condicional, que ya no le
        carga el bloque.

        A01 ("...cuanto stock deberia tener para dos meses") SI lo es, asi que
        seguira viendo el bloque. Su arreglo es otro: purgar el vocabulario
        temporal que hizo leer "2404" como "24/04". Un solo arreglo no cubre las
        dos, y tratarlas como el mismo fallo habria dejado A01 rota."""
        self.assertFalse(analytical_intent(
            "Revisa los movimientos del 2404 y despues dime cuanto stock hay."))
        self.assertTrue(analytical_intent(
            "Con los movimientos del 2404, cuanto stock deberia tener para dos meses?"))

    def test_the_block_carries_no_temporal_vocabulary(self):
        """La causa de A01: el bloque repetia "meses"/"dias" en cada prompt y
        cebo la lectura temporal. El schema enumera los tipos de supuesto; el
        prompt no necesita nombrarlos."""
        from app.assistant.orchestrator.llm.agent_prompts import ANALYSIS_BLOCK

        for word in ("horizon_months", "window_days", "lead_time_days", "meses"):
            self.assertNotIn(word, ANALYSIS_BLOCK, word)

    def test_plain_questions_never_trigger_analysis(self):
        for message in ("Stock del 2404", "Ventas del 2404",
                        "Proveedor BOSCH y sus ordenes de compra",
                        "Muestrame las ventas de enero a marzo"):
            with self.subTest(message=message):
                self.assertFalse(analytical_intent(message))

    def test_projection_questions_do(self):
        for message in ("Cual es la cobertura de stock del 2404?",
                        "Cuanto me faltaria comprar para cubrir tres meses?",
                        "Que productos estan en riesgo de quiebre?"):
            with self.subTest(message=message):
                self.assertTrue(analytical_intent(message))

    def test_a_normal_turn_gets_the_untouched_schema(self):
        from app.assistant.orchestrator.llm.plan_schema import (
            AGENT_DECISION_JSON_SCHEMA, agent_decision_response_format)

        fmt = agent_decision_response_format(analytical=False)
        self.assertIs(fmt["json_schema"]["schema"], AGENT_DECISION_JSON_SCHEMA)

    def test_a_normal_turn_gets_the_untouched_prompt(self):
        from app.assistant.orchestrator.llm.agent_prompts import build_agent_system_prompt

        self.assertNotIn("proyeccion", build_agent_system_prompt(analytical=False))

    def test_the_turn_decides_once_and_deterministically(self):
        """Ni el modelo ni la evidencia pueden convertir un turno normal en
        analítico a mitad de camino."""
        message = "Stock del 2404"
        self.assertEqual({analytical_intent(message) for _ in range(5)}, {False})


class AbHarnessTests(unittest.TestCase):
    """Un A/B que no conserva sus dos brazos no es un A/B."""

    def _arm(self) -> str:
        from evals.fase81g_closure import arm_id

        return arm_id()

    def test_the_arms_get_different_ids(self):
        with patch.dict(os.environ, {"ANDES_ASSISTANT_ANALYSIS_ENABLED": "0"}, clear=False):
            off = self._arm()
        with patch.dict(os.environ, {"ANDES_ASSISTANT_ANALYSIS_ENABLED": "1"}, clear=False):
            on = self._arm()
        self.assertNotEqual(off, on)

    def test_the_arms_write_to_different_paths(self):
        from evals.fase81g_closure import arm_path

        with patch.dict(os.environ, {"ANDES_ASSISTANT_ANALYSIS_ENABLED": "0"}, clear=False):
            off = arm_path(Path("d"), "fase81_final_report", ".json")
        with patch.dict(os.environ, {"ANDES_ASSISTANT_ANALYSIS_ENABLED": "1"}, clear=False):
            on = arm_path(Path("d"), "fase81_final_report", ".json")
        self.assertNotEqual(off, on)

    def test_the_same_configuration_is_a_repeat_not_a_new_arm(self):
        with patch.dict(os.environ, {"ANDES_ASSISTANT_ANALYSIS_ENABLED": "1"}, clear=False):
            first = self._arm()
            second = self._arm()
        self.assertEqual(first, second)

    def test_provenance_also_defines_an_arm(self):
        with patch.dict(os.environ, {"ANDES_ASSISTANT_PROVENANCE_ENFORCE": "1"},
                        clear=False):
            with_prov = self._arm()
        with patch.dict(os.environ, {"ANDES_ASSISTANT_PROVENANCE_ENFORCE": "0"},
                        clear=False):
            without = self._arm()
        self.assertNotEqual(with_prov, without)

    def test_every_run_artifact_carries_its_arm(self):
        from evals.fase81g_closure import arm_id, arm_path

        for stem in ("fase81_final_report", "fase81g_runs_llm",
                     "fase81g_scores_llm", "fase81g_stability_runs"):
            with self.subTest(stem=stem):
                self.assertIn(arm_id(), arm_path(Path("d"), stem, ".json").name)


if __name__ == "__main__":
    unittest.main()
