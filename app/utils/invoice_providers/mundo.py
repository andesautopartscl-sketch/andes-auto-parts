from __future__ import annotations

from typing import Any

from app.utils import invoice_vision

from .base import BaseInvoiceParser
from .registry import registry


@registry.register
class MundoParser(BaseInvoiceParser):
    """Post-proceso columnar (Mundo Repuestos y DTE similares): re-extrae ítems del bundle columnar."""

    nombre = "mundo"

    def matches(self, rut: str | None, ocr_text: str) -> bool:
        texto = ocr_text or ""
        if not texto.strip():
            return False
        texto_norm = invoice_vision._normalize_ocr_text(texto)
        lines = [ln.strip() for ln in texto_norm.splitlines() if ln.strip()]
        if invoice_vision._has_xinwang_column_layout(lines):
            return False
        from .tecnicor import is_tecnicor_invoice
        from .repuesto_center import is_repuesto_center_invoice
        from .huoying import is_huoying_invoice

        if is_tecnicor_invoice(rut, texto_norm):
            return False
        if is_repuesto_center_invoice(rut, texto_norm):
            return False
        if is_huoying_invoice(rut, texto_norm):
            return False
        return invoice_vision._looks_like_columnar_invoice(texto_norm, lines)

    def parse(self, data: dict[str, Any]) -> dict[str, Any]:
        texto = (data.get("ocr_texto_crudo") or "").strip()
        if not texto:
            return data

        texto_norm = invoice_vision._normalize_ocr_text(texto)
        lines = [ln.strip() for ln in texto_norm.splitlines() if ln.strip()]
        if not invoice_vision._looks_like_columnar_invoice(texto_norm, lines):
            return data
        if invoice_vision._is_autotec_invoice_text(texto_norm, data.get("rut_proveedor")):
            return data
        if invoice_vision._has_xinwang_column_layout(lines):
            return data

        rebuilt = invoice_vision._extract_productos_columnar_bundle(texto_norm, lines)
        if rebuilt:
            data["productos"] = rebuilt
            data["productos_fuente"] = "mundo_columnar"
            data["productos_n"] = len(rebuilt)
            p0 = rebuilt[0]
            data["producto_codigo"] = p0.get("codigo_proveedor")
            data["producto_cantidad"] = p0.get("cantidad")
            data["producto_valor_neto"] = p0.get("valor_neto")

        return data
