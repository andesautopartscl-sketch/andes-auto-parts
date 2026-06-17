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

OCR_FIXTURE_565092 = """
RC REPUESTOS CENTER S.A.
R.U.T. 79.656.210-2
FACTURA ELECTRÓNICA
N° 0000565092
SEÑORES ANDES AUTO PARTS
CÓDIGO
: C78074288-7
FORMA DE PAGO: Crédito
PRECIO
DETALLE
CANTIDAD
PRECIO ÍTEM
UNITARIO
1
67.000,00
67.000
1
55.000,00
55.000
CÓDIGO
MAXD07ORC EJE DE LEVAS (ADMISION) CON PIÑON
EJE DE LEVAS (ADMISION) CON PIÑON
MAXD083RC
EJE DE LEVAS (ESCAPE)
OBSERVACIONES: Basado en la Orden de Venta: 10411526
MONTO NETO
122.000
MONTO IVA 19%
23.180
MONTO EXENTO
0
MONTO TOTAL
145.180
"""

OCR_FIXTURE_565092_PDF_ROW = """
R.U.T. 79.656.210-2
FACTURA ELECTRÓNICA
N° 0000565092
SEÑORES ANDES AUTO PARTS
CÓDIGO : C78074288-7
FORMA DE PAGO: Crédito
CÓDIGO DETALLE CANTIDAD PRECIO UNITARIO PRECIO ÍTEM
MAXD070RC EJE DE LEVAS (ADMISION) CON PIÑON 1 67.000,00 67.000
MAXD083RC EJE DE LEVAS (ESCAPE) 1 55.000,00 55.000
OBSERVACIONES: Basado en la Orden de Venta: 10411526
MONTO NETO 122.000
MONTO IVA 19% 23.180
MONTO TOTAL 145.180
"""

OCR_FIXTURE_565092_PDF_COMPACT = """
R.U.T. 79.656.210-2
N° 0000565092
CÓDIGO
: C78074288-7
PRECIO DETALLE CANTIDAD PRECIO ÍTEM UNITARIO
1 67.000,00 67.000
1 55.000,00 55.000
CÓDIGO
MAXD070RC EJE DE LEVAS (ADMISION)
MAXD083RC EJE DE LEVAS (ESCAPE)
MONTO NETO
122.000
MONTO IVA 19%
23.180
MONTO TOTAL
145.180
"""

OCR_FIXTURE_565092_H_CODES = """
R.U.T. 79.656.210-2
FACTURA ELECTRÓNICA
N° 0000565092
FORMA DE PAGO: Crédito
CÓDIGO DETALLE CANTIDAD PRECIO UNITARIO PRECIO ÍTEM
H6007AC KIT DE DISTR. (CADENA) CON PIÑON 1 87.000,00 87.000
H400040 KIT DE DISTR. (CADENA) CON PIÑON 1 35.000,00 35.000
MONTO NETO 122.000
MONTO IVA 19% 23.180
MONTO TOTAL 145.180
"""


DTE_XML_FIXTURE_565092 = """<?xml version="1.0"?>
<DTE><Documento><Detalle>
<NroLinDet>1</NroLinDet>
<CdgItem><VlrCodigo>MAXD070RC</VlrCodigo></CdgItem>
<QtyItem>1</QtyItem><PrcItem>67000</PrcItem>
</Detalle><Detalle>
<NroLinDet>2</NroLinDet>
<CdgItem><VlrCodigo>MAXD083RC</VlrCodigo></CdgItem>
<QtyItem>1</QtyItem><PrcItem>55000</PrcItem>
</Detalle>
<Totales><MntNeto>122000</MntNeto><IVA>23180</IVA><MntTotal>145180</MntTotal></Totales>
</Documento></DTE>"""


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


def test_fixture_565092() -> None:
    """Layout foto: cantidades antes, códigos en DETALLE (MAXD07ORC OCR)."""
    parser = RepuestoCenterParser()
    data = parser.parse(
        {
            "rut_proveedor": "79.656.210-2",
            "ocr_texto_crudo": OCR_FIXTURE_565092,
            "productos": [],
            "total_neto": 145180,
            "iva": 565092,
            "total": 710272,
            "numero_documento": "565092",
        }
    )
    productos = data.get("productos") or []
    assert len(productos) == 2, productos
    assert productos[0]["codigo_proveedor"] == "MAXD070RC"
    assert productos[0]["cantidad"] == 1
    assert productos[0]["valor_neto"] == 67000
    assert productos[1]["codigo_proveedor"] == "MAXD083RC"
    assert productos[1]["cantidad"] == 1
    assert productos[1]["valor_neto"] == 55000
    assert data.get("total_neto") == 122000
    assert data.get("iva") == 23180
    assert data.get("total") == 145180
    assert data.get("metodo_pago") == "credito"
    suma = sum((p.get("cantidad") or 1) * (p.get("valor_neto") or 0) for p in productos)
    assert suma == 122000
    print("OK repuesto_center fixture 565092\n")


