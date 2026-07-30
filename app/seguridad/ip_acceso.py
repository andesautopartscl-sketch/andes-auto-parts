"""Clasificación y bloqueo de IPs de acceso (empresa / externa)."""
from __future__ import annotations

from datetime import datetime

from flask import Request, has_request_context, request

from app.extensions import db
from app.seguridad.models import IpAcceso

CLASIF_EMPRESA = "empresa"
CLASIF_EXTERNA = "externa"
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost", "0.0.0.0"})


def normalize_ip(raw: str | None) -> str | None:
    if not raw:
        return None
    ip = (raw or "").strip()
    if "," in ip:
        ip = ip.split(",")[0].strip()
    ip = ip[:80]
    return ip or None


def client_ip_from_request(req: Request | None = None) -> str | None:
    if req is None:
        if not has_request_context():
            return None
        req = request
    forwarded = (req.headers.get("X-Forwarded-For") or "").strip()
    if forwarded:
        return normalize_ip(forwarded)
    return normalize_ip(req.remote_addr)


def is_loopback_ip(ip: str | None) -> bool:
    key = (ip or "").strip().lower()
    return key in _LOOPBACK or key.startswith("127.")


def get_ip_regla(ip: str | None) -> IpAcceso | None:
    key = normalize_ip(ip)
    if not key:
        return None
    return IpAcceso.query.filter_by(ip=key).first()


def is_ip_blocked(ip: str | None) -> bool:
    regla = get_ip_regla(ip)
    return bool(regla and regla.bloqueada)


def upsert_ip_acceso(
    ip: str | None,
    *,
    clasificacion: str | None = None,
    bloqueada: bool | None = None,
    etiqueta: str | None = None,
    notas: str | None = None,
    actor: str | None = None,
) -> tuple[IpAcceso | None, str | None]:
    """Crea o actualiza regla de IP. Devuelve (regla, error)."""
    key = normalize_ip(ip)
    if not key:
        return None, "IP inválida."
    if clasificacion is not None and clasificacion not in {CLASIF_EMPRESA, CLASIF_EXTERNA}:
        return None, "Clasificación inválida."
    if bloqueada is True and is_loopback_ip(key):
        return None, "No se puede bloquear la IP local (loopback)."

    row = IpAcceso.query.filter_by(ip=key).first()
    now = datetime.utcnow()
    if row is None:
        row = IpAcceso(
            ip=key,
            clasificacion=clasificacion or CLASIF_EXTERNA,
            bloqueada=False,
            created_at=now,
            updated_at=now,
            creado_por=(actor or "")[:120] or None,
        )
        db.session.add(row)

    if clasificacion is not None:
        row.clasificacion = clasificacion
    if bloqueada is not None:
        row.bloqueada = bool(bloqueada)
    if etiqueta is not None:
        row.etiqueta = (etiqueta or "").strip()[:120] or None
    if notas is not None:
        row.notas = (notas or "").strip()[:2000] or None
    row.actualizado_por = (actor or "")[:120] or None
    row.updated_at = now
    return row, None


def login_audit_detalle(req: Request | None = None, *, extra: dict | None = None) -> dict:
    if req is None:
        if not has_request_context():
            return dict(extra or {})
        req = request
    ua = (req.user_agent.string or "")[:240]
    platform = (req.user_agent.platform or "")[:40]
    browser = (req.user_agent.browser or "")[:40]
    path = (req.path or "")[:120]
    next_q = (req.values.get("next") or "")[:80]
    origen = "mobile" if (next_q.startswith("/m") or path.startswith("/m")) else "erp"
    out = {
        "ua": ua,
        "platform": platform,
        "browser": browser,
        "origen": origen,
    }
    if extra:
        out.update(extra)
    return out


def format_audit_detalle(raw: str | None) -> str:
    if not raw:
        return ""
    text = (raw or "").strip()
    if not text:
        return ""
    if text.startswith("{") and text.endswith("}"):
        try:
            import json

            data = json.loads(text)
            if isinstance(data, dict):
                # Eventos de IP (clasificar / bloquear)
                if any(k in data for k in ("clasificacion", "bloqueada")) and data.get("ip"):
                    parts = [f"IP {data.get('ip')}"]
                    clasif = (data.get("clasificacion") or "").strip()
                    if clasif:
                        parts.append(clasif.upper())
                    if data.get("bloqueada") is True:
                        parts.append("BLOQUEADA")
                    elif clasif:
                        parts.append("permitida")
                    etiq = (data.get("etiqueta") or "").strip()
                    if etiq:
                        parts.append(etiq)
                    return " · ".join(parts)

                # Navegación / búsqueda en el ERP
                if data.get("pagina") or data.get("busqueda"):
                    parts = []
                    modulo = (data.get("modulo") or "").strip()
                    if modulo:
                        parts.append(modulo.upper())
                    pagina = (data.get("pagina") or "").strip()
                    if pagina:
                        parts.append(pagina)
                    busqueda = (data.get("busqueda") or "").strip()
                    if busqueda:
                        parts.append(f'buscó "{busqueda}"')
                    return " · ".join(parts)

                parts = []
                origen = (data.get("origen") or "").strip()
                if origen:
                    parts.append(origen.upper())
                browser = (data.get("browser") or "").strip()
                platform = (data.get("platform") or "").strip()
                if browser or platform:
                    parts.append(" · ".join(x for x in (browser, platform) if x))
                motivo = (data.get("motivo") or "").strip()
                if motivo:
                    parts.append(motivo.replace("_", " "))
                nota = (data.get("nota") or "").strip()
                if nota:
                    parts.append(nota)
                ip = (data.get("ip") or "").strip()
                if ip and "IP " not in " · ".join(parts):
                    parts.append(f"IP {ip}")
                if parts:
                    return " · ".join(parts)
        except Exception:
            pass
    return text[:240]
