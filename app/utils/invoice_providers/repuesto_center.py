"""Parser OCR Repuesto Center (Facele DTE): precios y pie sin mezclar con totales."""
from __future__ import annotations

import re
from typing import Any

from app.utils import invoice_vision

from .base import BaseInvoiceParser
from .registry import registry

_RC_RUT = "79.656.210-2"
_CODE_RE = re.compile(r"^[A-Z]{1,4}\d{2,6}[A-Z]{0,4}$")


def _is_rc_product_code(line: str) -> bool:
    c = (line or "").strip().upper()
    if not _CODE_RE.match(c) or len(c) < 4:
        return False
    return any(ch.isalpha() for ch in c) and any(ch.isdigit() for ch in c)


def _norm_rut(rut: str | None) -> str:
    return re.sub(r"[^0-9kK]", "", (rut or "")).upper()


def is_repuesto_center_invoice(rut: str | None, ocr_text: str) -> bool:
    if _norm_rut(rut) == _norm_rut(_RC_RUT):
        return True
    t = ocr_text or ""
    return bool(
        re.search(
            r"repuestos?\s*center|repuesto\s*rc|facele|faccle",
            t,
            re.IGNORECASE,
        )
    )


def _emitter_rut(texto: str) -> str:
    m = re.search(
        r"R\.?\s*U\.?\s*T\.?\s*79\.656\.210-2",
        texto,
        re.IGNORECASE,
    )
    return _RC_RUT if m else _RC_RUT


def _extract_item_codes(lines: list[str]) -> list[str]:
    codes: list[str] = []
    start: int | None = None
    for i, line in enumerate(lines):
        if not re.fullmatch(r"c[oó]digo", line.strip(), re.IGNORECASE):
            continue
        for j in range(i + 1, min(i + 5, len(lines))):
            nxt = lines[j].strip()
            if nxt.startswith(":"):
                break
            if _is_rc_product_code(nxt):
                start = j
                break
        if start is not None:
            break
    if start is None:
        return []

    for line in lines[start:]:
        low = line.lower()
        if low.startswith("observacion"):
            break
        if _is_rc_product_code(line):
            codes.append(line.upper())
    seen: set[str] = set()
    out: list[str] = []
    for c in codes:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _extract_qty_and_prices(lines: list[str]) -> tuple[list[int], list[int]]:
    start: int | None = None
    for i, line in enumerate(lines):
        if re.fullmatch(r"cantidad", line.strip(), re.IGNORECASE):
            start = i + 1
            break
    if start is None:
        return [], []

    qtys: list[int] = []
    prices: list[int] = []
    for line in lines[start:]:
        if re.search(r"monto\s+neto", line, re.IGNORECASE):
            break
        if re.fullmatch(r"\d{1,2}", line.strip()):
            qtys.append(int(line.strip()))
            continue
        if invoice_vision._is_chilean_price_line(line):
            m = invoice_vision._PRICE_LINE_RE.match(line.strip())
            if not m:
                continue
            val = invoice_vision._parse_monto_chileno(m.group(1))
            if val is not None and val >= 1000:
                prices.append(val)
    return qtys, prices


def _unit_prices(prices: list[int], n: int) -> list[int]:
    if n <= 0 or not prices:
        return []
    if len(prices) >= 2 * n:
        return [prices[i * 2] for i in range(n)]
    units: list[int] = []
    i = 0
    while len(units) < n and i < len(prices):
        units.append(prices[i])
        if i + 1 < len(prices) and prices[i + 1] == prices[i]:
            i += 2
        else:
            i += 1
    return units[:n]


def _extract_repuesto_center_productos(lines: list[str]) -> list[dict[str, Any]]:
    codes = _extract_item_codes(lines)
    if not codes:
        return []
    qtys, prices = _extract_qty_and_prices(lines)
    units = _unit_prices(prices, len(codes))
    productos: list[dict[str, Any]] = []
    for i, codigo in enumerate(codes):
        qty = qtys[i] if i < len(qtys) else 1
        unit = units[i] if i < len(units) else None
        if unit is None:
            continue
        productos.append(
            {"codigo_proveedor": codigo, "cantidad": max(1, qty), "valor_neto": unit}
        )
    return productos


def _repair_montos(
    neto: int | None,
    iva: int | None,
    total: int | None,
    productos: list[dict[str, Any]],
) -> tuple[int | None, int | None, int | None]:
    return invoice_vision.reconcile_factura_totals_con_lineas(
        productos, neto, iva, total
    )


@registry.register
class RepuestoCenterParser(BaseInvoiceParser):
    nombre = "repuesto_center"

    def matches(self, rut: str | None, ocr_text: str) -> bool:
        return is_repuesto_center_invoice(rut, ocr_text or "")

    def parse(self, data: dict[str, Any]) -> dict[str, Any]:
        texto = (data.get("ocr_texto_crudo") or "").strip()
        if not texto or not is_repuesto_center_invoice(data.get("rut_proveedor"), texto):
            return data

        texto_norm = invoice_vision._normalize_ocr_text(texto)
        lines = [ln.strip() for ln in texto_norm.splitlines() if ln.strip()]

        productos = _extract_repuesto_center_productos(lines)
        if productos:
            data["productos"] = productos
            data["productos_fuente"] = "repuesto_center"
            data["productos_n"] = len(productos)
            p0 = productos[0]
            data["producto_codigo"] = p0.get("codigo_proveedor")
            data["producto_cantidad"] = p0.get("cantidad")
            data["producto_valor_neto"] = p0.get("valor_neto")

        neto, iva, total = invoice_vision._extract_dte_footer_montos(lines)
        if neto is None:
            neto, iva, total = invoice_vision._extract_montos(texto_norm)

        neto, iva, total = _repair_montos(neto, iva, total, productos)

        if neto is not None:
            data["total_neto"] = neto
        if iva is not None:
            data["iva"] = iva
        if total is not None:
            data["total"] = total

        data["rut_proveedor"] = _emitter_rut(texto_norm)

        m = re.search(
            r"FACTURA\s+ELECTR[OÓ]NICA[\s\S]{0,60}?N[°º]?\s*0*(\d{3,8})\b",
            texto_norm,
            re.IGNORECASE,
        )
        if not m:
            m = re.search(r"N[°º]?\s*0*(\d{6,8})\b", texto_norm, re.IGNORECASE)
        if m:
            num = m.group(1).lstrip("0") or m.group(1)
            if len(num) >= 3:
                data["numero_documento"] = num

        if re.search(r"forma\s+de\s+pago\s*:\s*cr[eé]dito", texto_norm, re.IGNORECASE):
            data["metodo_pago"] = "credito"
        elif re.search(r"forma\s+de\s+pago\s*:\s*contado", texto_norm, re.IGNORECASE):
            data["metodo_pago"] = "contado"

        return data
