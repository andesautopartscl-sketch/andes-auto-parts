"""Regresión parser OCR Importadora ACD (Facto.cl, RUT 77.822.487-9)."""
from __future__ import annotations

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.utils.invoice_providers.acd import AcdParser, is_acd_invoice

OCR_FIXTURE_270 = """
IMPORTADORA Y EXPORTADORA ACD LIMITADA
MANTENIMIENTO Y REPARACION DE VEHICULOS AUTOMOTORES
Casa Matriz: Av. Santa Rosa 693, Santiago, SANTIAGO .Fono: 990704250 Email:
RepuestosgrupoACD@outlook.com
RUT: 77.822.487-9
FACTURA ELECTRONICA
N 270
S.I.I. SANTIAGO CENTRO
Señor(es)
RUT
ANDES AUTO PARTS LIMITADA
78074288-7
Fecha
Dirección
24-06-2026
LA CONCEPCION 81, OFICINA 214
Ciudad
SANTIAGO
Giro
VENTA DE PARTES DE PIEZAS ILIMITADA
Comuna
Providencia
Vendedor
DAVIS ARREDONDO
Condiciones de pago
Contado
Glosa
Cantidad Prc.Unit
Desc/Rcrg Afecto IVA Imp.Esp.
Monto
BISEL CROMADO DE NEBLINERO IZQ. CHERY
TIGGO 8 PRO MAX
1 UN
$33.613,45
SI
$33.613
Descuento afectos $
-$7.571
Monto Neto
$26.042
Monto Exento
IVA 19%
Total
$4.949
$30.991
Sistema de gestión www.Facto.cl
"""

OCR_FIXTURE_273 = """
IMPORTADORA Y EXPORTADORA ACD LIMITADA
MANTENIMIENTO Y REPARACION DE VEHICULOS AUTOMOTORES
RUT: 77.822.487-9
FACTURA ELECTRONICA
N 273
Condiciones de pago
Contado
Glosa
Cantidad Pr. Unit.
Desc./Rec. Afecto IVA Imp. Esp.
Monto
BISEL CROMADO DE NEBLINERO IZQ CHERY TIGGO 8 PRO MAX
1 UN
$26.042,00
0
SI
$26.042
Descuento afecto
$0
Monto Neto
$26.042
IVA 19%
$4.949
Total
$30.991
"""


def test_fixture_270() -> None:
    parser = AcdParser()
    assert is_acd_invoice("77.822.487-9", OCR_FIXTURE_270)
    data = parser.parse(
        {
            "rut_proveedor": "77.822.487-9",
            "ocr_texto_crudo": OCR_FIXTURE_270,
            "productos": [],
            "total_neto": 26042,
            "iva": 4949,
            "total": 30991,
        }
    )
    productos = data.get("productos") or []
    assert len(productos) == 1, productos
    p = productos[0]
    assert p.get("cantidad") == 1
    desc = (p.get("descripcion") or "").lower()
    assert "bisel cromado" in desc
    assert "cantidad prc.unit" not in desc
    assert "desc/rcrg" not in desc
    assert p.get("valor_neto") == 26042, p
    assert data.get("numero_documento") == "270", data.get("numero_documento")
    assert data.get("metodo_pago") == "contado"
    assert data.get("productos_fuente") == "acd_glosa"


def test_fixture_273() -> None:
    parser = AcdParser()
    data = parser.parse(
        {
            "rut_proveedor": "77.822.487-9",
            "ocr_texto_crudo": OCR_FIXTURE_273,
            "productos": [],
            "numero_documento": "273",
            "total_neto": 26042,
            "iva": 4949,
            "total": 30991,
        }
    )
    productos = data.get("productos") or []
    assert len(productos) == 1, productos
    p = productos[0]
    assert p.get("cantidad") == 1
    assert p.get("valor_neto") == 26042, p


OCR_FIXTURE_MERGED_HEADER = """
IMPORTADORA Y EXPORTADORA ACD LIMITADA
RUT: 77.822.487-9
Glosa
Desc/Rcrg Afecto IVA Imp.Esp. Monto BISEL CROMADO DE NEBLINERO IZQ. CHERY
TIGGO 8 PRO MAX
1 UN
$26.042,00
SI
$26.042
Monto Neto
$26.042
Total
$30.991
"""


