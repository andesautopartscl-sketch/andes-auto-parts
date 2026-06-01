"""Análisis de facturas chilenas vía Google Cloud Vision OCR."""
from __future__ import annotations

import base64
import logging
import os
import re
from pathlib import Path
from typing import Any

from google.cloud import vision
from google.oauth2 import service_account

MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_PDF_BYTES = 12 * 1024 * 1024
VISION_SCOPES = ["https://www.googleapis.com/auth/cloud-vision"]

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger(__name__)


def _credentials_path() -> Path:
    raw = (
        os.environ.get("GOOGLE_VISION_CREDENTIALS") or "data/google_service_account.json"
    ).strip()
    path = Path(raw)
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    return path


def _parse_monto_chileno(raw: str) -> int | None:
    s = (raw or "").strip()
    if not s:
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
        s = s.replace(".", "")
    try:
        v = float(s)
    except ValueError:
        return None
    if v < 0:
        return None
    return int(round(v))


def _as_monto_int(val: int | float | None) -> int | None:
    if val is None:
        return None
    return int(round(float(val)))


_RUT_RE = re.compile(
    r"\b(\d{1,2}\.?\d{3}\.?\d{3}-[\dkK]|\d{7,8}-[\dkK])\b",
    re.IGNORECASE,
)


_RECEPTOR_KW = ("receptor", "señor", "senor", "señores", "senores", "cliente", "comprador", "destinatario")
_RUT_COLON_RE = re.compile(
    r"R\.?\s*U\.?\s*T\.?\s*:\s*(\d{1,2}\.?\d{3}\.?\d{3}-[\dkK]|\d{7,8}-[\dkK])",
    re.IGNORECASE,
)


def _is_receptor_context(texto: str, rut_start: int, lookback: int = 120) -> bool:
    """True si el RUT aparece en contexto de receptor/comprador."""
    ctx = texto[max(0, rut_start - lookback) : rut_start].lower()
    return any(k in ctx for k in _RECEPTOR_KW)


def _extract_rut_emisor(texto: str) -> str | None:
    """Prioriza RUT del emisor/proveedor, no del receptor/comprador."""
    # Boleta térmica: el RUT del emisor aparece pegado al inicio ("RUT:84.726.100-5").
    # Suele ser el primero del documento, antes del RUT del cliente/receptor.
    m = re.search(r"RUT:\s*(\d{1,2}\.\d{3}\.\d{3}-[\dkK])", texto, re.IGNORECASE)
    if m and not _is_receptor_context(texto, m.start()):
        return m.group(1)

    emisor_patterns = [
        r"R\.?\s*U\.?\s*T\.?\s*Emisor\s*:?\s*(\d{1,2}\.?\d{3}\.?\d{3}-[\dkK]|\d{7,8}-[\dkK])",
        r"Emisor[\s\S]{0,120}?R\.?\s*U\.?\s*T\.?\s*:?\s*(\d{1,2}\.?\d{3}\.?\d{3}-[\dkK]|\d{7,8}-[\dkK])",
        r"RUT[\s\S]{0,30}?Emisor[\s\S]{0,80}?(\d{1,2}\.?\d{3}\.?\d{3}-[\dkK]|\d{7,8}-[\dkK])",
    ]
    for pat in emisor_patterns:
        m = re.search(pat, texto, re.IGNORECASE)
        if m:
            return m.group(1)

    # Header emisor: "R.U.T.: XX.XXX.XXX-X" (con dos puntos; receptor suele ir sin ":")
    for m in _RUT_COLON_RE.finditer(texto):
        if not _is_receptor_context(texto, m.start()):
            return m.group(1)

    candidates: list[tuple[int, int, str]] = []
    for m in _RUT_RE.finditer(texto):
        rut = m.group(1)
        if _is_receptor_context(texto, m.start()):
            continue
        start = max(0, m.start() - 80)
        end = min(len(texto), m.end() + 80)
        ctx = texto[start:end].lower()
        score = 0
        pre = texto[max(0, m.start() - 30) : m.start()]
        if re.search(r"R\.?\s*U\.?\s*T\.?\s*:\s*$", pre, re.IGNORECASE):
            score += 100
        if any(k in ctx for k in ("emisor", "proveedor", "vendedor", "razón social", "razon social")):
            score += 60
        if re.search(r"R\.?\s*U\.?\s*T\.?\s+(?!:)", pre, re.IGNORECASE):
            score -= 30
        if any(k in ctx for k in ("timbre", "sii", "resolución", "resolucion", "electrónica", "electronica")):
            score -= 35
        candidates.append((score, m.start(), rut))

    if not candidates:
        return None

    candidates.sort(key=lambda x: (-x[0], x[1]))
    if candidates[0][0] > 0:
        return candidates[0][2]
    return candidates[0][2]


