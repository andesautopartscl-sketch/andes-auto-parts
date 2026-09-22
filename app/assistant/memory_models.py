"""SQLAlchemy models for assistant memory (FASE 7B.1). Used by db.create_all()."""
from __future__ import annotations

from app.extensions import db


class AssistantMemorySlot(db.Model):
    __tablename__ = "assistant_memory_slot"

    id = db.Column(db.String(80), primary_key=True)
    actor_user = db.Column(db.String(80), nullable=False, index=True)
    scope = db.Column(db.String(20), nullable=False)
    conversation_id = db.Column(db.String(80), nullable=True)
    memory_type = db.Column(db.String(40), nullable=False)
    key = db.Column(db.String(120), nullable=False)
    value_json = db.Column(db.Text, nullable=False)
    confidence = db.Column(db.Float, nullable=True)
    source = db.Column(db.String(20), nullable=False)
    permission_epoch = db.Column(db.Integer, nullable=True)
    sensitivity = db.Column(db.String(20), nullable=False, default="benign")
    created_at = db.Column(db.String(40), nullable=False)
    updated_at = db.Column(db.String(40), nullable=False)
    expires_at = db.Column(db.String(40), nullable=True)
    deleted_at = db.Column(db.String(40), nullable=True)
    source_turn_id = db.Column(db.String(80), nullable=True)
    meta_json = db.Column(db.Text, nullable=True)
    # FASE 10.2.1 — ciclo de vida: approved | suggested | rejected | expired.
    #
    # `default` y `server_default` a la vez, y no por duplicar: el primero cubre
    # las filas que crea SQLAlchemy, el segundo las que crea el MemoryStore por
    # SQL directo. Sin el segundo, una fila insertada por el store quedaria en
    # NULL y el contrato dejaria de sostenerse.
    #
    # La politica que hara nacer `suggested` a lo derivado es de 10.2.2: aqui
    # todo nace `approved`, que es el comportamiento de siempre.
    status = db.Column(db.String(20), nullable=False,
                       default="approved", server_default="approved")
    status_changed_at = db.Column(db.String(40), nullable=True)
    status_by = db.Column(db.String(80), nullable=True)
