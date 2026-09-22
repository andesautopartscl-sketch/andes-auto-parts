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


if __name__ == "__main__":
    unittest.main()
