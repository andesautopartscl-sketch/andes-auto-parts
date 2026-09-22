"""FASE 9.2 — ordenes de cliente: el hueco comercial del asistente.

POR QUE ESTA TOOL, MEDIDO EN LA BASE REAL

    oc_clientes        71 ordenes · 93 lineas · 50 pagos     SIN TOOL
       precio_unitario 100% · estado 100% · total 100%
    ventas_documentos  11 documentos                         get_sales

``get_sales`` leia la tabla con 11 filas. Donde el negocio transacta de verdad
—con precios reales por linea— no miraba nadie. Medido por HTTP real tras
conectar la tool: 69 ordenes, 91 lineas, 12.640.536 CLP.

DOS REGLAS QUE SON DE CORRECCION, NO DE PERMISOS

1. `anulada` NO cuenta por defecto. Estados medidos: pagada 65, recibida 4,
   anulada 2. Sumar una anulada a los ingresos invierte el signo de la realidad,
   igual que contar una ``orden_compra`` como venta en 8.6. Hay que nombrarla
   para verla, y el payload declara que la excluyo.

2. `recibida` no es `pagada`. Cuatro ordenes estan recibidas y sin cobrar; un
   agregado que las mezcle responde otra pregunta. El desglose viaja siempre.

POR QUE ALLOWLIST DE SALIDA Y NO BLOCKLIST

La superficie de datos personales de una orden es mas densa que la de una venta
—direccion de despacho, vendedor, usuario, referencias de pago, el cliente
entero colgando de ``ventas_clientes``— y ademas incluye ``observaciones``,
texto libre donde cabe cualquier cosa. Una lista de lo prohibido no protege del
campo que alguien anada manana; una de lo permitido si.
"""
from __future__ import annotations

import importlib
import os
import unittest
from datetime import date

from app.internal_agent.orders import (
    DEFAULT_ESTADOS,
    ESTADO_ANULADO,
    FINANCE_LINE_FIELDS,
    NEVER_PROJECTED,
    PUBLIC_LINE_FIELDS,
    SELECTABLE_ESTADOS,
    validate_orders_args,
)
from app.internal_agent.m2m import InternalAuthError


class CancelledOrdersAreNotSalesTests(unittest.TestCase):
    """La decision de dominio que define esta tool."""

    def test_the_default_scope_excludes_cancelled_orders(self):
        self.assertNotIn(ESTADO_ANULADO, DEFAULT_ESTADOS)
        self.assertEqual(validate_orders_args({})["estados"], list(DEFAULT_ESTADOS))

    def test_a_cancelled_order_has_to_be_named_to_be_seen(self):
        args = validate_orders_args({"estados": ["pagada", "anulada"]})
        self.assertIn(ESTADO_ANULADO, args["estados"])

    def test_paid_and_delivered_stay_separate_states(self):
        """No se colapsan: cuatro ordenes recibidas y sin cobrar responden otra
        pregunta que las 65 cobradas."""
        self.assertEqual(set(DEFAULT_ESTADOS), {"pagada", "recibida"})

    def test_an_invented_state_is_refused(self):
        with self.assertRaises(InternalAuthError):
            validate_orders_args({"estados": ["entregada_quizas"]})

    def test_the_selectable_set_is_closed(self):
        self.assertEqual(SELECTABLE_ESTADOS, {"pagada", "recibida", "anulada"})


class TheOutputIsAnAllowlistTests(unittest.TestCase):

    def test_personal_and_free_text_columns_are_never_projected(self):
        for col in ("direccion_despacho", "vendedor", "usuario", "observaciones",
                    "referencia_pago", "cliente_id", "numero_factura"):
            with self.subTest(col=col):
                self.assertIn(col, NEVER_PROJECTED)
                self.assertNotIn(col, PUBLIC_LINE_FIELDS)
                self.assertNotIn(col, FINANCE_LINE_FIELDS)

    def test_money_lives_behind_the_finance_gate(self):
        self.assertEqual(FINANCE_LINE_FIELDS, ("precio_unitario", "subtotal"))
        for field in FINANCE_LINE_FIELDS:
            self.assertNotIn(field, PUBLIC_LINE_FIELDS)

    def test_the_gateway_copies_only_what_is_enumerated(self):
        """El Gateway tiene su propia allowlist: dos puertas, no una."""
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        if str(root / "andes_agent") not in sys.path:
            sys.path.append(str(root / "andes_agent"))
        from andes_agent.tools import get_orders as gw

        for field in ("vendedor", "direccion_despacho", "observaciones",
                      "usuario", "referencia_pago", "cliente"):
            self.assertNotIn(field, gw.PUBLIC_ITEM_FIELDS)
            self.assertNotIn(field, gw.PUBLIC_SCALARS)


