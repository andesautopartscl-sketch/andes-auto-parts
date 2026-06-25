"""Parser OCR Boston Ltda (RUT 76.351.383-1): facturas térmicas con código CHERY- y cant x precio."""
from __future__ import annotations

import re
from typing import Any

from app.utils import invoice_vision

from .base import BaseInvoiceParser
from .registry import registry

_BOSTON_RUT = "76.351.383-1"
_CODE_RE = re.compile(
    r"(?:CHERY|BOSTON)?-?(\d{9}[A-Z]{2,3})\b",
    re.IGNORECASE,
)
_QTY_PRICE_RE = re.compile(
    r"(\d{1,4}(?:[.,]\d{2})?)\s*x\s*([\d.,\s]+)",
    re.IGNORECASE,
)
_LINE_TOTAL_RE = re.compile(r"^[\$]?\s*([\d.,]+)\s*$")


def _norm_rut(rut: str | None) -> str:
    return re.sub(r"[^0-9kK]", "", (rut or "")).upper()


def is_boston_invoice(rut: str | None, ocr_text: str) -> bool:
    if _norm_rut(rut) == _norm_rut(_BOSTON_RUT):
        return True
    t = ocr_text or ""
    return bool(
        re.search(
            r"boston\s+limitada|inmobiliaria\s+boston",
            t,
            re.IGNORECASE,
        )
    )


def _fix_ocr_price(raw: str) -> str:
    s = (raw or "").strip()
    s = re.sub(r"(\d)\s+(\d)$", r"\1,\2", s)
    return s

def _parse_unit_price(
    price_raw: str, qty: int, line_total: int | None
) -> int | float | None:
    price_raw = _fix_ocr_price(price_raw)
    unit = invoice_vision._parse_monto_chileno(price_raw)
    if line_total and qty > 0:
        exact = line_total / qty
        if abs(exact - round(exact)) > 0.01:
            return exact
        unit_from_total = int(round(exact))
        if unit is None:
            return unit_from_total
        err_total = abs(unit_from_total * qty - line_total)
        err_ocr = abs(int(round(unit)) * qty - line_total)
        if err_total < err_ocr:
            return unit_from_total
    return unit


def _parse_qty(qty_raw: str, unit_price: int | float | None, line_total: int | None) -> int:
    qv = invoice_vision._parse_monto_chileno(qty_raw.replace(",", "."))
    if qv is None:
        return 1
    if unit_price and line_total and unit_price > 0:
        inferred = int(round(line_total / unit_price))
        if 1 <= inferred <= 999 and abs(inferred * unit_price - line_total) <= max(2, inferred):
            return inferred
    if qv >= 100 and qv % 100 == 0 and qv <= 99900:
        candidate = qv // 100
        if 1 <= candidate <= 999:
            return candidate
    return max(1, int(round(qv)))


def _item_section_bounds(lines: list[str]) -> tuple[int, int]:
    start = 0
    for i, line in enumerate(lines):
        low = line.lower()
        if low in ("cantidad", "total") or "detalle" in low:
            start = i + 1
    end = len(lines)
    for i, line in enumerate(lines):
        if re.search(r"monto\s+neto", line, re.IGNORECASE):
            end = i
            break
    return start, end


def _next_line_total(lines: list[str], idx: int, end: int) -> int | None:
    for j in range(idx + 1, min(end, idx + 4)):
        line = lines[j].strip()
        if re.fullmatch(r"alt", line, re.IGNORECASE):
            continue
        if not invoice_vision._is_chilean_price_line(line):
            continue
        m = _LINE_TOTAL_RE.match(line)
        if not m:
            continue
        val = invoice_vision._parse_monto_chileno(m.group(1))
        if val is not None and val >= 100:
            return val
    return None


def _extract_boston_productos(lines: list[str]) -> list[dict[str, Any]]:
    start, end = _item_section_bounds(lines)
    productos: list[dict[str, Any]] = []
    desc_parts: list[str] = []

    i = start
    while i < end:
        line = lines[i].strip()
        low = line.lower()

        if low in ("cantidad", "total", "alt") or not line:
            i += 1
            continue
        if re.search(r"^\(?[A-Z0-9]{5,}/", line):
            desc_parts.append(line)
            i += 1
            continue

        code_m = _CODE_RE.search(line)
        if not code_m:
            if re.search(r"[A-Za-zÁÉÍÓÚáéíóúñ]{3,}", line) and not _QTY_PRICE_RE.search(line):
                desc_parts.append(line)
            i += 1
            continue

        codigo = code_m.group(1).upper()
        qty = 1
        unit_price: int | float | None = None
        line_total = _next_line_total(lines, i, end)

        qp = _QTY_PRICE_RE.search(line)
        if qp:
            qty = _parse_qty(qp.group(1), None, line_total)
            unit_price = _parse_unit_price(qp.group(2), qty, line_total)
            if unit_price is not None and line_total and qty > 0:
                qty = _parse_qty(qp.group(1), unit_price, line_total)

        if unit_price is None and line_total:
            qty = 1
            unit_price = line_total

        if unit_price is not None and unit_price > 0:
            desc = " ".join(desc_parts).strip()
            productos.append(
                {
                    "codigo_proveedor": codigo,
                    "descripcion": desc[:255] if desc else "",
                    "cantidad": qty,
                    "valor_neto": unit_price,
                }
            )
        desc_parts = []
        i += 1

    return productos


def _extract_boston_footer(lines: list[str]) -> tuple[int | None, int | None, int | None]:
    texto = "\n".join(lines)
    return invoice_vision._extract_montos(texto)


@registry.register
class BostonParser(BaseInvoiceParser):
    nombre = "boston"

    def matches(self, rut: str | None, ocr_text: str) -> bool:
        return is_boston_invoice(rut, ocr_text or "")

    def parse(self, data: dict[str, Any]) -> dict[str, Any]:
        texto = (data.get("ocr_texto_crudo") or "").strip()
        if not texto or not is_boston_invoice(data.get("rut_proveedor"), texto):
            return data

        texto_norm = invoice_vision._normalize_ocr_text(texto)
        lines = [ln.strip() for ln in texto_norm.splitlines() if ln.strip()]

        productos = _extract_boston_productos(lines)
        if productos:
            data["productos"] = productos
            data["productos_fuente"] = "boston"
            data["productos_n"] = len(productos)
            p0 = productos[0]
            data["producto_codigo"] = p0.get("codigo_proveedor")
            data["producto_cantidad"] = p0.get("cantidad")
            data["producto_valor_neto"] = p0.get("valor_neto")

        neto, iva, total = _extract_boston_footer(lines)
        if neto is not None:
            data["total_neto"] = neto
        if iva is not None:
            data["iva"] = iva
        if total is not None:
            data["total"] = total

        if _norm_rut(data.get("rut_proveedor")) != _norm_rut(_BOSTON_RUT):
            data["rut_proveedor"] = _BOSTON_RUT

        return data
