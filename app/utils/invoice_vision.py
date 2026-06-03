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

    _meses_es = (
        "enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|"
        "octubre|noviembre|diciembre"
    )
    for m in re.finditer(
        rf"(\d{{1,2}})\s+de\s+({_meses_es})\s+del?\s+(\d{{4}})",
        texto,
        re.IGNORECASE,
    ):
        meses_map = {
            "enero": 1,
            "febrero": 2,
            "marzo": 3,
            "abril": 4,
            "mayo": 5,
            "junio": 6,
            "julio": 7,
            "agosto": 8,
            "septiembre": 9,
            "setiembre": 9,
            "octubre": 10,
            "noviembre": 11,
            "diciembre": 12,
        }
        mo = meses_map.get(m.group(2).lower())
        if mo:
            add(int(m.group(1)), mo, int(m.group(3)), 135)

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
        r"FACTURA\s+ELECTR[OÓ]NICA[\s\S]{0,50}?N[°º]?\s*0*(\d{3,6})\b",
        r"(?:^|\s)N[°º]?(\d{3,6})\b",
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
# Facturas sin código (Xinwang, RC SAP): "- DESC CANT PU PT" o bloque multilínea.
_SIN_CODIGO_INLINE_DASH_RE = re.compile(
    r"^-\s+(.+?)\s+(\d{1,5})\s+([\d.,]+)\s+([\d.,]+)\s*$",
    re.IGNORECASE,
)
_SIN_CODIGO_INLINE_DESC_RE = re.compile(
    r"^(.+?)\s+(\d{1,5})\s+(\d{1,5})\s+([\d.,]+)\s*$",
    re.IGNORECASE,
)
_EMPTY_CODIGO_LINE_RE = re.compile(r"^[\s\-—|\.]+$", re.IGNORECASE)

# Boleta térmica (ej: Fitalia): el ítem viene en líneas separadas, con posible
# ruido OCR entre el header y la fila real:
#   CODIGO-MARCA DESCRIPCION
#   CANTIDAD,00UN X
#   PRECIO_UNIT
#   0= TOTAL
_THERMAL_CODE_DESC_RE = re.compile(
    r"^\s*([A-Z0-9]{3,15}-[A-Z0-9]{2,12})\s+(.+)$",
    re.IGNORECASE,
)
_THERMAL_CODE_ONLY_RE = re.compile(
    r"^\s*([A-Z0-9]{3,15}-[A-Z0-9]{2,12})\s*$",
    re.IGNORECASE,
)
_THERMAL_QTY_LINE_RE = re.compile(
    r"^\s*(\d+),\d+\s*UN(?:\s*(?:X|x)\s*([\d.,]+))?",
    re.IGNORECASE,
)
_THERMAL_PRICE_RE = re.compile(r"^[\$]?\s*([\d.,]+)\s*$")
_THERMAL_LINE_TOTAL_RE = re.compile(r"^\s*0\s*=\s*([\d.,]+)\s*$", re.IGNORECASE)
_THERMAL_CODE_TOKEN_RE = re.compile(
    r"\b([A-Z0-9]{3,15}-[A-Z0-9]{2,12})\b",
    re.IGNORECASE,
)
# OCR térmico: "M604 15-BOSCH" → unir en "M60415-BOSCH"
_THERMAL_OCR_SPLIT_CODE_RE = re.compile(
    r"\b([A-Z0-9]+)\s+(\d+-[A-Z]{2,10})\b",
    re.IGNORECASE,
)


def _fix_thermal_ocr_split_codes(texto: str) -> str:
    """Elimina espacios OCR dentro del código proveedor (M604 15-BOSCH → M60415-BOSCH)."""
    if not (texto or "").strip():
        return texto
    return _THERMAL_OCR_SPLIT_CODE_RE.sub(r"\1\2", texto)


def _is_plausible_supplier_code(code: str) -> bool:
    """Código proveedor real (ej. M60415-BOSCH), no fragmentos de descripción (XUV-GENIO)."""
    c = (code or "").strip().upper()
    if "-" not in c:
        return False
    head, _, tail = c.partition("-")
    if not head or not tail:
        return False
    # Fitalia y similares: prefijo con al menos un dígito (M60415-BOSCH, 40043-GSP).
    return any(ch.isdigit() for ch in head)


def _collect_plausible_thermal_codes(text: str) -> list[str]:
    """Tokens CODIGO-MARCA válidos; ignora líneas que empiezan con '-' (-XUV-GENIO)."""
    found: list[str] = []

    def add(code: str) -> None:
        c = (code or "").strip().upper()
        if _is_plausible_supplier_code(c) and c not in found:
            found.append(c)

    for m in _THERMAL_CODE_TOKEN_RE.finditer(text):
        if m.start() > 0 and text[m.start() - 1] == "-":
            continue
        add(m.group(1))

    # OCR a veces separa: "M60415 BOSCH" en lugar de "M60415-BOSCH"
    for m in re.finditer(r"\b([A-Z]\d{4,6})\s+([A-Z]{2,12})\b", text, re.IGNORECASE):
        add(f"{m.group(1)}-{m.group(2)}")

    # Sin espacio: "M60415BOSCH" -> M60415-BOSCH (solo si el prefijo tiene dígitos)
    for m in re.finditer(r"\b([A-Z]\d{5,6})([A-Z]{4,12})\b", text, re.IGNORECASE):
        add(f"{m.group(1)}-{m.group(2)}")

    return found