class TheArgumentsAreValidatedTests(unittest.TestCase):

    def test_a_window_beyond_the_cap_is_refused(self):
        with self.assertRaises(InternalAuthError):
            validate_orders_args({"fecha_desde": "2020-01-01",
                                  "fecha_hasta": "2026-01-01"})

    def test_an_inverted_window_is_refused(self):
        with self.assertRaises(InternalAuthError):
            validate_orders_args({"fecha_desde": "2026-07-31",
                                  "fecha_hasta": "2026-07-01"})

    def test_a_valid_window_survives(self):
        args = validate_orders_args({"fecha_desde": "2026-07-01",
                                     "fecha_hasta": "2026-07-31"})
        self.assertEqual(args["fecha_desde"], date(2026, 7, 1))
        self.assertEqual(args["fecha_hasta"], date(2026, 7, 31))

    def test_sql_in_a_text_argument_is_refused(self):
        with self.assertRaises(InternalAuthError):
            validate_orders_args({"cliente": "acme'; DROP TABLE oc_clientes--"})

    def test_group_by_is_a_closed_vocabulary(self):
        self.assertEqual(validate_orders_args({"group_by": "mes"})["group_by"], "mes")
        with self.assertRaises(InternalAuthError):
            validate_orders_args({"group_by": "semana"})

    def test_limit_stays_inside_the_gateway_range(self):
        with self.assertRaises(InternalAuthError):
            validate_orders_args({"limit": 99})
        self.assertEqual(validate_orders_args({"limit": 20})["limit"], 20)


class TheToolIsInEveryAllowlistTests(unittest.TestCase):
    """Las cinco puertas. Una tool que falte en una no se ejecuta."""

    def test_orchestrator_catalog_contract_and_validator(self):
        from app.assistant.orchestrator.arg_schema import validate_tool_args
        from app.assistant.orchestrator.catalog import ALLOWED_TOOLS
        from app.assistant.orchestrator.tool_contracts import TOOL_CONTRACTS

        self.assertIn("get_orders", ALLOWED_TOOLS)
        self.assertIn("get_orders", TOOL_CONTRACTS)
        self.assertEqual(validate_tool_args("get_orders", {"limit": 5})["limit"], 5)

    def test_gateway_schema_and_registry(self):
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        if str(root / "andes_agent") not in sys.path:
            sys.path.append(str(root / "andes_agent"))
        from andes_agent.schemas import validate_tool_arguments
        from andes_agent.tools.registry import ALLOWED_TOOLS as GATEWAY

        self.assertIn("get_orders", GATEWAY)
        self.assertEqual(validate_tool_arguments("get_orders", {})["limit"], 10)

    def test_it_is_a_read_tool(self):
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        if str(root / "andes_agent") not in sys.path:
            sys.path.append(str(root / "andes_agent"))
        from andes_agent.tools import get_orders as gw

        self.assertFalse(gw.WRITE)

    def test_the_contract_and_the_schema_agree(self):
        from app.assistant.orchestrator.llm.plan_schema import (
            contract_schema_drift, format_drift)

        findings = [f for f in contract_schema_drift() if f["tool"] == "get_orders"]
        self.assertEqual(findings, [], "\n" + format_drift(findings))


