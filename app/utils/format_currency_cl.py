"""Formato de precios en pesos chilenos (IVA 19 % para mostrador)."""

from __future__ import annotations

# Precio en productos / ingresos: neto (sin IVA); precio al cliente típico incluye IVA.
IVA_CHILE = 0.19


def precio_neto_a_bruto_clp(precio_neto: object) -> int | None:
    """Convierte precio neto guardado en BD a precio con IVA, en pesos enteros."""
    if precio_neto is None:
        return None
    try:
        v = float(precio_neto)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    # Enteros CLP: mismo efecto que neto×1,19 y pasar a peso (coincide con ej. 42.009 → 49.990).
    return int(v * (1.0 + IVA_CHILE) + 1e-9)


def format_precio_publico_con_iva(precio_neto: object) -> str:
    """
    Para listados (ej. buscar productos): muestra precio al público con IVA.
    Formato: $49.990 (punto como separador de miles).
    """
    bruto = precio_neto_a_bruto_clp(precio_neto)
    if bruto is None:
        return "$0"
    s = "{:,.0f}".format(bruto).replace(",", ".")
    return f"${s}"
