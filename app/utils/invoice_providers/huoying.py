"""Parser OCR Huoying (COMERCIALIZADORA HUOYING LIMITADA, RUT 76.272.243-7).

Facturas DTE en imagen: columnas verticales (descripción arriba, cantidad/precio/valor
debajo del timbre). Código suele ser «-»; el ítem va en descripción.
"""
from __future__ import annotations

import re
from typing import Any

from app.utils import invoice_vision

from .base import BaseInvoiceParser
from .registry import registry

_HUOYING_RUT = "76.272.243-7"
_DESC_STOP_RE = re.compile(
    r"^(forma\s+de\s+pago|timbre|res\.\d+|r\.?\s*u\.?\s*t\.?|factura|fecha\s+emision|"
    r"cantidad|precio|valor|monto\s+neto|total|s\.i\.i)",
    re.IGNORECASE,
)
_QTY_LINE_RE = re.compile(r"^\d{1,3}$")
_DISCOUNT_RE = re.compile(r"^\d{1,2}[.,]\d{2}$")


def _norm_rut(rut: str | None) -> str:
    return re.sub(r"[^0-9kK]", "", (rut or "")).upper()


def is_huoying_invoice(rut: str | None, ocr_text: str) -> bool:
    if _norm_rut(rut) == _norm_rut(_HUOYING_RUT):
        return True
    t = ocr_text or ""
    return bool(re.search(r"huoying|comercializadora\s+huoying", t, re.IGNORECASE))


