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

from app.utils.invoice_vision import garantizar_producto_factura, parsear_factura_chilena

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


def test_fixture_123310() -> None:
    print("=" * 80)
    print("TEST FIXTURE OCR 123310 (2 ítems, bloques unit/total)")
    print("=" * 80)
    data = garantizar_producto_factura(parsear_factura_chilena(OCR_FIXTURE_123310))
    productos = data.get("productos") or []
    _print_productos(productos)
    _assert_productos(productos, EXPECTED_123310, "fixture 123310")


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
    test_fixture()
    test_fixture_123310()
    test_pdf_if_available()
