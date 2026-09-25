from __future__ import annotations

import unittest

from app.oc_clientes.ocr import (
    _extract_items,
    _extract_totales,
    _item_table_zone,
    _reject_item_line_as_documento_monto,
)


OC_74899_DOLLAR_ROWS = """
Orden de Compra N° 74899
Señores Andes Auto Parts
Atención Sr. ALBERT CASTILLO
Fecha O/C 21/09/2026
Fecha Entrega 21/09/2026
Rut 78074288-7
Dirección La Concepción N°81, Oficina 214 Providencia Santiago
Despachar A Maule 1033 Santiago Centro Santiago
Item Descripción Moneda Unidad Cantidad Precio Unitario Descto. Total
Forma de Pago
1 CYTIGOP74100 $ 1 140,336 140,336
PARACHOQUE TRASERO SUP
SAN
2 CYTIGOP63513 $ 1 116,806 116,806
FAROL INTERIOR TRASERO LH
SAN
3 CYTIGOP63502 $ 1 116,806 116,806
FAROL EXTERIOR TRASERO RH
SAN
4 CYTIGOP74012 $ 1 116,806 116,806
PARACHOQUE DELANT INFERIOR
SAN
Facturar a Comercial DV SPA RUT :76518007-4
Presentar Factura en
Maule 1033 Santiago Centro Santiago
Neto $ 490,754
IVA $ 93,243
Total $ 583,997
"""

OC_74899_FORMA_PAGO_MID_TABLE = """
Orden de Compra N° 74899
Señores Andes Auto Parts
Fecha O/C 21/09/2026
Rut 78074288-7
Item Descripción Moneda Unidad Cantidad Precio Unitario Descto. Total
1 CYTIGOP74100 $ 1 140,336 140,336
PARACHOQUE TRASERO SUP
SAN
Forma de Pago
2 CYTIGOP63513 $ 1 116,806 116,806
FAROL INTERIOR TRASERO LH
SAN
3 CYTIGOP63502 $ 1 116,806 116,806
FAROL EXTERIOR TRASERO RH
SAN
4 CYTIGOP74012 $ 1 116,806 116,806
PARACHOQUE DELANT INFERIOR
SAN
Facturar a Comercial DV SPA RUT :76518007-4
Neto $ 490,754
"""

OC_74899_COLUMNAR = """
Orden de Compra N° 74899
Señores Andes Auto Parts
Item Descripción Moneda Unidad Cantidad Precio Unitario Descto. Total
1
CYTIGOP74100
PARACHOQUE TRASERO SUP
SAN
2
CYTIGOP63513
FAROL INTERIOR TRASERO LH
SAN
3
CYTIGOP63502
FAROL EXTERIOR TRASERO RH
SAN
4
CYTIGOP74012
PARACHOQUE DELANT INFERIOR
SAN
1
1
1
1
140,336
140,336
116,806
116,806
116,806
116,806
116,806
116,806
Facturar a Comercial DV SPA RUT :76518007-4
Neto $
490,754
"""

OC_74899_HEADER_TOTAL = """
Item Descripción
Moneda
Unidad
Cantidad
Precio Unitario
Descto.
Total
1 CYTIGOP74100
PARACHOQUE TRASERO SUP
SAN
2 CYTIGOP63513
FAROL INTERIOR TRASERO LH
SAN
3 CYTIGOP63502
FAROL EXTERIOR TRASERO RH
SAN
4 CYTIGOP74012
PARACHOQUE DELANT INFERIOR
SAN
140,336
140,336
116,806
116,806
116,806
116,806
116,806
116,806
Facturar a Comercial DV SPA
Neto $ 490,754
"""

OC_74899_CODES_CLUSTERED = """
Orden de Compra N° 74899
Señores Andes Auto Parts
Item Descripción Moneda Unidad Cantidad Precio Unitario Descto. Total
1 CYTIGOP74100
PARACHOQUE TRASERO SUP
SAN
2 CYTIGOP63513
3 CYTIGOP63502
4 CYTIGOP74012
PARACHOQUE DELANT INFERIOR
SAN
FAROL INTERIOR TRASERO LH
FAROL EXTERIOR TRASERO RH
140,336
140,336
116,806
116,806
116,806
Facturar a Comercial DV SPA RUT :76518007-4
Neto $
116,806
"""


def _codes(items):
    return [it.get("codigo_producto") for it in items]


def _prices(items):
    return [float(it.get("precio_unitario") or 0) for it in items]