def _extract_fecha_emision(texto: str) -> str | None:
    """Detecta fecha de emisión; prioriza etiquetas y fechas recientes (>= 2020)."""
    candidates: list[tuple[int, tuple[int, int, int]]] = []

    def add(d: int, mo: int, y: int, score: int) -> None:
        if y < 100:
            y += 2000
        if not (1 <= d <= 31 and 1 <= mo <= 12 and 1990 <= y <= 2100):
            return
        if y < 2020:
            score -= 40
        candidates.append((score, (y, mo, d)))

    emision_label = r"(?:fecha\s*(?:de\s*)?emisi[oó]n|fecha\s+documento|fch\s*emisi[oó]n)"
    for m in re.finditer(
        emision_label + r"[\s\S]{0,40}?(\d{4})\s*[-/.\s]\s*(\d{1,2})\s*[-/.\s]\s*(\d{1,2})",
        texto,
        re.IGNORECASE,
    ):
        if m.group(1) and m.group(2) and m.group(3):
            add(int(m.group(3)), int(m.group(2)), int(m.group(1)), 130)
    for m in re.finditer(
        emision_label + r"[\s\S]{0,40}?(\d{1,2})\s*[-/.\s]\s*(\d{1,2})\s*[-/.\s]\s*(\d{4})",
        texto,
        re.IGNORECASE,
    ):
        if m.group(1) and m.group(2) and m.group(3):
            add(int(m.group(1)), int(m.group(2)), int(m.group(3)), 130)

    for m in re.finditer(r"(\d{4})\s*-\s*(\d{1,2})\s*-\s*(\d{1,2})\b", texto):
        add(int(m.group(3)), int(m.group(2)), int(m.group(1)), 85)

    for m in re.finditer(r"\b(\d{2})[/-](\d{2})[/-](\d{4})\b", texto):
        add(int(m.group(1)), int(m.group(2)), int(m.group(3)), 55)

    if not candidates:
        return None

    recent = [c for c in candidates if c[1][0] >= 2020]
    pool = recent if recent else candidates
    pool.sort(key=lambda c: (-c[0], -c[1][0], -c[1][1], -c[1][2]))
    y, mo, d = pool[0][1]
    return f"{d:02d}-{mo:02d}-{y}"


