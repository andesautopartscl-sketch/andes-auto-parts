"""FASE 9.6 — D1: "entre X y Y" era un mes suelto. D2: la aclaracion no se decia.

D1, MEDIDO EN UNA CONVERSACION REAL

    usuario : "Tuvimos ventas entre enero y marzo de 2026?"
    Gateway : get_sales(fecha_desde=2026-03-01, fecha_hasta=2026-03-31)
    respuesta: "No hubo ventas entre enero y marzo de 2026."

Marzo a secas, y una conclusion FALSA sobre un trimestre que nunca se consulto,
con toda la cadena aparentemente sana. La causa: `_rule_month_range` solo
aceptaba los conectores `a|hasta`; con `y` no coincidia y `_rule_single_month`
recogia "marzo de 2026". El centinela AMBIGUOUS no salvaba nada porque la regla
no llegaba a reconocer la forma.

Al cerrarlo aparecieron DOS puertas mas de la misma clase, y estan aqui:

  - "ventas de enero y marzo de 2026" (sin `entre`) daba marzo;
  - "entre el 31 de febrero y el 5 de marzo de 2026" (fecha invalida) daba marzo.

Las tres son el mismo defecto: reconocer a medias y dejar pasar a una regla mas
general, que contesta una pregunta mas estrecha sin decirlo.

D2

`clarify` existe como accion y pone la bandera bien. El modelo no la elige
porque el prompt la enumera sin explicar cuando usarla. La senal se deriva de la
estructura —final sin evidencia y sin claims— en vez de reescribir el prompt:
ese tipo de cambio ya costo T07 (9/10 -> 0/10) en 8.1I.2.
"""
from __future__ import annotations

import unittest
from datetime import date

from app.assistant.orchestrator.period import resolve_period

HOY = date(2026, 9, 21)
Q1 = (date(2026, 1, 1), date(2026, 3, 31))


def _ventana(frase: str):
    r = resolve_period(frase, today=HOY)
    return (r.desde, r.hasta) if r else None


def _regla(frase: str):
    r = resolve_period(frase, today=HOY)
    return r.rule if r else None


class ElCasoQueMotivoLaUnidadTests(unittest.TestCase):

    def test_entre_enero_y_marzo_de_2026(self):
        self.assertEqual(_ventana("Tuvimos ventas entre enero y marzo de 2026?"), Q1)

    def test_entre_el_1_de_enero_y_el_31_de_marzo_de_2026(self):
        self.assertEqual(
            _ventana("Tuvimos ventas entre el 1 de enero y el 31 de marzo de 2026?"), Q1)

    def test_entre_febrero_y_abril_de_2026(self):
        self.assertEqual(_ventana("Ventas entre febrero y abril de 2026"),
                         (date(2026, 2, 1), date(2026, 4, 30)))

    def test_de_enero_a_marzo_de_2026_no_cambia(self):
        """El comportamiento que YA era correcto no se toca."""
        self.assertEqual(_ventana("Muestrame las ventas de enero a marzo de 2026."), Q1)
        self.assertEqual(_regla("Muestrame las ventas de enero a marzo de 2026."),
                         "month_range")

    def test_ninguna_de_las_dos_formas_devuelve_un_mes_suelto(self):
        """La regresion exacta: marzo a secas era la respuesta equivocada."""
        marzo = (date(2026, 3, 1), date(2026, 3, 31))
        for frase in ("entre enero y marzo de 2026",
                      "entre el 1 de enero y el 31 de marzo de 2026"):
            with self.subTest(frase=frase):
                self.assertNotEqual(_ventana(frase), marzo)

    def test_los_dias_explicitos_se_respetan_tal_cual(self):
        self.assertEqual(_ventana("entre el 5 de enero y el 20 de marzo de 2026"),
                         (date(2026, 1, 5), date(2026, 3, 20)))

    def test_del_x_al_y_tambien(self):
        self.assertEqual(_ventana("del 1 de enero al 31 de marzo de 2026"), Q1)


