import sqlite3

print("=" * 80)
print("TABLES IN data/andes.db (OLD SYSTEM):")
print("=" * 80)
try:
    conn = sqlite3.connect("data/andes.db")
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = cursor.fetchall()
    for table in tables:
        print(f"  - {table[0]}")
        cursor.execute(f"SELECT COUNT(*) FROM {table[0]};")
        count = cursor.fetchone()[0]
        print(f"    Records: {count}")
    conn.close()
except Exception as e:
    print(f"  Error: {e}")

print("\n" + "=" * 80)
print("TABLES IN instance/andes.db (NEW SYSTEM):")
print("=" * 80)
try:
    conn = sqlite3.connect("instance/andes.db")
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = cursor.fetchall()
    for table in tables:
        print(f"  - {table[0]}")
        cursor.execute(f"SELECT COUNT(*) FROM {table[0]};")
        count = cursor.fetchone()[0]
        print(f"    Records: {count}")
    conn.close()
except Exception as e:
    print(f"  Error: {e}")
