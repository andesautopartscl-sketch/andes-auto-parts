#!/usr/bin/env python3
"""
Backfill one-shot: copia imagen_url desde producto_imagenes.es_principal=1
de otro producto activo con el mismo OEM.

No modifica FTS5 (imagen_url no está en el índice de búsqueda).
"""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "data" / "andes.db"
engine = create_engine(
    f"sqlite:///{DB_PATH.as_posix()}",
    echo=False,
    connect_args={"check_same_thread": False, "timeout": 120},
)


@event.listens_for(engine, "connect")
def _sqlite_busy_timeout(dbapi_conn, _connection_record) -> None:
    dbapi_conn.execute("PRAGMA busy_timeout=120000")

SQL_PRINCIPALES_POR_OEM = text(
    """
    SELECT upper(trim(p."CODIGO OEM")) AS oem, pi.ruta
    FROM producto_imagenes pi
    JOIN productos p ON pi.producto_codigo = p."CODIGO"
    WHERE p."ACTIVO" = 1
      AND p."CODIGO OEM" IS NOT NULL
      AND trim(p."CODIGO OEM") != ''
      AND pi.es_principal = 1
      AND pi.ruta IS NOT NULL
      AND trim(pi.ruta) != ''
    ORDER BY pi.id ASC
    """
)

SQL_CANDIDATOS = text(
    """
    SELECT p."CODIGO" AS codigo, upper(trim(p."CODIGO OEM")) AS oem
    FROM productos p
    WHERE p."ACTIVO" = 1
      AND (p.imagen_url IS NULL OR trim(p.imagen_url) = '')
      AND p."CODIGO OEM" IS NOT NULL
      AND trim(p."CODIGO OEM") != ''
    ORDER BY p."CODIGO"
    """
)

SQL_UPDATE = text(
    """
    UPDATE productos
    SET imagen_url = :url
    WHERE "CODIGO" = :codigo
      AND (imagen_url IS NULL OR trim(imagen_url) = '')
    """
)


def main() -> int:
    updated: list[tuple[str, str, str]] = []
    sin_principal: list[tuple[str, str]] = []

    try:
        with engine.begin() as conn:
            sess = Session(bind=conn)

            principal_por_oem: dict[str, str] = {}
            for row in sess.execute(SQL_PRINCIPALES_POR_OEM):
                oem = (row.oem or "").strip()
                url = (row.ruta or "").strip()
                if oem and url and oem not in principal_por_oem:
                    principal_por_oem[oem] = url

            for row in sess.execute(SQL_CANDIDATOS):
                codigo = (row.codigo or "").strip()
                oem = (row.oem or "").strip()
                if not codigo or not oem:
                    continue

                url = principal_por_oem.get(oem)
                if not url:
                    sin_principal.append((codigo, oem))
                    continue

                updated.append((codigo, oem, url))

            if updated:
                sess.execute(
                    SQL_UPDATE,
                    [{"codigo": codigo, "url": url} for codigo, _oem, url in updated],
                )

            sess.flush()
    except Exception as exc:
        print(f"ERROR (rollback automático): {exc}", file=sys.stderr)
        return 1

    print(f"Productos actualizados: {len(updated)}")
    for codigo, oem, url in updated:
        preview = url if len(url) <= 90 else url[:87] + "..."
        print(f"  {codigo}  OEM={oem}  url={preview}")

    if sin_principal:
        print(f"\nSin imagen principal OEM (omitidos): {len(sin_principal)}")
        for codigo, oem in sin_principal[:30]:
            print(f"  {codigo}  OEM={oem}")
        if len(sin_principal) > 30:
            print(f"  ... y {len(sin_principal) - 30} más")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
