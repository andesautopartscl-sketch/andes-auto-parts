"""Regresión parser OCR ALI REPUESTOS (factura térmica columnar)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.utils.invoice_providers.ali_repuestos import AliRepuestosParser

# OCR real factura N°34084 — columnas Cant/Detalle/Precio/Total separadas
OCR_FIXTURE_34084 = """
R.U.T: 77.229.308-9
FACTURA ELECTRÓNICA
N°34084
ALI REPUESTOS
Emisión: 11-06-2026 11:44:26
Cant
Detalle
Precio Unit.
Total
1 DFM FILTRO AIRE TS EVO 1.5
8.000
8.000
1 DFM FILTRO POLEN SX6/SX5/T
7.000
7.000
1 JMC SENSOR ABS DEL RH VIG
20.000
20.000
DESCUENTO:
2.000
NETO:
27.731
I.V.A. (19%):
5.269
TOTAL:
33.000
"""

# Tras repartir descuento 2.000 sobre neto 27.731 (bruto 35.000)
EXPECTED_34084 = [
    {"descripcion": "DFM FILTRO AIRE TS EVO 1.5", "cantidad": 1, "valor_neto": 6339},
    {"descripcion": "DFM FILTRO POLEN SX6/SX5/T", "cantidad": 1, "valor_neto": 5546},
    {"descripcion": "JMC SENSOR ABS DEL RH VIG", "cantidad": 1, "valor_neto": 15846},
]


def test_fixture_34084_productos() -> None:
    parser = AliRepuestosParser()
    data = parser.parse(
        {
            "rut_proveedor": "77.229.308-9",
            "ocr_texto_crudo": OCR_FIXTURE_34084,
            "productos": [],
        }
    )
    productos = data.get("productos") or []
    assert len(productos) == 3, f"esperados 3 ítems, got {len(productos)}: {productos}"
    for exp, got in zip(EXPECTED_34084, productos):
        assert got["cantidad"] == exp["cantidad"]
        assert got["valor_neto"] == exp["valor_neto"], (
            f"{got.get('descripcion')}: valor_neto {got.get('valor_neto')} != {exp['valor_neto']}"
        )
        assert exp["descripcion"] in (got.get("descripcion") or "")
    assert data.get("total_neto") == 27731
    assert data.get("iva") == 5269
    assert data.get("total") == 33000
    assert data.get("descuento") == 2000
    suma = sum((p.get("cantidad") or 1) * (p.get("valor_neto") or 0) for p in productos)
    assert suma == 27731, f"suma netos {suma} != 27731"
    print("OK ali_repuestos fixture 34084 (3 ítems con descuento prorrateado)\n")


def test_fixture_single_item_descuento() -> None:
    """Un solo ítem: neto del pie reemplaza precio bruto si hay descuento."""
    ocr = """
ALI REPUESTOS
R.U.T: 77.229.308-9
1 FILTRO ACEITE UNICO
35.000
35.000
DESCUENTO:
2.400
NETO:
27.731
I.V.A. (19%):
5.269
TOTAL:
33.000
"""
    parser = AliRepuestosParser()
    data = parser.parse(
        {
            "rut_proveedor": "77.229.308-9",
            "ocr_texto_crudo": ocr,
            "productos": [],
        }
    )
    productos = data.get("productos") or []
    assert len(productos) == 1
    assert productos[0]["valor_neto"] == 27731
    print("OK ali_repuestos ítem único con descuento\n")


if __name__ == "__main__":
    test_fixture_34084_productos()
    test_fixture_single_item_descuento()
