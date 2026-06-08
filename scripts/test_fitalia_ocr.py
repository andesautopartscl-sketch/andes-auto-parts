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

from app import create_app
from app.utils.codigo_matcher import aplicar_fuzzy_a_productos
from app.utils.invoice_vision import analizar_factura, garantizar_producto_factura

IMAGE_PATH = os.path.expanduser(
    r"~/Downloads/WhatsApp Image 2026-06-08 at 10.40.39.jpeg"
)

app = create_app()

with app.app_context():
    print(f"Leyendo imagen: {IMAGE_PATH}")
    with open(IMAGE_PATH, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("utf-8")

    media_type = "image/jpeg"
    resultado = analizar_factura(image_b64, media_type)
    data = garantizar_producto_factura(resultado)

    rut = data.get("rut_proveedor", "")
    if rut and data.get("productos"):
        data["productos"] = aplicar_fuzzy_a_productos(data["productos"], rut, threshold=85)

    print("=" * 80)
    print("OCR TEXTO CRUDO:")
    print("=" * 80)
    print(data.get("ocr_texto_crudo", "(sin texto OCR)"))
    print()
    print("=" * 80)
    print("PRODUCTOS CON FUZZY MATCHING:")
    print("=" * 80)
    for i, p in enumerate(data.get("productos") or [], 1):
        match_info = ""
        if p.get("match_type"):
            match_info = (
                f" | match={p['match_type']} score={p.get('match_score')} "
                f"interno={p.get('codigo_interno')}"
            )
            if p.get("codigo_ocr_original"):
                match_info += f" ocr_orig={p['codigo_ocr_original']}"
        print(
            f"  {i}. {p.get('codigo_proveedor', '(sin código)'):20s} "
            f"qty={p.get('cantidad')} neto={p.get('valor_neto')}{match_info}"
        )
    print()
    print("=" * 80)
    print("RESULTADO COMPLETO (sin OCR crudo):")
    print("=" * 80)
    res_copy = {k: v for k, v in data.items() if k != "ocr_texto_crudo"}
    print(json.dumps(res_copy, indent=2, ensure_ascii=False, default=str))
