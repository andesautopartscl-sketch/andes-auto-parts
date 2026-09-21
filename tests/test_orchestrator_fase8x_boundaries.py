"""FASE 8.x — revision final de fronteras. Tres defectos, los tres de borde.

Ninguno se ve leyendo una capa sola: los tres viven donde una capa declara algo
y la siguiente hace otra cosa, que es el patron de todo este bloque.

1. **El contexto ofrecia fechas que el normalizador tiraba.**
   `build_agent_context_note` lleva `periodo_desde`/`periodo_hasta` al prompt
   desde `resolved_entities`, y esos valores salen de la evidencia ERP de un
   turno anterior. El modelo los usaba en el turno siguiente y
   `normalize_agent_args` los descartaba por no aparecer literalmente en el
   mensaje de ESE turno. La llamada salia sin filtro y la respuesta volvia a
   hablar de todo el historial: el fallo de V02 reapareciendo en multi-turno,
   provocado esta vez por el propio sistema.

2. **El probe del schema analitico comprobaba por identidad.**
   `analytic_schema is not BASE` — una copia profunda que no ensanchara NADA
   habria pasado. Y el defecto que ese probe existe para atrapar es justo ese:
   en 8.5 el validador aceptaba peldanos que el schema no podia emitir, y la
   corrida reporto "cero usos de la escalera" como si midiera al modelo.

3. **La tarjeta mostraba cifras sin su alcance.**
   El texto de `get_sales` declara siempre su ventana desde 8.x, y la tarjeta no
   la proyectaba porque `_scalar` se niega —con razon— a rendir un dict. Una
   tarjeta que diga "0 unidades en 2 documentos" sin decir sobre que periodo es
   la falsedad de V02 en forma de tarjeta.
"""
from __future__ import annotations

import importlib
import os
import unittest


class ContextDatesAreAThirdLegitimateSourceTests(unittest.TestCase):
    """Tres fuentes verificables, ninguna del modelo: lo que el usuario
    escribio, lo que el sistema derivo de sus palabras, y lo que el ERP ya
    devolvio en un turno anterior."""

    def setUp(self):
        self._prev = os.environ.get("ANDES_ASSISTANT_PERIOD_RESOLUTION")
        os.environ["ANDES_ASSISTANT_PERIOD_RESOLUTION"] = "1"
        import app.assistant.orchestrator.agent_config as ac
        importlib.reload(ac)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("ANDES_ASSISTANT_PERIOD_RESOLUTION", None)
        else:
            os.environ["ANDES_ASSISTANT_PERIOD_RESOLUTION"] = self._prev
        import app.assistant.orchestrator.agent_config as ac
        importlib.reload(ac)

    CTX = {"resolved_entities": {"codigo": "2417",
                                 "periodo_desde": "2026-01-01",
                                 "periodo_hasta": "2026-03-31"}}
    SEGUNDO = "Y las ventas del 2417 en ese mismo periodo?"

    def _norm(self, args, message=SEGUNDO, context=None):
        from app.assistant.orchestrator.tool_contracts import normalize_agent_args

        return normalize_agent_args("get_sales", args, user_message=message,
                                    context=context)

    def test_the_prompt_really_offers_those_dates(self):
        """La premisa. Si dejara de ofrecerlas, esta prueba dejaria de medir."""
        from app.assistant.orchestrator.llm.agent_prompts import (
            build_agent_context_note)

        note = build_agent_context_note(self.CTX)
        self.assertIn("2026-01-01", note)
        self.assertIn("2026-03-31", note)

    def test_a_date_the_system_established_is_accepted(self):
        out = self._norm({"codigo": "2417", "fecha_desde": "2026-01-01",
                          "fecha_hasta": "2026-03-31"}, context=self.CTX)
        self.assertEqual(out.get("fecha_desde"), "2026-01-01")
        self.assertEqual(out.get("fecha_hasta"), "2026-03-31")

    def test_without_that_context_the_old_behaviour_is_unchanged(self):
        out = self._norm({"codigo": "2417", "fecha_desde": "2026-01-01"})
        self.assertNotIn("fecha_desde", out)

    def test_an_invented_date_is_still_dropped_even_with_context(self):
        """Lo decisivo: se anade una fuente, no se afloja la regla."""
        out = self._norm({"fecha_desde": "1999-01-01"}, context=self.CTX)
        self.assertNotIn("fecha_desde", out)

    def test_a_prior_window_is_never_injected_by_itself(self):
        """Asimetria deliberada: la ventana del mensaje actual es inequivoca y
        se inyecta; la de un turno anterior es contextual y solo se acepta si el
        modelo la usa. "Y las ventas de todo el ano?" debe poder ensanchar."""
        out = self._norm({"codigo": "2417"}, context=self.CTX)
        self.assertNotIn("fecha_desde", out)
        self.assertEqual(out.get("codigo"), "2417")

    def test_a_stale_window_is_visible_in_the_answer(self):
        """El riesgo que esto introduce —arrastrar una ventana que ya no aplica—
        queda acotado porque el texto declara SIEMPRE cual se consulto. Antes el
        descarte silencioso producia una llamada sin filtro, que es peor: mas
        amplia y sin decirlo."""
        from app.assistant.orchestrator.composer import compose_answer

        data = {"unidades": 0, "documentos": 0, "count": 0, "items": [],
                "periodo": {"desde": "2026-01-01", "hasta": "2026-03-31"}}
        reply = compose_answer(
            plan={"steps": [{"step": 1, "tool": "get_sales"}]},
            evidence=[{"tool": "get_sales", "ok": True, "empty": False,
                       "finance_redacted": False, "data": data, "meta": {}}])["reply"]
        self.assertIn("2026-01-01", reply)
        self.assertIn("2026-03-31", reply)

    def test_only_period_keys_are_admitted_from_context(self):
        """El contexto no es una puerta abierta: solo dos claves, y con forma
        de fecha."""
        from app.assistant.orchestrator.tool_contracts import established_dates

        self.assertEqual(established_dates(self.CTX),
                         frozenset({"2026-01-01", "2026-03-31"}))
        self.assertEqual(established_dates(
            {"resolved_entities": {"codigo": "2417", "periodo_desde": "ayer"}}),
            frozenset())
        for basura in (None, {}, {"resolved_entities": None}, "texto"):
            self.assertEqual(established_dates(basura), frozenset())

    def test_the_context_never_becomes_grounding(self):
        """Aceptar una fecha como ARGUMENTO no la convierte en evidencia. El
        verifier no mira el contexto, y no puede empezar a mirarlo."""
        from pathlib import Path

        src = Path("app/assistant/orchestrator/answer_verifier.py").read_text(
            encoding="utf-8")
        self.assertNotIn("resolved_entities", src)
        self.assertNotIn("context", src)


