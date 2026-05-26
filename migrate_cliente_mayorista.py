#!/usr/bin/env python
"""SQLite: add cliente_mayorista y margen_descuento_pct a ventas_clientes."""

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "andes.db"


def main() -> None:
    if not DB_PATH.is_file():
        print(f"No se encontro la base: {DB_PATH}")
        return
    conn = sqlite3.connect(str(DB_PATH))
    try:

        def col_names() -> set[str]:
            cur = conn.execute("PRAGMA table_info(ventas_clientes)")
            return {row[1] for row in cur.fetchall()}

        cols = col_names()
        if "cliente_mayorista" not in cols:
            conn.execute(
                "ALTER TABLE ventas_clientes ADD COLUMN cliente_mayorista BOOLEAN NOT NULL DEFAULT 0"
            )
            print("OK: columna cliente_mayorista")
        else:
            print("Skip: cliente_mayorista ya existe")
        cols = col_names()
        if "margen_descuento_pct" not in cols:
            conn.execute(
                "ALTER TABLE ventas_clientes ADD COLUMN margen_descuento_pct REAL NOT NULL DEFAULT 0"
            )
            print("OK: columna margen_descuento_pct")
        else:
            print("Skip: margen_descuento_pct ya existe")
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
