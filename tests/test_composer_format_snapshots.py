"""FASE 5.1 — AnswerComposer presentation snapshots (evidence-only)."""
from __future__ import annotations

import unittest

from app.assistant.orchestrator.composer import compose_answer


def _plan():
    return {"answer_style": "operational", "steps": []}


class ComposerFormatSnapshots(unittest.TestCase):
    def test_snapshot_stock_movements_sorted_and_separated(self):
        evidence = [
            {
                "ok": True,
                "empty": False,
                "tool": "get_stock_movements",
                "classification": "INTERNAL",
                "data": {
                    "codigo": "2404",
                    "count": 3,
                    "items": [
                        {
                            "fecha": "2026-05-12",
                            "tipo": "ingreso",
                            "cantidad": 1,
                            "bodega": "Bodega 1",
                            "marca": "BOSCH",
                        },
                        {
                            "fecha": "2026-07-31",
                            "tipo": "ajuste",
                            "cantidad": -1,
                            "bodega": "Bodega 1",
                            "marca": "BOSCH",
                            "usuario": "albertadmin",
                            "observacion": "Doc 302",
                        },
                        {
                            "fecha": "2026-07-31",
                            "tipo": "ingreso",
                            "cantidad": 2,
                            "bodega": "Bodega 1",
                            "marca": "BOSCH",
                        },
                    ],
                },
                "meta": {},
            }
        ]
        reply = compose_answer(plan=_plan(), evidence=evidence)["reply"]
        expected = (
            "Movimientos de 2404 — 3 registro(s):\n"
            "1. 2026-07-31 · ajuste\n"
            "   Cantidad: -1\n"
            "   Bodega: Bodega 1\n"
            "   Marca: BOSCH\n"
            "   Usuario: albertadmin\n"
            "   Observación: Doc 302\n"
            "\n"
            "2. 2026-07-31 · ingreso\n"
            "   Cantidad: 2\n"
            "   Bodega: Bodega 1\n"
            "   Marca: BOSCH\n"
            "\n"
            "3. 2026-05-12 · ingreso\n"
            "   Cantidad: 1\n"
            "   Bodega: Bodega 1\n"
            "   Marca: BOSCH"
        )
        self.assertEqual(reply, expected)
        # Does not invent fields absent from first/third rows
        self.assertEqual(reply.count("Usuario:"), 1)
        self.assertEqual(reply.count("Observación:"), 1)

    def test_snapshot_inventory(self):
        evidence = [
            {
                "ok": True,
                "empty": False,
                "tool": "get_inventory",
                "classification": "INTERNAL",
                "data": {
                    "codigo": "2404",
                    "total_stock": 2,
                    "items": [{"bodega": "Bodega 1", "stock": 2, "marca": "BOSCH"}],
                },
                "meta": {},
            }
        ]
        reply = compose_answer(plan=_plan(), evidence=evidence)["reply"]
        self.assertEqual(
            reply,
            "Stock de 2404\n"
            "Total: 2\n"
            "Por bodega:\n"
            "• Bodega 1: 2 (BOSCH)",
        )

    def test_snapshot_product(self):
        evidence = [
            {
                "ok": True,
                "empty": False,
                "tool": "get_product",
                "classification": "INTERNAL",
                "data": {
                    "codigo": "2404",
                    "descripcion": "FILTRO DIESEL",
                    "marca": "BOSCH",
                    "modelo": "T60",
                },
                "meta": {},
            }
        ]
        reply = compose_answer(plan=_plan(), evidence=evidence)["reply"]
        self.assertEqual(
            reply,
            "Producto 2404\n"
            "Descripción: FILTRO DIESEL\n"
            "Marca: BOSCH\n"
            "Modelo: T60",
        )

    def test_snapshot_customer(self):
        evidence = [
            {
                "ok": True,
                "empty": False,
                "tool": "get_customer",
                "classification": "CONFIDENTIAL",
                "data": {
                    "count": 1,
                    "items": [{"nombre": "ALBERT CASTILLO", "rut": "1-9"}],
                },
                "meta": {},
            }
        ]
        reply = compose_answer(plan=_plan(), evidence=evidence)["reply"]
        self.assertEqual(
            reply,
            "Clientes — 1 resultado(s):\n"
            "• ALBERT CASTILLO — RUT 1-9\n"
            "Nota: email, teléfono y dirección no se exponen en esta tool.",
        )
        self.assertNotIn("@", reply)

    def test_snapshot_supplier(self):
        evidence = [
            {
                "ok": True,
                "empty": False,
                "tool": "get_supplier",
                "classification": "CONFIDENTIAL",
                "data": {
                    "count": 1,
                    "items": [
                        {
                            "nombre": "ANDES SPA",
                            "empresa": "ANDES AUTO PARTS LTDA",
                            "rut": "76.000.000-0",
                            "ciudad": "SANTIAGO",
                        }
                    ],
                },
                "meta": {},
            }
        ]
        reply = compose_answer(plan=_plan(), evidence=evidence)["reply"]
        self.assertEqual(
            reply,
            "Proveedores — 1 resultado(s):\n"
            "• ANDES SPA\n"
            "  Empresa: ANDES AUTO PARTS LTDA\n"
            "  RUT: 76.000.000-0\n"
            "  Ciudad: SANTIAGO\n"
            "Nota: email, teléfono y dirección no se exponen en esta tool.",
        )

    def test_snapshot_purchase_orders(self):
        evidence = [
            {
                "ok": True,
                "empty": False,
                "tool": "get_purchase_orders",
                "classification": "INTERNAL",
                "data": {
                    "count": 1,
                    "items": [
                        {
                            "numero": "OC-100",
                            "fecha": "2026-09-01",
                            "estado": "abierta",
                            "proveedor": "ANDES",
                        }
                    ],
                },
                "meta": {},
            }
        ]
        reply = compose_answer(plan=_plan(), evidence=evidence)["reply"]
        self.assertEqual(
            reply,
            "Órdenes de compra — 1 documento(s):\n"
            "• OC-100 — 2026-09-01 · abierta · ANDES",
        )

    def test_snapshot_dashboard_kpis_finance_null(self):
        evidence = [
            {
                "ok": True,
                "empty": False,
                "tool": "get_dashboard_kpis",
                "classification": "CONFIDENTIAL",
                "finance_redacted": True,
                "stock_omitted": True,
                "data": {
                    "docs_periodo": 2,
                    "ventas_periodo": None,
                    "stock_critico": None,
                },
                "meta": {
                    "periodo": "7d",
                    "fecha_desde": "2026-09-08",
                    "fecha_hasta": "2026-09-14",
                },
            }
        ]
        reply = compose_answer(plan={"answer_style": "financial"}, evidence=evidence)["reply"]
        self.assertIn("KPIs — período 7d", reply)
        self.assertIn("Documentos: 2", reply)
        self.assertIn("Ventas: no disponible por permisos", reply)
        self.assertNotIn("Ventas: 0", reply)
        self.assertIn("Stock crítico no incluido", reply)

    def test_movements_do_not_invent_usuario(self):
        evidence = [
            {
                "ok": True,
                "empty": False,
                "tool": "get_stock_movements",
                "classification": "INTERNAL",
                "data": {
                    "codigo": "2404",
                    "count": 1,
                    "items": [
                        {
                            "fecha": "2026-07-31",
                            "tipo": "ingreso",
                            "cantidad": 2,
                            "bodega": "Bodega 1",
                        }
                    ],
                },
                "meta": {},
            }
        ]
        reply = compose_answer(plan=_plan(), evidence=evidence)["reply"]
        self.assertNotIn("Usuario", reply)
        self.assertNotIn("Observación", reply)
        self.assertIn("Cantidad: 2", reply)


if __name__ == "__main__":
    unittest.main()
