"""FASE 8.x — el contrato y el schema de generacion tienen que decir lo mismo.

El defecto que cierra esta prueba costo O01 y O04 en las corridas con LLM real,
y ninguna prueba determinista lo veia porque cada lado era coherente consigo
mismo: `tool_contracts` declaraba `oem`, `arg_schema` lo validaba, el prompt lo
anunciaba, el aviso de reintento lo nombraba — y el schema de generacion, con
`strict=True` y `additionalProperties=False`, no lo tenia entre sus propiedades.
Bajo strict eso no es "opcional": es imposible de emitir.

Medido el 2026-09-20, identico en los dos brazos:

    O01  "OEM 038-1701225"    -> necesita `oem`     -> 2 reintentos, fallback
    O04  "OEM ZZZNOEXISTE999" -> necesita `oem`     -> 2 reintentos, fallback
    O02  "codigo 2404"        -> necesita `codigo`  -> PASS
    O03  "codigo 2404"        -> necesita `codigo`  -> PASS

La particion es exacta: todo caso que necesitaba una clave emitible paso, todo
caso que necesitaba una inemitible murio. No era varianza del modelo.
"""
from __future__ import annotations

import copy
import unittest

from app.assistant.orchestrator.catalog import ALLOWED_TOOLS
from app.assistant.orchestrator.llm.plan_schema import (
    AGENT_DECISION_JSON_SCHEMA,
    INTERNAL_ONLY_ARGS,
    PLAN_JSON_SCHEMA,
    contract_schema_drift,
    format_drift,
)
from app.assistant.orchestrator.tool_contracts import TOOL_CONTRACTS


def _args_node(schema: dict) -> dict:
    """El nodo `arguments`. En la decision del agente cuelga de la raiz; en el
    plan cuelga de cada step. Los dos comparten la misma tabla derivada."""
    props = schema["properties"]
    if "arguments" in props:
        return props["arguments"]
    return props["steps"]["items"]["properties"]["arguments"]


def _emittable(schema: dict) -> set[str]:
    return set(_args_node(schema)["properties"])


def _declared(tool: str) -> dict:
    spec = TOOL_CONTRACTS[tool]
    return {**(spec.get("required") or {}), **(spec.get("optional") or {})}


class ContractAndSchemaAgreeTests(unittest.TestCase):
    """La invariante general, recorrida automaticamente sobre todas las tools."""

    def test_no_drift_in_any_direction(self):
        findings = contract_schema_drift()
        self.assertEqual(findings, [], "\n" + format_drift(findings))

    def test_every_contract_argument_is_emittable(self):
        """Eje 1, dicho explicitamente: contract_arg no en schema.properties."""
        emittable = _emittable(AGENT_DECISION_JSON_SCHEMA)
        for tool in sorted(ALLOWED_TOOLS):
            for arg in sorted(_declared(tool)):
                with self.subTest(tool=tool, arg=arg):
                    self.assertIn(
                        arg, emittable,
                        f"{tool}.{arg} declarado en el contrato y no emitible")

    def test_the_four_arguments_that_were_missing_are_there(self):
        """Regresion nominal del defecto medido, para que no vuelva en silencio."""
        emittable = _emittable(AGENT_DECISION_JSON_SCHEMA)
        for arg in ("oem", "modelo", "cliente", "group_by"):
            self.assertIn(arg, emittable, arg)

    def test_the_planner_schema_carries_the_same_arguments(self):
        """Las dos rutas comparten tabla: si divergen, el planner hereda el bug."""
        self.assertEqual(_emittable(PLAN_JSON_SCHEMA),
                         _emittable(AGENT_DECISION_JSON_SCHEMA))

    def test_strict_mode_still_lists_every_property_as_required(self):
        """Sin esto el proveedor rechaza el schema entero."""
        for schema in (AGENT_DECISION_JSON_SCHEMA, PLAN_JSON_SCHEMA):
            args = _args_node(schema)
            self.assertIs(args["additionalProperties"], False)
            self.assertEqual(sorted(args["required"]), sorted(args["properties"]))

    def test_the_normaliser_keeps_every_advertised_argument(self):
        """Eje 2 — el eslabon que faltaba en el primer intento de arreglo.

        `oem` estaba fuera del schema Y fuera de la lista de passthrough del
        normalizador. Hacerlo emitible sin tocar el normalizador habria dejado
        O01/O04 exactamente igual de rotos, con el mismo `one_required`, y
        habria parecido que el diagnostico era falso."""
        findings = [f for f in contract_schema_drift() if f["side"] == "normalizer"]
        self.assertEqual(findings, [], "\n" + format_drift(findings))

    def test_a_dead_property_is_reported_as_drift(self):
        """Eje 3. Se comprueba contra la UNION de contratos: `oem` no pertenece a
        get_sales y eso no es drift, porque `arguments` es un objeto compartido
        por todas las tools."""
        union = {k for t in ALLOWED_TOOLS for k in _declared(t)}
        self.assertEqual(_emittable(AGENT_DECISION_JSON_SCHEMA) - union, set(),
                         "propiedades emitibles que ninguna tool declara")

    def test_the_validator_accepts_every_advertised_argument(self):
        """Eje 2: anunciar en el prompt algo que el validador rechaza es la misma
        clase de mentira, con el rechazo un paso mas tarde."""
        findings = [f for f in contract_schema_drift() if f["side"] == "validator"]
        self.assertEqual(findings, [], "\n" + format_drift(findings))

    def test_internal_only_arguments_stay_an_explicit_short_list(self):
        """`tipos` lo acepta el validador y el contrato NO lo declara, a
        proposito: elegir que cuenta como venta es del sistema, no del modelo —
        con orden_compra dentro, el resultado cambiaria de signo. La excepcion se
        escribe para que no pueda crecer en silencio."""
        self.assertEqual(INTERNAL_ONLY_ARGS, {"get_sales": frozenset({"tipos"})})
        emittable = _emittable(AGENT_DECISION_JSON_SCHEMA)
        for tool, args in INTERNAL_ONLY_ARGS.items():
            for arg in args:
                self.assertNotIn(arg, _declared(tool))
                self.assertNotIn(arg, emittable)


