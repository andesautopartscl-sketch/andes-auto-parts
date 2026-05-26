"""Analiza ctimes de jpg coincidentes en Pics para detectar posible cluster de ~21 archivos."""
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from catalog_pic_match import (  # noqa: E402
    DB_PATH,
    DEFAULT_PICS,
    IMG_EXT,
    build_automatons,
    file_matches,
    load_all_codes,
)

PICS = Path(os.environ.get("USERPROFILE", "")) / "OneDrive" / "Desktop" / "Pics"


def main() -> None:
    conn = sqlite3.connect(str(DB_PATH))
    codes = load_all_codes(conn)
    conn.close()
    a_sub, a_compact = build_automatons(codes)

    rows: list[tuple[str, float, float]] = []
    for p in PICS.iterdir():
        if not p.is_file() or p.suffix.lower() not in IMG_EXT:
            continue
        if not file_matches(p.name.lower(), a_sub, a_compact):
            continue
        st = p.stat()
        rows.append((p.name, st.st_ctime, st.st_mtime))

    rows.sort(key=lambda x: x[1])
    print(f"Matched files: {len(rows)}")
    if len(rows) < 25:
        for r in rows:
            print(r)
        return

    # Diferencias entre ctimes consecutivos
    gaps = []
    for i in range(1, len(rows)):
        gaps.append((rows[i][0], rows[i][1] - rows[i - 1][1], rows[i][1]))

    # Los 21 saltos más grandes (podrían separar grupos)
    gaps.sort(key=lambda x: -x[1])
    print("\nTop 30 largest ctime gaps between consecutive sorted files:")
    for g in gaps[:30]:
        print(f"  gap {g[1]:.1f}s after prev  file={g[0]}")

    # Últimos 25 por ctime (más recientes)
    print("\n25 most recent by ctime (name, ctime):")
    for name, ct, mt in sorted(rows, key=lambda x: -x[1])[:25]:
        print(f"  {name}  ctime={ct}")

    print("\n25 oldest by ctime:")
    for name, ct, mt in rows[:25]:
        print(f"  {name}  ctime={ct}")


if __name__ == "__main__":
    main()
