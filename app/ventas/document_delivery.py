from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from pathlib import Path

from flask import current_app
from app.utils.rut_utils import format_rut


PUBLIC_DOCS_DIR = Path(__file__).resolve().parents[2] / "data" / "public_docs"


def _secret_key() -> bytes:
    key = (current_app.secret_key or "andes-default-secret").encode("utf-8")
    return key


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(raw: str) -> bytes:
    pad = "=" * ((4 - len(raw) % 4) % 4)
    return base64.urlsafe_b64decode((raw + pad).encode("ascii"))


def _safe_slug(text: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", text or "")
    return value.strip("_") or "documento"


def build_public_pdf_token(doc_id: int, ttl_seconds: int = 60 * 60 * 24 * 7) -> str:
    payload = {
        "doc_id": int(doc_id),
        "exp": int(time.time()) + int(ttl_seconds),
    }
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(_secret_key(), payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"


def read_public_pdf_token(token: str) -> int | None:
    try:
        payload_b64, sig = token.split(".", 1)
        expected = hmac.new(_secret_key(), payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None

        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        exp = int(payload.get("exp") or 0)
        if exp < int(time.time()):
            return None

        doc_id = int(payload.get("doc_id") or 0)
        return doc_id if doc_id > 0 else None
    except Exception:
        return None


def document_pdf_path(doc_tipo: str, doc_numero: str, doc_id: int) -> Path:
    PUBLIC_DOCS_DIR.mkdir(parents=True, exist_ok=True)
    filename = _safe_slug(f"{doc_tipo}_{doc_numero}_{doc_id}.pdf")
    return PUBLIC_DOCS_DIR / filename


def _escape_pdf_text(value: str) -> str:
    return (value or "").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _build_minimal_pdf(lines: list[str]) -> bytes:
    """Build a tiny valid PDF (Helvetica text only) without external deps."""
    stream_parts = ["BT", "/F1 11 Tf", "50 800 Td", "14 TL"]
    for line in lines:
        txt = _escape_pdf_text((line or "").encode("latin-1", "replace").decode("latin-1"))
        stream_parts.append(f"({txt}) Tj")
        stream_parts.append("T*")
    stream_parts.append("ET")
    stream = "\n".join(stream_parts).encode("latin-1", "replace")

    objects = [
        b"1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj\n",
        b"2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj\n",
        b"3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>endobj\n",
        b"4 0 obj<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>endobj\n",
        b"5 0 obj<< /Length " + str(len(stream)).encode("ascii") + b" >>stream\n" + stream + b"\nendstream\nendobj\n",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(len(out))
        out.extend(obj)

    xref_pos = len(out)
    out.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode("ascii"))

    out.extend(
        (
            f"trailer<< /Size {len(offsets)} /Root 1 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(out)


def _doc_title(doc_tipo: str) -> str:
    return {
        "cotizacion": "COTIZACION",
        "orden_venta": "ORDEN DE VENTA",
        "orden_compra": "ORDEN DE COMPRA",
        "factura": "FACTURA",
        "boleta": "BOLETA",
        "factura_proveedor": "FACTURA PROVEEDOR",
    }.get((doc_tipo or "").strip().lower(), (doc_tipo or "DOCUMENTO").replace("_", " ").upper())


def _fmt_currency_clp(value: float | int | None) -> str:
    n = float(value or 0)
    return "$ " + f"{int(round(n)):,}".replace(",", ".")


def _fmt_rut(raw: str | None) -> str:
    try:
        return format_rut(raw) or ""
    except Exception:
        return raw or ""


def _split_text(text: str, max_len: int) -> list[str]:
    src = (text or "").strip()
    if not src:
        return []
    words = src.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = (current + " " + word).strip()
        if len(candidate) <= max_len:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
    if current:
        lines.append(current)
    return lines


def _resolve_logo_path() -> Path | None:
    static_dir = Path(__file__).resolve().parents[1] / "static"
    for name in ("logo.png", "logo_andes.png", "logo.jpg", "logo.jpeg"):
        p = static_dir / name
        if p.exists() and p.is_file():
            return p
    return None


def render_document_pdf(doc, company: dict) -> Path:
    """Generate/overwrite the PDF file for a sales document and return its path.

    Primary layout uses reportlab.platypus (Table/Paragraph styles) to produce
    a professional structured business document.
    """
    doc_tipo = (getattr(doc, "tipo", "") or "documento").strip().lower()
    doc_numero = (getattr(doc, "numero", "") or str(getattr(doc, "id", 0))).strip()
    doc_id = int(getattr(doc, "id", 0))
    pdf_path = document_pdf_path(doc_tipo, doc_numero, doc_id)

    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

        styles = getSampleStyleSheet()
        style_body = ParagraphStyle(
            "Body",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=8.6,
            leading=10.8,
            textColor=colors.HexColor("#1E293B"),
        )
        style_label = ParagraphStyle(
            "Label",
            parent=styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=7.8,
            leading=10,
            textColor=colors.HexColor("#475569"),
        )
        style_title = ParagraphStyle(
            "DocTitle",
            parent=styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=12.8,
            leading=16,
            alignment=2,
            textColor=colors.HexColor("#7A1A18"),
        )
        style_footer_msg = ParagraphStyle(
            "FooterMsg",
            parent=styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=9.2,
            leading=12,
            textColor=colors.HexColor("#0F2A5C"),
        )

        title = _doc_title(doc_tipo)
        number_label = f"N° {doc_numero or doc_id}"
        issue_date = getattr(doc, "fecha_documento", None)
        issue_date_text = issue_date.strftime("%d/%m/%Y") if issue_date else ""

        company_name = company.get("name", "ANDES AUTO PARTS LTDA")
        company_rut = _fmt_rut(company.get("rut", "78.074.288-7")) or "78.074.288-7"
        company_addr = company.get("address", "LA CONCEPCION 81 OFICINA 214, PROVIDENCIA")
        company_business = company.get("business", "VENTA DE PARTES, PIEZAS Y ACCESORIOS AUTOMOTRICES")
        company_email = company.get("email", "andesautopartscl@gmail.com")
        company_phone = company.get("phone", "+56 9 2615 2826")

        party_name = (getattr(doc, "cliente_nombre", "") or "")
        party_rut = _fmt_rut(getattr(doc, "cliente_rut", "") or "")
        party_giro = (getattr(doc, "cliente_giro", "") or "")
        party_address = (getattr(doc, "cliente_direccion", "") or "")
        party_region = (getattr(doc, "cliente_region", "") or "")
        party_city = (getattr(doc, "cliente_ciudad", "") or "")
        party_phone = (getattr(doc, "cliente_telefono", "") or "")
        party_email = (getattr(doc, "cliente_email", "") or "")
        vendor = (getattr(doc, "usuario", "") or "")
        payment_method = (getattr(doc, "metodo_pago", "") or "").replace("_", " ").title() or "No especificada"
        notes = (getattr(doc, "observacion", "") or "").strip() or "Sin observaciones."

        subtotal = float(getattr(doc, "subtotal", 0) or 0)
        iva = float(getattr(doc, "impuesto", 0) or 0)
        total = float(getattr(doc, "total", 0) or 0)

        doc_template = SimpleDocTemplate(
            str(pdf_path),
            pagesize=A4,
            leftMargin=14 * mm,
            rightMargin=14 * mm,
            topMargin=12 * mm,
            bottomMargin=12 * mm,
            title=f"{title} {number_label}",
            author=company_name,
        )
        story = []

        # Header: Professional SII-style 2-column layout  
        # LEFT: Logo + Company info  |  RIGHT: Document box (bordered)
        logo_path = _resolve_logo_path() or (Path(__file__).resolve().parents[1] / "static" / "logo.png")
        if logo_path.exists() and logo_path.is_file():
            logo_img = Image(str(logo_path), width=24 * mm, height=16 * mm)
        else:
            logo_img = Spacer(24 * mm, 16 * mm)

        company_text = Paragraph(
            f"<b>{company_name}</b><br/>"
            f"<font size=7>RUT: {company_rut}</font><br/>"
            f"<font size=7>{company_business}</font><br/>"
            f"<font size=7>{company_addr}</font><br/>"
            f"<font size=7>{company_email} · {company_phone}</font>",
            style_body,
        )

        # Left column: logo + company details
        left_column = Table(
            [[logo_img, company_text]],
            colWidths=[26 * mm, 108 * mm],
            hAlign="LEFT",
        )
        left_column.setStyle(
            TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (0, -1), 0),
                ("RIGHTPADDING", (0, 0), (0, -1), 4),
                ("LEFTPADDING", (1, 0), (1, -1), 4),
                ("RIGHTPADDING", (1, 0), (-1, -1), 0),
            ])
        )

        # Right column: bordered document box
        doc_content = Table(
            [
                [Paragraph(f"<font size=14><b>{title}</b></font>", 
                           ParagraphStyle("DocTitle2", parent=styles["Normal"], 
                                        fontName="Helvetica-Bold", fontSize=14, 
                                        alignment=1, textColor=colors.HexColor("#DC2626")))],
                [Paragraph(f"<font size=11><b>{number_label}</b></font>", 
                           ParagraphStyle("DocNum", parent=styles["Normal"], 
                                        fontName="Helvetica-Bold", fontSize=11, 
                                        alignment=1, textColor=colors.HexColor("#0F172A")))],
                [Spacer(1, 3 * mm)],
                [Paragraph(f"<font size=8><b>RUT:</b> {company_rut}<br/><b>Fecha:</b> {issue_date_text}</font>", 
                           ParagraphStyle("DocMeta", parent=styles["Normal"], 
                                        fontName="Helvetica", fontSize=8, 
                                        alignment=1, textColor=colors.HexColor("#475569")))],
            ],
            colWidths=[46 * mm],
            hAlign="CENTER",
        )
        doc_content.setStyle(
            TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ])
        )

        doc_box_bordered = Table(
            [[doc_content]],
            colWidths=[46 * mm],
        )
        doc_box_bordered.setStyle(
            TableStyle([
                ("BOX", (0, 0), (-1, -1), 1.5, colors.HexColor("#DC2626")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ])
        )

        #Main header table: left company + right doc box
        header_table = Table(
            [[left_column, Spacer(2 * mm, 1), doc_box_bordered]],
            colWidths=[134 * mm, 2 * mm, 46 * mm],
            hAlign="LEFT",
        )
        header_table.setStyle(
            TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ])
        )
        story.append(header_table)
        story.append(Spacer(1, 6 * mm))

        # Client section
        left_client = Paragraph(
            f"<b>Razon Social:</b> {party_name or '-'}<br/>"
            f"<b>RUT:</b> {party_rut or '-'}<br/>"
            f"<b>Giro:</b> {party_giro or '-'}<br/>"
            f"<b>Direccion:</b> {party_address or '-'}<br/>"
            f"<b>Comuna/Ciudad:</b> {(party_city or '-') + (' / ' + party_region if party_region else '')}",
            style_body,
        )
        right_client = Paragraph(
            f"<b>Contacto:</b> {(party_phone or '-') + (' · ' + party_email if party_email else '')}<br/>"
            f"<b>Vendedor:</b> {vendor if vendor else '-'}<br/>"
            f"<b>Fecha de Emision:</b> {issue_date_text}<br/>"
            f"<b>Forma de pago:</b> {payment_method}",
            style_body,
        )
        client_table = Table([[left_client, right_client]], colWidths=[90 * mm, 90 * mm], hAlign="LEFT")
        client_table.setStyle(
            TableStyle(
                [
                    ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#94A3B8")),
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 7),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ]
            )
        )
        story.append(client_table)
        story.append(Spacer(1, 5 * mm))

        # Detail table (MANDATORY Table + TableStyle)
        detail_data = [["Codigo", "Descripcion", "Marca", "Cantidad", "Precio Unitario", "Subtotal"]]
        items = list(getattr(doc, "items", []) or [])
        if not items:
            detail_data.append(["-", "Sin items", "-", "0", _fmt_currency_clp(0), _fmt_currency_clp(0)])
        else:
            for item in items:
                detail_data.append(
                    [
                        (getattr(item, "codigo_producto", "") or "").upper(),
                        (getattr(item, "descripcion", "") or ""),
                        (getattr(item, "marca", "") or "").upper(),
                        str(int(getattr(item, "cantidad", 0) or 0)),
                        _fmt_currency_clp(float(getattr(item, "precio_unitario", 0) or 0)),
                        _fmt_currency_clp(float(getattr(item, "subtotal", 0) or 0)),
                    ]
                )

        detail_table = Table(
            detail_data,
            colWidths=[21 * mm, 72 * mm, 24 * mm, 16 * mm, 24 * mm, 23 * mm],
            repeatRows=1,
            hAlign="LEFT",
        )
        detail_table.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.55, colors.HexColor("#94A3B8")),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E2E8F0")),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, 0), 8.2),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#111827")),
                    ("FONTSIZE", (0, 1), (-1, -1), 8.3),
                    ("ALIGN", (0, 0), (2, -1), "LEFT"),
                    ("ALIGN", (3, 0), (5, -1), "RIGHT"),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 5),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        story.append(detail_table)
        story.append(Spacer(1, 4 * mm))

        # Totals (right side)
        totals_data = [
            ["Subtotal", _fmt_currency_clp(subtotal)],
            ["IVA (19%)", _fmt_currency_clp(iva)],
            ["TOTAL", _fmt_currency_clp(total)],
        ]
        totals_table = Table(totals_data, colWidths=[32 * mm, 36 * mm], hAlign="RIGHT")
        totals_table.setStyle(
            TableStyle(
                [
                    ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#94A3B8")),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
                    ("BACKGROUND", (0, 2), (-1, 2), colors.HexColor("#DBEAFE")),
                    ("ALIGN", (0, 0), (0, -1), "LEFT"),
                    ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                    ("FONTNAME", (0, 0), (-1, 1), "Helvetica"),
                    ("FONTNAME", (0, 2), (-1, 2), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, 1), 9),
                    ("FONTSIZE", (0, 2), (-1, 2), 11.5),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        totals_wrap = Table([["", totals_table]], colWidths=[112 * mm, 68 * mm], hAlign="LEFT")
        totals_wrap.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
        story.append(totals_wrap)
        story.append(Spacer(1, 5 * mm))

        # Footer: observations + payment + gratitude.
        obs_title = Paragraph("<b>OBSERVACIONES</b>", style_label)
        obs_text = Paragraph(notes, style_body)
        pay_title = Paragraph("<b>FORMA DE PAGO</b>", style_label)
        pay_text = Paragraph(payment_method, style_body)
        obs_table = Table(
            [[obs_title, pay_title], [obs_text, pay_text]],
            colWidths=[130 * mm, 50 * mm],
            hAlign="LEFT",
        )
        obs_table.setStyle(
            TableStyle(
                [
                    ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#94A3B8")),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F8FAFC")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        story.append(obs_table)
        story.append(Spacer(1, 4 * mm))
        story.append(Paragraph("Gracias por su preferencia", style_footer_msg))

        doc_template.build(story)
        current_app.logger.info("PDF built with platypus table layout at %s", str(pdf_path))
    except Exception as exc:
        current_app.logger.exception("PDF generation failed with platypus for doc_id=%s: %s", doc_id, exc)
        raise

    return pdf_path