def _extract_numero_documento(texto: str) -> str | None:
    # Alta prioridad: boletas/facturas térmicas ("NUMERO :0000335612", "N° FACTURA: 0012")
    priority_patterns = [
        r"NUMERO\s*:?\s*0*(\d+)",
        r"N[°º]?\s*FACTURA\s*:?\s*0*(\d+)",
    ]
    for pat in priority_patterns:
        m = re.search(pat, texto, re.IGNORECASE)
        if m and m.group(1):
            return m.group(1).lstrip("0") or m.group(1)

    patterns = [
        r"Folio\s*(?:Electr[oó]nico\s*)?(?:N[°º]?\s*)?:?\s*(\d{3,12})",
        r"Folio\s+N\s*(\d{3,12})",
        r"(?:N[°º]|Nro\.?\s*|Factura\s*(?:N[°º])?)\s*:?\s*(\d{3,12})",
        r"eDocument[\s\S]{0,40}?(\d{6,12})",
    ]
    for pat in patterns:
        m = re.search(pat, texto, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def _extract_montos(texto: str) -> tuple[int | None, int | None, int | None]:
    total_neto = None
    iva = None
    total = None
    lines = [ln.strip() for ln in (texto or "").splitlines() if ln.strip()]

    neto_patterns = [
        r"(?:Monto\s+)?Neto\s*(?:Exento)?\s*:?\s*\$?\s*([\d.,]+)",
        r"Sub\s*Total\s+Neto\s*:?\s*\$?\s*([\d.,]+)",
        r"Total\s+Neto\s*:?\s*\$?\s*([\d.,]+)",
    ]
    for pat in neto_patterns:
        m = re.search(pat, texto, re.IGNORECASE)
        if m:
            total_neto = _parse_monto_chileno(m.group(1))
            if total_neto is not None:
                break

    if total_neto is None:
        for idx, line in enumerate(lines):
            if re.fullmatch(r"neto\s*:?\s*", line, re.IGNORECASE):
                for nxt in lines[idx + 1 : idx + 4]:
                    m = re.match(r"^[\$]?\s*([\d.,]+)\s*$", nxt)
                    if m:
                        total_neto = _parse_monto_chileno(m.group(1))
                        if total_neto is not None:
                            break
                if total_neto is not None:
                    break

    iva_patterns = [
        r"19\s*%\s*I\.?\s*V\.?\s*A\.?\s*:?\s*\$?\s*([\d.,]+)",
        r"(?:Monto\s+)?IVA\s*(?:\(\s*19\s*%\s*\)|19\s*%)\s*:?\s*\$?\s*([\d.,]+)",
        r"IVA\s*:?\s*\$?\s*([\d.,]+)",
    ]
    for pat in iva_patterns:
        for m in re.finditer(pat, texto, re.IGNORECASE):
            val = _parse_monto_chileno(m.group(1))
            if val is not None and val >= 100:
                iva = val
                break
        if iva is not None:
            break

    if iva is None:
        for idx, line in enumerate(lines):
            if re.search(r"19\s*%\s*I\.?\s*V\.?\s*A", line, re.IGNORECASE):
                tail = re.search(r"([\d.,]+)\s*$", line)
                if tail:
                    val = _parse_monto_chileno(tail.group(1))
                    if val is not None and val >= 100:
                        iva = val
                        break
                for nxt in lines[idx + 1 : idx + 4]:
                    m = re.match(r"^[\$]?\s*([\d.,]+)\s*$", nxt)
                    if m:
                        val = _parse_monto_chileno(m.group(1))
                        if val is not None and val >= 100:
                            iva = val
                            break
                if iva is not None:
                    break

    total_patterns = [
        r"(?:^|\n)\s*Total\s*:?\s*\$?\s*([\d.,]+)",
        r"Total\s*(?:a\s+Pagar|General|Documento)\s*:?\s*\$?\s*([\d.,]+)",
        r"Monto\s+Total\s*:?\s*\$?\s*([\d.,]+)",
    ]
    for pat in total_patterns:
        m = re.search(pat, texto, re.IGNORECASE | re.MULTILINE)
        if m:
            total = _parse_monto_chileno(m.group(1))
            if total is not None:
                break

    if total is None:
        for idx, line in enumerate(lines):
            if re.fullmatch(r"total\s*:?\s*", line, re.IGNORECASE):
                for nxt in lines[idx + 1 : idx + 4]:
                    m = re.match(r"^[\$]?\s*([\d.,]+)\s*$", nxt)
                    if m:
                        total = _parse_monto_chileno(m.group(1))
                        if total is not None:
                            break
                if total is not None:
                    break

    if iva and total_neto and iva > total_neto:
        total_neto, iva = iva, total_neto

    if total_neto and iva and iva < 100 and total_neto > 500:
        if total:
            total_neto = int(round(total / 1.19))
            iva = int(round(total - total_neto))
        else:
            iva = int(round(total_neto * 0.19))

    if total and (total_neto is None or iva is None):
        calc_neto = int(round(total / 1.19))
        calc_iva = int(round(total - calc_neto))
        if total_neto is None:
            total_neto = calc_neto
        if iva is None:
            iva = calc_iva

    total_neto = _as_monto_int(total_neto)
    iva = _as_monto_int(iva)
    total = _as_monto_int(total)

    if total is None and total_neto is not None and iva is not None:
        total = int(total_neto + iva)
    elif total is not None and total_neto is not None and iva is not None:
        if total_neto + iva != total:
            total_neto = int(round(total / 1.19))
            iva = int(total - total_neto)

    return total_neto, iva, total


_CODE_LINE_RE = re.compile(r"^[\|\s]*([A-Z0-9]{4,10})\s*$", re.IGNORECASE)
_QTY_DESC_RE = re.compile(r"^(\d{1,2})\s+([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ0-9 /.\-]{9,})\s*$", re.IGNORECASE)
_PRICE_LINE_RE = re.compile(r"^[\$]?\s*([\d.,]+)\s*$")
_PRODUCT_HEADER_WORDS = frozenset(
    {
        "codigo",
        "código",
        "cantidad",
        "descripcion",
        "descripción",
        "valor",
        "precio unit",
        "precio unit.",
        "precio unitario",
    }
)
_OLD_PRODUCT_LINE_RE = re.compile(
    r"^\s*([A-Z0-9]{4,10})\s+(\d{1,5})\s+(.+?)\s+([\d.,]+)\s+([\d.,]+)\s*$",
    re.IGNORECASE,
)

# Boleta térmica (ej: Fitalia): el ítem viene en líneas separadas, con posible
# ruido OCR entre el header y la fila real:
#   CODIGO-MARCA DESCRIPCION
#   CANTIDAD,00UN X
#   PRECIO_UNIT
#   0= TOTAL
_THERMAL_CODE_RE = re.compile(r"^\s*([A-Z0-9]{3,15}-[A-Z]{2,10})\s+(.+)$", re.IGNORECASE)
_THERMAL_QTY_RE = re.compile(r"^\s*(\d+),\d+\s*UN\b", re.IGNORECASE)
_THERMAL_PRICE_RE = re.compile(r"^[\$]?\s*([\d.,]+)\s*$")


_CODE_STOPWORDS = frozenset(
    {
        "CODIGO",
        "CÓDIGO",
        "CANTIDAD",
        "DESCRIPCION",
        "DESCRIPCIÓN",
        "COMUNA",
        "CIUDAD",
        "REGION",
        "REGIÓN",
        "TELEFONO",
        "TELÉFONO",
        "FOLIO",
        "FECHA",
        "NETO",
        "TOTAL",
        "VALOR",
        "SANTIAGO",
        "TALLERES",
        "TRANSPORTE",
        "VENDEDOR",
    }
)


def _is_product_code_token(token: str) -> bool:
    t = (token or "").strip().upper()
    if not t or len(t) < 4 or len(t) > 10:
        return False
    if not re.fullmatch(r"[A-Z0-9]+", t):
        return False
    if t in _CODE_STOPWORDS:
        return False
    if not any(c.isdigit() for c in t):
        return False
    return True


def _is_codigo_header(line: str) -> bool:
    low = line.lower().strip().strip(":").strip()
    if low in ("codigo", "código"):
        return True
    return bool(re.match(r"^c[oó]digo", low))


def _normalize_ocr_text(texto: str) -> str:
    """Normaliza saltos y artefactos OCR (PDF/imagen) antes de parsear."""
    lines: list[str] = []
    for raw in (texto or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r"^[\|\s]+", "", line)
        lines.append(line)
    return "\n".join(lines)


def _extract_codigos_bloque_consecutivo(lines: list[str]) -> list[str]:
    """Respaldo: bloque de 2+ líneas consecutivas solo-código."""
    best: list[str] = []
    current: list[str] = []
    seen: set[str] = set()

    def flush() -> None:
        nonlocal best, current
        if len(current) > len(best):
            best = current[:]
        current = []

    for line in lines:
        m = _CODE_LINE_RE.match(line)
        if m and _is_product_code_token(m.group(1)):
            code = m.group(1).upper()
            if code not in seen:
                current.append(code)
                seen.add(code)
            continue
        flush()
    flush()
    return best if len(best) >= 2 else []


def _extract_codigos_producto(lines: list[str]) -> list[str]:
    """Bloque de códigos tras encabezado 'Código' (líneas solo-código)."""
    codigos: list[str] = []
    seen: set[str] = set()
    start_idx: int | None = None

    for idx, line in enumerate(lines):
        if _is_codigo_header(line):
            start_idx = idx + 1
            break

    if start_idx is None:
        for idx, line in enumerate(lines):
            low = line.lower().strip().strip(":").strip()
            if low == "cantidad":
                start_idx = idx + 1
                break

    if start_idx is None:
        return _extract_codigos_bloque_consecutivo(lines)

    stop_words = (
        "fecha de vencimiento",
        "fecha de compromiso",
        "descripcion",
        "descripción",
        "precio unit",
        "neto",
        "total",
        "observaciones",
    )

    def add_code(raw: str) -> None:
        code = raw.strip().upper()
        if not _is_product_code_token(code) or code in seen:
            return
        seen.add(code)
        codigos.append(code)

    for line in lines[start_idx:]:
        low = line.lower().strip().strip(":").strip()
        if any(low.startswith(w) for w in stop_words):
            break
        if low in ("cantidad", "fecha", "razón de referencia", "razon de referencia"):
            continue
        m = _CODE_LINE_RE.match(line)
        if m and _is_product_code_token(m.group(1)):
            add_code(m.group(1))
            continue
        if codigos:
            break

    if not codigos:
        return _extract_codigos_bloque_consecutivo(lines)
    return codigos


_QTY_ONLY_RE = re.compile(r"^(\d{1,2})$")
_SECTION_STOP_PREFIXES = (
    "precio unit",
    "son:",
    "timbre",
    "observaciones",
    "neto",
    "total",
    "documentos referenciados",
)


def _line_section_header(line: str) -> str | None:
    low = line.lower().strip().strip(":").strip()
    if _is_codigo_header(line):
        return "codigo"
    if low == "cantidad":
        return "cantidad"
    if low in ("descripcion", "descripción") or bool(re.match(r"^descr", low)):
        return "descripcion"
    if low == "valor":
        return "valor"
    return None


def _section_header_indices(lines: list[str]) -> dict[str, int]:
    indices: dict[str, int] = {}
    for idx, line in enumerate(lines):
        hdr = _line_section_header(line)
        if hdr and hdr not in indices:
            indices[hdr] = idx
    return indices


def _extract_codigos_seccion(lines: list[str], indices: dict[str, int]) -> list[str]:
    """Bloque Código: líneas solo-código hasta Descripción."""
    if "descripcion" not in indices:
        return _extract_codigos_producto(lines)

    start = indices.get("codigo", indices.get("cantidad"))
    if start is None:
        return _extract_codigos_bloque_consecutivo(lines)

    codigos: list[str] = []
    seen: set[str] = set()
    for line in lines[start + 1 : indices["descripcion"]]:
        if _line_section_header(line):
            continue
        m = _CODE_LINE_RE.match(line)
        if m and _is_product_code_token(m.group(1)):
            code = m.group(1).upper()
            if code not in seen:
                seen.add(code)
                codigos.append(code)
    return codigos if codigos else _extract_codigos_producto(lines)


def _extract_cantidades_seccion(lines: list[str], indices: dict[str, int]) -> list[int]:
    """Bloque Cantidad: números solos; si no hay, cantidad+descripción."""
    cantidades: list[int] = []
    if "cantidad" in indices:
        end = indices.get("descripcion", len(lines))
        if indices["cantidad"] < end:
            for line in lines[indices["cantidad"] + 1 : end]:
                if _line_section_header(line):
                    continue
                if _CODE_LINE_RE.match(line):
                    continue
                m = _QTY_ONLY_RE.match(line)
                if m:
                    qty = int(m.group(1))
                    if 1 <= qty <= 99:
                        cantidades.append(qty)
    if cantidades:
        return cantidades
    return _extract_cantidades_descripciones(lines)


def _extract_precios_valor_seccion(lines: list[str], indices: dict[str, int]) -> list[int]:
    """Bloque Valor: precios sueltos tras encabezado Valor."""
    if "valor" not in indices:
        return []

    precios: list[int] = []
    for line in lines[indices["valor"] + 1 :]:
        low = line.lower().strip().strip(":").strip()
        if any(low.startswith(p) for p in _SECTION_STOP_PREFIXES):
            break
        if re.fullmatch(r"(?:neto|total)\s*:?\s*", low, re.IGNORECASE):
            break
        m = _PRICE_LINE_RE.match(line)
        if not m:
            continue
        val = _parse_monto_chileno(m.group(1))
        if val is not None and val >= 50:
            precios.append(val)
    return precios


def _log_precios_tras_header(
    lines: list[str], header_label: str, start_idx: int | None
) -> list[tuple[str, int | None]]:
    """Debug: líneas tras Valor / Precio Unit y monto parseado (si aplica)."""
    parsed: list[tuple[str, int | None]] = []
    if start_idx is None:
        print(f"  [{header_label}] header no encontrado", flush=True)
        return parsed

    print(f"  [{header_label}] header en linea {start_idx + 1}: {lines[start_idx]!r}", flush=True)
    for offset, line in enumerate(lines[start_idx + 1 :], start=1):
        line_no = start_idx + 1 + offset
        low = line.lower().strip().strip(":").strip()
        if any(low.startswith(p) for p in _SECTION_STOP_PREFIXES):
            print(f"    L{line_no:03d}| STOP ({low}) -> {line!r}", flush=True)
            break
        if re.fullmatch(r"(?:neto|total)\s*:?\s*", low, re.IGNORECASE):
            print(f"    L{line_no:03d}| STOP (neto/total) -> {line!r}", flush=True)
            break
        m = _PRICE_LINE_RE.match(line)
        if m:
            val = _parse_monto_chileno(m.group(1))
            ok = val is not None and val >= 50
            parsed.append((line, val if ok else None))
            print(
                f"    L{line_no:03d}| PRECIO raw={line!r} parsed={val} usable={ok}",
                flush=True,
            )
        else:
            print(f"    L{line_no:03d}| skip -> {line!r}", flush=True)
    return parsed


def _find_precio_unit_index(lines: list[str]) -> int | None:
    for idx, line in enumerate(lines):
        low = line.lower().strip().strip(":").strip()
        if low.startswith("precio unit"):
            return idx
    return None


def _extract_productos_columnas(lines: list[str]) -> list[dict[str, Any]]:
    """PDF/imagen con columnas en bloques: Código, Cantidad, Descripción, Valor."""
    indices = _section_header_indices(lines)
    if "descripcion" not in indices and "codigo" not in indices:
        return []

    codigos = _extract_codigos_seccion(lines, indices)
    if not codigos:
        return []

    cantidades = _extract_cantidades_seccion(lines, indices)
    precios_raw = _extract_precios_valor_seccion(lines, indices)
    precio_unit_idx = _find_precio_unit_index(lines)

    print("=== _extract_productos_columnas DEBUG ===", flush=True)
    print(f"  indices seccion: {indices}", flush=True)
    print(f"  codigos ({len(codigos)}): {codigos}", flush=True)
    print(f"  cantidades ({len(cantidades)}): {cantidades}", flush=True)
    print("  --- precios tras header Valor ---", flush=True)
    _log_precios_tras_header(lines, "Valor", indices.get("valor"))
    print("  --- precios tras header Precio Unit ---", flush=True)
    _log_precios_tras_header(lines, "Precio Unit", precio_unit_idx)
    print(f"  precios_raw (bloque Valor): {precios_raw}", flush=True)
    if not precios_raw:
        precios_raw = _extract_precios_candidatos(lines, codigos)
        print(f"  precios_raw (fallback candidatos): {precios_raw}", flush=True)

    unit_prices = _select_unit_prices(precios_raw, cantidades)
    print(f"  unit_prices seleccionados: {unit_prices}", flush=True)
    print("=== FIN _extract_productos_columnas DEBUG ===", flush=True)

    n = min(len(codigos), len(cantidades), len(unit_prices))
    if n <= 0:
        return []

    return [
        {
            "codigo_proveedor": codigos[i],
            "cantidad": cantidades[i],
            "valor_neto": unit_prices[i],
        }
        for i in range(n)
    ]


def _extract_cantidades_descripciones(lines: list[str]) -> list[int]:
    """Líneas 'cantidad + descripción' (bloque tras 'Descripción' o en todo el texto)."""
    cantidades: list[int] = []
    after_desc_header = False

    for line in lines:
        low = line.lower().strip().strip(":").strip()
        if low in ("descripcion", "descripción") or re.match(r"^descr", low):
            after_desc_header = True
            continue
        if after_desc_header:
            if low.startswith("precio unit") or low.startswith("son:") or low.startswith("timbre"):
                break
            m = _QTY_DESC_RE.match(line)
            if m:
                qty = int(m.group(1))
                if 1 <= qty <= 99:
                    cantidades.append(qty)
                continue

    if not cantidades:
        for line in lines:
            m = _QTY_DESC_RE.match(line)
            if m:
                qty = int(m.group(1))
                if 1 <= qty <= 99:
                    cantidades.append(qty)
    return cantidades


def _find_descripcion_index(lines: list[str]) -> int | None:
    for idx, line in enumerate(lines):
        low = line.lower().strip().strip(":").strip()
        if low in ("descripcion", "descripción") or re.match(r"^descr", low):
            return idx
    return None


def _extract_precios_candidatos(lines: list[str], codigos: list[str]) -> list[int]:
    """Precios sueltos entre el bloque de códigos y la sección Descripción."""
    precios: list[int] = []
    desc_idx = _find_descripcion_index(lines)
    if desc_idx is None:
        desc_idx = len(lines)

    last_code_idx = -1
    for idx, line in enumerate(lines[:desc_idx]):
        m = _CODE_LINE_RE.match(line)
        if m and _is_product_code_token(m.group(1)):
            if m.group(1).upper() in {c.upper() for c in codigos}:
                last_code_idx = idx

    start = last_code_idx + 1 if last_code_idx >= 0 else 0
    for line in lines[start:desc_idx]:
        low = line.lower().strip().strip(":").strip()
        if not line or low in _PRODUCT_HEADER_WORDS:
            continue
        if _QTY_DESC_RE.match(line) or _CODE_LINE_RE.match(line):
            continue
        if re.search(r"[a-záéíóúñ]", line, re.IGNORECASE) and not _PRICE_LINE_RE.match(line):
            continue
        m = _PRICE_LINE_RE.match(line)
        if not m:
            continue
        val = _parse_monto_chileno(m.group(1))
        if val is not None and val >= 50:
            precios.append(val)
    return precios


def _select_unit_prices(precios: list[int], cantidades: list[int]) -> list[int]:
    """Elige precio unitario por ítem, en orden de aparición en el OCR."""
    if not precios or not cantidades:
        return []
    n = len(cantidades)
    if len(precios) == n:
        return precios[:n]

    price_set = set(precios)
    used_idx: set[int] = set()
    units: list[int] = []

    def is_line_total(val: int) -> bool:
        return any(val == pu * q for pu in precios for q in cantidades if q > 1)

    for qty in cantidades:
        chosen: int | None = None
        chosen_idx: int | None = None
        if qty > 1:
            for idx, p in enumerate(precios):
                if idx in used_idx:
                    continue
                if p * qty in price_set:
                    chosen = p
                    chosen_idx = idx
                    break
        if chosen is None:
            for idx, p in enumerate(precios):
                if idx in used_idx:
                    continue
                if is_line_total(p):
                    continue
                chosen = p
                chosen_idx = idx
                break
        if chosen is None:
            for idx, p in enumerate(precios):
                if idx not in used_idx:
                    chosen = p
                    chosen_idx = idx
                    break
        if chosen is None or chosen_idx is None:
            break
        used_idx.add(chosen_idx)
        units.append(chosen)
    return units


def _extract_productos_inline(texto: str) -> list[dict[str, Any]]:
    """Respaldo: línea continua código + cantidad + descripción + precios."""
    productos: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_line in texto.splitlines():
        line = raw_line.strip()
        m = _OLD_PRODUCT_LINE_RE.match(line)
        if not m:
            continue
        codigo = m.group(1).strip().upper()
        if codigo in seen:
            continue
        try:
            qty = int(m.group(2))
        except ValueError:
            continue
        if qty <= 0 or qty > 99999:
            continue
        valor_neto = _parse_monto_chileno(m.group(4))
        if valor_neto is None:
            line_total = _parse_monto_chileno(m.group(5))
            if line_total is not None and qty:
                valor_neto = int(round(line_total / qty))
        if valor_neto is None:
            continue
        seen.add(codigo)
        productos.append(
            {"codigo_proveedor": codigo, "cantidad": qty, "valor_neto": valor_neto}
        )

    if not productos:
        productos = _extract_productos_termico(texto, seen)

    return productos


def _extract_productos_termico(
    texto: str, seen: set[str] | None = None
) -> list[dict[str, Any]]:
    """Parser para boletas térmicas con el ítem repartido en líneas separadas.

    Tolera líneas de ruido OCR entre el código y la cantidad/precio (ej. teclas
    del laptop capturadas en la foto). Estructura esperada:
        CODIGO-MARCA DESCRIPCION
        CANTIDAD,00UN X
        PRECIO_UNIT
        0= TOTAL
    """
    if seen is None:
        seen = set()
    lines = [ln.strip() for ln in texto.splitlines() if ln.strip()]
    productos: list[dict[str, Any]] = []

    for i, line in enumerate(lines):
        mc = _THERMAL_CODE_RE.match(line)
        if not mc:
            continue
        codigo = mc.group(1).strip().upper()
        if codigo in seen:
            continue
        descripcion = mc.group(2).strip()

        # Busca la cantidad ("2,00UN X") más adelante, saltando ruido.
        qty = None
        qty_idx = None
        for j in range(i + 1, min(len(lines), i + 20)):
            mq = _THERMAL_QTY_RE.match(lines[j])
            if mq:
                try:
                    qty = int(mq.group(1))
                except ValueError:
                    qty = None
                qty_idx = j
                break
        if not qty or qty <= 0 or qty > 99999 or qty_idx is None:
            continue

        # Precio unitario: primer número en las líneas siguientes a la cantidad.
        precio = None
        for k in range(qty_idx + 1, min(len(lines), qty_idx + 8)):
            mp = _THERMAL_PRICE_RE.match(lines[k])
            if mp:
                val = _parse_monto_chileno(mp.group(1))
                if val is not None and val > 0:
                    precio = val
                    break
        if precio is None:
            continue

        seen.add(codigo)
        productos.append(
            {
                "codigo_proveedor": codigo,
                "descripcion": descripcion,
                "cantidad": qty,
                "valor_neto": precio,
            }
        )

    return productos


def _extract_productos(texto: str) -> list[dict[str, Any]]:
    """Extrae productos cuando Vision OCR separa columnas en bloques distintos."""
    texto_norm = _normalize_ocr_text(texto)
    lines = [ln.strip() for ln in texto_norm.splitlines() if ln.strip()]
    print("=== OCR NORMALIZADO (primeras 50 lineas) ===", flush=True)
    for i, ln in enumerate(lines[:50], start=1):
        print(f"{i:02d}|{ln}", flush=True)
    print("=== FIN OCR NORMALIZADO ===", flush=True)

    codigos = _extract_codigos_producto(lines)
    cantidades = _extract_cantidades_descripciones(lines)
    precios_raw = _extract_precios_candidatos(lines, codigos)
    unit_prices = _select_unit_prices(precios_raw, cantidades)

    n = min(len(codigos), len(cantidades), len(unit_prices))
    productos: list[dict[str, Any]] = []
    for i in range(n):
        productos.append(
            {
                "codigo_proveedor": codigos[i],
                "cantidad": cantidades[i],
                "valor_neto": unit_prices[i],
            }
        )

    if productos:
        return productos

    productos = _extract_productos_columnas(lines)
    if productos:
        return productos

    return _extract_productos_inline(texto)


def parsear_factura_chilena(texto: str) -> dict[str, Any]:
    """Extrae campos habituales de facturas chilenas desde texto OCR."""
    ocr_log = "=== TEXTO OCR COMPLETO ===\n%s\n=== FIN OCR ===" % (texto or "")
    try:
        from flask import has_app_context, current_app

        if has_app_context():
            current_app.logger.info(ocr_log)
        else:
            logger.info(ocr_log)
    except Exception:
        logger.info(ocr_log)
    print(ocr_log, flush=True)

    resultado: dict[str, Any] = {
        "rut_proveedor": None,
        "numero_documento": None,
        "fecha": None,
        "metodo_pago": None,
        "productos": [],
        "total_neto": None,
        "iva": None,
        "total": None,
    }
    if not (texto or "").strip():
        return resultado

    texto_parse = _normalize_ocr_text(texto)

    resultado["rut_proveedor"] = _extract_rut_emisor(texto_parse)
    resultado["numero_documento"] = _extract_numero_documento(texto_parse)
    resultado["fecha"] = _extract_fecha_emision(texto_parse)

    texto_lower = texto_parse.lower()
    if re.search(r"\bcr[eé]dito\b", texto_lower):
        resultado["metodo_pago"] = "credito"
    elif re.search(r"\btransferencia\b", texto_lower):
        resultado["metodo_pago"] = "transferencia"
    elif re.search(r"\bcheque\b", texto_lower):
        resultado["metodo_pago"] = "cheque"
    elif re.search(r"\bcontado\b|\befectivo\b", texto_lower):
        resultado["metodo_pago"] = "contado"

    neto, iva, total = _extract_montos(texto_parse)
    resultado["total_neto"] = neto
    resultado["iva"] = iva
    resultado["total"] = total
    resultado["productos"] = _extract_productos(texto_parse)
    logger.info("Productos extraídos: %s", resultado["productos"])

    return resultado


def _convert_pdf_first_page_to_png(pdf_bytes: bytes) -> bytes:
    """Convierte la primera página del PDF a PNG (PyMuPDF; pdf2image como respaldo)."""
    last_error: Exception | None = None

    try:
        import fitz  # PyMuPDF

        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            if doc.page_count < 1:
                raise ValueError("El PDF no tiene páginas")
            page = doc.load_page(0)
            pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False)
            return pix.tobytes("png")
    except ImportError as exc:
        last_error = exc
    except Exception as exc:
        last_error = exc

    try:
        from io import BytesIO

        from pdf2image import convert_from_bytes

        pages = convert_from_bytes(pdf_bytes, first_page=1, last_page=1, fmt="png")
        if not pages:
            raise ValueError("No se pudo leer la primera página del PDF")
        buf = BytesIO()
        pages[0].save(buf, format="PNG")
        return buf.getvalue()
    except ImportError as exc:
        if last_error is not None:
            raise ValueError(
                "Para analizar PDF instale pymupdf (pip install pymupdf) "
                "o pdf2image con Poppler."
            ) from exc
        raise ValueError("Instale pymupdf para analizar PDF (pip install pymupdf).") from exc
    except Exception as exc:
        last_error = exc

    msg = "No se pudo convertir el PDF a imagen"
    if last_error:
        msg = f"{msg}: {last_error}"
    raise ValueError(msg)


