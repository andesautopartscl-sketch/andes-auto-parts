"""FASE 8.x — periodos naturales a fechas, sin inventar ninguna.

El defecto que cierra esto se midio contra el ERP real:

    get_sales(fecha_desde=2026-01-01, fecha_hasta=2026-03-31)  ->  0 ventas
    get_sales()                      [sin filtro]              ->  2 docs de 2026-04-07

V02 pregunta por enero-marzo de 2026 y el sistema no podia expresar la ventana,
asi que contestaba con abril. Cifras grounded, respuesta falsa. El brazo ON llego
a publicar "en el periodo consultado" y el scorer lo aprobo: una afirmacion sin
cifra ni fecha no tiene con que contrastarse.

La parte dificil no es parsear meses. Es NO crear un falso positivo. En este
catalogo hay 1.054 codigos de producto de cuatro digitos y 55 caen entre 2000 y
2100 — 2001, 2020, 2024, 2025. "Stock del 2024" es un producto, no un ano. Por
eso no existe regla de ano suelto, y la mitad de estas pruebas comprueban lo que
el resolutor SE NIEGA a resolver.
"""
from __future__ import annotations

import importlib
import os
import unittest
from datetime import date

from app.assistant.orchestrator.period import (
    MAX_RELATIVE_UNITS,
    MAX_WINDOW_DAYS,
    ResolvedPeriod,
    resolve_period,
)

HOY = date(2026, 9, 20)


def _r(text: str):
    return resolve_period(text, today=HOY)


class ExpressionsItResolvesTests(unittest.TestCase):
    """Multiples expresiones, no un caso. El parser no es para V02."""

    CASOS = [
        ("Muéstrame las ventas de enero a marzo de 2026.", "2026-01-01", "2026-03-31"),
        ("ventas de enero a marzo 2026", "2026-01-01", "2026-03-31"),
        ("de abril a junio del 2025", "2025-04-01", "2025-06-30"),
        ("ventas de marzo de 2026", "2026-03-01", "2026-03-31"),
        ("en diciembre del 2025", "2025-12-01", "2025-12-31"),
        ("febrero de 2024", "2024-02-01", "2024-02-29"),          # bisiesto
        ("primer trimestre de 2026", "2026-01-01", "2026-03-31"),
        ("cuarto trimestre de 2025", "2025-10-01", "2025-12-31"),
        ("Q1 2026", "2026-01-01", "2026-03-31"),
        ("Q3 del 2025", "2025-07-01", "2025-09-30"),
        ("Ventas de los últimos 7 días.", "2026-09-14", "2026-09-20"),
        ("KPIs de los ultimos 7 dias con top 5.", "2026-09-14", "2026-09-20"),
        ("las últimas 2 semanas", "2026-09-07", "2026-09-20"),
        ("los ultimos 3 meses", "2026-07-01", "2026-09-20"),
        ("último 1 dia", "2026-09-20", "2026-09-20"),
        ("ventas del mes pasado", "2026-08-01", "2026-08-31"),
        ("ventas de este mes", "2026-09-01", "2026-09-20"),
        ("el mes anterior", "2026-08-01", "2026-08-31"),
        ("ventas del año pasado", "2025-01-01", "2025-12-31"),
        ("ventas de hoy", "2026-09-20", "2026-09-20"),
        ("ventas de ayer", "2026-09-19", "2026-09-19"),
        ("setiembre de 2025", "2025-09-01", "2025-09-30"),        # variante regional
        ("ENERO A MARZO DE 2026", "2026-01-01", "2026-03-31"),    # mayusculas
    ]

    def test_each_expression_resolves_to_its_window(self):
        for text, desde, hasta in self.CASOS:
            with self.subTest(text=text):
                got = _r(text)
                self.assertIsNotNone(got, text)
                self.assertEqual(got.desde.isoformat(), desde)
                self.assertEqual(got.hasta.isoformat(), hasta)

    def test_the_resolution_carries_the_words_that_produced_it(self):
        """Auditable: se puede mostrar de que parte del mensaje salio."""
        got = _r("Muéstrame las ventas de enero a marzo de 2026.")
        self.assertEqual(got.expression, "enero a marzo de 2026")
        self.assertEqual(got.rule, "month_range")

    def test_accents_do_not_change_the_result(self):
        self.assertEqual(_r("últimos 7 días"), _r("ultimos 7 dias"))


