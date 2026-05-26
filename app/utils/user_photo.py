"""Fotos de perfil de usuarios del sistema (almacenamiento en data/usuarios_fotos/)."""
from __future__ import annotations

import io
from pathlib import Path

from flask import current_app, url_for
from werkzeug.utils import secure_filename

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MAX_BYTES = 3 * 1024 * 1024
THUMB_SIZE = (256, 256)


def photos_root() -> Path:
    root = Path(current_app.root_path).resolve().parent / "data" / "usuarios_fotos"
    root.mkdir(parents=True, exist_ok=True)
    return root


def photo_file_path(user_id: int) -> Path:
    return photos_root() / f"{int(user_id)}.jpg"


def user_has_photo(user) -> bool:
    if not user or not getattr(user, "id", None):
        return False
    return photo_file_path(int(user.id)).is_file()


def user_photo_url(user, uid: int | None = None) -> str | None:
    if user is None and uid is None:
        return None
    user_id = int(uid if uid is not None else user.id)
    if not photo_file_path(user_id).is_file():
        return None
    try:
        return url_for("seguridad.usuario_foto", uid=user_id)
    except RuntimeError:
        return None


def user_initials(user) -> str:
    name = (getattr(user, "nombre", None) or getattr(user, "usuario", None) or "?").strip()
    parts = name.split()
    if not parts:
        return "?"
    first = parts[0][0] if parts[0] else "?"
    last = parts[-1][0] if len(parts) > 1 and parts[-1] else ""
    return (first + last).upper()


def _allowed(filename: str) -> bool:
    ext = Path(filename or "").suffix.lower()
    return ext in ALLOWED_EXTENSIONS


def save_user_photo(user_id: int, file_storage) -> tuple[bool, str]:
    if file_storage is None or not getattr(file_storage, "filename", None):
        return False, "No se recibió archivo"

    filename = secure_filename(file_storage.filename or "")
    if not _allowed(filename):
        return False, "Formato no permitido. Usa JPG, PNG o WEBP."

    raw = file_storage.read()
    if not raw:
        return False, "Archivo vacío"
    if len(raw) > MAX_BYTES:
        return False, "La imagen no puede superar 3 MB"

    try:
        from PIL import Image

        img = Image.open(io.BytesIO(raw))
        img = img.convert("RGB")
        img.thumbnail(THUMB_SIZE, Image.Resampling.LANCZOS)
        dest = photo_file_path(user_id)
        img.save(dest, format="JPEG", quality=88, optimize=True)
    except Exception as exc:
        return False, f"No se pudo procesar la imagen: {exc}"

    return True, str(dest.name)


def delete_user_photo_file(user_id: int) -> None:
    path = photo_file_path(user_id)
    if path.is_file():
        path.unlink(missing_ok=True)
