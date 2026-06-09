from __future__ import annotations

from .base import BaseInvoiceParser
from .generico import GenericParser


class InvoiceParserRegistry:
    def __init__(self) -> None:
        self._parsers: list[BaseInvoiceParser] = []

    def register(self, parser_cls: type[BaseInvoiceParser] | None = None):
        """Decorador: @registry.register sobre una subclase de BaseInvoiceParser."""

        def _register(cls: type[BaseInvoiceParser]) -> type[BaseInvoiceParser]:
            self._parsers.append(cls())
            return cls

        if parser_cls is not None:
            return _register(parser_cls)
        return _register

    def find(self, rut: str | None, ocr_text: str) -> BaseInvoiceParser:
        """Primer parser que matchea; GenericParser si ninguno aplica."""
        text = ocr_text or ""
        rut_s = rut or ""
        for parser in self._parsers:
            if parser.matches(rut_s, text):
                return parser
        return GenericParser()

    def list_providers(self) -> list[str]:
        return [p.nombre for p in self._parsers]


registry = InvoiceParserRegistry()
