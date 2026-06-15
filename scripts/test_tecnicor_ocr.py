"""Regresión parser OCR Tecnicor (factura DTE con precios tras timbre)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.utils.invoice_providers.tecnicor import TecnicorParser

OCR_FIXTURE_3636124 = """
TECNICOR
RAUL TAGLE E HIJOS LIMITADA
R.U.T.: 81.448.200-6
FACTURA ELECTRONICA
N° 3636124
FECHA EMISION
: 15-06-2026
CONDICION DE PAGO: Contado
CODIGO CANT. U/M
DETALLE
670750
2,00
CU CORREA POLY V MITSUBA GPK-2490
PAGADO
SON: VEINTINUEVE MIL SEISCIENTOS NOVENTA Y TRES PESOS.
PRECIO
LISTA
DSCTO.
PRECIO
UNITARIO
TOTAL
12.476
12.475,82
24.952
SUBTOTAL NETO
24.952
TOTAL NETO
I.V.A 19%
TOTAL
24.952
4.741
29.693
Sirvase cancelar con cheque cruzado y nominativo a nombre de:
RAUL TAGLE E HIJOS LTDA.
tecnicorchile.cl
"""


def test_fixture_3636124() -> None:
    parser = TecnicorParser()
    data = parser.parse(
        {
            "rut_proveedor": "81.448.200-6",
            "ocr_texto_crudo": OCR_FIXTURE_3636124,
            "productos": [],
            "metodo_pago": "cheque",
            "total_neto": 20968,
            "iva": 3984,
            "total": 24952,
        }
    )
    productos = data.get("productos") or []
    assert len(productos) == 1, productos
    p0 = productos[0]
    assert p0["codigo_proveedor"] == "670750"
    assert p0["cantidad"] == 2, f"cantidad {p0.get('cantidad')}"
    assert p0["valor_neto"] == 12476, f"valor_neto {p0.get('valor_neto')}"
    assert data.get("total_neto") == 24952
    assert data.get("iva") == 4741
    assert data.get("total") == 29693
    assert data.get("metodo_pago") == "contado"
    suma = sum((p.get("cantidad") or 1) * (p.get("valor_neto") or 0) for p in productos)
    assert suma == 24952, f"suma líneas {suma}"
    print("OK tecnicor fixture 3636124\n")


if __name__ == "__main__":
    test_fixture_3636124()
