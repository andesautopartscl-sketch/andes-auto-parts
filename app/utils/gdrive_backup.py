"""Backup de SQLite a Google Drive con retención configurable."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.utils.datetime_utils import CHILE_TZ

logger = logging.getLogger(__name__)

STATUS_FILENAME = "gdrive_backup_status.json"
BACKUP_NAME_PREFIX = "andes_backup_"


@dataclass
class BackupResult:
    success: bool
    message: str
    filename: str = ""
    size_bytes: int = 0
    deleted_count: int = 0
    drive_file_id: str = ""
    ran_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def get_backup_config() -> dict[str, Any]:
    root = project_root()

    oauth_raw = (os.environ.get("GDRIVE_OAUTH_CREDENTIALS") or "data/gdrive_oauth_credentials.json").strip()
    oauth_path = Path(oauth_raw)
    if not oauth_path.is_absolute():
        oauth_path = root / oauth_path

    token_raw = (os.environ.get("GDRIVE_TOKEN_PATH") or "data/gdrive_token.json").strip()
    token_path = Path(token_raw)
    if not token_path.is_absolute():
        token_path = root / token_path

    folder_id = (os.environ.get("GDRIVE_FOLDER_ID") or "").strip()
    try:
        retention_days = int((os.environ.get("BACKUP_RETENTION_DAYS") or "30").strip() or "30")
    except ValueError:
        retention_days = 30
    return {
        "oauth_credentials_path": oauth_path,
        "token_path": token_path,
        "folder_id": folder_id,
        "retention_days": max(0, retention_days),
        "db_path": root / "data" / "andes.db",
        "temp_dir": root / "data" / "backups",
        "status_path": root / "data" / STATUS_FILENAME,
    }


def format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{num_bytes} B"


def load_last_status(status_path: Path | None = None) -> dict[str, Any] | None:
    path = status_path or get_backup_config()["status_path"]
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_status(status_path: Path, result: BackupResult) -> None:
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _create_db_snapshot(db_path: Path, dest_db: Path) -> None:
    dest_db.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=60)
    try:
        dst = sqlite3.connect(str(dest_db))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _build_drive(*, oauth_credentials_path: Path, token_path: Path, interactive: bool) -> Any:
    """Crea servicio Drive API v3 usando token OAuth (usuario)."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token_path = Path(token_path)
    if not token_path.is_file():
        if interactive:
            raise RuntimeError(
                "Token OAuth no encontrado. Ejecuta: python scripts/setup_gdrive_token.py"
            )
        raise RuntimeError(
            "Token OAuth no encontrado. Ejecuta: python scripts/setup_gdrive_token.py"
        )

    creds = Credentials.from_authorized_user_file(
        str(token_path),
        scopes=["https://www.googleapis.com/auth/drive.file"],
    )

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")

    return build("drive", "v3", credentials=creds, cache_discovery=False)


def authorize_gdrive_interactive(
    oauth_credentials_path: Path | str | None = None,
    token_path: Path | str | None = None,
) -> None:
    """Compat: ya no se usa (usar scripts/setup_gdrive_token.py)."""
    raise RuntimeError("Usa: python scripts/setup_gdrive_token.py")


