"""OCR y parseo de Órdenes de Compra de clientes (pipeline autónomo).

No importa invoice_vision ni invoice_providers.
"""
from __future__ import annotations

import io
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageEnhance, ImageFilter
from google.cloud import vision
from google.oauth2 import service_account
from sqlalchemy import text

from app.extensions import db
from app.utils.codigo_matcher import fuzzy_match_catalogo_codigo
from app.utils.rut_utils import clean_rut, format_rut
from app.ventas.models import Cliente

logger = logging.getLogger(__name__)

OCR_PARSER_REV = "oc-cliente-v9"
RUT_PROPIO = "78074288-7"
RUT_PROPIO_NORM = clean_rut(RUT_PROPIO)

MAX_FILE_BYTES = 12 * 1024 * 1024
MIN_PDF_NATIVE_CHARS = 200
VISION_SCOPES = ["https://www.googleapis.com/auth/cloud-vision"]
FUZZY_THRESHOLD = 92
_VISION_OCR_TIMEOUT_SEC = 120.0
_vision_client_cache: dict[str, vision.ImageAnnotatorClient] = {}

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_RUT_RE = re.compile(
    r"\b(\d{1,2}\.?\d{3}\.?\d{3}-[\dkK]|\d{7,8}-[\dkK])\b",
    re.IGNORECASE,
)

_ITEM_LINE_RE = re.compile(
    r"^\s*(\d{1,3})\s+"
    r"([A-Z0-9][A-Z0-9\-\./]{3,24})"
    r"(?:\s+(.+?))?"
    r"\s+(\d{1,6})\s+"
    r"([\d.,:\s]+?)\s+"
    r"([\d.,:\s]+?)\s*$",
    re.IGNORECASE,
)

_ITEM_MINIMAL_RE = re.compile(
    r"^\s*(\d{1,3})\s+"
    r"([A-Z0-9][A-Z0-9\-\./]{3,24})\s+"
    r"(\d{1,6})\s+"
    r"([\d.,:\s+]+)\s+"
    r"([\d.,:\s+]+)\s*$",
    re.IGNORECASE,
)

_ITEM_CODE_LINE_RE = re.compile(
    r"^\s*(\d{1,3})\s+([A-Z0-9][A-Z0-9\-\./]{5,24})\s*$",
    re.IGNORECASE,
)

_ITEM_CODE_WITH_DESC_RE = re.compile(
    r"^\s*(\d{1,3})\s+([A-Z0-9][A-Z0-9\-\./]{5,24})(?:\s+(.+))?\s*$",
    re.IGNORECASE,
)

_STANDALONE_CODE_RE = re.compile(
    r"^([A-Z0-9][A-Z0-9\-\./]{5,24})$",
    re.IGNORECASE,
)

_ITEM_ONE_LINE_RE = re.compile(
    r"^\s*(\d{1,3})\s+"
    r"([A-Z0-9][A-Z0-9\-\./]{5,24})\s+"
    r"([A-ZÁÉÍÓÚÑ][^\d$]{2,}?)"
    r"(?:\s+\$)?"
    r"\s+(\d{1,6})\s+"
    r"([\d.,]+)"
    r"(?:\s+([\d.,]+))?\s*$",
    re.IGNORECASE,
)

# OC chilena típica: "1 CYTIGOP74100 $ 1 140,336 140,336" (descripción en la línea siguiente)
_ITEM_CODE_PRICE_LINE_RE = re.compile(
    r"^\s*(\d{1,3})\s+"
    r"([A-Z0-9][A-Z0-9\-\./]{5,24})\s+"
    r"\$?\s*"
    r"(?:(?:SAN|UND|UN|UNIDAD|KIT|SET)\s+)?"
    r"\$?\s*"
    r"(\d{1,6})\s+"
    r"([\d.,]+)"
    r"(?:\s+([\d.,]+))?"
    r"\s*$",
    re.IGNORECASE,
)

_ITEM_NUM_CODE_RE = re.compile(
    r"^\s*(\d{1,3})\s+([A-Z0-9][A-Z0-9\-\./]{5,24})(?:\s|$)",
    re.IGNORECASE,
)

_TABLE_HEADER_WORDS = frozenset({
    "cantidad", "precio", "unitario", "descto", "descto.", "total", "totil", "totals",
    "moneda", "maneda", "unidad", "item", "descripción", "descripcion", "descripci",
    "precio unitario", "iva", "neto", "noto", "desct", "totil",
})

_PRODUCT_CODE_RE = re.compile(
    r"\b([A-Z]{2,6}[A-Z0-9]{4,18})\b",
    re.IGNORECASE,
)

_SKIP_PRODUCT_CODES = frozenset({
    "DESPACHAR", "FACTURAR", "PRESENTAR", "OBSERVACIONES", "CONCEPCION",
    "PROVIDENCIA", "SANTIAGO", "CONCEPCI", "TELEFONO", "DIRECCION",
    "ATENCION", "SENORES", "ANDES", "PARTS", "COMERCIAL",
})


def _looks_like_product_code(code: str) -> bool:
    c = (code or "").upper().strip()
    if len(c) < 8 or c in _SKIP_PRODUCT_CODES:
        return False
    if c.isalpha():
        return False
    if re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", c):
        return False
    if re.fullmatch(r"\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}", c):
        return False
    if _RUT_RE.fullmatch(c) or re.fullmatch(r"\d{7,8}-[\dkK]", c, re.I):
        return False
    if re.match(r"^[A-Z]{2,5}-[A-Z0-9][A-Z0-9\-]{4,}$", c):
        return True
    return bool(re.search(r"\d", c) or "ERP" in c or "OEM" in c)


def _item_table_zone(texto: str) -> str:
    """Cuerpo de la tabla. No cortar en Fecha O/C ni Forma de Pago: el OCR
    suele intercalarlo del encabezado y se pierden las filas 2+."""
    m = re.search(r"Item\s+Descripci[oó]n", texto, re.IGNORECASE)
    if not m:
        return texto
    tail = texto[m.end():]
    m_end = re.search(
        r"(?:Facturar\s+a|Presentar\s+Factura)",
        tail,
        re.IGNORECASE,
    )
    if m_end:
        return tail[: m_end.start()]
    m_neto = re.search(r"(?m)^\s*(?:Neto|Noto)\s*\$", tail, re.IGNORECASE)
    if m_neto:
        return tail[: m_neto.start()]
    m_obs = re.search(r"(?m)^\s*Observaciones\b", tail, re.IGNORECASE)
    if m_obs:
        return tail[: m_obs.start()]
    return tail


def _match_item_code_line(line: str) -> re.Match[str] | None:
    s = (line or "").strip()
    if not s:
        return None
    m = _ITEM_CODE_LINE_RE.match(s)
    if m:
        return m
    m = _ITEM_CODE_WITH_DESC_RE.match(s)
    if m and _looks_like_product_code(m.group(2)):
        return m
    return None


def _is_standalone_code_line(line: str) -> str | None:
    s = (line or "").strip()
    if not s or _is_table_header_word(s):
        return None
    m = _STANDALONE_CODE_RE.match(s)
    if not m:
        return None
    code = m.group(1).upper()
    if _looks_like_product_code(code):
        return code
    return None


def _is_unit_token(line: str) -> bool:
    return bool(re.fullmatch(r"(SAN|UND|UN|UNIDAD|\$|S|KIT|SET)", (line or "").strip(), re.I))


