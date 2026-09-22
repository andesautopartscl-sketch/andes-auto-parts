"""FASE 9.4 — el verificador castigaba al modelo por redactar como una persona.

LO QUE SE MIDIO

Con PERIOD ON, V02 ("ventas de enero a marzo de 2026") pasaba 7 de 16 corridas.
Las 7 eran exactamente aquellas en que el modelo escribio numeros de dia:

    "entre el 1 de enero y el 31 de marzo de 2026"   -> grounded
    "de enero a marzo de 2026"                       -> DESCARTADO
    "en el primer trimestre de 2026"                 -> DESCARTADO

Las tres formas que `mask_dates` reconocia exigian todas un numero de dia. Una
expresion de mes sin dia no era fecha para el verificador, asi que "2026"
sobrevivia como numero suelto, se comparaba contra las cifras de la evidencia
—que para un resultado vacio son {'0'}— y el claim caia. Sin claims,
`no_grounded_claim` mandaba el turno al composer, y `expect_fallback: false`
fallaba el caso. Una loteria sobre la redaccion.

LA REGLA DE SEGURIDAD

Un ano NUNCA se reconoce solo. Se midio antes que **55 codigos del catalogo caen
en 2000-2100**: cualquier regla que convierta un numero aislado de ese rango en
fecha rompe A01 y con el toda pregunta que cite un codigo. El ano solo cuenta
cuando va pegado a un nombre de mes o a "trimestre" — la misma logica de
conjuncion que explica A01.

Esta clase de prueba es la que protege esa regla. Media suite de aqui existe
para que la correccion no se pase de lista.
"""
from __future__ import annotations

import json
import unittest

from app.assistant.orchestrator.answer_verifier import (
    _claim_grounded,
    _collect_evidence_dates,
    _collect_evidence_numbers,
    mask_dates,
)

VENTANA = {"desde": "2026-01-01", "hasta": "2026-03-31"}


def _contexto(data=None):
    """Evidencia de V02: ventana Q1 2026, cero filas."""
    data = data if data is not None else {
        "count": 0, "items": [], "unidades": 0, "documentos": 0, "periodo": VENTANA}
    blob = json.dumps([{"data": data, "meta": {}, "tool": "get_sales"}],
                      ensure_ascii=False)
    fechas: set[str] = set()
    numeros: set[str] = set()
    _collect_evidence_dates(data, fechas)
    _collect_evidence_numbers(data, numeros)
    return blob, fechas, numeros


def _grounded(texto, data=None):
    blob, fechas, numeros = _contexto(data)
    return _claim_grounded(texto, blob, {"get_sales"}, numeros, fechas)


class LasDosExpresionesQueMotivaronLaUnidadTests(unittest.TestCase):

    def test_de_enero_a_marzo_de_2026(self):
        self.assertTrue(_grounded("No hubo ventas de enero a marzo de 2026."))

    def test_en_el_primer_trimestre_de_2026(self):
        self.assertTrue(_grounded("No hubo ventas en el primer trimestre de 2026."))

    def test_el_ano_ya_no_se_escapa_como_numero_suelto(self):
        """La causa exacta: 2026 llegaba a _claim_number_tokens."""
        from app.assistant.orchestrator.answer_verifier import _claim_number_tokens

        sin_fechas, _ = mask_dates("No hubo ventas de enero a marzo de 2026.")
        self.assertNotIn("2026", _claim_number_tokens(sin_fechas))

    def test_variantes_de_la_misma_idea(self):
        for frase in ("Sin ventas entre enero y marzo de 2026.",
                      "No hubo ventas en marzo del 2026.",
                      "Nada vendido en el primer trimestre del 2026.",
                      "Cero ventas en el primero trimestre de 2026."):
            with self.subTest(frase=frase):
                self.assertTrue(_grounded(frase), frase)

    def test_las_abreviaturas_ordinales_quedan_fuera_a_proposito(self):
        """"1ER" tiene 3 caracteres, una letra y un digito: `_CODE_RE` lo toma
        por codigo inventado y descarta el claim antes de mirar fechas. Ese
        detector es el nucleo anti-alucinacion y no se relaja por una
        abreviatura. Queda como estaba hoy, y esta prueba fija esa frontera."""
        self.assertFalse(_grounded("Cero ventas en el 1er trimestre de 2026."))