def _vision_ocr_text(image_bytes: bytes, cred_path: Path) -> str:
    credentials = service_account.Credentials.from_service_account_file(
        str(cred_path),
        scopes=VISION_SCOPES,
    )
    client = vision.ImageAnnotatorClient(credentials=credentials)
    image = vision.Image(content=image_bytes)

    try:
        response = client.text_detection(image=image)
    except Exception as exc:
        raise ValueError("No se pudo contactar a Google Cloud Vision") from exc

    if response.error.message:
        raise ValueError(response.error.message)

    if response.full_text_annotation and response.full_text_annotation.text:
        return response.full_text_annotation.text
    if response.text_annotations:
        return response.text_annotations[0].description or ""
    return ""


def analizar_factura(image_base64: str, media_type: str) -> dict[str, Any]:
    """OCR con Google Cloud Vision y parseo de factura chilena."""
    cred_path = _credentials_path()
    if not cred_path.is_file():
        raise ValueError(
            f"No se encontró el archivo de credenciales: {cred_path}. "
            "Configura GOOGLE_VISION_CREDENTIALS en .env"
        )

    b64 = (image_base64 or "").strip()
    if "," in b64 and b64.lower().startswith("data:"):
        b64 = b64.split(",", 1)[1]
    if not b64:
        raise ValueError("Archivo vacío")

    mt = (media_type or "image/jpeg").strip().lower()
    if mt == "image/jpg":
        mt = "image/jpeg"
    allowed = {"image/jpeg", "image/png", "image/webp", "application/pdf"}
    if mt not in allowed:
        raise ValueError("Formato no soportado. Use JPG, PNG, WEBP o PDF.")

    try:
        file_bytes = base64.b64decode(b64, validate=True)
    except Exception as exc:
        raise ValueError("Base64 inválido") from exc

    preview_base64: str | None = None
    if mt == "application/pdf":
        if len(file_bytes) > MAX_PDF_BYTES:
            raise ValueError("El PDF es demasiado grande (máx. 12 MB)")
        image_bytes = _convert_pdf_first_page_to_png(file_bytes)
        preview_base64 = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
    else:
        image_bytes = file_bytes
        if len(image_bytes) > MAX_IMAGE_BYTES:
            raise ValueError("El archivo es demasiado grande (máx. 12 MB)")

    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ValueError("La imagen generada desde el PDF es demasiado grande (máx. 12 MB)")

    texto = _vision_ocr_text(image_bytes, cred_path).strip()
    print(texto, flush=True)
    if mt == "application/pdf":
        print("=== OCR PDF ===", texto[:3000], flush=True)
    if not texto:
        raise ValueError("No se detectó texto en el documento. Pruebe con mejor calidad.")

    resultado = parsear_factura_chilena(texto)
    resultado["ocr_texto_crudo"] = texto
    if preview_base64:
        resultado["preview_base64"] = preview_base64
    return resultado


def analyze_chilean_invoice(base64_data: str, media_type: str) -> dict[str, Any]:
    """Alias usado por rutas existentes."""
    return analizar_factura(base64_data, media_type)