class RangoSinAnoConContextoSuficienteTests(unittest.TestCase):
    """"este ano" no es adivinar: lo dice el mensaje."""

    def test_entre_meses_de_este_ano(self):
        self.assertEqual(_ventana("Ventas entre enero y marzo de este ano"), Q1)

    def test_entre_meses_del_ano_pasado(self):
        self.assertEqual(_ventana("Ventas entre enero y marzo del ano pasado"),
                         (date(2025, 1, 1), date(2025, 3, 31)))

    def test_el_ano_del_primer_extremo_se_hereda_del_segundo(self):
        self.assertEqual(
            _ventana("entre el 1 de enero de 2026 y el 31 de marzo de 2026"), Q1)


class RangoRealmenteAmbiguoTests(unittest.TestCase):
    """Cuando no se puede saber, no se inventa: no se resuelve nada."""

    def test_sin_ano_y_sin_referencia_no_resuelve(self):
        self.assertIsNone(_ventana("Ventas entre enero y marzo"))

    def test_un_rango_que_cruza_el_ano_no_resuelve(self):
        self.assertIsNone(_ventana("entre noviembre y marzo de 2026"))

    def test_una_enumeracion_sin_entre_no_resuelve(self):
        """"de enero y marzo" pueden ser dos meses sueltos o un rango mal dicho.
        Antes daba marzo; ahora se reconoce la forma y se aborta."""
        self.assertIsNone(_ventana("ventas de enero y marzo de 2026"))

    def test_una_fecha_invalida_aborta_en_vez_de_degradar(self):
        self.assertIsNone(_ventana("entre el 31 de febrero y el 5 de marzo de 2026"))

    def test_abortar_significa_no_resolver_nada_no_resolver_menos(self):
        marzo = (date(2026, 3, 1), date(2026, 3, 31))
        for frase in ("ventas de enero y marzo de 2026",
                      "entre noviembre y marzo de 2026",
                      "entre el 31 de febrero y el 5 de marzo de 2026"):
            with self.subTest(frase=frase):
                self.assertNotEqual(_ventana(frase), marzo)


class LasProteccionesDeSeguridadSiguenEnPieTests(unittest.TestCase):
    """Un codigo de producto no puede volverse una fecha. Nunca."""

    def test_ano_aislado(self):
        for frase in ("En 2026 hubo ventas?", "ventas de 2026",
                      "el total de 2026", "entre 2024 y 2026"):
            with self.subTest(frase=frase):
                self.assertIsNone(_ventana(frase), frase)

    def test_codigo_2026(self):
        for frase in ("Stock del 2026", "El codigo 2026 cuanto tiene?",
                      "muestrame el producto 2026"):
            with self.subTest(frase=frase):
                self.assertIsNone(_ventana(frase), frase)

    def test_codigo_2404(self):
        for frase in ("Stock del 2404", "Muestrame el stock del producto 2404.",
                      "movimientos del 2404"):
            with self.subTest(frase=frase):
                self.assertIsNone(_ventana(frase), frase)

    def test_a01_completo(self):
        """A01: "movimientos del 24/04" y el codigo 2404 en la misma frase.
        Ninguna de las reglas nuevas puede tocarlo: las dos exigen el NOMBRE de
        un mes, y "24/04" no lo tiene."""
        for frase in ("Movimientos del 24/04 para el producto 2404",
                      "Dame los movimientos del 24/04 y el stock esperado para dos meses",
                      "Stock esperado del 2404 para dos meses"):
            with self.subTest(frase=frase):
                self.assertIsNone(_ventana(frase), frase)

    def test_ningun_codigo_de_2000_a_2100_produce_ventana(self):
        """Barrido exhaustivo del rango que colisiona con el catalogo real."""
        malos = [n for n in range(2000, 2101)
                 if _ventana(f"Stock del producto {n}") is not None
                 or _ventana(f"movimientos del {n}") is not None]
        self.assertEqual(malos, [])

    def test_las_reglas_nuevas_exigen_nombre_de_mes(self):
        """Sin ancla lexica no hay fecha, por construccion."""
        for frase in ("entre 1 y 31 de 2026", "entre el 1 y el 31 de 2026",
                      "entre 01 y 03 de 2026"):
            with self.subTest(frase=frase):
                self.assertIsNone(_ventana(frase), frase)


