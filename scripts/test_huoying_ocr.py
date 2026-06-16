"""Regresión parser OCR Huoying (factura imagen 39039)."""
from __future__ import annotations

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.utils.invoice_providers.huoying import HuoyingParser, is_huoying_invoice

OCR_FIXTURE_39039 = """
COMERCIALIZADORA HUOYING LIMITADA
HUOYING
REPUESTOS
Giro: COMPRA Y VENTAS DE REPUESTOS DE
AUTOMOVILES Y ARTESANIA
LIRA 997- SANTIAGO
eMail: aolivera@fem.cl Telefono: 2 56994417747
TIPO DE VENTA: DEL GIRO
SEÑOR(ES): ANDES AUTO PARTS LIMITADA
R.U.T.:
78.074.288-7
Codigo
Descripcion
balancin maxus g10
Forma de Pago:Contado
Timbre Electrónico SII
Res.86 de 2005 Verifique documento: www.sii.cl
R.U.T.:76.272.243-7
FACTURA ELECTRONICA
N39039
S.I.I. SANTIAGO CENTRO
Fecha Emision: 16 de Junio del 2026
Cantidad
Precio
Adic.*
%Impto Desc.
Valor
3
10.084
20.00
24.202
MONTO NETO $ 24.202
I.V.A. 19% $ 4.598
IMPUESTO ADICIONAL $ 0
TOTAL $ 28.800
"""


def test_fixture_39039() -> None:
    parser = HuoyingParser()
    assert is_huoying_invoice("76.272.243-7", OCR_FIXTURE_39039)
    data = parser.parse(
        {
            "rut_proveedor": "76.272.243-7",
            "ocr_texto_crudo": OCR_FIXTURE_39039,
            "productos": [],
            "numero_documento": "39039",
            "total_neto": 24202,
            "iva": 4598,
            "total": 28800,
        }
    )
    productos = data.get("productos") or []
    assert len(productos) == 1, productos
    p = productos[0]
    assert p.get("cantidad") == 3
    assert p.get("descripcion", "").lower().startswith("balancin maxus")
    vn = p.get("valor_neto")
    assert vn is not None and abs(float(vn) * 3 - 24202) < 0.01, vn
    assert data.get("metodo_pago") == "contado"
    assert data.get("productos_fuente") == "huoying_columnar"
    print("OK huoying fixture 39039\n")


def test_live_image_if_present() -> None:
    img = Path(
        r"C:\Users\alber\.cursor\projects\c-AndesAutoParts\assets"
        r"\c__Users_alber_AppData_Roaming_Cursor_User_workspaceStorage_360ee4fa053714ac61fb8005423cab79_images_WhatsApp_Image_2026-06-16_at_12.45.28-6ef3d068-90f6-4304-af39-df739aeac95b.png"
    )
    if not img.is_file():
        print("SKIP live image (not in workspace)\n")
        return

    from app import create_app
    from app.utils.invoice_vision import analizar_factura, garantizar_producto_factura
    from app.utils.invoice_providers import registry

    app = create_app()
    with app.app_context():
        b64 = base64.b64encode(img.read_bytes()).decode()
        d = garantizar_producto_factura(analizar_factura(b64, "image/png"))
        p = registry.find(d.get("rut_proveedor"), d.get("ocr_texto_crudo") or "")
        assert p.nombre == "huoying", p.nombre
        d = p.parse(d)
        assert len(d.get("productos") or []) >= 1
        print("OK huoying live image", d.get("productos"))


if __name__ == "__main__":
    test_fixture_39039()
    test_live_image_if_present()
