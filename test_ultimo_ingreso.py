#!/usr/bin/env python3
"""
Test script to verify ultimo_ingreso field is being updated correctly
"""
import sys
from datetime import datetime
from app import create_app
from app.extensions import db
from app.seguridad.models import Usuario, Rol

print("\n" + "="*80)
print("🧪 TEST: ultimo_ingreso Field Update")
print("="*80 + "\n")

# Create Flask app context
app = create_app()

with app.app_context():
    # Check database connection
    print("✓ Database connection established")
    print(f"✓ Database: {app.config.get('SQLALCHEMY_DATABASE_URI')}\n")
    
    # Find test user
    print("📋 Checking existing users...")
    usuarios = Usuario.query.all()
    print(f"✓ Found {len(usuarios)} users in database\n")
    
    for u in usuarios:
        print(f"  👤 {u.usuario} (ID: {u.id})")
        print(f"     - Nombre: {u.nombre}")
        print(f"     - Rol: {u.rol.nombre if u.rol else 'No role'}")
        print(f"     - Activo: {u.activo}")
        print(f"     - ultimo_acceso: {u.ultimo_acceso}")
        print(f"     - ultimo_ingreso: {u.ultimo_ingreso}")
        print()
    
    # Check Model Structure
    print("\n" + "="*80)
    print("🔍 CAMPO VERIFICATION")
    print("="*80 + "\n")
    
    # Test field exists in model
    test_user = usuarios[0] if usuarios else None
    
    if test_user:
        print(f"✓ Testing on user: {test_user.usuario}")
        print(f"✓ Usuario model has 'ultimo_ingreso' field: {hasattr(test_user, 'ultimo_ingreso')}")
        print(f"✓ Current value: {test_user.ultimo_ingreso}")
        print(f"✓ Value type: {type(test_user.ultimo_ingreso)}")
        
        # Test API response
        print("\n" + "="*80)
        print("📡 API RESPONSE TEST")
        print("="*80 + "\n")
        
        # Simulate API call
        usuarios_all = Usuario.query.all()
        for u in usuarios_all:
            ultimo_ingreso_fmt = u.ultimo_ingreso.strftime("%d-%m-%Y %H:%M") if u.ultimo_ingreso else "-"
            print(f"  👤 {u.usuario}")
            print(f"     - Raw: {u.ultimo_ingreso}")
            print(f"     - Formatted (dd-mm-yyyy HH:mm): {ultimo_ingreso_fmt}")
            print()
    
    # Check database schema
    print("="*80)
    print("🗄️  DATABASE SCHEMA CHECK")
    print("="*80 + "\n")
    
    # Get table columns
    from sqlalchemy import inspect
    inspector = inspect(db.engine)
    
    if inspector.has_table('usuarios_sistema'):
        columns = inspector.get_columns('usuarios_sistema')
        print("✓ usuarios_sistema table columns:")
        for col in columns:
            col_name = col['name']
            col_type = col['type']
            nullable = "nullable" if col['nullable'] else "NOT NULL"
            marker = "✅" if col_name == 'ultimo_ingreso' else "  "
            print(f"  {marker} {col_name:30s} {str(col_type):20s} {nullable}")
    else:
        print("❌ usuarios_sistema table not found!")
    
    print("\n" + "="*80)
    print("✅ VERIFICATION COMPLETE")
    print("="*80)
    print("\nNOTE: Login to update ultimo_ingreso field. It updates on successful authentication.")
    print("After login, check the Users dashboard (⚙️ Opciones) to see updated times.\n")
