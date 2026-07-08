"""Test parser OCR factura Mundo Repuestos (columnas separadas)."""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from app.utils.invoice_vision import (
    _extract_fecha_emision,
    _extract_fecha_from_dte_pdf,
    garantizar_producto_factura,
    parsear_factura_chilena,
)

# OCR real (layout columnas separadas) — factura 122348
OCR_FIXTURE_122348 = """
MR MUNDO REPUESTOS S.A.
R.U.T.: 76.192.209-2
FACTURA ELECTRONICA
Nº 122348
Código
Cantidad
Descripción
Precio Unit.
Valor
0023060
P019694
H480120
H480130
0009530
2 FILTRO BENCINA CHV CORSA TODOS C/SEGUROS STP 15382
4 BUJIA HY ACCENT/RIO/130/SOUL 15 MOBIS
1 BIELETA B/ESTAB HY NEW ACCENT 06/ DEL DER SYG
1 BIELETA B/ESTAB HY NEW ACCENT 06/ DEL IZQ SYG
2 FILTRO ACEITE SZ AERIO SX4 SWIFT 05/ STP 10542
983
6.605
1.986
26.420
5.067
5.067
2.420
Neto
72.370
"""

PDF_PATH = Path(
    os.environ.get(
        "MUNDO_REPUESTOS_PDF",
        r"C:\Users\alber\Downloads\dte_76192209_33_122348.pdf",
    )
)

EXPECTED_122348 = [
    {"codigo_proveedor": "0023060", "cantidad": 2, "valor_neto": 983},
    {"codigo_proveedor": "P019694", "cantidad": 4, "valor_neto": 6605},
    {"codigo_proveedor": "H480120", "cantidad": 1, "valor_neto": 5067},
    {"codigo_proveedor": "H480130", "cantidad": 1, "valor_neto": 5067},
    {"codigo_proveedor": "0009530", "cantidad": 2, "valor_neto": 1210},
]

OCR_FIXTURE_123310 = """
MR MUNDO REPUESTOS S.A.
R.U.T.: 76.192.209-2
FACTURA ELECTRONICA
Nº 123310
Código
Cantidad
Descripción
Precio Unit.
Valor
P019694
9975237
4 BUJIA HY ACCENT/RIO/I30/SOUL 15 MOBIS
1 CORREA ALTERNADOR DCROER JMC VIGUS WORK
6.605
6.226
26.420
6.227
Neto
32.647
19% I.V.A.
6.203
Total
38.850
"""

EXPECTED_123310 = [
    {"codigo_proveedor": "P019694", "cantidad": 4, "valor_neto": 6605},
    {"codigo_proveedor": "9975237", "cantidad": 1, "valor_neto": 6226},
]

# OCR real Vision — factura 124980 (layout intercalado + pie apilado :$)
OCR_FIXTURE_124980 = """
MR MUNDO REPUESTOS S.A.
R.U.T.: 76.192.209-2
FACTURA ELECTRONICA
N° 124980
Código
Cantidad
Descripción
Precio Unit.
Valor
9961634
P019694
2 TERMINALES DE DIRECCION RH LH GW POER 2.0T
4 BUJIA HY ACCENT/RIO/I30/SOUL 15 MOBIS
5.420
10.840
6.605
26.420
0022171
1 RETEN CIG CHV D-MAX 2.5 11/ DELANT JAPON
2.873
2.874
9961239
9975240
KR00730
1 SENSOR POSICION EJE LEVA 1.6 CHANGAN CX70
1 SENSOR MAP DCROER JMC VIGUS WORK
7.235
7.235
8.244
8.245
4 ARTIC AXIAL KIA NEW RIO 06/ DH DER/IZQ SYG
6.000
24.000
Son: noventa y cuatro mil setecientos cuarenta y uno
Neto
:$
79.614
19% I.V.A. :$
15.127
Total
:$
94.741
"""

EXPECTED_124980 = [
    {"codigo_proveedor": "9961634", "cantidad": 2, "valor_neto": 5420},
    {"codigo_proveedor": "P019694", "cantidad": 4, "valor_neto": 6605},
    {"codigo_proveedor": "0022171", "cantidad": 1, "valor_neto": 2873},
    {"codigo_proveedor": "9961239", "cantidad": 1, "valor_neto": 7235},
    {"codigo_proveedor": "9975240", "cantidad": 1, "valor_neto": 8244},
    {"codigo_proveedor": "KR00730", "cantidad": 4, "valor_neto": 6000},
]

