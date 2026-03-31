"""
Stock control utility module for real-time inventory management.
Handles stock validation, deduction, reversal, and traceability.
"""

from datetime import datetime
from sqlalchemy import text
from app.extensions import db
from app.bodega.models import ProductoVarianteStock, MovimientoStock, IngresoDocumento, IngresoDocumentoItem
from app.inventario.models import LabelPrintHistory
from app.ventas.models import DocumentoVenta, DocumentoVentaItem, NotaCredito, NotaCreditoItem


# =====================================================
# STOCK VALIDATION
# =====================================================

def get_available_stock(codigo_producto: str, marca: str = None, bodega: str = None) -> int:
    """
    Get available stock for a product by brand/variant and warehouse.
    
    Args:
        codigo_producto: Product code
        marca: Brand/variant (optional, returns total if not specified)
        bodega: Warehouse (optional, returns total if not specified)
    
    Returns:
        Available quantity in stock
    """
    query = ProductoVarianteStock.query.filter_by(codigo_producto=codigo_producto)
    
    if marca:
        query = query.filter_by(marca=marca)
    if bodega:
        query = query.filter_by(bodega=bodega)
    
    variants = query.all()
    return sum(v.stock for v in variants) if variants else 0


def get_stock_by_variant(codigo_producto: str) -> list[dict]:
    """
    Get stock breakdown by variant (brand/warehouse).
    
    Returns:
        List of dicts with marca, bodega, stock
    """
    variants = ProductoVarianteStock.query.filter_by(codigo_producto=codigo_producto).all()
    return [
        {
            "marca": v.marca,
            "bodega": v.bodega,
            "stock": v.stock,
            "id": v.id,
        }
        for v in variants
    ]


def check_stock_availability(items: list[dict]) -> tuple[bool, str]:
    """
    Check if all items in a sales order have sufficient stock.
    
    Args:
        items: List of dicts with 'codigo_producto', 'marca', 'bodega', 'cantidad'
    
    Returns:
        (is_available, error_message)
    """
    for item in items:
        codigo = item.get("codigo_producto")
        marca = item.get("marca")
        bodega = item.get("bodega")
        cantidad = item.get("cantidad", 0)
        
        available = get_available_stock(codigo, marca, bodega)
        
        if available < cantidad:
            return False, f"Producto {codigo} ({marca}): stock insuficiente. Disponible: {available}, solicitado: {cantidad}"
    
    return True, ""


def validate_sale_items(items: list[dict]) -> tuple[bool, str]:
    """
    Validate items for a sale (stock + data validation).
    
    Returns:
        (is_valid, error_message)
    """
    if not items:
        return False, "No items in sale"
    
    for item in items:
        if not item.get("codigo_producto"):
            return False, "Product code is required"
        if not item.get("cantidad") or item["cantidad"] <= 0:
            return False, f"Invalid quantity for {item.get('codigo_producto')}"
        if not item.get("precio_unitario") or item["precio_unitario"] < 0:
            return False, f"Invalid price for {item.get('codigo_producto')}"
    
    # Check stock
    return check_stock_availability(items)


# =====================================================
# STOCK DEDUCTION (ON SALE)
# =====================================================

def deduct_stock_for_sale(documento_id: int, usuario: str = None) -> tuple[bool, str]:
    """
    Deduct stock for a sales document.
    Creates movement records (type: OUT, reason: sale).
    
    Args:
        documento_id: SalesDocument ID
        usuario: Username performing the action
    
    Returns:
        (success, message)
    """
    try:
        documento = DocumentoVenta.query.get(documento_id)
        if not documento:
            return False, "Documento no encontrado"
        
        if documento.stock_deducted:
            return False, "Stock ya fue deducido para este documento"
        
        for item in documento.items:
            # Get variant stock
            variant = ProductoVarianteStock.query.filter_by(
                codigo_producto=item.codigo_producto,
                marca=item.marca,
                bodega=item.bodega
            ).first()
            
            if not variant:
                return False, f"Stock record not found for {item.codigo_producto} {item.marca} {item.bodega}"
            
            # Check availability
            if variant.stock < item.cantidad:
                return False, f"Insuficiente stock para {item.codigo_producto}. Disponible: {variant.stock}, solicitado: {item.cantidad}"
            
            # Deduct stock
            variant.stock -= item.cantidad
            db.session.flush()
            
            # Create OUT movement record
            movimiento = MovimientoStock(
                codigo_producto=item.codigo_producto,
                tipo="salida",  # OUT sale
                cantidad=item.cantidad,
                fecha=datetime.utcnow(),
                usuario=usuario or "system",
                marca=item.marca,
                bodega=item.bodega,
                observacion=f"Venta documento {documento.tipo} #{documento.numero or documento.id}"
            )
            db.session.add(movimiento)
        
        # Mark as deducted
        documento.stock_deducted = True
        db.session.commit()
        
        return True, "Stock deducido exitosamente"
    
    except Exception as e:
        db.session.rollback()
        return False, f"Error al deducir stock: {str(e)}"


