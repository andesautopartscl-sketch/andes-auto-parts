#!/usr/bin/env python
"""Check all users in the consolidated database"""

import sqlite3

DB_PATH = r"c:\AndesAutoParts\data\andes.db"

print("=" * 80)
print(f"CHECKING DATABASE: {DB_PATH}")
print("=" * 80)

try:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Check usuarios_sistema table
    print("\n📋 USUARIOS_SISTEMA TABLE:")
    cursor.execute("SELECT id, nombre, usuario, activo FROM usuarios_sistema")
    rows = cursor.fetchall()
    print(f"   Found {len(rows)} records:")
    for row in rows:
        print(f"      ID: {row[0]}, Usuario: {row[1]}, Login: {row[2]}, Activo: {row[3]}")
    
    # Check tables in database
    print("\n📊 ALL TABLES IN DATABASE:")
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = cursor.fetchall()
    for table in tables:
        cursor.execute(f"SELECT COUNT(*) FROM {table[0]}")
        count = cursor.fetchone()[0]
        print(f"      {table[0]}: {count} records")
    
    conn.close()
    print("\n✅ DATABASE CHECK COMPLETE")
    
except Exception as e:
    print(f"\n❌ ERROR: {e}")

print("\n" + "=" * 80)
