"""Parser post-OCR para facturas térmicas ALI REPUESTOS (RUT 77.229.308-9)."""
from __future__ import annotations

import re
from typing import Any

from app.utils import invoice_vision

from .base import BaseInvoiceParser
from .registry import registry

_ALI_RUT = "77.229.308-9"
_ALI_RUT_COMPACT = re.sub(r"[^0-9kK-]", "", _ALI_RUT).upper()
_ALI_NAME = "ALI REPUESTOS"

_LINE_RE = re.compile(
    r"^(\d+)\s+(.+?)\s+([\d.,]+)\s+([\d.,]+)\s*$",
    re.IGNORECASE,
)
_NUMBER_ONLY_RE = re.compile(r"^[\d.,]+$")

_ITEM_SKIP_KW = re.compile(
    r"\b(DESCUENTO|NETO|IVA|I\.?\s*V\.?\s*A|TOTAL|SUBTOTAL|AFECTO|"
    r"PRECIO\s+UNIT|DETALLE|CANT)\b",
    re.IGNORECASE,
)

_FOOTER_KW_RE = re.compile(
    r"\b(DESCUENTO|NETO|I\.?\s*V\.?\s*A|TOTAL|SUBTOTAL)\b",
    re.IGNORECASE,
)


def _rut_matches(rut: str | None) -> bool:
    if not rut:
        return False
    compact = re.sub(r"[^0-9kK-]", "", (rut or "").strip()).upper()
    return compact == _ALI_RUT_COMPACT


def _parse_lines(texto: str) -> list[str]:
    texto_norm = invoice_vision._normalize_ocr_text(texto)
    return [ln.strip() for ln in texto_norm.splitlines() if ln.strip()]


def _extract_productos(lines: list[str]) -> list[dict[str, Any]]:
    """Extrae productos. Soporta columnas en la misma línea O en líneas separadas."""
    productos: list[dict[str, Any]] = []
    i = 0
    while i < len(lines):
        line = lines[i]

        if _ITEM_SKIP_KW.search(line):
            i += 1
            continue

        # Estrategia 1: qty + desc + precio_unit + total en la misma línea
        m = _LINE_RE.match(line)
        if m:
            qty = int(m.group(1))
            desc = re.sub(r"\s+", " ", (m.group(2) or "").strip())
            if 0 < qty <= 99999 and desc and re.search(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]", desc):
                unit = invoice_vision._parse_monto_chileno(m.group(3))
                total_linea = invoice_vision._parse_monto_chileno(m.group(4))
                if unit and unit > 0 and total_linea and total_linea > 0:
                    productos.append({
                        "codigo_proveedor": "",
                        "descripcion": desc[:255],
                        "cantidad": qty,
                        "valor_neto": unit,
                    })
            i += 1
            continue

        # Estrategia 2: qty + desc en esta línea, precios en líneas siguientes
        # (OCR de foto separa columnas de ticket térmico)
        m2 = re.match(r"^(\d+)\s+([A-Za-zÁÉÍÓÚÑáéíóúñ].{1,})", line)
        if m2:
            qty = int(m2.group(1))
            desc = re.sub(r"\s+", " ", (m2.group(2) or "").strip())
            if 0 < qty <= 99999 and desc and re.search(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]", desc):
                # Buscar precio unitario en la siguiente línea numérica
                unit = None
                advance = 1
                if i + advance < len(lines):
                    next_line = lines[i + advance].strip()
                    if _NUMBER_ONLY_RE.match(next_line):
                        unit = invoice_vision._parse_monto_chileno(next_line)
                        advance += 1
                if unit and unit > 0:
                    # Verificar si hay total en la línea siguiente
                    if i + advance < len(lines):
                        next_line2 = lines[i + advance].strip()
                        if _NUMBER_ONLY_RE.match(next_line2):
                            advance += 1  # consumir el total también
                    productos.append({
                        "codigo_proveedor": "",
                        "descripcion": desc[:255],
                        "cantidad": qty,
                        "valor_neto": unit,
                    })
                    i += advance
                    continue

        i += 1
    return productos