class OcClienteOcrItemsTests(unittest.TestCase):
    def test_filas_con_peso_y_forma_de_pago_en_cabecera(self):
        items = _extract_items(OC_74899_DOLLAR_ROWS)
        self.assertEqual(
            _codes(items),
            ["CYTIGOP74100", "CYTIGOP63513", "CYTIGOP63502", "CYTIGOP74012"],
        )
        self.assertIn("PARACHOQUE TRASERO SUP", items[0]["descripcion"])
        self.assertIn("FAROL INTERIOR TRASERO LH", items[1]["descripcion"])
        self.assertIn("FAROL EXTERIOR TRASERO RH", items[2]["descripcion"])
        self.assertIn("PARACHOQUE DELANT INFERIOR", items[3]["descripcion"])
        self.assertEqual(_prices(items), [140336.0, 116806.0, 116806.0, 116806.0])
        self.assertIn("CYTIGOP74012", _item_table_zone(OC_74899_DOLLAR_ROWS))

    def test_forma_de_pago_en_medio_no_borra_filas(self):
        items = _extract_items(OC_74899_FORMA_PAGO_MID_TABLE)
        self.assertEqual(len(items), 4)
        self.assertEqual(
            _codes(items),
            ["CYTIGOP74100", "CYTIGOP63513", "CYTIGOP63502", "CYTIGOP74012"],
        )
        self.assertTrue(all(it["descripcion"] for it in items))
        self.assertEqual(_prices(items), [140336.0, 116806.0, 116806.0, 116806.0])

    def test_ocr_columnar_cuatro_items(self):
        items = _extract_items(OC_74899_COLUMNAR)
        self.assertEqual(len(items), 4)
        self.assertEqual(
            _codes(items),
            ["CYTIGOP74100", "CYTIGOP63513", "CYTIGOP63502", "CYTIGOP74012"],
        )
        self.assertEqual(_prices(items), [140336.0, 116806.0, 116806.0, 116806.0])

    def test_total_de_cabecera_no_es_neto_del_documento(self):
        items = _extract_items(OC_74899_HEADER_TOTAL)
        self.assertEqual(len(items), 4)
        self.assertTrue(all(_codes(items)))
        totales = _extract_totales(OC_74899_HEADER_TOTAL)
        self.assertEqual(totales.get("neto"), 490754.0)
        self.assertNotEqual(totales.get("total"), 140336.0)
        self.assertNotEqual(totales.get("neto"), 116806.0)

    def test_un_solo_item_mantiene_codigo_y_neto(self):
        texto = """
Orden de Compra N° 100
Item Descripción Moneda Unidad Cantidad Precio Unitario Descto. Total
1 FILTROXX99 $ 1 50,000 50,000
FILTRO ACEITE
SAN
Facturar a ACME SPA
Neto $ 50,000
IVA $ 9,500
Total $ 59,500
"""
        items = _extract_items(texto)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["codigo_producto"], "FILTROXX99")
        self.assertEqual(items[0]["descripcion"], "FILTRO ACEITE")
        self.assertEqual(items[0]["precio_unitario"], 50000.0)
        totales = _extract_totales(texto)
        self.assertEqual(totales.get("neto"), 50000.0)
        self.assertEqual(totales.get("total"), 59500.0)

    def test_neto_pie_en_layout_con_peso(self):
        totales = _extract_totales(OC_74899_DOLLAR_ROWS)
        self.assertEqual(totales.get("neto"), 490754.0)
        self.assertEqual(totales.get("iva"), 93243.0)
        self.assertEqual(totales.get("total"), 583997.0)

    def test_codigos_juntos_pegan_descripciones_huerfanas(self):
        items = _extract_items(OC_74899_CODES_CLUSTERED)
        self.assertEqual(len(items), 4)
        self.assertEqual(
            _codes(items),
            ["CYTIGOP74100", "CYTIGOP63513", "CYTIGOP63502", "CYTIGOP74012"],
        )
        self.assertIn("PARACHOQUE TRASERO SUP", items[0]["descripcion"])
        self.assertIn("FAROL INTERIOR TRASERO LH", items[1]["descripcion"])
        self.assertIn("FAROL EXTERIOR TRASERO RH", items[2]["descripcion"])
        self.assertIn("PARACHOQUE DELANT INFERIOR", items[3]["descripcion"])
        self.assertEqual(_prices(items), [140336.0, 116806.0, 116806.0, 116806.0])
        neto = _extract_totales(OC_74899_CODES_CLUSTERED).get("neto")
        self.assertEqual(_reject_item_line_as_documento_monto(neto, items), None)



if __name__ == "__main__":
    unittest.main()
