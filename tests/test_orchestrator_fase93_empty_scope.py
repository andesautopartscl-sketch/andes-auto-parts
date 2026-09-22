"""FASE 9.3 — un vacio sin alcance no se puede leer.

LO QUE SE MIDIO

Con PERIOD encendido, V02 ("ventas de enero a marzo de 2026") resuelve la
ventana a 2026-01-01..2026-03-31 y la ventana LLEGA a la tool en 8 de 8
corridas: `get_sales` devuelve cero, que es el hecho correcto — el unico
documento del ERP es del 2026-04-07. Con PERIOD apagado la misma pregunta salia
SIN filtro y devolvia los documentos de abril presentados como el trimestre.

Pero cuando el verificador rechazaba la respuesta del agente y el composer tomaba
el relevo, lo que se publicaba era:

    `get_sales` no devolvio resultados.

y nada mas. El lector no puede distinguir "no hubo ventas en el trimestre" de
"la consulta no filtro nada". El alcance YA estaba en la evidencia
(`data["periodo"]`, que `sales.py` escribe siempre, tambien con count=0); nadie
lo leia.

LO QUE ESTO FIJA

Que el vacio declare su ventana cuando la evidencia la trae, y que NO invente
ninguna cuando no la trae. El silencio es la respuesta correcta ahi: publicar una
fecha que nadie midio seria peor que el silencio que esto corrige.

LO QUE ESTO NO ARREGLA, MEDIDO

El score de V02 no cambia por esto. `expect_fallback: false` hace que el scorer
falle el caso por HABER caido al composer, con independencia de lo que el
composer diga — se comprobo re-puntuando la misma corrida con ambos textos y los
motivos salen identicos. La causa del 5/8 esta en el verificador, aguas arriba.
Esta prueba cubre la calidad de la respuesta publicada, no la puntuacion.
"""
from __future__ import annotations

import unittest

from app.assistant.orchestrator.composer import compose_answer


def _evidencia(data, *, tool="get_sales", empty=True, ok=True):
    """Envoltorio con la forma EXACTA que produce normalize_tool_result.

    Importa que `empty` venga a True con `data` completo: el normalizador NO
    vacia `data` al marcar empty, y una prueba que construya empty=False para
    una lista vacia estaria probando un estado que el pipeline nunca genera.
    Ese error concreto dejo dos ramas muertas en produccion durante 8.8.
    """
    return [{"tool": tool, "ok": ok, "empty": empty, "classification": "INTERNAL",
             "data": data, "meta": {}}]


VENTANA = {"desde": "2026-01-01", "hasta": "2026-03-31"}


class PeriodoResueltoYCeroResultadosTests(unittest.TestCase):
    """El caso que motivo la unidad."""

    def test_el_vacio_declara_la_ventana_resuelta(self):
        r = compose_answer(plan={}, evidence=_evidencia(
            {"count": 0, "items": [], "periodo": VENTANA}))
        self.assertIn("no devolvió resultados", r["reply"])
        self.assertIn("2026-01-01", r["reply"])
        self.assertIn("2026-03-31", r["reply"])
        self.assertTrue(r["grounded"])

    def test_la_ventana_no_sustituye_al_hecho_de_que_no_hay_filas(self):
        """Decir el periodo no puede hacer parecer que hubo datos."""
        r = compose_answer(plan={}, evidence=_evidencia(
            {"count": 0, "items": [], "periodo": VENTANA}))
        self.assertIn("`get_sales`", r["reply"])
        self.assertIn("no devolvió resultados", r["reply"])

    def test_el_alcance_sobrevive_a_un_vacio_explicado(self):
        """Si una tool declara `not_found`, el alcance no puede desaparecer.

        No hay hoy ninguna que haga las dos cosas. Se fija igual porque la rama
        que se come al formateador es exactamente la clase de defecto que costo
        8.8, y ahi tampoco habia quien la ejerciera.
        """
        r = compose_answer(plan={}, evidence=_evidencia(
            {"count": 0, "items": [], "periodo": VENTANA, "not_found": True}))
        self.assertIn("2026-01-01", r["reply"])


class PeriodoResueltoYConResultadosTests(unittest.TestCase):
    """La rama no-vacia ya nombraba la ventana. Esto impide que se pierda."""

    def test_con_filas_la_ventana_se_sigue_declarando(self):
        r = compose_answer(plan={}, evidence=_evidencia(
            {"count": 1, "unidades": 3, "documentos": 1, "periodo": VENTANA,
             "items": [{"fecha": "2026-02-10", "numero": "FA-0002",
                        "codigo": "2417", "cantidad": 3}]},
            empty=False))
        self.assertIn("Periodo: 2026-01-01 a 2026-03-31.", r["reply"])
        self.assertIn("3 unidad(es)", r["reply"])

    def test_la_ventana_aparece_una_sola_vez(self):
        """Con filas, el formateador de la tool ya la dice: no se duplica."""
        r = compose_answer(plan={}, evidence=_evidencia(
            {"count": 1, "unidades": 3, "documentos": 1, "periodo": VENTANA,
             "items": [{"fecha": "2026-02-10", "numero": "FA-0002",
                        "codigo": "2417", "cantidad": 3}]},
            empty=False))
        self.assertEqual(r["reply"].count("Periodo:"), 1)


