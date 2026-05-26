#!/usr/bin/env python3
"""
Añade columnas opcionales a garantías para trazabilidad (línea de venta + NC).
Seguro de ejecutar varias veces (idempotente).
"""
from __future__ import annotations

import os
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "andes.db")


def main() -> None:
    if not os.path.isfile(DB_PATH):
        print(f"No existe la base: {DB_PATH}")
        return
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(garantias)")
        existing = {row[1] for row in cur.fetchall()}
        added = []
        if "documento_item_id" not in existing:
            cur.execute(
                "ALTER TABLE garantias ADD COLUMN documento_item_id INTEGER "
                "REFERENCES ventas_documentos_items(id)"
            )
            added.append("documento_item_id")
        if "nota_credito_id" not in existing:
            cur.execute(
                "ALTER TABLE garantias ADD COLUMN nota_credito_id INTEGER "
                "REFERENCES ventas_notas_credito(id)"
            )
            added.append("nota_credito_id")
        if "cliente_rut" not in existing:
            cur.execute("ALTER TABLE garantias ADD COLUMN cliente_rut VARCHAR(40) DEFAULT ''")
            added.append("cliente_rut")
        conn.commit()
        if added:
            print(f"OK: columnas añadidas a garantias: {', '.join(added)}")
        else:
            print("OK: garantias ya tenía las columnas de trazabilidad.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