# PDF nativo truncado (sin OCR Vision) — no debe quedar con 3 ítems erróneos
OCR_FIXTURE_124980_NATIVE = """
MR MUNDO REPUESTOS S.A.
R.U.T.: 76.192.209-2
FACTURA ELECTRONICA
N 124980
Codigo
Cantidad
Descripcion
Precio Unit.
Valor
9961634
P019694
0022171
2 TERMINALES DE DIRECCION RH LH GW POER 2.0T
4 BUJIA HY ACCENT/RIO/I30/SOUL 15 MOBIS
1 RETEN CIG CHV D-MAX 2.5 11/ DELANT JAPON
79.614
15.127
94.741
Neto
19% I.V.A.
4
Total
23
"""

# Pie OCR corrupto al inicio + neto/total mezclados en bloque Valor (caso PDF 124980)
OCR_FIXTURE_124980_BAD = """
Neto
19% I.V.A.
4
Total
23
MR MUNDO REPUESTOS S.A.
R.U.T.: 76.192.209-2
FACTURA ELECTRONICA
N° 124980
Código
Cantidad
Descripción
Precio Unit.
Valor
9961634
P019694
79.614
94.741
TERMINALES DE DIRECCION RH LH GW POER 2.0T
BUJIA HY ACCENT/RIO/I30/SOUL 15 MOBIS
5.420
10.840
6.605
26.420
0022171
RETEN CIG CHV D-MAX 2.5 11/ DELANT JAPON
2.873
2.874
9961239
9975240
KR00730
SENSOR POSICION EJE LEVA 1.6 CHANGAN CX70
SENSOR MAP DCROER JMC VIGUS WORK
7.235
7.235
8.244
8.245
ARTIC AXIAL KIA NEW RIO 06/ DH DER/IZQ SYG
6.000
24.000
Neto
:$
79.614
19% I.V.A. :$
15.127
Total
:$
94.741
"""


def _print_productos(productos: list[dict]) -> None:
    for i, p in enumerate(productos, 1):
        print(
            f"  {i}. {p.get('codigo_proveedor',''):10s} "
            f"qty={p.get('cantidad')} neto={p.get('valor_neto')}"
        )


def _assert_productos(productos: list[dict], expected: list[dict], label: str) -> None:
    assert len(productos) == len(expected), (
        f"{label}: esperados {len(expected)} productos, got {len(productos)}: {productos}"
    )
    for exp, got in zip(expected, productos):
        assert got["codigo_proveedor"] == exp["codigo_proveedor"]
        assert got["cantidad"] == exp["cantidad"]
        assert got["valor_neto"] == exp["valor_neto"]
    print(f"OK {label}\n")


def test_fixture() -> None:
    print("=" * 80)
    print("TEST FIXTURE OCR 122348 (columnas separadas)")
    print("=" * 80)
    data = garantizar_producto_factura(parsear_factura_chilena(OCR_FIXTURE_122348))
    productos = data.get("productos") or []
    _print_productos(productos)
    _assert_productos(productos, EXPECTED_122348, "fixture 122348")


def test_fecha_dte_xml() -> None:
    pdf_stub = b"""%PDF-1.4
1 0 obj<<>>endobj
trailer<<>>
<FchEmis>2026-06-11</FchEmis>
%%EOF"""
    fecha = _extract_fecha_from_dte_pdf(pdf_stub)
    assert fecha == "11-06-2026", f"fecha XML esperada 11-06-2026, got {fecha}"
    print("OK fecha DTE XML embebido\n")


def test_fecha_emision_mundo_multiline() -> None:
    ocr = """
Emisión
: 2026 06 11
Fecha de Vencimiento
Fecha de Compromiso
: 2026 06 11
"""
    fecha = _extract_fecha_emision(ocr)
    assert fecha == "11-06-2026", f"fecha multilínea esperada 11-06-2026, got {fecha}"
    print("OK fecha emisión multilínea Mundo\n")


def test_fecha_emision_mundo_columnar_lejos() -> None:
    """Layout Mundo: fecha de emisión ~10 líneas bajo la etiqueta."""
    ocr = """
Emisión
:ANDES AUTO PARTS LTDA
78.074.288-7
: LA CONCEPCION 81
: Providencia
: Santiago
: 954806153
: 2026-06-11
Fecha de Vencimiento
Fecha de Compromiso
: 2026 06 11
"""
    fecha = _extract_fecha_emision(ocr)
    assert fecha == "11-06-2026", f"fecha columnar esperada 11-06-2026, got {fecha}"
    print("OK fecha emisión columnar Mundo (fecha lejos de etiqueta)\n")


def test_fecha_emision_123310() -> None:
    """Folio 123310 no debe empujar el día a 12 cuando emisión es 11."""
    ocr = """
MR MUNDO REPUESTOS S.A.
FACTURA ELECTRONICA
Nº 123310
Emisión : 2026 06 11
Fecha de Vencimiento : 2026 06 11
Fecha de Compromiso : 2026 06 11
: 2026-06-11
: 2026-06-12
"""
    fecha = _extract_fecha_emision(ocr)
    assert fecha == "11-06-2026", f"fecha esperada 11-06-2026, got {fecha}"
    print("OK fecha emisión 123310 (día 11, no 12)\n")


