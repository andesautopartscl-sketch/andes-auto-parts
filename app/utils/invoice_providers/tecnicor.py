"""Parser OCR Tecnicor (Raul Tagle e Hijos): tabla con precios debajo del timbre."""
from __future__ import annotations

import re
from typing import Any

from app.utils import invoice_vision

from .base import BaseInvoiceParser
from .registry import registry

_TECNICOR_RUT = "81.448.200-6"
_QTY_ONLY_RE = re.compile(r"^\d{1,3},\d{2}$")
_CODE_RE = re.compile(r"^\d{5,8}$")


def _norm_rut(rut: str | None) -> str:
    return re.sub(r"[^0-9kK]", "", (rut or "")).upper()


def is_tecnicor_invoice(rut: str | None, ocr_text: str) -> bool:
    if _norm_rut(rut) == _norm_rut(_TECNICOR_RUT):
        return True
    t = ocr_text or ""
    return bool(
        re.search(
            r"tecnicor|tecnicorchile|raul\s+tagle\s+e\s+hijos",
            t,
            re.IGNORECASE,
        )
    )


def _extract_tecnicor_metodo_pago(texto: str) -> str | None:
    m = re.search(
        r"CONDICION\s+DE\s+PAGO\s*:?\s*([A-Za-zÁÉÍÓÚáéíóúñ]+)",
        texto,
        re.IGNORECASE,
    )
    if not m:
        return None
    val = m.group(1).strip().lower()
    if "contado" in val or "efectivo" in val:
        return "contado"
    if "credito" in val or "crédito" in val:
        return "credito"
    if "transfer" in val:
        return "transferencia"
    if "cheque" in val:
        return "cheque"
    return None


def _price_lines_after_unitario(lines: list[str]) -> list[int]:
    """Montos de la fila PRECIO LISTA / UNITARIO / TOTAL (tras cabeceras o timbre)."""
    start: int | None = None
    for i, line in enumerate(lines):
        low = line.lower().strip()
        if low == "unitario" or (
            "unitario" in low and "precio" not in low and len(low) < 20
        ):
            start = i + 1
            break
    if start is None:
        for i, line in enumerate(lines):
            if re.search(r"precio\s+unit", line, re.IGNORECASE):
                start = i + 1
                break
    if start is None:
        return []

    precios: list[int] = []
    for line in lines[start:]:
        if not invoice_vision._is_chilean_price_line(line):
            if precios:
                break
            continue
        m = invoice_vision._PRICE_LINE_RE.match(line.strip())
        if not m:
            continue
        val = invoice_vision._parse_monto_chileno(m.group(1))
        if val is not None and val >= 50:
            precios.append(val)
        if len(precios) >= 3:
            break
    return precios


def _extract_tecnicor_productos(lines: list[str]) -> list[dict[str, Any]]:
    productos: list[dict[str, Any]] = []
    in_items = False

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        low = line.lower()

        if re.search(r"\bcodigo\b", low) and "cliente" not in low:
            in_items = True
            i += 1
            continue
        if low in ("detalle", "descripcion", "descripción"):
            in_items = True
            i += 1
            continue

        if not in_items:
            i += 1
            continue

        if low.startswith("son:") or "subtotal neto" in low:
            break
        if low in ("pagado", "revishe", "revisado"):
            i += 1
            continue

        if _CODE_RE.fullmatch(line):
            codigo = line
            qty = 1
            j = i + 1
            if j < len(lines) and _QTY_ONLY_RE.fullmatch(lines[j].strip()):
                qv = invoice_vision._parse_monto_chileno(lines[j].strip())
                if qv is not None and 0 < qv <= 9999:
                    qty = max(1, int(round(qv)))
                j += 1
            productos.append(
                {"codigo_proveedor": codigo, "cantidad": qty, "valor_neto": None}
            )
            i = j
            continue

        i += 1

    precios = _price_lines_after_unitario(lines)
    unitario = precios[1] if len(precios) >= 2 else (precios[0] if precios else None)
    line_total = precios[2] if len(precios) >= 3 else None
    if unitario is None and precios:
        unitario = precios[0]

    for p in productos:
        if unitario is not None:
            p["valor_neto"] = unitario
        if (
            line_total
            and unitario
            and (p.get("cantidad") or 1) == 1
            and line_total > unitario * 1.5
        ):
            inferred = int(round(line_total / unitario))
            if inferred >= 2 and abs(inferred * unitario - line_total) <= max(2, inferred):
                p["cantidad"] = inferred

    return [p for p in productos if p.get("valor_neto")]


