"""
Restaura en app/static/productos_img las ~21 imágenes que estaban en el proyecto
antes del traslado masivo: son las 21 coincidencias con catálogo con ctime más
antiguo en OneDrive Pics (el lote masivo quedó con ctimes más recientes / otros
patrones como #Копия…).

Copia desde Pics (no borra en Pics). Nombres destino sin '#' inicial.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from catalog_pic_match import (  # noqa: E402
    DB_PATH,
    IMG_EXT,
    build_automatons,
    file_matches,
    load_all_codes,
)

PICS = Path(os.environ.get("USERPROFILE", "")) / "OneDrive" / "Desktop" / "Pics"
DEST = ROOT / "app" / "static" / "productos_img"
COUNT = 21


def norm_dest(name: str) -> str:
    p = Path(name)
    stem = p.stem.lstrip("#").strip() or p.stem
    ext = p.suffix.lower()
    if ext not in IMG_EXT:
        ext = p.suffix
    return f"{stem}{ext}"


def main() -> int:
    conn = sqlite3.connect(str(DB_PATH))
    codes = load_all_codes(conn)
    conn.close()
    a_sub, a_compact = build_automatons(codes)

    rows: list[tuple[Path, float]] = []
    for p in PICS.iterdir():
        if not p.is_file() or p.suffix.lower() not in IMG_EXT:
            continue
        if not file_matches(p.name.lower(), a_sub, a_compact):
            continue
        rows.append((p, p.stat().st_ctime))

    rows.sort(key=lambda x: x[1])
    pick = rows[:COUNT]

    DEST.mkdir(parents=True, exist_ok=True)

    print(f"Restaurando las {len(pick)} imágenes con ctime más antiguo → {DEST}\n")
    for src, _ct in pick:
        dst_name = norm_dest(src.name)
        dst = DEST / dst_name
        shutil.copy2(src, dst)
        print(f"  {src.name}  →  {dst_name}")

    print(f"\nListo: {len(pick)} archivos copiados.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
