"""Cabeceras HTTP de endurecimiento (no sustituyen permisos en servidor)."""
from __future__ import annotations

from flask import request


def apply_security_headers(response, *, session_cookie_secure: bool, hsts_enabled: bool) -> None:
    if response is None or getattr(response, "headers", None) is None:
        return
    h = response.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "SAMEORIGIN")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(self), geolocation=(), interest-cohort=()",
    )
    if hsts_enabled and session_cookie_secure and request.is_secure:
        h.setdefault("Strict-Transport-Security", "max-age=15552000; includeSubDomains")