class TheAnalyticSchemaProbeChecksContentTests(unittest.TestCase):
    """Un probe que comprueba identidad no comprueba nada."""

    def test_the_probe_asserts_the_ladder_is_really_emittable(self):
        from evals.fase81g_closure import probe_conditional_analysis

        r = probe_conditional_analysis()
        self.assertEqual(r["verdict"], "PASS")
        self.assertTrue(r["analytic_schema_really_emits_the_ladder"])
        d = r["emittable_detail"]
        self.assertEqual(d["ladder_rungs_missing"], [])
        self.assertTrue(d["mul_emittable"])
        self.assertTrue(d["assumptions_property"])
        self.assertEqual(d["assumption_kinds_missing"], [])
        self.assertEqual(d["bases_mismatch"], [])
        self.assertTrue(d["assumption_ids_required"])

    def test_a_plain_turn_inherits_none_of_it(self):
        from evals.fase81g_closure import probe_conditional_analysis

        self.assertTrue(probe_conditional_analysis()["plain_schema_has_no_ladder"])

    def test_the_generation_schema_and_the_validator_agree_on_assumptions(self):
        """La misma fuente de verdad en los dos lados: el defecto de 8.5 fue
        exactamente que no la compartian.

        El `finally` RESTAURA, no borra. La primera version hacia `pop()`, y como
        el closure corre esta suite EN PROCESO antes de escribir su informe, ese
        pop destruia la identidad del brazo en curso: una corrida con
        ANALYSIS=1 acabo escribiendose en el fichero de `an0-pv0-pr0`. Un test
        que muta entorno global y no lo devuelve puede reetiquetar un brazo
        entero del benchmark.
        """
        previo = os.environ.get("ANDES_ASSISTANT_ANALYSIS_ENABLED")
        os.environ["ANDES_ASSISTANT_ANALYSIS_ENABLED"] = "1"
        try:
            from app.assistant.orchestrator.analysis import (
                ASSUMPTION_KINDS, VALID_BASES)
            from app.assistant.orchestrator.llm.plan_schema import (
                agent_decision_response_format)

            schema = agent_decision_response_format(
                analytical=True)["json_schema"]["schema"]
            props = schema["properties"]["assumptions"]["items"]["properties"]
            self.assertEqual(set(props["kind"]["enum"]), set(ASSUMPTION_KINDS))
            self.assertEqual(set(props["basis"]["enum"]), set(VALID_BASES))
        finally:
            if previo is None:
                os.environ.pop("ANDES_ASSISTANT_ANALYSIS_ENABLED", None)
            else:
                os.environ["ANDES_ASSISTANT_ANALYSIS_ENABLED"] = previo

    def test_no_test_module_leaves_the_arm_flags_altered(self):
        """Guardia general sobre la clase entera de fallo: cualquier modulo que
        toque una bandera de brazo tiene que devolverla. Se comprueba corriendo
        los modulos que las manipulan y mirando el entorno antes y despues."""
        import unittest as ut

        flags = ("ANDES_ASSISTANT_ANALYSIS_ENABLED",
                 "ANDES_ASSISTANT_PERIOD_RESOLUTION",
                 "ANDES_ASSISTANT_PROVENANCE_ENFORCE")
        antes = {f: os.environ.get(f) for f in flags}
        suite = ut.TestLoader().loadTestsFromNames([
            "tests.test_orchestrator_fase8x_period",
            "tests.test_orchestrator_fase85b_capability_gate",
        ])
        ut.TextTestRunner(verbosity=0, stream=open(os.devnull, "w")).run(suite)
        self.assertEqual({f: os.environ.get(f) for f in flags}, antes)


