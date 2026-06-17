"""Parser OCR Repuesto Center (Facele DTE): precios y pie sin mezclar con totales."""
from __future__ import annotations

import re
from typing import Any

from app.utils import invoice_vision

from .base import BaseInvoiceParser
from .registry import registry

_RC_RUT = "79.656.210-2"
_CODE_RE = re.compile(r"^[A-Z]{1,6}\d{2,6}[A-Z0-9]{0,6}$")
_NUM_CODE_RE = re.compile(r"^\d{9,11}$")
_INLINE_CODE_RE = re.compile(
    r"\b([A-Z]{1,6}\d{2,6}[A-Z0-9]{0,6})\b", re.IGNORECASE
)
_INLINE_NUM_CODE_RE = re.compile(r"\b(\d{9,11})\b")
_CUSTOMER_CODE_RE = re.compile(r"^C\d{7,9}", re.IGNORECASE)
_QTY_PRICE_LINE_RE = re.compile(
    r"^\s*(\d{1,3})\s+([\d$][\d.,]*)\s*(?:([\d$][\d.,]*)\s*)?$"
)
_FULL_PRODUCT_ROW_RE = re.compile(
    r"^([A-Z]{1,6}\d{2,6}[A-Z0-9]{0,6})\b.*?\b(\d{1,3})\s+([\d.,]+)",
    re.IGNORECASE,
)


def _normalize_rc_code_ocr(code: str) -> str:
    """Corrige O/0 en códigos RC (MAXD07ORC → MAXD070RC)."""
    c = (code or "").strip().upper()
    if not c:
        return c
    c = re.sub(r"(\d)O(\d)", r"\g<1>0\2", c)
    c = re.sub(r"(\d)O([A-Z])", r"\g<1>0\2", c)
    c = re.sub(r"([A-Z])O(\d)", r"\g<1>0\2", c)
    return c


def _is_rc_numeric_code(code: str) -> bool:
    return bool(_NUM_CODE_RE.match((code or "").strip()))


def _is_rc_product_code(line: str) -> bool:
    c = _normalize_rc_code_ocr((line or "").strip())
    if not c or _CUSTOMER_CODE_RE.match(c):
        return False
    if _is_rc_numeric_code(c):
        return True
    if not _CODE_RE.match(c) or len(c) < 5:
        return False
    return any(ch.isalpha() for ch in c) and any(ch.isdigit() for ch in c)


def _parse_qty_price_line(line: str) -> tuple[int, int] | None:
    """PDF Facele: «1 67.000,00 67.000» en una sola línea."""
    m = _QTY_PRICE_LINE_RE.match((line or "").strip())
    if not m:
        return None
    qty = int(m.group(1))
    unit = invoice_vision._parse_monto_chileno(m.group(2))
    if unit is None or unit < 1000:
        return None
    return qty, unit


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
    codes = _extract_item_codes_columnar(lines)
    if len(codes) >= 2:
        return codes
    inline = _extract_item_codes_inline(lines)
    if inline:
        return inline
    return codes


def _extract_item_codes_columnar(lines: list[str]) -> list[str]:
    """Bloque vertical CÓDIGO → líneas de código (layout Facele clásico)."""
    codigo_idxs = [
        i
        for i, line in enumerate(lines)
        if re.fullmatch(r"c[oó]digo", line.strip(), re.IGNORECASE)
    ]
    start: int | None = None
    for idx in codigo_idxs:
        for j in range(idx + 1, min(idx + 6, len(lines))):
            nxt = lines[j].strip()
            if nxt.startswith(":"):
                continue
            if _CUSTOMER_CODE_RE.match(nxt):
                break
            if _is_rc_product_code(nxt):
                start = j
                break
            inline = _inline_code_from_line(nxt)
            if inline:
                start = j
                break
        if start is not None:
            break
    if start is None:
        return []

    out: list[str] = []
    seen: set[str] = set()
    for line in lines[start:]:
        low = line.lower()
        if low.startswith("observacion"):
            break
        for candidate in _codes_from_line(line):
            if candidate not in seen:
                seen.add(candidate)
                out.append(candidate)
    return out


def _inline_code_from_line(line: str) -> str | None:
    for candidate in _codes_from_line(line):
        return candidate
    return None


