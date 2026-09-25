"""FASE 10.1 — Context Router y Capability Router.

POR QUE EXISTEN, MEDIDO

El system prompt se reenvia ENTERO en cada decision. T09 toma 5 y su prompt son
~10 825 tokens, de los cuales ~6 015 son el mismo system cinco veces. Los
contratos de herramientas son el 27 % de ese system. Con 12 tools ya pesa; la
Fase 10 propone anadir once subsistemas sobre una holgura de 236 tokens.

Medido contra las 76 corridas reales ANTES de escribir el router:

    capability_miss      0
    visibles de media    6,0 de 12
    max_total_tokens     11 764 -> 10 964 proyectado

LAS DOS SALVAGUARDAS QUE HACEN ESTO SEGURO

1. Sin senal no se oculta nada. Un router que adivina mal en silencio es peor
   que no tener router.
2. Los routers solo QUITAN. La interseccion con lo que permisos y banderas ya
   autorizaron garantiza que ninguno puede reabrir una tool cerrada ni alcanzar
   contexto al que el servicio no tuviera acceso.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.assistant.orchestrator.capability_router import (
    CORE_TOOLS,
    select_capabilities,
)
from app.assistant.orchestrator.context_router import (
    BUDGETS,
    FORBIDDEN_KEYS,
    TOTAL_BUDGET_CHARS,
    select_context,
)

TODAS = frozenset({
    "get_inventory", "get_product", "search_catalog", "get_stock_movements",
    "get_supplier", "get_purchase_orders", "get_sales", "get_ingresos",
    "get_customer", "get_equivalences", "get_dashboard_kpis", "check_stock",
})


# ───────────────────────────────── A. seleccion de contexto

class ElContextoRelevanteEntraTests(unittest.TestCase):

    def test_las_entidades_resueltas_entran(self):
        d = select_context({"resolved_entities": {"codigo": "2404"}}, actor="ana")
        self.assertIn("resolved_entities", d.context)
        self.assertIn("incluido", d.why("resolved_entities"))

    def test_el_resumen_de_conversacion_entra_si_cabe(self):
        d = select_context({"conversation_summary": "consulto stock del 2404"},
                           actor="ana")
        self.assertIn("conversation_summary", d.context)

    def test_lo_que_entra_se_puede_explicar(self):
        d = select_context({"intent_hint": "inventory"}, actor="ana")
        self.assertIn("ch)", d.why("intent_hint"))


class ElContextoIrrelevanteQuedaFueraTests(unittest.TestCase):

    def test_un_bloque_vacio_no_entra(self):
        d = select_context({"conversation_summary": "", "last_tools": []},
                           actor="ana")
        self.assertNotIn("conversation_summary", d.context)
        self.assertEqual(d.why("conversation_summary"), "vacio")

    def test_un_bloque_no_declarado_queda_fuera_POR_DEFECTO(self):
        """Lo que hace que anadir agenda en 10.6 exija declararla con su
        presupuesto, en vez de colarse al prompt."""
        d = select_context({"agenda": [{"evento": "reunion"}]}, actor="ana")
        self.assertNotIn("agenda", d.context)
        self.assertEqual(d.why("agenda"), "bloque_no_declarado")

    def test_el_presupuesto_total_se_respeta(self):
        grande = {
            "conversation_summary": "x" * 5000,
            "last_tools": ["get_inventory"] * 200,
            "resolved_entities": {"codigo": "2404"},
            "intent_hint": "inventory",
        }
        d = select_context(grande, actor="ana")
        self.assertLessEqual(d.total_chars, TOTAL_BUDGET_CHARS)

    def test_cada_bloque_respeta_su_propio_techo(self):
        d = select_context({"conversation_summary": "y" * 9000}, actor="ana")
        self.assertLessEqual(d.included["conversation_summary"],
                             BUDGETS["conversation_summary"])

    def test_la_anafora_es_lo_ultimo_que_se_sacrifica(self):
        """`resolved_entities` sostiene "y cuanto stock tiene?": si el
        presupuesto aprieta, cae el resumen antes que las entidades."""
        d = select_context({
            "conversation_summary": "z" * 5000,
            "resolved_entities": {"codigo": "2404"},
        }, actor="ana")
        self.assertIn("resolved_entities", d.context)


class UnaListaSeRecortaNoSeTiraTests(unittest.TestCase):
    """Defecto propio, encontrado al medir el router recien escrito.

    Con una conversacion larga, `memory_hints` y `last_tools` se pasaban un poco
    del techo y se descartaban ENTEROS. Perder toda la memoria por pasarse un
    poco es peor que quedarse con los primeros hints, que son justo los que el
    selector ya priorizo.
    """

    def test_los_hints_que_caben_se_conservan(self):
        hints = [{"key": f"k{i}", "value": "x" * 80, "status": "active"}
                 for i in range(10)]
        d = select_context({"memory_hints": hints}, actor="ana")
        conservados = d.context.get("memory_hints") or []
        self.assertGreater(len(conservados), 0, "se tiro la memoria entera")
        self.assertLess(len(conservados), 10, "no se recorto nada")
        self.assertEqual(conservados, hints[:len(conservados)])

    def test_last_tools_tambien_se_recorta(self):
        d = select_context({"last_tools": ["get_inventory"] * 40}, actor="ana")
        self.assertGreater(len(d.context.get("last_tools") or []), 0)

    def test_si_ni_un_elemento_cabe_el_bloque_queda_fuera(self):
        d = select_context({"last_tools": ["x" * 5000]}, actor="ana")
        self.assertNotIn("last_tools", d.context)
        self.assertEqual(d.why("last_tools"), "sin_presupuesto")

    def test_un_dict_NO_se_recorta_a_medias(self):
        """Las claves de una entidad resuelta se sostienen entre si: media
        entidad es peor que ninguna."""
        d = select_context({"resolved_entities": {"codigo": "2404",
                                                  "relleno": "y" * 5000}},
                           actor="ana")
        self.assertNotIn("resolved_entities", d.context)

    def test_con_conversacion_larga_entran_TODOS_los_bloques(self):
        d = select_context({
            "conversation_summary": "resumen " * 200,
            "last_tools": ["get_inventory", "get_supplier"] * 12,
            "resolved_entities": {"codigo": "2404"},
            "intent_hint": "inventory",
            "memory_hints": [{"key": f"k{i}", "value": "z" * 80,
                              "status": "active"} for i in range(10)],
        }, actor="ana")
        self.assertEqual(d.excluded, {})
        self.assertLessEqual(d.total_chars, TOTAL_BUDGET_CHARS)


class LaMemoriaNoAprobadaQuedaFueraTests(unittest.TestCase):
    """La puerta que 10.4 necesitara, puesta desde ya.

    Hoy ningun hint trae `status`, asi que esto no cambia nada. Cuando exista el
    estado `suggested`, una memoria sugerida no podra llegar al prompt por
    olvido de nadie.
    """

    def test_una_memoria_sugerida_no_entra(self):
        d = select_context(
            {"memory_hints": [{"key": "marca_favorita", "status": "suggested"}]},
            actor="ana")
        self.assertNotIn("memory_hints", d.context)
        self.assertEqual(d.why("memory_hints"), "memoria_no_aprobada")

    def test_una_memoria_activa_si_entra(self):
        d = select_context(
            {"memory_hints": [{"key": "marca", "status": "active"}]}, actor="ana")
        self.assertIn("memory_hints", d.context)

    def test_sin_status_entra_porque_hoy_ninguna_lo_trae(self):
        """No cambiar el comportamiento actual es parte del diseno."""
        d = select_context({"memory_hints": [{"key": "marca"}]}, actor="ana")
        self.assertIn("memory_hints", d.context)

    def test_se_filtra_hint_a_hint_no_el_bloque_entero(self):
        d = select_context({"memory_hints": [
            {"key": "buena", "status": "active"},
            {"key": "sugerida", "status": "suggested"},
        ]}, actor="ana")
        claves = [h["key"] for h in d.context["memory_hints"]]
        self.assertEqual(claves, ["buena"])


class NingunSecretoLlegaAlPromptTests(unittest.TestCase):

    def test_una_clave_prohibida_tumba_el_bloque(self):
        for clave in ("api_key", "password", "token", "secret", "credential"):
            with self.subTest(clave=clave):
                d = select_context(
                    {"resolved_entities": {"codigo": "2404", clave: "xyz"}},
                    actor="ana")
                self.assertNotIn("resolved_entities", d.context)
                self.assertEqual(d.why("resolved_entities"), "clave_prohibida")

    def test_tambien_anidada(self):
        d = select_context(
            {"memory_hints": [{"key": "x", "meta": {"nested": {"token": "abc"}}}]},
            actor="ana")
        self.assertNotIn("memory_hints", d.context)

    def test_la_lista_cubre_lo_que_traera_el_vault(self):
        for esperada in ("vault", "private_key", "credentials", "apikey"):
            self.assertIn(esperada, FORBIDDEN_KEYS)


class ElContextoAjenoQuedaFueraTests(unittest.TestCase):

    def test_memoria_de_otro_actor_no_entra(self):
        d = select_context(
            {"memory_hints": [{"key": "x", "actor_user": "otro"}]}, actor="ana")
        self.assertNotIn("memory_hints", d.context)
        self.assertEqual(d.why("memory_hints"), "actor_ajeno")

    def test_memoria_de_otra_conversacion_no_entra(self):
        d = select_context(
            {"memory_hints": [{"key": "x", "conversation_id": "c-otra"}]},
            actor="ana", conversation_id="c-mia")
        self.assertNotIn("memory_hints", d.context)
        self.assertEqual(d.why("memory_hints"), "conversacion_ajena")

    def test_la_memoria_de_la_conversacion_actual_si_entra(self):
        d = select_context(
            {"memory_hints": [{"key": "x", "conversation_id": "c-mia",
                               "actor_user": "ana"}]},
            actor="ana", conversation_id="c-mia")
        self.assertIn("memory_hints", d.context)


class ElRouterDeContextoSoloQuitaTests(unittest.TestCase):

    def test_nunca_introduce_un_bloque_que_no_estaba(self):
        d = select_context({"intent_hint": "inventory"}, actor="ana")
        self.assertTrue(set(d.context) <= {"intent_hint", "force_scenario"})

    def test_un_contexto_vacio_produce_un_contexto_vacio(self):
        self.assertEqual(select_context({}, actor="ana").context, {})
        self.assertEqual(select_context(None, actor="ana").context, {})


# ───────────────────────────────── B. seleccion de capacidades

class LaToolRelevanteEntraTests(unittest.TestCase):

    def test_una_pregunta_de_proveedor_trae_get_supplier(self):
        d = select_capabilities("Proveedor BOSCH y sus ordenes de compra.",
                                available=TODAS)
        self.assertIn("get_supplier", d.selected)
        self.assertIn("get_purchase_orders", d.selected)

    def test_el_caso_T09_trae_sus_TRES_tools(self):
        d = select_capabilities(
            "Del proveedor BOSCH dame sus ordenes de compra y el stock actual del 2404.",
            available=TODAS)
        for t in ("get_supplier", "get_purchase_orders", "get_inventory"):
            self.assertIn(t, d.selected, t)

    def test_el_caso_C04_trae_get_ingresos(self):
        """El unico capability_miss que aparecio al medir contra las 76
        corridas: `ingresos` es un requisito declarado SIN senal en
        `_AUTO_SIGNALS`. La tabla de visibilidad cubre ese hueco."""
        d = select_capabilities("Compara ingresos y stock actual del 2404.",
                                available=TODAS)
        self.assertIn("get_ingresos", d.selected)
        self.assertIn("get_inventory", d.selected)

    def test_el_nucleo_esta_siempre_que_haya_senal(self):
        d = select_capabilities("Movimientos del 2404.", available=TODAS)
        self.assertTrue(CORE_TOOLS <= d.selected)


class LaToolIrrelevanteQuedaFueraTests(unittest.TestCase):

    def test_una_pregunta_de_stock_no_trae_proveedores_ni_kpis(self):
        d = select_capabilities("Cuanto stock tiene el 2404?", available=TODAS)
        for t in ("get_supplier", "get_purchase_orders", "get_dashboard_kpis",
                  "get_equivalences"):
            self.assertIn(t, d.hidden, t)

    def test_se_oculta_algo_de_verdad(self):
        d = select_capabilities("Movimientos del 2404.", available=TODAS)
        self.assertGreaterEqual(len(d.hidden), 4)

    def test_cada_tool_seleccionada_explica_por_que(self):
        d = select_capabilities("Proveedor BOSCH.", available=TODAS)
        for t in d.selected:
            self.assertIn(t, d.reasons)
        self.assertEqual(d.reasons["get_supplier"], "supplier")


class SinSenalNoSeOcultaNadaTests(unittest.TestCase):
    """La salvaguarda principal: un router que adivina mal en silencio es peor
    que no tener router."""

    def test_un_saludo_ve_el_catalogo_completo(self):
        d = select_capabilities("Hola", available=TODAS)
        self.assertEqual(d.selected, TODAS)
        self.assertEqual(d.hidden, frozenset())
        self.assertTrue(d.fell_back)

    def test_una_pregunta_sin_senal_tambien(self):
        d = select_capabilities("a ver que tal", available=TODAS)
        self.assertEqual(d.selected, TODAS)
        self.assertTrue(d.fell_back)


class ElRouterDeCapacidadesSoloQuitaTests(unittest.TestCase):

    def test_get_orders_sigue_cerrado_si_no_esta_disponible(self):
        """La bandera de ORDERS manda. El router no puede reabrirla."""
        d = select_capabilities("Pedidos del cliente Juan.", available=TODAS)
        self.assertNotIn("get_orders", d.selected)

    def test_nunca_selecciona_fuera_de_lo_disponible(self):
        for msg in ("Proveedor BOSCH", "stock del 2404", "Hola",
                    "ingresos y ventas", "equivalencias OEM"):
            with self.subTest(msg=msg):
                d = select_capabilities(msg, available=TODAS)
                self.assertTrue(d.selected <= TODAS, msg)

    def test_no_inventa_tools(self):
        d = select_capabilities("dame el clima y las ventas", available=TODAS)
        self.assertTrue(d.selected <= TODAS)

    def test_con_una_sola_disponible_devuelve_esa_o_menos(self):
        d = select_capabilities("stock del 2404", available={"get_inventory"})
        self.assertTrue(d.selected <= {"get_inventory"})


class ElPromptNoPuedeQuedarseSinToolsTests(unittest.TestCase):
    """Un conjunto vacio no es una optimizacion: es un turno roto."""

    def _user(self, **kw):
        from app.assistant.orchestrator.llm.agent_prompts import (
            build_agent_system_prompt)

        return build_agent_system_prompt(**kw)

    def test_un_capabilities_vacio_se_ignora(self):
        self.assertEqual(self._user(), self._user(capabilities=frozenset()))

    def test_capabilities_None_es_el_comportamiento_de_9x(self):
        self.assertEqual(self._user(), self._user(capabilities=None))

    def test_capabilities_no_puede_anadir_una_tool_no_autorizada(self):
        p = self._user(capabilities={"get_orders", "get_inventory"})
        self.assertNotIn("get_orders", p)
        self.assertIn("get_inventory", p)


# ───────────────────────────────── C. presupuesto de tokens

class ElPromptSeEncogeDeVerdadTests(unittest.TestCase):
    """FASE 10.1b — el bloque de tools vive en el USER prompt, no en el system.

    Medido: con los contratos dentro del system, el Capability Router los hacia
    variar por turno, el cache de prefijo cayo de 81,3 % a 13,6 % y el
    equivalente facturable SE DUPLICO (126 090 -> 256 939) pese a bajar los
    tokens brutos un 11 %. El user prompt no se cachea de todos modos, asi que
    ahi la variacion es gratis y el system queda invariable y cacheable entero.
    """

    def _tam(self, capabilities=None):
        from app.assistant.orchestrator.llm.agent_prompts import (
            build_agent_system_prompt)

        return len(build_agent_system_prompt(capabilities=capabilities))

    def test_menos_tools_es_menos_prompt(self):
        completo = self._tam()
        acotado = self._tam({"get_inventory", "get_product"})
        self.assertLess(acotado, completo)

    def test_el_ahorro_es_material_no_simbolico(self):
        """Se multiplica por el numero de decisiones: T09 hace 5."""
        completo = self._tam()
        acotado = self._tam({"get_inventory", "get_product", "search_catalog",
                             "get_stock_movements"})
        ahorro_por_decision = (completo - acotado) // 4
        self.assertGreater(ahorro_por_decision, 50, "menos de 50 tok no compensa")

    def test_la_lista_de_nombres_y_los_contratos_se_filtran_JUNTOS(self):
        """Nombrar una tool sin su contrato la vuelve inllamable: es el defecto
        O01/O04 al reves."""
        from app.assistant.orchestrator.llm.agent_prompts import (
            build_agent_system_prompt)

        p = build_agent_system_prompt(capabilities={"get_inventory"})
        self.assertNotIn("get_equivalences", p)


# ───────────────────────────────── D. banderas y compatibilidad

class ApagadoElComportamientoEsElDeNueveTests(unittest.TestCase):

    def test_ambas_banderas_por_defecto_apagadas(self):
        from app.assistant.orchestrator.agent_config import (
            capability_router_enabled, context_router_enabled)

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANDES_ASSISTANT_CAPABILITY_ROUTER", None)
            os.environ.pop("ANDES_ASSISTANT_CONTEXT_ROUTER", None)
            self.assertFalse(capability_router_enabled())
            self.assertFalse(context_router_enabled())

    def test_se_pueden_encender_por_entorno(self):
        from app.assistant.orchestrator.agent_config import (
            capability_router_enabled, context_router_enabled)

        with patch.dict(os.environ, {
            "ANDES_ASSISTANT_CAPABILITY_ROUTER": "1",
            "ANDES_ASSISTANT_CONTEXT_ROUTER": "1",
        }, clear=False):
            self.assertTrue(capability_router_enabled())
            self.assertTrue(context_router_enabled())


class LaObservabilidadNoFiltraContenidoTests(unittest.TestCase):

    def test_la_observacion_de_capacidades_son_nombres(self):
        d = select_capabilities("Proveedor BOSCH", available=TODAS)
        obs = d.observation()
        self.assertEqual(
            set(obs),
            {"capabilities_selected", "capabilities_hidden", "capability_reasons",
             "capability_requirements", "capability_fallback"})

    def test_la_observacion_de_contexto_son_tamanos(self):
        d = select_context({"conversation_summary": "secreto operativo"},
                           actor="ana")
        obs = d.observation()
        self.assertNotIn("secreto operativo", str(obs))
        self.assertEqual(set(obs), {"context_included", "context_excluded",
                                    "context_chars", "context_estimated_tokens"})


if __name__ == "__main__":
    unittest.main()