class ACardNeverShowsAFigureWithoutItsScopeTests(unittest.TestCase):

    def _summary(self, periodo):
        from app.assistant.orchestrator.answer_view import _with_flat_scope
        from app.assistant.orchestrator.answer_view import TOOL_VIEWS

        data = _with_flat_scope({"unidades": 0, "documentos": 2,
                                 "ingresos": 0.0, "periodo": periodo})
        return data, TOOL_VIEWS["get_sales"]["summary"]

    def test_the_scope_is_part_of_the_sales_summary(self):
        _, summary = self._summary({"desde": None, "hasta": None})
        self.assertIn("periodo", summary)

    def test_an_unfiltered_window_says_so_on_the_card(self):
        data, _ = self._summary({"desde": None, "hasta": None})
        self.assertEqual(data["periodo"], "sin filtro de fecha")

    def test_a_filtered_window_shows_its_dates(self):
        data, _ = self._summary({"desde": "2026-01-01", "hasta": "2026-03-31"})
        self.assertEqual(data["periodo"], "2026-01-01 a 2026-03-31")

    def test_a_half_open_window_is_not_rendered_as_closed(self):
        data, _ = self._summary({"desde": "2026-01-01", "hasta": None})
        self.assertEqual(data["periodo"], "desde 2026-01-01")

    def test_a_payload_without_scope_is_left_untouched(self):
        from app.assistant.orchestrator.answer_view import _with_flat_scope

        self.assertEqual(_with_flat_scope({"unidades": 3}), {"unidades": 3})

    def test_nested_structures_are_still_refused_as_card_fields(self):
        """El aplanado es una excepcion declarada, no una puerta para proyectar
        dicts en general."""
        from app.assistant.orchestrator.answer_view import _scalar

        self.assertIsNone(_scalar({"desde": "x"}))
        self.assertIsNone(_scalar(["a", "b"]))

    def test_the_card_and_the_text_say_the_same_thing(self):
        """La invariante: una tarjeta no puede mostrar algo que el texto no
        pueda afirmar, ni callar lo que el texto declara."""
        from app.assistant.orchestrator.answer_view import _with_flat_scope
        from app.assistant.orchestrator.composer import compose_answer

        data = {"unidades": 0, "documentos": 2, "count": 2, "ingresos": 0.0,
                "items": [{"fecha": "2026-04-07", "numero": "FA-0001",
                           "codigo": "2417", "cantidad": 1}],
                "periodo": {"desde": None, "hasta": None}}
        reply = compose_answer(
            plan={"steps": [{"step": 1, "tool": "get_sales"}]},
            evidence=[{"tool": "get_sales", "ok": True, "empty": False,
                       "finance_redacted": False, "data": data, "meta": {}}])["reply"]
        card = _with_flat_scope(data)["periodo"]
        self.assertIn("sin filtro de fecha", reply)
        self.assertIn("sin filtro de fecha", card)


