from datetime import datetime
import unittest

from app.oc_clientes.services import aging_bucket, periodo_cobros


class OcResumenKpisTests(unittest.TestCase):
    def test_aging_bucket_ranges(self):
        self.assertEqual(aging_bucket(0), "0-30")
        self.assertEqual(aging_bucket(30), "0-30")
        self.assertEqual(aging_bucket(31), "31-60")
        self.assertEqual(aging_bucket(60), "31-60")
        self.assertEqual(aging_bucket(61), "61-90")
        self.assertEqual(aging_bucket(90), "61-90")
        self.assertEqual(aging_bucket(91), "90+")
        self.assertEqual(aging_bucket(-5), "0-30")

    def test_periodo_cobros_mes_septiembre_2026(self):
        p = periodo_cobros(2026, 9)
        self.assertEqual(p["year"], 2026)
        self.assertEqual(p["month"], 9)
        self.assertFalse(p["anio_completo"])
        self.assertEqual(p["label"], "Septiembre 2026")
        self.assertEqual(p["inicio"], datetime(2026, 9, 1))
        self.assertEqual(p["fin"], datetime(2026, 9, 30, 23, 59, 59))

    def test_periodo_cobros_anio_completo_por_month_cero(self):
        p = periodo_cobros(2026, 0)
        self.assertEqual(p["month"], 0)
        self.assertTrue(p["anio_completo"])
        self.assertEqual(p["label"], "2026")
        self.assertEqual(p["inicio"], datetime(2026, 1, 1))
        self.assertEqual(p["fin"], datetime(2026, 12, 31, 23, 59, 59))

    def test_periodo_cobros_anio_completo_flag(self):
        p = periodo_cobros(2025, 4, anio_completo=True)
        self.assertEqual(p["month"], 0)
        self.assertEqual(p["label"], "2025")
        self.assertEqual(p["inicio"].month, 1)
        self.assertEqual(p["fin"].month, 12)


if __name__ == "__main__":
    unittest.main()
