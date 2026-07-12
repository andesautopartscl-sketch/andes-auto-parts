"""Etiquetas mobile: reutiliza generación del módulo Bodega."""
from __future__ import annotations

from flask import session

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

SESSION_PENDING_KEY = "mobile_etiquetas_pending"


def guardar_pending(codigos_raw: str, print_mode: str) -> None:
    """Conserva códigos tras vista previa (fallback si el POST/GET de impresión llega vacío)."""
    if not (codigos_raw or "").strip():
        return
    session[SESSION_PENDING_KEY] = {
        "codigos": codigos_raw.strip(),
        "print_mode": (print_mode or "a4").strip(),
    }


def leer_pending() -> tuple[str, str]:
    data = session.get(SESSION_PENDING_KEY) or {}
    return (data.get("codigos") or "").strip(), (data.get("print_mode") or "a4").strip()


def puede_imprimir_etiquetas(user: str | None, rol: str | None) -> bool:
    return has_permission(user, rol, "bodega_etiquetas")


def generar_etiquetas(codigos_raw: str, fp: str = "") -> tuple[list[dict], list[str]]:
    codes = _parse_codigos(codigos_raw)
    labels, missing = _build_labels_from_codes(codes, fp)
    return labels, missing


def registrar_impresion(labels: list[dict]) -> None:
    if labels:
        _registrar_historial_etiquetas(labels)
