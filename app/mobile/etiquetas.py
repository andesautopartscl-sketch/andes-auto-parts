"""Etiquetas mobile: reutiliza generación del módulo Bodega."""
from __future__ import annotations

from app.bodega.routes import (
    _build_labels_from_codes,
    _parse_codigos,
    _registrar_historial_etiquetas,
)
from app.utils.permissions import has_permission


PRINT_MODES = [
    {"value": "a4", "label": "A4"},
    {"value": "thermal", "label": "Térmica 60×40"},
    {"value": "thermal_100x150", "label": "Térmica 100×150"},
]


def puede_imprimir_etiquetas(user: str | None, rol: str | None) -> bool:
    return has_permission(user, rol, "bodega_etiquetas")


def generar_etiquetas(codigos_raw: str, fp: str = "") -> tuple[list[dict], list[str]]:
    codes = _parse_codigos(codigos_raw)
    labels, missing = _build_labels_from_codes(codes, fp)
    return labels, missing


def registrar_impresion(labels: list[dict]) -> None:
    if labels:
        _registrar_historial_etiquetas(labels)
