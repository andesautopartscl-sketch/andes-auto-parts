from __future__ import annotations

from typing import Any

from app.utils import invoice_vision

from .base import BaseInvoiceParser
from .registry import registry


@registry.register
class AutotecParser(BaseInvoiceParser):
    """Post-proceso Autotec: delega en reparar_productos_autotec_factura sin modificarla."""

    nombre = "autotec"

    def matches(self, rut: str | None, ocr_text: str) -> bool:
        return invoice_vision._is_autotec_invoice_text(ocr_text or "", rut)

    def parse(self, data: dict[str, Any]) -> dict[str, Any]:
        return invoice_vision.reparar_productos_autotec_factura(data)