class ConsultaSinPeriodoTests(unittest.TestCase):

    def test_sin_filtro_declarado_se_dice_que_cubre_todo_el_historial(self):
        """desde=null y hasta=null significa "sin filtro", y hay que decirlo:
        callarlo es lo que dejo pasar la respuesta de abril como si fuera Q1."""
        r = compose_answer(plan={}, evidence=_evidencia(
            {"count": 0, "items": [], "periodo": {"desde": None, "hasta": None}}))
        self.assertIn("sin filtro de fecha", r["reply"])
        self.assertIn("todo el historial", r["reply"])

    def test_sin_campo_periodo_no_se_dice_nada_del_alcance(self):
        """Una tool que no declara periodo no tiene alcance que publicar."""
        r = compose_answer(plan={}, evidence=_evidencia({"count": 0, "items": []}))
        self.assertNotIn("Periodo", r["reply"])
        self.assertIn("no devolvió resultados", r["reply"])

    def test_una_tool_sin_vocabulario_de_periodo_no_gana_uno(self):
        r = compose_answer(plan={}, evidence=_evidencia(
            {"count": 0, "items": []}, tool="get_inventory"))
        self.assertNotIn("Periodo", r["reply"])


class NingunaFechaInventadaTests(unittest.TestCase):
    """El riesgo de este cambio es publicar una fecha que nadie midio."""

    def test_no_aparece_ninguna_fecha_cuando_la_evidencia_no_trae_ninguna(self):
        r = compose_answer(plan={}, evidence=_evidencia({"count": 0, "items": []}))
        import re
        self.assertIsNone(re.search(r"\d{4}-\d{2}-\d{2}", r["reply"]),
                          f"fecha inventada en: {r['reply']!r}")

    def test_un_periodo_malformado_no_produce_fechas(self):
        for basura in ("enero a marzo", 2026, ["2026-01-01"], None):
            with self.subTest(basura=basura):
                r = compose_answer(plan={}, evidence=_evidencia(
                    {"count": 0, "items": [], "periodo": basura}))
                import re
                self.assertIsNone(re.search(r"\d{4}-\d{2}-\d{2}", r["reply"]))

    def test_una_ventana_a_medias_se_declara_a_medias(self):
        """Media ventana es un hecho incomodo, no una licencia para completarla."""
        r = compose_answer(plan={}, evidence=_evidencia(
            {"count": 0, "items": [], "periodo": {"desde": "2026-01-01", "hasta": None}}))
        self.assertIn("2026-01-01", r["reply"])
        self.assertNotIn("2026-03-31", r["reply"])
        self.assertIn("no disponible", r["reply"])


class TrazabilidadConLaEvidenciaTests(unittest.TestCase):

    def test_la_ventana_publicada_es_LA_de_la_evidencia_y_no_otra(self):
        otra = {"desde": "2025-07-01", "hasta": "2025-09-30"}
        r = compose_answer(plan={}, evidence=_evidencia(
            {"count": 0, "items": [], "periodo": otra}))
        self.assertIn("2025-07-01", r["reply"])
        self.assertIn("2025-09-30", r["reply"])
        self.assertNotIn("2026", r["reply"])

    def test_la_respuesta_sigue_declarandose_grounded(self):
        r = compose_answer(plan={}, evidence=_evidencia(
            {"count": 0, "items": [], "periodo": VENTANA}))
        self.assertTrue(r["grounded"])

    def test_el_chequeo_de_grounding_no_confunde_la_fecha_con_un_codigo(self):
        """assert_reply_grounded marca tokens con pinta de codigo de producto.
        Una fecha ISO tiene digitos y guiones: si la tomara por codigo, este
        arreglo se anularia solo reemplazando la respuesta entera."""
        from app.assistant.orchestrator.composer import assert_reply_grounded

        ev = _evidencia({"count": 0, "items": [], "periodo": VENTANA})
        r = compose_answer(plan={}, evidence=ev)
        assert_reply_grounded(r["reply"], ev)  # no debe levantar
        self.assertNotIn("Solo puedo reportar valores presentes", r["reply"])

    def test_el_nombre_de_la_tool_se_conserva(self):
        r = compose_answer(plan={}, evidence=_evidencia(
            {"count": 0, "items": [], "periodo": VENTANA}, tool="get_orders"))
        self.assertIn("`get_orders`", r["reply"])
        self.assertIn("2026-01-01", r["reply"])

    def test_varias_tools_declaran_cada_una_su_propio_alcance(self):
        ev = (_evidencia({"count": 0, "items": [], "periodo": VENTANA})
              + _evidencia({"count": 0, "items": []}, tool="get_inventory"))
        r = compose_answer(plan={}, evidence=ev)
        self.assertEqual(r["reply"].count("Periodo:"), 1)
        self.assertIn("`get_inventory`", r["reply"])


if __name__ == "__main__":
    unittest.main()