class BothRenderingsExistTests(unittest.TestCase):
    """Una tool no esta entregada hasta que existen sus DOS renderizados: la
    leccion de V02 en 8.9, donde el fallback publico nombres de campos."""

    def _reply(self, data: dict) -> str:
        from app.assistant.orchestrator.composer import compose_answer
        from app.assistant.orchestrator.normalizer import normalize_tool_result

        item = normalize_tool_result(200, {"ok": True, "tool": "get_orders",
                                           "classification": "CONFIDENTIAL",
                                           "data": data, "meta": {}})
        item["tool"] = "get_orders"
        return compose_answer(plan={"steps": [{"step": 1, "tool": "get_orders"}]},
                              evidence=[item])["reply"]

    BASE = {"ordenes": 69, "lineas": 91, "unidades": 91, "count": 91,
            "ordenes_por_estado": {"pagada": 65, "recibida": 4},
            "anuladas_excluidas": True,
            "periodo": {"desde": None, "hasta": None},
            "items": [{"numero_oc": "74867", "fecha": "2026-09-16",
                       "estado": "recibida", "codigo": "GRPOERP62011",
                       "cantidad": 1}]}

    def test_the_text_is_prose_not_field_names(self):
        reply = self._reply(dict(self.BASE))
        self.assertIn("orden(es)", reply)
        self.assertNotIn("Campos en evidencia", reply)
        self.assertNotIn("anuladas_excluidas", reply)

    def test_the_text_always_states_that_cancelled_orders_are_out(self):
        """Un total que calla que excluyo las anuladas se lee como el total de
        todo, y son dos cifras distintas."""
        self.assertIn("anuladas", self._reply(dict(self.BASE)).lower())

    def test_the_text_always_states_its_scope(self):
        sin = self._reply(dict(self.BASE))
        self.assertIn("sin filtro de fecha", sin)
        con = self._reply({**self.BASE,
                           "periodo": {"desde": "2026-07-01", "hasta": "2026-07-31"}})
        self.assertIn("2026-07-01", con)

    def test_the_state_breakdown_travels(self):
        self.assertIn("pagada", self._reply(dict(self.BASE)))

    def test_the_card_carries_the_scope_too(self):
        from app.assistant.orchestrator.answer_view import TOOL_VIEWS

        self.assertIn("periodo", TOOL_VIEWS["get_orders"]["summary"])

    def test_the_card_never_projects_personal_fields(self):
        from app.assistant.orchestrator.answer_view import TOOL_VIEWS

        spec = TOOL_VIEWS["get_orders"]
        for field in spec["fields"]:
            self.assertNotIn(field, NEVER_PROJECTED)


class TheRequirementTypeIsConservativeTests(unittest.TestCase):
    """Una orden de cliente y una de compra apuntan a lados opuestos del
    negocio y comparten la palabra. Un falso positivo levanta un requisito que
    nadie cubre, y un requisito sin cubrir bloquea el final_answer."""

    def _types(self, message: str) -> list[str]:
        from app.assistant.orchestrator.goal_coverage import extract_requirement_types

        return extract_requirement_types(message)[1]

    def test_a_customer_order_is_detected_with_its_qualifier(self):
        for message in ("Cuantas ordenes de cliente hay?",
                        "Dame las OC de clientes de julio"):
            with self.subTest(message=message):
                self.assertIn("customer_orders", self._types(message))

    def test_a_purchase_order_never_becomes_a_customer_order(self):
        types = self._types("Ordenes de compra de BOSCH")
        self.assertIn("purchase_orders", types)
        self.assertNotIn("customer_orders", types)

    def test_a_bare_order_word_raises_nothing(self):
        for message in ("en orden alfabetico", "pon esto en orden"):
            with self.subTest(message=message):
                self.assertNotIn("customer_orders", self._types(message))

    def test_the_benchmark_dataset_gains_no_new_requirement(self):
        """Ningun caso existente puede empezar a pedir esta tool: eso seria una
        regresion silenciosa sobre 76 casos que hoy pasan."""
        from evals.fase81g_closure import _load_cases

        for case in _load_cases():
            with self.subTest(case=case.get("id")):
                self.assertNotIn("customer_orders",
                                 self._types(case.get("prompt") or ""))

    def test_the_tool_is_reachable_from_its_requirement(self):
        from app.assistant.orchestrator.goal_coverage import covering_tools

        self.assertEqual(covering_tools("customer_orders"), frozenset({"get_orders"}))