def _clean_item_description(raw: str) -> str:
    s = re.sub(r"\s+", " ", (raw or "").strip())
    if not s:
        return ""
    while True:
        nxt = re.sub(r"^[\d$.,]+\s+", "", s)
        if nxt == s:
            break
        s = nxt
    s = re.sub(r"^\$\s*", "", s)
    s = re.sub(r"\b(SAN|UND|UNIDAD|KIT|SET)\b", " ", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip(" -$")
    if not s or s == "$" or re.fullmatch(r"[\d$.,]+", s):
        return ""
    return s[:255]


def _item_code_key(it: dict[str, Any]) -> str:
    return (it.get("codigo_producto") or "").strip().upper()


def _count_item_codes(items: list[dict[str, Any]]) -> int:
    return sum(1 for it in items if _item_code_key(it))


def _parse_code_price_line(line: str) -> dict[str, Any] | None:
    m = _ITEM_CODE_PRICE_LINE_RE.match((line or "").strip())
    if not m:
        return None
    codigo = m.group(2).upper().strip()
    if not _looks_like_product_code(codigo):
        return None
    qty = max(int(m.group(3)), 1)
    precio = _parse_monto_chileno(m.group(4)) or 0.0
    sub = _parse_monto_chileno(m.group(5)) if m.group(5) else None
    if sub is None:
        sub = round(precio * qty, 2)
    return {
        "numero_item": int(m.group(1)),
        "codigo_producto": codigo,
        "descripcion": "",
        "cantidad": qty,
        "precio_unitario": precio,
        "subtotal": sub,
    }


def _overlay_item_descriptions(
    base: list[dict[str, Any]], extra: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_code = {_item_code_key(it): it for it in extra if _item_code_key(it)}
    out: list[dict[str, Any]] = []
    for it in base:
        row = dict(it)
        desc = _clean_item_description(row.get("descripcion") or "")
        if not desc:
            other = by_code.get(_item_code_key(row))
            if other:
                desc = _clean_item_description(other.get("descripcion") or "")
        row["descripcion"] = desc
        if not float(row.get("precio_unitario") or 0):
            other = by_code.get(_item_code_key(row))
            if other and float(other.get("precio_unitario") or 0):
                row["cantidad"] = other.get("cantidad") or row.get("cantidad") or 1
                row["precio_unitario"] = other["precio_unitario"]
                row["subtotal"] = other.get("subtotal") or other["precio_unitario"]
        out.append(row)
    return out


def _harvest_missing_numbered_items(
    texto: str, already: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Códigos 'N CODE' que quedaron fuera de la zona de tabla (encabezado OCR)."""
    used = {_item_code_key(it) for it in already if _item_code_key(it)}
    lines = [ln.strip() for ln in (texto or "").splitlines()]
    extra: list[dict[str, Any]] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        parsed = _parse_code_price_line(ln)
        if parsed and parsed["codigo_producto"] not in used:
            i += 1
            more, i = _collect_following_descriptions(lines, i)
            parsed["descripcion"] = _clean_item_description(" ".join(more))
            used.add(parsed["codigo_producto"])
            extra.append(parsed)
            continue
        m = _match_item_code_line(ln)
        if m and _looks_like_product_code(m.group(2)):
            code = m.group(2).upper()
            if code not in used:
                desc_parts: list[str] = []
                if m.lastindex and m.lastindex >= 3 and m.group(3):
                    desc_parts.append(m.group(3).strip())
                i += 1
                more, i = _collect_following_descriptions(lines, i)
                desc_parts.extend(more)
                extra.append(
                    {
                        "numero_item": int(m.group(1)),
                        "codigo_producto": code,
                        "descripcion": _clean_item_description(" ".join(desc_parts)),
                        "cantidad": 1,
                        "precio_unitario": 0.0,
                        "subtotal": 0.0,
                    }
                )
                used.add(code)
                continue
        i += 1
    extra.sort(key=lambda it: int(it.get("numero_item") or 0))
    return extra


def _collect_following_descriptions(lines: list[str], start: int) -> tuple[list[str], int]:
    """Toma solo la descripción del ítem actual (1 línea de pieza + unidad).

    Evita pegar el siguiente ítem (p. ej. otro PARACHOQUE…) cuando el OCR
    no trae número/código de la 2.ª fila.
    """
    parts: list[str] = []
    i = start
    while i < len(lines):
        cur = lines[i]
        if _match_item_code_line(cur) or _is_table_section_end(cur):
            break
        if _is_standalone_code_line(cur):
            break
        if re.fullmatch(r"\d{1,3}", cur) and int(cur) <= 200:
            break
        if _is_unit_token(cur):
            i += 1
            if parts:
                break
            continue
        if _is_description_line(cur):
            parts.append(cur)
            i += 1
            while i < len(lines) and _is_unit_token(lines[i]):
                i += 1
            break
        break
    return parts, i


def _is_table_section_end(line: str) -> bool:
    return bool(
        re.match(
            r"^(facturar|presentar|observ|rut|rat\b|maneda|moneda|unidad|cantidad|neto|noto|iva|total\b|item\b)",
            (line or "").strip(),
            re.I,
        )
    )


def _credentials_path() -> Path:
    raw = (
        os.environ.get("GOOGLE_VISION_CREDENTIALS") or "data/google_service_account.json"
    ).strip()
    path = Path(raw)
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    return path


def _parse_monto_chileno(raw: str) -> float | None:
    plain = (raw or "").strip()
    if not plain:
        return None
    s = re.sub(r"\s+", "", plain)
    s = s.lstrip("+")  # OCR: "+2,008" → "2,008"
    s = re.sub(r"^(\d+):(\d{3})$", r"\1.\2", s)

    # Miles chilenos con punto: 163.017
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
        return float(s.replace(".", ""))

    # OCR con coma como separador de miles: 163,017
    if re.fullmatch(r"\d{1,3}(,\d{3})+", s):
        return float(s.replace(",", ""))

    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        left, right = s.split(",", 1)
        if right.isdigit() and len(right) == 3 and left.replace(".", "").isdigit():
            return float(left.replace(".", "") + right)
        s = s.replace(".", "").replace(",", ".")
    try:
        v = float(s)
    except ValueError:
        if re.fullmatch(r"\d{3,9}", plain.replace(" ", "")):
            return float(plain.replace(" ", ""))
        return None
    if v < 0:
        return None
    return round(v, 2)


def _score_chilean_totales_triplet(
    neto: float, iva: float, total: float
) -> float | None:
    """Puntaje de coherencia contable (menor = mejor). None si no cuadra."""
    if neto <= 0 or iva < 0 or total <= 0:
        return None
    sum_err = abs(neto + iva - total)
    iva_expected = round(neto * 0.19, 0)
    iva_err = abs(iva - iva_expected)
    if sum_err > max(5.0, total * 0.015):
        return None
    if iva_err > max(50.0, neto * 0.06):
        return None
    if total < neto or neto < iva:
        return None
    return sum_err + iva_err


def _pick_coherent_totales_triplet(
    amounts: list[tuple[int, float]],
) -> dict[str, float | None]:
    """Elige neto/iva/total coherentes entre montos OCR (evita ruido de firmas)."""
    if len(amounts) < 3:
        return {"neto": None, "iva": None, "total": None}

    best: dict[str, float] | None = None
    best_key: tuple[float, float] | None = None
    vals = amounts[-12:]  # últimos montos del pie

    for i in range(len(vals)):
        for j in range(len(vals)):
            if j == i:
                continue
            for k in range(len(vals)):
                if k in (i, j):
                    continue
                for neto, iva, total in (
                    (vals[i][1], vals[j][1], vals[k][1]),
                    (vals[i][1], vals[k][1], vals[j][1]),
                    (vals[j][1], vals[i][1], vals[k][1]),
                    (vals[j][1], vals[k][1], vals[i][1]),
                    (vals[k][1], vals[i][1], vals[j][1]),
                    (vals[k][1], vals[j][1], vals[i][1]),
                ):
                    score = _score_chilean_totales_triplet(neto, iva, total)
                    if score is None:
                        continue
                    pos = (vals[i][0] + vals[j][0] + vals[k][0]) / 3.0
                    key = (score, -pos)
                    if best_key is None or key < best_key:
                        best_key = key
                        best = {"neto": neto, "iva": iva, "total": total}

    if best:
        return best
    return {"neto": None, "iva": None, "total": None}


def _totales_triplet_coherent(
    neto: float | None, iva: float | None, total: float | None
) -> bool:
    if neto is None or iva is None or total is None:
        return False
    return _score_chilean_totales_triplet(neto, iva, total) is not None


def _find_cantidad_header_index(lines: list[str]) -> int | None:
    for i, ln in enumerate(lines):
        if re.search(r"\bCantidad\b", ln, re.IGNORECASE):
            return i
    return None


def _parse_fecha_chilena(raw: str | None) -> str | None:
    s = (raw or "").strip()
    if not s:
        return None
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d-%m-%y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s[:10], fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    m = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})", s)
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 2000
    try:
        return datetime(y, mo, d).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _preprocess_image_for_ocr(image_bytes: bytes) -> bytes:
    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")
        w, h = img.size
        if w < 1500:
            scale = 1500 / w
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        img = img.convert("L")
        enhancer = ImageEnhance.Contrast(img)
        img = enhancer.enhance(1.5)
        img = img.filter(ImageFilter.SHARPEN)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return image_bytes


def _vision_contact_error(exc: BaseException) -> ValueError:
    """Traduce fallos de Vision a un mensaje accionable (no siempre es red)."""
    msg = str(exc) or ""
    low = msg.lower()
    if "billing" in low or "billing_disabled" in low:
        return ValueError(
            "Google Cloud Vision requiere facturación activa en el proyecto de GCP. "
            "Activá billing en Google Cloud Console (Vision API) y reintentá en unos minutos."
        )
    if "permission_denied" in low or "403" in low or "permission denied" in low:
        return ValueError(
            "Google Cloud Vision rechazó la solicitud (sin permiso o API deshabilitada). "
            f"Detalle: {msg[:220]}"
        )
    if "unauthenticated" in low or "401" in low or (
        "invalid" in low and "credential" in low
    ):
        return ValueError(
            "Credenciales de Google Cloud Vision inválidas o vencidas. "
            "Revisá GOOGLE_VISION_CREDENTIALS / data/google_service_account.json."
        )
    if "timeout" in low or "deadline" in low or "timed out" in low:
        return ValueError(
            "Google Cloud Vision no respondió a tiempo (timeout). "
            "Reintentá con un archivo más liviano o más tarde."
        )
    return ValueError(
        "No se pudo contactar a Google Cloud Vision (timeout o red). "
        f"Detalle: {type(exc).__name__}: {msg[:200]}"
    )


def _vision_ocr_text(image_bytes: bytes, cred_path: Path) -> str:
    client = _vision_client_cache.get(str(cred_path.resolve()))
    if client is None:
        credentials = service_account.Credentials.from_service_account_file(
            str(cred_path),
            scopes=VISION_SCOPES,
        )
        client = vision.ImageAnnotatorClient(credentials=credentials)
        _vision_client_cache[str(cred_path.resolve())] = client
    processed = _preprocess_image_for_ocr(image_bytes)
    image = vision.Image(content=processed)
    image_context = vision.ImageContext(language_hints=["es"])

    try:
        response = client.document_text_detection(
            image=image,
            image_context=image_context,
            timeout=_VISION_OCR_TIMEOUT_SEC,
        )
    except Exception as exc:
        raise _vision_contact_error(exc) from exc

    if response.error.message:
        raise ValueError(response.error.message)

    if response.full_text_annotation and response.full_text_annotation.text:
        return response.full_text_annotation.text
    if response.text_annotations:
        return response.text_annotations[0].description or ""
    return ""


def _extract_pdf_native_text(pdf_bytes: bytes) -> str:
    try:
        import fitz  # PyMuPDF

        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            if doc.page_count < 1:
                return ""
            return (doc.load_page(0).get_text("text") or "").strip()
    except Exception:
        return ""


def _convert_pdf_first_page_to_png(pdf_bytes: bytes) -> bytes:
    try:
        import fitz  # PyMuPDF

        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            if doc.page_count < 1:
                raise ValueError("El PDF no tiene páginas")
            page = doc.load_page(0)
            # Render más liviano: reduce tiempo/cuello de botella en PDFs pesados.
            pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
            return pix.tobytes("png")
    except ImportError as exc:
        raise ValueError(
            "Para analizar PDF instale pymupdf (pip install pymupdf)."
        ) from exc
    except Exception as exc:
        raise ValueError(f"No se pudo convertir el PDF a imagen: {exc}") from exc


def _extension_from_filename(filename: str) -> str:
    name = (filename or "").lower().strip()
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[-1]


def _extract_text_from_file(file_bytes: bytes, ext: str, cred_path: Path) -> tuple[str, str]:
    """Retorna (texto, fuente) donde fuente es 'pdf_text', 'vision' o 'vision_pdf'."""
    ext = (ext or "").lower().lstrip(".")
    if ext == "pdf":
        t0 = __import__("time").perf_counter()
        native = _extract_pdf_native_text(file_bytes)
        logger.info(
            "oc OCR pdf native chars=%s (%.2fs)",
            len(native or ""),
            __import__("time").perf_counter() - t0,
        )
        if len(native) >= MIN_PDF_NATIVE_CHARS:
            return native, "pdf_text"
        logger.info("oc OCR pdf: renderizando 1ra página PNG para OCR…")
        png = _convert_pdf_first_page_to_png(file_bytes)
        logger.info("oc OCR pdf: PNG listo (%s bytes), llamando Vision…", len(png))
        t1 = __import__("time").perf_counter()
        texto = _vision_ocr_text(png, cred_path)
        logger.info(
            "oc OCR pdf: Vision listo (%.2fs), texto chars=%s",
            __import__("time").perf_counter() - t1,
            len(texto or ""),
        )
        return texto, "vision_pdf"
    return _vision_ocr_text(file_bytes, cred_path), "vision"


def _extract_numero_oc(texto: str) -> str | None:
    patterns = [
        r"Orden\s+de\s+Compra\s+N[°ºo\.]*\s*(\d{3,10})",
        r"O\s*/\s*C\.?\s*N[°ºo\.]*\s*(\d{3,10})",
        r"OC\s*N[°ºo\.]*\s*(\d{3,10})",
        r"O\.?C\.?\s*[:#]?\s*(\d{3,10})",
    ]
    for pat in patterns:
        m = re.search(pat, texto, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def _extract_labeled_field(texto: str, labels: list[str]) -> str | None:
    for label in labels:
        pat = rf"{re.escape(label)}\s*:?\s*(.+?)(?:\n|$)"
        m = re.search(pat, texto, re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            if not val or len(val) >= 200:
                continue
            if _is_table_header_word(val):
                continue
            return val
    return None


def _is_table_header_word(value: str) -> bool:
    key = re.sub(r"\s+", " ", (value or "").strip().lower())
    if key in _TABLE_HEADER_WORDS:
        return True
    return any(w in key for w in ("precio unitario", "descto"))


def _extract_forma_pago(texto: str) -> str | None:
    m = re.search(
        r"Forma\s+de\s+Pago\s*:?\s*(.+?)(?:\n|$)",
        texto,
        re.IGNORECASE,
    )
    if not m:
        return None
    val = m.group(1).strip()
    if not val or _is_table_header_word(val):
        return None
    if re.match(r"^despachar\b", val, re.IGNORECASE):
        return None
    return val[:100]


def _document_footer_text(texto: str) -> str:
    """Pie real del documento (después de la tabla). Evita 'Total' del encabezado."""
    m = re.search(r"Facturar\s+a", texto, re.IGNORECASE)
    if m:
        return texto[m.start():]
    m = re.search(r"Presentar\s+Factura", texto, re.IGNORECASE)
    if m:
        return texto[m.start():]
    lines = [ln for ln in (texto or "").splitlines() if ln.strip()]
    return "\n".join(lines[-18:])


def _extract_labeled_monto(texto: str, labels: tuple[str, ...], min_val: float = 1000.0) -> float | None:
    found: list[float] = []
    lines = texto.splitlines()
    for label in labels:
        for m in re.finditer(
            rf"(?m)^\s*{label}\s*\$?\s*:?\s*([\d.,]+)",
            texto,
            re.IGNORECASE,
        ):
            parsed = _parse_monto_chileno(m.group(1))
            if parsed is not None and parsed >= min_val:
                found.append(parsed)
        for i, ln in enumerate(lines):
            if re.match(rf"^\s*{label}\s*\$?\s*:?\s*$", ln.strip(), re.IGNORECASE):
                for nxt in lines[i + 1 : i + 6]:
                    parsed = _parse_monto_chileno(nxt.strip())
                    if parsed is not None and parsed >= min_val:
                        found.append(parsed)
                        break
    return found[-1] if found else None


def _extract_footer_totales(texto: str) -> dict[str, float | None]:
    """Totales al pie del documento (OCR suele poner montos tras firmas)."""
    lines = [ln.strip() for ln in texto.splitlines() if ln.strip()]
    amounts: list[tuple[int, float]] = []
    start = max(0, len(lines) - 45)
    for idx, ln in enumerate(lines[start:], start=start):
        if re.match(r"^(?:Neto|Noto|IVA|Total)\s*\$?\s*:?\s*$", ln, re.I):
            continue
        if re.search(r"[A-Za-zÁÉÍÓÚáéíóú]{4,}", ln) and not re.fullmatch(
            r"[\d.,+\s$]+", ln
        ):
            continue
        amt = _parse_monto_chileno(ln)
        if amt is not None and amt >= 100:
            amounts.append((idx, amt))

    picked = _pick_coherent_totales_triplet(amounts)
    if _totales_triplet_coherent(
        picked.get("neto"), picked.get("iva"), picked.get("total")
    ):
        return picked
    return {"neto": None, "iva": None, "total": None}


def _extract_totales(texto: str) -> dict[str, float | None]:
    pie = _document_footer_text(texto)
    neto = _extract_labeled_monto(pie, ("Neto", "Noto"))
    iva = _extract_labeled_monto(pie, ("IVA",), min_val=100.0)
    total = _extract_labeled_monto(pie, ("Total", "Totil"))

    m = re.search(
        r"IVA\s*(?:\(19%\)|19\s*%)\s*:?\s*\$?\s*([\d.,+]+)",
        pie,
        re.IGNORECASE,
    )
    if m:
        parsed = _parse_monto_chileno(m.group(1))
        if parsed and parsed >= 100:
            iva = parsed

    if not _totales_triplet_coherent(neto, iva, total):
        footer = _extract_footer_totales(pie)
        if _totales_triplet_coherent(
            footer.get("neto"), footer.get("iva"), footer.get("total")
        ):
            neto = neto or footer.get("neto")
            iva = iva or footer.get("iva")
            total = total or footer.get("total")

    if neto is None:
        neto = _extract_labeled_monto(texto, ("Neto", "Noto"))

    # El "Total" de la cabecera de tabla suele pegarse al primer monto de línea.
    if total is not None and neto is not None and total + 1 < neto:
        total = None
    if iva is not None and neto is not None and iva >= neto:
        iva = None

    return {"neto": neto, "iva": iva, "total": total}


def _is_description_line(line: str) -> bool:
    s = line.strip()
    if not s or len(s) < 3:
        return False
    if (
        _ITEM_MINIMAL_RE.match(s)
        or _ITEM_LINE_RE.match(s)
        or _ITEM_CODE_LINE_RE.match(s)
        or _ITEM_CODE_PRICE_LINE_RE.match(s)
    ):
        return False
    if re.match(r"^(neto|noto|iva|total|subtotal|descripci|facturar|presentar|observ)", s, re.IGNORECASE):
        return False
    if _is_table_header_word(s):
        return False
    if _RUT_RE.search(s):
        return False
    if re.fullmatch(r"[\d.,+$]+", s):
        return False
    if re.fullmatch(r"[A-Z]{1,4}", s):
        return False
    return bool(re.search(r"[A-Za-zÁÉÍÓÚáéíóúÑñ]{3,}", s))


def _extract_codigo_producto(texto: str) -> str | None:
    for m in _ITEM_CODE_LINE_RE.finditer(texto):
        code = m.group(2).upper().strip()
        if _looks_like_product_code(code):
            return code
    for m in _PRODUCT_CODE_RE.finditer(texto):
        code = m.group(1).upper()
        if _looks_like_product_code(code):
            return code
    return None


def _extract_descripcion_tabla(texto: str, codigo: str | None) -> str:
    lines = [ln.strip() for ln in texto.splitlines()]
    parts: list[str] = []
    after_code = False
    for ln in lines:
        if codigo and codigo in ln.upper().replace(" ", ""):
            after_code = True
            continue
        if not after_code:
            continue
        if re.match(
            r"^(facturar|presentar|observ|fecha|forma|rut|rat\b|maneda|moneda|cantidad|item|neto|noto|iva|total)",
            ln,
            re.I,
        ):
            break
        if re.fullmatch(r"(SAN|UND|UN|UNIDAD|\$|S|KIT|SET)", ln, re.I):
            continue
        if _is_description_line(ln):
            parts.append(ln)
            if len(parts) >= 2:
                break
        elif parts:
            break
    return " ".join(parts)[:255]


def _extract_observaciones(texto: str) -> dict[str, str | None]:
    m = re.search(r"Observaciones\s*\n([\s\S]*)", texto, re.IGNORECASE)
    rest = (m.group(1) if m else "").strip()
    obs_lines: list[str] = []
    for ln in rest.splitlines():
        s = ln.strip()
        if not s:
            continue
        if re.match(r"^(Luis|Nombre|Firma)\b", s, re.I):
            break
        if re.match(r"^(?:Neto|Noto|IVA|Total)\s*\$?\s*:?\s*$", s, re.I):
            continue
        if re.match(
            r"^(?:Fecha|Forma|Maneda|Moneda|Cantidad|Precio|Descto|Totil|Rut|Rat)\b",
            s,
            re.I,
        ):
            break
        if re.fullmatch(r"[\d.,]+", s):
            continue
        obs_lines.append(s)
    obs = "\n".join(obs_lines).strip()
    marca = None
    vehiculo = None
    vin = None

    # VIN / chasis en observaciones
    m_vin = re.search(
        r"(?:VIN|CHASIS|CHASSIS)\s*[;:=\-]?\s*([A-HJ-NPR-Z0-9]{11,17})",
        obs,
        re.IGNORECASE,
    )
    if m_vin:
        vin = re.sub(r"[^A-Za-z0-9]", "", m_vin.group(1)).upper()
    if not vin:
        for ln in obs.splitlines():
            m2 = re.search(r"\b([A-HJ-NPR-Z0-9]{17})\b", ln.upper())
            if m2:
                vin = m2.group(1)
                break

    if re.search(r"GREAT\s*WALL", obs, re.IGNORECASE):
        marca = "GREAT WALL"
        for ln in obs.splitlines():
            vm = re.search(
                r"(GREAT\s*WALL\s+POER\s+[\d.]+\s+\d{4})",
                ln,
                re.IGNORECASE,
            )
            if vm:
                vehiculo = re.sub(r"\s+", " ", vm.group(1)).strip()
                break
    elif re.search(r"\bJAC\b", obs, re.IGNORECASE) or re.search(
        r"\bJAC\s+T\d", texto, re.IGNORECASE
    ):
        marca = "JAC"
        for ln in obs.splitlines():
            vm = re.search(r"(JAC\s+T\d+[^\n]*\d{4})", ln, re.IGNORECASE)
            if vm:
                vehiculo = re.sub(r"\s+", " ", vm.group(1)).strip()
                break
    elif re.search(r"\bMAXUS\b", obs, re.IGNORECASE) or re.search(
        r"\bMAXUS\b", texto, re.IGNORECASE
    ):
        marca = "MAXUS"
        for ln in obs.splitlines():
            vm = re.search(r"(MAXUS\s+T\d+\s+\d{4})", ln, re.IGNORECASE)
            if vm:
                vehiculo = re.sub(r"\s+", " ", vm.group(1)).strip()
                break
    elif re.search(r"\bCHANGAN\b", obs, re.IGNORECASE) or re.search(
        r"\bCHANGAN\b", texto, re.IGNORECASE
    ):
        marca = "CHANGAN"
        for ln in obs.splitlines():
            vm = re.search(
                r"(CHANGAN\s+HUNTER(?:\s+4X4)?\s+[\d.]+\s+\d{4})",
                ln,
                re.IGNORECASE,
            )
            if vm:
                vehiculo = re.sub(r"\s+", " ", vm.group(1)).strip().upper()
                break
        if not vehiculo:
            for ln in obs.splitlines():
                vm = re.search(
                    r"(HUNTER(?:\s+4X4)?\s+[\d.]+\s+\d{4})",
                    ln,
                    re.IGNORECASE,
                )
                if vm:
                    vehiculo = "CHANGAN " + re.sub(r"\s+", " ", vm.group(1)).strip().upper()
                    break
    elif re.search(r"\bHUNTER\s+4X4\b", obs, re.IGNORECASE) or re.search(
        r"\bHUNTER\s+4X4\b", texto, re.IGNORECASE
    ):
        marca = "CHANGAN"
        for ln in (obs or texto).splitlines():
            vm = re.search(
                r"(HUNTER(?:\s+4X4)?\s+[\d.]+\s+\d{4})",
                ln,
                re.IGNORECASE,
            )
            if vm:
                vehiculo = "CHANGAN " + re.sub(r"\s+", " ", vm.group(1)).strip().upper()
                break
    elif re.search(r"\bJMC\b", obs, re.IGNORECASE):
        marca = "JMC"
        for ln in obs.splitlines():
            vm = re.search(r"(JMC\s+[A-Z0-9][A-Z0-9 \-.]*\d{4})", ln, re.IGNORECASE)
            if vm and not re.search(r"\bVIN\b", ln, re.I):
                vehiculo = re.sub(r"\s+", " ", vm.group(1)).strip().upper()
                break
    elif re.search(r"\bCHERY\b", obs, re.IGNORECASE):
        marca = "CHERY"
        for ln in obs.splitlines():
            vm = re.search(r"(CHERY\s+[A-Z0-9][A-Z0-9 \-.]*\d{4})", ln, re.IGNORECASE)
            if vm and not re.search(r"\bVIN\b", ln, re.I):
                vehiculo = re.sub(r"\s+", " ", vm.group(1)).strip().upper()
                break
    elif re.search(r"\bBYD\b", obs, re.IGNORECASE):
        marca = "BYD"
        for ln in obs.splitlines():
            vm = re.search(r"(BYD\s+[A-Z0-9][A-Z0-9 \-.]*\d{4})", ln, re.IGNORECASE)
            if vm and not re.search(r"\bVIN\b", ln, re.I):
                vehiculo = re.sub(r"\s+", " ", vm.group(1)).strip().upper()
                break

    # Genérico: línea con marca+año si aún no hay vehiculo
    if not vehiculo:
        for ln in obs.splitlines():
            s = re.sub(r"\s+", " ", ln).strip()
            if re.search(r"\bVIN\b|\bCHASIS\b|\bGUIA\b|\bSINIESTRO\b", s, re.I):
                continue
            if re.match(r"^[-–]", s):
                continue
            if re.search(r"\b(?:19|20)\d{2}\b", s) and len(s) >= 8:
                vehiculo = s.upper()
                if not marca:
                    for brand in (
                        "GREAT WALL", "CHANGAN", "CHERY", "BYD", "JMC", "JAC",
                        "MAXUS", "HAVAL", "GEELY", "FOTON", "MG",
                    ):
                        if brand in vehiculo:
                            marca = brand
                            break
                break

    return {"texto": obs, "marca": marca, "vehiculo": vehiculo, "vin": vin}


def _is_price_block_end(line: str) -> bool:
    return bool(
        re.match(
            r"^(facturar|presentar|observ|neto|noto|iva)\b",
            (line or "").strip(),
            re.I,
        )
    )


def _pair_columnar_amounts(amounts: list[float]) -> list[tuple[int, float, float]]:
    """Agrupa Precio Unitario + Total repetido (140,336 / 140,336)."""
    rows: list[tuple[int, float, float]] = []
    i = 0
    while i < len(amounts):
        precio = amounts[i]
        if i + 1 < len(amounts) and abs(amounts[i + 1] - precio) / max(precio, 1) <= 0.02:
            rows.append((1, precio, precio))
            i += 2
        else:
            rows.append((1, precio, precio))
            i += 1
    return rows


def _allocate_columnar_prices(
    amounts: list[float], n_items: int
) -> list[tuple[int, float, float]]:
    """Reparte montos OCR a N ítems. Si hay N+1 montos y el primero está duplicado
    (unitario=total de la 1.ª fila), el resto son precios sueltos — no se pierda
    el 4.º ítem."""
    if n_items <= 0:
        return _pair_columnar_amounts(amounts)
    if not amounts:
        return []
    if len(amounts) <= n_items:
        return [(1, amt, amt) for amt in amounts]
    if len(amounts) >= 2 * n_items:
        return _pair_columnar_amounts(amounts[: 2 * n_items])

    extra = len(amounts) - n_items
    rows: list[tuple[int, float, float]] = []
    i = 0
    while i < len(amounts) and len(rows) < n_items:
        precio = amounts[i]
        if (
            extra > 0
            and i + 1 < len(amounts)
            and abs(amounts[i + 1] - precio) / max(precio, 1) <= 0.02
        ):
            rows.append((1, precio, precio))
            i += 2
            extra -= 1
        else:
            rows.append((1, precio, precio))
            i += 1
    return rows


def _extract_columnar_amount_values(texto: str) -> list[float]:
    lines = [ln.strip() for ln in texto.splitlines()]
    header_i = _find_cantidad_header_index(lines)
    if header_i is None:
        return []

    amounts: list[float] = []
    for ln in lines[header_i + 1 :]:
        if _is_price_block_end(ln):
            break
        if not ln or _is_table_header_word(ln) or _is_unit_token(ln):
            continue
        if _parse_code_price_line(ln) or _match_item_code_line(ln) or _is_standalone_code_line(ln):
            continue
        if _is_description_line(ln):
            continue
        if re.fullmatch(r"\d{1,3}", ln) and int(ln) <= 200:
            continue
        amt = _parse_monto_chileno(ln)
        if amt is None or amt < 500:
            continue
        amounts.append(amt)
    return amounts


def _extract_items_columnar_prices(
    texto: str, n_items: int | None = None
) -> list[tuple[int, float, float]]:
    """Precios/cantidades en bloque columnar tras encabezado Cantidad."""
    amounts = _extract_columnar_amount_values(texto)
    if n_items and n_items > 0:
        return _allocate_columnar_prices(amounts, n_items)
    return _pair_columnar_amounts(amounts)


def _split_piece_phrases(text: str) -> list[str]:
    """Separa descripciones pegadas del OCR (2 parachoques en un solo string)."""
    s = re.sub(r"\s+", " ", (text or "").strip())
    if not s:
        return []
    parts = re.split(
        r"(?=\bPARACHOQUES?\s+(?:DELANTERO|TRASERO|DELANT|TRAS)\b)",
        s,
        flags=re.IGNORECASE,
    )
    out = [p.strip(" -|,") for p in parts if p and p.strip(" -|,")]
    if len(out) >= 2:
        return out
    return [s]


def _looks_like_piece_description(line: str) -> bool:
    s = (line or "").strip()
    if not s or len(s) < 6:
        return False
    if not _is_description_line(s):
        return False
    return bool(
        re.search(
            r"\b(PARACHOQUE|GUARDAFANGO|OPTICA|FAROL|RADIADOR|CAPOT|"
            r"PUERTA|MOLDURA|ESPEJO|TERMINAL|BISEL|SOPORTE|DEFENSA)\b",
            s,
            re.I,
        )
    )


def _extract_orphan_description_items(
    texto: str,
    already: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Descripciones sueltas (sin N°/código) que el OCR dejó fuera del 1.er ítem."""
    zone = _item_table_zone(texto)
    lines = [ln.strip() for ln in zone.splitlines() if ln.strip()]
    taken: set[str] = set()
    for it in already:
        for part in _split_piece_phrases(it.get("descripcion") or ""):
            taken.add(re.sub(r"\s+", " ", part).strip().upper())

    orphans: list[dict[str, Any]] = []
    for ln in lines:
        if _match_item_code_line(ln) or _is_standalone_code_line(ln):
            continue
        if _is_table_section_end(ln) or _is_unit_token(ln) or _is_table_header_word(ln):
            continue
        if not _looks_like_piece_description(ln):
            continue
        key = re.sub(r"\s+", " ", ln).strip().upper()
        if key in taken:
            continue
        taken.add(key)
        orphans.append(
            {
                "numero_item": len(already) + len(orphans) + 1,
                "codigo_producto": "",
                "descripcion": ln[:255],
                "cantidad": 1,
                "precio_unitario": 0.0,
                "subtotal": 0.0,
            }
        )
    return orphans


def _fill_missing_descriptions_from_orphans(
    items: list[dict[str, Any]],
    orphans: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Pega descripciones sueltas a ítems que el OCR dejó sin desc (códigos juntos)."""
    if not items or not orphans:
        return orphans
    oi = 0
    for it in items:
        if _clean_item_description(it.get("descripcion") or ""):
            continue
        if oi >= len(orphans):
            break
        it["descripcion"] = _clean_item_description(orphans[oi].get("descripcion") or "")
        oi += 1
    return orphans[oi:]


def _reject_item_line_as_documento_monto(
    monto: float | None, items: list[dict[str, Any]]
) -> float | None:
    """El OCR a menudo etiqueta un precio de línea como Neto/Total del documento."""
    if monto is None or not items:
        return monto
    for it in items:
        for key in ("precio_unitario", "subtotal"):
            val = float(it.get(key) or 0)
            if val >= 500 and abs(monto - val) <= 1:
                return None
    return monto


def _repair_merged_item_descriptions(
    rows: list[dict[str, Any]],
    prices: list[tuple[int, float, float]],
) -> list[dict[str, Any]]:
    """Si 1 ítem juntó 2 piezas y hay 2 precios, parte descripción y filas."""
    if len(rows) != 1 or len(prices) < 2:
        return rows
    phrases = _split_piece_phrases(rows[0].get("descripcion") or "")
    if len(phrases) < 2:
        return rows
    base = dict(rows[0])
    out: list[dict[str, Any]] = []
    for idx, phrase in enumerate(phrases[: len(prices)]):
        item = dict(base)
        item["numero_item"] = idx + 1
        item["descripcion"] = phrase[:255]
        if idx > 0:
            item["codigo_producto"] = ""
            item["codigo_ocr_original"] = ""
        out.append(item)
    return out


def _merge_items_rows_with_prices(
    rows: list[dict[str, Any]],
    prices: list[tuple[int, float, float]],
    neto_leido: float | None,
) -> list[dict[str, Any]]:
    if not rows:
        return rows

    rows = _repair_merged_item_descriptions(rows, prices)

    out: list[dict[str, Any]] = []
    for idx, row in enumerate(rows):
        item = dict(row)
        if idx < len(prices) and not float(item.get("precio_unitario") or 0):
            qty, precio, subtotal = prices[idx]
            item["cantidad"] = qty
            item["precio_unitario"] = precio
            item["subtotal"] = subtotal
        elif idx < len(prices):
            qty, precio, subtotal = prices[idx]
            if not float(item.get("precio_unitario") or 0):
                item["cantidad"] = qty
                item["precio_unitario"] = precio
                item["subtotal"] = subtotal
        elif prices and idx == 0:
            _, precio, _ = prices[0]
            qty = int(item.get("cantidad") or 1)
            item["precio_unitario"] = precio
            item["subtotal"] = round(precio * qty, 2)
        out.append(item)

    # Hay más precios que filas: crear ítems placeholder con esos montos.
    if len(prices) > len(out):
        for extra_i in range(len(out), len(prices)):
            qty, precio, subtotal = prices[extra_i]
            out.append(
                {
                    "numero_item": len(out) + 1,
                    "codigo_producto": "",
                    "descripcion": "",
                    "cantidad": qty,
                    "precio_unitario": precio,
                    "subtotal": subtotal,
                }
            )

    if len(out) == 1 and neto_leido and not prices:
        if not float(out[0].get("precio_unitario") or 0):
            out[0]["precio_unitario"] = neto_leido
            out[0]["subtotal"] = neto_leido

    return out


def _extract_items_table_rows(texto: str) -> list[dict[str, Any]]:
    """Filas ítem+código+descripción (varias líneas por ítem, PDF o imagen)."""
    zone = _item_table_zone(texto)
    lines = [ln.strip() for ln in zone.splitlines()]
    items: list[dict[str, Any]] = []
    i = 0
    while i < len(lines):
        ln = lines[i]

        priced = _parse_code_price_line(ln)
        if priced:
            i += 1
            more, i = _collect_following_descriptions(lines, i)
            priced["descripcion"] = _clean_item_description(" ".join(more))
            items.append(priced)
            continue

        m_one = _ITEM_ONE_LINE_RE.match(ln)
        if m_one:
            codigo = m_one.group(2).upper()
            desc = (m_one.group(3) or "").strip()
            desc = re.sub(r"\s+\$.*$", "", desc).strip()
            qty = max(int(m_one.group(4)), 1)
            precio = _parse_monto_chileno(m_one.group(5)) or 0.0
            sub = _parse_monto_chileno(m_one.group(6)) if m_one.group(6) else None
            if sub is None:
                sub = round(precio * qty, 2)
            items.append(
                {
                    "numero_item": int(m_one.group(1)),
                    "codigo_producto": codigo,
                    "descripcion": _clean_item_description(desc),
                    "cantidad": qty,
                    "precio_unitario": precio,
                    "subtotal": sub,
                }
            )
            i += 1
            continue

        m = _match_item_code_line(ln)
        if m:
            codigo = m.group(2).upper().strip()
            if _looks_like_product_code(codigo):
                desc_parts: list[str] = []
                if m.lastindex and m.lastindex >= 3 and m.group(3):
                    desc_parts.append(m.group(3).strip())
                i += 1
                more, i = _collect_following_descriptions(lines, i)
                desc_parts.extend(more)
                items.append(
                    {
                        "numero_item": int(m.group(1)),
                        "codigo_producto": codigo,
                        "descripcion": _clean_item_description(" ".join(desc_parts)),
                        "cantidad": 1,
                        "precio_unitario": 0.0,
                        "subtotal": 0.0,
                    }
                )
                continue
            i += 1
            continue

        if re.fullmatch(r"\d{1,3}", ln) and int(ln) <= 200:
            num_item = int(ln)
            if i + 1 < len(lines):
                code = _is_standalone_code_line(lines[i + 1])
                if code:
                    i += 2
                    desc_parts, i = _collect_following_descriptions(lines, i)
                    items.append(
                        {
                            "numero_item": num_item,
                            "codigo_producto": code,
                            "descripcion": _clean_item_description(" ".join(desc_parts)),
                            "cantidad": 1,
                            "precio_unitario": 0.0,
                            "subtotal": 0.0,
                        }
                    )
                    continue

        code = _is_standalone_code_line(ln)
        if code and not any(it.get("codigo_producto") == code for it in items):
            i += 1
            desc_parts, i = _collect_following_descriptions(lines, i)
            items.append(
                {
                    "numero_item": len(items) + 1,
                    "codigo_producto": code,
                    "descripcion": _clean_item_description(" ".join(desc_parts)),
                    "cantidad": 1,
                    "precio_unitario": 0.0,
                    "subtotal": 0.0,
                }
            )
            continue

        i += 1

    harvested = _harvest_missing_numbered_items(texto, items)
    if harvested:
        items.extend(harvested)
        items.sort(key=lambda it: int(it.get("numero_item") or 0))

    orphans = _extract_orphan_description_items(texto, items)
    leftover = _fill_missing_descriptions_from_orphans(items, orphans)
    if leftover:
        items.extend(leftover)
    return items


def _extract_items_columnar_single(texto: str, neto_leido: float | None) -> dict[str, Any] | None:
    """OC con columnas separadas por OCR (cantidad/precio lejos del código)."""
    lines = [ln.strip() for ln in texto.splitlines()]
    cantidad = precio = subtotal = None

    header_i = _find_cantidad_header_index(lines)
    if header_i is not None:
        data_lines: list[str] = []
        for ln in lines[header_i + 1 : header_i + 14]:
            if re.match(r"^(?:Noto|Neto|IVA|Total|Facturar|Observ)", ln, re.IGNORECASE):
                break
            if ln:
                data_lines.append(ln)

        amounts: list[float] = []
        for ln in data_lines:
            if re.fullmatch(r"\d{1,4}", ln) and cantidad is None:
                cantidad = int(ln)
                continue
            if _is_table_header_word(ln):
                continue
            amt = _parse_monto_chileno(ln)
            if amt is not None and amt >= 100:
                amounts.append(amt)

        if amounts:
            precio = amounts[0]
            if len(amounts) > 1 and cantidad == 1:
                if abs(amounts[1] - amounts[0]) / max(amounts[0], 1) <= 0.15:
                    subtotal = amounts[0]
                else:
                    subtotal = amounts[1]
            else:
                subtotal = amounts[1] if len(amounts) > 1 else None
        if cantidad is None:
            cantidad = 1

    if precio is None and neto_leido is not None:
        precio = neto_leido
        cantidad = cantidad or 1
        subtotal = neto_leido

    if precio is None:
        return None

    cantidad = cantidad or 1
    if subtotal is None or subtotal < precio * 0.5:
        subtotal = round(precio * cantidad, 2)
    if (
        neto_leido is not None
        and cantidad == 1
        and abs(precio - neto_leido) <= max(100.0, neto_leido * 0.05)
    ):
        precio = neto_leido
        subtotal = neto_leido

    codigo = _extract_codigo_producto(texto)
    desc = _extract_descripcion_tabla(texto, codigo)
    obs = _extract_observaciones(texto)
    if obs.get("vehiculo") and obs["vehiculo"].upper() not in (desc or "").upper():
        desc = f"{desc} {obs['vehiculo']}".strip() if desc else obs["vehiculo"]

    return {
        "numero_item": 1,
        "codigo_producto": codigo or "",
        "descripcion": desc,
        "marca": obs.get("marca") or "",
        "cantidad": cantidad,
        "precio_unitario": precio,
        "subtotal": subtotal,
    }


def _parse_item_line(line: str) -> dict[str, Any] | None:
    s = line.strip()
    m = _ITEM_MINIMAL_RE.match(s) or _ITEM_LINE_RE.match(s)
    if not m:
        return None
    groups = m.groups()
    if len(groups) == 5:
        num_item, codigo, cantidad, precio_raw, total_raw = groups
        desc_inline = ""
    else:
        num_item, codigo, desc_inline, cantidad, precio_raw, total_raw = groups
    precio = _parse_monto_chileno(precio_raw)
    subtotal = _parse_monto_chileno(total_raw)
    if precio is None and subtotal is not None:
        try:
            cant_i = int(cantidad)
            if cant_i > 0:
                precio = round(subtotal / cant_i, 2)
        except ValueError:
            pass
    if precio is None:
        return None
    try:
        cant_i = max(int(cantidad), 1)
    except ValueError:
        cant_i = 1
    if subtotal is None:
        subtotal = round(cant_i * precio, 2)
    desc = (desc_inline or "").strip()
    desc = _clean_item_description(desc)
    return {
        "numero_item": int(num_item),
        "codigo_producto": codigo.upper().strip(),
        "descripcion": desc,
        "cantidad": cant_i,
        "precio_unitario": precio,
        "subtotal": subtotal,
    }


def _extract_items(texto: str, neto_leido: float | None = None) -> list[dict[str, Any]]:
    line_items: list[dict[str, Any]] = []
    for line in texto.splitlines():
        parsed = _parse_item_line(line)
        if parsed:
            line_items.append(parsed)

    table_rows = _extract_items_table_rows(texto)
    items: list[dict[str, Any]] = []
    if table_rows:
        n_coded = _count_item_codes(table_rows) or len(table_rows)
        prices = _extract_items_columnar_prices(texto, n_coded)
        merged = _merge_items_rows_with_prices(table_rows, prices, neto_leido)
        # No pisar un parse con 4 códigos por una zona de tabla truncada (1 fila).
        if _count_item_codes(merged) >= _count_item_codes(line_items):
            items = _overlay_item_descriptions(merged, line_items)
        else:
            items = _overlay_item_descriptions(line_items, merged)
    else:
        items = line_items

    if not items or all(not it.get("precio_unitario") for it in items):
        col_item = _extract_items_columnar_single(texto, neto_leido)
        if col_item:
            if items and not items[0].get("precio_unitario"):
                existing_code = (items[0].get("codigo_producto") or "").strip()
                items[0].update(
                    {
                        "cantidad": col_item.get("cantidad") or items[0].get("cantidad"),
                        "precio_unitario": col_item.get("precio_unitario"),
                        "subtotal": col_item.get("subtotal"),
                        "marca": col_item.get("marca") or items[0].get("marca"),
                    }
                )
                if not existing_code and col_item.get("codigo_producto"):
                    items[0]["codigo_producto"] = col_item["codigo_producto"]
                if not (items[0].get("descripcion") or "").strip() and col_item.get("descripcion"):
                    items[0]["descripcion"] = col_item["descripcion"]
            else:
                items = [col_item]

    if items:
        obs = _extract_observaciones(texto)
        for it in items:
            if obs.get("marca") and not it.get("marca"):
                it["marca"] = obs["marca"]
            desc = _clean_item_description(it.get("descripcion") or "")
            veh = (obs.get("vehiculo") or "").strip()
            if veh and veh.upper() not in desc.upper():
                desc = f"{desc} {veh}".strip() if desc else veh
            elif len(items) == 1 and obs.get("texto"):
                for ln in obs["texto"].splitlines():
                    ln = ln.strip()
                    if re.match(r"^(MAXUS|GREAT\s*WALL|JAC|CHANGAN|HUNTER)\b", ln, re.I):
                        if ln.upper() not in desc.upper():
                            desc = f"{desc} {ln}".strip() if desc else ln
                        break
            it["descripcion"] = desc[:255]

    return items


def _extract_cliente_rut(texto: str) -> tuple[str | None, str | None]:
    """Retorna (rut_formateado, razon_social) del emisor de la OC (no RUT_PROPIO)."""
    facturar_ctx = ""
    m_fact = re.search(
        r"Facturar\s+a\s*:?\s*([\s\S]{0,400})",
        texto,
        re.IGNORECASE,
    )
    if m_fact:
        facturar_ctx = m_fact.group(1)

    razon_social: str | None = None
    if facturar_ctx:
        compact = re.sub(r"\s+", " ", facturar_ctx).strip()
        compact = re.split(r"\s*Presentar\b", compact, maxsplit=1, flags=re.IGNORECASE)[0]
        compact = re.split(r"\s*RUT\b", compact, maxsplit=1, flags=re.IGNORECASE)[0]
        compact = compact.strip(" :-\n")
        if len(compact) >= 3:
            razon_social = compact[:200]

    candidates: list[tuple[int, str]] = []
    for m in _RUT_RE.finditer(texto):
        rut_raw = m.group(1)
        rut_norm = clean_rut(rut_raw)
        if not rut_norm or rut_norm == RUT_PROPIO_NORM:
            continue
        score = 0
        start = m.start()
        ctx = texto[max(0, start - 80) : min(len(texto), m.end() + 80)].lower()
        if "facturar" in ctx:
            score += 100
        if "emisor" in ctx or "cliente" in ctx:
            score += 50
        if "despachar" in ctx:
            score -= 30
        if facturar_ctx and rut_raw in facturar_ctx:
            score += 80
        candidates.append((score, rut_raw))

    if not candidates:
        return None, razon_social

    candidates.sort(key=lambda x: x[0], reverse=True)
    return format_rut(candidates[0][1]), razon_social


def _lookup_cliente_por_rut(rut: str | None) -> tuple[int | None, str | None]:
    if not rut:
        return None, None
    rut_norm = clean_rut(rut)
    if not rut_norm:
        return None, None
    for cl in Cliente.query.filter(Cliente.activo.is_(True)).all():
        if clean_rut(cl.rut) == rut_norm:
            return cl.id, cl.nombre
    return None, None


def _load_product_catalog() -> dict[str, dict[str, Any]]:
    rows = db.session.execute(
        text(
            """
            SELECT UPPER(TRIM(CODIGO)) AS codigo,
                   COALESCE(DESCRIPCION, '') AS descripcion,
                   COALESCE(MARCA, '') AS marca
            FROM productos
            WHERE COALESCE(ACTIVO, 1) = 1
            """
        )
    ).mappings().all()
    return {
        (r["codigo"] or "").strip(): {
            "descripcion": r["descripcion"] or "",
            "marca": (r["marca"] or "").strip().upper(),
        }
        for r in rows
        if (r["codigo"] or "").strip()
    }


def _enrich_items_with_catalog(
    items: list[dict[str, Any]],
    catalogo: dict[str, dict[str, Any]],
    warnings: list[str],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for it in items:
        codigo_ocr = (it.get("codigo_producto") or "").strip().upper()
        row = dict(it)
        row["codigo_ocr_original"] = codigo_ocr
        row["en_inventario"] = False
        row["marca"] = row.get("marca") or ""

        match = fuzzy_match_catalogo_codigo(codigo_ocr, catalogo, FUZZY_THRESHOLD)
        if match:
            codigo_final = match["codigo"]
            row["codigo_producto"] = codigo_final
            row["en_inventario"] = True
            meta = match.get("meta") or {}
            if not row.get("descripcion"):
                row["descripcion"] = meta.get("descripcion") or ""
            if not row.get("marca"):
                row["marca"] = meta.get("marca") or ""
            if match["match_type"] == "fuzzy" and codigo_final != codigo_ocr:
                warnings.append(
                    f"Código corregido por similitud: {codigo_ocr} → {codigo_final}"
                )
        out.append(row)
    return out


def parse_oc_text(texto: str) -> dict[str, Any]:
    """Parsea texto OCR/nativo de una OC chilena."""
    texto = (texto or "").replace("\r\n", "\n").replace("\r", "\n")
    warnings: list[str] = []

    numero_oc = _extract_numero_oc(texto)
    fecha_oc = _parse_fecha_chilena(
        _extract_labeled_field(texto, ["Fecha O/C", "Fecha OC", "Fecha O.C."])
    )
    fecha_entrega = _parse_fecha_chilena(
        _extract_labeled_field(texto, ["Fecha Entrega", "Fecha de Entrega"])
    )
    forma_pago = _extract_forma_pago(texto)
    vendedor = _extract_labeled_field(
        texto,
        ["Vendedor", "Vendedor/a", "Ejecutivo", "Asesor comercial", "Emitido por", "Elaborado por"],
    )
    direccion = _extract_labeled_field(texto, ["Despachar a", "Despacho a", "Dirección de despacho", "Despachar A"])

    rut_cliente, razon_social = _extract_cliente_rut(texto)
    cliente_id, cliente_nombre = _lookup_cliente_por_rut(rut_cliente)
    if rut_cliente and cliente_id is None:
        warnings.append(f"Cliente no encontrado por RUT {rut_cliente}")

    totales_leidos = _extract_totales(texto)
    items = _extract_items(texto, totales_leidos.get("neto"))
    catalogo = _load_product_catalog()
    items = _enrich_items_with_catalog(items, catalogo, warnings)
    obs_info = _extract_observaciones(texto)
    observaciones = (obs_info.get("texto") or "").strip() or None

    totales_leidos["neto"] = _reject_item_line_as_documento_monto(
        totales_leidos.get("neto"), items
    )
    totales_leidos["total"] = _reject_item_line_as_documento_monto(
        totales_leidos.get("total"), items
    )
    if totales_leidos.get("iva") is not None and totales_leidos.get("neto") is None:
        totales_leidos["iva"] = None

    suma_items = round(sum(float(it.get("subtotal") or 0) for it in items), 2)
    neto_leido = totales_leidos.get("neto")
    checksum_ok = True
    if neto_leido is not None and items:
        tolerancia = max(50.0, neto_leido * 0.02)
        if abs(suma_items - neto_leido) > tolerancia:
            checksum_ok = False
            warnings.append(
                f"Suma de ítems (${suma_items:,.0f}) no cuadra con neto leído (${neto_leido:,.0f})"
            )

    return {
        "numero_oc": numero_oc,
        "fecha_oc": fecha_oc,
        "fecha_entrega": fecha_entrega,
        "forma_pago": forma_pago,
        "vendedor": vendedor,
        "direccion_despacho": direccion,
        "cliente_id": cliente_id,
        "cliente_nombre": cliente_nombre,
        "cliente_rut": rut_cliente,
        "cliente_razon_social": razon_social,
        "observaciones": observaciones,
        "vehiculo_ocr": {
            "marca": obs_info.get("marca"),
            "vehiculo": obs_info.get("vehiculo"),
            "vin": obs_info.get("vin"),
        },
        "totales_leidos": totales_leidos,
        "suma_items": suma_items,
        "checksum_ok": checksum_ok,
        "items": items,
        "warnings": warnings,
        "ocr_parser_rev": OCR_PARSER_REV,
    }


def escanear_oc(file_bytes: bytes, filename: str = "") -> dict[str, Any]:
    """Pipeline completo: archivo → texto → parseo estructurado."""
    if not file_bytes:
        raise ValueError("Archivo vacío")
    if len(file_bytes) > MAX_FILE_BYTES:
        raise ValueError("El archivo es demasiado grande (máx. 12 MB)")

    ext = _extension_from_filename(filename)
    allowed = {"jpg", "jpeg", "png", "pdf"}
    if ext not in allowed:
        raise ValueError("Formato no soportado. Use JPG, PNG o PDF.")

    cred_path = _credentials_path()
    if not cred_path.is_file():
        raise ValueError(
            f"No se encontró el archivo de credenciales: {cred_path}. "
            "Configura GOOGLE_VISION_CREDENTIALS en .env"
        )

    try:
        texto, fuente = _extract_text_from_file(file_bytes, ext, cred_path)
    except ValueError:
        raise
    except Exception as exc:
        logger.exception("Error en OCR de OC cliente")
        raise ValueError(f"Error al procesar el documento: {exc}") from exc

    if not (texto or "").strip():
        raise ValueError("No se pudo extraer texto del documento")

    resultado = parse_oc_text(texto)
    resultado["texto_fuente"] = fuente
    resultado["ocr_texto_crudo"] = texto[:8000]
    return resultado
