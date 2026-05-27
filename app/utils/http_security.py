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
    h.setdefault(
        "Content-Security-Policy",
        (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' cdnjs.cloudflare.com cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' cdnjs.cloudflare.com fonts.googleapis.com; "
            "font-src 'self' fonts.gstatic.com cdnjs.cloudflare.com; "
            "img-src 'self' data: blob: res.cloudinary.com *.cloudinary.com; "
            "connect-src 'self' api.baseapi.cl api.mymemory.translated.net; "
            "frame-src 'self'; "
            "object-src 'none'"
        ),
    )