# =====================================================
# STOCK RESTORATION (ON CREDIT NOTE)
# =====================================================

def restore_stock_for_credit_note(nota_credito_id: int, usuario: str = None) -> tuple[bool, str]:
    """
    Restore stock for a credit note.
    Creates movement records (type: IN, reason: credit note/return).
    
    Args:
        nota_credito_id: CreditNote ID
        usuario: Username performing the action
    
    Returns:
        (success, message)
    """
    try:
        nota = NotaCredito.query.get(nota_credito_id)
        if not nota:
            return False, "Nota de crédito no encontrada"
        
        if nota.stock_restored:
            return False, "Stock ya fue restaurado para esta nota de crédito"
        
        for item in nota.items:
            # Get variant stock
            variant = ProductoVarianteStock.query.filter_by(
                codigo_producto=item.codigo_producto,
                marca=item.marca,
                bodega=item.bodega
            ).first()
            
            if not variant:
                # Create new variant record if it doesn't exist
                variant = ProductoVarianteStock(
                    codigo_producto=item.codigo_producto,
                    marca=item.marca,
                    bodega=item.bodega,
                    stock=0
                )
                db.session.add(variant)
                db.session.flush()
            
            # Restore stock
            variant.stock += item.cantidad
            db.session.flush()
            
            # Create IN movement record
            movimiento = MovimientoStock(
                codigo_producto=item.codigo_producto,
                tipo="ingreso",  # IN return
                cantidad=item.cantidad,
                fecha=datetime.utcnow(),
                usuario=usuario or "system",
                marca=item.marca,
                bodega=item.bodega,
                observacion=f"Nota de crédito #{nota.numero or nota.id} - {nota.razon or 'Devolución'}"
            )
            db.session.add(movimiento)
        
        # Mark as restored
        nota.stock_restored = True
        db.session.commit()
        
        return True, "Stock restaurado exitosamente"
    
    except Exception as e:
        db.session.rollback()
        return False, f"Error al restaurar stock: {str(e)}"


# =====================================================
# PRODUCT TRACEABILITY
# =====================================================

