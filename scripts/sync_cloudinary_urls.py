#!/usr/bin/env python3
"""
Sincroniza URLs de Cloudinary (epc_despiece / producto.despiece) desde la BD local hacia Render.

Generar SQL desde la máquina de desarrollo:
  python scripts/sync_cloudinary_urls.py --generate

Aplicar (local o Render Shell si está disponible):
  python scripts/sync_cloudinary_urls.py --apply

Producción sin Shell (plan gratuito Render):
  GET /admin/sync-cloudinary-urls?token=<ANDES_SYNC_TOKEN>

Variables de entorno:
  ANDES_DB_PATH — ruta a andes.db
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.utils.cloudinary_url_sync import (  # noqa: E402
    apply_cloudinary_url_sync,
    generate_sync_sql_file,
    resolve_db_path,
    resolve_sql_path,
)


def cmd_generate(db_path: Path | None, sql_path: Path | None) -> int:
    try:
        stats = generate_sync_sql_file(db_path, sql_path)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Base de datos: {stats['db_path']}")
    print(f"SQL generado:  {stats['sql_path']}")
    print(f"  oem_despiece (por oem_norm): {stats['oem_despiece']} UPDATE(s)")
    print(f"  productos (por CODIGO):      {stats['productos']} UPDATE(s)")
    print(f"  Total:                       {stats['total']} UPDATE(s)")
    if stats["total"] == 0:
        print("AVISO: no hay filas con 'cloudinary' en imagen_static / despiece.")
    return 0


def cmd_apply(db_path: Path | None, sql_path: Path | None, dry_run: bool) -> int:
    result = apply_cloudinary_url_sync(db_path, sql_path, dry_run=dry_run)

    print(f"Base de datos: {result.db_path}")
    print(f"SQL:           {result.sql_path}")
    print(f"Sentencias:    {result.total}")
    if dry_run:
        print("\n[DRY-RUN] No se modifica la base de datos.\n")

    for i, line in enumerate(result.detalles, start=1):
        print(f"[{i}/{result.total}] {line}")

    if result.error:
        print(f"\nERROR: {result.error}", file=sys.stderr)

    print(
        f"\nResumen: {result.actualizados} aplicado(s), "
        f"{result.omitidos} sin filas coincidentes, {result.errores} error(es)."
    )
    if result.error:
        return 1
    return 0 if result.omitidos == 0 else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=None)
    parser.add_argument("--sql-path", type=Path, default=None)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--generate", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db_path = resolve_db_path(args.db_path) if args.db_path else None
    sql_path = resolve_sql_path(args.sql_path) if args.sql_path else None

    if args.generate:
        return cmd_generate(db_path, sql_path)
    return cmd_apply(db_path, sql_path, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
