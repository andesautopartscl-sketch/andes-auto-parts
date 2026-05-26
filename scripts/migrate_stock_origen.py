#!/usr/bin/env python3
"""
Migracion segura para separar stock/costo por origen de compra.

- productos_variantes_stock: agrega origen_compra y unique por codigo+marca+bodega+origen.
- movimientos_stock: agrega origen_compra para auditoria.
- ingresos_documentos_items: agrega origen_compra por linea de ingreso.
- ventas_documentos_items: agrega origen_compra elegido en ventas.
- ventas_notas_credito_items: conserva origen al devolver stock.

Idempotente: se puede ejecutar varias veces.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Iterable


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "andes.db")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return {str(r[1]) for r in cur.fetchall()}


def _ensure_column(
    conn: sqlite3.Connection,
    table: str,
    col_name: str,
    ddl_fragment: str,
    *,
    backfill_sql: str | None = None,
) -> bool:
    cols = _columns(conn, table)
    if col_name in cols:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {ddl_fragment}")
    if backfill_sql:
        conn.execute(backfill_sql)
    return True


def _index_columns(conn: sqlite3.Connection, table: str, index_name: str) -> list[str]:
    rows = conn.execute(f"PRAGMA index_info({index_name})").fetchall()
    rows = sorted(rows, key=lambda r: int(r[0]))
    return [str(r[2]) for r in rows]


def _has_unique_index_for_cols(conn: sqlite3.Connection, table: str, cols: Iterable[str]) -> bool:
    want = list(cols)
    idx_rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
    for r in idx_rows:
        # PRAGMA index_list => seq, name, unique, origin, partial
        name = str(r[1])
        unique_flag = int(r[2] or 0)
        if unique_flag != 1:
            continue
        if _index_columns(conn, table, name) == want:
            return True
    return False


def _rebuild_variantes_with_origin(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS productos_variantes_stock_new (
            id INTEGER PRIMARY KEY,
            codigo_producto VARCHAR(100) NOT NULL,
            marca VARCHAR(120) NOT NULL,
            proveedor VARCHAR(150),
            bodega VARCHAR(120) NOT NULL,
            origen_compra VARCHAR(20) NOT NULL DEFAULT 'nacional',
            stock INTEGER NOT NULL DEFAULT 0,
            margen_override_pct FLOAT,
            precio_publico_neto_override FLOAT,
            metadata_json TEXT,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO productos_variantes_stock_new
            (id, codigo_producto, marca, proveedor, bodega, origen_compra, stock,
             margen_override_pct, precio_publico_neto_override, metadata_json, created_at, updated_at)
        SELECT
            id,
            codigo_producto,
            marca,
            proveedor,
            bodega,
            COALESCE(NULLIF(TRIM(origen_compra), ''), 'nacional') AS origen_compra,
            stock,
            margen_override_pct,
            precio_publico_neto_override,
            metadata_json,
            created_at,
            updated_at
        FROM productos_variantes_stock
        """
    )
    conn.execute("DROP TABLE productos_variantes_stock")
    conn.execute("ALTER TABLE productos_variantes_stock_new RENAME TO productos_variantes_stock")

    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_variante_codigo_marca_bodega_origen
        ON productos_variantes_stock(codigo_producto, marca, bodega, origen_compra)
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_productos_variantes_stock_codigo_producto ON productos_variantes_stock(codigo_producto)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_productos_variantes_stock_marca ON productos_variantes_stock(marca)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_productos_variantes_stock_bodega ON productos_variantes_stock(bodega)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_productos_variantes_stock_origen_compra ON productos_variantes_stock(origen_compra)"
    )


def main() -> None:
    if not os.path.isfile(DB_PATH):
        print(f"No existe la base: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")
        changes: list[str] = []

        if "origen_compra" not in _columns(conn, "productos_variantes_stock"):
            conn.execute(
                "ALTER TABLE productos_variantes_stock ADD COLUMN origen_compra VARCHAR(20) NOT NULL DEFAULT 'nacional'"
            )
            conn.execute(
                "UPDATE productos_variantes_stock SET origen_compra = 'nacional' WHERE origen_compra IS NULL OR TRIM(origen_compra) = ''"
            )
            changes.append("productos_variantes_stock.origen_compra")

        # Rebuild only if unique by origen is missing.
        if not _has_unique_index_for_cols(
            conn,
            "productos_variantes_stock",
            ["codigo_producto", "marca", "bodega", "origen_compra"],
        ):
            _rebuild_variantes_with_origin(conn)
            changes.append("uq_variante_codigo_marca_bodega_origen")

        if _ensure_column(
            conn,
            "movimientos_stock",
            "origen_compra",
            "VARCHAR(20) NOT NULL DEFAULT 'nacional'",
            backfill_sql=(
                "UPDATE movimientos_stock SET origen_compra = 'nacional' "
                "WHERE origen_compra IS NULL OR TRIM(origen_compra) = ''"
            ),
        ):
            changes.append("movimientos_stock.origen_compra")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_movimientos_stock_origen_compra ON movimientos_stock(origen_compra)"
            )

        if _ensure_column(
            conn,
            "ingresos_documentos_items",
            "origen_compra",
            "VARCHAR(20) NOT NULL DEFAULT 'nacional'",
            backfill_sql=(
                "UPDATE ingresos_documentos_items SET origen_compra = 'nacional' "
                "WHERE origen_compra IS NULL OR TRIM(origen_compra) = ''"
            ),
        ):
            changes.append("ingresos_documentos_items.origen_compra")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_ingresos_documentos_items_origen_compra "
                "ON ingresos_documentos_items(origen_compra)"
            )

        if _ensure_column(
            conn,
            "ventas_documentos_items",
            "origen_compra",
            "VARCHAR(20) NOT NULL DEFAULT 'nacional'",
            backfill_sql=(
                "UPDATE ventas_documentos_items SET origen_compra = 'nacional' "
                "WHERE origen_compra IS NULL OR TRIM(origen_compra) = ''"
            ),
        ):
            changes.append("ventas_documentos_items.origen_compra")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_ventas_documentos_items_origen_compra "
                "ON ventas_documentos_items(origen_compra)"
            )

        if _ensure_column(
            conn,
            "ventas_notas_credito_items",
            "origen_compra",
            "VARCHAR(20) NOT NULL DEFAULT 'nacional'",
            backfill_sql=(
                "UPDATE ventas_notas_credito_items SET origen_compra = 'nacional' "
                "WHERE origen_compra IS NULL OR TRIM(origen_compra) = ''"
            ),
        ):
            changes.append("ventas_notas_credito_items.origen_compra")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_ventas_notas_credito_items_origen_compra "
                "ON ventas_notas_credito_items(origen_compra)"
            )

        conn.commit()
        if changes:
            print("OK: migracion aplicada.")
            print("Cambios:")
            for item in changes:
                print(f" - {item}")
        else:
            print("OK: no habia cambios pendientes (idempotente).")
    except Exception:
        conn.rollback()
        raise
    finally:
        try:
            conn.execute("PRAGMA foreign_keys = ON")
        except Exception:
            pass
        conn.close()


if __name__ == "__main__":
    main()