def test_fixture_565092_pdf_row() -> None:
    """PDF nativo: código, cantidad y precio en la misma fila."""
    parser = RepuestoCenterParser()
    data = parser.parse(
        {
            "rut_proveedor": "79.656.210-2",
            "ocr_texto_crudo": OCR_FIXTURE_565092_PDF_ROW,
            "productos": [],
        }
    )
    productos = data.get("productos") or []
    assert len(productos) == 2, productos
    assert productos[0]["codigo_proveedor"] == "MAXD070RC"
    assert productos[0]["valor_neto"] == 67000
    assert productos[1]["codigo_proveedor"] == "MAXD083RC"
    assert productos[1]["valor_neto"] == 55000
    assert data.get("total_neto") == 122000
    assert data.get("total") == 145180
    print("OK repuesto_center fixture 565092 PDF row\n")


def test_fixture_565092_pdf_compact() -> None:
    """PDF nativo: cantidad y precios compactos antes del bloque CÓDIGO."""
    parser = RepuestoCenterParser()
    data = parser.parse(
        {
            "rut_proveedor": "79.656.210-2",
            "ocr_texto_crudo": OCR_FIXTURE_565092_PDF_COMPACT,
            "productos": [],
        }
    )
    productos = data.get("productos") or []
    assert len(productos) == 2, productos
    assert productos[0]["cantidad"] == 1
    assert productos[0]["valor_neto"] == 67000
    assert productos[1]["valor_neto"] == 55000
    print("OK repuesto_center fixture 565092 PDF compact\n")


def test_fixture_565092_h_codes() -> None:
    """PDF fila con códigos cortos H6007AC / H400040 (1 letra + dígitos)."""
    parser = RepuestoCenterParser()
    data = parser.parse(
        {
            "rut_proveedor": "79.656.210-2",
            "ocr_texto_crudo": OCR_FIXTURE_565092_H_CODES,
            "productos": [],
            "total_neto": 145180,
            "iva": 565092,
            "total": 710272,
        }
    )
    productos = data.get("productos") or []
    assert len(productos) == 2, productos
    assert productos[0]["codigo_proveedor"] == "H6007AC"
    assert productos[0]["valor_neto"] == 87000
    assert productos[1]["codigo_proveedor"] == "H400040"
    assert productos[1]["valor_neto"] == 35000
    assert data.get("total_neto") == 122000
    assert data.get("total") == 145180
    print("OK repuesto_center fixture 565092 H-codes\n")


def test_dte_xml_productos() -> None:
    from app.utils.invoice_vision import (
        _extract_montos_from_dte_xml,
        _extract_productos_from_dte_xml,
    )

    productos = _extract_productos_from_dte_xml(DTE_XML_FIXTURE_565092)
    assert len(productos) == 2, productos
    assert productos[0]["codigo_proveedor"] == "MAXD070RC"
    assert productos[0]["valor_neto"] == 67000
    neto, iva, total = _extract_montos_from_dte_xml(DTE_XML_FIXTURE_565092)
    assert neto == 122000
    assert iva == 23180
    assert total == 145180
    print("OK repuesto_center DTE XML\n")


def test_folio_no_es_codigo() -> None:
    from app.utils.invoice_providers.repuesto_center import (
        _extract_item_codes,
        _normalize_rc_code_ocr,
    )
    from app.utils import invoice_vision as iv

    lines = [
        ln.strip()
        for ln in iv._normalize_ocr_text(
            "N° 0000565092\nMAXDO7ORC EJE\nMAXD083RC EJE"
        ).splitlines()
        if ln.strip()
    ]
    codes = _extract_item_codes(lines, "565092")
    assert "0000565092" not in codes, codes
    assert _normalize_rc_code_ocr("MAXDO7ORC") == "MAXD070RC"
    print("OK repuesto_center folio filter\n")


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


def test_repair_neto_cuadra_total_polluido() -> None:
    """Neto correcto pero total OCR 652025 (folio+87560) → total 87560."""
    from app.utils.invoice_vision import reconcile_factura_totals_con_lineas

    productos = [
        {"cantidad": 1, "valor_neto": 45680},
        {"cantidad": 1, "valor_neto": 27900},
    ]
    neto, iva, total = reconcile_factura_totals_con_lineas(
        productos, 73580, None, 652025
    )
    assert neto == 73580
    assert iva == 13980
    assert total == 87560


if __name__ == "__main__":
    test_fixture_564465()
    test_fixture_565092()
    test_fixture_565092_pdf_row()
    test_fixture_565092_pdf_compact()
    test_fixture_565092_h_codes()
    test_dte_xml_productos()
    test_folio_no_es_codigo()
    test_repair_folio_en_neto()
    test_repair_neto_cuadra_total_polluido()
