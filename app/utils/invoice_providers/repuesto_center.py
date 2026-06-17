"""Parser OCR Repuesto Center (Facele DTE): precios y pie sin mezclar con totales."""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

from app.utils import invoice_vision

from .base import BaseInvoiceParser
from .registry import registry

_RC_RUT = "79.656.210-2"
_CODE_RE = re.compile(r"^[A-Z]{1,6}\d{2,6}[A-Z0-9]{0,6}$")
_DIGIT_PREFIX_CODE_RE = re.compile(r"^\d{3,6}[A-Z]{1,6}$")
_RC_TOKEN_RE = re.compile(
    r"^(?:[A-Z]{1,6}\d{2,6}[A-Z0-9]{0,6}|\d{3,6}[A-Z]{1,6})$",
    re.IGNORECASE,
)
_NUM_CODE_RE = re.compile(r"^\d{9,11}$")
_INLINE_CODE_RE = re.compile(
    r"\b((?:[A-Z]{1,6}\d{2,6}[A-Z0-9]{0,6})|(?:\d{3,6}[A-Z]{1,6}))\b",
    re.IGNORECASE,
)
_INLINE_NUM_CODE_RE = re.compile(r"\b(\d{9,11})\b")
_CUSTOMER_CODE_RE = re.compile(r"^C\d{7,9}", re.IGNORECASE)
_QTY_PRICE_LINE_RE = re.compile(
    r"^\s*(\d{1,3})\s+([\d$][\d.,]*)\s*(?:([\d$][\d.,]*)\s*)?$"
)
_FULL_PRODUCT_ROW_RE = re.compile(
    r"^((?:[A-Z]{1,6}\d{2,6}[A-Z0-9]{0,6})|(?:\d{3,6}[A-Z]{1,6}))\b.*?\b(\d{1,3})\s+([\d.,]+)",
    re.IGNORECASE,
)


def _normalize_rc_code_ocr(code: str) -> str:
    """Corrige O/0 en códigos RC (MAXD07ORC / MAXDO7ORC → MAXD070RC)."""
    c = (code or "").strip().upper()
    if not c:
        return c
    c = re.sub(r"(?<=[A-Z])O(?=\d)", "0", c)
    c = re.sub(r"(?<=\d)O(?=\d)", "0", c)
    c = re.sub(r"(?<=\d)O(?=[A-Z])", "0", c)
    c = re.sub(r"(\d)O(\d)", r"\g<1>0\2", c)
    c = re.sub(r"(\d)O([A-Z])", r"\g<1>0\2", c)
    c = re.sub(r"([A-Z])O(\d)", r"\g<1>0\2", c)
    return c


def _digits_key(value: str | None) -> str:
    return re.sub(r"\D", "", (value or "")).lstrip("0") or "0"


def _is_folio_token(code: str, folio: str | None) -> bool:
    if not folio:
        return False
    return _digits_key(code) == _digits_key(folio)


def _is_folio_context_line(line: str) -> bool:
    return bool(
        re.search(
            r"(?:^N[°º]?\s*0*\d{3,8}\b|factura\s+electr)",
            (line or "").strip(),
            re.IGNORECASE,
        )
    )


def _is_ui_noise_line(line: str) -> bool:
    low = (line or "").strip().lower()
    if not low:
        return True
    noise = (
        "analizar factura",
        "datos detectados",
        "aplicar datos",
        "cancelar",
        "cerrar",
        "ítems api",
        "parser ocr",
        "fuente ítems",
        "análisis listo",
    )
    return low in noise or low == "auto"


def _is_rc_numeric_code(code: str) -> bool:
    return bool(_NUM_CODE_RE.match((code or "").strip()))


def _is_rc_product_code(line: str, folio: str | None = None) -> bool:
    c = _normalize_rc_code_ocr((line or "").strip())
    if not c or _CUSTOMER_CODE_RE.match(c):
        return False
    if folio and _is_folio_token(c, folio):
        return False
    if _is_rc_numeric_code(c):
        return True
    if _DIGIT_PREFIX_CODE_RE.match(c):
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


def _extract_item_codes(lines: list[str], folio: str | None = None) -> list[str]:
    columnar = _extract_item_codes_columnar(lines, folio)
    inline = _extract_item_codes_inline(lines, folio)
    loose = _extract_item_codes_loose(lines, folio)
    return max((columnar, inline, loose), key=len)


def _extract_item_codes_loose(lines: list[str], folio: str | None = None) -> list[str]:
    """Escaneo amplio: códigos en DETALLE aunque vayan antes de CANTIDAD."""
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if re.search(r"monto\s+neto", line, re.IGNORECASE):
            break
        if line.lower().startswith("observacion"):
            break
        if _is_ui_noise_line(line) or _is_folio_context_line(line):
            continue
        for code in _codes_from_line(line, folio):
            if code not in seen:
                seen.add(code)
                out.append(code)
    return out