def test_fixture_merged_header_line() -> None:
    parser = AcdParser()
    data = parser.parse(
        {
            "rut_proveedor": "77.822.487-9",
            "ocr_texto_crudo": OCR_FIXTURE_MERGED_HEADER,
            "productos": [],
            "total_neto": 26042,
        }
    )
    productos = data.get("productos") or []
    assert len(productos) == 1, productos
    desc = (productos[0].get("descripcion") or "").lower()
    assert desc.startswith("bisel cromado")
    assert "desc/rcrg" not in desc


OCR_FIXTURE_DUP_ROWS = """
IMPORTADORA Y EXPORTADORA ACD LIMITADA
RUT: 77.822.487-9
Glosa
BISEL CROMADO DE NEBLINERO IZQ CHERY TIGGO 8 PRO MAX
1 UN
$26.042,00
SI
$26.042
BISEL CROMADO DE NEBLINERO IZQ CHERY TIGGO 8 PRO MAX
1 UN
$26.042,00
SI
$26.042
Monto Neto
$52.084
Total
$61.980
"""


OCR_FIXTURE_270_DUP_OCR = """
IMPORTADORA Y EXPORTADORA ACD LIMITADA
RUT: 77.822.487-9
FACTURA ELECTRONICA
N 270
Glosa
BISEL CROMADO DE NEBLINERO IZQ CHERY TIGGO 8 PRO MAX
1 UN
$26.042,00
SI
$26.042
BISEL CROMADO DE NEBLINERO IZQ CHERY TIGGO 8 PRO MAX
1 UN
$26.042,00
SI
$26.042
Monto Neto
$26.042
Total
$30.991
"""


OCR_FIXTURE_270_DISCOUNT_DUP_DESC = """
IMPORTADORA Y EXPORTADORA ACD LIMITADA
RUT: 77.822.487-9
FACTURA ELECTRONICA
N 270
Glosa
Cantidad Prc.Unit
Desc/Rcrg Afecto IVA Imp.Esp.
Monto
BISEL CROMADO DE NEBLINERO IZQ. CHERY
TIGGO 8 PRO MAX
1 UN
$33.613,45
SI
$33.613
BISEL CROMADO DE NEBLINERO IZQ CHERY TIGGO 8 PRO MAX
1 UN
$33.613,45
SI
$33.613
Descuento afectos $
-$7.571
Monto Neto
$26.042
IVA 19%
$4.949
Total
$30.991
"""


def test_fixture_270_discount_dup_desc() -> None:
    """Dos bloques OCR con glosa casi igual y precio bruto → un ítem neto 26042."""
    parser = AcdParser()
    data = parser.parse(
        {
            "rut_proveedor": "77.822.487-9",
            "ocr_texto_crudo": OCR_FIXTURE_270_DISCOUNT_DUP_DESC,
            "productos": [],
            "total_neto": 26042,
            "iva": 4949,
            "total": 30991,
        }
    )
    productos = data.get("productos") or []
    assert len(productos) == 1, productos
    assert productos[0].get("cantidad") == 1
    assert productos[0].get("valor_neto") == 26042
    assert data.get("numero_documento") == "270"


OCR_FIXTURE_270_BROKEN_FOOTER_LABEL = """
IMPORTADORA Y EXPORTADORA ACD LIMITADA
RUT: 77.822.487-9
FACTURA ELECTRONICA
N 270
Glosa
BISEL CROMADO DE NEBLINERO IZQ. CHERY TIGGO 8 PRO MAX
1 UN
$33.613,45
SI
$33.613
BISEL CROMADO DE NEBLINERO IZQ CHERY TIGGO 8 PRO MAX
1 UN
$33.613,45
SI
$33.613
Descuento afectos
-$7.571
Neto documento
$26.042
Impuesto
$4.949
Total
$30.991
"""


def test_fixture_270_broken_footer_label_wrong_neto() -> None:
    """Pie sin «Monto Neto» legible y neto global inflado → triplete 26042/4949/30991."""
    parser = AcdParser()
    data = parser.parse(
        {
            "rut_proveedor": "77.822.487-9",
            "ocr_texto_crudo": OCR_FIXTURE_270_BROKEN_FOOTER_LABEL,
            "productos": [],
            "total_neto": 67226,
            "iva": 12773,
            "total": 79999,
        }
    )
    productos = data.get("productos") or []
    assert len(productos) == 1, productos
    assert productos[0].get("valor_neto") == 26042
    assert data.get("total_neto") == 26042
    assert data.get("total") == 30991


