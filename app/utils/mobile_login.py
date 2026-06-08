"""Detección de contexto PWA / mobile para redirects post-login."""
from __future__ import annotations

from flask import Request, url_for

from app.utils.login_wall import safe_next_path

MOBILE_PWA_COOKIE = "andes_mobile_pwa"


def is_mobile_login_context(request: Request, next_url: str | None = None) -> bool:
    nxt = safe_next_path(next_url) or ""
    if nxt.startswith("/m"):
        return True
    if (request.cookies.get(MOBILE_PWA_COOKIE) or "").strip() == "1":
        return True
    ref = (request.referrer or "").lower()
    if "/m/" in ref or ref.rstrip("/").endswith("/m"):
        return True
    ua = (request.user_agent.string or "").lower()
    if any(token in ua for token in ("iphone", "ipad", "android", "mobile", "samsung")):
        return True
    return False


def mobile_login_target(next_url: str | None = None) -> str:
    nxt = safe_next_path(next_url)
    if nxt and nxt.startswith("/m"):
        return nxt
    return url_for("mobile.home")
