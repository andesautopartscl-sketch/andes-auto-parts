"""
Revierte el traslado masivo: mueve todo desde app/static/productos_img hacia la carpeta
Pics de OneDrive, anteponiendo '#' al nombre del archivo (convención original).

Si ya existe un archivo con el mismo nombre en destino, usa sufijo _revertDupN.

Uso:
  python scripts/revert_productos_img_to_pics.py [ruta_destino_pics]
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "app" / "static" / "productos_img"
DEFAULT_PICS = Path(os.environ.get("USERPROFILE", "")) / "OneDrive" / "Desktop" / "Pics"

IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def pick_unique(dest_dir: Path, name: str) -> Path:
    c = dest_dir / name
    if not c.exists():
        return c
    stem = Path(name).stem
    ext = Path(name).suffix
    n = 1
    while True:
        cand = dest_dir / f"{stem}_revertDup{n}{ext}"
        if not cand.exists():
            return cand
        n += 1


def main() -> int:
    dest = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_PICS
    if not SRC.is_dir():
        print(f"No existe origen: {SRC}")
        return 1
    dest.mkdir(parents=True, exist_ok=True)

    moved = 0
    errors = 0
    for src in sorted(SRC.iterdir(), key=lambda p: p.name.lower()):
        if not src.is_file():
            continue
        ext = src.suffix.lower()
        if ext not in IMG_EXT:
            continue
        base = src.name
        if base.startswith("#"):
            new_name = base
        else:
            new_name = "#" + base
        dst = pick_unique(dest, new_name)
        try:
            shutil.move(str(src), str(dst))
            moved += 1
            if moved % 5000 == 0:
                print(f"  … {moved}")
        except OSError as e:
            errors += 1
            print(f"ERROR {src.name}: {e}")

    print(f"Listo: movidos {moved} archivos a {dest}, errores: {errors}")
    return 0 if errors == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