class TheCapabilityIsOffUntilMeasuredTests(unittest.TestCase):
    """Anadir una tool cambia el prompt de las 76 preguntas, y la seleccion de
    tool es la puerta mas puntuada del benchmark. Eso no se declara neutro sin
    una corrida con modelo: se apaga y se mide.

    La tool SI vive en las cinco allowlists y el validador la acepta — asi el
    drift, los contratos y las pruebas la cubren igual — pero el modelo no la ve.
    """

    def setUp(self):
        self._prev = os.environ.get("ANDES_ASSISTANT_ORDERS_ENABLED")

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("ANDES_ASSISTANT_ORDERS_ENABLED", None)
        else:
            os.environ["ANDES_ASSISTANT_ORDERS_ENABLED"] = self._prev
        self._reload()

    def _reload(self):
        import app.assistant.orchestrator.agent_config as ac
        import app.assistant.orchestrator.llm.agent_prompts as ap
        importlib.reload(ac)
        importlib.reload(ap)
        return ap

    def _prompt(self, flag: str) -> str:
        os.environ["ANDES_ASSISTANT_ORDERS_ENABLED"] = flag
        return self._reload().build_agent_system_prompt()

    def test_the_default_is_off(self):
        os.environ.pop("ANDES_ASSISTANT_ORDERS_ENABLED", None)
        import app.assistant.orchestrator.agent_config as ac
        importlib.reload(ac)
        self.assertFalse(ac.orders_enabled())

    def test_the_model_does_not_see_the_tool_when_it_is_off(self):
        self.assertNotIn("get_orders", self._prompt("0"))

    def test_the_model_sees_it_when_it_is_on(self):
        self.assertIn("get_orders", self._prompt("1"))

    def test_the_only_difference_between_the_two_prompts_is_this_tool(self):
        """La prueba de neutralidad, dicha con precision: apagada, el prompt es
        el de antes y la UNICA diferencia con el encendido es get_orders. La
        linea de nombres tambien cambia —pierde la tool—, asi que la comparacion
        tiene que normalizarla en vez de exigir igualdad literal."""
        apagado = self._prompt("0").splitlines()
        encendido = self._prompt("1").splitlines()

        # El encendido tiene exactamente una linea mas: el contrato de la tool.
        sin_contrato = [l for l in encendido if not l.startswith("get_orders:")]
        self.assertEqual(len(sin_contrato), len(apagado))

        contratos = [l for l in encendido if l.startswith("get_orders:")]
        self.assertEqual(len(contratos), 1, "la tool aporta UNA linea de contrato")

        def _normalizar(linea: str) -> str:
            return linea.replace("get_orders, ", "").replace(", get_orders", "")

        for izq, der in zip(apagado, sin_contrato):
            self.assertEqual(izq, _normalizar(der),
                             "apagar no puede cambiar nada mas que esta tool")
        self.assertNotIn("get_orders", chr(10).join(apagado))

    def test_the_system_still_accepts_the_tool_while_hidden(self):
        """Ocultarla al modelo no la saca del sistema: el drift, el validador y
        los contratos la siguen cubriendo."""
        self._prompt("0")
        from app.assistant.orchestrator.arg_schema import validate_tool_args
        from app.assistant.orchestrator.catalog import ALLOWED_TOOLS

        self.assertIn("get_orders", ALLOWED_TOOLS)
        self.assertEqual(validate_tool_args("get_orders", {"limit": 5})["limit"], 5)


class EveryVisibleToolCarriesItsContractTests(unittest.TestCase):
    """El defecto de O01/O04 al reves: nombrar una tool sin su contrato la
    vuelve inllamable. Nombres y contratos tienen que filtrarse JUNTOS."""

    def _visible_and_contracted(self, flag: str) -> tuple[set, set]:
        os.environ["ANDES_ASSISTANT_ORDERS_ENABLED"] = flag
        import app.assistant.orchestrator.agent_config as ac
        import app.assistant.orchestrator.llm.agent_prompts as ap
        importlib.reload(ac)
        importlib.reload(ap)
        from app.assistant.orchestrator.catalog import ALLOWED_TOOLS
        from app.assistant.orchestrator.tool_contracts import format_contracts_for_prompt

        visibles = set(ac.model_facing_tools(ALLOWED_TOOLS))
        con_contrato = {l.split(":", 1)[0]
                        for l in format_contracts_for_prompt(only=visibles).splitlines()
                        if l.strip()}
        return visibles, con_contrato

    def setUp(self):
        self._prev = os.environ.get("ANDES_ASSISTANT_ORDERS_ENABLED")

    def tearDown(self):
        """RESTAURA, no borra. Un `pop()` aqui deja la bandera apagada y
        reetiqueta el brazo de la corrida entera: exactamente el defecto que ya
        costo una corrida en 8.x, repetido por mi en la clase de al lado."""
        if self._prev is None:
            os.environ.pop("ANDES_ASSISTANT_ORDERS_ENABLED", None)
        else:
            os.environ["ANDES_ASSISTANT_ORDERS_ENABLED"] = self._prev
        import app.assistant.orchestrator.agent_config as ac
        import app.assistant.orchestrator.llm.agent_prompts as ap
        importlib.reload(ac)
        importlib.reload(ap)

    def test_names_and_contracts_match_in_both_states(self):
        for flag in ("0", "1"):
            with self.subTest(flag=flag):
                visibles, con_contrato = self._visible_and_contracted(flag)
                self.assertEqual(visibles, con_contrato)

    def test_a_hidden_tool_appears_in_neither(self):
        visibles, con_contrato = self._visible_and_contracted("0")
        self.assertNotIn("get_orders", visibles)
        self.assertNotIn("get_orders", con_contrato)


if __name__ == "__main__":
    unittest.main()