class TheReadOnlyPerimeterIsUnchangedTests(unittest.TestCase):
    """Nada de esta fase abrio una via de escritura."""

    def test_the_five_allowlists_still_agree_and_carry_no_write_tool(self):
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        if str(root / "andes_agent") not in sys.path:
            sys.path.append(str(root / "andes_agent"))
        from andes_agent.tools.registry import ALLOWED_TOOLS as GATEWAY

        from app.assistant.orchestrator.arg_schema import (
            ArgSchemaError, validate_tool_args)
        from app.assistant.orchestrator.catalog import ALLOWED_TOOLS
        from app.assistant.orchestrator.tool_contracts import TOOL_CONTRACTS

        self.assertEqual(set(ALLOWED_TOOLS), set(TOOL_CONTRACTS))
        self.assertEqual(set(ALLOWED_TOOLS), set(GATEWAY))
        for tool in ALLOWED_TOOLS:
            try:
                validate_tool_args(tool, {})
            except ArgSchemaError as exc:
                self.assertNotIn("Unknown tool", str(exc), tool)
        prefijos = ("create_", "update_", "delete_", "anular_", "post_",
                    "put_", "write_", "set_")
        self.assertEqual([t for t in set(ALLOWED_TOOLS) | set(GATEWAY)
                          if t.startswith(prefijos)], [])

    def test_the_agent_decision_tool_field_is_guarded_by_the_validator(self):
        """Asimetria conocida y medida: el schema del PLAN restringe `tool` por
        enum y el de la decision no. Medido en 131 corridas reales: CERO tools
        inventadas, y el validador las rechaza igualmente. Se deja como esta
        porque apretar el schema de generacion es un cambio que exige A/B con
        modelo, y este proyecto ya pago una vez por uno sin medir."""
        from app.assistant.orchestrator.agent_schema import (
            AgentDecisionError, validate_agent_decision)

        # Dos caminos, y el de escritura dispara ANTES que el de allowlist.
        for tool, code in (("delete_everything", "write_not_allowed"),
                           ("get_inventado", "tool_not_allowed")):
            with self.subTest(tool=tool):
                with self.assertRaises(AgentDecisionError) as ctx:
                    validate_agent_decision({"action": "call_tool", "tool": tool,
                                             "arguments": {}})
                self.assertEqual(ctx.exception.code, code)

class TheScopeInEvidenceMakesAPeriodClaimRefutableTests(unittest.TestCase):
    """Frontera ERP -> evidencia -> verifier, sin cobertura hasta ahora.

    Antes de 8.x el payload de get_sales callaba su ventana cuando no habia
    filtro, asi que "en el periodo consultado" no tenia con que contrastarse: el
    brazo ON de la tercera A/B publico esa frase, era falsa, y el scorer la
    aprobo. Con el alcance SIEMPRE en evidencia, una afirmacion de periodo sobre
    una consulta sin filtro pasa a ser refutable.
    """

    def _verify(self, periodo, texto):
        from app.assistant.orchestrator.answer_verifier import verify_agent_answer
        from app.assistant.orchestrator.evidence_store import EvidenceStore

        store = EvidenceStore()
        store.add_from_tool_result(
            tool="get_sales", arguments={},
            result={"ok": True, "status": 200, "tool": "get_sales",
                    "classification": "INTERNAL", "write": False, "meta": {},
                    "empty": False, "error_code": None, "message": None,
                    "finance_redacted": False, "stock_omitted": False,
                    "data": {"unidades": 0, "documentos": 0, "count": 0,
                             "items": [], "periodo": periodo}})
        return verify_agent_answer(
            store=store,
            decision={"action": "final_answer", "draft_reply": texto,
                      "claims": [{"kind": "dato", "text": texto,
                                  "evidence_ids": ["e1"]}],
                      "calculations": []})

    VENTANA = {"desde": "2026-01-01", "hasta": "2026-03-31"}

    def test_a_window_that_is_in_evidence_is_grounded(self):
        r = self._verify(self.VENTANA,
                         "No hubo ventas entre el 2026-01-01 y el 2026-03-31.")
        self.assertEqual(r.failures, 0)
        self.assertFalse(r.answer_replaced)

    def test_a_window_that_contradicts_the_evidence_is_dropped(self):
        r = self._verify(self.VENTANA,
                         "No hubo ventas entre el 2025-05-01 y el 2025-06-30.")
        self.assertGreater(r.failures, 0)
        self.assertTrue(r.answer_replaced)

    def test_claiming_a_window_over_an_unfiltered_query_is_now_refutable(self):
        """El caso que importa: exactamente la falsedad de V02, ahora atrapada."""
        r = self._verify({"desde": None, "hasta": None},
                         "No hubo ventas entre el 2026-01-01 y el 2026-03-31.")
        self.assertGreater(r.failures, 0)
        self.assertTrue(r.answer_replaced)
        self.assertIn("sin filtro de fecha", r.reply)


class TheHarnessDeclaresItsOwnArmIntegrityTests(unittest.TestCase):
    """Guardia observable del defecto que ocurrio de verdad: un test que no
    devolvia una bandera reetiquetaba el brazo de la corrida entera."""

    def test_every_live_deterministic_report_declares_a_stable_arm(self):
        import glob
        import json
        from pathlib import Path

        from evals.fase81g_closure import OUT_DIR

        encontrados = 0
        for path in glob.glob(str(Path(OUT_DIR) / "fase81_final_report.det.*-pr*.json")):
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            integridad = data.get("arm_integrity")
            self.assertIsNotNone(integridad, path)
            self.assertTrue(integridad["stable"],
                            f"{path}: {integridad}")
            self.assertEqual(integridad["before_tests"], data["arm"]["arm"])
            encontrados += 1
        self.assertGreaterEqual(encontrados, 1, "no hay informes deterministas")


if __name__ == "__main__":
    unittest.main()
