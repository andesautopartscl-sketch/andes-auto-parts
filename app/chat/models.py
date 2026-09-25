from datetime import datetime

from app.extensions import db


def normalize_chat_archive_pin(raw) -> str | None:
    pin = str(raw or "").strip()
    if not pin:
        return None
    if (not pin.isdigit()) or len(pin) < 4 or len(pin) > 8:
        raise ValueError("La clave de archivados debe tener 4 a 8 dígitos.")
    return pin


DEFAULT_CHAT_LABELS = (
    ("vendedor", "Vendedor", "#2563eb"),
    ("transportista", "Transportista", "#ea580c"),
    ("bodega", "Bodega", "#0f766e"),
    ("finanzas", "Finanzas", "#7c3aed"),
    ("compras", "Compras", "#db2777"),
    ("postventa", "Postventa", "#0891b2"),
    ("administracion", "Administración", "#475569"),
    ("urgente", "Urgente", "#dc2626"),
)


class ChatMessage(db.Model):
    __tablename__ = "chat_messages"

    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("usuarios_sistema.id"), nullable=False, index=True)
    receiver_id = db.Column(db.Integer, db.ForeignKey("usuarios_sistema.id"), nullable=False, index=True)
    content = db.Column(db.Text, nullable=False)
    message_type = db.Column(db.String(20), default="text", nullable=False, index=True)
    media_path = db.Column(db.String(500), nullable=True)
    media_name = db.Column(db.String(255), nullable=True)
    media_mime = db.Column(db.String(120), nullable=True)
    media_size = db.Column(db.Integer, nullable=True)
    sent_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    is_read = db.Column(db.Boolean, default=False, nullable=False, index=True)
    read_at = db.Column(db.DateTime, nullable=True, index=True)
    edited_at = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(20), default="sent", nullable=False, index=True)
    deleted_for_sender = db.Column(db.Boolean, default=False, nullable=False)
    deleted_for_receiver = db.Column(db.Boolean, default=False, nullable=False)
    deleted_for_all = db.Column(db.Boolean, default=False, nullable=False, index=True)
    deleted_at = db.Column(db.DateTime, nullable=True)

    def to_dict(self, current_user_id: int) -> dict:
        return {
            "id": self.id,
            "sender_id": self.sender_id,
            "receiver_id": self.receiver_id,
            "content": self.content,
            "message_type": self.message_type or "text",
            "media_name": self.media_name,
            "media_mime": self.media_mime,
            "media_size": self.media_size,
            "sent_at": self.sent_at.isoformat() if self.sent_at else None,
            "is_read": bool(self.is_read),
            "read_at": self.read_at.isoformat() if self.read_at else None,
            "edited_at": self.edited_at.isoformat() if self.edited_at else None,
            "status": self.status or "sent",
            "deleted_for_sender": bool(self.deleted_for_sender),
            "deleted_for_receiver": bool(self.deleted_for_receiver),
            "deleted_for_all": bool(self.deleted_for_all),
            "deleted_at": self.deleted_at.isoformat() if self.deleted_at else None,
            "mine": self.sender_id == current_user_id,
        }


class ChatLabel(db.Model):
    __tablename__ = "chat_labels"

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(48), unique=True, nullable=False, index=True)
    nombre = db.Column(db.String(60), nullable=False)
    color = db.Column(db.String(16), nullable=False, default="#64748b")
    orden = db.Column(db.Integer, nullable=False, default=0)
    sistema = db.Column(db.Boolean, nullable=False, default=False)
    activo = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "slug": self.slug,
            "nombre": self.nombre,
            "color": self.color or "#64748b",
            "sistema": bool(self.sistema),
        }


class ChatConversationMeta(db.Model):
    __tablename__ = "chat_conversation_meta"
    __table_args__ = (
        db.UniqueConstraint("owner_user_id", "other_user_id", name="uq_chat_conv_meta_pair"),
    )

    id = db.Column(db.Integer, primary_key=True)
    owner_user_id = db.Column(db.Integer, db.ForeignKey("usuarios_sistema.id"), nullable=False, index=True)
    other_user_id = db.Column(db.Integer, db.ForeignKey("usuarios_sistema.id"), nullable=False, index=True)
    archived = db.Column(db.Boolean, nullable=False, default=False, index=True)
    archived_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ChatConversationLabel(db.Model):
    __tablename__ = "chat_conversation_labels"
    __table_args__ = (
        db.UniqueConstraint(
            "owner_user_id", "other_user_id", "label_id", name="uq_chat_conv_label"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    owner_user_id = db.Column(db.Integer, db.ForeignKey("usuarios_sistema.id"), nullable=False, index=True)
    other_user_id = db.Column(db.Integer, db.ForeignKey("usuarios_sistema.id"), nullable=False, index=True)
    label_id = db.Column(db.Integer, db.ForeignKey("chat_labels.id"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


def ensure_default_chat_labels() -> None:
    existing = {row.slug for row in ChatLabel.query.all()}
    now = datetime.utcnow()
    next_orden = (max((row.orden or 0) for row in ChatLabel.query.all()) if existing else 0)
    for slug, nombre, color in DEFAULT_CHAT_LABELS:
        if slug in existing:
            continue
        next_orden += 1
        db.session.add(
            ChatLabel(
                slug=slug,
                nombre=nombre,
                color=color,
                orden=next_orden,
                sistema=True,
                activo=True,
                created_at=now,
            )
        )
    db.session.flush()
