#!/usr/bin/env python3
"""
Sincroniza URLs de Cloudinary (epc_despiece / producto.despiece) desde la BD local hacia Render.

Generar SQL desde la máquina de desarrollo (lee data/andes.db):
  python scripts/sync_cloudinary_urls.py --generate
  python scripts/sync_cloudinary_urls.py --generate --db-path data/andes.db

Aplicar en Render (Shell del servicio web):
  python scripts/sync_cloudinary_urls.py --apply

Variables de entorno:
  ANDES_DB_PATH — ruta a andes.db (p. ej. /opt/render/project/src/data/andes.db)

Nota: oem_despiece se actualiza por oem_norm (no por id), porque los id pueden
diferir entre la BD local y la de producción.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path(os.environ.get("ANDES_DB_PATH") or (ROOT / "data" / "andes.db"))
DEFAULT_SQL = Path(__file__).resolve().parent / "sync_cloudinary_urls.sql"
SQLITE_TIMEOUT = 30

OEM_QUERY = """
    SELECT oem_norm, imagen_static
    FROM oem_despiece
    WHERE imagen_static IS NOT NULL
      AND instr(lower(imagen_static), 'cloudinary') > 0
    ORDER BY oem_norm
"""

PRODUCTOS_QUERY = """
    SELECT CODIGO, despiece
    FROM productos
    WHERE despiece IS NOT NULL
      AND instr(lower(despiece), 'cloudinary') > 0
    ORDER BY CODIGO
