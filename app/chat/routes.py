from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from flask import Blueprint, abort, jsonify, request, send_file, session, url_for
from sqlalchemy import and_, func, or_
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from app.extensions import db
from app.seguridad.models import Usuario
from app.utils.decorators import login_required
from .models import ChatMessage

chat_bp = Blueprint("chat", __name__, url_prefix="/chat")
ONLINE_WINDOW_SECONDS = 120
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
UPLOAD_ROOT = Path(__file__).resolve().parents[2] / "data" / "chat_uploads"
ALLOWED_EXTENSIONS = {
    "pdf", "png", "jpg", "jpeg", "gif", "webp", "bmp", "svg",
    "doc", "docx", "xls", "xlsx", "csv", "txt", "rtf", "odt", "ods", "ppt", "pptx",
    "mp3", "wav", "ogg", "m4a", "aac", "webm", "mp4",
}


def _current_user() -> Usuario | None:
    username = (session.get("user") or "").strip()
    if not username:
        return None
    return Usuario.query.filter_by(usuario=username).first()


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clean_text(value: str | None) -> str:
    return (value or "").strip()


def _is_allowed_extension(filename: str) -> bool:
    if "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in ALLOWED_EXTENSIONS


def _message_type_for_upload(mime: str, force_audio: bool = False) -> str:
    mime = _clean_text(mime).lower()
    if force_audio or mime.startswith("audio/"):
        return "audio"
    if mime.startswith("image/"):
        return "image"
    return "file"


def _ensure_upload_root() -> None:
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)


def _save_upload(file_obj: FileStorage, force_audio: bool = False) -> dict:
    _ensure_upload_root()

    original_name = secure_filename(file_obj.filename or "")
    if not original_name:
        raise ValueError("Archivo invalido")
    if not _is_allowed_extension(original_name):
        raise ValueError("Tipo de archivo no permitido")

    now = datetime.utcnow()
    shard_dir = Path(str(now.year), f"{now.month:02d}")
    final_dir = UPLOAD_ROOT / shard_dir
    final_dir.mkdir(parents=True, exist_ok=True)

    ext = original_name.rsplit(".", 1)[1].lower()
    stored_name = f"{now.strftime('%Y%m%d%H%M%S')}_{uuid4().hex[:14]}.{ext}"
    full_path = final_dir / stored_name
    file_obj.save(str(full_path))

    size = full_path.stat().st_size if full_path.exists() else 0
    if size <= 0:
        try:
            full_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise ValueError("Archivo vacio")
    if size > MAX_UPLOAD_BYTES:
        try:
            full_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise ValueError("Archivo excede el tamano maximo")

    mime = _clean_text(file_obj.mimetype) or "application/octet-stream"
    message_type = _message_type_for_upload(mime, force_audio=force_audio)
    relative_path = (shard_dir / stored_name).as_posix()

    return {
        "message_type": message_type,
        "media_path": relative_path,
        "media_name": original_name,
        "media_mime": mime,
        "media_size": int(size),
    }


def _resolve_media_path(relative_path: str) -> Path | None:
    rel = _clean_text(relative_path)
    if not rel:
        return None
    target = (UPLOAD_ROOT / rel).resolve()
    root = UPLOAD_ROOT.resolve()
    if not str(target).startswith(str(root)):
        return None
    return target


def _can_access_message(message: ChatMessage, user_id: int) -> bool:
    return message.sender_id == user_id or message.receiver_id == user_id


