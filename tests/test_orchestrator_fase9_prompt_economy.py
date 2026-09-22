"""FASE 9.1 — economia de prompt: abaratar sin perder una sola senal.

POR QUE, MEDIDO

    system prompt      1731 tokens, de los cuales 592 son contratos (34%)
    coste por tool     ~49 tokens por decision
    pico observado     10.620 tokens    ·    techo del presupuesto 12.000

Con 12 tools quedan 1.380 tokens de margen. Las tres capacidades comerciales que
la fase 9 justifica (ordenes, demanda, sourcing) cuestan 735 de pico, asi que
CABEN sin tocar el prompt. La economia no es una puerta; es holgura para que el
crecimiento de la evidencia comercial no acerque el pico al techo.

QUE SE QUITA Y POR QUE ES NEUTRO

`:string` en los argumentos. Es redundante dos veces: el schema de generacion
corre en modo strict, asi que `q` ya esta fijado a ["string","null"] y el modelo
NO PUEDE emitir otra cosa aunque el prompt calle. Se le estaba diciendo al
modelo lo que el decodificador ya impone.

`required=none` / `optional=none`. Una clausula vacia no informa de nada que la
ausencia no diga igual.

QUE NO SE TOCA

Las descripciones. Son la senal con la que el modelo elige tool, y la seleccion
es la puerta mas puntuada del benchmark. Quitarlas ahorraba 155 tokens mas por
decision y no vale su riesgo: esto es exactamente la clase de cambio que tumbo
T07 de 9/10 a 0/10 en 8.1I.2.

Los tipos que NO son string tampoco: una fecha lleva formato y un int lleva
rango, y eso el schema no lo dice todo.

POR QUE NO SE HACE INYECCION CONDICIONAL DE CONTRATOS

Era la idea obvia —renderizar solo los contratos de las tools que cubren los
requisitos detectados— y la medicion la desaconseja HOY:

    62 de 76 casos con extraction=detected
    media de 1,84 tools cubiertas de 12  ->  se renderizaria el 15%
    PERO 4 casos (C02, C04, C08, G06) se quedarian SIN el contrato de la tool
    que su gold espera, y los cuatro PASAN hoy en el benchmark valido.

Cambiar 4 casos que pasan por un ahorro que ya no hace falta es un mal negocio.
La causa es estructural y vale registrarla: los requisitos se extraen del primer
mensaje, y el agente usa legitimamente tools mas alla de lo detectado (C02 pide
inventario y ademas quiere el detalle del producto). Una inyeccion condicional
correcta tendria que crecer con la evidencia, no fijarse en el turno 1.
"""
from __future__ import annotations

import unittest

from app.assistant.orchestrator.agent_config import CHARS_PER_TOKEN, MAX_AGENT_STEPS
from app.assistant.orchestrator.catalog import ALLOWED_TOOLS
from app.assistant.orchestrator.tool_contracts import (
    TOOL_CONTRACTS,
    format_contracts_for_prompt,
)

# Lo medido antes del cambio, para que el ahorro sea comprobable y no una
# afirmacion en un comentario.
# Medido con las 12 tools de 8.x. Con 13 la referencia se recalcula sobre el
# mismo formato viejo, para que el ahorro siga siendo comparable cuando el
# catalogo crezca: lo que se mide es el FORMATO, no el numero de tools.
def _legacy_chars() -> int:
    from app.assistant.orchestrator.catalog import ALLOWED_TOOLS
    from app.assistant.orchestrator.tool_contracts import TOOL_CONTRACTS

    total = 0
    for name in sorted(ALLOWED_TOOLS):
        spec = TOOL_CONTRACTS[name]
        req = spec.get("required") or {}
        opt = spec.get("optional") or {}
        req_s = ",".join(f"{k}:{v.get('type')}" for k, v in req.items()) or "none"
        opt_s = ",".join(f"{k}:{v.get('type')}" for k, v in opt.items()) or "none"
        line = f"{name}: {spec['description']}. required={req_s}. optional={opt_s}."
        any_of = spec.get("any_of") or ()
        if any_of:
            line += f" requiere al menos uno de: {'|'.join(any_of)}."
        total += len(line) + 1
    return total - 1


CONTRATOS_ANTES_CHARS = _legacy_chars()


def _rendered() -> dict[str, str]:
    return {line.split(":", 1)[0]: line
            for line in format_contracts_for_prompt().splitlines() if line.strip()}


class NothingIsLostTests(unittest.TestCase):
    """La compresion no puede quitar informacion, solo redundancia."""

    def test_every_tool_still_has_a_line(self):
        self.assertEqual(set(_rendered()), set(ALLOWED_TOOLS))

    def test_every_description_survives(self):
        rendered = _rendered()
        for tool in sorted(ALLOWED_TOOLS):
            with self.subTest(tool=tool):
                self.assertIn(TOOL_CONTRACTS[tool]["description"], rendered[tool])

    def test_every_argument_survives(self):
        rendered = _rendered()
        for tool in sorted(ALLOWED_TOOLS):
            spec = TOOL_CONTRACTS[tool]
            for arg in {**(spec.get("required") or {}), **(spec.get("optional") or {})}:
                with self.subTest(tool=tool, arg=arg):
                    self.assertIn(arg, rendered[tool])

    def test_required_and_optional_stay_distinguishable(self):
        rendered = _rendered()
        for tool in sorted(ALLOWED_TOOLS):
            spec = TOOL_CONTRACTS[tool]
            with self.subTest(tool=tool):
                self.assertEqual(bool(spec.get("required")),
                                 "required=" in rendered[tool])
                self.assertEqual(bool(spec.get("optional")),
                                 "optional=" in rendered[tool])

    def test_the_conditional_anchor_is_untouched(self):
        self.assertIn("al menos uno de: oem|codigo", _rendered()["get_equivalences"])


