"""
Mueve imágenes .jpg / .jpeg / .png que coinciden con códigos del catálogo desde la
carpeta Pics (OneDrive Desktop por defecto) hacia app/static/productos_img.

Misma regla de coincidencia que count_pics_vs_catalog_codes.py.
Nombres de destino: se elimina # inicial del nombre; si ya existe otro archivo con
el mismo nombre, se usa sufijo _dup1, _dup2, …

Uso:
  python scripts/move_matched_pics_to_productos.py [ruta_origen]
  python scripts/move_matched_pics_to_productos.py --dry-run [ruta_origen]

  Si no pasás ruta, usa OneDrive/Desktop/Pics.
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

from catalog_pic_match import (
    DB_PATH,
    DEFAULT_PICS,
    IMG_EXT,
    ROOT,
    build_automatons,
    file_matches,
    load_all_codes,
)

DEST_DIR = ROOT / "app" / "static" / "productos_img"


def normalize_dest_filename(filename: str) -> str:
    p = Path(filename)
    stem = p.stem.lstrip("#").strip() or p.stem
    ext = p.suffix.lower()
    if ext not in IMG_EXT:
        ext = p.suffix
    return f"{stem}{ext}"


def pick_destination(dest_dir: Path, filename: str) -> Path:
    """Si el nombre ya existe, añade _dupN antes de la extensión."""
    name = normalize_dest_filename(filename)
    candidate = dest_dir / name
    if not candidate.exists():
        return candidate
    p = Path(name)
    stem, ext = p.stem, p.suffix
    n = 1
    while True:
        cand = dest_dir / f"{stem}_dup{n}{ext}"
        if not cand.exists():
            return cand
        n += 1


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    argv = [a for a in sys.argv[1:] if a != "--dry-run"]
    pics_root = Path(argv[0]).resolve() if argv else DEFAULT_PICS

    if not pics_root.is_dir():
        print(f"No existe la carpeta de origen: {pics_root}")
        return 1

    if not DB_PATH.is_file():
        print(f"No existe la base de datos: {DB_PATH}")
        return 1

    dest_dir = DEST_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH))
    try:
        codes = load_all_codes(conn)
    finally:
        conn.close()

    a_sub, a_compact = build_automatons(codes)

    moved = 0
    skipped_not_matched = 0
    errors = 0

    log_path = ROOT / "data" / "move_matched_pics_log.txt"
    log_lines: list[str] = []
    log_lines.append(f"Origen: {pics_root}")
    log_lines.append(f"Destino: {dest_dir}")
    log_lines.append(f"Dry-run: {dry_run}")
    log_lines.append("")

    for src in sorted(pics_root.iterdir(), key=lambda p: p.name.lower()):
        if not src.is_file():
            continue
        if src.suffix.lower() not in IMG_EXT:
            continue
        name_lower = src.name.lower()
        if not file_matches(name_lower, a_sub, a_compact):
            skipped_not_matched += 1
            continue

        dst = pick_destination(dest_dir, src.name)
        try:
            if dry_run:
                log_lines.append(f"DRY {src.name} -> {dst.name}")
                moved += 1
            else:
                shutil.move(str(src), str(dst))
                moved += 1
                if moved % 5000 == 0:
                    print(f"  … {moved} archivos movidos")
        except OSError as e:
            errors += 1
            log_lines.append(f"ERROR {src.name}: {e}")

    summary = (
        f"Movidos: {moved}, no coinciden catálogo (omitidos): {skipped_not_matched}, errores: {errors}"
    )
    log_lines.append("")
    log_lines.append(summary)

    log_path.write_text("\n".join(log_lines), encoding="utf-8")

    print(summary)
    print(f"Log: {log_path}")
    return 0 if errors == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