class LasFormasQueYaFuncionabanSiguenFuncionandoTests(unittest.TestCase):
    """Ampliar la granularidad hacia arriba no puede aflojar nada de abajo."""

    def test_iso(self):
        self.assertTrue(_grounded("No se registraron ventas entre 2026-01-01 y 2026-03-31."))

    def test_dia_de_mes_de_ano(self):
        self.assertTrue(_grounded("No hubo ventas entre el 1 de enero y el 31 de marzo de 2026."))

    def test_dia_barra_mes_barra_ano(self):
        self.assertTrue(_grounded("Las ventas del 01/01/2026 al 31/03/2026 fueron 0."))

    def test_dia_y_mes_sin_ano_sigue_siendo_parcial(self):
        _, encontradas = mask_dates("el 1 de enero")
        self.assertEqual(encontradas, ["??-01-01"])

    def test_una_fecha_exacta_no_la_funda_una_referencia_de_mes(self):
        """La asimetria es deliberada: "marzo de 2026" lo funda 2026-03-31,
        pero 2026-03-31 NO lo funda "marzo de 2026"."""
        self.assertFalse(_grounded(
            "La venta fue el 2026-03-15.",
            {"count": 0, "items": [], "periodo": {"desde": None, "hasta": None},
             "etiqueta": "marzo de 2026"}))


class UnAnoAisladoNuncaEsUnaFechaTests(unittest.TestCase):
    """La regla que protege los 55 codigos del catalogo en 2000-2100."""

    def test_un_ano_solo_no_produce_fecha(self):
        for frase in ("En 2026 hubo 5 ventas.",
                      "El total de 2026 es 0.",
                      "2026",
                      "Entre 2024 y 2026 no hubo movimientos."):
            with self.subTest(frase=frase):
                _, encontradas = mask_dates(frase)
                self.assertEqual(encontradas, [], f"{frase!r} -> {encontradas}")

    def test_un_ano_solo_sigue_sin_estar_fundado(self):
        self.assertFalse(_grounded("En 2026 hubo 5 ventas."))

    def test_un_numero_del_rango_de_anos_no_se_enmascara_por_estar_cerca_de_un_mes(self):
        """Cercania no es adyacencia: hace falta "<mes> de <ano>"."""
        _, encontradas = mask_dates("En marzo se vendio el codigo 2026.")
        self.assertEqual(encontradas, [])

    def test_trimestre_sin_ano_no_inventa_uno(self):
        _, encontradas = mask_dates("No hubo ventas en el primer trimestre.")
        self.assertEqual(encontradas, [])


class CodigosDeProductoTests(unittest.TestCase):
    """Un codigo no puede volverse fecha, ni fundar una."""

    def test_codigos_que_parecen_anos(self):
        for frase in ("El codigo 2026 tiene 2 unidades.",
                      "Los codigos 2026 y 2404 existen.",
                      "Stock de 2417: 5 unidades.",
                      "Producto 2000 y producto 2100."):
            with self.subTest(frase=frase):
                _, encontradas = mask_dates(frase)
                self.assertEqual(encontradas, [], f"{frase!r} -> {encontradas}")

    def test_un_codigo_no_se_funda_por_existir_una_ventana(self):
        self.assertFalse(_grounded("El codigo 2026 tiene 2 unidades."))

    def test_un_codigo_presente_en_la_evidencia_si_se_funda(self):
        data = {"count": 1, "items": [{"codigo": "2417", "cantidad": 3}],
                "periodo": VENTANA}
        self.assertTrue(_grounded("El codigo 2417 tiene 3 unidades.", data))