class TheDetectorActuallyFiresTests(unittest.TestCase):
    """Un detector que devuelve cero sin haber disparado nunca no prueba nada."""

    def test_it_catches_the_exact_defect_that_broke_O01(self):
        import app.assistant.orchestrator.llm.plan_schema as ps

        broken = copy.deepcopy(AGENT_DECISION_JSON_SCHEMA)
        broken["properties"]["arguments"]["properties"].pop("oem")
        original = ps.AGENT_DECISION_JSON_SCHEMA
        ps.AGENT_DECISION_JSON_SCHEMA = broken
        try:
            findings = ps.contract_schema_drift()
            rendered = ps.format_drift(findings)
        finally:
            ps.AGENT_DECISION_JSON_SCHEMA = original
        rows = [f for f in findings
                if f["arg"] == "oem" and f["side"] == "generation_schema"]
        self.assertTrue(rows, "el detector no vio la clave inemitible")
        self.assertEqual(rows[0]["tool"], "get_equivalences")
        self.assertIn("get_equivalences.oem", rendered)
        self.assertIn("generation_schema", rendered)

    def test_it_catches_the_hand_written_passthrough_list(self):
        """El eje 2 tiene que disparar contra la lista literal que habia antes."""
        import app.assistant.orchestrator.tool_contracts as tc

        original = tc.passthrough_string_keys
        tc.passthrough_string_keys = lambda tool: (
            "marca", "bodega", "proveedor", "numero", "estado", "periodo", "rut")
        try:
            findings = contract_schema_drift()
        finally:
            tc.passthrough_string_keys = original
        dropped = {(f["tool"], f["arg"]) for f in findings if f["side"] == "normalizer"}
        # 9.2 — get_orders comparte `cliente` y `group_by` con get_sales, asi que
        # la lista literal de antes tampoco los habria pasado para la tool nueva.
        self.assertEqual(dropped, {
            ("get_equivalences", "oem"), ("get_equivalences", "modelo"),
            ("get_sales", "cliente"), ("get_sales", "group_by"),
            ("get_orders", "cliente"), ("get_orders", "group_by")})

    def test_it_catches_a_dead_property(self):
        import app.assistant.orchestrator.llm.plan_schema as ps

        broken = copy.deepcopy(AGENT_DECISION_JSON_SCHEMA)
        broken["properties"]["arguments"]["properties"]["zzz_fantasma"] = {
            "type": ["string", "null"]}
        original = ps.AGENT_DECISION_JSON_SCHEMA
        ps.AGENT_DECISION_JSON_SCHEMA = broken
        try:
            findings = ps.contract_schema_drift()
        finally:
            ps.AGENT_DECISION_JSON_SCHEMA = original
        self.assertTrue([f for f in findings if f["arg"] == "zzz_fantasma"])

    def test_the_diagnostic_names_tool_argument_and_side(self):
        rendered = format_drift([{"tool": "get_equivalences", "arg": "oem",
                                  "side": "generation_schema", "detail": "d"}])
        for token in ("get_equivalences", "oem", "generation_schema"):
            self.assertIn(token, rendered)

    def test_a_conflicting_type_between_two_tools_is_refused_at_build(self):
        """Dos tools declarando la misma clave con tipos distintos no caben en un
        objeto `arguments` unico; elegir uno por orden de iteracion seria mudo."""
        import app.assistant.orchestrator.llm.plan_schema as ps

        poisoned = copy.deepcopy(TOOL_CONTRACTS)
        poisoned["get_sales"]["optional"]["codigo"] = {"type": "int"}
        original = ps.TOOL_CONTRACTS
        ps.TOOL_CONTRACTS = poisoned
        try:
            with self.assertRaises(RuntimeError):
                ps._build_argument_props()
        finally:
            ps.TOOL_CONTRACTS = original

    def test_an_unmappable_contract_type_is_refused_at_build(self):
        import app.assistant.orchestrator.llm.plan_schema as ps

        poisoned = copy.deepcopy(TOOL_CONTRACTS)
        poisoned["get_sales"]["optional"]["cliente"] = {"type": "decimal"}
        original = ps.TOOL_CONTRACTS
        ps.TOOL_CONTRACTS = poisoned
        try:
            with self.assertRaises(RuntimeError):
                ps._build_argument_props()
        finally:
            ps.TOOL_CONTRACTS = original