def _extract_footer(texto: str) -> dict[str, int | None]:
    """
    Soporta dos layouts del OCR:
    A) label y valor en la misma línea:  "NETO: 31.597"  (ancla $ al final de línea)
    B) labels en columna y valores en columna separada (OCR de foto térmica):
       "DESCUENTO:"  "NETO:"  "I.V.A.:"  "TOTAL:"  luego  "2.400"  "31.597" ...
    Layout B siempre corre cuando encuentra el bloque DESCUENTO (sobreescribe A).
    """
    lines = [ln.strip() for ln in texto.splitlines() if ln.strip()]

    # Layout A: label + valor en la MISMA línea (ancla $ evita cruzar saltos)
    def _same_line(pattern: str) -> int | None:
        m = re.search(pattern, texto, re.IGNORECASE | re.MULTILINE)
        if not m:
            return None
        return invoice_vision._parse_monto_chileno(m.group(1))

    descuento = _same_line(r"DESCUENTO\s*:\s*\$?\s*([\d.,]+)\s*$")
    neto      = _same_line(r"(?<!\w)NETO\s*:\s*\$?\s*([\d.,]+)\s*$")
    iva       = _same_line(
        r"I\.?\s*V\.?\s*A\.?\s*(?:\(\s*19\s*%\s*\))?\s*:\s*\$?\s*([\d.,]+)\s*$"
    )
    total_candidates: list[int] = []
    for mt in re.finditer(
        r"(?<!\w)TOTAL\s*:\s*\$?\s*([\d.,]+)\s*$", texto, re.IGNORECASE | re.MULTILINE
    ):
        v = invoice_vision._parse_monto_chileno(mt.group(1))
        if v and v > 0:
            total_candidates.append(v)
    total = max(total_candidates) if total_candidates else None

    # Layout B: labels y valores en líneas separadas — corre SIEMPRE que exista
    # el bloque DESCUENTO (sobreescribe Layout A si encuentra los 4 números)
    footer_start = None
    for idx, ln in enumerate(lines):
        if re.search(r"\bDESCUENTO\b", ln, re.IGNORECASE):
            footer_start = idx
            break

    if footer_start is not None:
        numbers: list[int] = []
        for idx in range(footer_start, min(footer_start + 20, len(lines))):
            ln = lines[idx]
            if _NUMBER_ONLY_RE.match(ln):
                v = invoice_vision._parse_monto_chileno(ln)
                if v is not None and v >= 0:
                    numbers.append(v)
        if len(numbers) >= 4:
            descuento = numbers[0]
            neto      = numbers[1]
            iva       = numbers[2]
            total     = numbers[3]
        elif len(numbers) == 3:
            neto  = numbers[0]
            iva   = numbers[1]
            total = numbers[2]

    return {
        "descuento": descuento,
        "total_neto": neto,
        "iva":        iva,
        "total":      total,
    }


@registry.register
class AliRepuestosParser(BaseInvoiceParser):
    """Factura térmica simple: Cant | Detalle | Precio Unit. | Total."""

    nombre = "ali_repuestos"

    def matches(self, rut: str | None, ocr_text: str) -> bool:
        texto = (ocr_text or "").upper()
        if _ALI_NAME in texto:
            return True
        return _rut_matches(rut)

    def parse(self, data: dict[str, Any]) -> dict[str, Any]:
        texto = (data.get("ocr_texto_crudo") or "").strip()
        if not texto:
            return data

        # DEBUG temporal — escribir OCR a archivo para diagnóstico
        try:
            import pathlib
            _dbg_path = pathlib.Path("C:/AndesAutoParts/debug_ali_ocr.txt")
            _dbg_path.write_text(
                f"=== OCR CRUDO ===\n{texto}\n\n"
                f"=== DATA KEYS ===\n{list(data.keys())}\n",
                encoding="utf-8"
            )
        except Exception:
            pass
        # FIN DEBUG

        if not self.matches(data.get("rut_proveedor"), texto):
            return data

        lines = _parse_lines(texto)
        productos = _extract_productos(lines)
        footer = _extract_footer(texto)

        if productos:
            data["productos"] = productos
            data["productos_fuente"] = self.nombre
            data["productos_n"] = len(productos)
            p0 = productos[0]
            data["producto_codigo"] = p0.get("codigo_proveedor") or None
            data["producto_cantidad"] = p0.get("cantidad")
            data["producto_valor_neto"] = p0.get("valor_neto")

        if footer.get("descuento") is not None:
            data["descuento"] = footer["descuento"]
        if footer.get("total_neto") is not None:
            data["total_neto"] = footer["total_neto"]
        if footer.get("iva") is not None:
            data["iva"] = footer["iva"]
        if footer.get("total") is not None:
            data["total"] = footer["total"]

        # Recalcular valor_neto por unidad: total real ÷ cantidad
        # (total ya tiene el descuento aplicado; esto reemplaza el
        #  precio de lista que venía del OCR)
        _total_real = data.get("total_neto")
        if _total_real and data.get("productos"):
            _prods = data["productos"]
            for _p in _prods:
                _qty = _p.get("cantidad") or 1
                if _qty > 0:
                    _p["valor_neto"] = round(_total_real / _qty)
            data["productos"] = _prods
            data["producto_valor_neto"] = _prods[0].get("valor_neto")

        if _rut_matches(data.get("rut_proveedor")) or not data.get("rut_proveedor"):
            data["rut_proveedor"] = _ALI_RUT

        return data
