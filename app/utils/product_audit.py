import json
from datetime import datetime
from typing import Any

from flask import Request

from app.models import ProductoAuditDiff, ProductoAuditEvent


def _to_text(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, (dict, list, tuple)):
        try:
            return json.dumps(v, ensure_ascii=False, default=str)
        except Exception:
            return str(v)
    return str(v)


def build_diffs(before: dict[str, Any] | None, after: dict[str, Any] | None) -> list[dict[str, str | None]]:
    before = before or {}
    after = after or {}
    keys = sorted(set(before.keys()) | set(after.keys()))
    out: list[dict[str, str | None]] = []
    for k in keys:
        b = before.get(k)
        a = after.get(k)
        if _to_text(b) == _to_text(a):
            continue
        out.append(
            {
                "campo": str(k),
                "valor_anterior": _to_text(b),
                "valor_nuevo": _to_text(a),
            }
        )
    return out


def register_product_audit(
    sess,
    *,
    actor: str,
    action: str,
    modulo: str = "productos",
    producto_codigo: str | None = None,
    req: Request | None = None,
    metadata: dict[str, Any] | None = None,
    diffs: list[dict[str, str | None]] | None = None,
) -> ProductoAuditEvent:
    meta_text = None
    if metadata:
        try:
            meta_text = json.dumps(metadata, ensure_ascii=False, default=str)
        except Exception:
            meta_text = str(metadata)

    event = ProductoAuditEvent(
        created_at=datetime.utcnow(),
        actor=(actor or "sistema").strip() or "sistema",
        action=(action or "unknown").strip() or "unknown",
        modulo=(modulo or "productos").strip() or "productos",
        producto_codigo=(producto_codigo or "").strip().upper() or None,
        ip=(req.remote_addr if req else None),
        user_agent=((req.headers.get("User-Agent") if req else None) or "")[:255] or None,
        request_path=(req.path if req else None),
        metadata_json=meta_text,
    )
    sess.add(event)
    sess.flush()

    for d in diffs or []:
        sess.add(
            ProductoAuditDiff(
                event_id=event.id,
                campo=(d.get("campo") or "").strip()[:120] or "unknown",
                valor_anterior=d.get("valor_anterior"),
                valor_nuevo=d.get("valor_nuevo"),
            )
        )
    return event


# Sinónimos en español (y variaciones) → código almacenado en ProductoAuditEvent.action
_AUDIT_ACTION_ALIASES: dict[str, str] = {
    "search": "search",
    "buscar": "search",
    "busqueda": "search",
    "view": "view",
    "ver": "view",
    "vista": "view",
    "ficha": "view",
    "consulta": "view",
    "update": "update",
    "actualizar": "update",
    "edicion": "update",
    "modificar": "update",
    "create": "create",
    "crear": "create",
    "alta": "create",
    "deactivate": "deactivate",
    "desactivar": "deactivate",
    "baja": "deactivate",
    "reactivate": "reactivate",
    "reactivar": "reactivate",
    "reactivate_bulk": "reactivate_bulk",
    "despiece_save": "despiece_save",
    "despiece": "despiece_save",
    "unknown": "unknown",
    "desconocido": "unknown",
}


def _unaccent_lower(s: str) -> str:
    t = (s or "").strip().lower()
    for old, new in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ñ", "n")):
        t = t.replace(old, new)
    return t


def resolve_producto_audit_action_filter(user_input: str) -> str:
    """Convierte lo escrito en el filtro a un action exacto; si no hay mapa, usa el valor tal cual."""
    t = _unaccent_lower(user_input)
    if not t:
        return ""
    return _AUDIT_ACTION_ALIASES.get(t, t)