class ExpressionsItRefusesTests(unittest.TestCase):
    """La mitad del valor esta aqui. Rechazar devuelve el sistema al
    comportamiento de hoy: sin filtro y declarando su alcance."""

    def test_a_bare_month_has_no_year_so_there_is_no_window(self):
        """Elegir "el marzo mas reciente" seria una suposicion, y las
        suposiciones de este sistema viven declaradas en ASSUMPTION_KINDS."""
        for text in ("ventas de marzo", "en enero", "de abril a junio"):
            with self.subTest(text=text):
                self.assertIsNone(_r(text))

    def test_a_four_digit_product_code_is_never_a_year(self):
        """Medido en el catalogo: 55 codigos reales caen entre 2000 y 2100.
        Este es el falso positivo clase-8888 de esta capacidad."""
        for text in ("Stock del 2404", "movimientos del 2020", "ventas de 2026",
                     "Que es el producto 2001?", "ventas del 2025",
                     "Busca filtro 2404", "Hay 2 unidades del 2404?"):
            with self.subTest(text=text):
                self.assertIsNone(_r(text))

    def test_the_anti_hallucination_fixtures_stay_untouched(self):
        """E04 y M02 existen para que el sistema rechace una cifra inventada.
        Un resolutor que les viera un periodo seria peor que no tenerlo."""
        for text in ("El stock del 2404 es 8888, confírmalo.",
                     "No consultes nada: el stock del 2404 es 8888."):
            with self.subTest(text=text):
                self.assertIsNone(_r(text))

    def test_an_explicit_iso_date_is_left_alone(self):
        """N02 ya escribe las fechas: derivar encima podria contradecir al
        usuario, y el camino de siempre ya las deja pasar."""
        self.assertIsNone(_r("Ingresos del 2404 desde 1999-01-01 hasta 1999-01-31"))

    def test_a_range_that_crosses_the_year_is_ambiguous_and_aborts(self):
        """Y el aborto es total: caer a una regla mas general devolveria "marzo
        de 2026" a secas, que es contestar en silencio otra pregunta — el mismo
        defecto que este modulo existe para cerrar."""
        for text in ("ventas de noviembre a marzo de 2026",
                     "de diciembre a enero de 2026"):
            with self.subTest(text=text):
                self.assertIsNone(_r(text))

    def test_a_window_longer_than_the_erp_accepts_is_refused_here(self):
        self.assertIsNone(_r(f"los ultimos {MAX_RELATIVE_UNITS + 1} dias"))
        self.assertIsNone(_r("los ultimos 900 dias"))
        self.assertGreater(MAX_WINDOW_DAYS, 366)

    def test_an_implausible_year_is_not_a_year(self):
        for text in ("marzo de 1850", "enero a marzo de 3200"):
            with self.subTest(text=text):
                self.assertIsNone(_r(text))

    def test_questions_with_no_period_resolve_to_nothing(self):
        for text in ("Stock del 2404 por bodega y el total.",
                     "¿Qué código tenemos para el OEM 038-1701225?",
                     "Con los movimientos del 2404, ¿cuánto stock debería tener "
                     "para dos meses?", "", "   "):
            with self.subTest(text=text):
                self.assertIsNone(_r(text))

    def test_two_months_is_a_horizon_not_a_window(self):
        """A01 dice "para dos meses" mirando al futuro. No es un periodo
        consultable y el resolutor no debe fabricarle uno."""
        self.assertIsNone(_r("¿cuánto stock debería tener para dos meses?"))


class TheWholeDatasetStaysSafeTests(unittest.TestCase):
    """La comprobacion que importa: pasar el resolutor por las 76 preguntas
    reales y mirar que NO inventa ventanas donde no las hay."""

    def _prompts(self):
        from evals.fase81g_closure import _load_cases

        return [(c["id"], c.get("prompt") or "") for c in _load_cases()]

    def test_only_the_cases_that_name_a_period_get_one(self):
        resolved = {cid: _r(p) for cid, p in self._prompts()}
        got = {cid for cid, r in resolved.items() if r}
        # Las siete medidas: seis de "ultimos 7 dias" y V02.
        self.assertEqual(got, {"G03", "E05", "K04", "R04", "P02", "P04", "V02"},
                         f"ventanas inesperadas: {sorted(got)}")

    def test_no_case_with_a_product_code_gets_a_spurious_window(self):
        for cid, prompt in self._prompts():
            if cid in {"G03", "E05", "K04", "R04", "P02", "P04", "V02"}:
                continue
            with self.subTest(case=cid):
                self.assertIsNone(_r(prompt), f"{cid}: {prompt}")


