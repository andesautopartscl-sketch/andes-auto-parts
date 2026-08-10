"""
Sincroniza la base local del PC hacia Render (app móvil / ERP online).

Dirección: solo PC → Render. No modifica andes.db local.
Uso:
  python scripts/sync_db_to_render.py
  python scripts/sync_db_to_render.py --force

Variables (.env):
  ANDES_DB_SYNC_TOKEN   token compartido con Render
  ANDES_RENDER_SYNC_URL URL completa del endpoint (default onrender)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["ANDES_SKIP_AUTO_CREATE_APP"] = "1"

from app.utils.load_env import load_project_dotenv  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("sync_db_to_render")

STATE_PATH = ROOT / "data" / "sync_to_render_state.json"
LOCK_PATH = ROOT / "data" / "sync_to_render.lock"
DB_PATH = ROOT / "data" / "andes.db"
DEFAULT_URL = "https://andes-auto-parts.onrender.com/admin/backups/sync"


def _fingerprint() -> dict:
    parts = []
    for name in ("andes.db", "andes.db-wal", "andes.db-shm"):
        p = ROOT / "data" / name
        if p.is_file():
            st = p.stat()
            parts.append({"name": name, "size": st.st_size, "mtime_ns": st.st_mtime_ns})
    return {"files": parts}


def _load_state() -> dict:
    if not STATE_PATH.is_file():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(payload: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _acquire_lock(ttl_sec: int = 900) -> bool:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    if LOCK_PATH.is_file():
        try:
            age = now - LOCK_PATH.stat().st_mtime
            if age < ttl_sec:
                return False
        except OSError:
            pass
        try:
            LOCK_PATH.unlink()
        except OSError:
            return False
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        return True
    except FileExistsError:
        return False


def _release_lock() -> None:
    try:
        LOCK_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def _build_zip(dest_zip: Path) -> int:
    """Copia limpia (incluye WAL) y empaqueta andes.db en un ZIP."""
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(ROOT / "data")) as tmp:
        snap = Path(tmp) / "andes.db"
        src = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True, timeout=60)
        dst = sqlite3.connect(str(snap))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        if dest_zip.exists():
            dest_zip.unlink()
        with zipfile.ZipFile(dest_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.write(snap, arcname="andes.db")
    return dest_zip.stat().st_size


def sync(*, force: bool = False) -> int:
    load_project_dotenv()
    token = (os.environ.get("ANDES_DB_SYNC_TOKEN") or "").strip()
    url = (os.environ.get("ANDES_RENDER_SYNC_URL") or DEFAULT_URL).strip()
    if not token or len(token) < 16:
        logger.error("Falta ANDES_DB_SYNC_TOKEN en .env (mín. 16 caracteres).")
        return 2
    if not DB_PATH.is_file():
        logger.error("No existe %s", DB_PATH)
        return 2

    fp = _fingerprint()
    state = _load_state()
    if not force and state.get("fingerprint") == fp and state.get("last_success"):
        logger.info("Sin cambios desde el último sync; se omite subida.")
        return 0

    if not _acquire_lock():
        logger.warning("Otro sync está en curso; se omite.")
        return 0

    zip_path = ROOT / "data" / "backups" / "andes_sync_upload.zip"
    try:
        logger.info("Generando snapshot limpio…")
        size = _build_zip(zip_path)
        logger.info("Subiendo %s (%.1f MB) → %s", zip_path.name, size / (1024 * 1024), url)

        import requests

        with open(zip_path, "rb") as fh:
            resp = requests.post(
                url,
                headers={"X-Andes-Sync-Token": token, "X-Requested-With": "XMLHttpRequest"},
                files={"archivo": ("andes_para_render.zip", fh, "application/zip")},
                timeout=180,
            )
        try:
            data = resp.json()
        except Exception:
            data = {"success": False, "message": resp.text[:500]}

        if resp.status_code != 200 or not data.get("success"):
            logger.error(
                "Sync falló HTTP %s: %s",
                resp.status_code,
                data.get("message") or data,
            )
            _save_state(
                {
                    "fingerprint": state.get("fingerprint"),
                    "last_success": False,
                    "last_error": data.get("message") or f"HTTP {resp.status_code}",
                    "last_attempt_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
            )
            return 1

        logger.info("OK: %s", data.get("message") or "restaurado")
        _save_state(
            {
                "fingerprint": fp,
                "last_success": True,
                "last_message": data.get("message"),
                "last_size_bytes": data.get("size_bytes") or size,
                "last_success_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
        )
        return 0
    except Exception as exc:
        logger.exception("Error en sync: %s", exc)
        return 1
    finally:
        _release_lock()
        try:
            zip_path.unlink(missing_ok=True)
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync unidireccional andes.db PC → Render")
    parser.add_argument("--force", action="store_true", help="Subir aunque no haya cambios")
    args = parser.parse_args()
    return sync(force=bool(args.force))


if __name__ == "__main__":
    raise SystemExit(main())
