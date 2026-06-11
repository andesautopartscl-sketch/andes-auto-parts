"""Registro de parsers post-OCR por proveedor.

Cada proveedor nuevo: crear un módulo en este paquete con @registry.register.
Los parsers en invoice_vision.py no se modifican; solo se importan desde aquí.
"""
from __future__ import annotations

from .base import BaseInvoiceParser
from .generico import GenericParser
from .registry import InvoiceParserRegistry, registry

# Auto-registro de proveedores (import side-effect; orden = prioridad en find()).
from . import ali_repuestos as _ali_repuestos  # noqa: F401
from . import autotec as _autotec  # noqa: F401
from . import fitalia as _fitalia  # noqa: F401
from . import mundo as _mundo  # noqa: F401

__all__ = [
    "BaseInvoiceParser",
    "GenericParser",
    "InvoiceParserRegistry",
    "registry",
]
