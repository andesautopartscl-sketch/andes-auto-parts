"""Regresión Xinwang folio 2902 — cantidad y precio sin separador de miles."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "invoice_vision", ROOT / "app" / "utils" / "invoice_vision.py"
)
iv = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(iv)

PARACHOQUE = """
IMPORTADORA Y EXPORTADORA XINWANG SPA
R.U.T.: 78.031.825-2
FACTURA ELECTRONICA N 2902
Codigo
-
Descripcion
Cantidad
Precio
Valor
PARACHOQUE TRAS SUP NEGRO
1 0
36975
36975
Forma de Pago:Contado
MONTO NETO $ 36.975
IVA 19% $ 7.025
TOTAL $ 44.000
"""

SIN_PRECIOS = """
XINWANG
Codigo
Descripcion
Cantidad
Precio
Valor
PARACHOQUE TRAS SUP NEGRO
Forma de Pago:Contado
MONTO NETO $ 36.975
"""


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in iv._normalize_ocr_text(text).splitlines() if ln.strip()]


def main() -> None:
    lines = _lines(PARACHOQUE)
    assert iv._is_xinwang_monto_line("36975"), "36975 debe ser monto Xinwang"
    productos = iv._extract_productos_sin_codigo_xinwang(lines)
    assert productos, "sin productos PARACHOQUE"
    p0 = productos[0]
    assert p0.get("cantidad") == 1, p0
    assert p0.get("valor_neto") == 36975, p0
    assert "PARACHOQUE" in (p0.get("descripcion") or ""), p0
    print("OK PARACHOQUE con precios:", p0)

    lines2 = _lines(SIN_PRECIOS)
    fb = iv._xinwang_fallback_from_totals(lines2, 36975)
    assert fb and fb[0].get("valor_neto") == 36975, fb
    assert fb[0].get("cantidad") == 1, fb
    print("OK fallback sin precios:", fb[0])


if __name__ == "__main__":
    main()
