"""Detección de contexto PWA / mobile para redirects post-login."""
from __future__ import annotations

from flask import Request, url_for

from app.utils.login_wall import safe_next_path

MOBILE_PWA_COOKIE = "andes_mobile_pwa"
_MOBILE_UA_TOKENS = ("iphone", "ipad", "android", "mobile", "samsung")


def _is_mobile_user_agent(request: Request) -> bool:
    ua = (request.user_agent.string or "").lower()
    return any(token in ua for token in _MOBILE_UA_TOKENS)


def is_mobile_login_context(request: Request, next_url: str | None = None) -> bool:
    nxt = safe_next_path(next_url) or ""
    if nxt.startswith("/m"):
        return True
    ref = (request.referrer or "").lower()
    if "/m/" in ref or ref.rstrip("/").endswith("/m"):
        return True
    # Cookie PWA solo aplica en UA móvil. En PC no debe secuestrar el ERP
    # (la cookie dura 1 año y puede quedar de una visita previa a /m/).
    if (request.cookies.get(MOBILE_PWA_COOKIE) or "").strip() == "1" and _is_mobile_user_agent(request):
        return True
    if _is_mobile_user_agent(request):
        return True
    return False


def mobile_login_target(next_url: str | None = None) -> str:
    nxt = safe_next_path(next_url)
    if nxt and nxt.startswith("/m"):
        return nxt
    return url_for("mobile.home")


def clear_mobile_pwa_cookie(response) -> None:
    """Elimina la cookie de contexto PWA (path=/)."""
    response.set_cookie(
        MOBILE_PWA_COOKIE,
        "",
        max_age=0,
        expires=0,
        path="/",
        samesite="Lax",
    )