def _serialize_message(message: ChatMessage, current_user_id: int) -> dict:
    data = message.to_dict(current_user_id)
    msg_type = data.get("message_type") or "text"
    
    # Check if message is deleted for current user
    is_deleted_for_user = False
    if message.deleted_for_all:
        is_deleted_for_user = True
    elif current_user_id == message.sender_id and message.deleted_for_sender:
        is_deleted_for_user = True
    elif current_user_id == message.receiver_id and message.deleted_for_receiver:
        is_deleted_for_user = True
    
    data["is_deleted"] = is_deleted_for_user
    data["is_text"] = msg_type == "text"
    data["is_audio"] = msg_type == "audio"
    data["is_image"] = msg_type == "image"
    data["is_file"] = msg_type == "file"
    
    if message.media_path:
        data["media_url"] = url_for("chat.chat_media_view", message_id=message.id)
        data["media_download_url"] = url_for("chat.chat_media_download", message_id=message.id)
    else:
        data["media_url"] = None
        data["media_download_url"] = None
    
    # Compute status indicator based on message state
    status_map = {
        "sent": "✔",
        "delivered": "✔✔",
        "read": "✔✔",
    }
    data["status_icon"] = status_map.get(message.status or "sent", "✔")
    data["status_color"] = "blue" if message.status == "read" else "gray"
    data["is_edited"] = bool(message.edited_at)
    data["can_delete_for_all"] = bool(message.sender_id == current_user_id and not message.deleted_for_all)
    
    return data


def _touch_presence(user: Usuario | None) -> None:
    if user is None:
        return
    now = datetime.utcnow()
    user.last_seen = now
    user.en_linea = True
    db.session.flush()