def _extract_tecnicor_footer_montos(
    lines: list[str],
) -> tuple[int | None, int | None, int | None]:
    """Pie Tecnicor: etiquetas TOTAL NETO / I.V.A / TOTAL y montos en líneas siguientes."""
    total_label_idx: int | None = None
    for i, line in enumerate(lines):
        if not re.fullmatch(r"TOTAL", line.strip(), re.IGNORECASE):
            continue
        window = " ".join(lines[max(0, i - 6) : i + 1]).lower()
        if "total neto" in window or "i.v.a" in window or "subtotal" in window:
            total_label_idx = i

    if total_label_idx is not None:
        amounts: list[int] = []
        for line in lines[total_label_idx + 1 : total_label_idx + 8]:
            if not invoice_vision._is_chilean_price_line(line):
                if amounts:
                    break
                continue
            m = invoice_vision._PRICE_LINE_RE.match(line.strip())
            if not m:
                continue
            val = invoice_vision._parse_monto_chileno(m.group(1))
            if val is not None and val >= 100:
                amounts.append(val)
        if len(amounts) >= 3:
            return amounts[0], amounts[1], amounts[2]
        if len(amounts) == 2:
            neto = amounts[0]
            iva = amounts[1]
            return neto, iva, int(neto + iva)

    for i, line in enumerate(lines):
        if re.search(r"SUBTOTAL\s+NETO", line, re.IGNORECASE):
            for nxt in lines[i + 1 : i + 4]:
                neto = invoice_vision._parse_monto_chileno(nxt)
                if neto is not None and neto >= 1000:
                    iva = int(round(neto * 0.19))
                    return neto, iva, int(neto + iva)

    return None, None, None


@registry.register
class TecnicorParser(BaseInvoiceParser):
    nombre = "tecnicor"

    def matches(self, rut: str | None, ocr_text: str) -> bool:
        return is_tecnicor_invoice(rut, ocr_text or "")

    def parse(self, data: dict[str, Any]) -> dict[str, Any]:
        texto = (data.get("ocr_texto_crudo") or "").strip()
        if not texto or not is_tecnicor_invoice(data.get("rut_proveedor"), texto):
            return data

        texto_norm = invoice_vision._normalize_ocr_text(texto)
        lines = [ln.strip() for ln in texto_norm.splitlines() if ln.strip()]

        productos = _extract_tecnicor_productos(lines)
        if productos:
            data["productos"] = productos
            data["productos_fuente"] = "tecnicor"
            data["productos_n"] = len(productos)
            p0 = productos[0]
            data["producto_codigo"] = p0.get("codigo_proveedor")
            data["producto_cantidad"] = p0.get("cantidad")
            data["producto_valor_neto"] = p0.get("valor_neto")

        neto, iva, total = _extract_tecnicor_footer_montos(lines)
        if neto is None:
            neto, iva, total = invoice_vision._extract_montos(texto_norm)

        precios = _price_lines_after_unitario(lines)
        line_total = precios[2] if len(precios) >= 3 else None
        if productos and line_total:
            suma = sum(
                (p.get("cantidad") or 1) * (p.get("valor_neto") or 0) for p in productos
            )
            if suma == line_total or (neto is not None and neto < line_total):
                neto = line_total
                iva = int(round(neto * 0.19))
                total = int(neto + iva)

        if neto is not None:
            data["total_neto"] = neto
        if iva is not None:
            data["iva"] = iva
        if total is not None:
            data["total"] = total

        metodo = _extract_tecnicor_metodo_pago(texto_norm)
        if metodo:
            data["metodo_pago"] = metodo

        if _norm_rut(data.get("rut_proveedor")) != _norm_rut(_TECNICOR_RUT):
            data["rut_proveedor"] = _TECNICOR_RUT

        return data
