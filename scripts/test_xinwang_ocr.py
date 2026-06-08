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


def main() -> None:
    test_fixture_qty()
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