def _extract_item_codes_columnar(lines: list[str], folio: str | None = None) -> list[str]:
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
            if _is_folio_context_line(nxt):
                continue
            if _is_rc_product_code(nxt, folio):
                start = j
                break
            inline = _inline_code_from_line(nxt, folio)
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
        if _is_ui_noise_line(line) or _is_folio_context_line(line):
            continue
        for candidate in _codes_from_line(line, folio):
            if candidate not in seen:
                seen.add(candidate)
                out.append(candidate)
    return out


def _inline_code_from_line(line: str, folio: str | None = None) -> str | None:
    for candidate in _codes_from_line(line, folio):
        return candidate
    return None


def _codes_from_line(line: str, folio: str | None = None) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    stripped = (line or "").strip()
    if _is_folio_context_line(stripped):
        return found

    def add_code(raw: str) -> None:
        code = _normalize_rc_code_ocr(raw)
        if _is_rc_product_code(code, folio) and code not in seen:
            seen.add(code)
            found.append(code)

    if _is_rc_product_code(stripped, folio):
        add_code(stripped)
    for token in re.split(r"\s+", stripped):
        add_code(token)
    for m in _INLINE_CODE_RE.finditer(stripped):
        add_code(m.group(1))
    for m in _INLINE_NUM_CODE_RE.finditer(stripped):
        raw = m.group(1)
        if _is_rc_numeric_code(raw) and not _is_folio_token(raw, folio) and raw not in seen:
            seen.add(raw)
            found.append(raw)
    return found


def _extract_item_codes_inline(lines: list[str], folio: str | None = None) -> list[str]:
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
        if _is_ui_noise_line(line):
            continue
        for code in _codes_from_line(line, folio):
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


def _is_facele_tail_stop_line(line: str) -> bool:
    low = (line or "").strip().lower()
    if not low:
        return True
    if low.startswith("res. 80 de") or "verifique documento" in low:
        return True
    if low.startswith("timbre electronico"):
        return True
    return False


def _facele_tail_unit_qty_after(
    lines: list[str], code_idx: int, folio: str | None
) -> tuple[int | None, int]:
    """Tras un código Facele: precio unitario (coma) y cantidad en líneas siguientes."""
    n = len(lines)
    for j in range(code_idx + 1, min(code_idx + 10, n)):
        nxt = (lines[j] or "").strip()
        if _is_facele_tail_stop_line(nxt):
            break
        if re.search(r"monto\s+neto", nxt, re.IGNORECASE):
            break
        if nxt.lower().startswith("observacion"):
            break
        nxt_codes = _codes_from_line(nxt, folio)
        if (
            j > code_idx + 1
            and nxt_codes
            and _is_rc_product_code(nxt_codes[0], folio)
        ):
            break
        qp = _parse_qty_price_line(nxt)
        if qp:
            return qp[1], qp[0]
        if "," in nxt and invoice_vision._is_chilean_price_line(nxt):
            m = invoice_vision._PRICE_LINE_RE.match(nxt)
            if not m:
                continue
            val = invoice_vision._parse_monto_chileno(m.group(1))
            if val is None or val < 1000:
                continue
            qty = 1
            for k in range(j + 1, min(j + 4, n)):
                qline = (lines[k] or "").strip()
                if re.fullmatch(r"\d{1,2}", qline):
                    qty = int(qline)
                    break
                if (
                    _is_facele_tail_stop_line(qline)
                    or _codes_from_line(qline, folio)
                    or re.search(r"monto\s+neto", qline, re.IGNORECASE)
                ):
                    break
            return val, qty
    return None, 1


def _extract_productos_facele_pdf_tail(
    lines: list[str], folio: str | None = None
) -> list[dict[str, Any]]:
    """PDF Facele: bloque columnar al final (código, detalle, precio, cantidad)."""
    productos: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, line in enumerate(lines):
        if _is_ui_noise_line(line) or _is_folio_context_line(line):
            continue
        codes = _codes_from_line(line, folio)
        if not codes:
            continue
        code = _normalize_rc_code_ocr(codes[0])
        if not _is_rc_product_code(code, folio) or code in seen:
            continue
        unit, qty = _facele_tail_unit_qty_after(lines, i, folio)
        if unit is None:
            continue
        seen.add(code)
        productos.append(
            {
                "codigo_proveedor": code,
                "cantidad": max(1, qty),
                "valor_neto": unit,
            }
        )
    return productos


