from datetime import datetime

from app.extensions import db


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
