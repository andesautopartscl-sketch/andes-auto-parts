"""Filtro Jinja para mostrar metadata de auditoría en español (no JSON crudo)."""

import json
from typing import Any

from markupsafe import Markup, escape

_LABELS = {
    "source": "Origen",
    "q": "Búsqueda",
    "page": "Página",
    "per_page": "Resultados por página",
    "total_count": "Total encontrados",
    "count": "Cantidad",
    "codigos": "Códigos",
    "changed_fields": "Campos modificados",
    "tipo": "Tipo",
    "cantidad_movimiento": "Cantidad movimiento",
    "stock_total_actual": "Stock total actual",
    "marca": "Marca",
    "bodega": "Bodega",
    "proveedor": "Proveedor",
    "observacion": "Observación",
    "imagenes_count": "Imágenes adjuntas",
    "modelo": "Modelo",
}

_SOURCE_DISPLAY = {
    "ficha_modal": "Ficha técnica (modal)",
    "search": "Buscador",
}


def _fmt_val(key: str, val: Any) -> str:
    if key == "source" and isinstance(val, str):
        return _SOURCE_DISPLAY.get(val, val)
    if isinstance(val, (list, dict)):
        try:
            return json.dumps(val, ensure_ascii=False)
        except Exception:
            return str(val)
    return str(val)


def format_audit_metadata(raw: str | None) -> Markup:
    if raw is None or not str(raw).strip():
        return Markup("—")
    s = str(raw).strip()
    try:
        data = json.loads(s)
    except Exception:
        return Markup(f'<span class="audit-meta-raw">{escape(s[:400])}</span>')

    if not isinstance(data, dict):
        return Markup(escape(s))

    parts: list[str] = []
    for key in sorted(data.keys()):
        label = _LABELS.get(key, key.replace("_", " ").title())
        val = _fmt_val(key, data[key])
        parts.append(
            f'<span class="audit-meta-item"><span class="audit-meta-k">{escape(label)}:</span> '
            f'<span class="audit-meta-v">{escape(val)}</span></span>'
        )

    if not parts:
        return Markup("—")

    return Markup('<span class="audit-meta">' + " ".join(parts) + "</span>")
