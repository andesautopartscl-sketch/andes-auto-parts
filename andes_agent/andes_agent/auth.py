"""Machine-to-machine authentication. Cookies are never accepted."""
from __future__ import annotations

import hmac

AUTH_ERROR_MISSING = "unauthorized"
AUTH_ERROR_INVALID = "unauthorized"
AUTH_ERROR_COOKIE = "cookies_not_allowed"


class AuthError(Exception):
    def __init__(self, code: str, message: str, status: int = 401):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _header(headers: dict[str, str], name: str) -> str:
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value or ""
    return ""


def authenticate(headers: dict[str, str], expected_token: str) -> None:
    if _header(headers, "Cookie"):
        raise AuthError(AUTH_ERROR_COOKIE, "Gateway does not accept session cookies", status=401)

    authorization = _header(headers, "Authorization")
    if not authorization:
        raise AuthError(AUTH_ERROR_MISSING, "Missing Authorization bearer token", status=401)

    scheme, _, credential = authorization.partition(" ")
    if scheme.lower() != "bearer" or not credential.strip():
        raise AuthError(AUTH_ERROR_MISSING, "Authorization must be a Bearer token", status=401)

    provided = credential.strip()
    expected = (expected_token or "").strip()
    if not expected:
        raise AuthError(AUTH_ERROR_INVALID, "Invalid bearer token", status=401)
    if len(provided) != len(expected) or not hmac.compare_digest(provided, expected):
        raise AuthError(AUTH_ERROR_INVALID, "Invalid bearer token", status=401)
