# Real-Time Stock Control System Documentation

## Overview

The ERP system now includes a comprehensive real-time stock control system with:

1. **Real-Time Stock Validation** - Prevent overselling with instant availability checks
2. **Automatic Stock Deduction** - Remove stock from inventory when sales are confirmed
3. **Credit Note System** - Full product return management with automatic stock restoration
4. **Product Traceability** - Complete history of all stock movements and sales

## Features

### 1. Real-Time Stock Validation on Sales

When creating a sales order (Orden de Venta), the system:
- Validates stock availability before allowing document confirmation
- Shows clear error messages if insufficient stock
- Checks variants by brand and warehouse
- Prevents double-deduction with session tracking

**Files Modified:**
- `app/ventas/routes.py` - Added stock check endpoints
- `app/templates/ventas/documento.html` - Added form-level validation

### 2. Automatic Stock Deduction

When a sales order is marked as "entregada" (delivered):
- Stock is automatically deducted from the correct warehouse/variant
- A movement record (type: "salida") is created for audit trail
- Session tracking prevents double-processing
- All warehouse totals are updated

**Implementation:**
```python
# In ventas/routes.py
_discount_stock_for_sale(items, doc_number)  # Called on form submission
```

### 3. Credit Notes (Product Returns)

Customers can create credit notes from existing sales documents:
- Link to original invoice/order
- Select specific products to return
- Automatic stock restoration to original warehouse/variant
- Movement record created (type: "ingreso", reason: credit note)

**API Endpoint:**
```
POST /ventas/api/credit-note
{
    "documento_id": 123,
    "items": [
        {
            "codigo_producto": "PRD001",
            "marca": "GENÉRICA",
            "bodega": "Bodega 1",
            "cantidad": 2,
            "precio_unitario": 50.00
        }
    ],
    "razon": "Producto defectuoso"
}
```

### 4. Product Traceability (Full History)

Click the arrow icon (↗) next to any product to view:
- **Stock Entrada (Ingresos)** - All prior stock receipts from suppliers
  - Supplier name and RUT
  - Invoice/document number
  - Quantity and variant
  - Date and time
- **Ventas** - All sales transactions
  - Customer name and RUT
  - Document type and number  
  - Quantity and price
  - Date and time
- **Devoluciones** - All credit notes/returns
  - Original document linked
  - Reason for return
  - Quantity returned
- **Stock Summary** - Current inventory by warehouse/variant

**API Endpoint:**
```
GET /ventas/api/product/history/<codigo_producto>
```

Returns:
```json
{
  "codigo_producto": "PRD001",
  "ingresos": [...],
  "ventas": [...],
  "notas_credito": [...],
  "stock_summary": {
    "Bodega 1 (GENÉRICA)": {
      "bodega": "Bodega 1",
      "marca": "GENÉRICA",
      "stock": 45
    }
  },
  "last_sale": {
    "cliente": "Cliente Nombre",
    "cliente_rut": "12.345.678-9",
    "cantidad": 5,
    "fecha": "2026-03-21T14:30:00"
  }
}
```

## Database Schema

### New Tables

#### ventas_documentos
```sql
CREATE TABLE ventas_documentos (
  id INTEGER PRIMARY KEY,
  tipo STRING(20),          -- factura, boleta, orden_venta, orden_compra, cotizacion
  numero STRING(60),        -- Document number
  fecha_documento DATETIME, -- Document date
  cliente_id INTEGER,       -- FK to ventas_clientes
  cliente_nombre STRING(200),
  cliente_rut STRING(20),
  subtotal FLOAT,
  impuesto FLOAT,
  total FLOAT,
  status STRING(50),        -- pendiente, aprobada, entregada, anulada
  stock_deducted BOOLEAN,   -- True if stock was deducted
  created_at DATETIME,
  updated_at DATETIME
);
```

