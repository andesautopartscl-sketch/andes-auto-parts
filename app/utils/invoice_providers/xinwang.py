from __future__ import annotations

import re
from typing import Any

from app.utils import invoice_vision

from .base import BaseInvoiceParser
from .registry import registry


@registry.register
class XinwangParser(BaseInvoiceParser):
    """Facturas Xinwang / Xingwang: ítems sin código, layout columnar intercalado."""

    nombre = "xinwang"

    def matches(self, rut: str | None, ocr_text: str) -> bool:
        texto = (ocr_text or "").strip()
        if not texto:
            return False
        texto_norm = invoice_vision._normalize_ocr_text(texto)
        if re.search(r"xin\s*wang|xing\s*wang", texto_norm, re.IGNORECASE):
            return True
        lines = [ln.strip() for ln in texto_norm.splitlines() if ln.strip()]
        return invoice_vision._has_xinwang_column_layout(lines)

    def parse(self, data: dict[str, Any]) -> dict[str, Any]:
        texto = (data.get("ocr_texto_crudo") or "").strip()
        if not texto:
            return data

        texto_norm = invoice_vision._normalize_ocr_text(texto)
        lines = [ln.strip() for ln in texto_norm.splitlines() if ln.strip()]
        if not invoice_vision._is_xinwang_invoice_text(lines):
            return data
        if invoice_vision._is_autotec_invoice_text(texto_norm, data.get("rut_proveedor")):
            return data

        rebuilt: list[dict[str, Any]] = []
        if invoice_vision._has_xinwang_column_layout(lines):
            rebuilt = invoice_vision._extract_productos_sin_codigo_xinwang(lines)
        if not rebuilt:
            rebuilt = invoice_vision._xinwang_fallback_from_totals(
                lines, data.get("total_neto")
            )
            if rebuilt:
                data["productos_fuente"] = "xinwang_fallback_neto"
        if rebuilt:
            data["productos"] = rebuilt
            if not data.get("productos_fuente"):
                data["productos_fuente"] = "xinwang_sin_codigo"
            data["productos_n"] = len(rebuilt)
            p0 = rebuilt[0]
            data["producto_codigo"] = p0.get("codigo_proveedor")
            data["producto_cantidad"] = p0.get("cantidad")
            data["producto_valor_neto"] = p0.get("valor_neto")

        return data
