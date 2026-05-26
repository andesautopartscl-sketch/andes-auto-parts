#!/usr/bin/env python3
"""
Migra imágenes de app/static/productos_img/ a Cloudinary y actualiza la BD.

Uso (desde la raíz del proyecto):
  python scripts/migrate_to_cloudinary.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.utils.load_env import load_project_dotenv

load_project_dotenv(force=True)

from sqlalchemy import func

from app.models import Producto, ProductoImagen, SessionDB
from app.utils.cloudinary_config import is_configured, upload_image


STATIC_DIR = ROOT / "app" / "static" / "productos_img"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _parse_filename(stem: str) -> tuple[str, str]:
    """
    Retorna (codigo_base, tipo) con tipo en ('principal', 'extra', 'despiece').
    Soporta: CODIGO.jpg, CODIGO_2.jpg, OEM-0.png, CODIGO_despiece.jpg
    """
    low = stem.lower()
    if "_despiece" in low:
        base = re.sub(r"_despiece.*$", "", stem, flags=re.I).strip().upper()
        return base, "despiece"
    m = re.match(r"^(.+)_(\d+)$", stem)
    if m:
        return m.group(1).strip().upper(), "extra"
    m_dash = re.match(r"^(.+)-(\d+)$", stem)
    if m_dash:
        kind = "principal" if m_dash.group(2) == "0" else "extra"
        return m_dash.group(1).strip().upper(), kind
    return stem.strip().upper(), "principal"


def _find_product(sess, code: str):
    if not code:
        return None
    p = sess.query(Producto).filter(func.upper(func.trim(Producto.codigo)) == code).first()
    if p:
        return p
    return (
        sess.query(Producto)
        .filter(func.upper(func.trim(Producto.codigo_oem)) == code)
        .first()
    )


def _apply_url(sess, producto: Producto, url: str, kind: str, filename: str) -> None:
    if kind == "despiece":
        producto.despiece = url
        return
    if kind == "principal":
        producto.imagen_url = url
    row = (
        sess.query(ProductoImagen)
        .filter(
            ProductoImagen.producto_codigo == producto.codigo,
            ProductoImagen.ruta == url,
        )
        .first()
    )
    if not row:
        sess.add(
            ProductoImagen(
                producto_codigo=producto.codigo,
                ruta=url,
                es_principal=(kind == "principal"),
            )
        )
    elif kind == "principal":
        row.es_principal = True
        row.ruta = url


def main() -> int:
    if not is_configured():
        print("ERROR: Cloudinary no configurado. Revise CLOUDINARY_* en .env")
        return 1

    if not STATIC_DIR.is_dir():
        print(f"ERROR: No existe carpeta {STATIC_DIR}")
        return 1

    files = sorted(
        f
        for f in STATIC_DIR.iterdir()
        if f.is_file() and f.suffix.lower() in IMAGE_EXTS
    )
    total = len(files)
    if not total:
        print("No hay imágenes en productos_img.")
        return 0

    print(f"Migrando {total} archivos desde {STATIC_DIR}")
    sess = SessionDB()
    ok = 0
    fail = 0
    sin_producto = 0

    try:
        for i, path in enumerate(files, start=1):
            name = path.name
            print(f"Subiendo imagen {i} de {total}: {name}")
            try:
                stem = path.stem
                code, kind = _parse_filename(stem)
                public_id = f"andes_erp/productos/{path.stem}"
                result = upload_image(path, public_id=public_id)
                url = (result.get("url") or "").strip()
                if not url:
                    raise RuntimeError("Cloudinary no devolvió URL")

                producto = _find_product(sess, code)
                if producto:
                    _apply_url(sess, producto, url, kind, name)
                    ok += 1
                else:
                    sin_producto += 1
                    print(f"  Aviso: sin producto para código/OEM '{code}' — subida OK, sin actualizar BD")
                    ok += 1
            except Exception as exc:
                fail += 1
                print(f"  FALLÓ: {exc}")

        sess.commit()
    except Exception as exc:
        sess.rollback()
        print(f"ERROR al guardar BD: {exc}")
        return 1
    finally:
        sess.close()

    print()
    print("=== Resumen migración Cloudinary ===")
    print(f"  Exitosas (subida): {ok}")
    print(f"  Fallidas:          {fail}")
    print(f"  Sin producto BD:   {sin_producto}")
    print(f"  Total archivos:    {total}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
