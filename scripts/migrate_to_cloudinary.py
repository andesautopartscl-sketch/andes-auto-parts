#!/usr/bin/env python3
"""
Migra imágenes de app/static/ a Cloudinary.

Carpetas:
  productos_img/  → andes_erp/productos (+ BD productos)
  epc_despiece/   → andes_erp/epc_despiece (+ BD oem_despiece)
  productos360/   → andes_erp/productos360 (+ mapa CLOUDINARY_STATIC)
  img/            → andes_erp/img (+ mapa)
  icons/          → andes_erp/icons (+ mapa)

Uso:
  python scripts/migrate_to_cloudinary.py
  python scripts/migrate_to_cloudinary.py --include-productos-img
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = ROOT / "app" / "static"
OUTPUT_PY = ROOT / "app" / "utils" / "cloudinary_static_urls.py"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.utils.load_env import load_project_dotenv

load_project_dotenv(force=True)

from sqlalchemy import func, or_

from app.models import OemDespiece, Producto, ProductoImagen, SessionDB
from app.utils.cloudinary_config import is_configured, upload_image

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".ico"}

# Archivos de marca en raíz de static/ (referenciados en templates)
EXTRA_BRAND_FILES = ("logo_andes.png", "logo.png")


@dataclass
class FolderResult:
    label: str
    total: int = 0
    ok: int = 0
    fail: int = 0
    db_updated: int = 0
    skipped: int = 0
    urls: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def _iter_image_files(base: Path, recursive: bool = False) -> list[Path]:
    if not base.is_dir():
        return []
    if recursive:
        files = [
            p
            for p in base.rglob("*")
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS
        ]
    else:
        files = [p for p in base.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    return sorted(files, key=lambda p: str(p.relative_to(base)).lower())


def _static_key(local_path: Path, static_subdir: str) -> str:
    rel = local_path.relative_to(STATIC_ROOT / static_subdir)
    prefix = static_subdir.replace("\\", "/").strip("/")
    return f"{prefix}/{rel.as_posix()}"


def _public_id_for(folder: str, local_path: Path, static_subdir: str) -> str:
    if static_subdir == "productos360":
        rel = local_path.relative_to(STATIC_ROOT / "productos360")
        return f"{folder}/{rel.with_suffix('').as_posix()}"
    name = local_path.stem
    return f"{folder}/{name}"


def _upload_file(path: Path, cloud_folder: str, static_subdir: str) -> str:
    public_id = _public_id_for(cloud_folder, path, static_subdir)
    result = upload_image(path, public_id=public_id)
    url = (result.get("url") or "").strip()
    if not url:
        raise RuntimeError("Cloudinary no devolvió URL")
    return url


def _write_cloudinary_static_py(mapping: dict[str, str]) -> None:
    lines = [
        "# Generado por scripts/migrate_to_cloudinary.py — no editar a mano.",
        "# Ejecute el script de migración para actualizar.",
        "",
        "CLOUDINARY_STATIC: dict[str, str] = {",
    ]
    for key in sorted(mapping):
        url = mapping[key].replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'    "{key}": "{url}",')
    lines.append("}")
    lines.append("")
    OUTPUT_PY.write_text("\n".join(lines), encoding="utf-8")


def _parse_productos_filename(stem: str) -> tuple[str, str]:
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


def _apply_producto_url(sess, producto: Producto, url: str, kind: str) -> None:
    if kind == "despiece":
        producto.despiece = url
        return
    if kind == "principal":
        producto.imagen_url = url
    exists = (
        sess.query(ProductoImagen)
        .filter(
            ProductoImagen.producto_codigo == producto.codigo,
            ProductoImagen.ruta == url,
        )
        .first()
    )
    if not exists:
        sess.add(
            ProductoImagen(
                producto_codigo=producto.codigo,
                ruta=url,
                es_principal=(kind == "principal"),
            )
        )


def migrate_map_folder(
    static_subdir: str,
    cloud_folder: str,
    *,
    recursive: bool = False,
    label: str | None = None,
) -> FolderResult:
    res = FolderResult(label=label or static_subdir)
    base = STATIC_ROOT / static_subdir
    files = _iter_image_files(base, recursive=recursive)
    res.total = len(files)
    if not res.total:
        print(f"  [{res.label}] Sin archivos en {base}")
        return res

    print(f"\n=== {res.label} ({res.total} archivos) ===")
    for i, path in enumerate(files, start=1):
        key = _static_key(path, static_subdir) if static_subdir else path.name
        if static_subdir == "":
            key = path.name
        print(f"  Subiendo {i}/{res.total}: {key}")
        try:
            if static_subdir:
                url = _upload_file(path, cloud_folder, static_subdir)
            else:
                result = upload_image(path, public_id=f"{cloud_folder}/{path.stem}")
                url = (result.get("url") or "").strip()
            res.urls[key] = url
            res.ok += 1
        except Exception as exc:
            res.fail += 1
            msg = f"{key}: {exc}"
            res.errors.append(msg)
            print(f"    FALLÓ: {exc}")
    return res


def migrate_productos_img(sess, include: bool) -> FolderResult:
    res = FolderResult(label="productos_img")
    if not include:
        print("\n=== productos_img (omitido; use --include-productos-img) ===")
        res.skipped = 1
        return res

    base = STATIC_ROOT / "productos_img"
    files = _iter_image_files(base, recursive=False)
    res.total = len(files)
    print(f"\n=== productos_img ({res.total} archivos) ===")

    for i, path in enumerate(files, start=1):
        name = path.name
        print(f"  Subiendo {i}/{res.total}: {name}")
        try:
            code, kind = _parse_productos_filename(path.stem)
            url = _upload_file(path, "andes_erp/productos", "productos_img")
            res.urls[f"productos_img/{name}"] = url
            producto = _find_product(sess, code)
            if producto:
                _apply_producto_url(sess, producto, url, kind)
                res.db_updated += 1
                res.ok += 1
            else:
                print(f"    Aviso: sin producto para '{code}' — subida OK")
                res.ok += 1
        except Exception as exc:
            res.fail += 1
            res.errors.append(f"{name}: {exc}")
            print(f"    FALLÓ: {exc}")
    return res


def migrate_epc_despiece(sess) -> FolderResult:
    res = FolderResult(label="epc_despiece")
    base = STATIC_ROOT / "epc_despiece"
    files = _iter_image_files(base, recursive=False)
    res.total = len(files)
    print(f"\n=== epc_despiece ({res.total} archivos) ===")

    for i, path in enumerate(files, start=1):
        fname = path.name
        rel_old = f"epc_despiece/{fname}"
        stem = path.stem.upper()
        print(f"  Subiendo {i}/{res.total}: {fname}")
        try:
            url = _upload_file(path, "andes_erp/epc_despiece", "epc_despiece")
            res.urls[rel_old] = url
            res.ok += 1

            rows = (
                sess.query(OemDespiece)
                .filter(
                    or_(
                        OemDespiece.imagen_static == rel_old,
                        OemDespiece.imagen_static == fname,
                        OemDespiece.imagen_static.like(f"%{fname}"),
                        func.upper(OemDespiece.oem_norm) == stem,
                        func.upper(OemDespiece.producto_codigo) == stem,
                    )
                )
                .all()
            )
            seen_ids = set()
            for row in rows:
                if row.id in seen_ids:
                    continue
                seen_ids.add(row.id)
                row.imagen_static = url
                res.db_updated += 1

            for p in sess.query(Producto).filter(
                or_(
                    func.upper(func.trim(Producto.codigo)) == stem,
                    func.upper(func.trim(Producto.codigo_oem)) == stem,
                )
            ):
                if p.despiece in (rel_old, fname, None, ""):
                    p.despiece = url
                    res.db_updated += 1
        except Exception as exc:
            res.fail += 1
            res.errors.append(f"{fname}: {exc}")
            print(f"    FALLÓ: {exc}")
    return res


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrar imágenes static/ a Cloudinary")
    parser.add_argument(
        "--include-productos-img",
        action="store_true",
        help="Re-subir productos_img y actualizar BD (ya migrado por defecto se omite)",
    )
    args = parser.parse_args()

    if not is_configured():
        print("ERROR: Cloudinary no configurado. Revise CLOUDINARY_* en .env")
        return 1

    all_urls: dict[str, str] = {}
    results: list[FolderResult] = []

    sess = SessionDB()
    try:
        results.append(migrate_productos_img(sess, args.include_productos_img))
        results.append(migrate_epc_despiece(sess))
        sess.commit()
    except Exception as exc:
        sess.rollback()
        print(f"ERROR BD productos/epc: {exc}")
        return 1
    finally:
        sess.close()

    for subdir, folder in (
        ("productos360", "andes_erp/productos360"),
        ("img", "andes_erp/img"),
        ("icons", "andes_erp/icons"),
    ):
        r = migrate_map_folder(subdir, folder, recursive=(subdir == "productos360"))
        results.append(r)
        all_urls.update(r.urls)

    brand_res = FolderResult(label="logos (raíz static)")
    brand_files = [STATIC_ROOT / name for name in EXTRA_BRAND_FILES if (STATIC_ROOT / name).is_file()]
    brand_res.total = len(brand_files)
    print(f"\n=== logos raíz ({brand_res.total} archivos) ===")
    for i, path in enumerate(brand_files, start=1):
        print(f"  Subiendo {i}/{brand_res.total}: {path.name}")
        try:
            result = upload_image(path, public_id=f"andes_erp/brand/{path.stem}")
            url = (result.get("url") or "").strip()
            all_urls[path.name] = url
            brand_res.ok += 1
        except Exception as exc:
            brand_res.fail += 1
            print(f"    FALLÓ: {exc}")
    results.append(brand_res)

    for r in results:
        all_urls.update(r.urls)

    _write_cloudinary_static_py(all_urls)

    print("\n" + "=" * 60)
    print("RESUMEN POR CARPETA")
    print("=" * 60)
    total_ok = total_fail = total_files = 0
    for r in results:
        total_ok += r.ok
        total_fail += r.fail
        total_files += r.total
        print(f"\n{r.label}:")
        print(f"  Archivos:      {r.total}")
        print(f"  Subidas OK:    {r.ok}")
        print(f"  Fallidas:      {r.fail}")
        if r.db_updated:
            print(f"  BD actualizada:{r.db_updated}")
        if r.skipped:
            print("  (omitida)")
        if r.errors:
            print(f"  Errores ({len(r.errors)}):")
            for e in r.errors[:5]:
                print(f"    - {e}")
            if len(r.errors) > 5:
                print(f"    ... y {len(r.errors) - 5} más")

    print("\n" + "-" * 60)
    print(f"TOTAL archivos procesados: {total_files}")
    print(f"TOTAL subidas OK:          {total_ok}")
    print(f"TOTAL fallidas:            {total_fail}")
    print(f"Entradas en CLOUDINARY_STATIC: {len(all_urls)}")
    print(f"Archivo generado: {OUTPUT_PY}")

    sample = list(all_urls.items())[:8]
    if sample:
        print("\nMuestra de URLs:")
        for k, v in sample:
            print(f"  {k}")
            print(f"    → {v[:80]}...")

    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
