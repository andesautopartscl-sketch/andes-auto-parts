"""Normalización de campos de clientes/proveedores (texto y email)."""


def party_text_upper(raw: str | None) -> str:
    return (raw or "").strip().upper()


def normalize_party_email(raw: str | None) -> str:
    e = (raw or "").strip()
    return e.lower() if e else ""
