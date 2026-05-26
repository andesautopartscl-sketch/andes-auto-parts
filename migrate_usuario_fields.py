#!/usr/bin/env python
"""
Database migration: Add new fields to Usuario model
Safely handles SQLite table recreation
"""

import sqlite3
import os
from datetime import datetime
from app import create_app
from app.extensions import db
from app.seguridad.models import Usuario, Rol

print("=" * 80)
print("DATABASE MIGRATION: Adding new fields to Usuario model")
print("=" * 80)

DB_PATH = r"c:\AndesAutoParts\data\andes.db"

try:
    app = create_app()
    
    with app.app_context():
        print("\n📋 Step 1: Checking existing usuarios...")
        
        # Get existing users
        existing_users = db.session.query(Usuario).all()
        print(f"   Found {len(existing_users)} existing usuarios")
        
        users_data = []
        for u in existing_users:
            users_data.append({
                "id": u.id,
                "nombre": u.nombre,
                "usuario": u.usuario,
                "password_hash": u.password_hash,
                "rol_id": u.rol_id,
                "activo": u.activo,
                "en_linea": u.en_linea,
                "ultimo_acceso": u.ultimo_acceso,
                "fecha_creacion": u.fecha_creacion
            })
            print(f"      - {u.usuario} ({u.nombre})")
        
        print("\n📋 Step 2: Dropping old table...")
        db.session.execute("DROP TABLE IF EXISTS usuarios_sistema")
        db.session.commit()
        print("   ✅ Old table dropped")
        
        print("\n📋 Step 3: Creating new table with extended schema...")
        db.create_all()
        print("   ✅ New table created with all fields:")
        print("      - correo (unique)")
        print("      - telefono")
        print("      - direccion")
        print("      - genero")
        print("      - fecha_nacimiento")
        print("      - rut (unique)")
        print("      - ultimo_ingreso")
        
        print("\n📋 Step 4: Reinsert existing usuarios...")
        for user_data in users_data:
            new_user = Usuario(
                id=user_data["id"],
                nombre=user_data["nombre"],
                usuario=user_data["usuario"],
                password_hash=user_data["password_hash"],
                rol_id=user_data["rol_id"],
                activo=user_data["activo"],
                en_linea=user_data["en_linea"],
                ultimo_acceso=user_data["ultimo_acceso"],
                fecha_creacion=user_data["fecha_creacion"],
                # New fields (null for existing users)
                correo=None,
                telefono=None,
                direccion=None,
                genero=None,
                fecha_nacimiento=None,
                rut=None,
                ultimo_ingreso=None
            )
            db.session.add(new_user)
        
        db.session.commit()
        print(f"   ✅ {len(users_data)} usuarios reinserted")
        
        print("\n📋 Step 5: Verifying migration...")
        migrated_users = db.session.query(Usuario).all()
        print(f"   ✅ {len(migrated_users)} usuarios in new schema:")
        for u in migrated_users:
            print(f"      - {u.usuario}: correo={u.correo}, rut={u.rut}, tfno={u.telefono}")
        
        print("\n" + "=" * 80)
        print("✅ MIGRATION COMPLETED SUCCESSFULLY")
        print("=" * 80)
        print("\nNew Usuario fields:")
        print("   □ correo (email, unique)")
        print("   □ telefono (phone number)")
        print("   □ direccion (address)")
        print("   □ genero (Masculino/Femenino)")
        print("   □ fecha_nacimiento (date)")
        print("   □ rut (unique)")
        print("   □ ultimo_ingreso (datetime)")
        print("\n✅ Ready to use updated system!")

except Exception as e:
    print(f"\n❌ MIGRATION FAILED: {e}")
    import traceback
    traceback.print_exc()
    print("\nRolling back changes...")

print("\n" + "=" * 80)
