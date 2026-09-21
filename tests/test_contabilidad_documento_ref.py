from __future__ import annotations

import unittest
from datetime import datetime
from types import SimpleNamespace

from app.contabilidad.routes import (
    _documento_ref_key,
    _emisor_nombre_key,
    _emisores_mismo,
    _mensaje_movimiento_documento_ref_duplicado,
)


class DocumentoRefKeyTests(unittest.TestCase):
    def test_normaliza_espacios_y_mayusculas(self):
        self.assertEqual(_documento_ref_key("  11870  "), "11870")
        self.assertEqual(_documento_ref_key("f-11870"), "F-11870")

    def test_vacio(self):
        self.assertEqual(_documento_ref_key(""), "")
        self.assertEqual(_documento_ref_key(None), "")

    def test_razon_social_colapsa_espacios(self):
        self.assertEqual(_emisor_nombre_key("  RG   PUBLICIDAD  SPA "), "RG PUBLICIDAD SPA")


class EmisorMatchTests(unittest.TestCase):
    def test_mismo_rut_distinto_formato(self):
        self.assertTrue(
            _emisores_mismo("77.185.197-5", "RG PUBLICIDAD", "771851975", "OTRO NOMBRE")
        )

    def test_rut_distinto_no_coincide(self):
        self.assertFalse(
            _emisores_mismo("77.185.197-5", "RG PUBLICIDAD", "76.000.000-1", "RG PUBLICIDAD")
        )

    def test_sin_rut_usa_razon_social(self):
        self.assertTrue(
            _emisores_mismo("", "RG  PUBLICIDAD SPA", "", "rg publicidad spa")
        )
        self.assertFalse(
            _emisores_mismo("", "RG PUBLICIDAD SPA", "", "OTRA EMPRESA")
        )

    def test_sin_emisor_en_el_formulario_alerta_cualquier_match(self):
        self.assertTrue(_emisores_mismo("", "", "77.185.197-5", "RG PUBLICIDAD"))


class MensajeDuplicadoTests(unittest.TestCase):
    def test_incluye_numero_emisor_y_fecha(self):
        mov = SimpleNamespace(
            id=42,
            documento_ref="11870",
            emisor_nombre="RG PUBLICIDAD SPA",
            fecha=datetime(2026, 9, 16, 14, 34),
        )
        msg = _mensaje_movimiento_documento_ref_duplicado(mov)
        self.assertIn("11870", msg)
        self.assertIn("RG PUBLICIDAD SPA", msg)
        self.assertIn("#42", msg)
        self.assertIn("16-09-2026", msg)
        self.assertIn("ya está ingresado", msg)


if __name__ == "__main__":
    unittest.main()
