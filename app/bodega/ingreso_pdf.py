"""PDF de detalle de ingreso (lista operativa / completo con costos)."""
from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path
from typing import Any


def _txt(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _esc(value: Any) -> str:
    s = _txt(value)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _money_cl(value: Any) -> str:
    if value is None or value == "":
        return "—"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "—"
    formatted = f"{num:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"${formatted}"


def _resolve_logo_path() -> Path | None:
    static_dir = Path(__file__).resolve().parents[1] / "static"
    for name in ("logo_andes.png", "logo.png", "logo.jpg", "logo.jpeg"):
        p = static_dir / name
        if p.exists() and p.is_file():
            return p
    return None


def build_ingreso_detalle_pdf(
    *,
    doc: Any,
    items: list[Any],
    proveedor_codes: dict[int, str] | None = None,
    include_valores: bool = False,
    proveedor_rut_fmt: str = "",
) -> bytes:
    """Genera PDF A4 landscape del detalle de ingreso.

    include_valores=False → versión para bodega/etiquetado (sin V. neto).
    include_valores=True  → versión completa con costos.
    """
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        HRFlowable,
        Image,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    proveedor_codes = proveedor_codes or {}
    page_size = landscape(A4)
    buffer = io.BytesIO()

    ink = colors.HexColor("#0f172a")
    muted = colors.HexColor("#64748b")
    soft = colors.HexColor("#f8fafc")
    line = colors.HexColor("#e2e8f0")
    accent = colors.HexColor("#0f766e")
    accent_soft = colors.HexColor("#ecfdf5")
    header_bg = colors.HexColor("#0f766e")
    white = colors.white

    doc_num = _txt(getattr(doc, "numero_documento", None)) or f"ING-{getattr(doc, 'id', '')}"
    doc_id = int(getattr(doc, "id", 0) or 0)
    fecha = getattr(doc, "fecha_documento", None)
    fecha_txt = fecha.strftime("%d/%m/%Y") if fecha else "—"
    proveedor = _txt(getattr(doc, "proveedor_nombre", None)) or "—"
    rut = _txt(proveedor_rut_fmt) or _txt(getattr(doc, "proveedor_rut", None)) or "—"
    anulado = bool(getattr(doc, "anulado", False))
    observacion = _txt(getattr(doc, "observacion", None))
    metodo_pago = _txt(getattr(doc, "metodo_pago", None))

    titulo = "Detalle de ingreso" if include_valores else "Lista para etiquetar"
    badge = "CON COSTOS" if include_valores else "SIN COSTOS · BODEGA"
    subtitulo = (
        "Documento completo con valores netos de costo"
        if include_valores
        else "Listado operativo para etiquetado en bodega"
    )

    styles = getSampleStyleSheet()
    style_brand = ParagraphStyle(
        "IngBrand",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=13,
        textColor=ink,
    )
    style_doc_box = ParagraphStyle(
        "IngDocBox",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=9,
        leading=11,
        textColor=accent,
        alignment=TA_RIGHT,
    )
    style_doc_box_sub = ParagraphStyle(
        "IngDocBoxSub",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=7.5,
        leading=9.5,
        textColor=muted,
        alignment=TA_RIGHT,
    )
    style_title = ParagraphStyle(
        "IngTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=14,
        leading=17,
        textColor=ink,
    )
    style_sub = ParagraphStyle(
        "IngSub",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.2,
        leading=10.5,
        textColor=muted,
    )
    style_badge = ParagraphStyle(
        "IngBadge",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=7,
        leading=9,
        textColor=accent if not include_valores else colors.HexColor("#b45309"),
        alignment=TA_CENTER,
    )
    style_meta_lbl = ParagraphStyle(
        "IngMetaLbl",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=6.8,
        leading=8.5,
        textColor=muted,
    )
    style_meta_val = ParagraphStyle(
        "IngMetaVal",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8.4,
        leading=10.5,
        textColor=ink,
    )
    style_cell = ParagraphStyle(
        "IngCell",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=7.5,
        leading=9.4,
        textColor=colors.HexColor("#1e293b"),
    )
    style_cell_code = ParagraphStyle(
        "IngCellCode",
        parent=style_cell,
        fontName="Helvetica-Bold",
        textColor=ink,
    )
    style_cell_num = ParagraphStyle(
        "IngCellNum",
        parent=style_cell,
        alignment=TA_RIGHT,
        fontName="Helvetica-Bold",
    )
    style_th = ParagraphStyle(
        "IngTh",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=7,
        leading=9,
        textColor=white,
        alignment=TA_CENTER,
    )
    style_footer = ParagraphStyle(
        "IngFooter",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=7.2,
        leading=9,
        textColor=muted,
        alignment=TA_LEFT,
    )
    style_footer_r = ParagraphStyle(
        "IngFooterR",
        parent=style_footer,
        alignment=TA_RIGHT,
    )

    pdf = SimpleDocTemplate(
        buffer,
        pagesize=page_size,
        leftMargin=11 * mm,
        rightMargin=11 * mm,
        topMargin=10 * mm,
        bottomMargin=14 * mm,
        title=f"{titulo} · {doc_num}",
        author="Andes Auto Parts",
    )

    story: list[Any] = []

    # —— Encabezado marca + caja documento ——
    logo_path = _resolve_logo_path()
    if logo_path is not None:
        logo = Image(str(logo_path), width=22 * mm, height=14 * mm)
    else:
        logo = Spacer(22 * mm, 14 * mm)

    brand_text = Paragraph(
        "<b>ANDES AUTO PARTS</b><br/>"
        "<font size='7.2' color='#64748b'>Repuestos y gestión de bodega</font>",
        style_brand,
    )
    brand_block = Table(
        [[logo, brand_text]],
        colWidths=[24 * mm, 95 * mm],
    )
    brand_block.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )

    estado = "ANULADO" if anulado else "Vigente"
    doc_box_inner = Table(
        [
            [Paragraph(f"Documento N° {_esc(doc_num)}", style_doc_box)],
            [Paragraph(f"ID interno #{doc_id} · {estado}", style_doc_box_sub)],
        ],
        colWidths=[78 * mm],
    )
    doc_box_inner.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), soft),
                ("BOX", (0, 0), (-1, -1), 1, line),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
            ]
        )
    )

    header = Table(
        [[brand_block, doc_box_inner]],
        colWidths=[160 * mm, 90 * mm],
    )
    header.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(header)
    story.append(Spacer(1, 2.5 * mm))
    story.append(HRFlowable(width="100%", thickness=2, color=accent, spaceBefore=0, spaceAfter=3 * mm))

    # —— Título + badge ——
    badge_bg = accent_soft if not include_valores else colors.HexColor("#fffbeb")
    badge_border = accent if not include_valores else colors.HexColor("#f59e0b")
    badge_cell = Table(
        [[Paragraph(_esc(badge), style_badge)]],
        colWidths=[42 * mm],
    )
    badge_cell.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), badge_bg),
                ("BOX", (0, 0), (-1, -1), 0.8, badge_border),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ]
        )
    )
    title_row = Table(
        [
            [
                Paragraph(_esc(titulo), style_title),
                badge_cell,
            ],
            [
                Paragraph(_esc(subtitulo), style_sub),
                Paragraph("", style_sub),
            ],
        ],
        colWidths=[205 * mm, 45 * mm],
    )
    title_row.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                ("SPAN", (1, 0), (1, 1)),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ]
        )
    )
    story.append(title_row)
    story.append(Spacer(1, 3.5 * mm))

    # —— Meta en tarjetas ——
    def meta_cell(label: str, value: str) -> list[Any]:
        return [
            Paragraph(_esc(label).upper(), style_meta_lbl),
            Paragraph(_esc(value), style_meta_val),
        ]

    meta_rows = [
        [
            meta_cell("Fecha", fecha_txt),
            meta_cell("Proveedor", proveedor),
            meta_cell("RUT", rut),
            meta_cell("Estado", estado),
        ]
    ]
    if metodo_pago or observacion:
        meta_rows.append(
            [
                meta_cell("Método pago", metodo_pago or "—"),
                meta_cell("Observación", observacion or "—"),
                meta_cell("Líneas", str(len(items))),
                meta_cell("Generado", datetime.now().strftime("%d/%m/%Y %H:%M")),
            ]
        )
    else:
        meta_rows.append(
            [
                meta_cell("Líneas", str(len(items))),
                meta_cell("Generado", datetime.now().strftime("%d/%m/%Y %H:%M")),
                meta_cell("", ""),
                meta_cell("", ""),
            ]
        )

    # Flatten: each meta_cell is 2 paragraphs stacked in one table cell
    flat_meta: list[list[Any]] = []
    for row in meta_rows:
        flat_meta.append(
            [
                Table(
                    [[c[0]], [c[1]]],
                    colWidths=[60 * mm],
                )
                for c in row
            ]
        )

    meta_table = Table(flat_meta, colWidths=[62.5 * mm] * 4)
    meta_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), soft),
                ("BOX", (0, 0), (-1, -1), 1, line),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, line),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    for row in flat_meta:
        for cell in row:
            cell.setStyle(
                TableStyle(
                    [
                        ("LEFTPADDING", (0, 0), (-1, -1), 0),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                        ("TOPPADDING", (0, 0), (-1, -1), 0),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                    ]
                )
            )
    story.append(meta_table)
    story.append(Spacer(1, 4 * mm))

    # —— Tabla de ítems ——
    headers = [
        "Cód. int.",
        "Cód. prov.",
        "Descripción",
        "Marca",
        "Bodega",
        "Origen",
        "Cant.",
    ]
    if include_valores:
        headers.append("V. neto")
    headers.append("Nota")

    header_row = [Paragraph(_esc(h), style_th) for h in headers]
    data: list[list[Any]] = [header_row]

    for it in items:
        item_id = int(getattr(it, "id", 0) or 0)
        cod_int = _txt(getattr(it, "codigo_producto", None)) or "—"
        cod_prov = _txt(proveedor_codes.get(item_id)) or "—"
        desc = _txt(getattr(it, "descripcion_producto", None)) or "—"
        marca = _txt(getattr(it, "marca", None)) or "—"
        bodega = _txt(getattr(it, "bodega", None)) or "—"
        origen = _txt(getattr(it, "origen_compra", None)) or "—"
        cant = int(getattr(it, "cantidad", 0) or 0)
        nota = _txt(getattr(it, "nota", None)) or "—"

        row = [
            Paragraph(_esc(cod_int), style_cell_code),
            Paragraph(_esc(cod_prov), style_cell),
            Paragraph(_esc(desc), style_cell),
            Paragraph(_esc(marca), style_cell),
            Paragraph(_esc(bodega), style_cell),
            Paragraph(_esc(origen), style_cell),
            Paragraph(str(cant), style_cell_num),
        ]
        if include_valores:
            row.append(Paragraph(_esc(_money_cl(getattr(it, "valor_neto", None))), style_cell_num))
        row.append(Paragraph(_esc(nota), style_cell))
        data.append(row)

    if len(data) == 1:
        empty_cols = len(headers)
        data.append(
            [Paragraph("No hay ítems para mostrar.", style_cell)]
            + [Paragraph("", style_cell)] * (empty_cols - 1)
        )

    if include_valores:
        col_widths = [
            22 * mm,
            24 * mm,
            74 * mm,
            24 * mm,
            24 * mm,
            18 * mm,
            14 * mm,
            24 * mm,
            26 * mm,
        ]
    else:
        col_widths = [
            24 * mm,
            26 * mm,
            88 * mm,
            26 * mm,
            26 * mm,
            20 * mm,
            14 * mm,
            26 * mm,
        ]

    table = Table(data, colWidths=col_widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), header_bg),
                ("TEXTCOLOR", (0, 0), (-1, 0), white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 7.2),
                ("ALIGN", (0, 0), (-1, 0), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LINEBELOW", (0, 0), (-1, 0), 0, header_bg),
                ("LINEBELOW", (0, 1), (-1, -2), 0.4, line),
                ("LINEBELOW", (0, -1), (-1, -1), 0.8, accent),
                ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#0d9488")),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [white, soft]),
            ]
        )
    )
    story.append(table)
    story.append(Spacer(1, 5 * mm))

    generado = datetime.now().strftime("%d/%m/%Y %H:%M")
    foot_l = "Andes Auto Parts · Documento de ingreso"
    if not include_valores:
        foot_l += " · Sin valores de costo"
    foot_r = f"Generado {generado} · {len(items)} línea(s)"
    foot = Table(
        [
            [
                Paragraph(_esc(foot_l), style_footer),
                Paragraph(_esc(foot_r), style_footer_r),
            ]
        ],
        colWidths=[150 * mm, 100 * mm],
    )
    foot.setStyle(
        TableStyle(
            [
                ("LINEABOVE", (0, 0), (-1, -1), 0.6, line),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(foot)

    def _on_page(canvas, _doc):
        canvas.saveState()
        # Barra inferior de marca
        canvas.setFillColor(accent)
        canvas.rect(0, 0, page_size[0], 5 * mm, fill=1, stroke=0)
        canvas.setFillColor(white)
        canvas.setFont("Helvetica", 7)
        canvas.drawString(11 * mm, 1.6 * mm, "ANDES AUTO PARTS")
        canvas.drawRightString(page_size[0] - 11 * mm, 1.6 * mm, f"Pág. {_doc.page}")
        canvas.restoreState()

    pdf.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
    return buffer.getvalue()