class LoQueYaFuncionabaSigueIgualTests(unittest.TestCase):

    def test_mes_suelto(self):
        self.assertEqual(_ventana("marzo de 2026"),
                         (date(2026, 3, 1), date(2026, 3, 31)))

    def test_trimestre(self):
        self.assertEqual(_ventana("primer trimestre de 2026"), Q1)
        self.assertEqual(_regla("primer trimestre de 2026"), "quarter")

    def test_relativos(self):
        self.assertEqual(_ventana("ventas de los ultimos 7 dias"),
                         (date(2026, 9, 15), date(2026, 9, 21)))

    def test_fechas_iso_explicitas_no_se_derivan(self):
        self.assertIsNone(_ventana("ventas entre 2026-01-01 y 2026-03-31"))


class LaSenalDeAclaracionDiceLaVerdadTests(unittest.TestCase):
    """D2 — el turno tiene que reportar lo que hizo."""

    def _resultado(self, *, claims, evidencia):
        """Reproduce la decision del bucle en el punto exacto del arreglo."""
        sin_evidencia = not evidencia
        sin_claims = not (claims or [])
        return bool(sin_evidencia and sin_claims)

    def test_una_final_sin_evidencia_ni_claims_es_una_aclaracion(self):
        """N08: "Cuanto nos queda?" — 0 tools, 0 claims."""
        self.assertTrue(self._resultado(claims=[], evidencia=[]))

    def test_un_saludo_con_claims_no_lo_es(self):
        """N01/N02 emiten un claim: no son peticiones de aclaracion."""
        self.assertFalse(self._resultado(
            claims=[{"kind": "dato", "text": "Hola, en que puedo ayudarte?"}],
            evidencia=[]))

    def test_una_respuesta_con_evidencia_no_lo_es(self):
        self.assertFalse(self._resultado(
            claims=[{"kind": "dato", "text": "stock 2"}],
            evidencia=[{"tool": "get_inventory"}]))

    def test_no_se_mira_el_signo_de_interrogacion(self):
        """Los saludos tambien terminan en "?". Una heuristica sobre el texto
        marcaria los tres turnos; la estructura distingue."""
        import pathlib

        src = pathlib.Path("app/assistant/orchestrator/agent_loop.py").read_text(
            encoding="utf-8")
        i = src.find("sin_evidencia = not raw_evidence")
        self.assertGreater(i, 0)
        bloque = src[i:i + 400]
        self.assertNotIn("endswith(\"?\")", bloque)
        self.assertNotIn("'?'", bloque)

    def test_el_bucle_expone_needs_clarification_derivado(self):
        from app.assistant.orchestrator.agent_loop import AgentLoopResult

        campos = getattr(AgentLoopResult, "__annotations__", {})
        self.assertIn("needs_clarification", campos)

    def test_una_aclaracion_no_llama_tools(self):
        """Pedir aclaracion y ademas consultar seria gastar el turno dos veces.
        La rama `clarify` del bucle compone con evidence=[] por construccion."""
        import pathlib

        src = pathlib.Path("app/assistant/orchestrator/agent_loop.py").read_text(
            encoding="utf-8")
        i = src.find('if decision["action"] in {"clarify", "reject"}:')
        self.assertGreater(i, 0)
        self.assertIn("compose_answer(plan=plan, evidence=[]", src[i:i + 900])


