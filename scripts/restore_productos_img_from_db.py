"""Copia de Pics a productos_img solo archivos referenciados en BD (despiece, imagen_url, producto_imagenes)."""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "andes.db"
DEST = ROOT / "app" / "static" / "productos_img"
PICS = Path(os.environ.get("USERPROFILE", "")) / "OneDrive" / "Desktop" / "Pics"


def basename_from_ref(ref: str) -> str | None:
    r = (ref or "").strip().replace("\\", "/")
    if not r:
        return None
    if "productos_img/" in r.lower():
        m = re.search(r"productos_img/([^/?#]+)", r, re.I)
        if m:
            return m.group(1)
    # solo nombre
    if "/" not in r and "\\" not in r and r.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
        return Path(r).name
    return None


def main() -> int:
    conn = sqlite3.connect(str(DB))
    names: set[str] = set()

    for codigo, despiece in conn.execute(
        "SELECT CODIGO, despiece FROM productos WHERE despiece IS NOT NULL AND TRIM(despiece) != ''"
    ):
        b = basename_from_ref(despiece or "")
        if b:
            names.add(b)

    for codigo, url in conn.execute(
        "SELECT CODIGO, imagen_url FROM productos WHERE imagen_url IS NOT NULL AND TRIM(imagen_url) != ''"
    ):
        b = basename_from_ref(url or "")
        if b:
            names.add(b)

    try:
        for pc, ruta in conn.execute(
            "SELECT producto_codigo, ruta FROM producto_imagenes WHERE ruta IS NOT NULL AND TRIM(ruta) != ''"
        ):
            b = basename_from_ref(ruta or "")
            if b:
                names.add(b)
    except sqlite3.OperationalError:
        pass

    conn.close()

    DEST.mkdir(parents=True, exist_ok=True)

    def find_in_pics(want: str) -> Path | None:
        """Nombre exacto en Pics, o con # prefijo, o solo stem."""
        candidates = [
            PICS / want,
            PICS / f"#{want}",
            PICS / want.lower(),
            PICS / f"#{want}".lower(),
        ]
        for p in candidates:
            if p.is_file():
                return p
        stem = Path(want).stem
        ext = Path(want).suffix
        for p in PICS.iterdir():
            if not p.is_file():
                continue
            if p.stem.lstrip("#").lower() == stem.lower() and p.suffix.lower() == ext.lower():
                return p
        return None

    copied = 0
    missing: list[str] = []
    for name in sorted(names):
        src = find_in_pics(name)
        if not src:
            missing.append(name)
            continue
        dst = DEST / name
        if dst.exists():
            continue
        shutil.copy2(src, dst)
        copied += 1

    print(f"Referencias únicas en BD: {len(names)}")
    print(f"Copiadas a productos_img: {copied}")
    print(f"No encontradas en {PICS}: {len(missing)}")
    if missing:
        print("Faltantes:", missing[:40])
        if len(missing) > 40:
            print("...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
