"""
SQLite: añade emisor_nombre y emisor_rut a movimientos_contables (Libro diario).
Idempotente: no duplica columnas si ya existen.
"""
import os
import sqlite3


def main() -> None:
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    db_path = os.path.join(root, "data", "andes.db")
    if not os.path.isfile(db_path):
        print(f"No se encontró {db_path}")
        return
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(movimientos_contables)")
        cols = {row[1] for row in cur.fetchall()}
        if "emisor_nombre" not in cols:
            cur.execute(
                "ALTER TABLE movimientos_contables "
                "ADD COLUMN emisor_nombre VARCHAR(200) DEFAULT ''"
            )
            print("OK: columna emisor_nombre")
        else:
            print("Skip: emisor_nombre ya existe")
        if "emisor_rut" not in cols:
            cur.execute(
                "ALTER TABLE movimientos_contables "
                "ADD COLUMN emisor_rut VARCHAR(24) DEFAULT ''"
            )
            print("OK: columna emisor_rut")
            try:
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS ix_movimientos_contables_emisor_rut "
                    "ON movimientos_contables (emisor_rut)"
                )
            except sqlite3.OperationalError:
                pass
        else:
            print("Skip: emisor_rut ya existe")
        conn.commit()
    finally:
        conn.close()
    print("Listo.")


if __name__ == "__main__":
    main()
