from __future__ import annotations

import json
from datetime import datetime

from flask import has_request_context, request

from app.extensions import db
from app.seguridad.models import AuditEvent


def record_audit_event(
    accion: str,
    detalle: str | dict | None = None,
    *,
    actor_usuario: str | None = None,
) -> None:
    """
    Graba un evento de auditoría. Nunca relanza: el flujo principal no debe fallar por el log.
    """
    try:
        ip: str | None = None
        ruta: str | None = None
        if has_request_context():
            raw = (request.headers.get("X-Forwarded-For") or request.remote_addr or "") or ""
            if "," in raw:
                ip = raw.split(",")[0].strip()[:80] or None
            else:
                ip = (raw.strip()[:80] or None) if raw else None
            ruta = (request.path or "")[:500] or None
        d_text: str | None
        if detalle is None:
            d_text = None
        elif isinstance(detalle, dict):
            d_text = json.dumps(detalle, ensure_ascii=False)[:2000]
        else:
            d_text = str(detalle)[:2000]
        ev = AuditEvent(
            created_at=datetime.utcnow(),
            actor_usuario=(actor_usuario or "")[:120] or None,
            accion=(accion or "")[:200] or "evento",
            detalle=d_text,
            ip=ip,
            ruta=ruta,
        )
        db.session.add(ev)
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