def _thermal_qty_from_text(texto: str) -> int | None:
    """Cantidad desde TOT.UNIDADES o línea '2,00UN'."""
    m_tot = re.search(r"TOT\.?\s*UNIDADES\s*:?\s*(\d+)(?:,\d+)?", texto, re.IGNORECASE)
    if m_tot:
        return int(m_tot.group(1))
    m_un = re.search(r"(\d+),\d+\s*UN", texto, re.IGNORECASE)
    if m_un:
        return int(m_un.group(1))
    return None


def _thermal_unit_price_from_text(texto: str, codigo: str, qty: int) -> int | None:
    """Precio unitario: 'UN X 8.500' cerca del código o neto/cantidad."""
    head = codigo.split("-", 1)[0]
    pos = texto.upper().find(head.upper())
    chunk = texto[pos : pos + 500] if pos >= 0 else texto

    m_px = re.search(r"(\d+),\d+\s*UN\s*(?:X|x)\s*([\d.,]+)", chunk, re.IGNORECASE)
    if m_px:
        val = _parse_monto_chileno(m_px.group(2))
        if val is not None and val > 0:
            return val

    for m in _THERMAL_PRICE_RE.finditer(chunk):
        val = _parse_monto_chileno(m.group(1))
        if val is not None and 100 <= val <= 50_000_000:
            return val

    neto, _, _ = _extract_montos(texto)
    if neto is not None and qty > 0:
        return int(round(neto / qty))
    return None


def _build_producto_termico(codigo: str, texto: str, qty: int | None = None) -> dict[str, Any] | None:
    if qty is None:
        qty = _thermal_qty_from_text(texto)
    if not qty or qty <= 0 or qty > 99999:
        return None
    precio = _thermal_unit_price_from_text(texto, codigo, qty)
    if precio is None:
        return None
    return {
        "codigo_proveedor": codigo.upper(),
        "cantidad": qty,
        "valor_neto": precio,
    }


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
        for ch in ("\u2013", "\u2014", "\u2212"):
            line = line.replace(ch, "-")
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
    if not precios_raw:
        precios_raw = _extract_precios_candidatos(lines, codigos)

    unit_prices = _select_unit_prices(precios_raw, cantidades)

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

    # PDF columnas (Mundo Repuestos, etc.): N ítems → 2N montos (unitario, total línea).
    if len(precios) >= 2 * n:
        units: list[int] = []
        for i in range(n):
            unit = precios[i * 2]
            qty = max(1, int(cantidades[i] or 1))
            total_idx = i * 2 + 1
            if total_idx < len(precios):
                line_total = precios[total_idx]
                if unit * qty == line_total:
                    units.append(unit)
                    continue
                if line_total % qty == 0:
                    implied = line_total // qty
                    if abs(implied - unit) <= max(5, int(round(unit * 0.01))):
                        units.append(implied)
                        continue
            units.append(unit)
        return units

    price_set = set(precios)
    used_idx: set[int] = set()
    units: list[int] = []

    def is_line_total(val: int) -> bool:
        for pu in precios:
            for q in cantidades:
                if q <= 1:
                    continue
                expected = pu * q
                if val == expected or abs(val - expected) <= max(5, int(round(pu * 0.01))):
                    return True
        return False

    for qty in cantidades:
        chosen: int | None = None
        chosen_idx: int | None = None
        if qty > 1:
            for idx, p in enumerate(precios):
                if idx in used_idx:
                    continue
                product = p * qty
                if any(
                    product == other or abs(product - other) <= max(5, int(round(p * 0.01)))
                    for other in price_set
                ):
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


def _is_price_only_line(line: str) -> bool:
    """True si la línea es un monto (no cantidad 1-99 ni fragmento numérico de descripción)."""
    s = (line or "").strip()
    if re.fullmatch(r"\d{1,2}", s):
        return False
    m = _PRICE_LINE_RE.match(s)
    if not m:
        return False
    token = m.group(1)
    if "," in token or "." in token:
        val = _parse_monto_chileno(token)
        return val is not None and val >= 50
    if re.fullmatch(r"\d{3,8}", token):
        return False
    val = _parse_monto_chileno(token)
    return val is not None and val >= 500


