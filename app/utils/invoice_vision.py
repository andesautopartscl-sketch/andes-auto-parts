"""Análisis de facturas chilenas vía Google Cloud Vision OCR."""
from __future__ import annotations

import base64
import os
import re
from pathlib import Path
from typing import Any

from google.cloud import vision
from google.oauth2 import service_account

MAX_IMAGE_BYTES = 12 * 1024 * 1024
VISION_SCOPES = ["https://www.googleapis.com/auth/cloud-vision"]

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


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


def parsear_factura_chilena(texto: str) -> dict[str, Any]:
    """Extrae campos habituales de facturas chilenas desde texto OCR."""
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

    rut_match = re.search(
        r"\b(\d{1,2}\.?\d{3}\.?\d{3}-[\dkK]|\d{7,8}-[\dkK])\b",
        texto,
    )
    if rut_match:
        resultado["rut_proveedor"] = rut_match.group(1)

    doc_match = re.search(
        r"(?:N[°º]\s*|Nro\.?\s*|Folio\s*|Factura\s*|Boleta\s*|Gu[ií]a\s*)"
        r":?\s*(\d{3,12})",
        texto,
        re.IGNORECASE,
    )
    if doc_match:
        resultado["numero_documento"] = doc_match.group(1)

    fecha_match = re.search(r"\b(\d{2})[/-](\d{2})[/-](\d{4})\b", texto)
    if fecha_match:
        resultado["fecha"] = (
            f"{fecha_match.group(1)}-{fecha_match.group(2)}-{fecha_match.group(3)}"
        )

    texto_lower = texto.lower()
    if re.search(r"\bcr[eé]dito\b", texto_lower):
        resultado["metodo_pago"] = "credito"
    elif re.search(r"\btransferencia\b", texto_lower):
        resultado["metodo_pago"] = "transferencia"
    elif re.search(r"\bcheque\b", texto_lower):
        resultado["metodo_pago"] = "cheque"
    elif re.search(r"\bcontado\b|\befectivo\b", texto_lower):
        resultado["metodo_pago"] = "contado"

    total_match = re.search(
        r"(?:^|\n)\s*Total\s*:?\s*\$?\s*([\d.,]+)",
        texto,
        re.IGNORECASE | re.MULTILINE,
    )
    if not total_match:
        total_match = re.search(
            r"Total\s*(?:a\s+pagar|general)?\s*:?\s*\$?\s*([\d.,]+)",
            texto,
            re.IGNORECASE,
        )
    if total_match:
        resultado["total"] = _parse_monto_chileno(total_match.group(1))

    neto_match = re.search(
        r"(?:Monto\s+)?Neto\s*:?\s*\$?\s*([\d.,]+)",
        texto,
        re.IGNORECASE,
    )
    if neto_match:
        resultado["total_neto"] = _parse_monto_chileno(neto_match.group(1))

    iva_match = re.search(
        r"IVA\s*(?:\(\s*19\s*%\s*\))?\s*:?\s*\$?\s*([\d.,]+)",
        texto,
        re.IGNORECASE,
    )
    if iva_match:
        resultado["iva"] = _parse_monto_chileno(iva_match.group(1))

    return resultado


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
    allowed = {"image/jpeg", "image/png", "image/webp"}
    if mt == "application/pdf":
        raise ValueError(
            "Google Vision OCR requiere imagen (JPG, PNG o WEBP). "
            "Suba una foto o captura del PDF."
        )
    if mt not in allowed:
        raise ValueError("Formato no soportado. Use JPG, PNG o WEBP.")

    try:
        image_bytes = base64.b64decode(b64, validate=True)
    except Exception as exc:
        raise ValueError("Base64 inválido") from exc
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ValueError("El archivo es demasiado grande (máx. 12 MB)")

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

    texto = ""
    if response.full_text_annotation and response.full_text_annotation.text:
        texto = response.full_text_annotation.text
    elif response.text_annotations:
        texto = response.text_annotations[0].description or ""

    if not texto.strip():
        raise ValueError("No se detectó texto en la imagen. Pruebe con mejor iluminación.")

    return parsear_factura_chilena(texto)


def analyze_chilean_invoice(base64_data: str, media_type: str) -> dict[str, Any]:
    """Alias usado por rutas existentes."""
    return analizar_factura(base64_data, media_type)
