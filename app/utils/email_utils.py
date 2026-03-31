"""Email utility for sending ERP documents via SMTP.

Configuration (environment variables or app.config):
  MAIL_SERVER   - SMTP host, default 'smtp.gmail.com'
  MAIL_PORT     - SMTP port, default 587
  MAIL_USE_TLS  - '1' or 'true' to enable STARTTLS, default True
  MAIL_USERNAME - sender address / login
  MAIL_PASSWORD - SMTP password / app password
  MAIL_FROM     - display From address (falls back to MAIL_USERNAME)

If MAIL_USERNAME is not set, all send attempts are silently skipped and
the function returns (False, "SMTP not configured").
"""
from __future__ import annotations

import os
import smtplib
import ssl
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def _cfg(key: str, default: str = "") -> str:
    """Read config from environment (allow Flask app.config override later)."""
    return os.environ.get(key, default)


def _is_configured() -> bool:
    return bool(_cfg("MAIL_USERNAME"))


def send_document_email(
    to: str = "",
    subject: str = "",
    body_html: str = "",
    attachment_bytes: bytes | None = None,
    attachment_filename: str | None = None,
    # legacy positional kwarg alias
    to_address: str = "",
) -> tuple[bool, str]:
    """Send an email with an optional attachment.

    Returns (success: bool, message: str).
    """
    dest = to or to_address
    if not _is_configured():
        return False, "SMTP no configurado (MAIL_USERNAME no definido)"

    if not dest or "@" not in dest:
        return False, "Dirección de email inválida"

    server = _cfg("MAIL_SERVER", "smtp.gmail.com")
    port = int(_cfg("MAIL_PORT", "587"))
    use_tls = _cfg("MAIL_USE_TLS", "1").lower() not in ("0", "false", "no")
    username = _cfg("MAIL_USERNAME")
    password = _cfg("MAIL_PASSWORD")
    from_addr = _cfg("MAIL_FROM") or username

    msg = MIMEMultipart("mixed")
    msg["From"] = from_addr
    msg["To"] = dest
    msg["Subject"] = subject

    msg.attach(MIMEText(body_html, "html", "utf-8"))

    if attachment_bytes and attachment_filename:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(attachment_bytes)
        encoders.encode_base64(part)
        safe_name = attachment_filename.replace('"', "")
        part.add_header("Content-Disposition", f'attachment; filename="{safe_name}"')
        msg.attach(part)

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(server, port, timeout=15) as smtp:
            if use_tls:
                smtp.starttls(context=context)
            smtp.login(username, password)
            smtp.sendmail(from_addr, [dest], msg.as_string())
        return True, "Enviado correctamente"
    except smtplib.SMTPAuthenticationError:
        return False, "Error de autenticacion SMTP. Verifica credenciales."
    except smtplib.SMTPException as exc:
        return False, f"Error SMTP: {exc}"
    except Exception as exc:
        return False, f"Error al enviar email: {exc}"


def build_document_email_body(
    documento_tipo: str = "",
    numero: str = "",
    empresa: str = "",
    cliente: str = "",
    total: float = 0.0,
    fecha: str = "",
    items: list | None = None,
    # legacy positional aliases kept for backwards-compat
    tipo: str = "",
) -> str:
    doc_tipo = tipo or documento_tipo
    tipo_label = {
        "cotizacion": "Cotización",
        "orden_venta": "Orden de Venta",
        "orden_compra": "Orden de Compra",
        "factura": "Factura",
        "boleta": "Boleta",
        "factura_proveedor": "Factura Proveedor",
    }.get(doc_tipo, doc_tipo.replace("_", " ").title())

    items_html = ""
    if items:
        rows = "".join(
            f"<tr><td style='padding:4px 8px;border-bottom:1px solid #e2e8f0'>{i.get('codigo','')}</td>"
            f"<td style='padding:4px 8px;border-bottom:1px solid #e2e8f0'>{i.get('descripcion','')}</td>"
            f"<td style='padding:4px 8px;border-bottom:1px solid #e2e8f0;text-align:center'>{int(i.get('cantidad',0))}</td>"
            f"<td style='padding:4px 8px;border-bottom:1px solid #e2e8f0;text-align:right'>${i.get('subtotal',0):,.0f}</td></tr>"
            for i in items
        )
        items_html = f"""
        <table style="width:100%;border-collapse:collapse;margin:16px 0;font-size:13px;">
          <thead>
            <tr style="background:#f1f5f9;">
              <th style="padding:6px 8px;text-align:left;">Código</th>
              <th style="padding:6px 8px;text-align:left;">Descripción</th>
              <th style="padding:6px 8px;text-align:center;">Qty</th>
              <th style="padding:6px 8px;text-align:right;">Subtotal</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
        <p style="text-align:right;font-weight:bold;font-size:15px;">Total: ${total:,.0f}</p>
        """

    cliente_row = f"<p><strong>Cliente:</strong> {cliente}</p>" if cliente else ""
    fecha_row = f"<p><strong>Fecha:</strong> {fecha}</p>" if fecha else ""

    return f"""
    <div style="font-family: Arial, sans-serif; max-width: 640px; margin: 0 auto; padding: 24px; border: 1px solid #e2e8f0; border-radius: 8px;">
      <h2 style="color: #1d4ed8; margin-bottom: 4px;">{empresa}</h2>
      <p style="color:#64748b;margin-bottom:16px;">Sistema ERP – Documento Digital</p>
      <hr style="border:none;border-top:1px solid #e2e8f0;margin:12px 0;">
      <h3 style="color:#0f172a;">{tipo_label} N° {numero}</h3>
      {cliente_row}
      {fecha_row}
      {items_html}
      <p>Si tiene alguna consulta, responda este correo o contáctenos directamente.</p>
      <br>
      <p style="color: #64748b; font-size: 11px;">Este mensaje fue generado automáticamente por el sistema ERP de {empresa}. No responder a mensajes automáticos.</p>
    </div>
    """
