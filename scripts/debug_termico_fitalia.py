"""Debug paso a paso de _extract_productos_termico (factura Fitalia).

Uso:
    python scripts/debug_termico_fitalia.py
    python scripts/debug_termico_fitalia.py "ruta/imagen.jpeg"

Con imagen: corre Google Vision y luego el parser térmico sobre el OCR crudo.
Sin imagen: usa el OCR de referencia guardado de la boleta M60415-BOSCH.
"""
from __future__ import annotations

import base64
import mimetypes
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except Exception:
    pass

# OCR de referencia (Google Vision sobre boleta Fitalia 335612)
FITALIA_OCR_REF = """\
RUT:84.726.100-5
FACTURA ELECTRONICA
NUMERO:0000335612
FITALIA REPUESTOS SPA
FECHA:01/06/2026
M60415-BOSCH FILTRO PETR M/PICKUP-SCORPIO
-XUV-GENIO
2,00UN x 8.500
0= 17.000
TOT.UNIDADES:2,00
TOTAL NETO:
17.000
MONTO TOTAL:
20.230
"""


def main() -> None:
    from app.utils.invoice_vision import (
        _extract_productos_termico,
        _normalize_ocr_text,
        analizar_factura,
    )

    if len(sys.argv) >= 2:
        path = Path(sys.argv[1])
        if not path.is_file():
            print(f"No existe: {path}", file=sys.stderr)
            sys.exit(1)
        mt = mimetypes.guess_type(str(path))[0] or "image/jpeg"
        if path.suffix.lower() == ".pdf":
            mt = "application/pdf"
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        print(f"=== OCR Google Vision ({path.name}) ===\n", flush=True)
        data = analizar_factura(b64, mt)
        texto = data.get("ocr_texto_crudo") or ""
        print(texto, flush=True)
        print("\n=== FIN OCR ===\n", flush=True)
    else:
        print("=== OCR de referencia Fitalia (sin imagen) ===\n", flush=True)
        texto = FITALIA_OCR_REF

    texto_norm = _normalize_ocr_text(texto)
    print("=== _extract_productos_termico ===\n", flush=True)
    resultado = _extract_productos_termico(texto_norm)
    print("\n=== RESULTADO FINAL ===", flush=True)
    print(resultado, flush=True)


if __name__ == "__main__":
    main()