def _user_payload(user: Usuario, unread_count: int, online_threshold: datetime, now: datetime) -> dict:
    display_name = (user.nombre or user.usuario or "Usuario").strip() or "Usuario"
    is_recent = bool(user.last_seen and user.last_seen >= online_threshold)
    is_online = is_recent
    minutes_ago = None
    if user.last_seen:
        delta_seconds = max(0.0, (now - user.last_seen).total_seconds())
        minutes_ago = int(delta_seconds // 60)

    if is_online:
        presence_label = "Activo ahora"
    elif minutes_ago is None:
        presence_label = "Sin actividad"
    elif minutes_ago <= 1:
        presence_label = "Hace 1 minuto"
    else:
        presence_label = f"Hace {minutes_ago} minutos"

    return {
        "id": user.id,
        "username": user.usuario,
        "name": display_name,
        "online": is_online,
        "status_label": presence_label,
        "last_seen": user.last_seen.isoformat() if user.last_seen else None,
        "unread": unread_count,
    }


@chat_bp.get("/api/users")
@login_required
def chat_users():
    current_user = _current_user()
    if current_user is None:
        return jsonify(ok=False, message="No autenticado"), 401

    now = datetime.utcnow()
    online_threshold = now - timedelta(seconds=ONLINE_WINDOW_SECONDS)
    _touch_presence(current_user)

    (
        Usuario.query.filter(
            Usuario.en_linea.is_(True),
            or_(Usuario.last_seen.is_(None), Usuario.last_seen < online_threshold),
        ).update({"en_linea": False}, synchronize_session=False)
    )

    unread_rows = (
        db.session.query(ChatMessage.sender_id, func.count(ChatMessage.id))
        .filter(
            ChatMessage.receiver_id == current_user.id,
            ChatMessage.is_read.is_(False),
        )
        .group_by(ChatMessage.sender_id)
        .all()
    )
    unread_by_sender = {sender_id: count for sender_id, count in unread_rows}

    users = (
        Usuario.query.filter(Usuario.activo.is_(True), Usuario.id != current_user.id)
        .order_by(Usuario.nombre.asc(), Usuario.usuario.asc())
        .all()
    )

    payload = [
        _user_payload(user, unread_by_sender.get(user.id, 0), online_threshold, now)
        for user in users
    ]

    payload.sort(key=lambda item: (0 if item["unread"] > 0 else 1, item["name"].lower()))
    db.session.commit()
    return jsonify(ok=True, users=payload)


@chat_bp.get("/api/messages/<int:other_user_id>")
@login_required
def chat_messages(other_user_id: int):
    current_user = _current_user()
    if current_user is None:
        return jsonify(ok=False, message="No autenticado"), 401
    _touch_presence(current_user)

    other_user = db.session.get(Usuario, other_user_id)
    if other_user is None or not other_user.activo:
        return jsonify(ok=False, message="Usuario no disponible"), 404

    after_id = _safe_int(request.args.get("after_id"), 0)
    limit = min(max(_safe_int(request.args.get("limit"), 60), 10), 200)

    q = ChatMessage.query.filter(
        or_(
            and_(ChatMessage.sender_id == current_user.id, ChatMessage.receiver_id == other_user_id),
            and_(ChatMessage.sender_id == other_user_id, ChatMessage.receiver_id == current_user.id),
        )
    )

    if after_id > 0:
        q = q.filter(ChatMessage.id > after_id)

    items = q.order_by(ChatMessage.id.asc()).limit(limit).all()

    (
        ChatMessage.query.filter(
            ChatMessage.sender_id == other_user_id,
            ChatMessage.receiver_id == current_user.id,
            ChatMessage.is_read.is_(False),
        ).update({"is_read": True}, synchronize_session=False)
    )
    db.session.commit()

    return jsonify(
        ok=True,
        messages=[_serialize_message(item, current_user.id) for item in items],
        other_user={
            "id": other_user.id,
            "name": (other_user.nombre or other_user.usuario or "Usuario").strip() or "Usuario",
            "username": other_user.usuario,
        },
    )


@chat_bp.post("/api/messages/send")
@login_required
def chat_send_message():
    current_user = _current_user()
    if current_user is None:
        return jsonify(ok=False, message="No autenticado"), 401
    _touch_presence(current_user)

    payload = request.get_json(silent=True) or request.form
    receiver_id = _safe_int((payload or {}).get("receiver_id"), 0)
    content = _clean_text((payload or {}).get("content"))

    if receiver_id <= 0:
        return jsonify(ok=False, message="Destino invalido"), 400
    if not content:
        return jsonify(ok=False, message="El mensaje no puede estar vacio"), 400
    if len(content) > 3000:
        return jsonify(ok=False, message="El mensaje excede el largo permitido"), 400

    receiver = db.session.get(Usuario, receiver_id)
    if receiver is None or not receiver.activo:
        return jsonify(ok=False, message="Usuario destino no disponible"), 404

    msg = ChatMessage(
        sender_id=current_user.id,
        receiver_id=receiver.id,
        content=content,
        message_type="text",
        sent_at=datetime.utcnow(),
        is_read=False,
    )
    db.session.add(msg)
    db.session.commit()

    return jsonify(ok=True, message=_serialize_message(msg, current_user.id))


@chat_bp.post("/api/messages/upload")
@login_required
def chat_send_upload():
    current_user = _current_user()
    if current_user is None:
        return jsonify(ok=False, message="No autenticado"), 401
    _touch_presence(current_user)

    receiver_id = _safe_int(request.form.get("receiver_id"), 0)
    caption = _clean_text(request.form.get("content"))
    voice_note = request.form.get("voice_note") == "1"
    file_obj = request.files.get("file")

    if receiver_id <= 0:
        return jsonify(ok=False, message="Destino invalido"), 400
    if file_obj is None:
        return jsonify(ok=False, message="Debe adjuntar un archivo"), 400
    if request.content_length and request.content_length > MAX_UPLOAD_BYTES + (1024 * 300):
        return jsonify(ok=False, message="Archivo excede el tamano maximo"), 400

    receiver = db.session.get(Usuario, receiver_id)
    if receiver is None or not receiver.activo:
        return jsonify(ok=False, message="Usuario destino no disponible"), 404

    try:
        uploaded = _save_upload(file_obj, force_audio=voice_note)
    except ValueError as exc:
        db.session.rollback()
        return jsonify(ok=False, message=str(exc)), 400

    content = caption
    if not content:
        if uploaded["message_type"] == "audio":
            content = "Mensaje de audio"
        elif uploaded["message_type"] == "image":
            content = "Imagen"
        else:
            content = uploaded["media_name"]

    msg = ChatMessage(
        sender_id=current_user.id,
        receiver_id=receiver.id,
        content=content,
        message_type=uploaded["message_type"],
        media_path=uploaded["media_path"],
        media_name=uploaded["media_name"],
        media_mime=uploaded["media_mime"],
        media_size=uploaded["media_size"],
        sent_at=datetime.utcnow(),
        is_read=False,
    )
    db.session.add(msg)
    db.session.commit()

    return jsonify(ok=True, message=_serialize_message(msg, current_user.id))


@chat_bp.get("/api/unread_count")
@login_required
def chat_unread_count():
    current_user = _current_user()
    if current_user is None:
        return jsonify(ok=False, message="No autenticado"), 401
    _touch_presence(current_user)

    total = (
        db.session.query(func.count(ChatMessage.id))
        .filter(
            ChatMessage.receiver_id == current_user.id,
            ChatMessage.is_read.is_(False),
        )
        .scalar()
        or 0
    )

    return jsonify(ok=True, unread=int(total))


@chat_bp.post("/api/messages/mark_read/<int:other_user_id>")
@login_required
def chat_mark_read(other_user_id: int):
    current_user = _current_user()
    if current_user is None:
        return jsonify(ok=False, message="No autenticado"), 401
    _touch_presence(current_user)

    (
        ChatMessage.query.filter(
            ChatMessage.sender_id == other_user_id,
            ChatMessage.receiver_id == current_user.id,
            ChatMessage.is_read.is_(False),
        ).update({"is_read": True}, synchronize_session=False)
    )
    db.session.commit()

    return jsonify(ok=True)


@chat_bp.post("/api/presence/heartbeat")
@login_required
def chat_presence_heartbeat():
    current_user = _current_user()
    if current_user is None:
        return jsonify(ok=False, message="No autenticado"), 401

    _touch_presence(current_user)
    db.session.commit()
    return jsonify(ok=True)


@chat_bp.get("/api/messages/media/<int:message_id>")
@login_required
def chat_media_view(message_id: int):
    current_user = _current_user()
    if current_user is None:
        return jsonify(ok=False, message="No autenticado"), 401

    message = db.session.get(ChatMessage, message_id)
    if message is None or not _can_access_message(message, current_user.id):
        abort(404)
    if not message.media_path:
        abort(404)

    path = _resolve_media_path(message.media_path)
    if path is None or not path.exists():
        abort(404)

    _touch_presence(current_user)
    db.session.commit()
    return send_file(path, mimetype=message.media_mime or None, as_attachment=False, download_name=message.media_name or path.name)


@chat_bp.get("/api/messages/media/<int:message_id>/download")
@login_required
def chat_media_download(message_id: int):
    current_user = _current_user()
    if current_user is None:
        return jsonify(ok=False, message="No autenticado"), 401

    message = db.session.get(ChatMessage, message_id)
    if message is None or not _can_access_message(message, current_user.id):
        abort(404)
    if not message.media_path:
        abort(404)

    path = _resolve_media_path(message.media_path)
    if path is None or not path.exists():
        abort(404)

    _touch_presence(current_user)
    db.session.commit()
    return send_file(path, mimetype=message.media_mime or None, as_attachment=True, download_name=message.media_name or path.name)


@chat_bp.put("/api/messages/<int:message_id>")
@login_required
def chat_edit_message(message_id: int):
    current_user = _current_user()
    if current_user is None:
        return jsonify(ok=False, message="No autenticado"), 401
    _touch_presence(current_user)

    message = db.session.get(ChatMessage, message_id)
    if message is None or not _can_access_message(message, current_user.id):
        return jsonify(ok=False, message="Mensaje no encontrado"), 404

    if message.sender_id != current_user.id:
        return jsonify(ok=False, message="Solo puedo editar tus propios mensajes"), 403

    payload = request.get_json(silent=True) or {}
    new_content = _clean_text(payload.get("content"))

    if not new_content:
        return jsonify(ok=False, message="El contenido no puede estar vacio"), 400
    if len(new_content) > 3000:
        return jsonify(ok=False, message="El mensaje excede el largo permitido"), 400

    message.content = new_content
    message.edited_at = datetime.utcnow()
    db.session.commit()

    return jsonify(ok=True, message=_serialize_message(message, current_user.id))


@chat_bp.post("/api/messages/<int:message_id>/read")
@login_required
def chat_message_read(message_id: int):
    current_user = _current_user()
    if current_user is None:
        return jsonify(ok=False, message="No autenticado"), 401
    _touch_presence(current_user)

    message = db.session.get(ChatMessage, message_id)
    if message is None or not _can_access_message(message, current_user.id):
        return jsonify(ok=False, message="Mensaje no encontrado"), 404

    if message.receiver_id != current_user.id:
        return jsonify(ok=False, message="No puedo marcar mensajes recibidos como leidos"), 403

    if not message.is_read:
        message.is_read = True
        message.read_at = datetime.utcnow()
        message.status = "read"
        db.session.commit()

    return jsonify(ok=True, message=_serialize_message(message, current_user.id))


# In-memory typing status: {(sender_id, receiver_id): datetime}
_typing_status = {}


@chat_bp.post("/api/typing/start/<int:other_user_id>")
@login_required
def chat_typing_start(other_user_id: int):
    current_user = _current_user()
    if current_user is None:
        return jsonify(ok=False, message="No autenticado"), 401
    _touch_presence(current_user)

    other_user = db.session.get(Usuario, other_user_id)
    if other_user is None or not other_user.activo:
        return jsonify(ok=False, message="Usuario no disponible"), 404

    key = (current_user.id, other_user_id)
    _typing_status[key] = datetime.utcnow()
    return jsonify(ok=True)


@chat_bp.post("/api/typing/stop/<int:other_user_id>")
@login_required
def chat_typing_stop(other_user_id: int):
    current_user = _current_user()
    if current_user is None:
        return jsonify(ok=False, message="No autenticado"), 401
    _touch_presence(current_user)

    key = (current_user.id, other_user_id)
    _typing_status.pop(key, None)
    return jsonify(ok=True)


@chat_bp.get("/api/typing/<int:other_user_id>")
@login_required
def chat_typing_status(other_user_id: int):
    current_user = _current_user()
    if current_user is None:
        return jsonify(ok=False, message="No autenticado"), 401
    _touch_presence(current_user)

    key = (other_user_id, current_user.id)
    typing_at = _typing_status.get(key)

    is_typing = False
    if typing_at:
        elapsed = (datetime.utcnow() - typing_at).total_seconds()
        is_typing = elapsed < 5

    if not is_typing:
        _typing_status.pop(key, None)

    return jsonify(ok=True, is_typing=is_typing)


@chat_bp.post("/api/messages/<int:message_id>/delete-me")
@login_required
def chat_delete_for_me(message_id: int):
    current_user = _current_user()
    if current_user is None:
        return jsonify(ok=False, message="No autenticado"), 401
    _touch_presence(current_user)

    message = db.session.get(ChatMessage, message_id)
    if message is None or not _can_access_message(message, current_user.id):
        return jsonify(ok=False, message="Mensaje no encontrado"), 404

    if current_user.id == message.sender_id:
        message.deleted_for_sender = True
    elif current_user.id == message.receiver_id:
        message.deleted_for_receiver = True
    else:
        return jsonify(ok=False, message="No tienes permiso"), 403

    db.session.commit()
    return jsonify(ok=True, message=_serialize_message(message, current_user.id))


@chat_bp.post("/api/messages/<int:message_id>/delete-all")
@login_required
def chat_delete_for_all(message_id: int):
    current_user = _current_user()
    if current_user is None:
        return jsonify(ok=False, message="No autenticado"), 401
    _touch_presence(current_user)

    message = db.session.get(ChatMessage, message_id)
    if message is None:
        return jsonify(ok=False, message="Mensaje no encontrado"), 404

    if message.sender_id != current_user.id:
        return jsonify(ok=False, message="Solo puedo eliminar mis propios mensajes"), 403

    # Check time restriction (10 minutes)
    now = datetime.utcnow()
    elapsed_minutes = (now - message.sent_at).total_seconds() / 60
    if elapsed_minutes > 10:
        return jsonify(ok=False, message="El mensaje expiró, solo puedes eliminarlo para ti"), 400

    message.deleted_for_all = True
    message.deleted_at = now
    db.session.commit()
    return jsonify(ok=True, message=_serialize_message(message, current_user.id))