#### ventas_documentos_items
```sql
CREATE TABLE ventas_documentos_items (
  id INTEGER PRIMARY KEY,
  documento_id INTEGER,     -- FK to ventas_documentos
  codigo_producto STRING(100),
  marca STRING(120),        -- Product variant/brand
  bodega STRING(120),       -- Warehouse
  cantidad INTEGER,
  precio_unitario FLOAT,
  subtotal FLOAT
);
```

#### ventas_notas_credito
```sql
CREATE TABLE ventas_notas_credito (
  id INTEGER PRIMARY KEY,
  documento_venta_id INTEGER,  -- FK to ventas_documentos
  numero STRING(60),
  razon STRING(255),        -- Reason for return
  subtotal FLOAT,
  total FLOAT,
  status STRING(50),        -- pendiente, aprobada, procesada
  stock_restored BOOLEAN,   -- True if stock was restored
  fecha_documento DATETIME,
  created_at DATETIME
);
```

#### ventas_notas_credito_items
```sql
CREATE TABLE ventas_notas_credito_items (
  id INTEGER PRIMARY KEY,
  nota_credito_id INTEGER,  -- FK to ventas_notas_credito
  codigo_producto STRING(100),
  marca STRING(120),
  bodega STRING(120),
  cantidad INTEGER,
  precio_unitario FLOAT,
  subtotal FLOAT
);
```

## Utility Modules

### app/utils/stock_control.py

Pure Python utility functions for stock operations:

```python
# Check stock availability
is_available, error_msg = check_stock_availability(items)

# Get detailed stock for product
variants = get_stock_by_variant(codigo_producto)

# Get complete product history
history = get_product_history(codigo_producto)

# Deduct stock for sale
success, msg = deduct_stock_for_sale(documento_id, usuario)

# Restore stock for credit note
success, msg = restore_stock_for_credit_note(nota_credito_id, usuario)
```

### app/static/stock_control.js

Client-side JavaScript utilities:

```javascript
// Check stock via API
StockControl.checkStockAvailability(items)
  .then(result => {
    console.log(result.available); // true/false
  });

// Get product history
StockControl.getProductHistory(codigo)
  .then(history => {
    // Display traceability modal
  });

// Create credit note
StockControl.createCreditNote(documentoId, items, razon)
  .then(result => {
    console.log(result.success);
  });

// Open traceability modal
window.openProductoTraceability(codigo);
window.closeProductoTraceability();
```

## Workflow Examples

### Example 1: Complete Sales Order with Stock Deduction

```
1. User creates new sales order (Orden de Venta)
   - Select client
   - Add items with quantities
   - Set status to "entregada"
   - Click "Generar documento"

2. System validates:
   - All items have sufficient stock
   - All required fields are filled
   - Document number is unique

3. System deducts stock:
   - Updates ProductoVarianteStock table
   - Creates MovimientoStock entry (type: salida)
   - Updates master stock in productos table
   - Sets DocumentoVenta.stock_deducted = True

4. Order is saved and displayed
   - User can print or email document
   - Stock is now reflected in inventory
```

### Example 2: Process Customer Return with Credit Note

```
1. User finds original sales order
   - Go to Ventas > Historial (future feature)
   - Or access via API: GET /api/documentos/<id>

2. Create credit note:
   - Click "New Credit Note" button
   - Select items to return
   - Enter reason ("Defective", "Wrong item", etc.)
   - Click "Crear Nota de Crédito"

3. System restores stock:
   - Adds quantity back to ProductoVarianteStock
   - Creates MovimientoStock entry (type: ingreso)
   - Sets NotaCredito.stock_restored = True
   - Links to original DocumentoVenta

4. Credit note is saved
   - Customer gets refund/exchange documentation
   - Inventory is updated
```

### Example 3: View Product Traceability

```
1. Find product in search:
   - Go to Productos > Buscar
   - Search for product code
   - Click arrow icon (↗) next to product code

2. Modal opens showing:
   - Current stock by warehouse/variant
   - All supplier ingresos (with dates, quantities)
   - All customer sales (with dates, prices)
   - All returns/credit notes
   - Last sale highlight

3. Timeline shows:
   - Green 📥 = Stock In (supplier receipt)
   - Red 📤 = Stock Out (customer sale)
   - Blue ↩️ = Return (credit note)
```