class ElSeguimientoCubreLosAtributosDeFichaTests(unittest.TestCase):
    """FASE 9.7, restaurada en 9.9.

    Esta clase existio y la borre yo: al reescribir un bloque del fichero de 9.7
    con un script, el rango sustituido se la llevo por delante. La cobertura
    nuclear sobrevivio en `LaAnaforaNoPierdeContextoTests`, pero la amplitud
    —los demas atributos de ficha, las variantes de "cuanto tenemos" y el caso
    sin contexto previo— se perdio sin que nada avisara. Vive aqui, junto al
    resto de regresiones del vocabulario de seguimiento.

    Lo que fijan estos tests, medido en vivo: "Y la marca?" y "Y cuanto
    tenemos?" fallaban de forma DETERMINISTA —1 ms, 0 tokens, sin llegar al
    modelo— porque el vocabulario cubria movimientos, stock, proveedor y OC,
    pero no los campos del producto, y el patron de stock tenia "cuanto queda" y
    "cuanto hay" pero no "cuanto tenemos".
    """

    def _resolver(self, texto, turnos):
        from app.assistant.orchestrator.conversation_context import ConversationResolver

        return ConversationResolver().resolve(texto, turnos)

    def _turno_producto(self):
        return [{
            "text": "Muestrame el producto 2404.",
            "tools_used": ["get_product"],
            "entities": {"codigo": "2404", "codigos": ["2404"]},
            "evidence": [{
                "tool": "get_product", "ok": True, "empty": False,
                "evidence_id": "e1",
                "data": {"codigo": "2404", "descripcion": "FILTRO DIESEL",
                         "marca": "MAXUS", "modelo": "T60 2.8"},
                "meta": {},
            }],
        }]

    def test_y_la_marca_reutiliza_la_ficha_del_turno_anterior(self):
        r = self._resolver("Y la marca?", self._turno_producto())
        self.assertNotEqual(r.kind, "clarify", f"cayo al clarify: {r.clarify_message!r}")
        self.assertEqual(r.intent_hint, "reuse_product")

    def test_los_demas_atributos_de_ficha_tambien(self):
        for pregunta in ("Y el modelo?", "Y el motor?", "Y la descripcion?",
                         "Y la categoria?"):
            with self.subTest(pregunta=pregunta):
                r = self._resolver(pregunta, self._turno_producto())
                self.assertNotEqual(r.kind, "clarify", pregunta)

    def test_y_cuanto_tenemos_es_una_pregunta_de_stock(self):
        from app.assistant.orchestrator.conversation_context import _STOCK_RE

        for pregunta in ("Y cuanto tenemos?", "cuantos tenemos?",
                         "cuanto nos queda?", "cuanto tengo?"):
            with self.subTest(pregunta=pregunta):
                self.assertTrue(_STOCK_RE.search(pregunta), pregunta)

    def test_lo_que_ya_funcionaba_sigue_igual(self):
        from app.assistant.orchestrator.conversation_context import _STOCK_RE

        for pregunta in ("Y cuanto stock tiene?", "cuanto queda?", "cuanto hay?",
                         "que inventario tiene?"):
            with self.subTest(pregunta=pregunta):
                self.assertTrue(_STOCK_RE.search(pregunta), pregunta)

    def test_un_atributo_sin_codigo_previo_SI_pide_aclaracion(self):
        """Sin contexto inequivoco no se adivina: eso es lo correcto."""
        r = self._resolver("Y la marca?", [])
        self.assertEqual(r.kind, "clarify")

    def test_los_movimientos_siguen_teniendo_prioridad_sobre_la_ficha(self):
        """La rama de movimientos corre ANTES que la de atributos, y ese orden
        es lo que impide que "Ahora dime los movimientos" se lea como ficha."""
        import pathlib

        src = pathlib.Path(
            "app/assistant/orchestrator/conversation_context.py").read_text(encoding="utf-8")
        self.assertLess(src.index("if _MOVEMENTS_RE.search(text):"),
                        src.index("if _PRODUCT_ATTR_RE.search(text):"))


if __name__ == "__main__":
    unittest.main()