def test_fixture_123310() -> None:
    print("=" * 80)
    print("TEST FIXTURE OCR 123310 (2 ítems, bloques unit/total)")
    print("=" * 80)
    data = garantizar_producto_factura(parsear_factura_chilena(OCR_FIXTURE_123310))
    productos = data.get("productos") or []
    _print_productos(productos)
    _assert_productos(productos, EXPECTED_123310, "fixture 123310")


def test_fixture_124980() -> None:
    print("=" * 80)
    print("TEST FIXTURE OCR 124980 (6 ítems, layout intercalado)")
    print("=" * 80)
    data = garantizar_producto_factura(parsear_factura_chilena(OCR_FIXTURE_124980))
    productos = data.get("productos") or []
    _print_productos(productos)
    _assert_productos(productos, EXPECTED_124980, "fixture 124980")
    assert data.get("total_neto") == 79614, f"neto esperado 79614, got {data.get('total_neto')}"
    assert data.get("total") == 94741, f"total esperado 94741, got {data.get('total')}"
    assert data.get("iva") == 15127, f"iva esperado 15127, got {data.get('iva')}"
    print("OK montos 124980\n")


def test_fixture_124980_bad_footer() -> None:
    """Pie espurio (19/4/23) y neto/total en columna Valor no deben corromper ítems."""
    print("=" * 80)
    print("TEST FIXTURE OCR 124980 BAD (pie corrupto + totales en precios)")
    print("=" * 80)
    data = garantizar_producto_factura(parsear_factura_chilena(OCR_FIXTURE_124980_BAD))
    productos = data.get("productos") or []
    _print_productos(productos)
    _assert_productos(productos, EXPECTED_124980, "fixture 124980 bad footer")
    assert data.get("total_neto") == 79614, f"neto esperado 79614, got {data.get('total_neto')}"
    assert data.get("total") == 94741, f"total esperado 94741, got {data.get('total')}"
    print("OK montos 124980 bad footer\n")


def test_fixture_124980_native_pdf() -> None:
    """Texto nativo PDF truncado: montos por triplete, sin precios de pie como ítems."""
    print("=" * 80)
    print("TEST FIXTURE OCR 124980 NATIVE PDF (3 códigos, pie en Valor)")
    print("=" * 80)
    from app.utils.invoice_vision import _pdf_native_parse_is_sufficient

    from app.utils.invoice_providers import registry

    data = garantizar_producto_factura(parsear_factura_chilena(OCR_FIXTURE_124980_NATIVE))
    assert not _pdf_native_parse_is_sufficient(data), (
        "PDF nativo truncado no debe considerarse suficiente sin OCR"
    )
    p = registry.find(data.get("rut_proveedor"), OCR_FIXTURE_124980_NATIVE)
    data = p.parse(data)
    productos = data.get("productos") or []
    _print_productos(productos)
    assert data.get("total_neto") == 79614, f"neto esperado 79614, got {data.get('total_neto')}"
    assert data.get("total") == 94741, f"total esperado 94741, got {data.get('total')}"
    assert data.get("iva") == 15127, f"iva esperado 15127, got {data.get('iva')}"
    for p in productos:
        assert int(p.get("valor_neto") or 0) < 50_000, (
            f"precio unitario no puede ser monto de pie: {p}"
        )
    print("OK fixture 124980 native pdf\n")


def test_pdf_if_available() -> None:
    if not PDF_PATH.is_file():
        print(f"PDF {PDF_PATH} no encontrado — omitiendo test live OCR\n")
        return
    from app.utils.invoice_vision import analizar_factura

    print("=" * 80)
    print(f"TEST PDF: {PDF_PATH.name}")
    print("=" * 80)
    b64 = base64.b64encode(PDF_PATH.read_bytes()).decode()
    data = garantizar_producto_factura(analizar_factura(b64, "application/pdf"))
    productos = data.get("productos") or []
    _print_productos(productos)
    print(json.dumps({k: v for k, v in data.items() if k != "ocr_texto_crudo"}, indent=2, ensure_ascii=False))
    print()


if __name__ == "__main__":
    test_fecha_dte_xml()
    test_fecha_emision_mundo_multiline()
    test_fecha_emision_mundo_columnar_lejos()
    test_fecha_emision_123310()
    test_fixture()
    test_fixture_123310()
    test_fixture_124980()
    test_fixture_124980_bad_footer()
    test_fixture_124980_native_pdf()
    test_pdf_if_available()
