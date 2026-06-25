"""Registro de parsers post-OCR por proveedor.

Cada proveedor nuevo: crear un módulo en este paquete con @registry.register.
Los parsers en invoice_vision.py no se modifican; solo se importan desde aquí.

Antes de cambiar invoice_vision o un parser, correr regresión:
  python scripts/test_invoice_parsers_regression.py
"""
from __future__ import annotations

from .base import BaseInvoiceParser
from .generico import GenericParser
from .registry import InvoiceParserRegistry, registry

# Auto-registro de proveedores (import side-effect; orden = prioridad en find()).
from . import ali_repuestos as _ali_repuestos  # noqa: F401
from . import autotec as _autotec  # noqa: F401
from . import fitalia as _fitalia  # noqa: F401
from . import xinwang as _xinwang  # noqa: F401
from . import tecnicor as _tecnicor  # noqa: F401
from . import repuesto_center as _repuesto_center  # noqa: F401
from . import huoying as _huoying  # noqa: F401
from . import acd as _acd  # noqa: F401
from . import mundo as _mundo  # noqa: F401
from . import boston as _boston  # noqa: F401

__all__ = [
    "BaseInvoiceParser",
    "GenericParser",
    "InvoiceParserRegistry",
    "registry",
]