def test_fixture_270_wrong_global_neto() -> None:
    """OCR global infiere neto=67226 (2×bruto); el pie Facto debe imponer 26042 y 1 ítem."""
    parser = AcdParser()
    data = parser.parse(
        {
            "rut_proveedor": "77.822.487-9",
            "ocr_texto_crudo": OCR_FIXTURE_270_DISCOUNT_DUP_DESC,
            "productos": [],
            "total_neto": 67226,
            "iva": 12773,
            "total": 79999,
        }
    )
    productos = data.get("productos") or []
    assert len(productos) == 1, productos
    assert productos[0].get("cantidad") == 1
    assert productos[0].get("valor_neto") == 26042
    assert data.get("total_neto") == 26042
    assert data.get("total") == 30991


def test_fixture_ocr_duplicate_single_item() -> None:
    """OCR duplica la misma fila pero neto documento es de un solo ítem → cantidad 1."""
    parser = AcdParser()
    data = parser.parse(
        {
            "rut_proveedor": "77.822.487-9",
            "ocr_texto_crudo": OCR_FIXTURE_270_DUP_OCR,
            "productos": [],
            "total_neto": 26042,
        }
    )
    productos = data.get("productos") or []
    assert len(productos) == 1, productos
    assert productos[0].get("cantidad") == 1
    assert productos[0].get("valor_neto") == 26042
    assert data.get("numero_documento") == "270"


def test_fixture_duplicate_rows_merge() -> None:
    """Dos filas idénticas (OCR/UI duplicado) → un ítem cantidad 2."""
    parser = AcdParser()
    data = parser.parse(
        {
            "rut_proveedor": "77.822.487-9",
            "ocr_texto_crudo": OCR_FIXTURE_DUP_ROWS,
            "productos": [],
            "total_neto": 52084,
        }
    )
    productos = data.get("productos") or []
    assert len(productos) == 1, productos
    assert productos[0].get("cantidad") == 2
    assert productos[0].get("valor_neto") == 26042


OCR_FIXTURE_NO_GLOSA = """
IMPORTADORA Y EXPORTADORA ACD LIMITADA
RUT: 77.822.487-9
FACTURA ELECTRONICA
N 273
Condiciones de pago
Contado
BISEL CROMADO DE NEBLINERO IZQ CHERY TIGGO 8 PRO MAX
1 UN
$26.042,00
SI
$26.042
Monto Neto
$26.042
IVA 19%
$4.949
Total
$30.991
"""


def test_fixture_no_glosa_header() -> None:
    parser = AcdParser()
    data = parser.parse(
        {
            "rut_proveedor": "77.822.487-9",
            "ocr_texto_crudo": OCR_FIXTURE_NO_GLOSA,
            "productos": [],
            "total_neto": 26042,
        }
    )
    productos = data.get("productos") or []
    assert len(productos) == 1, productos
    assert productos[0].get("valor_neto") == 26042


def test_live_image_if_present() -> None:
    img = Path(
        r"C:\Users\alber\.cursor\projects\c-AndesAutoParts\assets"
        r"\c__Users_alber_AppData_Roaming_Cursor_User_workspaceStorage_360ee4fa053714ac61fb8005423cab79_images_image-95a4a01f-9d13-45f3-bbbf-e7a16c6ee2d5.png"
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
        assert p.nombre == "acd", p.nombre
        d = p.parse(d)
        assert len(d.get("productos") or []) >= 1, d.get("productos")
        print("OK acd live image", d.get("productos"))


if __name__ == "__main__":
    test_fixture_270()
    test_fixture_273()
    test_fixture_merged_header_line()
    test_fixture_270_discount_dup_desc()
    test_fixture_270_broken_footer_label_wrong_neto()
    test_fixture_270_wrong_global_neto()
    test_fixture_ocr_duplicate_single_item()
    test_fixture_duplicate_rows_merge()
    test_fixture_no_glosa_header()
    print("OK acd fixtures")
