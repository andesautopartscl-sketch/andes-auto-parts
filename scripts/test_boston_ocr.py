"""Regresión parser OCR Boston Ltda (factura térmica CHERY- códigos)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.utils.invoice_providers.boston import BostonParser

OCR_FIXTURE_365597 = """
R.U.T.: 76.351.383-1
FACTURA ELECTRONICA
MONO 365597
S.I.I-SANTIAGO CENTRO
SOCIEDAD DE INVERSIONES E
INMOBILIARIA BOSTON LIMITADA
Giro ARRIENDO DE INMUEBLES NO AMOBLADOS
COMPRA Y VENTA DE REPUESTOS DE VEHICULOS
Forma de Pago :Contado
Vendedor William Baraza
Fecha de Emision: 23 de Junio de 2026
Cliente ANDES AUTOPARTS LTDA
R.U.T 78.074.288-7
Detalle
Cantidad
Total
FILTRO DE POLEN TIGGO 2 PRO MAX
CHERY-301001665AA-200 x 2.941
ALT
5.882
FILTRO DE AIRE TIGGO 2 PRO 15/ TIGGO 2
(D4G15BY/BW)/TIGGO 2 PRO MAX
CHERY-151000158AA- 2.00 x 3.361 5
6.723
ALT
Monto Neto $ :
12.605
Monto I.V.A. 19% $:
2.395
Monto Total $
15.000
"""


def test_fixture_365597() -> None:
    parser = BostonParser()
    data = parser.parse(
        {
            "rut_proveedor": "76.351.383-1",
            "numero_documento": "365597",
            "ocr_texto_crudo": OCR_FIXTURE_365597,
            "productos": [],
            "total_neto": 12605,
            "iva": 2395,
            "total": 15000,
        }
    )
    productos = data.get("productos") or []
    assert len(productos) == 2, productos

    p0, p1 = productos
    assert p0["codigo_proveedor"] == "301001665AA"
    assert p0["cantidad"] == 2, f"cantidad p0 {p0.get('cantidad')}"
    assert p0["valor_neto"] == 2941, f"valor_neto p0 {p0.get('valor_neto')}"

    assert p1["codigo_proveedor"] == "151000158AA"
    assert p1["cantidad"] == 2, f"cantidad p1 {p1.get('cantidad')}"
    assert p1["valor_neto"] == 3361.5, f"valor_neto p1 {p1.get('valor_neto')}"

    assert data.get("total_neto") == 12605
    assert data.get("iva") == 2395
    assert data.get("total") == 15000

    suma = sum((p.get("cantidad") or 1) * (p.get("valor_neto") or 0) for p in productos)
    assert suma == 12605, f"suma líneas {suma}"
    print("OK boston fixture 365597\n")


if __name__ == "__main__":
    test_fixture_365597()