def _codes_from_line(line: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    stripped = (line or "").strip()
    if _is_rc_product_code(stripped):
        code = _normalize_rc_code_ocr(stripped)
        if code not in seen:
            seen.add(code)
            found.append(code)
    for m in _INLINE_CODE_RE.finditer(stripped):
        raw = _normalize_rc_code_ocr(m.group(1))
        if _is_rc_product_code(raw) and raw not in seen:
            seen.add(raw)
            found.append(raw)
    for m in _INLINE_NUM_CODE_RE.finditer(stripped):
        raw = m.group(1)
        if _is_rc_numeric_code(raw) and raw not in seen:
            seen.add(raw)
            found.append(raw)
    return found


def _extract_item_codes_inline(lines: list[str]) -> list[str]:
    """Códigos embebidos en DETALLE cuando cantidades van antes (foto OCR)."""
    start: int | None = None
    end = len(lines)
    for i, line in enumerate(lines):
        low = line.lower()
        if re.search(r"\bunitario\b", low) or re.search(r"precio\s+[ií]tem", low):
            start = i + 1
        if low.startswith("observacion"):
            end = i
            break
    if start is None:
        for i, line in enumerate(lines):
            if re.fullmatch(r"cantidad", line.strip(), re.IGNORECASE):
                start = i + 1
                break
    if start is None:
        return []

    out: list[str] = []
    seen: set[str] = set()
    for line in lines[start:end]:
        if re.fullmatch(r"c[oó]digo", line.strip(), re.IGNORECASE):
            continue
        for code in _codes_from_line(line):
            if code not in seen:
                seen.add(code)
                out.append(code)
    return out


def _extract_qty_and_prices(lines: list[str]) -> tuple[list[int], list[int]]:
    start: int | None = None
    for i, line in enumerate(lines):
        if re.fullmatch(r"cantidad", line.strip(), re.IGNORECASE):
            start = i + 1
            break
    if start is None:
        for i, line in enumerate(lines):
            if re.search(r"\bunitario\b", line, re.IGNORECASE):
                start = i + 1
                break
    if start is None:
        return [], []

    qtys: list[int] = []
    prices: list[int] = []
    for line in lines[start:]:
        if re.search(r"monto\s+neto", line, re.IGNORECASE):
            break
        if line.lower().startswith("observacion"):
            break
        if re.fullmatch(r"c[oó]digo", line.strip(), re.IGNORECASE):
            break
        qty_price = _parse_qty_price_line(line)
        if qty_price:
            qtys.append(qty_price[0])
            prices.append(qty_price[1])
            continue
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


def _extract_productos_pdf_rows(lines: list[str]) -> list[dict[str, Any]]:
    """PDF nativo Facele: fila completa código + cantidad + precio."""
    productos: list[dict[str, Any]] = []
    for line in lines:
        if re.search(r"monto\s+neto", line, re.IGNORECASE):
            break
        if line.lower().startswith("observacion"):
            break
        m = _FULL_PRODUCT_ROW_RE.match(line.strip())
        if not m:
            continue
        code = _normalize_rc_code_ocr(m.group(1))
        if not _is_rc_product_code(code):
            continue
        unit = invoice_vision._parse_monto_chileno(m.group(3))
        if unit is None or unit < 1000:
            continue
        productos.append(
            {
                "codigo_proveedor": code,
                "cantidad": max(1, int(m.group(2))),
                "valor_neto": unit,
            }
        )
    return productos


def _extract_repuesto_center_productos(lines: list[str]) -> list[dict[str, Any]]:
    row_products = _extract_productos_pdf_rows(lines)
    if row_products:
        return row_products

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
            {
                "codigo_proveedor": _normalize_rc_code_ocr(codigo),
                "cantidad": max(1, qty),
                "valor_neto": unit,
            }
        )
    return productos


def _extract_repuesto_center_footer_montos(
    lines: list[str],
) -> tuple[int | None, int | None, int | None]:
    """Pie Facele: prioriza bloque MONTO NETO / IVA / TOTAL del documento."""
    return invoice_vision._extract_dte_footer_montos(lines)


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

        neto, iva, total = _extract_repuesto_center_footer_montos(lines)
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
