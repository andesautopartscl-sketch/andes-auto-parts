"""Test OCR factura Xinwang — cantidad debe ser 3, no 1."""
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
    analizar_factura,
    garantizar_producto_factura,
    parsear_factura_chilena,
)

XINWANG_FIXTURE = """
XINWANG MOTOR
Codigo
-
3 3  4.874  14.622
- FILTRO ACEITE MOTOR 3 4874 14622
"""

XINWANG_FIXTURE_2947 = """
IMPORTACION Y EXPORTACION XINWANG SPA
R.U.T.: 78.031.825-2
FACTURA ELECTRONICA N 2947
Codigo
Descripcion
Cantidad
Precio
%Impto Adic.*
%Desc.
Valor
RODAMIENTO CAZOLETA
22
6.723
13.446
MUÑON DELT RH
1 1
58.823
58.823
Forma de Pago: Contado
MONTO NETO $ 72.269
I.V.A. 19% $ 13.731
IMPUESTO ADICIONAL $ 0
TOTAL $ 86.000
"""

XINWANG_FIXTURE_2947_INLINE = """
IMPORTACION Y EXPORTACION XINWANG SPA
R.U.T.: 78.031.825-2
FACTURA ELECTRONICA N 2947
Codigo
Descripcion
Cantidad
Precio
Valor
RODAMIENTO CAZOLETA   2 2   6.723   13.446
MUÑON DELT RH          1 1  58.823   58.823
Forma de Pago: Contado
MONTO NETO $ 72.269
I.V.A. 19% $ 13.731
TOTAL $ 86.000
"""

PDF_PATH = Path(
    os.environ.get(
        "XINWANG_PDF",
        r"C:\Users\alber\Downloads\ANDESS86900.pdf",
    )
)


def test_fixture_qty() -> None:
    print("=" * 80)
    print("TEST FIXTURE Xinwang cantidad 3")
    print("=" * 80)
    data = garantizar_producto_factura(parsear_factura_chilena(XINWANG_FIXTURE))
    productos = data.get("productos") or []
    assert any(p.get("cantidad") == 3 for p in productos), productos
    print(f"OK qty=3 en {productos}\n")


def test_fixture_2947() -> None:
    """Folio 2947: cantidad '22' OCR (2 2 sin espacio) y filas inline."""
    from app.utils.invoice_providers.xinwang import XinwangParser

    parser = XinwangParser()
    for label, fixture in (
        ("stacked merged qty", XINWANG_FIXTURE_2947),
        ("inline spaced qty", XINWANG_FIXTURE_2947_INLINE),
    ):
        data = parser.parse(
            {
                "rut_proveedor": "78.031.825-2",
                "ocr_texto_crudo": fixture,
                "numero_documento": "2947",
            }
        )
        productos = data.get("productos") or []
        assert len(productos) == 2, (label, productos)
        assert productos[0]["descripcion"] == "RODAMIENTO CAZOLETA", (label, productos)
        assert productos[0]["cantidad"] == 2, (label, productos)
        assert productos[0]["valor_neto"] == 6723, (label, productos)
        assert productos[1]["descripcion"] == "MUÑON DELT RH", (label, productos)
        assert productos[1]["cantidad"] == 1, (label, productos)
        assert productos[1]["valor_neto"] == 58823, (label, productos)
        suma = sum((p["cantidad"] * p["valor_neto"]) for p in productos)
        assert suma == 72269, (label, suma, productos)
    print("OK Xinwang fixture 2947\n")


def main() -> None:
    test_fixture_qty()
    test_fixture_2947()
    if not PDF_PATH.is_file():
        print(f"ERROR: no se encuentra {PDF_PATH}")
        sys.exit(1)

    print(f"Leyendo: {PDF_PATH}")
    b64 = base64.b64encode(PDF_PATH.read_bytes()).decode()
    data = garantizar_producto_factura(analizar_factura(b64, "application/pdf"))

    print("=" * 80)
    print("PRODUCTOS:")
    print("=" * 80)
    productos = data.get("productos") or []
    for i, p in enumerate(productos, 1):
        print(
            f"  {i}. qty={p.get('cantidad')} neto={p.get('valor_neto')} "
            f"desc={p.get('descripcion', '')[:50]!r}"
        )

    assert productos, "sin productos detectados"
    qtys = [p.get("cantidad") for p in productos]
    assert 3 in qtys, f"se esperaba cantidad 3 en algún ítem, got {qtys}"
    assert all(q != 1 or len(productos) == 1 for q in qtys) or max(qtys) >= 3

    bad = [p for p in productos if p.get("cantidad") == 1 and len(productos) > 1]
    if bad and not any(p.get("cantidad") == 3 for p in productos):
        print("FALLO: ningún producto con cantidad 3")
        sys.exit(1)

    print("\nOK Xinwang")
    res = {k: v for k, v in data.items() if k != "ocr_texto_crudo"}
    print(json.dumps(res, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
