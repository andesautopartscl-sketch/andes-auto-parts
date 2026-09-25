from __future__ import annotations

import unittest

from app.import_excel import (
    OEM_SHIFT_MARCA_BLOCK,
    detect_column_shift,
    _overlay_missing_columns,
)
import pandas as pd


def _sku(marca: str, oem: str, homologados: str = "", descripcion: str = "VALVULA") -> dict:
    return {
        "marca": marca,
        "oem": oem,
        "homologados": homologados,
        "descripcion": descripcion,
    }


class DetectColumnShiftTests(unittest.TestCase):
    def test_swap_entre_marcas_bloquea(self):
        existing = {}
        incoming = {}
        for i in range(OEM_SHIFT_MARCA_BLOCK):
            baic = f"BAIC{i:03d}"
            jmc = f"JMC{i:03d}"
            existing[baic] = _sku("BAIC", f"OEM-BAIC-{i}")
            existing[jmc] = _sku("JMC", f"OEM-JMC-{i}", "VIGUS PLUS 2.4,CONQUER 2.4")
            incoming[baic] = _sku("BAIC", f"OEM-JMC-{i}", "VIGUS PLUS 2.4,CONQUER 2.4")
            incoming[jmc] = _sku("JMC", f"OEM-BAIC-{i}")

        report = detect_column_shift(existing, incoming)
        self.assertTrue(report["blocked"])
        self.assertGreaterEqual(report["oem_marca_mismatch"], OEM_SHIFT_MARCA_BLOCK)
        self.assertTrue(report["samples"])
        self.assertEqual(report["samples"][0]["codigo"], "BAIC000")
        self.assertEqual(report["samples"][0]["pertenecia_a"], "JMC000")

    def test_cambio_oem_nuevo_no_bloquea(self):
        existing = {"BAIX72002": _sku("BAIC", "K00530031")}
        incoming = {"BAIX72002": _sku("BAIC", "K00530031-NEW")}
        report = detect_column_shift(existing, incoming)
        self.assertFalse(report["blocked"])
        self.assertEqual(report["oem_shifts"], 0)

    def test_pocos_intercambios_no_bloquea(self):
        existing = {
            "A1": _sku("BAIC", "OEM-A"),
            "B1": _sku("JMC", "OEM-B"),
        }
        incoming = {
            "A1": _sku("BAIC", "OEM-B"),
            "B1": _sku("JMC", "OEM-A"),
        }
        report = detect_column_shift(existing, incoming)
        self.assertFalse(report["blocked"])
        self.assertEqual(report["oem_shifts"], 2)
        self.assertEqual(report["oem_marca_mismatch"], 2)

    def test_overlay_conserva_precio_si_excel_no_lo_trae(self):
        df = pd.DataFrame(
            [{"CODIGO": "BAIX72002", "CODIGO OEM": "K00530031", "P_PUBLICO": None}]
        )
        existing = {
            "BAIX72002": {"CODIGO": "BAIX72002", "CODIGO OEM": "HP2", "P_PUBLICO": 19990}
        }
        out = _overlay_missing_columns(
            df,
            ["CODIGO", "CODIGO OEM", "P_PUBLICO"],
            {"CODIGO", "CODIGO OEM"},
            existing,
        )
        self.assertEqual(out.iloc[0]["CODIGO OEM"], "K00530031")
        self.assertEqual(out.iloc[0]["P_PUBLICO"], 19990)


if __name__ == "__main__":
    unittest.main()
