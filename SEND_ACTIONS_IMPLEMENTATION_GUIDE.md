# Document Send Actions - Implementation Flow

## User Interface Flow

### Email Flow
```
User View (Cotización/OV/Factura)
           |
           v
    [📧 Enviar por correo] button
    (visible when: doc_type & doc_number & party.email)
           |
           v
    Confirmation dialog: "¿Enviar a user@example.com?"
           |
           v
    Button shows "📧 Enviando..."
           |
           v
    AJAX POST /ventas/api/enviar_email/{tipo}/{numero}
           |
           v
    Backend finds document by tipo+numero
           |
           v
    Builds rich HTML email with items table
           |
           v
    Sends via SMTP (smtplib)
           |
           v
    Response: {"ok": true, "message": "Email enviado a..."}
           |
           v
    Button shows "📧 Enviado ✓" for 3 seconds
           |
           v
    Reverts to "📧 Enviar por correo"
```

### WhatsApp Flow
```
User View (Cotización/OV/Factura)
           |
           v
    [💬 WhatsApp] button
    (visible when: doc_type & doc_number & party.telefono)
           |
           v
    Button shows "💬 Abriendo..."
           |
           v
    AJAX GET /ventas/api/whatsapp/{tipo}/{numero}
           |
           v
    Backend finds document by tipo+numero
           |
           v
    Formats phone with +56 (Chile country code)
           |
           v
    Response: {"ok": true, "whatsapp_url": "https://wa.me/..."}
           |
           v
    window.open(whatsapp_url, '_blank')
           |
           v
    Opens wa.me/{phone}?text={message} in new tab
           |
           v
    User can directly send message on WhatsApp
```

## Backend API Reference

### Endpoint 1: Send Email by Document ID
```
POST /ventas/api/documento/<doc_id>/enviar_email

Request:
  - doc_id: integer (document ID from database)

Response Success:
  {
    "ok": true,
    "message": "Email enviado a cliente@example.com"
  }

Response Error:
  {
    "ok": false,
    "error": "El documento no tiene email de destinatario"
  }

HTTP Status:
  - 200: Success
  - 404: Document not found
  - 400: Missing email
  - 500: SMTP error
```

### Endpoint 2: Send Email by Type & Number
```
POST /ventas/api/enviar_email/<tipo>/<numero>

Path Parameters:
  - tipo: string (cotizacion, orden_venta, factura, etc.)
  - numero: string (case-insensitive, will be uppercased)

Response Success:
  {
    "ok": true,
    "message": "Email enviado a cliente@example.com"
  }

Response Error:
  {
    "ok": false,
    "error": "Documento no encontrado"
  }

HTTP Status:
  - 200: Success
  - 404: Document not found
  - 400: Missing parameters or email
  - 500: SMTP error
```

### Endpoint 3: Get WhatsApp Link by Type & Number
```
GET /ventas/api/whatsapp/<tipo>/<numero>

Path Parameters:
  - tipo: string (cotizacion, orden_venta, factura, etc.)
  - numero: string (case-insensitive)

Response Success:
  {
    "ok": true,
    "whatsapp_url": "https://wa.me/56912345678?text=Hola...",
    "phone": "+56912345678",
    "message": "Hola, le enviamos su Cotización N° 001..."
  }

Response Error:
  {
    "ok": false,
    "error": "El documento no tiene teléfono del cliente"
  }

HTTP Status:
  - 200: Success with link
  - 404: Document not found
  - 400: Missing phone
```

## Template Variable Reference

### Variables in Document Forms

```
Available in all views (cotización, orden_venta, factura):

{
  "doc_type": "cotizacion|orden_venta|factura|...",
  "doc_number": "001",
  "doc_date": "2024-03-23",
  "party": {
    "name": "Cliente ABC",
    "email": "cliente@example.com",
    "telefono": "+56912345678",
    "rut": "12.345.678-9",
    "address": "Av. Principal 123",
    "region": "Metropolitana",
    "ciudad": "Santiago"
  },
  "doc_id": 42,  # NULL for new documents
  "estado_pago": "pendiente|pagado",
  "metodo_pago": "efectivo|transferencia|...",
  ... other fields ...
}
```

## Conditional Rendering Logic

### Email Button Condition
```jinja2
{% if doc_type and doc_number %}
  {% if party %}
    {% if party.email %}
      <!-- Button shows -->
    {% endif %}
  {% endif %}
{% endif %}
```

### WhatsApp Button Condition
```jinja2
{% if doc_type and doc_number %}
  {% if party %}
    {% if party.telefono %}
      <!-- Button shows -->
    {% endif %}
  {% endif %}
{% endif %}
```

## Files Modified

1. **app/templates/ventas/_erp_footer.html**
   - Updated button visibility conditions
   - Uses `party.email` and `party.telefono` instead of passed variables

2. **app/templates/ventas/_erp_scripts.html**
   - Moved handlers outside conditional block
   - Extracts DOC_NUMERO and DOC_TIPO from form elements
   - Calls new tipo/numero endpoints

3. **app/ventas/routes.py**
   - Added `api_enviar_email(tipo, numero)` function
   - Added `api_whatsapp(tipo, numero)` function
   - Both use `_load_document_by_number()` to find documents

## Email Configuration

Set these environment variables to enable email sending:

```bash
MAIL_SERVER=smtp.gmail.com          # SMTP server
MAIL_PORT=587                       # SMTP port (usually 587 for TLS)
MAIL_USE_TLS=1                      # Enable TLS encryption
MAIL_USERNAME=your@gmail.com        # Gmail account
MAIL_PASSWORD=your_app_password     # Gmail app password
MAIL_FROM=your@gmail.com            # From address (optional)
```

For testing without email:
```bash
# Leave MAIL_USERNAME empty/unset
# System will silently skip sending and return:
# {"ok": false, "error": "SMTP no configurado"}
```

## Testing the Implementation

```bash
# 1. Verify routes are registered
python test_routes.py

# 2. Verify UI elements
python test_ui_buttons.py

# 3. Start Flask dev server
python run.py

# 4. Navigate to:
# http://localhost:5000/ventas/cotizacion
# http://localhost:5000/ventas/orden_venta
# http://localhost:5000/ventas/factura

# 5. Create a document with:
# - Client name
# - Client email
# - Client phone number
# - At least one line item

# 6. Verify buttons appear:
# - [📧 Enviar por correo]
# - [💬 WhatsApp]

# 7. Test email button:
# - Click button
# - Confirm dialog
# - Wait for response
# - Should see "✓ Enviado"

# 8. Test WhatsApp button:
# - Click button
# - New window should open with wa.me link
# - Pre-filled with document reference message
```

## Troubleshooting

### Buttons not showing
- Check browser console for JS errors
- Verify `party.email` and `party.telefono` have values
- Ensure `doc_type` and `doc_number` are set

### Email not sending
- Check MAIL_USERNAME is set (otherwise silently skips)
- Verify SMTP credentials are correct
- Check email address format is valid
- For Gmail: use 16-character app password, not account password

### WhatsApp link not opening
- Check phone number is in valid format
- System should convert "9 1234 5678" → "+56912345678"
- Try opening link manually: https://wa.me/56912345678?text=test

### Client info missing
- Verify client is selected when creating document
- Check that client record has email/phone filled in
- Edit client to add missing contact info
