from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseInvoiceParser(ABC):
    """Contrato para post-procesar el dict devuelto por analizar_factura()."""

    nombre: str

    @abstractmethod
    def matches(self, rut: str | None, ocr_text: str) -> bool:
        """True si este parser aplica al RUT y/o texto OCR."""

    @abstractmethod
    def parse(self, data: dict[str, Any]) -> dict[str, Any]:
        """Refina productos y metadatos del análisis OCR."""
