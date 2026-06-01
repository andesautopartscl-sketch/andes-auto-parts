"""Debug: corre el OCR de una factura y muestra el texto crudo de Google Vision.

Uso:
    python scripts/debug_ocr_factura.py "ruta/a/la/imagen.jpeg"
"""
from __future__ import annotations

import base64
import mimetypes
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except Exception:
    pass

os.chdir(PROJECT_ROOT)

from app.utils.invoice_vision import analizar_factura  # noqa: E402


def main() -> None:
    if len(sys.argv) < 2:
        print("Falta la ruta de la imagen.")
        sys.exit(1)

    path = Path(sys.argv[1])
    if not path.is_file():
        print(f"No existe el archivo: {path}")
        sys.exit(1)

    mt = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    if path.suffix.lower() == ".pdf":
        mt = "application/pdf"

    b64 = base64.b64encode(path.read_bytes()).decode("ascii")

    data = analizar_factura(b64, mt)

    print("\n\n========== OCR TEXTO CRUDO (Google Vision) ==========")
    print(data.get("ocr_texto_crudo", "NO HAY TEXTO"))
    print("========== FIN OCR CRUDO ==========\n")

    print("========== CAMPOS DETECTADOS ==========")
    for k in (
        "numero_documento",
        "tipo_documento",
        "rut_proveedor",
        "rut_emisor",
        "razon_social_emisor",
        "fecha",
        "total_neto",
        "iva",
        "total",
    ):
        print(f"{k}: {data.get(k)!r}")

    print("\n========== PRODUCTOS DETECTADOS ==========")
    productos = data.get("productos") or []
    if not productos:
        print("(ninguno)")
    for i, p in enumerate(productos, 1):
        print(f"[{i}] {p!r}")
    print("========== FIN ==========")


if __name__ == "__main__":
    main()
