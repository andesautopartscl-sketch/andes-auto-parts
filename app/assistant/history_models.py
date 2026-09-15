"""SQLAlchemy models for assistant history (FASE 7A). Used by db.create_all()."""
from __future__ import annotations

from app.extensions import db


class AssistantConversation(db.Model):
    __tablename__ = "assistant_conversation"

    id = db.Column(db.String(80), primary_key=True)
    client_conversation_id = db.Column(db.String(80), nullable=True)
    actor_user = db.Column(db.String(80), nullable=False, index=True)
    title = db.Column(db.String(120), nullable=True)
    status = db.Column(db.String(20), nullable=False, default="active")
    created_at = db.Column(db.String(40), nullable=False)
    updated_at = db.Column(db.String(40), nullable=False)
    last_turn_at = db.Column(db.String(40), nullable=True)
    turn_count = db.Column(db.Integer, nullable=False, default=0)
    retention_until = db.Column(db.String(40), nullable=False)
    deleted_at = db.Column(db.String(40), nullable=True)


class AssistantTurn(db.Model):
    __tablename__ = "assistant_turn"

    id = db.Column(db.String(80), primary_key=True)
    conversation_id = db.Column(db.String(80), db.ForeignKey("assistant_conversation.id"), nullable=False)
    actor_user = db.Column(db.String(80), nullable=False, index=True)
    seq = db.Column(db.Integer, nullable=False)
    correlation_id = db.Column(db.String(80), nullable=True)
    message_hash = db.Column(db.String(64), nullable=True)
    message_excerpt = db.Column(db.String(120), nullable=True)
    reply_excerpt = db.Column(db.String(240), nullable=True)
    scenario = db.Column(db.String(80), nullable=True)
    classification = db.Column(db.String(40), nullable=True)
    tools_used_json = db.Column(db.Text, nullable=True)
    entities_json = db.Column(db.Text, nullable=True)
    evidence_json = db.Column(db.Text, nullable=True)
    flags_json = db.Column(db.Text, nullable=True)
    llm_latency_ms = db.Column(db.Integer, nullable=True)
    total_latency_ms = db.Column(db.Integer, nullable=True)
    planner_mode = db.Column(db.String(32), nullable=True)
    created_at = db.Column(db.String(40), nullable=False)

    __table_args__ = (
        db.UniqueConstraint("conversation_id", "seq", name="ux_asst_turn_conv_seq_sa"),
    )
