"""Parser OCR Importadora y Exportadora ACD (Facto.cl, RUT 77.822.487-9).

Layout DTE: columna Glosa (sin código), cantidad «N UN», precio unitario y monto línea.
A veces hay descuento documento; el neto unitario se toma del total neto cuando aplica.
"""
from __future__ import annotations

import re
from typing import Any

from app.utils import invoice_vision

from .base import BaseInvoiceParser
from .registry import registry

_ACD_RUT = "77.822.487-9"
_QTY_UN_RE = re.compile(r"^(\d{1,4})\s*UN\b", re.IGNORECASE)
_TABLE_HEADER_RE = re.compile(
    r"^(?:glosa|cantidad|prc\.?\s*unit|pr\.?\s*unit\.?|desc(?:\.|/|\s)?(?:rcrg|/rec)?|"
    r"afecto\s*iva|imp\.?\s*esp\.?|monto|rec\.?|v\.?\s*neto(?:\s*unit\.?)?)$",
    re.IGNORECASE,
)
_HEADER_PREFIX_RE = re.compile(
    r"^(?:(?:cantidad|prc\.?\s*unit|pr\.?\s*unit\.?|desc[/\.]?(?:rcrg|/rec)?|"
    r"afecto\s*iva|imp\.?\s*esp\.?|monto|v\.?\s*neto(?:\s*unit\.?)?)\s*)+",
    re.IGNORECASE,
)
_FOOTER_RE = re.compile(
    r"(?:descuento\s+afect|monto\s+neto|timbre\s+elect|resoluci[oó]n\s+ex)",
    re.IGNORECASE,
)
_AFFECT_RE = re.compile(r"^(?:SI|NO|0)$", re.IGNORECASE)
_HEADER_WORDS = frozenset(
    {
        "glosa",
        "cantidad",
        "prc",
        "unit",
        "pr",
        "desc",
        "rcrg",
        "rec",
        "afecto",
        "iva",
        "imp",
        "esp",
        "monto",
        "v",
        "neto",
    }
)


def _norm_rut(rut: str | None) -> str:
    return re.sub(r"[^0-9kK]", "", (rut or "")).upper()