## Configuration Files

### Integration Points

**Templates:**
- `app/templates/ventas/base.html` - Traceability modal and stock_control.js
- `app/templates/ventas/documento.html` - Form-level stock validation
- `app/templates/buscar.html` - Product search with traceability icons

**Routes:**
- `app/ventas/routes.py` - All stock control APIs and form processing
- `app/bodega/routes.py` - Stock movement tracking (existing)

**Models:**
- `app/ventas/models.py` - DocumentoVenta, DocumentoVentaItem, NotaCredito, NotaCreditoItem
- `app/bodega/models.py` - MovimientoStock, ProductoVarianteStock (existing)

## API Reference

### Stock Validation

**POST /ventas/api/stock/check**
```json
Request:
{
  "items": [
    {
      "codigo_producto": "PRD001",
      "marca": "GENÉRICA",
      "bodega": "Bodega 1",
      "cantidad": 5
    }
  ]
}

Response:
{
  "success": true,
  "available": true,
  "message": "Stock disponible para todos los items"
}
```

### Get Product Stock

**GET /ventas/api/stock/product/{codigo}**
```json
Response:
{
  "success": true,
  "codigo": "PRD001",
  "total_stock": 100,
  "by_variant": [
    {
      "marca": "GENÉRICA",
      "bodega": "Bodega 1",
      "stock": 50,
      "id": 1
    }
  ]
}
```

### Get Product History

**GET /ventas/api/product/history/{codigo}**
```json
Response:
{
  "success": true,
  "codigo_producto": "PRD001",
  "ingresos": [...],
  "ventas": [...],
  "notas_credito": [...],
  "stock_summary": {...},
  "last_sale": {...}
}
```

### Create Credit Note

**POST /ventas/api/credit-note**
```json
Request:
{
  "documento_id": 123,
  "items": [...],
  "razon": "Producto defectuoso"
}

Response:
{
  "success": true,
  "nota_credito": {
    "id": 456,
    "numero": "NC-20260321-143000",
    "documento_venta_id": 123,
    "total": 250.00,
    "status": "pendiente"
  },
  "message": "Nota de crédito creada exitosamente"
}
```

## Installation & Setup

### 1. Run Migration

```bash
cd /AndesAutoParts
python migrate_stock_control.py
```

### 2. Restart Application

```bash
python run.py
```

### 3. Verify Installation

The system will show:
- ✓ ventas_documentos table created
- ✓ ventas_documentos_items table created
- ✓ ventas_notas_credito table created
- ✓ ventas_notas_credito_items table created

## Error Messages

| Error | Cause | Solution |
|-------|-------|----------|
| "Stock insuficiente para PRD001" | Not enough inventory | Receive more stock or reduce order quantity |
| "La variante PRD001 / MARCA en BODEGA no existe" | Variant/warehouse combo not found | Create the variant first in bodega module |
| "Producto no encontrado" | Product code doesn't exist | Check spelling or create product |
| "Nota de crédito creada" pero stock no se restaura | Credit note created but stock_restored=False | Manually check and update stock |

## Performance Notes

- Stock queries use indexes on codigo_producto, marca, bodega for O(1) lookups
- Traceability modal limits to 1000 records per type for performance
- Movement records are created in real-time (no batch processing)
- Credit notes support bulk returns (multiple items in single note)

## Future Enhancements

Potential features for next phase:
- [ ] Sales document history view/list
- [ ] Dashboard for stock alerts (low inventory)
- [ ] Automatic reorder points
- [ ] Multi-warehouse transfers
- [ ] Inventory adjustment audit log
- [ ] Barcode scanning integration
- [ ] Email notifications for low stock
- [ ] Integration with supplier API for auto-restocking

## Support

For issues or questions:
1. Check logs in `instance/` directory
2. Verify database integrity: `app/utils/stock_control.py` functions
3. Review test workflows in documentation
4. Contact development team with specific error message
