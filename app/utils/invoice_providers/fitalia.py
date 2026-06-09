from __future__ import annotations

from typing import Any

from app.utils import invoice_vision

from .base import BaseInvoiceParser
from .registry import registry


@registry.register
class FitaliaParser(BaseInvoiceParser):
    """Post-proceso Fitalia: fallback térmico, validación de precios y normalización OCR."""

    nombre = "fitalia"

    def matches(self, rut: str | None, ocr_text: str) -> bool:
        return invoice_vision._is_fitalia_invoice_text(ocr_text or "")

    def parse(self, data: dict[str, Any]) -> dict[str, Any]:
        texto = (data.get("ocr_texto_crudo") or "").strip()
        if not texto or not invoice_vision._is_fitalia_invoice_text(texto):
            return data

        texto_norm = invoice_vision._normalize_ocr_text(texto)
        productos = list(data.get("productos") or [])

        if not productos:
            productos = invoice_vision._extract_productos_fitalia_fallback(texto_norm) or []

        if productos and invoice_vision._looks_like_thermal_invoice(texto_norm):
            productos = invoice_vision._validar_consistencia_precios_termico(
                productos, texto_norm
            )

        productos = invoice_vision._normalize_fitalia_codigos_en_productos(
            productos, texto_norm
        )

        if productos:
            data["productos"] = productos
            data["productos_fuente"] = "fitalia"
            data["productos_n"] = len(productos)
            p0 = productos[0]
            data["producto_codigo"] = p0.get("codigo_proveedor")
            data["producto_cantidad"] = p0.get("cantidad")
            data["producto_valor_neto"] = p0.get("valor_neto")

        return data
