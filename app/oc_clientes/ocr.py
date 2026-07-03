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

OCR_PARSER_REV = "oc-cliente-v2"
RUT_PROPIO = "78074288-7"
RUT_PROPIO_NORM = clean_rut(RUT_PROPIO)

MAX_FILE_BYTES = 12 * 1024 * 1024
MIN_PDF_NATIVE_CHARS = 200
VISION_SCOPES = ["https://www.googleapis.com/auth/cloud-vision"]
FUZZY_THRESHOLD = 92

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
    return bool(re.search(r"\d", c) or "ERP" in c or "OEM" in c)


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


def _vision_ocr_text(image_bytes: bytes, cred_path: Path) -> str:
    credentials = service_account.Credentials.from_service_account_file(
        str(cred_path),
        scopes=VISION_SCOPES,
    )
    client = vision.ImageAnnotatorClient(credentials=credentials)
    processed = _preprocess_image_for_ocr(image_bytes)
    image = vision.Image(content=processed)
    image_context = vision.ImageContext(language_hints=["es"])

    try:
        response = client.document_text_detection(
            image=image, image_context=image_context
        )
    except Exception as exc:
        raise ValueError("No se pudo contactar a Google Cloud Vision") from exc

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
            pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False)
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
        native = _extract_pdf_native_text(file_bytes)
        if len(native) >= MIN_PDF_NATIVE_CHARS:
            return native, "pdf_text"
        png = _convert_pdf_first_page_to_png(file_bytes)
        return _vision_ocr_text(png, cred_path), "vision_pdf"
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
    m = re.search(r"Forma\s+de\s+Pago\s*:?\s*(.*?)(?:\n|$)", texto, re.IGNORECASE)
    if not m:
        return None
    val = m.group(1).strip()
    if not val or _is_table_header_word(val):
        return None
    return val[:100]


def _extract_totales(texto: str) -> dict[str, float | None]:
    neto = iva = total = None
    lines = [ln.strip() for ln in texto.splitlines()]
    for i, ln in enumerate(lines):
        if re.match(r"^(?:Neto|Noto)\b", ln, re.IGNORECASE):
            inline = re.search(r"([\d.,+]+)\s*$", ln)
            if inline:
                neto = _parse_monto_chileno(inline.group(1))
            if neto is None:
                for nxt in lines[i + 1 : i + 4]:
                    parsed = _parse_monto_chileno(nxt)
                    if parsed is not None and parsed >= 500:
                        neto = parsed
                        break
            break
    if neto is None:
        m = re.search(
            r"(?:Neto|Noto)\s*\$?\s*:?\s*([\d.,+]+)",
            texto,
            re.IGNORECASE,
        )
        if m:
            neto = _parse_monto_chileno(m.group(1))
    m = re.search(r"IVA\s*(?:\(19%\)|19\s*%)?\s*:?\s*\$?\s*([\d.,+]+)", texto, re.IGNORECASE)
    if m:
        iva = _parse_monto_chileno(m.group(1))
    m = re.search(r"Total\s*\$?\s*:?\s*([\d.,+]+)", texto, re.IGNORECASE)
    if m:
        total = _parse_monto_chileno(m.group(1))
    if total is None:
        m = re.search(r"TotalS?\s*\n\s*([\d.,+]+)", texto, re.IGNORECASE)
        if m:
            total = _parse_monto_chileno(m.group(1))
    return {"neto": neto, "iva": iva, "total": total}


def _is_description_line(line: str) -> bool:
    s = line.strip()
    if not s or len(s) < 3:
        return False
    if _ITEM_MINIMAL_RE.match(s) or _ITEM_LINE_RE.match(s) or _ITEM_CODE_LINE_RE.match(s):
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
    m = re.search(
        r"Observaciones\s*\n([\s\S]*?)(?:Maneda|Moneda|Unidad|Fecha\s*O/?C|Cantidad|Neto|Noto|IVA\b)",
        texto,
        re.IGNORECASE,
    )
    obs = (m.group(1) if m else "").strip()
    marca = None
    vehiculo = None
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
    return {"texto": obs, "marca": marca, "vehiculo": vehiculo}