class OnlyRedundancyIsRemovedTests(unittest.TestCase):

    def test_no_string_type_is_annotated_because_strict_mode_guarantees_it(self):
        """El argumento de correccion: el schema ya fija el tipo."""
        from app.assistant.orchestrator.llm.plan_schema import (
            AGENT_DECISION_JSON_SCHEMA)

        props = AGENT_DECISION_JSON_SCHEMA["properties"]["arguments"]["properties"]
        rendered = _rendered()
        for tool in sorted(ALLOWED_TOOLS):
            spec = TOOL_CONTRACTS[tool]
            for arg, meta in {**(spec.get("required") or {}),
                              **(spec.get("optional") or {})}.items():
                if (meta or {}).get("type") != "string":
                    continue
                with self.subTest(tool=tool, arg=arg):
                    self.assertNotIn(f"{arg}:string", rendered[tool])
                    # y el schema lo garantiza igualmente
                    self.assertIn("string", props[arg]["type"])

    def test_every_non_string_type_is_still_annotated(self):
        """date e int SI informan: formato y rango. No se tocan."""
        rendered = _rendered()
        for tool in sorted(ALLOWED_TOOLS):
            spec = TOOL_CONTRACTS[tool]
            for arg, meta in {**(spec.get("required") or {}),
                              **(spec.get("optional") or {})}.items():
                kind = (meta or {}).get("type")
                if kind == "string":
                    continue
                with self.subTest(tool=tool, arg=arg, kind=kind):
                    self.assertIn(f"{arg}:{kind}", rendered[tool])

    def test_empty_clauses_are_omitted_not_rendered_as_none(self):
        self.assertNotIn("=none", format_contracts_for_prompt())


class TheSavingIsRealAndBounded(unittest.TestCase):

    def test_the_measured_saving_holds(self):
        ahorro = (CONTRATOS_ANTES_CHARS - len(format_contracts_for_prompt())) / CHARS_PER_TOKEN
        self.assertGreater(ahorro, 100, "el ahorro medido era 112 tok/decision")
        self.assertGreater(ahorro * MAX_AGENT_STEPS, 2 * 49 * MAX_AGENT_STEPS / 2,
                           "debe valer al menos una tool de margen")

    def test_the_headroom_admits_the_three_planned_capabilities(self):
        """La aritmetica que reordeno el roadmap: ordenes + demanda + sourcing
        caben, asi que la economia es holgura, no una puerta."""
        from app.assistant.orchestrator.agent_config import TOKEN_BUDGET_FALLBACK

        pico_medido = 10620
        coste_por_tool = 49
        ahorro = (CONTRATOS_ANTES_CHARS - len(format_contracts_for_prompt())) / CHARS_PER_TOKEN
        pico = pico_medido - ahorro * MAX_AGENT_STEPS + 3 * coste_por_tool * MAX_AGENT_STEPS
        self.assertLess(pico, TOKEN_BUDGET_FALLBACK,
                        "tres capacidades nuevas deben caber bajo el techo")


class ConditionalInjectionStaysDeferredWithItsEvidenceTests(unittest.TestCase):
    """La decision de NO hacerlo queda comprobable, no en un comentario.

    Si alguien —yo incluido— quiere retomarla, esta prueba le dice exactamente
    que casos se romperian y por que la causa es estructural."""

    GAPS = {"C02", "C04", "C08", "G06"}

    def test_a_turn_one_only_injection_would_starve_four_passing_cases(self):
        from evals.fase81g_closure import _load_cases

        from app.assistant.orchestrator.goal_coverage import (
            GoalCoverage, covering_tools)

        huecos = set()
        for case in _load_cases():
            snap = GoalCoverage.from_message(case.get("prompt") or "").safe_snapshot()
            tipos = [r["type"] for r in (snap.get("requirements") or [])]
            if snap.get("extraction") != "detected" or not tipos:
                continue
            cubiertas: set[str] = set()
            for t in tipos:
                cubiertas |= set(covering_tools(t))
            esperadas = set(case.get("expected_tools") or [])
            for fam in (case.get("acceptable_tool_families") or {}).values():
                esperadas |= set(fam)
            if esperadas - cubiertas:
                huecos.add(str(case.get("id")))
        self.assertEqual(huecos, self.GAPS,
                         "cambio el mapa de cobertura: reevaluar la decision")

    def test_the_full_contract_set_is_what_ships_today(self):
        self.assertEqual(len(_rendered()), len(ALLOWED_TOOLS))


if __name__ == "__main__":
    unittest.main()
