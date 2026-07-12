"""Rutas de la app móvil separada (andes_mobile).

Orden de resolución:
1. ANDES_MOBILE_ROOT (env)
2. <raíz del ERP>/andes_mobile  (producción / Render / repo)
3. C:\\App movil andes          (atajo local / junction)

La UI y el server Python viven fuera de app/; el ERP solo los monta.
"""
from __future__ import annotations

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_LOCAL_ALIAS = Path(r"C:\App movil andes")


def _is_valid_mobile_root(root: Path) -> bool:
    try:
        return (root / "templates" / "mobile").is_dir() and (root / "static").is_dir()
    except OSError:
        return False


def mobile_ui_root() -> Path | None:
    raw = (os.environ.get("ANDES_MOBILE_ROOT") or "").strip()
    candidates: list[Path] = []
    if raw:
        candidates.append(Path(raw))
    candidates.append(_REPO_ROOT / "andes_mobile")
    candidates.append(_DEFAULT_LOCAL_ALIAS)

    for root in candidates:
        if _is_valid_mobile_root(root):
            try:
                return root.resolve()
            except OSError:
                return root
    return None


def mobile_templates_parent() -> Path | None:
    root = mobile_ui_root()
    return (root / "templates") if root else None


def mobile_static_dir() -> Path:
    root = mobile_ui_root()
    if root is not None:
        return root / "static"
    raise RuntimeError(
        "Andes Mobile UI no encontrada. Debe existir "
        f"{_REPO_ROOT / 'andes_mobile'} "
        r"(o C:\App movil andes / ANDES_MOBILE_ROOT)."
    )


def mobile_server_dir() -> Path:
    root = mobile_ui_root()
    if root is not None and (root / "server").is_dir():
        return (root / "server").resolve()
    candidate = _REPO_ROOT / "andes_mobile" / "server"
    if candidate.is_dir():
        return candidate.resolve()
    raise RuntimeError(f"Andes Mobile server no encontrado en {candidate}")