def _is_desc_stop(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return True
    if _DESC_STOP_RE.match(s):
        return True
    if s in ("-", "—"):
        return True
    if re.fullmatch(r"\d{1,3}", s):
        return True
    if invoice_vision._is_chilean_price_line(s):
        return True
    return False


def _extract_descriptions(lines: list[str]) -> list[str]:
    """Texto bajo cabecera Descripcion (puede haber varias líneas / ítems)."""
    start: int | None = None
    for i, line in enumerate(lines):
        if re.fullmatch(r"descripcion|descripción", line.strip(), re.IGNORECASE):
            start = i + 1
            break
    if start is None:
        return []

    descs: list[str] = []
    buf: list[str] = []
    for line in lines[start:]:
        s = line.strip()
        if _is_desc_stop(s):
            if buf:
                descs.append(" ".join(buf))
                buf = []
            if re.fullmatch(r"cantidad", s, re.IGNORECASE):
                break
            continue
        if invoice_vision._looks_like_product_description(s):
            if buf:
                descs.append(" ".join(buf))
                buf = []
            buf.append(s)
        elif buf and re.search(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]", s):
            buf.append(s)
    if buf:
        descs.append(" ".join(buf))
    return descs


def _extract_value_rows(lines: list[str]) -> list[tuple[int, int, int | None, int]]:
    """Tras cabecera Valor: bloques cantidad, precio unit., (desc%), total línea."""
    valor_idx: int | None = None
    for i, line in enumerate(lines):
        if re.fullmatch(r"valor", line.strip(), re.IGNORECASE):
            valor_idx = i
    if valor_idx is None:
        cant_idx: int | None = None
        for i, line in enumerate(lines):
            if re.fullmatch(r"cantidad", line.strip(), re.IGNORECASE):
                cant_idx = i
        if cant_idx is None:
            return []
        valor_idx = cant_idx

    rows: list[tuple[int, int, int | None, int]] = []
    pending_qty: int | None = None
    pending_price: int | None = None
    pending_disc: int | None = None

    for line in lines[valor_idx + 1 :]:
        s = line.strip()
        if not s:
            continue
        if re.search(r"monto\s+neto|i\.?\s*v\.?\s*a|total\s*\$", s, re.IGNORECASE):
            break

        if _QTY_LINE_RE.fullmatch(s):
            if pending_qty is not None and pending_price is not None:
                total = pending_disc if pending_disc and pending_disc > pending_price else pending_price
                if pending_qty > 1 and pending_price:
                    rows.append((pending_qty, pending_price, None, total))
            pending_qty = int(s)
            pending_price = None
            pending_disc = None
            continue

        if not invoice_vision._is_chilean_price_line(s):
            continue

        val = invoice_vision._parse_monto_chileno(s)
        if val is None or val <= 0:
            continue

        if pending_qty is None:
            continue

        if pending_price is None:
            pending_price = val
            continue

        if pending_disc is None and _DISCOUNT_RE.match(s) and val < 100:
            pending_disc = val
            continue

        line_total = val
        rows.append((pending_qty, pending_price, pending_disc, line_total))
        pending_qty = None
        pending_price = None
        pending_disc = None

    if pending_qty is not None and pending_price is not None:
        rows.append((pending_qty, pending_price, pending_disc, pending_price))

    return rows


def _unit_neto(qty: int, unit_price: int, disc_pct: int | None, line_total: int) -> int | float:
    if qty <= 0:
        return unit_price
    if line_total > 0:
        if line_total % qty == 0:
            return line_total // qty
        # Fracción exacta (ej. 24202/3) para que Cant × V. neto = valor línea.
        return line_total / qty
    if disc_pct and 0 < disc_pct < 100:
        return int(round(unit_price * (1 - disc_pct / 100.0)))
    return unit_price


def _extract_huoying_productos(lines: list[str]) -> list[dict[str, Any]]:
    descs = _extract_descriptions(lines)
    value_rows = _extract_value_rows(lines)
    if not value_rows:
        return []

    productos: list[dict[str, Any]] = []
    for i, (qty, unit_price, disc_raw, line_total) in enumerate(value_rows):
        disc_pct = int(disc_raw) if disc_raw and disc_raw < 100 else None
        desc = descs[i] if i < len(descs) else (descs[0] if len(descs) == 1 else "")
        desc = re.sub(r"\s+", " ", (desc or "").strip())[:255]
        unit_neto = _unit_neto(qty, unit_price, disc_pct, line_total)
        if unit_neto <= 0 or qty <= 0:
            continue
        productos.append(
            {
                "codigo_proveedor": "",
                "descripcion": desc,
                "cantidad": qty,
                "valor_neto": unit_neto,
            }
        )
    return productos


def _extract_huoying_metodo_pago(texto: str) -> str | None:
    m = re.search(
        r"forma\s+de\s+pago\s*:?\s*([A-Za-zÁÉÍÓÚáéíóúñ]+)",
        texto,
        re.IGNORECASE,
    )
    if not m:
        return None
    val = m.group(1).strip().lower()
    if "contado" in val:
        return "contado"
    if "credito" in val or "crédito" in val:
        return "credito"
    if "transfer" in val:
        return "transferencia"
    if "cheque" in val:
        return "cheque"
    return None


@registry.register
class HuoyingParser(BaseInvoiceParser):
    nombre = "huoying"

    def matches(self, rut: str | None, ocr_text: str) -> bool:
        return is_huoying_invoice(rut, ocr_text)

    def parse(self, data: dict[str, Any]) -> dict[str, Any]:
        texto = (data.get("ocr_texto_crudo") or "").strip()
        if not texto or not is_huoying_invoice(data.get("rut_proveedor"), texto):
            return data

        texto_norm = invoice_vision._normalize_ocr_text(texto)
        lines = [ln.strip() for ln in texto_norm.splitlines() if ln.strip()]

        if _norm_rut(data.get("rut_proveedor")) != _norm_rut(_HUOYING_RUT):
            data["rut_proveedor"] = _HUOYING_RUT

        productos = _extract_huoying_productos(lines)
        if productos:
            data["productos"] = productos
            data["productos_fuente"] = "huoying_columnar"
            data["productos_n"] = len(productos)
            p0 = productos[0]
            data["producto_codigo"] = p0.get("codigo_proveedor") or ""
            data["producto_cantidad"] = p0.get("cantidad")
            data["producto_valor_neto"] = p0.get("valor_neto")
            if p0.get("descripcion"):
                data["producto_descripcion"] = p0["descripcion"]

        mp = _extract_huoying_metodo_pago(texto_norm)
        if mp:
            data["metodo_pago"] = mp

        return data