def _log_repuesto_center_extraction_debug(
    lines: list[str],
    folio: str | None,
    ocr_text: str,
    productos: list[dict[str, Any]],
) -> None:
    """Logging temporal: OCR crudo y líneas descartadas cuando no hay ítems."""
    if productos:
        return
    discarded: list[str] = []
    for i, line in enumerate(lines):
        stripped = (line or "").strip()
        if not stripped:
            discarded.append(f"[{i}] vacía")
            continue
        if _is_ui_noise_line(stripped):
            discarded.append(f"[{i}] ui_noise: {stripped!r}")
            continue
        if _is_folio_context_line(stripped):
            discarded.append(f"[{i}] folio_ctx: {stripped!r}")
            continue
        codes = _codes_from_line(stripped, folio)
        if codes and _is_rc_product_code(_normalize_rc_code_ocr(codes[0]), folio):
            unit, qty = _facele_tail_unit_qty_after(lines, i, folio)
            if unit is None:
                discarded.append(
                    f"[{i}] código sin precio: {stripped!r} codes={codes}"
                )
            continue
        if re.search(r"monto\s+neto", stripped, re.IGNORECASE):
            discarded.append(f"[{i}] stop_monto_neto: {stripped!r}")
        if stripped.lower().startswith("observacion"):
            discarded.append(f"[{i}] stop_observacion: {stripped!r}")
    logger.info(
        "[repuesto_center_debug] folio=%s sin productos; líneas=%s",
        folio,
        len(lines),
    )
    logger.info(
        "[repuesto_center_debug] OCR crudo (hasta 4000 chars):\n%s",
        (ocr_text or "")[:4000],
    )
    logger.info(
        "[repuesto_center_debug] descartadas/seguimiento (%s):\n%s",
        len(discarded),
        "\n".join(discarded[:80]),
    )


def _extract_productos_pdf_rows(
    lines: list[str], folio: str | None = None
) -> list[dict[str, Any]]:
    """PDF nativo Facele: fila completa código + cantidad + precio."""
    productos: list[dict[str, Any]] = []
    for line in lines:
        if re.search(r"monto\s+neto", line, re.IGNORECASE):
            break
        if line.lower().startswith("observacion"):
            break
        if _is_ui_noise_line(line):
            continue
        m = _FULL_PRODUCT_ROW_RE.match(line.strip())
        if not m:
            continue
        code = _normalize_rc_code_ocr(m.group(1))
        if not _is_rc_product_code(code, folio):
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


def _extract_repuesto_center_productos(
    lines: list[str], folio: str | None = None
) -> list[dict[str, Any]]:
    row_products = _extract_productos_pdf_rows(lines, folio)
    if row_products:
        return row_products

    tail_products = _extract_productos_facele_pdf_tail(lines, folio)
    if tail_products:
        return tail_products

    codes = _extract_item_codes(lines, folio)
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


def _apply_productos_to_data(
    data: dict[str, Any], productos: list[dict[str, Any]], fuente: str
) -> None:
    if not productos:
        return
    data["productos"] = productos
    data["productos_fuente"] = fuente
    data["productos_n"] = len(productos)
    p0 = productos[0]
    data["producto_codigo"] = p0.get("codigo_proveedor")
    data["producto_cantidad"] = p0.get("cantidad")
    data["producto_valor_neto"] = p0.get("valor_neto")


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
        lines = [
            ln.strip()
            for ln in texto_norm.splitlines()
            if ln.strip() and not _is_ui_noise_line(ln.strip())
        ]

        folio = data.get("numero_documento")
        if not folio:
            m = re.search(r"N[°º]?\s*0*(\d{6,8})\b", texto_norm, re.IGNORECASE)
            if m:
                folio = m.group(1).lstrip("0") or m.group(1)

        dte_productos = data.get("_dte_productos") or []
        if dte_productos:
            productos = dte_productos
            _apply_productos_to_data(data, productos, "repuesto_center_dte_xml")
        else:
            productos = _extract_repuesto_center_productos(lines, folio)
            if productos:
                _apply_productos_to_data(data, productos, "repuesto_center")
            elif not (data.get("productos") or []):
                _log_repuesto_center_extraction_debug(lines, folio, texto, [])

        neto, iva, total = _extract_repuesto_center_footer_montos(lines)
        if neto is None and data.get("_dte_neto") is not None:
            neto = data.get("_dte_neto")
            iva = data.get("_dte_iva")
            total = data.get("_dte_total")
        if neto is None:
            neto, iva, total = invoice_vision._extract_montos(texto_norm)

        neto, iva, total = _repair_montos(
            neto, iva, total, data.get("productos") or productos
        )

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