def get_product_history(codigo_producto: str, limit: int = 1000) -> dict:
    """
    Get complete product traceability: all stock entries, sales, credit notes.
    
    Args:
        codigo_producto: Product code
        limit: Maximum number of records per type
    
    Returns:
        Dict with 'ingresos', 'ventas', 'notas_credito', 'stock_summary'
    """
    
    # Get all ingreso (stock in) records
    ingresos = db.session.query(IngresoDocumentoItem, IngresoDocumento).join(
        IngresoDocumento,
        IngresoDocumentoItem.ingreso_documento_id == IngresoDocumento.id
    ).filter(
        IngresoDocumentoItem.codigo_producto == codigo_producto
    ).order_by(IngresoDocumento.created_at.desc()).limit(limit).all()
    
    ingresos_list = []
    for item, doc in ingresos:
        ingresos_list.append({
            "id": doc.id,
            "tipo": "ingreso",
            "documento_numero": doc.numero_documento,
            "proveedor": doc.proveedor_nombre,
            "proveedor_rut": doc.proveedor_rut,
            "fecha": doc.fecha_documento.isoformat() if doc.fecha_documento else None,
            "cantidad": item.cantidad,
            "marca": item.marca,
            "bodega": item.bodega,
            "timestamp": doc.created_at.isoformat() if doc.created_at else None,
        })
    
    # Get all sales (stock out) records from DocumentoVenta
    sales = db.session.query(DocumentoVentaItem, DocumentoVenta).join(
        DocumentoVenta,
        DocumentoVentaItem.documento_id == DocumentoVenta.id
    ).filter(
        DocumentoVentaItem.codigo_producto == codigo_producto,
        DocumentoVenta.stock_deducted == True  # Only deducted sales
    ).order_by(DocumentoVenta.fecha_documento.desc()).limit(limit).all()
    
    ventas_list = []
    for item, doc in sales:
        ventas_list.append({
            "id": doc.id,
            "tipo": "venta",
            "documento_numero": doc.numero,
            "documento_tipo": doc.tipo,
            "cliente": doc.cliente_nombre,
            "cliente_rut": doc.cliente_rut,
            "fecha": doc.fecha_documento.isoformat() if doc.fecha_documento else None,
            "cantidad": item.cantidad,
            "precio_unitario": item.precio_unitario,
            "marca": item.marca,
            "bodega": item.bodega,
            "timestamp": doc.created_at.isoformat() if doc.created_at else None,
        })
    
    # Get all credit notes (stock in returns)
    notas_credito = db.session.query(NotaCreditoItem, NotaCredito, DocumentoVenta).join(
        NotaCredito,
        NotaCreditoItem.nota_credito_id == NotaCredito.id
    ).join(
        DocumentoVenta,
        NotaCredito.documento_venta_id == DocumentoVenta.id
    ).filter(
        NotaCreditoItem.codigo_producto == codigo_producto,
        NotaCredito.stock_restored == True  # Only processed credit notes
    ).order_by(NotaCredito.fecha_documento.desc()).limit(limit).all()
    
    notas_list = []
    for item, nota, doc_original in notas_credito:
        notas_list.append({
            "id": nota.id,
            "tipo": "nota_credito",
            "numero": nota.numero,
            "documento_original": doc_original.numero or f"#{doc_original.id}",
            "cliente": doc_original.cliente_nombre,
            "razon": nota.razon or "Devolución",
            "fecha": nota.fecha_documento.isoformat() if nota.fecha_documento else None,
            "cantidad": item.cantidad,
            "marca": item.marca,
            "bodega": item.bodega,
            "timestamp": nota.created_at.isoformat() if nota.created_at else None,
        })
    
    # Get current stock summary by warehouse
    variants = ProductoVarianteStock.query.filter_by(codigo_producto=codigo_producto).all()
    stock_summary = {}
    for v in variants:
        key = f"{v.bodega} ({v.marca})"
        stock_summary[key] = {
            "bodega": v.bodega,
            "marca": v.marca,
            "stock": v.stock,
        }
    
    product_row = db.session.execute(
        text("SELECT id FROM productos WHERE UPPER(CODIGO) = :codigo LIMIT 1"),
        {"codigo": codigo_producto},
    ).mappings().first()
    product_id = int(product_row.get("id") or 0) if product_row else 0

    label_prints = []
    if product_id > 0:
        history_rows = db.session.query(LabelPrintHistory).filter(
            LabelPrintHistory.product_id == product_id
        ).order_by(
            LabelPrintHistory.date_time.desc(), LabelPrintHistory.id.desc()
        ).limit(limit).all()
        label_prints = [
            {
                "id": row.id,
                "tipo": "label_print",
                "product_id": row.product_id,
                "product_name": row.product_name,
                "quantity": row.quantity,
                "user_id": row.user_id,
                "date_time": row.date_time.isoformat() if row.date_time else None,
                "document_reference": row.document_reference or "",
            }
            for row in history_rows
        ]

    return {
        "codigo_producto": codigo_producto,
        "ingresos": ingresos_list,
        "ventas": ventas_list,
        "notas_credito": notas_list,
        "label_prints": label_prints,
        "stock_summary": stock_summary,
        "last_sale": ventas_list[0] if ventas_list else None,
    }


def get_last_sale_info(codigo_producto: str) -> dict or None:
    """Get the most recent sale for a product."""
    history = get_product_history(codigo_producto, limit=5)
    return history.get("last_sale")


# =====================================================
# WAREHOUSE & VARIANT HELPERS
# =====================================================

def get_all_warehouses() -> list[str]:
    """Get list of all warehouses in the system."""
    bodegas = db.session.query(ProductoVarianteStock.bodega).distinct().all()
    return [b[0] for b in bodegas if b[0]]


def get_all_product_brands(codigo_producto: str) -> list[str]:
    """Get list of all brands/variants for a product."""
    marcas = db.session.query(ProductoVarianteStock.marca).filter_by(
        codigo_producto=codigo_producto
    ).distinct().all()
    return [m[0] for m in marcas if m[0]]