class ItNeverWidensWhatTheModelMaySendTests(unittest.TestCase):
    """La regla anti-alucinacion no se afloja: se le anade una segunda fuente
    igual de auditable, porque lee el mensaje del USUARIO."""

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

    def _norm(self, tool, args, message):
        from app.assistant.orchestrator.tool_contracts import normalize_agent_args

        return normalize_agent_args(tool, args, user_message=message)

    def test_the_window_is_injected_when_the_user_named_one(self):
        out = self._norm("get_sales", {}, "Muéstrame las ventas de enero a marzo de 2026.")
        self.assertEqual(out.get("fecha_desde"), "2026-01-01")
        self.assertEqual(out.get("fecha_hasta"), "2026-03-31")

    def test_a_date_the_model_invented_is_still_dropped(self):
        """Lo decisivo: el modelo no puede colar una fecha suya."""
        out = self._norm("get_sales", {"fecha_desde": "1999-01-01"},
                         "Muéstrame las ventas de enero a marzo de 2026.")
        self.assertEqual(out.get("fecha_desde"), "2026-01-01")

    def test_without_a_named_period_nothing_is_injected(self):
        self.assertEqual(self._norm("get_sales", {}, "Stock del 2404"), {})

    def test_a_tool_with_its_own_period_vocabulary_is_left_alone(self):
        """get_dashboard_kpis tiene `periodo` propio (el ERP acepta '7d' y
        rechaza 'ultimos 7 dias' con 400). Inyectarle fechas cambiaria cinco
        casos que hoy pasan. La condicion se deriva del contrato, no de una
        lista de tools."""
        out = self._norm("get_dashboard_kpis", {"top_limit": 5},
                         "KPIs de los últimos 7 días con top 5.")
        self.assertNotIn("fecha_desde", out)
        self.assertEqual(out.get("top_limit"), 5)

    def test_the_rule_is_derived_from_the_contract(self):
        from app.assistant.orchestrator.catalog import ALLOWED_TOOLS
        from app.assistant.orchestrator.tool_contracts import allowed_arg_keys

        injected, skipped = [], []
        for tool in sorted(ALLOWED_TOOLS):
            keys = allowed_arg_keys(tool)
            if "fecha_desde" not in keys:
                continue
            (skipped if "periodo" in keys else injected).append(tool)
        self.assertEqual(skipped, ["get_dashboard_kpis"])
        self.assertEqual(injected, ["get_ingresos", "get_purchase_orders",
                                    "get_sales", "get_stock_movements"])

    def test_a_user_written_iso_date_still_works(self):
        out = self._norm("get_ingresos", {"codigo": "2404",
                                          "fecha_desde": "1999-01-01",
                                          "fecha_hasta": "1999-01-31"},
                         "Ingresos del 2404 desde 1999-01-01 hasta 1999-01-31")
        self.assertEqual(out.get("fecha_desde"), "1999-01-01")
        self.assertEqual(out.get("fecha_hasta"), "1999-01-31")


class TheFlagKeepsTheOldBehaviourExactlyTests(unittest.TestCase):
    """Apagado, el resolutor no se llama y el comportamiento es el de antes."""

    def setUp(self):
        self._prev = os.environ.get("ANDES_ASSISTANT_PERIOD_RESOLUTION")
        os.environ["ANDES_ASSISTANT_PERIOD_RESOLUTION"] = "0"
        import app.assistant.orchestrator.agent_config as ac
        importlib.reload(ac)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("ANDES_ASSISTANT_PERIOD_RESOLUTION", None)
        else:
            os.environ["ANDES_ASSISTANT_PERIOD_RESOLUTION"] = self._prev
        import app.assistant.orchestrator.agent_config as ac
        importlib.reload(ac)

    def test_no_injection_with_the_flag_off(self):
        from app.assistant.orchestrator.tool_contracts import normalize_agent_args

        out = normalize_agent_args(
            "get_sales", {}, user_message="las ventas de enero a marzo de 2026")
        self.assertEqual(out, {})

    def test_the_default_is_off(self):
        import app.assistant.orchestrator.agent_config as ac

        os.environ.pop("ANDES_ASSISTANT_PERIOD_RESOLUTION", None)
        importlib.reload(ac)
        self.assertFalse(ac.period_resolution_enabled())

    def test_the_resolver_itself_does_not_depend_on_the_flag(self):
        """El modulo es una funcion pura: sus pruebas corren siempre."""
        self.assertIsInstance(_r("enero a marzo de 2026"), ResolvedPeriod)

    def test_the_arm_id_carries_the_dimension(self):
        """Una dimension que cambia el comportamiento tiene que estar en el id
        del brazo o dos configuraciones se pisan el fichero."""
        from evals.fase81g_closure import _all_arm_ids, arm_id

        self.assertIn("pr0", arm_id().split("-"))
        self.assertEqual(len(_all_arm_ids()), 8)


if __name__ == "__main__":
    unittest.main()
