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


OCR_FIXTURE_565092_050327 = """
R.U.T. 79.656.210-2
FACTURA ELECTRÓNICA
N° 0000565092
CÓDIGO DETALLE CANTIDAD PRECIO UNITARIO PRECIO ÍTEM
050327RC KIT DE DISTRIBUCION CON BOMBA 1 47.200,00 47.200
050328RC KIT DE DISTRIBUCION CON BOMBA 1 74.800,00 74.800
MONTO NETO 122.000
MONTO IVA 19% 23.180
MONTO TOTAL 145.180
"""

OCR_FIXTURE_567124_PDF_ROW = """
R.U.T. 79.656.210-2
FACTURA ELECTRÓNICA
N° 0000567124
SEÑORES ANDES AUTO PARTS
CÓDIGO DETALLE CANTIDAD PRECIO UNITARIO PRECIO ÍTEM
2901 FILTRO DE ACEITE 3 1.500,00 4.500
VG4020RC KIT EMPAQUETADURAS 1 46.010,00 46.010
OBSERVACIONES: Basado en la Orden de Venta : 10413157
MONTO NETO 50.510
MONTO IVA 19% 9.597
MONTO TOTAL 60.107
"""

OCR_FIXTURE_567124_COLUMNAR = """
R.U.T. 79.656.210-2
FACTURA ELECTRÓNICA
N° 0000567124
SEÑORES ANDES AUTO PARTS
CÓDIGO
2901
VG4020RC
FILTRO DE ACEITE
KIT EMPAQUETADURAS
CANTIDAD
3
1
PRECIO ÍTEM
UNITARIO
1.500,00
46.010,00
4.500
46.010
MONTO NETO
50.510
MONTO IVA 19%
9.597
MONTO TOTAL
60.107
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


OCR_FIXTURE_565092_PDF_NATIVE = """
R.U.T. 79.656.210-2
FACTURA ELECTRÓNICA
N° 0000565092
MONTO NETO
MONTO IVA 19%
MONTO TOTAL
122.000
23.180
145.180
Galvarino 8601, Oficinas 6 y 7
MAXD070RC
67.000
EJE DE LEVAS (ADMISION) CON PIÑON
EJE DE LEVAS (ADMISION) CON PIÑON
67.000,00
1
MAXD083RC
55.000
EJE DE LEVAS (ESCAPE)
EJE DE LEVAS (ESCAPE)
55.000,00
1
"""


OCR_FIXTURE_567580_PDF_NATIVE = """
Facturación Electrónica   -   www.facele.cl  -  Tel: (+56 02) 334 6746
MONTO EXENTO
MONTO NETO
MONTO IVA 19%
MONTO TOTAL
La Cisterna
78.074.288-7
SANTIAGO
2026-06-23
VENTA DE PARTES, PIEZAS Y ACCESORIOS
64.867
Salas 8973
79.656.210-2
Quilicura
SANTIAGO
Crédito
LA CONCEPCION 81 OFICINA 214
0000567580
PROVIDENCIA
C78074288-7
54.510
10.357
ANDES AUTO PARTS
FACTURA ELECTRÓNICA
0
Basado en la Orden de Venta : 10413371
2026-06-22
Santiago
Res. 80 de 2014 - Verifique Documento: www.sii.cl
Timbre Electronico S.I.I.
Galvarino 8601, Oficinas 6 y 7
GPP057RC
11.920
RODAMIENTO DE MAZA DEL (88X55X46)
RODAMIENTO DE MAZA DEL (88X55X46)
11.920,00
1
TP120RC
39.090
PORTA FILTRO
PORTA FILTRO
39.090,00
1
ZXT1026RC
3.500
FILTRO A/C
FILTRO A/C
3.500,00
1
"""


def test_fixture_565092_pdf_native() -> None:
    """PDF Facele nativo: ítems al final, después de etiquetas MONTO NETO."""
    parser = RepuestoCenterParser()
    data = parser.parse(
        {
            "rut_proveedor": "79.656.210-2",
            "ocr_texto_crudo": OCR_FIXTURE_565092_PDF_NATIVE,
            "productos": [],
            "numero_documento": "565092",
        }
    )
    productos = data.get("productos") or []
    assert len(productos) == 2, productos
    assert productos[0]["codigo_proveedor"] == "MAXD070RC"
    assert productos[0]["valor_neto"] == 67000
    assert productos[1]["codigo_proveedor"] == "MAXD083RC"
    assert productos[1]["valor_neto"] == 55000
    assert data.get("productos_fuente") == "repuesto_center"
    print("OK repuesto_center fixture 565092 PDF native\n")


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


def test_fixture_565092_050327() -> None:
    """Códigos con prefijo numérico 050327RC / 050328RC (PDF Facele)."""
    parser = RepuestoCenterParser()
    data = parser.parse(
        {
            "rut_proveedor": "79.656.210-2",
            "ocr_texto_crudo": OCR_FIXTURE_565092_050327,
            "productos": [],
            "total_neto": 145180,
            "iva": 565092,
            "total": 710272,
            "numero_documento": "565092",
        }
    )
    productos = data.get("productos") or []
    assert len(productos) == 2, productos
    assert productos[0]["codigo_proveedor"] == "050327RC"
    assert productos[0]["valor_neto"] == 47200
    assert productos[1]["codigo_proveedor"] == "050328RC"
    assert productos[1]["valor_neto"] == 74800
    assert data.get("total_neto") == 122000
    assert data.get("iva") == 23180
    assert data.get("total") == 145180
    print("OK repuesto_center fixture 565092 050327RC\n")


def test_fixture_567124_pdf_row() -> None:
    """PDF fila: código numérico corto 2901 + VG4020RC (folio 567124)."""
    parser = RepuestoCenterParser()
    data = parser.parse(
        {
            "rut_proveedor": "79.656.210-2",
            "ocr_texto_crudo": OCR_FIXTURE_567124_PDF_ROW,
            "productos": [],
            "numero_documento": "567124",
        }
    )
    productos = data.get("productos") or []
    assert len(productos) == 2, productos
    assert productos[0]["codigo_proveedor"] == "2901"
    assert productos[0]["cantidad"] == 3
    assert productos[0]["valor_neto"] == 1500
    assert productos[1]["codigo_proveedor"] == "VG4020RC"
    assert productos[1]["cantidad"] == 1
    assert productos[1]["valor_neto"] == 46010
    assert data.get("total_neto") == 50510
    assert data.get("iva") == 9597
    assert data.get("total") == 60107
    suma = sum((p.get("cantidad") or 1) * (p.get("valor_neto") or 0) for p in productos)
    assert suma == 50510
    print("OK repuesto_center fixture 567124 PDF row\n")


def test_fixture_567124_columnar() -> None:
    """Layout columnar: 2901 numérico corto alineado con cantidades/precios."""
    parser = RepuestoCenterParser()
    data = parser.parse(
        {
            "rut_proveedor": "79.656.210-2",
            "ocr_texto_crudo": OCR_FIXTURE_567124_COLUMNAR,
            "productos": [],
            "numero_documento": "567124",
        }
    )
    productos = data.get("productos") or []
    assert len(productos) == 2, productos
    assert productos[0]["codigo_proveedor"] == "2901"
    assert productos[0]["cantidad"] == 3
    assert productos[0]["valor_neto"] == 1500
    assert productos[1]["codigo_proveedor"] == "VG4020RC"
    assert productos[1]["valor_neto"] == 46010
    assert data.get("total_neto") == 50510
    assert data.get("total") == 60107
    print("OK repuesto_center fixture 567124 columnar\n")


def test_fixture_567580_pdf_native() -> None:
    """PDF Facele nativo folio 567580: códigos basura en pie + 3 ítems al final."""
    parser = RepuestoCenterParser()
    data = parser.parse(
        {
            "rut_proveedor": "79.656.210-2",
            "ocr_texto_crudo": OCR_FIXTURE_567580_PDF_NATIVE,
            "productos": [],
            "total_neto": 64867,
            "iva": 567580,
            "total": 632447,
            "numero_documento": "567580",
        }
    )
    productos = data.get("productos") or []
    assert len(productos) == 3, productos
    assert productos[0]["codigo_proveedor"] == "GPP057RC"
    assert productos[0]["valor_neto"] == 11920
    assert productos[1]["codigo_proveedor"] == "TP120RC"
    assert productos[1]["valor_neto"] == 39090
    assert productos[2]["codigo_proveedor"] == "ZXT1026RC"
    assert productos[2]["valor_neto"] == 3500
    assert data.get("total_neto") == 54510
    assert data.get("iva") == 10357
    assert data.get("total") == 64867
    suma = sum((p.get("cantidad") or 1) * (p.get("valor_neto") or 0) for p in productos)
    assert suma == 54510
    print("OK repuesto_center fixture 567580 PDF native\n")


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
    test_fixture_565092_pdf_native()
    test_fixture_565092_pdf_row()
    test_fixture_565092_pdf_compact()
    test_fixture_565092_h_codes()
    test_fixture_565092_050327()
    test_fixture_567124_pdf_row()
    test_fixture_567124_columnar()
    test_fixture_567580_pdf_native()
    test_dte_xml_productos()
    test_folio_no_es_codigo()
    test_repair_folio_en_neto()
    test_repair_neto_cuadra_total_polluido()