def is_acd_invoice(rut: str | None, ocr_text: str) -> bool:
    if _norm_rut(rut) == _norm_rut(_ACD_RUT):
        return True
    t = ocr_text or ""
    return bool(
        re.search(
            r"importadora\s+y\s+exportadora\s+acd|exportadora\s+acd\s+limitada|"
            r"repuestosgrupoacd",
            t,
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
        "productos detectados",
        "aplicar datos",
        "cancelar",
        "cerrar",
        "ítems api",
        "items api",
        "parser ocr",
        "fuente ítems",
        "fuente items",
        "análisis listo",
        "auto",
        "código",
        "codigo",
        "cant.",
        "v. neto",
        "v. neto unit.",
        "descripción",
        "descripcion",
        "rut proveedor",
        "método de pago",
        "metodo de pago",
    )
    if low in noise:
        return True
    if re.fullmatch(r"productos detectados\s*\(\d+\)", low):
        return True
    return False


def _line_is_only_headers(line: str) -> bool:
    cleaned = re.sub(r"[/\.]", " ", (line or "").lower())
    words = [w.rstrip(".,:;") for w in cleaned.split() if w.strip()]
    if not words:
        return True
    return all(w in _HEADER_WORDS for w in words)


def _has_product_text_after_headers(line: str) -> bool:
    cleaned = _strip_header_prefix((line or "").strip())
    if not cleaned or cleaned == (line or "").strip():
        return False
    return not _line_is_only_headers(cleaned) and bool(
        re.search(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]{4,}", cleaned)
    )


def _is_table_header_line(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return True
    if _has_product_text_after_headers(s):
        return False
    if _TABLE_HEADER_RE.fullmatch(s):
        return True
    if re.search(
        r"^(?:cantidad|prc\.?\s*unit|pr\.?\s*unit).+(?:desc|afecto|monto)",
        s,
        re.IGNORECASE,
    ):
        return True
    if re.search(r"desc[/\.]?(?:rcrg|/rec).*afecto\s*iva", s, re.IGNORECASE):
        return True
    if re.fullmatch(r"cantidad\s+(?:prc\.?\s*unit|pr\.?\s*unit\.?)", s, re.IGNORECASE):
        return True
    if _line_is_only_headers(s):
        return True
    return False


def _strip_header_prefix(line: str) -> str:
    s = (line or "").strip()
    if not s:
        return s
    m = _HEADER_PREFIX_RE.match(s)
    if not m:
        return s
    rest = s[m.end():].strip()
    if rest and re.search(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]{4,}", rest):
        return rest
    return s


def _normalize_desc_line(line: str) -> str:
    s = _strip_header_prefix((line or "").strip())
    if _is_table_header_line(s):
        return ""
    return s


def _is_valid_product_desc(desc: str) -> bool:
    desc = re.sub(r"\s+", " ", (desc or "").strip())
    if len(desc) < 4:
        return False
    if _line_is_only_headers(desc):
        return False
    if not re.search(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]{4,}", desc):
        return False
    return True


def _filter_acd_lines(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        s = (line or "").strip()
        if not s or _is_ui_noise_line(s):
            continue
        out.append(s)
    return out


def _calc_valor_neto(
    qty: int,
    unit_price: int | None,
    line_total: int | None,
    total_neto: int | None,
    *,
    single_item_doc: bool,
) -> int | float | None:
    if qty <= 0:
        return None
    if line_total and line_total > 0:
        if (
            total_neto
            and qty > 1
            and abs(qty * line_total - total_neto) <= 2
        ):
            return line_total
        if (
            single_item_doc
            and total_neto
            and qty == 1
            and line_total > total_neto
        ):
            per_unit = total_neto / qty
            return int(per_unit) if per_unit == int(per_unit) else round(per_unit, 1)
        if line_total % qty == 0:
            return line_total // qty
        return round(line_total / qty, 1)
    if unit_price and unit_price > 0:
        if (
            single_item_doc
            and total_neto
            and qty == 1
            and abs(unit_price - total_neto) <= 2
        ):
            return total_neto
        return unit_price
    if single_item_doc and total_neto and total_neto > 0:
        per_unit = total_neto / qty
        return int(per_unit) if per_unit == int(per_unit) else round(per_unit, 1)
    return None


def _normalize_desc_merge_key(desc: str) -> str:
    """Clave de fusión tolerante a puntuación OCR (IZQ. vs IZQ)."""
    s = re.sub(r"[^\w\s]", " ", (desc or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def _collapse_acd_similar_products(
    productos: list[dict[str, Any]], total_neto: int | None = None
) -> list[dict[str, Any]]:
    """Agrupa filas con la misma glosa (variantes OCR) y corrige qty duplicada."""
    if len(productos) <= 1:
        return productos
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for p in productos:
        key = _normalize_desc_merge_key(p.get("descripcion") or "")
        if not key:
            key = f"__row_{len(order)}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(p)
    if len(order) == len(productos):
        return productos

    collapsed: list[dict[str, Any]] = []
    for key in order:
        rows = groups[key]
        if len(rows) == 1:
            collapsed.append(dict(rows[0]))
            continue
        qty = sum(int(r.get("cantidad") or 0) for r in rows)
        line_total = next((r.get("_line_total") for r in rows if r.get("_line_total")), None)
        unit_price = next((r.get("_unit_price") for r in rows if r.get("_unit_price")), None)
        best_desc = max(((r.get("descripcion") or "") for r in rows), key=len)
        if total_neto and line_total and qty > 0:
            projected = qty * line_total
            if projected > total_neto + 2:
                qty = 1
        elif len(rows) > 1 and line_total and not total_neto:
            qty = 1
        collapsed.append(
            {
                "codigo_proveedor": rows[0].get("codigo_proveedor") or "",
                "descripcion": best_desc,
                "cantidad": max(1, qty),
                "valor_neto": 0,
                "_unit_price": unit_price,
                "_line_total": line_total,
            }
        )
    return collapsed


def _scan_acd_amounts(texto: str) -> list[int]:
    """Montos chilenos en todo el OCR (tolerante a espacios OCR)."""
    seen: set[int] = set()
    out: list[int] = []
    for raw in re.findall(r"\$\s*[\d\s.,]+", texto or ""):
        val = _parse_acd_price(re.sub(r"\s+", "", raw))
        if val and val >= 100 and val not in seen:
            seen.add(val)
            out.append(val)
    for m in re.finditer(r"(?<!\d)([\d]{1,3}\.[\d]{3})(?:,[\d]{1,2})?(?!\d)", texto or ""):
        val = _parse_acd_price(m.group(0))
        if val and 500 <= val <= 50_000_000 and val not in seen:
            seen.add(val)
            out.append(val)
    return out


def _pick_acd_footer_triplet(
    amounts: list[int],
) -> tuple[int | None, int | None, int | None]:
    """Neto + IVA 19% + total coherentes entre montos del documento."""
    uniq = sorted(set(amounts))
    best: tuple[int | None, int | None, int | None] = (None, None, None)
    best_score = -1
    for neto in uniq:
        if not (1_000 <= neto <= 5_000_000):
            continue
        iva_exp = int(round(neto * 0.19))
        total_exp = neto + iva_exp
        tol = max(50, int(round(neto * 0.025)))
        iva_hits = [a for a in uniq if abs(a - iva_exp) <= tol]
        total_hits = [a for a in uniq if abs(a - total_exp) <= tol]
        if not iva_hits or not total_hits:
            continue
        score = 3
        if 10_000 <= neto <= 500_000:
            score += 1
        if score > best_score:
            best = (neto, iva_hits[0], total_hits[0])
            best_score = score
    return best


def _acd_products_same_family(productos: list[dict[str, Any]]) -> bool:
    keys = [_normalize_desc_merge_key(p.get("descripcion") or "") for p in productos]
    keys = [k for k in keys if k]
    if not keys:
        return True
    if len(set(keys)) == 1:
        return True
    base = keys[0].split()
    for k in keys[1:]:
        words = k.split()
        if len(base) >= 4 and words[:4] == base[:4]:
            continue
        overlap = len(set(base) & set(words))
        if overlap < max(4, int(min(len(set(base)), len(set(words))) * 0.65)):
            return False
    return True


def _resolve_acd_document_totals(
    data: dict[str, Any],
    lines: list[str],
    texto_norm: str,
    productos: list[dict[str, Any]] | None = None,
) -> tuple[int | None, int | None, int | None]:
    """Prioriza pie Facto; si el neto global viene inflado por líneas duplicadas, corrige."""
    neto, iva, total = _extract_acd_footer_montos(lines)
    if neto is None:
        neto, iva, total = _pick_acd_footer_triplet(_scan_acd_amounts(texto_norm))

    suma = 0
    if productos:
        suma = sum((p.get("cantidad") or 1) * (p.get("valor_neto") or 0) for p in productos)

    wrong = data.get("total_neto")
    if isinstance(wrong, str):
        wrong = invoice_vision._parse_monto_chileno(wrong)

    if neto is not None and wrong and suma and abs(wrong - suma) <= max(2, len(productos or [])):
        if abs(wrong - neto) > max(500, int(neto * 0.05)):
            pass
        elif wrong <= neto + max(2, int(neto * 0.01)):
            neto = wrong
    elif neto is None and wrong and suma and abs(wrong - suma) <= max(2, len(productos or [])):
        neto, iva, total = _pick_acd_footer_triplet(_scan_acd_amounts(texto_norm))

    if neto is None and total and iva and total > iva:
        neto = total - iva
    if neto and iva is None and total and total > neto:
        iva = total - neto
    if neto and iva and total is None:
        total = neto + iva
    return neto, iva, total


def _parse_acd_footer_amount(line: str) -> int | None:
    s = (line or "").strip()
    if not s:
        return None
    val = _parse_acd_price(s)
    if val:
        return val
    m = re.search(r"([\d.,]+)\s*$", s)
    if m:
        return invoice_vision._parse_monto_chileno(m.group(1))
    return None


def _parse_acd_footer_next(lines: list[str], idx: int) -> int | None:
    for nxt in lines[idx + 1 : idx + 4]:
        if _FOOTER_RE.search(nxt) and not re.search(r"descuento", nxt, re.IGNORECASE):
            break
        val = _parse_acd_footer_amount(nxt)
        if val is not None and val >= 100:
            return val
    return None


def _extract_acd_footer_montos(lines: list[str]) -> tuple[int | None, int | None, int | None]:
    """Pie Facto: Monto Neto / IVA 19% / Total (prioridad sobre montos de línea OCR)."""
    filtered = _filter_acd_lines([ln.strip() for ln in lines if ln.strip()])
    neto, iva, total = invoice_vision._extract_dte_footer_montos(filtered)
    if neto is not None and (iva is not None or total is not None):
        if iva is None and total is not None and total > neto:
            iva = total - neto
        if total is None and iva is not None:
            total = neto + iva
        return neto, iva, total

    neto = iva = total = None
    for i, line in enumerate(filtered):
        low = line.lower()
        if re.search(r"monto\s+neto", low):
            neto = _parse_acd_footer_amount(line) or _parse_acd_footer_next(filtered, i)
        elif re.search(r"iva\s+19\s*%", low) or re.fullmatch(r"iva\s*:?\s*", low):
            iva = _parse_acd_footer_amount(line) or _parse_acd_footer_next(filtered, i)
        elif re.fullmatch(r"total\s*:?\s*", low) or (
            re.match(r"^total\b", low) and _parse_acd_footer_amount(line)
        ):
            total = _parse_acd_footer_amount(line) or _parse_acd_footer_next(filtered, i)

    if neto is None:
        return None, None, None
    if iva is None and total is not None and total > neto:
        iva = total - neto
    if total is None and iva is not None:
        total = neto + iva
    return neto, iva, total


def _reconcile_acd_productos_with_footer(
    productos: list[dict[str, Any]],
    total_neto: int | None,
    *,
    texto_norm: str = "",
) -> list[dict[str, Any]]:
    """Unifica duplicados OCR cuando la suma de líneas supera el neto del pie."""
    if not productos or len(productos) < 2:
        return productos

    neto = total_neto
    if neto is None and texto_norm:
        neto, _, _ = _pick_acd_footer_triplet(_scan_acd_amounts(texto_norm))
    if not neto:
        return productos

    suma = sum((p.get("cantidad") or 1) * (p.get("valor_neto") or 0) for p in productos)
    tol = max(2, int(neto * 0.01))
    if suma <= neto + tol:
        return productos
    if not _acd_products_same_family(productos):
        return productos

    best = max(productos, key=lambda p: len(p.get("descripcion") or ""))
    return [
        {
            "codigo_proveedor": best.get("codigo_proveedor") or "",
            "descripcion": best.get("descripcion") or "",
            "cantidad": 1,
            "valor_neto": neto,
        }
    ]


def _parse_acd_price(line: str) -> int | None:
    """Monto de línea Facto; tolera prefijo «$» que _parse_monto_chileno no acepta."""
    s = re.sub(r"^[\$\s]+", "", (line or "").strip())
    if not s:
        return None
    return invoice_vision._parse_monto_chileno(s)


def _merge_duplicate_productos(
    productos: list[dict[str, Any]], total_neto: int | None = None
) -> list[dict[str, Any]]:
    """Fusiona filas repetidas; no suma cantidad si el neto del documento indica un solo ítem."""
    merged: list[dict[str, Any]] = []
    for p in productos:
        desc_key = _normalize_desc_merge_key(p.get("descripcion") or "")
        key = (desc_key, p.get("valor_neto"))
        found = False
        for m in merged:
            m_key = (
                _normalize_desc_merge_key(m.get("descripcion") or ""),
                m.get("valor_neto"),
            )
            if m_key == key:
                line_total = p.get("_line_total") or m.get("_line_total")
                new_qty = int(m.get("cantidad") or 0) + int(p.get("cantidad") or 0)
                if total_neto and line_total and abs(line_total - total_neto) <= 2:
                    found = True
                    break
                if (
                    total_neto
                    and line_total
                    and line_total > 0
                    and new_qty * line_total > total_neto + 2
                ):
                    found = True
                    break
                m["cantidad"] = new_qty
                found = True
                break
        if not found:
            merged.append(dict(p))
    return merged


def _extract_acd_numero_documento(texto: str) -> str | None:
    """Folio Facto: «N 270», «N° 270», «N ° 270» cerca de FACTURA ELECTRÓNICA."""
    if not texto:
        return None
    folio_pat = r"N\s*[°º*]?\s*0*(\d{2,6})\b"
    m = re.search(
        r"FACTURA\s+ELECTR[OÓ]NICA[\s\S]{0,120}?" + folio_pat,
        texto,
        re.IGNORECASE,
    )
    if m:
        return (m.group(1) or "").lstrip("0") or m.group(1)
    for line in texto.splitlines():
        s = line.strip()
        if not s or re.search(r"rut\s*:", s, re.IGNORECASE):
            continue
        m = re.match(folio_pat, s, re.IGNORECASE)
        if m:
            num = (m.group(1) or "").lstrip("0") or m.group(1)
            if num and len(num) <= 6:
                return num
    return None


def _find_glosa_section_start(lines: list[str]) -> int | None:
    for i, line in enumerate(lines):
        if re.fullmatch(r"glosa", line.strip(), re.IGNORECASE):
            return i
    for i, line in enumerate(lines):
        s = line.strip()
        if re.fullmatch(r"cantidad\s+(?:prc\.?\s*unit|pr\.?\s*unit\.?)", s, re.IGNORECASE):
            return i
        if re.search(r"desc[/\.]?(?:rcrg|/rec).*afecto\s*iva", s, re.IGNORECASE):
            return i
    return None


def _extract_dte_productos_sin_codigo(texto: str) -> list[dict[str, Any]]:
    """Ítems Facto DTE sin VlrCodigo (solo NmbItem + QtyItem + PrcItem)."""
    if not texto or "<Detalle" not in texto:
        return []
    productos: list[dict[str, Any]] = []
    for block in re.findall(r"<Detalle\b[^>]*>.*?</Detalle>", texto, re.IGNORECASE | re.DOTALL):
        nmb_m = re.search(r"<NmbItem>([^<]+)</NmbItem>", block, re.IGNORECASE)
        qty_m = re.search(r"<QtyItem>([^<]+)</QtyItem>", block, re.IGNORECASE)
        prc_m = re.search(r"<PrcItem>([^<]+)</PrcItem>", block, re.IGNORECASE)
        if not nmb_m or not prc_m:
            continue
        desc = re.sub(r"\s+", " ", (nmb_m.group(1) or "").strip())[:255]
        if not _is_valid_product_desc(desc):
            continue
        prc_val = invoice_vision._parse_monto_chileno(prc_m.group(1))
        if not prc_val or prc_val <= 0:
            continue
        qty_raw = invoice_vision._parse_monto_chileno(qty_m.group(1) if qty_m else "1")
        qty = max(1, int(round(qty_raw or 1)))
        productos.append(
            {
                "codigo_proveedor": "",
                "descripcion": desc,
                "cantidad": qty,
                "valor_neto": int(round(prc_val)),
            }
        )
    return productos


def _extract_acd_productos_by_qty_blocks(
    lines: list[str], total_neto: int | None = None
) -> list[dict[str, Any]]:
    """Respaldo: localiza bloques «descripción + N UN + precios» sin cabecera Glosa."""
    productos: list[dict[str, Any]] = []
    past_header = False
    for i, line in enumerate(lines):
        s = line.strip()
        if re.search(r"factura\s+elect", s, re.IGNORECASE):
            past_header = True
        if not past_header or _FOOTER_RE.search(s):
            continue
        m_qty = _QTY_UN_RE.match(s)
        if not m_qty:
            continue
        qty = int(m_qty.group(1))
        desc_parts: list[str] = []
        j = i - 1
        while j >= 0 and len(desc_parts) < 6:
            prev = lines[j].strip()
            if _FOOTER_RE.search(prev) or _QTY_UN_RE.match(prev):
                break
            if _is_table_header_line(prev) and not desc_parts:
                j -= 1
                continue
            cleaned = _normalize_desc_line(prev)
            if cleaned and invoice_vision._looks_like_product_description(cleaned):
                desc_parts.insert(0, cleaned)
                j -= 1
                continue
            if desc_parts:
                break
            j -= 1

        unit_price: int | None = None
        line_total: int | None = None
        k = i + 1
        while k < len(lines) and k < i + 8:
            nxt = lines[k].strip()
            if _FOOTER_RE.search(nxt) or _QTY_UN_RE.match(nxt):
                break
            if invoice_vision._is_chilean_price_line(nxt):
                val = _parse_acd_price(nxt)
                if val and val > 0:
                    if unit_price is None:
                        unit_price = val
                    line_total = val
                k += 1
                continue
            if _AFFECT_RE.fullmatch(nxt):
                k += 1
                continue
            if _normalize_desc_line(nxt) and invoice_vision._looks_like_product_description(
                _normalize_desc_line(nxt)
            ):
                break
            k += 1

        desc = re.sub(r"\s+", " ", " ".join(desc_parts)).strip()[:255]
        if not _is_valid_product_desc(desc):
            continue
        productos.append(
            {
                "codigo_proveedor": "",
                "descripcion": desc,
                "cantidad": qty,
                "valor_neto": 0,
                "_unit_price": unit_price,
                "_line_total": line_total,
            }
        )

    return _finalize_acd_productos(productos, total_neto)


def _finalize_acd_productos(
    productos: list[dict[str, Any]], total_neto: int | None
) -> list[dict[str, Any]]:
    productos = _collapse_acd_similar_products(productos, total_neto)
    productos = _merge_duplicate_productos(productos, total_neto)
    single_item_doc = len(productos) == 1
    finalized: list[dict[str, Any]] = []
    for p in productos:
        valor_neto = _calc_valor_neto(
            int(p.get("cantidad") or 0),
            p.pop("_unit_price", None),
            p.pop("_line_total", None),
            total_neto,
            single_item_doc=single_item_doc,
        )
        if not valor_neto or valor_neto <= 0:
            continue
        p["valor_neto"] = valor_neto
        finalized.append(p)
    return finalized


def _extract_acd_productos(
    lines: list[str], total_neto: int | None = None
) -> list[dict[str, Any]]:
    lines = _filter_acd_lines(lines)

    glosa_idx = _find_glosa_section_start(lines)
    if glosa_idx is None:
        return _extract_acd_productos_by_qty_blocks(lines, total_neto)

    start = glosa_idx + 1
    while start < len(lines) and _is_table_header_line(lines[start]):
        start += 1

    end = len(lines)
    for i in range(start, len(lines)):
        if _FOOTER_RE.search(lines[i]):
            end = i
            break

    productos: list[dict[str, Any]] = []
    i = start
    while i < end:
        desc_parts: list[str] = []
        qty: int | None = None

        while i < end:
            s = lines[i].strip()
            if not s:
                i += 1
                continue
            m_qty = _QTY_UN_RE.match(s)
            if m_qty:
                qty = int(m_qty.group(1))
                i += 1
                break
            if invoice_vision._is_chilean_price_line(s) or _AFFECT_RE.fullmatch(s):
                break
            cleaned = _normalize_desc_line(s)
            if not cleaned:
                i += 1
                continue
            if invoice_vision._looks_like_product_description(cleaned) or (
                desc_parts and re.search(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]", cleaned)
            ):
                desc_parts.append(cleaned)
                i += 1
                continue
            if desc_parts:
                break
            i += 1

        if not desc_parts or not qty or qty <= 0:
            if qty is None and not desc_parts:
                i += 1
            continue

        unit_price: int | None = None
        line_total: int | None = None
        while i < end:
            s = lines[i].strip()
            if not s:
                i += 1
                continue
            if _FOOTER_RE.search(s):
                break
            if _QTY_UN_RE.match(s):
                break
            cleaned = _normalize_desc_line(s)
            if cleaned and invoice_vision._looks_like_product_description(cleaned):
                break
            if invoice_vision._is_chilean_price_line(s):
                val = _parse_acd_price(s)
                if val and val > 0:
                    if unit_price is None:
                        unit_price = val
                    line_total = val
                i += 1
                continue
            if _AFFECT_RE.fullmatch(s):
                i += 1
                continue
            break

        desc = re.sub(r"\s+", " ", " ".join(desc_parts)).strip()[:255]
        if not _is_valid_product_desc(desc):
            continue

        productos.append(
            {
                "codigo_proveedor": "",
                "descripcion": desc,
                "cantidad": qty,
                "valor_neto": 0,
                "_unit_price": unit_price,
                "_line_total": line_total,
            }
        )

    finalized = _finalize_acd_productos(productos, total_neto)

    if not finalized:
        finalized = _extract_acd_productos_by_qty_blocks(lines, total_neto)
    return finalized


def _extract_acd_metodo_pago(texto: str) -> str | None:
    m = re.search(
        r"condiciones\s+de\s+pago\s*:?\s*([A-Za-zÁÉÍÓÚáéíóúñ]+)",
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
class AcdParser(BaseInvoiceParser):
    nombre = "acd"

    def matches(self, rut: str | None, ocr_text: str) -> bool:
        return is_acd_invoice(rut, ocr_text)

    def parse(self, data: dict[str, Any]) -> dict[str, Any]:
        texto = (data.get("ocr_texto_crudo") or "").strip()
        if not texto or not is_acd_invoice(data.get("rut_proveedor"), texto):
            return data

        texto_norm = invoice_vision._normalize_ocr_text(texto)
        lines = [ln.strip() for ln in texto_norm.splitlines() if ln.strip()]

        if _norm_rut(data.get("rut_proveedor")) != _norm_rut(_ACD_RUT):
            data["rut_proveedor"] = _ACD_RUT

        footer_neto, footer_iva, footer_total = _resolve_acd_document_totals(
            data, lines, texto_norm, None
        )
        if footer_neto is not None:
            data["total_neto"] = footer_neto
        if footer_iva is not None:
            data["iva"] = footer_iva
        if footer_total is not None:
            data["total"] = footer_total

        total_neto = data.get("total_neto")
        if isinstance(total_neto, str):
            total_neto = invoice_vision._parse_monto_chileno(total_neto)

        productos = _extract_acd_productos(lines, total_neto)
        productos_fuente = "acd_glosa"
        if not productos:
            productos = _extract_dte_productos_sin_codigo(texto_norm)
            if productos:
                single = len(productos) == 1
                for p in productos:
                    qty = int(p.get("cantidad") or 1)
                    vn = p.get("valor_neto")
                    if (
                        single
                        and total_neto
                        and qty == 1
                        and isinstance(vn, (int, float))
                        and vn > total_neto
                    ):
                        p["valor_neto"] = total_neto
                productos_fuente = "acd_dte_xml"

        footer_neto, footer_iva, footer_total = _resolve_acd_document_totals(
            data, lines, texto_norm, productos
        )
        if footer_neto is not None:
            data["total_neto"] = footer_neto
            total_neto = footer_neto
        if footer_iva is not None:
            data["iva"] = footer_iva
        if footer_total is not None:
            data["total"] = footer_total

        productos = _reconcile_acd_productos_with_footer(
            productos, total_neto, texto_norm=texto_norm
        )
        if productos:
            data["productos"] = productos
            data["productos_fuente"] = productos_fuente
            data["productos_n"] = len(productos)
            p0 = productos[0]
            data["producto_codigo"] = p0.get("codigo_proveedor") or ""
            data["producto_cantidad"] = p0.get("cantidad")
            data["producto_valor_neto"] = p0.get("valor_neto")
            if p0.get("descripcion"):
                data["producto_descripcion"] = p0["descripcion"]

        mp = _extract_acd_metodo_pago(texto_norm)
        if mp:
            data["metodo_pago"] = mp

        if not data.get("numero_documento"):
            nd = _extract_acd_numero_documento(texto_norm)
            if nd:
                data["numero_documento"] = nd

        return data
