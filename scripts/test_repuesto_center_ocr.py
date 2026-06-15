"""Regresión parser OCR Repuesto Center (Facele DTE)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.utils.invoice_providers.repuesto_center import RepuestoCenterParser

OCR_FIXTURE_564465 = """
REPUESTO RC REPUESTOS CENTER S.A.
R.U.T. 79.656.210-2
FACTURA ELECTRÓNICA
N° 0000564465
SEÑORES ANDES AUTO PARTS
: 78.074.288-7
CÓDIGO
HJ076RC
REFUERZO PARACHOQUE DEL
JC305
AMORTIGUADORES TRASEROS
OBSERVACIONES: Basado en la Orden de Venta: 10411000
FECHA EMISIÓN :
2026-06-12
FORMA DE PAGO: Crédito
CANTIDAD
PRECIO ÍTEM
UNITARIO
1
45.680
45.680
1
27.900,00
27.900
MONTO NETO
MONTO IVA 19%
MONTO EXENTO
73.580
13.980
0
MONTO TOTAL
87.560
FacEle Facturación Electrónica www.facele.cl
"""


def test_fixture_564465() -> None:
    parser = RepuestoCenterParser()
    data = parser.parse(
        {
            "rut_proveedor": "78.074.288-7",
            "ocr_texto_crudo": OCR_FIXTURE_564465,
            "productos": [],
            "total_neto": 87560,
            "iva": 564465,
            "total": 652025,
            "numero_documento": "564465",
        }
    )
    productos = data.get("productos") or []
    assert len(productos) == 2, productos
    assert productos[0]["codigo_proveedor"] == "HJ076RC"
    assert productos[0]["cantidad"] == 1
    assert productos[0]["valor_neto"] == 45680
    assert productos[1]["codigo_proveedor"] == "JC305"
    assert productos[1]["cantidad"] == 1, f"JC305 qty {productos[1].get('cantidad')}"
    assert productos[1]["valor_neto"] == 27900
    assert data.get("rut_proveedor") == "79.656.210-2"
    assert data.get("total_neto") == 73580
    assert data.get("iva") == 13980
    assert data.get("total") == 87560
    assert data.get("metodo_pago") == "credito"
    suma = sum((p.get("cantidad") or 1) * (p.get("valor_neto") or 0) for p in productos)
    assert suma == 73580
    print("OK repuesto_center fixture 564465\n")


def test_repair_folio_en_neto() -> None:
    """Folio 564465 en neto + total 87560 en IVA → no debe quedar 652025."""
    parser = RepuestoCenterParser()
    data = parser.parse(
        {
            "rut_proveedor": "79.656.210-2",
            "ocr_texto_crudo": OCR_FIXTURE_564465,
            "productos": [
                {"codigo_proveedor": "HJ076RC", "cantidad": 1, "valor_neto": 45680},
                {"codigo_proveedor": "JC305", "cantidad": 1, "valor_neto": 27900},
            ],
            "total_neto": 564465,
            "iva": 87560,
            "total": 652025,
        }
    )
    assert data.get("total") == 87560, f"total {data.get('total')}"
    assert data.get("total_neto") == 73580
    assert data.get("iva") == 13980
    print("OK repuesto_center repair folio+total OCR\n")


if __name__ == "__main__":
    test_fixture_564465()
