"""
Cuenta cuántas imágenes .jpg / .jpeg / .png en una carpeta coinciden con códigos del
catálogo (código interno, OEM, alternativos, homologados).

Uso:
  python scripts/count_pics_vs_catalog_codes.py [ruta_pics]

Por defecto: %USERPROFILE%\\OneDrive\\Desktop\\Pics
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from catalog_pic_match import (
    DB_PATH,
    DEFAULT_PICS,
    IMG_EXT,
    build_automatons,
    file_matches,
    load_all_codes,
)


def main() -> int:
    pics_root = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PICS
    if not pics_root.is_dir():
        print(f"No existe la carpeta: {pics_root}")
        return 1

    if not DB_PATH.is_file():
        print(f"No existe la base de datos: {DB_PATH}")
        return 1

    conn = sqlite3.connect(str(DB_PATH))
    try:
        codes = load_all_codes(conn)
    finally:
        conn.close()

    print(f"Base de datos: {DB_PATH}")
    print(f"Carpeta: {pics_root}")
    print(f"Códigos únicos (interno + OEM + alternativos + homologados, longitud >= 3): {len(codes)}")

    print("Construyendo autómatas…")
    a_sub, a_compact = build_automatons(codes)

    total_img = 0
    matched = 0
    for p in pics_root.iterdir():
        if not p.is_file():
            continue
        if p.suffix.lower() not in IMG_EXT:
            continue
        total_img += 1
        name_lower = p.name.lower()
        if file_matches(name_lower, a_sub, a_compact):
            matched += 1

    print()
    print(f"Imágenes .jpg / .jpeg / .png en la carpeta: {total_img}")
    print(f"Imágenes cuyo nombre coincide con al menos un código del catálogo: {matched}")
    if total_img:
        print(f"Porcentaje: {100.0 * matched / total_img:.2f}%")
    print(f"Sin coincidencia con catálogo: {total_img - matched}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
