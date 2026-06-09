#!/usr/bin/env python3
"""
Elimina referencias en BD a imágenes de producto ya borradas en Cloudinary.

Por defecto quita el formato legacy (una sola imagen plana por OEM):
  andes_erp/productos/E4G16-3707110
y conserva las de galería en subcarpeta:
  andes_erp/productos/E4G16-3707110/archivo

No modifica FTS5.
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_cfg_path = ROOT / "app" / "utils" / "cloudinary_config.py"
_cfg_spec = importlib.util.spec_from_file_location("cloudinary_config", _cfg_path)
_cfg = importlib.util.module_from_spec(_cfg_spec)
assert _cfg_spec.loader is not None
_cfg_spec.loader.exec_module(_cfg)

image_ref_dedupe_key = _cfg.image_ref_dedupe_key
public_id_from_url = _cfg.public_id_from_url

DB_PATH = ROOT / "data" / "andes.db"
engine = create_engine(
    f"sqlite:///{DB_PATH.as_posix()}",
    echo=False,
    connect_args={"check_same_thread": False, "timeout": 120},
)


@event.listens_for(engine, "connect")
def _sqlite_busy_timeout(dbapi_conn, _connection_record) -> None:
    dbapi_conn.execute("PRAGMA busy_timeout=120000")


def _sanitize_storage_key(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", (value or "").strip().upper()) or "producto"


def is_legacy_flat_product_image(ref: str, oem: str) -> bool:
    """True si la ref apunta al public_id plano antiguo (sin subcarpeta por archivo)."""
    pid = public_id_from_url(ref)
    if not pid:
        return False
    stem = _sanitize_storage_key(oem)
    legacy_pid = f"andes_erp/productos/{stem}".lower()
    return image_ref_dedupe_key(ref) == legacy_pid


def matches_public_id(ref: str, target_public_id: str) -> bool:
    key = image_ref_dedupe_key(ref)
    target = (target_public_id or "").strip().lower()
    return bool(key and target and key == target)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--oem", required=True, help="Código OEM, ej. E4G16-3707110")
    parser.add_argument(
        "--public-id",
        default="",
        help="Public id exacto a quitar (opcional). Si no se indica, quita solo el asset plano legacy del OEM.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Solo mostrar qué se borraría, sin modificar la BD.",
    )
    args = parser.parse_args()

    oem = (args.oem or "").strip().upper()
    if not oem:
        print("ERROR: OEM vacío", file=sys.stderr)
        return 1

    target_pid = (args.public_id or "").strip().lower()

    def should_remove(ref: str) -> bool:
        if target_pid:
            return matches_public_id(ref, target_pid)
        return is_legacy_flat_product_image(ref, oem)

    deleted_rows: list[tuple[int, str, str]] = []
    cleared_productos: list[str] = []
    promoted: list[tuple[str, int, str]] = []

    try:
        with engine.begin() as conn:
            sess = Session(bind=conn)

            img_rows = sess.execute(
                text(
                    """
                    SELECT pi.id, pi.producto_codigo, pi.ruta, pi.es_principal
                    FROM producto_imagenes pi
                    JOIN productos p ON pi.producto_codigo = p."CODIGO"
                    WHERE upper(trim(p."CODIGO OEM")) = :oem
                    ORDER BY pi.id
                    """
                ),
                {"oem": oem},
            ).fetchall()

            for row in img_rows:
                ruta = (row.ruta or "").strip()
                if not ruta or not should_remove(ruta):
                    continue
                deleted_rows.append((row.id, row.producto_codigo, ruta))
                if not args.dry_run:
                    sess.execute(
                        text("DELETE FROM producto_imagenes WHERE id = :id"),
                        {"id": row.id},
                    )

            prod_rows = sess.execute(
                text(
                    """
                    SELECT "CODIGO", imagen_url
                    FROM productos
                    WHERE upper(trim("CODIGO OEM")) = :oem
                      AND imagen_url IS NOT NULL
                      AND trim(imagen_url) != ''
                    """
                ),
                {"oem": oem},
            ).fetchall()

            for row in prod_rows:
                url = (row.imagen_url or "").strip()
                if not url or not should_remove(url):
                    continue
                cleared_productos.append(row.CODIGO)
                if not args.dry_run:
                    sess.execute(
                        text(
                            """
                            UPDATE productos
                            SET imagen_url = NULL
                            WHERE "CODIGO" = :codigo
                            """
                        ),
                        {"codigo": row.CODIGO},
                    )

            if not args.dry_run:
                owners = sorted({codigo for _, codigo, _ in deleted_rows})
                for codigo in owners:
                    principal = sess.execute(
                        text(
                            """
                            SELECT id, ruta FROM producto_imagenes
                            WHERE producto_codigo = :codigo
                            ORDER BY es_principal DESC, id ASC
                            LIMIT 1
                            """
                        ),
                        {"codigo": codigo},
                    ).fetchone()
                    if not principal:
                        continue
                    has_principal = sess.execute(
                        text(
                            """
                            SELECT 1 FROM producto_imagenes
                            WHERE producto_codigo = :codigo AND es_principal = 1
                            LIMIT 1
                            """
                        ),
                        {"codigo": codigo},
                    ).fetchone()
                    if has_principal:
                        continue
                    ruta = (principal.ruta or "").strip()
                    if not ruta:
                        continue
                    sess.execute(
                        text(
                            """
                            UPDATE producto_imagenes
                            SET es_principal = 1
                            WHERE id = :id
                            """
                        ),
                        {"id": principal.id},
                    )
                    sess.execute(
                        text(
                            """
                            UPDATE productos
                            SET imagen_url = :url
                            WHERE "CODIGO" = :codigo
                            """
                        ),
                        {"url": ruta, "codigo": codigo},
                    )
                    promoted.append((codigo, principal.id, ruta))

                owner_with_gallery = sess.execute(
                    text(
                        """
                        SELECT p."CODIGO"
                        FROM productos p
                        WHERE upper(trim(p."CODIGO OEM")) = :oem
                          AND EXISTS (
                            SELECT 1 FROM producto_imagenes pi
                            WHERE pi.producto_codigo = p."CODIGO"
                          )
                        ORDER BY p."CODIGO"
                        LIMIT 1
                        """
                    ),
                    {"oem": oem},
                ).fetchone()
                if owner_with_gallery:
                    portada = sess.execute(
                        text(
                            """
                            SELECT ruta FROM producto_imagenes
                            WHERE producto_codigo = :codigo AND es_principal = 1
                            LIMIT 1
                            """
                        ),
                        {"codigo": owner_with_gallery.CODIGO},
                    ).fetchone()
                    if portada and (portada.ruta or "").strip():
                        url = portada.ruta.strip()
                        sess.execute(
                            text(
                                """
                                UPDATE productos
                                SET imagen_url = :url
                                WHERE upper(trim("CODIGO OEM")) = :oem
                                  AND "CODIGO" != :owner
                                  AND (imagen_url IS NULL OR trim(imagen_url) = '')
                                """
                            ),
                            {"url": url, "oem": oem, "owner": owner_with_gallery.CODIGO},
                        )

            sess.flush()
    except Exception as exc:
        print(f"ERROR (rollback automático): {exc}", file=sys.stderr)
        return 1

    mode = "[DRY-RUN] " if args.dry_run else ""
    print(f"{mode}OEM: {oem}")
    print(f"{mode}Filas producto_imagenes eliminadas: {len(deleted_rows)}")
    for img_id, codigo, ruta in deleted_rows:
        preview = ruta if len(ruta) <= 90 else ruta[:87] + "..."
        print(f"  id={img_id} {codigo} {preview}")

    print(f"{mode}Productos con imagen_url limpiada: {len(cleared_productos)}")
    if cleared_productos and len(cleared_productos) <= 30:
        for codigo in cleared_productos:
            print(f"  {codigo}")
    elif cleared_productos:
        for codigo in cleared_productos[:10]:
            print(f"  {codigo}")
        print(f"  ... y {len(cleared_productos) - 10} más")

    if promoted:
        print(f"Nueva imagen principal asignada:")
        for codigo, img_id, ruta in promoted:
            preview = ruta if len(ruta) <= 80 else ruta[:77] + "..."
            print(f"  {codigo} id={img_id} {preview}")

    if args.dry_run:
        print("\nEjecutá sin --dry-run para aplicar los cambios.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