def _extract_items_columnar(texto: str, neto_leido: float | None) -> dict[str, Any] | None:
    """OC con columnas separadas por OCR (cantidad/precio lejos del código)."""
    lines = [ln.strip() for ln in texto.splitlines()]
    cantidad = precio = subtotal = None

    header_i = None
    for i, ln in enumerate(lines):
        if re.fullmatch(r"Cantidad", ln, re.IGNORECASE):
            header_i = i
            break
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
    if neto_leido is not None and abs(subtotal - neto_leido) > abs(precio - neto_leido):
        subtotal = neto_leido
        if cantidad == 1:
            precio = neto_leido

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
    if desc and re.fullmatch(r"[\d.,]+", desc):
        desc = ""
    return {
        "numero_item": int(num_item),
        "codigo_producto": codigo.upper().strip(),
        "descripcion": desc,
        "cantidad": cant_i,
        "precio_unitario": precio,
        "subtotal": subtotal,
    }


def _extract_items(texto: str, neto_leido: float | None = None) -> list[dict[str, Any]]:
    lines = [ln.rstrip() for ln in texto.splitlines()]
    items: list[dict[str, Any]] = []
    pending_desc_for: int | None = None

    for line in lines:
        parsed = _parse_item_line(line)
        if parsed:
            items.append(parsed)
            pending_desc_for = len(items) - 1 if not parsed.get("descripcion") else None
            continue
        m_code = _ITEM_CODE_LINE_RE.match(line.strip())
        if m_code and not items:
            items.append(
                {
                    "numero_item": int(m_code.group(1)),
                    "codigo_producto": m_code.group(2).upper(),
                    "descripcion": "",
                    "cantidad": 1,
                    "precio_unitario": 0.0,
                    "subtotal": 0.0,
                }
            )
            pending_desc_for = 0
            continue
        if pending_desc_for is not None:
            if re.fullmatch(r"(SAN|UND|UN|UNIDAD|\$|S|KIT|SET)", line.strip(), re.I):
                pending_desc_for = None
                continue
            if re.match(r"^(facturar|presentar|observ|fecha|forma|rut|cantidad|neto|noto|iva|total)", line.strip(), re.I):
                pending_desc_for = None
                continue
        if pending_desc_for is not None and _is_description_line(line):
            prev = items[pending_desc_for].get("descripcion") or ""
            new_desc = f"{prev} {line.strip()}".strip() if prev else line.strip()
            items[pending_desc_for]["descripcion"] = new_desc[:255]
            if not re.search(r"^(SAN|UND|UN|UNIDAD|KIT|SET)$", line.strip(), re.I):
                pending_desc_for = None

    if not items or all(not it.get("precio_unitario") for it in items):
        col_item = _extract_items_columnar(texto, neto_leido)
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
            desc = (it.get("descripcion") or "").strip()
            veh = (obs.get("vehiculo") or "").strip()
            if veh and veh.upper() not in desc.upper():
                it["descripcion"] = f"{desc} {veh}".strip()[:255]
            it["descripcion"] = re.sub(r"\s+", " ", it.get("descripcion") or "").strip()[:255]

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
    direccion = _extract_labeled_field(texto, ["Despachar a", "Despacho a", "Dirección de despacho", "Despachar A"])

    rut_cliente, razon_social = _extract_cliente_rut(texto)
    cliente_id, cliente_nombre = _lookup_cliente_por_rut(rut_cliente)
    if rut_cliente and cliente_id is None:
        warnings.append(f"Cliente no encontrado por RUT {rut_cliente}")

    totales_leidos = _extract_totales(texto)
    items = _extract_items(texto, totales_leidos.get("neto"))
    catalogo = _load_product_catalog()
    items = _enrich_items_with_catalog(items, catalogo, warnings)

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
        "direccion_despacho": direccion,
        "cliente_id": cliente_id,
        "cliente_nombre": cliente_nombre,
        "cliente_rut": rut_cliente,
        "cliente_razon_social": razon_social,
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
