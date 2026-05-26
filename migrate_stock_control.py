#!/usr/bin/env python3
"""
Migration script to add real-time stock control tables:
- DocumentoVenta (sales documents/invoices)
- DocumentoVentaItem (sales line items)
- NotaCredito (credit notes)
- NotaCreditoItem (credit note items)

Run: python migrate_stock_control.py
"""

import sys
from app import create_app, db
from app.ventas.models import DocumentoVenta, DocumentoVentaItem, NotaCredito, NotaCreditoItem

def migrate():
    """Create all new stock control tables."""
    app = create_app()
    
    with app.app_context():
        print("=" * 60)
        print("CREATING STOCK CONTROL TABLES")
        print("=" * 60)
        
        try:
            # Create all tables
            db.create_all()
            print("✓ All tables created successfully!")
            
            # Verify tables exist
            inspector = db.inspect(db.engine)
            tables = inspector.get_table_names()
            
            required_tables = {
                "ventas_documentos": "Sales Documents",
                "ventas_documentos_items": "Sales Document Items",
                "ventas_notas_credito": "Credit Notes",
                "ventas_notas_credito_items": "Credit Note Items",
            }
            
            print("\nVerifying tables:")
            for table_name, description in required_tables.items():
                if table_name in tables:
                    columns = [col["name"] for col in inspector.get_columns(table_name)]
                    print(f"  ✓ {description} ({table_name})")
                    print(f"    Columns: {', '.join(columns[:5])}..." if len(columns) > 5 else f"    Columns: {', '.join(columns)}")
                else:
                    print(f"  ✗ {description} ({table_name}) - MISSING!")
                    return False
            
            print("\n" + "=" * 60)
            print("MIGRATION COMPLETED SUCCESSFULLY!")
            print("=" * 60)
            print("\nNext steps:")
            print("1. Run the Flask application: python run.py")
            print("2. Test the stock control with:")
            print("   - POST /ventas/api/stock/check")
            print("   - GET /ventas/api/stock/product/<codigo>")
            print("   - GET /ventas/api/product/history/<codigo>")
            print("   - POST /ventas/api/credit-note")
            print("3. Create sales documents and verify stock deduction")
            print("4. Create credit notes to test stock restoration")
            
            return True
            
        except Exception as e:
            print(f"\n✗ Error during migration: {str(e)}")
            print("\nTroubleshooting:")
            print("- Ensure the app/extensions.py has db configured correctly")
            print("- Check that app/ventas/models.py has the new model classes")
            print("- Verify the database file has write permissions")
            return False


if __name__ == "__main__":
    success = migrate()
    sys.exit(0 if success else 1)