"""


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _connect(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(db_path, timeout=SQLITE_TIMEOUT)


def _fetch_updates(conn: sqlite3.Connection) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    oem_rows = [(str(r[0]), str(r[1])) for r in conn.execute(OEM_QUERY)]
    prod_rows = [(str(r[0]), str(r[1])) for r in conn.execute(PRODUCTOS_QUERY)]
    return oem_rows, prod_rows


def _build_sql(oem_rows: list[tuple[str, str]], prod_rows: list[tuple[str, str]]) -> str:
    lines: list[str] = [
        "-- Generado por scripts/sync_cloudinary_urls.py --generate",
        "-- Aplicar en producción: python scripts/sync_cloudinary_urls.py --apply",
        "-- oem_despiece: clave oem_norm (los id locales no coinciden con Render)",
        "BEGIN TRANSACTION;",
        "",
    ]
    if oem_rows:
        lines.append(f"-- oem_despiece ({len(oem_rows)} registros)")
        for oem_norm, url in oem_rows:
            lines.append(
                "UPDATE oem_despiece "
                f"SET imagen_static={_sql_literal(url)} "
                f"WHERE oem_norm={_sql_literal(oem_norm)};"
            )
        lines.append("")

    if prod_rows:
        lines.append(f"-- productos.despiece ({len(prod_rows)} registros)")
        for codigo, url in prod_rows:
            lines.append(
                "UPDATE productos "
                f"SET despiece={_sql_literal(url)} "
                f"WHERE CODIGO={_sql_literal(codigo)};"
            )
        lines.append("")

    lines.append("COMMIT;")
    lines.append("")
    return "\n".join(lines)


def cmd_generate(db_path: Path, sql_path: Path) -> int:
    if not db_path.is_file():
        print(f"ERROR: no existe la base de datos: {db_path}", file=sys.stderr)
        return 1

    conn = _connect(db_path)
    try:
        oem_rows, prod_rows = _fetch_updates(conn)
    finally:
        conn.close()

    sql_text = _build_sql(oem_rows, prod_rows)
    sql_path.write_text(sql_text, encoding="utf-8")

    total = len(oem_rows) + len(prod_rows)
    print(f"Base de datos: {db_path}")
    print(f"SQL generado:  {sql_path}")
    print(f"  oem_despiece (por oem_norm): {len(oem_rows)} UPDATE(s)")
    print(f"  productos (por CODIGO):      {len(prod_rows)} UPDATE(s)")
    print(f"  Total:                       {total} UPDATE(s)")
    if total == 0:
        print("AVISO: no hay filas con 'cloudinary' en imagen_static / despiece.")
    return 0


def _iter_update_statements(sql_path: Path) -> list[str]:
    if not sql_path.is_file():
        raise FileNotFoundError(sql_path)
    statements: list[str] = []
    for raw in sql_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("--"):
            continue
        upper = line.upper()
        if upper in ("BEGIN TRANSACTION;", "COMMIT;", "BEGIN;", "COMMIT;"):
            continue
        if upper.startswith("UPDATE "):
            statements.append(line if line.endswith(";") else f"{line};")
    return statements


def cmd_apply(db_path: Path, sql_path: Path, dry_run: bool) -> int:
    if not db_path.is_file():
        print(f"ERROR: no existe la base de datos: {db_path}", file=sys.stderr)
        print(
            "  En Render suele ser: /opt/render/project/src/data/andes.db",
            file=sys.stderr,
        )
        print("  Use --db-path o export ANDES_DB_PATH=...", file=sys.stderr)
        return 1

    try:
        statements = _iter_update_statements(sql_path)
    except FileNotFoundError:
        print(f"ERROR: no existe {sql_path}. Ejecute primero --generate en local.", file=sys.stderr)
        return 1

    if not statements:
        print("No hay sentencias UPDATE en el archivo SQL.")
        return 1

    print(f"Base de datos: {db_path.resolve()}")
    print(f"SQL:           {sql_path.resolve()}")
    print(f"Sentencias:    {len(statements)}")
    if dry_run:
        print("\n[DRY-RUN] No se modifica la base de datos.\n")

    conn = _connect(db_path)
    ok = 0
    skipped = 0
    errors = 0

    try:
        if not dry_run:
            conn.execute("BEGIN")

        for i, stmt in enumerate(statements, start=1):
            preview = stmt if len(stmt) <= 120 else stmt[:117] + "..."
            if dry_run:
                print(f"[{i}/{len(statements)}] {preview}")
                ok += 1
                continue

            try:
                cur = conn.execute(stmt)
                n = cur.rowcount
                if n > 0:
                    print(f"[{i}/{len(statements)}] OK ({n} fila(s)): {preview}")
                    ok += 1
                else:
                    print(
                        f"[{i}/{len(statements)}] SIN CAMBIO (0 filas; "
                        f"oem_norm/CODIGO no encontrado en esta BD): {preview}",
                        file=sys.stderr,
                    )
                    skipped += 1
            except sqlite3.Error as exc:
                print(f"[{i}/{len(statements)}] ERROR: {exc}\n  {preview}", file=sys.stderr)
                errors += 1
                if not dry_run:
                    conn.rollback()
                return 1

        if not dry_run:
            conn.commit()
            print("\nTransacción confirmada (COMMIT).")
    except Exception:
        if not dry_run:
            conn.rollback()
        raise
    finally:
        conn.close()

    print(f"\nResumen: {ok} aplicado(s), {skipped} sin filas coincidentes, {errors} error(es).")
    return 1 if errors else (0 if skipped == 0 else 2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help=f"Ruta SQLite (default: ANDES_DB_PATH o {ROOT / 'data' / 'andes.db'})",
    )
    parser.add_argument("--sql-path", type=Path, default=DEFAULT_SQL, help=f"Archivo SQL (default: {DEFAULT_SQL})")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--generate", action="store_true", help="Extrae URLs locales y escribe el .sql")
    mode.add_argument("--apply", action="store_true", help="Ejecuta el .sql en la BD (Render o local)")
    parser.add_argument("--dry-run", action="store_true", help="Con --apply: solo lista sentencias")
    args = parser.parse_args()

    db_path = (args.db_path or DEFAULT_DB).resolve()
    sql_path = args.sql_path.resolve()

    if args.generate:
        return cmd_generate(db_path, sql_path)
    return cmd_apply(db_path, sql_path, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
