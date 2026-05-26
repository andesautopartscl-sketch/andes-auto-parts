#!/usr/bin/env python3
"""
Exporta filas oem_despiece desde la BD local y aplica INSERT OR REPLACE en producción.

Generar SQL:
  python scripts/sync_oem_despiece.py --generate

Aplicar (local / Render Shell):
  python scripts/sync_oem_despiece.py --apply

Producción sin Shell:
  GET /admin/sync-oem-despiece?token=<ANDES_SYNC_TOKEN>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.utils.oem_despiece_sync import (  # noqa: E402
    DEFAULT_OEM_NORMS,
    apply_oem_despiece_sql,
    generate_oem_despiece_sql_file,
    resolve_db_path,
    resolve_sql_path,
)


def cmd_generate(db_path: Path | None, sql_path: Path | None, oem_norms: list[str] | None) -> int:
    norms = tuple(oem_norms) if oem_norms else DEFAULT_OEM_NORMS
    try:
        stats = generate_oem_despiece_sql_file(norms, db_path, sql_path)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Base de datos: {stats['db_path']}")
    print(f"SQL generado:  {stats['sql_path']}")
    print(f"  Solicitados: {stats['solicitados']}")
    print(f"  Encontrados: {stats['encontrados']}")
    if stats["omitidos_generacion"]:
        print(f"  No en BD local: {', '.join(stats['omitidos_generacion'])}", file=sys.stderr)
    for oem in stats["oem_norms"]:
        print(f"    - {oem}")
    return 0 if stats["encontrados"] else 1


def cmd_apply(db_path: Path | None, sql_path: Path | None, dry_run: bool) -> int:
    result = apply_oem_despiece_sql(db_path, sql_path, dry_run=dry_run)
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
        f"{result.omitidos} sin cambio, {result.errores} error(es)."
    )
    return 1 if result.error else (0 if result.omitidos == 0 else 2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=None)
    parser.add_argument("--sql-path", type=Path, default=None)
    parser.add_argument(
        "--oem-norm",
        action="append",
        dest="oem_norms",
        metavar="OEM",
        help="oem_norm a exportar (repetible; default: lista fija de 8 registros)",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--generate", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db_path = resolve_db_path(args.db_path) if args.db_path else None
    sql_path = resolve_sql_path(args.sql_path) if args.sql_path else None

    if args.generate:
        return cmd_generate(db_path, sql_path, args.oem_norms)
    return cmd_apply(db_path, sql_path, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