def list_drive_backups(
    folder_id: str | None = None,
    oauth_credentials_path: Path | str | None = None,
    token_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    cfg = get_backup_config()
    folder_id = folder_id or cfg["folder_id"]
    token_path = Path(token_path or cfg["token_path"])
    if not folder_id:
        return []

    results: list[dict[str, Any]] = []
    service = _build_drive(oauth_credentials_path=Path("."), token_path=token_path, interactive=False)
    query = f"'{folder_id}' in parents and trashed=false and name contains '{BACKUP_NAME_PREFIX}'"

    page_token = None
    while True:
        resp = (
            service.files()
            .list(
                q=query,
                fields="nextPageToken, files(id, name, size, createdTime, modifiedTime)",
                orderBy="createdTime desc",
                pageSize=100,
                pageToken=page_token,
            )
            .execute()
        )
        for item in resp.get("files", []):
            size_bytes = int(item.get("size") or 0)
            results.append(
                {
                    "id": item.get("id", ""),
                    "name": item.get("name", ""),
                    "size_bytes": size_bytes,
                    "size_display": format_size(size_bytes),
                    "created_at": item.get("createdTime", ""),
                    "modified_at": item.get("modifiedTime", ""),
                }
            )
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return results


def download_drive_file(
    file_id: str,
    oauth_credentials_path: Path | str | None = None,
    token_path: Path | str | None = None,
) -> tuple[bytes, str]:
    cfg = get_backup_config()
    token_path = Path(token_path or cfg["token_path"])
    service = _build_drive(oauth_credentials_path=Path("."), token_path=token_path, interactive=False)

    meta = service.files().get(fileId=file_id, fields="name").execute()
    filename = meta.get("name") or "backup.zip"

    from googleapiclient.http import MediaIoBaseDownload
    import io

    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue(), filename


def _delete_old_backups(service, folder_id: str, retention_days: int) -> int:
    if retention_days <= 0:
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    deleted = 0
    query = f"'{folder_id}' in parents and trashed=false and name contains '{BACKUP_NAME_PREFIX}'"
    page_token = None
    while True:
        resp = (
            service.files()
            .list(
                q=query,
                fields="nextPageToken, files(id, name, createdTime)",
                orderBy="createdTime desc",
                pageSize=100,
                pageToken=page_token,
            )
            .execute()
        )
        for item in resp.get("files", []):
            created_raw = item.get("createdTime") or ""
            if not created_raw:
                continue
            created = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            if created >= cutoff:
                continue
            try:
                service.files().delete(fileId=item["id"]).execute()
                deleted += 1
                logger.info("Eliminado backup antiguo en Drive: %s", item.get("name"))
            except Exception as exc:
                logger.warning("No se pudo eliminar backup antiguo (%s): %s", item.get("name"), exc)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return deleted


def run_gdrive_backup(*, logger_instance: logging.Logger | None = None) -> BackupResult:
    log = logger_instance or logger
    cfg = get_backup_config()
    ran_at = datetime.now(CHILE_TZ).isoformat()

    if not cfg["folder_id"]:
        result = BackupResult(success=False, message="GDRIVE_FOLDER_ID no configurado", ran_at=ran_at)
        _save_status(cfg["status_path"], result)
        log.error(result.message)
        return result

    oauth_path = Path(cfg["oauth_credentials_path"])
    token_path = Path(cfg["token_path"])
    if not oauth_path.is_file():
        result = BackupResult(
            success=False,
            message=f"Credenciales OAuth no encontradas: {oauth_path}",
            ran_at=ran_at,
        )
        _save_status(cfg["status_path"], result)
        log.error(result.message)
        return result
    if not token_path.is_file():
        result = BackupResult(
            success=False,
            message="Token OAuth de Google Drive no encontrado. Ejecuta: python scripts/setup_gdrive_token.py",
            ran_at=ran_at,
        )
        _save_status(cfg["status_path"], result)
        log.error(result.message)
        return result

    db_path = Path(cfg["db_path"])
    if not db_path.is_file():
        result = BackupResult(
            success=False,
            message=f"Base de datos no encontrada: {db_path}",
            ran_at=ran_at,
        )
        _save_status(cfg["status_path"], result)
        log.error(result.message)
        return result

    ts = datetime.now().strftime("%Y-%m-%d_%H-%M")
    backup_db_name = f"{BACKUP_NAME_PREFIX}{ts}.db"
    zip_name = f"{BACKUP_NAME_PREFIX}{ts}.zip"
    temp_dir = Path(cfg["temp_dir"])
    temp_dir.mkdir(parents=True, exist_ok=True)
    local_db = temp_dir / backup_db_name
    local_zip = temp_dir / zip_name

    try:
        log.info("Creando copia de %s", db_path.name)
        _create_db_snapshot(db_path, local_db)

        with zipfile.ZipFile(local_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(local_db, arcname=backup_db_name)
        local_db.unlink(missing_ok=True)

        zip_size = local_zip.stat().st_size
        service = _build_drive(oauth_credentials_path=oauth_path, token_path=token_path, interactive=False)

        from googleapiclient.http import MediaFileUpload

        media = MediaFileUpload(str(local_zip), mimetype="application/zip", resumable=True)
        uploaded = (
            service.files()
            .create(
                body={"name": zip_name, "parents": [cfg["folder_id"]]},
                media_body=media,
                fields="id",
            )
            .execute()
        )

        deleted_count = _delete_old_backups(service, cfg["folder_id"], cfg["retention_days"])

        result = BackupResult(
            success=True,
            message="Backup subido correctamente a Google Drive",
            filename=zip_name,
            size_bytes=zip_size,
            deleted_count=deleted_count,
            drive_file_id=uploaded.get("id", "") or "",
            ran_at=ran_at,
        )
        log.info(
            "Backup OK | fecha=%s | archivo=%s | tamaño=%s | eliminados=%s",
            ran_at,
            zip_name,
            format_size(zip_size),
            deleted_count,
        )
        _save_status(cfg["status_path"], result)
        return result
    except Exception as exc:
        result = BackupResult(success=False, message=str(exc), ran_at=ran_at)
        log.exception("Error en backup a Google Drive: %s", exc)
        _save_status(cfg["status_path"], result)
        return result
    finally:
        # En Windows, MediaFileUpload puede mantener el archivo abierto.
        try:
            fd = getattr(media, "_fd", None)  # type: ignore[name-defined]
            if fd:
                fd.close()
        except Exception:
            pass
        local_db.unlink(missing_ok=True)
        try:
            local_zip.unlink(missing_ok=True)
        except PermissionError:
            # Si el SO mantiene el handle un momento, dejamos el zip para limpieza posterior.
            pass
