"""Fuzzy matching de códigos de proveedor contra el catálogo del ERP."""
from __future__ import annotations

from typing import Any

from rapidfuzz import fuzz, process

from app.bodega.models import ProveedorCodigoInterno
from app.utils.rut_utils import clean_rut


def fuzzy_match_codigo(
    codigo_ocr: str,
    rut_proveedor: str,
    threshold: int = 85,
) -> dict[str, Any] | None:
    """Busca el código más parecido en proveedor_codigo_interno del
    proveedor identificado por RUT.

    Args:
        codigo_ocr: código leído por el OCR (puede tener errores)
        rut_proveedor: RUT del proveedor (con o sin puntos/guion)
        threshold: score mínimo (0-100) para aceptar el match. 85 es
                   conservador para códigos alfanuméricos.

    Returns:
        dict con codigo_proveedor (corregido), codigo_interno y score,
        o None si no hay match suficientemente bueno.
    """
    if not codigo_ocr or not rut_proveedor:
        return None

    rut_norm = clean_rut(rut_proveedor)
    if not rut_norm:
        return None

    mapeos = ProveedorCodigoInterno.query.filter_by(
        proveedor_rut=rut_norm
    ).all()

    if not mapeos:
        return None

    dict_mapeos = {
        m.codigo_proveedor.upper().strip(): m.codigo_interno
        for m in mapeos
        if m.codigo_proveedor
    }

    if not dict_mapeos:
        return None

    codigos = list(dict_mapeos.keys())
    codigo_norm = codigo_ocr.upper().strip()

    if codigo_norm in dict_mapeos:
        return {
            "codigo_proveedor": codigo_norm,
            "codigo_interno": dict_mapeos[codigo_norm],
            "score": 100,
            "match_type": "exact",
        }

    match = process.extractOne(
        codigo_norm,
        codigos,
        scorer=fuzz.ratio,
        score_cutoff=threshold,
    )

    if not match:
        return None

    matched_codigo, score, _ = match
    return {
        "codigo_proveedor": matched_codigo,
        "codigo_interno": dict_mapeos[matched_codigo],
        "score": int(score),
        "match_type": "fuzzy",
    }


def aplicar_fuzzy_a_productos(
    productos: list[dict[str, Any]],
    rut_proveedor: str,
    threshold: int = 85,
) -> list[dict[str, Any]]:
    """Aplica fuzzy matching a una lista de productos de factura.

    Para cada producto, intenta encontrar el código correcto en BD.
    Si hay match, agrega los campos: codigo_ocr_original (preserva
    el código del OCR), codigo_interno, match_score, match_type.
    Si no hay match, mantiene el código del OCR sin cambios.
    """
    if not productos or not rut_proveedor:
        return productos

    for producto in productos:
        codigo_ocr = producto.get("codigo_proveedor", "")
        if not codigo_ocr:
            continue

        match = fuzzy_match_codigo(codigo_ocr, rut_proveedor, threshold)
        if match:
            producto["codigo_ocr_original"] = codigo_ocr
            producto["codigo_proveedor"] = match["codigo_proveedor"]
            producto["codigo_interno"] = match["codigo_interno"]
            producto["match_score"] = match["score"]
            producto["match_type"] = match["match_type"]

    return productos
