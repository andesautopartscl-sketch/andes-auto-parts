"""Parser de códigos escaneados (QR / barcode) — alineado a etiquetas Andes."""
from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

from app.extensions import db
from app.productos.routes import _find_producto_by_codigo


_CODIGO_RE = re.compile(r"^[A-Za-z0-9._\-/]+$")


def parse_qr_payload(raw: str) -> str | None:
    """
    Extrae código interno desde QR de etiquetas Andes.

    - Bodega (etiquetas retail): texto plano con el código (generar_qr en bodega/routes).
    - Admin (etiqueta_print): URL host + 'producto/' + código.
    """
    text = unquote((raw or "").strip())
    if not text:
        return None

    lower = text.lower()
    for marker in ("/m/producto/", "/producto/"):
        idx = lower.find(marker)
        if idx >= 0:
            rest = text[idx + len(marker) :]
            codigo = rest.split("?")[0].split("#")[0].strip("/")
            if codigo:
                return codigo.strip().upper()

    if "://" in text:
        try:
            path = urlparse(text).path or ""
            parts = [p for p in path.split("/") if p]
            if len(parts) >= 2 and parts[-2].lower() == "producto":
                return parts[-1].strip().upper()
        except Exception:
            pass

    if _CODIGO_RE.match(text):
        return text.strip().upper()

    cleaned = text.strip().upper()
    return cleaned if cleaned else None


def normalize_barcode_payload(raw: str) -> str | None:
    """Code128/EAN en etiquetas Andes codifican el código interno del producto."""
    text = (raw or "").strip()
    if not text:
        return None
    return text.strip().upper()


def producto_existe(codigo_raw: str) -> dict:
    """Validación rápida para API del escáner."""
    codigo = (codigo_raw or "").strip().upper()
    if not codigo:
        return {"exists": False, "codigo": ""}
    producto = _find_producto_by_codigo(db.session, codigo)
    if producto is None or producto.activo is False:
        return {"exists": False, "codigo": codigo}
    return {
        "exists": True,
        "codigo": (producto.codigo or codigo).strip(),
        "descripcion": (producto.descripcion or "").strip(),
        "marca": (producto.marca or "").strip(),
    }
