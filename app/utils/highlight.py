"""Resaltado del término buscado dentro de un texto, seguro para plantillas."""
from __future__ import annotations

import re

from markupsafe import Markup, escape

# Mismos separadores que usa la búsqueda de productos.
_SEPARADORES = re.compile(r"[\s,;]+")


def highlight_match(value, term) -> Markup:
    """Envuelve en <mark> las coincidencias de `term`, escapando siempre el texto."""
    texto = "" if value is None else str(value)
    buscado = (term or "").strip()
    if not buscado:
        return escape(texto)

    tokens = [re.escape(t) for t in _SEPARADORES.split(buscado.lower()) if t]
    if not tokens:
        return escape(texto)

    patron = re.compile("(" + "|".join(tokens) + ")", re.IGNORECASE)
    # Se escapa antes de insertar las marcas: el término viene del usuario.
    escapado = str(escape(texto))
    return Markup(patron.sub(r"<mark>\1</mark>", escapado))
