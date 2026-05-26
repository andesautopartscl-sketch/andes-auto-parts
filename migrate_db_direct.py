#!/usr/bin/env python
"""
Safe database migration using direct SQL
Adds new fields to usuarios_sistema table
"""

import sqlite3
from datetime import datetime

DB_PATH = r"c:\AndesAutoParts\data\andes.db"

print("=" * 80)
print("DATABASE MIGRATION: Adding new fields to usuarios_sistema")
print("=" * 80)

try:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    print("\n📋 Step 1: Checking current table structure...")
    cursor.execute("PRAGMA table_info(usuarios_sistema)")
    columns = cursor.fetchall()
    print(f"   Current columns ({len(columns)}):")
    existing_cols = set()
    for col in columns:
        print(f"      - {col[1]} ({col[2]})")
        existing_cols.add(col[1])
    
    print("\n📋 Step 2: Backing up existing usuarios...")
    cursor.execute("SELECT * FROM usuarios_sistema")
    backup_users = cursor.fetchall()
    print(f"   Found {len(backup_users)} usuarios")
    
    # Get column names
    col_names = [description[0] for description in cursor.description]
    print(f"   Columns: {col_names}")
    
    print("\n📋 Step 3: Recreating table with new schema...")
    
    # Drop old table
    cursor.execute("DROP TABLE IF EXISTS usuarios_sistema")
    print("   Old table dropped")
    
    # Create new table with all fields
    create_sql = """
    CREATE TABLE usuarios_sistema (
        id INTEGER PRIMARY KEY,
        nombre VARCHAR(120),
        usuario VARCHAR(80) UNIQUE NOT NULL,
        password_hash VARCHAR(200),
        
        -- New fields: Información personal
        correo VARCHAR(120) UNIQUE,
        telefono VARCHAR(20),
        direccion VARCHAR(255),
        genero VARCHAR(20),
        fecha_nacimiento DATE,
        rut VARCHAR(20) UNIQUE,
        
        -- Relationships and status
        rol_id INTEGER,
        activo BOOLEAN DEFAULT 1,
        en_linea BOOLEAN DEFAULT 0,
        
        -- Timestamps
        ultimo_acceso DATETIME,
        ultimo_ingreso DATETIME,
        fecha_creacion DATETIME,
        
        FOREIGN KEY (rol_id) REFERENCES roles(id)
    )
    """
    cursor.execute(create_sql)
    print("   New table created with fields:")
    print("      - correo (unique)")
    print("      - telefono")
    print("      - direccion")
    print("      - genero")
    print("      - fecha_nacimiento")
    print("      - rut (unique)")
    print("      - ultimo_ingreso")
    
    print("\n📋 Step 4: Restoring usuarios data...")
    
    # Map old column names to new
    col_mappings = {
        'id': 'id',
        'nombre': 'nombre',
        'usuario': 'usuario',
        'password_hash': 'password_hash',
        'rol_id': 'rol_id',
        'activo': 'activo',
        'en_linea': 'en_linea',
        'ultimo_acceso': 'ultimo_acceso',
        'fecha_creacion': 'fecha_creacion'
    }
    
    insert_count = 0
    for user_row in backup_users:
        user_dict = {col_names[i]: user_row[i] for i in range(len(col_names))}
        
        insert_sql = """
        INSERT INTO usuarios_sistema 
        (id, nombre, usuario, password_hash, rol_id, activo, en_linea, 
         ultimo_acceso, fecha_creacion, correo, telefono, direccion, genero, 
         fecha_nacimiento, rut, ultimo_ingreso)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        
        params = (
            user_dict.get('id'),
            user_dict.get('nombre'),
            user_dict.get('usuario'),
            user_dict.get('password_hash'),
            user_dict.get('rol_id'),
            user_dict.get('activo'),
            user_dict.get('en_linea'),
            user_dict.get('ultimo_acceso'),
            user_dict.get('fecha_creacion'),
            None,  # correo
            None,  # telefono
            None,  # direccion
            None,  # genero
            None,  # fecha_nacimiento
            None,  # rut
            None   # ultimo_ingreso
        )
        
        cursor.execute(insert_sql, params)
        print(f"   Restored: {user_dict.get('usuario')}")
        insert_count += 1
    
    conn.commit()
    print(f"   ✅ {insert_count} usuarios restored")
    
    print("\n📋 Step 5: Verifying migration...")
    cursor.execute("PRAGMA table_info(usuarios_sistema)")
    new_columns = cursor.fetchall()
    print(f"   New table has {len(new_columns)} columns:")
    for col in new_columns:
        print(f"      - {col[1]} ({col[2]})")
    
    cursor.execute("SELECT COUNT(*) FROM usuarios_sistema")
    count = cursor.fetchone()[0]
    print(f"   ✅ {count} usuarios in table")
    
    cursor.execute("SELECT usuario, correo, rut, ultimo_ingreso FROM usuarios_sistema")
    users_check = cursor.fetchall()
    print(f"   Sample: {users_check[0]}")
    
    conn.close()
    
    print("\n" + "=" * 80)
    print("✅ MIGRATION COMPLETED SUCCESSFULLY!")
    print("=" * 80)
    
except Exception as e:
    print(f"\n❌ MIGRATION FAILED: {e}")
    import traceback
    traceback.print_exc()

print("\n" + "=" * 80)