def _is_sin_codigo_marker_line(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return True
    if _EMPTY_CODIGO_LINE_RE.match(s):
        return True
    return s.upper() in ("-", "—", "|", ".", "N/A", "NA", "S/C", "SIN CODIGO", "SIN CÓDIGO")


def _is_codigo_column_header(line: str) -> bool:
    low = (line or "").lower().strip().strip(":").strip()
    if low in ("codigo", "código"):
        return True
    return bool(re.match(r"^c[oó]digo\s*$", low))


def _find_xinwang_codigo_index(lines: list[str]) -> int | None:
    for i, line in enumerate(lines):
        if _is_codigo_column_header(line):
            return i
    return None


def _xinwang_zone_stop_line(line: str) -> bool:
    low = (line or "").lower().strip()
    if low.startswith("forma de pago"):
        return True
    if low.startswith("timbre"):
        return True
    if low.startswith("referencias"):
        return True
    return False


def _xinwang_product_zone_slice(lines: list[str]) -> tuple[int, int] | None:
    """Índices [start, end) del bloque de ítems entre Codigo y Forma de Pago."""
    codigo_idx = _find_xinwang_codigo_index(lines)
    if codigo_idx is None:
        return None
    start = codigo_idx + 1
    end = len(lines)
    for i in range(start, len(lines)):
        if _xinwang_zone_stop_line(lines[i]):
            end = i
            break
    return start, end


def _xinwang_uses_stacked_descriptions(lines: list[str]) -> bool:
    """True si las descripciones van apiladas bajo Codigo (layout ANDESS86900)."""
    zone = _xinwang_product_zone_slice(lines)
    if zone is None:
        return False
    start, end = zone
    for line in lines[start:end]:
        s = (line or "").strip()
        if not s:
            continue
        low = s.lower().strip().strip(":").strip()
        if _looks_like_xinwang_descripcion(s) and not re.match(r"^\d", s):
            return True
        if low in (
            "descripcion",
            "descripción",
            "cantidad",
            "precio",
            "valor",
        ):
            return False
    return False


def _xinwang_interleaved_data_start(lines: list[str], zone_start: int, zone_end: int) -> int:
    """En layout intercalado, los ítems empiezan tras la fila de encabezado Valor."""
    for i in range(zone_start, zone_end):
        low = (lines[i] or "").lower().strip().strip(":").strip()
        if low == "valor":
            return i + 1
    return zone_start


def _is_xinwang_zone_skip_line(line: str) -> bool:
    """Encabezados de columna, cantidad y montos (no descripción de producto)."""
    s = (line or "").strip()
    if not s:
        return True
    low = s.lower().strip().strip(":").strip()
    if low in (
        "descripcion",
        "descripción",
        "cantidad",
        "precio",
        "valor",
        "codigo",
        "código",
    ):
        return True
    if low.startswith("%impto") or low.startswith("%desc"):
        return True
    if low.startswith("adic") or re.match(r"^adic\.?\*?$", low):
        return True
    if _is_sin_codigo_marker_line(s):
        return True
    if _is_xinwang_qty_line(s):
        return True
    if _is_price_only_line(s):
        return True
    return False


def _is_xinwang_interleaved_desc_line(line: str) -> bool:
    s = (line or "").strip()
    if not s or len(s) < 4 or re.match(r"^\d", s):
        return False
    low = s.lower().strip()
    if low.startswith("adic") or "*" in s and len(s) < 12:
        return False
    return _looks_like_xinwang_descripcion(s)


def _collect_xinwang_descripciones_from_zone(zone_lines: list[str]) -> list[str]:
    descs: list[str] = []
    for line in zone_lines:
        s = line.strip()
        if _is_xinwang_zone_skip_line(s):
            continue
        if _is_xinwang_interleaved_desc_line(s):
            descs.append(s)
    return descs


def _collect_xinwang_prices_from_zone(zone_lines: list[str]) -> list[int]:
    prices: list[int] = []
    for line in zone_lines:
        s = line.strip()
        if not s:
            continue
        if _is_price_only_line(s):
            val = _parse_monto_chileno(s)
            if val is not None:
                prices.append(val)
    return prices


def _pair_xinwang_unit_prices(prices: list[int], n: int) -> list[int]:
    """Empareja N descripciones con M montos (M=2N → unitario; M=N → directo)."""
    if n <= 0 or not prices:
        return []
    m = len(prices)
    if m == 2 * n:
        return [prices[i * 2] for i in range(n)]
    if m == n:
        return prices[:n]
    if m > n:
        step = max(1, m // n)
        return [prices[i * step] for i in range(n)]
    return prices[:n]


def _xinwang_interleaved_data_lines(lines: list[str]) -> list[str]:
    zone = _xinwang_product_zone_slice(lines)
    if zone is None:
        return []
    start, end = zone
    data_start = _xinwang_interleaved_data_start(lines, start, end)
    return lines[data_start:end]


def _extract_xinwang_descripciones_interleaved(lines: list[str]) -> list[str]:
    return _collect_xinwang_descripciones_from_zone(_xinwang_interleaved_data_lines(lines))


def _extract_xinwang_all_prices_interleaved(lines: list[str]) -> list[int]:
    return _collect_xinwang_prices_from_zone(_xinwang_interleaved_data_lines(lines))


def _extract_xinwang_units_qty_aligned(
    data_lines: list[str], n_descs: int
) -> list[int]:
    """Asigna el 1.er monto tras cada cantidad; si hay desc. sin precio antes del bloque, toma N montos."""
    units: list[int] = []
    desc_seen = 0
    i = 0
    while i < len(data_lines) and len(units) < n_descs:
        line = data_lines[i].strip()
        if _is_xinwang_interleaved_desc_line(line):
            desc_seen += 1
            i += 1
            continue
        if not _is_xinwang_qty_line(line):
            i += 1
            continue
        unpriced = desc_seen - len(units)
        if unpriced <= 0:
            i += 1
            continue
        j = i + 1
        prices: list[int] = []
        while j < len(data_lines):
            s = data_lines[j].strip()
            if not s:
                j += 1
                continue
            if _is_xinwang_qty_line(s):
                break
            if _is_xinwang_interleaved_desc_line(s):
                j += 1
                continue
            if _is_price_only_line(s):
                val = _parse_monto_chileno(s)
                if val is not None:
                    prices.append(val)
                j += 1
                continue
            break
        if prices:
            take = min(unpriced, len(prices), n_descs - len(units))
            units.extend(prices[:take])
        i += 1
    return units


def _extract_xinwang_unit_prices_sequential(
    lines: list[str], descs: list[str]
) -> list[int]:
    """Precio unitario = 1.er monto tras cada línea de cantidad (1 1 / 11)."""
    zone = _xinwang_product_zone_slice(lines)
    if zone is None:
        return []
    start, end = zone
    start = _xinwang_interleaved_data_start(lines, start, end)
    units: list[int] = []
    i = start
    while i < end and len(units) < len(descs):
        line = lines[i].strip()
        if _is_xinwang_zone_skip_line(line) and not _is_xinwang_qty_line(line):
            i += 1
            continue
        if not _is_xinwang_qty_line(line):
            i += 1
            continue
        j = i + 1
        while j < end:
            s = lines[j].strip()
            if not s:
                j += 1
                continue
            if _is_xinwang_qty_line(s):
                break
            if _is_xinwang_interleaved_desc_line(s):
                break
            if _is_price_only_line(s):
                val = _parse_monto_chileno(s)
                if val is not None:
                    units.append(val)
                i = j + 1
                break
            j += 1
        else:
            i += 1
    return units


def _has_xinwang_column_layout(lines: list[str]) -> bool:
    """Xinwang: columna Codigo; ítems apilados o filas intercaladas."""
    has_codigo = any(_is_codigo_column_header(ln) for ln in lines)
    has_cols = any(ln.lower().strip() == "cantidad" for ln in lines) and any(
        ln.lower().strip() == "valor" for ln in lines
    )
    if not (has_codigo and has_cols):
        return False
    if len(_extract_xinwang_descripciones(lines)) >= 1:
        return True
    data_lines = _xinwang_interleaved_data_lines(lines)
    if not data_lines:
        return False
    descs = _collect_xinwang_descripciones_from_zone(data_lines)
    prices = _collect_xinwang_prices_from_zone(data_lines)
    return len(descs) >= 1 and len(prices) >= len(descs)


def _has_sin_codigo_layout(lines: list[str]) -> bool:
    """Layout tipo Xinwang / SAP: CODIGO/CANTIDAD + ITEM/PRECIO + TOTAL."""
    if _has_xinwang_column_layout(lines):
        return True
    joined = "\n".join(lines).lower().replace(" ", "")
    if "codigo/cantidad" in joined or "código/cantidad" in joined:
        return True
    if "item/precio" in joined:
        for i, line in enumerate(lines):
            if line.strip().lower() == "total" and i > 0:
                prev = lines[i - 1].lower()
                if "precio" in prev or "item" in prev:
                    return True
    return False


def _xinwang_desc_stop_line(line: str) -> bool:
    low = (line or "").lower().strip()
    if low.startswith("forma de pago"):
        return True
    if low in ("descripcion", "descripción", "cantidad", "precio", "valor"):
        return True
    if low.startswith("ciudad:"):
        return True
    if low.startswith("%impto") or low.startswith("%desc"):
        return True
    return False


def _looks_like_xinwang_descripcion(line: str) -> bool:
    s = (line or "").strip()
    if not s or len(s) < 4:
        return False
    if _is_sin_codigo_marker_line(s):
        return False
    if not re.search(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]{3,}", s):
        return False
    low = s.lower().strip().strip(":").strip()
    if low in _PRODUCT_HEADER_WORDS or low in ("codigo", "código"):
        return False
    if low.startswith(("forma de pago", "fecha ", "r.u.t", "giro:", "direccion", "dirección")):
        return False
    return True


def _extract_xinwang_descripciones(lines: list[str]) -> list[str]:
    """Descripciones verticales bajo encabezado Codigo (hasta Forma de Pago / bloque numérico)."""
    start: int | None = None
    for i, line in enumerate(lines):
        if _is_codigo_column_header(line):
            start = i + 1
            break
    if start is None:
        return []

    descs: list[str] = []
    for line in lines[start:]:
        if _xinwang_desc_stop_line(line):
            break
        s = line.strip()
        if _looks_like_xinwang_descripcion(s):
            descs.append(s)
    return descs


def _find_xinwang_numeric_start(lines: list[str]) -> int | None:
    """Primera línea de datos tras encabezados Cantidad/Precio/Valor."""
    valor_idx: int | None = None
    for i, line in enumerate(lines):
        low = line.lower().strip().strip(":").strip()
        if low == "valor":
            valor_idx = i
    if valor_idx is not None:
        return valor_idx + 1
    for i, line in enumerate(lines):
        low = line.lower()
        if "desc" in low and "%" in line:
            return i + 1
    for i, line in enumerate(lines):
        if line.lower().strip() == "cantidad" and i + 1 < len(lines):
            nxt = lines[i + 1].lower().strip()
            if nxt == "precio":
                j = i + 2
                while j < len(lines) and lines[j].lower().strip() not in ("valor",):
                    j += 1
                if j < len(lines):
                    return j + 1
    return None


def _is_xinwang_qty_line(line: str) -> bool:
    """Cantidad Xinwang: '1 1' (2.º dígito = % imp. adic.) o '11' (mismo código)."""
    s = (line or "").strip()
    if re.fullmatch(r"1\s+1", s):
        return True
    if re.fullmatch(r"11", s):
        return True
    return False


def _extract_xinwang_unit_prices(lines: list[str]) -> list[int]:
    """Precio unitario por ítem: 1.ª línea de monto tras cada línea de cantidad."""
    start = _find_xinwang_numeric_start(lines)
    if start is None:
        return []

    units: list[int] = []
    i = start
    while i < len(lines):
        line = lines[i].strip()
        if _is_detalle_producto_stop_line(line) or line.lower().startswith("timbre"):
            break
        if not _is_xinwang_qty_line(line):
            i += 1
            continue
        j = i + 1
        while j < len(lines):
            s = lines[j].strip()
            if not s:
                j += 1
                continue
            if _is_detalle_producto_stop_line(s) or s.lower().startswith("timbre"):
                break
            if _is_xinwang_qty_line(s):
                break
            if _is_price_only_line(s):
                val = _parse_monto_chileno(s)
                if val is not None:
                    units.append(val)
                if j + 1 < len(lines) and _is_price_only_line(lines[j + 1].strip()):
                    i = j + 2
                else:
                    i = j + 1
                break
            j += 1
        else:
            i += 1
    return units


def _extract_productos_sin_codigo_xinwang(lines: list[str]) -> list[dict[str, Any]]:
    if _xinwang_uses_stacked_descriptions(lines):
        descs = _extract_xinwang_descripciones(lines)
        units = _extract_xinwang_unit_prices(lines)
    else:
        descs = _extract_xinwang_descripciones_interleaved(lines)
        data_lines = _xinwang_interleaved_data_lines(lines)
        units = _extract_xinwang_units_qty_aligned(data_lines, len(descs))
        if len(units) != len(descs):
            prices = _extract_xinwang_all_prices_interleaved(lines)
            units = _pair_xinwang_unit_prices(prices, len(descs))
        if len(units) != len(descs):
            seq = _extract_xinwang_unit_prices_sequential(lines, descs)
            if len(seq) == len(descs):
                units = seq
    if not descs or not units:
        return []
    n = min(len(descs), len(units))
    return [_producto_sin_codigo(descs[i], 1, units[i]) for i in range(n)]


def _is_detalle_producto_stop_line(line: str) -> bool:
    low = (line or "").lower().strip()
    if low.startswith(("referencias", "son ", "timbre", "tipo doc", "monto neto", "monto iva")):
        return True
    if re.match(r"^monto\s", low):
        return True
    if low in ("pesos", "monto exento", "monto total"):
        return True
    return False


def _looks_like_product_description(line: str) -> bool:
    s = (line or "").strip()
    if not s or len(s) < 3:
        return False
    if _is_sin_codigo_marker_line(s):
        return False
    if _PRICE_LINE_RE.match(s):
        return False
    if _CODE_LINE_RE.match(s) and _is_product_code_token(s):
        return False
    if re.fullmatch(r"\d{1,5}", s):
        return False
    if not re.search(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]", s):
        return False
    low = s.lower().strip().strip(":").strip()
    if low in _PRODUCT_HEADER_WORDS:
        return False
    if low in ("receptor", "giro", "direccion", "dirección", "comuna", "ciudad", "forma de pago"):
        return False
    if low.startswith("fecha de"):
        return False
    return True


def _producto_sin_codigo(desc: str, qty: int, valor_neto: int) -> dict[str, Any]:
    desc_norm = re.sub(r"\s+", " ", (desc or "").strip())[:255]
    return {
        "codigo_proveedor": "",
        "descripcion": desc_norm,
        "cantidad": qty,
        "valor_neto": valor_neto,
    }


def _find_sin_codigo_detalle_start(lines: list[str]) -> int | None:
    for i, line in enumerate(lines):
        if line.strip().lower() == "total" and i > 0:
            prev = lines[i - 1].lower()
            if "precio" in prev or "item" in prev:
                return i + 1
    for i, line in enumerate(lines):
        low = line.lower()
        if "codigo" in low and "cantidad" in low:
            j = i + 1
            while j < len(lines) and j < i + 8:
                if lines[j].strip().lower() == "total":
                    return j + 1
                if _looks_like_product_description(lines[j]):
                    return j
                j += 1
    return None


def _extract_productos_sin_codigo_inline(lines: list[str]) -> list[dict[str, Any]]:
    productos: list[dict[str, Any]] = []
    for line in lines:
        s = line.strip()
        if not s:
            continue
        m_dash = _SIN_CODIGO_INLINE_DASH_RE.match(s)
        if m_dash:
            qty = int(m_dash.group(2))
            unit = _parse_monto_chileno(m_dash.group(3))
            if unit and 0 < qty <= 99999:
                productos.append(_producto_sin_codigo(m_dash.group(1), qty, unit))
            continue
        m_desc = _SIN_CODIGO_INLINE_DESC_RE.match(s)
        if m_desc and re.search(r"[A-Za-zÁÉÍÓÚÑ]", m_desc.group(1)):
            qty = int(m_desc.group(2))
            unit = _parse_monto_chileno(m_desc.group(4))
            if unit and 0 < qty <= 99999:
                productos.append(_producto_sin_codigo(m_desc.group(1), qty, unit))
    return productos


def _extract_productos_sin_codigo_bloque(lines: list[str]) -> list[dict[str, Any]]:
    """Ítems en bloques verticales (descripción, cantidad, precios) sin columna código."""
    start = _find_sin_codigo_detalle_start(lines)
    if start is None:
        return []

    productos: list[dict[str, Any]] = []
    desc_parts: list[str] = []
    qty: int | None = None
    prices: list[int] = []

    def flush() -> None:
        nonlocal desc_parts, qty, prices
        if not desc_parts or qty is None or not prices:
            desc_parts = []
            qty = None
            prices = []
            return
        unit = prices[0]
        if len(prices) >= 2 and qty > 1 and prices[0] * qty == prices[-1]:
            unit = prices[0]
        elif len(prices) >= 2 and prices[0] == prices[-1]:
            unit = prices[0]
        productos.append(_producto_sin_codigo(" ".join(desc_parts), qty, unit))
        desc_parts = []
        qty = None
        prices = []

    for line in lines[start:]:
        if _is_detalle_producto_stop_line(line):
            break
        s = line.strip()
        if not s or _is_sin_codigo_marker_line(s):
            continue

        m_dash = _SIN_CODIGO_INLINE_DASH_RE.match(s)
        if m_dash:
            flush()
            q = int(m_dash.group(2))
            pu = _parse_monto_chileno(m_dash.group(3))
            if pu and 0 < q <= 99999:
                productos.append(_producto_sin_codigo(m_dash.group(1), q, pu))
            continue

        m_desc = _SIN_CODIGO_INLINE_DESC_RE.match(s)
        if m_desc and re.search(r"[A-Za-zÁÉÍÓÚÑ]", m_desc.group(1)):
            flush()
            q = int(m_desc.group(2))
            pu = _parse_monto_chileno(m_desc.group(4))
            if pu and 0 < q <= 99999:
                productos.append(_producto_sin_codigo(m_desc.group(1), q, pu))
            continue

        m_qty = re.fullmatch(r"(\d{1,2})", s)
        if m_qty and desc_parts and qty is None:
            qty = int(m_qty.group(1))
            continue

        if _is_price_only_line(s):
            val = _parse_monto_chileno(s)
            if val is not None:
                prices.append(val)
                if desc_parts and qty is not None and len(prices) >= 2:
                    flush()
            continue

        if _looks_like_product_description(s):
            if prices and desc_parts and qty is not None:
                flush()
            desc_parts.append(s)
            continue

        if desc_parts and qty is None and re.fullmatch(r"[A-Z0-9]{3,12}", s, re.IGNORECASE):
            desc_parts.append(s)
            continue

    flush()
    return productos


def _extract_productos_sin_codigo(texto: str, lines: list[str] | None = None) -> list[dict[str, Any]]:
    if lines is None:
        lines = [ln.strip() for ln in _normalize_ocr_text(texto).splitlines() if ln.strip()]

    if _has_xinwang_column_layout(lines):
        xinwang = _extract_productos_sin_codigo_xinwang(lines)
        if xinwang:
            return xinwang

    inline = _extract_productos_sin_codigo_inline(lines)
    if inline and not _has_sin_codigo_layout(lines):
        return inline

    if not _has_sin_codigo_layout(lines):
        return []

    bloque = _extract_productos_sin_codigo_bloque(lines)
    productos = inline if len(inline) >= len(bloque) else bloque
    if not productos:
        productos = inline or bloque

    # No usar si hay códigos reales en bloque Código (evitar pisar Mundo Repuestos, etc.)
    indices = _section_header_indices(lines)
    codigos = _extract_codigos_seccion(lines, indices) if indices else []
    if not codigos:
        codigos = _extract_codigos_producto(lines)
    real_codes = [c for c in codigos if not _is_sin_codigo_marker_line(c)]
    if real_codes and productos:
        return []

    return productos


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


def _is_thermal_desc_continuation(line: str) -> bool:
    """Línea de descripción multilínea (ej. '-XUV-GENIO') antes de cantidad/precio."""
    s = (line or "").strip()
    if not s or len(s) < 2:
        return False
    low = s.lower()
    if low.startswith("total") or low.startswith("monto total"):
        return False
    if _THERMAL_CODE_DESC_RE.match(s) or _THERMAL_CODE_ONLY_RE.match(s):
        return False
    if _THERMAL_QTY_LINE_RE.match(s) or _THERMAL_LINE_TOTAL_RE.match(s):
        return False
    if _THERMAL_PRICE_RE.match(s):
        return False
    return bool(re.search(r"[A-Za-zÁÉÍÓÚáéíóúÑñ]", s))


def _parse_thermal_qty_line(line: str) -> tuple[int | None, int | None]:
    """Cantidad y precio unitario opcional en '2,00UN X 8.500'."""
    mq = _THERMAL_QTY_LINE_RE.match(line)
    if not mq:
        return None, None
    try:
        qty = int(mq.group(1))
    except ValueError:
        return None, None
    precio = None
    if mq.group(2):
        precio = _parse_monto_chileno(mq.group(2))
    return qty, precio


def _extract_productos_termico(
    texto: str, seen: set[str] | None = None
) -> list[dict[str, Any]]:
    """Parser para boletas térmicas con el ítem repartido en líneas separadas.

    Tolera líneas de ruido OCR entre el código y la cantidad/precio (ej. teclas
    del laptop capturadas en la foto). Formatos:
        CODIGO-MARCA DESCRIPCION
        CANTIDAD,00UN X
        PRECIO_UNIT
        0= TOTAL
    o (PDF / OCR en columnas):
        CODIGO-MARCA
        DESCRIPCION
        -continuacion
        CANTIDAD,00UN X 8.500
    """
    if seen is None:
        seen = set()
    texto = _fix_thermal_ocr_split_codes(texto)
    lines = [ln.strip() for ln in texto.splitlines() if ln.strip()]
    productos: list[dict[str, Any]] = []

    for i, line in enumerate(lines):
        if line.strip().startswith("-"):
            continue

        codigo: str | None = None
        descripcion = ""

        mc = _THERMAL_CODE_DESC_RE.match(line)
        if mc:
            codigo = mc.group(1).strip().upper()
            descripcion = mc.group(2).strip()
            desc_parts: list[str] = []
            for j in range(i + 1, min(len(lines), i + 12)):
                if _THERMAL_QTY_LINE_RE.match(lines[j]):
                    break
                if _is_thermal_desc_continuation(lines[j]):
                    desc_parts.append(lines[j].strip())
            if desc_parts:
                descripcion = (descripcion + " " + " ".join(desc_parts)).strip()
        else:
            mo = _THERMAL_CODE_ONLY_RE.match(line)
            if not mo:
                continue
            codigo = mo.group(1).strip().upper()
            desc_parts = []
            for j in range(i + 1, min(len(lines), i + 12)):
                if _THERMAL_QTY_LINE_RE.match(lines[j]):
                    break
                if _is_thermal_desc_continuation(lines[j]):
                    desc_parts.append(lines[j].strip())
            descripcion = " ".join(desc_parts).strip()

        if not codigo or not _is_plausible_supplier_code(codigo) or codigo in seen:
            continue

        qty: int | None = None
        qty_idx: int | None = None
        precio: int | None = None
        for j in range(i + 1, min(len(lines), i + 22)):
            q, p_inline = _parse_thermal_qty_line(lines[j])
            if q is not None:
                qty = q
                qty_idx = j
                precio = p_inline
                break

        if not qty or qty <= 0 or qty > 99999 or qty_idx is None:
            continue

        if precio is None:
            for k in range(qty_idx + 1, min(len(lines), qty_idx + 8)):
                mt = _THERMAL_LINE_TOTAL_RE.match(lines[k])
                if mt:
                    total_line = _parse_monto_chileno(mt.group(1))
                    if total_line is not None and qty:
                        precio = int(round(total_line / qty))
                    break
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


def _extract_productos_fitalia_fallback(texto: str) -> list[dict[str, Any]]:
    """Respaldo boleta térmica: último CODIGO-MARCA antes de totales + cantidad TOT.UNIDADES."""
    if not re.search(
        r"factura\s+electronica|numero\s*:\s*0*\d+|total\s+neto|monto\s+total",
        texto,
        re.IGNORECASE,
    ):
        return []

    before_totals = texto
    m_neto = re.search(r"total\s+neto", texto, re.IGNORECASE)
    if m_neto:
        before_totals = texto[: m_neto.start()]

    codes = _collect_plausible_thermal_codes(before_totals)
    if not codes:
        codes = _collect_plausible_thermal_codes(texto)
    if not codes:
        return []

    prod = _build_producto_termico(codes[-1], texto)
    return [prod] if prod else []


def _extract_productos(texto: str) -> list[dict[str, Any]]:
    """Extrae productos cuando Vision OCR separa columnas en bloques distintos."""
    texto_norm = _normalize_ocr_text(texto)
    lines = [ln.strip() for ln in texto_norm.splitlines() if ln.strip()]

    # Boleta térmica (Fitalia, etc.): priorizar ítems multilínea antes que columnas.
    termico = _extract_productos_termico(texto_norm)
    if termico:
        return termico

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

    productos = _extract_productos_inline(texto_norm)
    if productos:
        return productos

    fallback = _extract_productos_fitalia_fallback(texto_norm)
    if fallback:
        return fallback

    sin_codigo = _extract_productos_sin_codigo(texto_norm, lines)
    if sin_codigo:
        return sin_codigo

    # Último recurso: regex multilínea en todo el OCR (PDF con texto desordenado).
    m_block = re.search(
        r"\b([A-Z]\d{4,6}-[A-Z0-9]{2,12})\b[\s\S]{0,500}?(\d+),\d+\s*UN",
        texto_norm,
        re.IGNORECASE,
    )
    if m_block:
        codigo = m_block.group(1).upper()
        if _is_plausible_supplier_code(codigo):
            prod = _build_producto_termico(codigo, texto_norm, int(m_block.group(2)))
            if prod:
                return [prod]

    return []


def _extract_producto_regex_simple(texto: str) -> list[dict[str, Any]]:
    """Extracción directa en todo el OCR: código + '2,00UN x 8.500' (mismo renglón)."""
    if not (texto or "").strip():
        return []

    m = re.search(
        r"\b([A-Z]\d{4,6}-[A-Z0-9]{2,12})\b[\s\S]{0,800}?(\d+),\d+\s*UN\s*(?:X|x)\s*([\d.,]+)",
        texto,
        re.IGNORECASE,
    )
    codigo: str | None = None
    qty: int | None = None
    precio: int | None = None

    if m:
        codigo = m.group(1).upper()
        qty = int(m.group(2))
        precio = _parse_monto_chileno(m.group(3))
    else:
        m2 = re.search(
            r"\b([A-Z]\d{4,6})\s+([A-Z]{2,12})\b[\s\S]{0,800}?(\d+),\d+\s*UN\s*(?:X|x)\s*([\d.,]+)",
            texto,
            re.IGNORECASE,
        )
        if m2:
            codigo = f"{m2.group(1).upper()}-{m2.group(2).upper()}"
            qty = int(m2.group(3))
            precio = _parse_monto_chileno(m2.group(4))

    if not codigo or not _is_plausible_supplier_code(codigo):
        return []
    if qty is None:
        qty = _thermal_qty_from_text(texto)
    if not qty or qty <= 0:
        return []
    if precio is None:
        precio = _thermal_unit_price_from_text(texto, codigo, qty)
    if precio is None:
        return []

    return [
        {
            "codigo_proveedor": codigo,
            "cantidad": qty,
            "valor_neto": precio,
        }
    ]


def garantizar_producto_factura(data: dict[str, Any]) -> dict[str, Any]:
    """Asegura productos[] y campos planos para la UI (preview + aplicar)."""
    productos = list(data.get("productos") or [])
    texto = (data.get("ocr_texto_crudo") or "").strip()

    if not productos and texto:
        texto_norm = _normalize_ocr_text(texto)
        productos = _extract_productos_termico(texto_norm)
        if not productos:
            productos = _extract_productos_fitalia_fallback(texto_norm)
        if not productos:
            productos = (
                _extract_productos(texto_norm)
                or _extract_producto_regex_simple(texto_norm)
            )

    if productos:
        data["productos"] = productos
        p0 = productos[0]
        data["producto_codigo"] = p0.get("codigo_proveedor")
        data["producto_cantidad"] = p0.get("cantidad")
        data["producto_valor_neto"] = p0.get("valor_neto")
    else:
        data["productos"] = []

    return data


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

    resultado["ocr_texto_crudo"] = texto
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
    if not resultado["productos"]:
        resultado["productos"] = _extract_productos_fitalia_fallback(texto_parse)
    logger.info("Productos extraídos: %s", resultado["productos"])
    if not resultado["productos"]:
        resultado["productos"] = _extract_producto_regex_simple(texto_parse)

    garantizar_producto_factura(resultado)

    if not resultado["productos"]:
        logger.warning(
            "OCR sin productos detectados (revisar codigo/cantidad en texto crudo)"
        )

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
