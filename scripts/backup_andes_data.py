"""
Copia de respaldo: base SQLite y (opcional) archivos de chat.
Uso: python scripts/backup_andes_data.py
     python scripts/backup_andes_data.py --keep 14
No modifica la aplicación; solo lee y escribe en data/backups/ .
"""
from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    data = root / "data"
    out_dir = data / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)

    parser = argparse.ArgumentParser(description="Respaldo de andes.db y chat_uploads (zip).")
    parser.add_argument("--keep", type=int, default=0, help="Mantener solo las N copias de DB más recientes (0 = no borrar).")
    args = parser.parse_args()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    db_src = data / "andes.db"
    if not db_src.is_file():
        print(f"ERROR: no existe {db_src}", file=sys.stderr)
        return 1

    db_dst = out_dir / f"andes_{ts}.db"
    shutil.copy2(db_src, db_dst)
    print(f"OK DB: {db_dst.name}")

    chat = data / "chat_uploads"
    if chat.is_dir() and any(chat.iterdir()):
        zpath = out_dir / f"chat_uploads_{ts}.zip"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in chat.rglob("*"):
                if f.is_file():
                    zf.write(f, f.relative_to(chat.parent))
        print(f"OK ZIP: {zpath.name}")
    else:
        print("Sin archivos en data/chat_uploads; solo se copió la DB.")

    if args.keep and args.keep > 0:
        dbs = sorted(out_dir.glob("andes_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in dbs[args.keep :]:
            try:
                old.unlink()
                print(f"Eliminada copia antigua: {old.name}")
            except OSError as e:
                print(f"Aviso: no se pudo eliminar {old.name}: {e}", file=sys.stderr)
        zips = sorted(out_dir.glob("chat_uploads_*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in zips[args.keep :]:
            try:
                old.unlink()
            except OSError:
                pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
