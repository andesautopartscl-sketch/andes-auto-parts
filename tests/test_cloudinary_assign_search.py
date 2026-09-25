from __future__ import annotations

import unittest

from app.utils.cloudinary_product_import import assign_search_tokens, search_productos_for_assign


class AssignSearchTokenTests(unittest.TestCase):
    def test_parte_palabras_por_espacio_y_coma(self):
        self.assertEqual(assign_search_tokens("BUJIA V80"), ["BUJIA", "V80"])
        self.assertEqual(assign_search_tokens("  bujia, v80 ; faw "), ["bujia", "v80", "faw"])

    def test_vacio(self):
        self.assertEqual(assign_search_tokens(""), [])
        self.assertEqual(assign_search_tokens("   "), [])


class AssignSearchCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app import create_app
        from app.extensions import db

        cls.app = create_app()
        cls.ctx = cls.app.app_context()
        cls.ctx.push()
        cls.sess = db.session

    @classmethod
    def tearDownClass(cls):
        cls.ctx.pop()

    def test_bujia_v80_encuentra_por_descripcion_y_modelo(self):
        items = search_productos_for_assign(self.sess, "BUJIA V80", limit=30)
        codes = {it["codigo"] for it in items}
        self.assertTrue(
            items,
            "«BUJIA V80» no debería quedar vacío: la bujía está en descripción y V80 en modelo",
        )
        self.assertTrue(
            codes & {"W6556RC", "MAX151ORG", "MAX151RC", "TM3008RC", "FOT029RC"},
            f"esperaba bujías de V80, obtuve {sorted(codes)}",
        )
        self.assertTrue(
            any((it.get("modelo") or "") and "V80" in it["modelo"].upper() for it in items),
            "los resultados deben incluir modelo (p. ej. V80)",
        )
        self.assertTrue(
            all(isinstance(it.get("stock"), int) and it["stock"] >= 0 for it in items),
            "cada resultado debe traer stock total (variantes) para elegir código",
        )

    def test_vg4056rc_stock_usa_variantes(self):
        from app.utils.cloudinary_product_import import _stock_map_for_codigos

        esperado = _stock_map_for_codigos(self.sess, ["VG4056RC"]).get("VG4056RC", 0)
        items = search_productos_for_assign(self.sess, "VG4056RC", limit=10)
        self.assertTrue(items, "VG4056RC debería aparecer en la búsqueda por código interno")
        hit = next((it for it in items if (it.get("codigo") or "").upper() == "VG4056RC"), None)
        self.assertIsNotNone(hit)
        self.assertEqual(
            hit["stock"],
            esperado,
            "el stock del modal debe ser la suma de productos_variantes_stock, no 0 si hay unidades",
        )


if __name__ == "__main__":
    unittest.main()