class AnyOfStaysSingleSourcedTests(unittest.TestCase):
    """OBJETIVO 3 — la regla oem|codigo sigue definida en UN solo sitio."""

    def test_the_anchor_is_unchanged(self):
        self.assertEqual(TOOL_CONTRACTS["get_equivalences"]["any_of"],
                         ("oem", "codigo"))

    def test_every_anchor_is_emittable(self):
        """El ancla es el unico argumento que el modelo esta OBLIGADO a mandar."""
        emittable = _emittable(AGENT_DECISION_JSON_SCHEMA)
        for tool in sorted(ALLOWED_TOOLS):
            for arg in TOOL_CONTRACTS[tool].get("any_of") or ():
                with self.subTest(tool=tool, arg=arg):
                    self.assertIn(arg, emittable)

    def test_prompt_retry_and_validator_all_read_the_same_tuple(self):
        from app.assistant.orchestrator.arg_schema import (
            ArgSchemaError, validate_tool_args)
        from app.assistant.orchestrator.tool_contracts import (
            format_contracts_for_prompt, inspect_arg_fields)

        line = [ln for ln in format_contracts_for_prompt().splitlines()
                if ln.startswith("get_equivalences:")][0]
        self.assertIn("al menos uno de: oem|codigo", line)
        self.assertEqual(inspect_arg_fields("get_equivalences", {"marca": "BOSCH"}),
                         {"oem|codigo": "one_required"})
        with self.assertRaises(ArgSchemaError):
            validate_tool_args("get_equivalences", {"marca": "BOSCH"})
        self.assertEqual(
            validate_tool_args("get_equivalences", {"oem": "038-1701225"})["oem"],
            "038-1701225")

    def test_changing_the_anchor_moves_prompt_and_retry_together(self):
        """La prueba de que no esta duplicada: se toca el contrato y los tres
        consumidores cambian solos."""
        import app.assistant.orchestrator.tool_contracts as tc

        original = tc.TOOL_CONTRACTS["get_equivalences"]["any_of"]
        tc.TOOL_CONTRACTS["get_equivalences"]["any_of"] = ("marca",)
        try:
            line = [ln for ln in tc.format_contracts_for_prompt().splitlines()
                    if ln.startswith("get_equivalences:")][0]
            self.assertIn("al menos uno de: marca", line)
            self.assertEqual(tc.inspect_arg_fields("get_equivalences", {"oem": "X"}),
                             {"marca": "one_required"})
        finally:
            tc.TOOL_CONTRACTS["get_equivalences"]["any_of"] = original


class EndToEndEmissionTests(unittest.TestCase):
    """De la decision del modelo a los argumentos que salen hacia el Gateway."""

    def _strict_payload(self, **args) -> dict:
        """Una decision como la produce el proveedor bajo strict: TODAS las
        propiedades presentes, las no usadas en null."""
        payload = {k: None for k in _emittable(AGENT_DECISION_JSON_SCHEMA)}
        payload.update(args)
        return {k: v for k, v in payload.items() if v is not None}

    def test_an_oem_decision_survives_normalisation_and_validation(self):
        from app.assistant.orchestrator.arg_schema import validate_tool_args
        from app.assistant.orchestrator.tool_contracts import normalize_agent_args

        raw = self._strict_payload(oem="038-1701225")
        norm = normalize_agent_args("get_equivalences", raw)
        self.assertEqual(norm.get("oem"), "038-1701225")
        self.assertEqual(validate_tool_args("get_equivalences", norm)["oem"],
                         "038-1701225")

    def test_a_modelo_decision_survives_too(self):
        from app.assistant.orchestrator.arg_schema import validate_tool_args
        from app.assistant.orchestrator.tool_contracts import normalize_agent_args

        raw = self._strict_payload(codigo="2404", modelo="T60 2.8")
        out = validate_tool_args("get_equivalences",
                                 normalize_agent_args("get_equivalences", raw))
        self.assertEqual(out.get("codigo"), "2404")
        self.assertEqual(out.get("modelo"), "T60 2.8")

    def test_the_sales_arguments_survive_too(self):
        from app.assistant.orchestrator.arg_schema import validate_tool_args
        from app.assistant.orchestrator.tool_contracts import normalize_agent_args

        raw = self._strict_payload(cliente="ACME", group_by="mes")
        out = validate_tool_args("get_sales",
                                 normalize_agent_args("get_sales", raw))
        self.assertEqual(out.get("cliente"), "ACME")
        self.assertEqual(out.get("group_by"), "mes")

    def test_a_missing_anchor_still_fails_with_a_named_field(self):
        from app.assistant.orchestrator.tool_contracts import structured_invalid_args

        err = structured_invalid_args("get_equivalences", {"marca": "BOSCH"})
        self.assertEqual(err["fields"], {"oem|codigo": "one_required"})


if __name__ == "__main__":
    unittest.main()