class A01YLosNumerosQueParecenFechasTests(unittest.TestCase):
    """A01: "movimientos del 24/04" y el codigo 2404 en la misma frase.

    El hallazgo de 8.x es que el modelo lee 2404 como 24/04. Ese defecto vive en
    el planificador, no aqui — pero una regla de fechas demasiado avida en el
    verificador lo empeoraria, y esta prueba lo impide.
    """

    def test_la_frase_de_a01_no_gana_ninguna_fecha_nueva(self):
        _, encontradas = mask_dates("Movimientos del 24/04 para el producto 2404.")
        self.assertEqual(encontradas, [])

    def test_2404_no_se_lee_como_periodo(self):
        _, encontradas = mask_dates("Stock esperado del 2404 para dos meses.")
        self.assertEqual(encontradas, [])

    def test_los_numeros_de_las_fixtures_anti_alucinacion_siguen_intactos(self):
        """8888 y 9999 son los centinelas del scorer: si una regla de fechas los
        tocara, el detector de cifras inventadas dejaria de ver."""
        for frase in ("El codigo 8888 no existe.", "Cantidad 9999."):
            with self.subTest(frase=frase):
                sin_fechas, encontradas = mask_dates(frase)
                self.assertEqual(encontradas, [])
                self.assertIn(frase.split()[-1].rstrip("."), sin_fechas)


class ElReconocimientoNoEsAceptacionTests(unittest.TestCase):
    """Detectar una expresion no la da por buena: sigue teniendo que cuadrar."""

    def test_un_mes_fuera_de_la_ventana_no_esta_fundado(self):
        self.assertFalse(_grounded("Hubo ventas en febrero de 2026."))

    def test_un_trimestre_de_otro_ano_no_esta_fundado(self):
        self.assertFalse(_grounded("No hubo ventas en el primer trimestre de 2025."))

    def test_un_trimestre_que_si_cubre_la_evidencia_esta_fundado(self):
        self.assertTrue(_grounded("Nada en el primer trimestre de 2026."))

    def test_un_mes_del_medio_de_la_ventana_no_se_funda_solo_por_estar_dentro(self):
        """La evidencia tiene 2026-01-01 y 2026-03-31, ninguna de febrero.
        El verificador compara con las fechas que HAY, no con el intervalo."""
        _, encontradas = mask_dates("febrero de 2026")
        self.assertEqual(encontradas, ["2026-02-??"])
        self.assertFalse(_grounded("Sin ventas en febrero de 2026."))

    def test_una_fecha_invalida_no_se_degrada_a_mes(self):
        """"99 de julio de 2026" es una fecha invalida, no el mes de julio.
        Degradarla seria reinterpretar lo que el modelo quiso decir, y la
        frontera "una fecha invalida no es una fecha" ya estaba fijada en 8.1H.2.
        Esta regla no la mueve — y la suite existente lo detecto cuando si la
        movia."""
        for frase in ("99 de julio de 2026", "45 de enero de 2026"):
            with self.subTest(frase=frase):
                _, encontradas = mask_dates(frase)
                self.assertEqual(encontradas, [], f"{frase!r} -> {encontradas}")

    def test_un_dia_valido_si_produce_la_fecha_completa_y_no_el_mes(self):
        _, encontradas = mask_dates("9 de julio de 2026")
        self.assertEqual(encontradas, ["2026-07-09"])

    def test_un_ano_invalido_no_produce_canonico(self):
        for frase in ("enero de 1800", "marzo de 3200"):
            with self.subTest(frase=frase):
                _, encontradas = mask_dates(frase)
                self.assertEqual(encontradas, [])


class LaMismaFechaNoSeCuentaDosVecesTests(unittest.TestCase):
    """El orden de las reglas es parte de la correccion."""

    def test_dia_de_mes_de_ano_no_produce_ademas_una_referencia_de_mes(self):
        _, encontradas = mask_dates("el 31 de marzo de 2026")
        self.assertEqual(encontradas, ["2026-03-31"])

    def test_una_frase_con_las_dos_formas_produce_una_de_cada(self):
        _, encontradas = mask_dates("del 1 de enero a marzo de 2026")
        self.assertEqual(sorted(encontradas), ["2026-03-??", "??-01-01"])

    def test_el_texto_enmascarado_no_conserva_el_ano(self):
        sin_fechas, _ = mask_dates("de enero a marzo de 2026")
        self.assertNotIn("2026", sin_fechas)


if __name__ == "__main__":
    unittest.main()
