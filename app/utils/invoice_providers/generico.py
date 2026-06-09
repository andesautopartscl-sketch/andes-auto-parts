from __future__ import annotations

from typing import Any

from .base import BaseInvoiceParser


class GenericParser(BaseInvoiceParser):
    """Fallback: no aplica post-proceso específico de proveedor."""

    nombre = "generico"

    def matches(self, rut: str | None, ocr_text: str) -> bool:
        return True

    def parse(self, data: dict[str, Any]) -> dict[str, Any]:
        return data
