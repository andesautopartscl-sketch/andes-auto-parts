"""Normalización de teléfonos para clientes/proveedores y documentos (Chile + internacional con +)."""

from __future__ import annotations

import re


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def is_chile_pais(pais: str | None) -> bool:
    n = (pais or "").strip().lower()
    return n in {"chile", "cl", "chile (cl)"}


def phone_to_compact_e164(raw: str | None, pais_hint: str | None = None) -> str:
    """
    Guarda en BD formato compacto E.164 sin espacios, ej. +56939486438.
    - Chile (o país vacío, por compatibilidad): 9 dígitos empezando en 9 → +56...
    - Con + al inicio: se conserva el código país indicado por el usuario.
    - Otro país sin +: se antepone + a los dígitos (el usuario debe incluir código de país).
    """
    s = (raw or "").strip()
    if not s:
        return ""
    d = _digits(s)
    if not d:
        return ""
    had_plus = s.lstrip().startswith("+") or s.replace(" ", "").startswith("+")
    if had_plus:
        return "+" + d
    chile = is_chile_pais(pais_hint) or not (pais_hint or "").strip()
    if chile:
        if len(d) == 9 and d[0] == "9":
            return "+56" + d
        if len(d) == 11 and d.startswith("569"):
            return "+" + d
        if len(d) == 11 and d.startswith("56") and d[2] == "9":
            return "+" + d
        if len(d) >= 11 and d.startswith("56"):
            return "+" + d
    return "+" + d


def format_phone_display(stored: str | None) -> str:
    """Presentación legible, ej. +56 9 3948 6438 (Chile móvil)."""
    raw = (stored or "").strip()
    if not raw:
        return ""
    d = _digits(raw)
    if not d:
        return raw
    compact = "+" + d
    return _format_compact_nice(compact)


def _format_compact_nice(compact_plus_digits: str) -> str:
    d = _digits(compact_plus_digits)
    if not d:
        return ""
    if d.startswith("56"):
        nat = d[2:]
        if len(nat) == 9 and nat[0] == "9":
            return f"+56 {nat[0]} {nat[1:5]} {nat[5:9]}"
        if len(nat) == 9:
            return f"+56 {nat[0]} {nat[1:5]} {nat[5:9]}"
        if len(nat) == 10:
            return f"+56 {nat[:2]} {nat[2:6]} {nat[6:10]}"
        return "+56 " + nat
    if d.startswith("1") and len(d) == 11:
        return f"+1 {d[1:4]} {d[4:7]} {d[7:11]}"
    return _intl_space_generic(d)


def _intl_space_generic(d: str) -> str:
    if len(d) <= 3:
        return "+" + d
    cc_len = 2 if len(d) >= 11 else 1
    cc = d[:cc_len]
    r = d[cc_len:]
    parts: list[str] = [f"+{cc}"]
    while r:
        if len(r) == 4:
            parts.append(r)
            break
        take = min(4, len(r)) if len(r) % 3 == 1 and len(r) > 4 else min(3, len(r))
        if take <= 0:
            parts.append(r)
            break
        parts.append(r[:take])
        r = r[take:]
    return " ".join(parts)
