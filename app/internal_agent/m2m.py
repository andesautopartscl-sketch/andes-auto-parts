"""M2M auth for /internal/agent/* — cookies are never accepted."""
from __future__ import annotations

import hmac
import os

from flask import Request, jsonify

ALLOWED_ENVIRONMENTS = frozenset({"local", "production"})
FORBIDDEN_BODY_KEYS = frozenset({
    "sql",
    "query_raw",
    "raw_sql",
    "order_by",
    "table",
    "column",
    "url",
    "headers",
    "shell",
    "command",
    "cmd",
    "eval",
    "password",
    "token",
    "authorization",
    "cookie",
    "cookies",
    "api_key",
    "secret",
})


class InternalAuthError(Exception):
    def __init__(self, code: str, message: str, status: int = 401):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def service_token() -> str:
    return (os.environ.get("ANDES_AGENT_SERVICE_TOKEN") or "").strip()


def current_environment() -> str:
    value = (os.environ.get("ANDES_ENV") or "local").strip().lower()
    if value not in ALLOWED_ENVIRONMENTS:
        raise InternalAuthError("invalid_environment", "ANDES_ENV must be 'local' or 'production'", status=400)
    return value


def _header(request: Request, name: str) -> str:
    return (request.headers.get(name) or "").strip()


def authenticate_m2m(request: Request) -> str:
    if _header(request, "Cookie") or bool(request.cookies):
        raise InternalAuthError("cookies_not_allowed", "Internal agent API does not accept session cookies", status=401)

    authorization = _header(request, "Authorization")
    if not authorization:
        raise InternalAuthError("unauthorized", "Missing Authorization bearer token", status=401)
    scheme, _, credential = authorization.partition(" ")
    if scheme.lower() != "bearer" or not credential.strip():
        raise InternalAuthError("unauthorized", "Authorization must be a Bearer token", status=401)

    expected = service_token()
    provided = credential.strip()
    if not expected or len(provided) != len(expected) or not hmac.compare_digest(provided, expected):
        raise InternalAuthError("unauthorized", "Invalid bearer token", status=401)

    env_header = _header(request, "X-Andes-Env")
    if env_header not in ALLOWED_ENVIRONMENTS:
        raise InternalAuthError("invalid_environment", "X-Andes-Env must be 'local' or 'production'", status=400)
    if env_header != current_environment():
        raise InternalAuthError("invalid_environment", "Environment mismatch", status=400)
    return env_header


def actor_username(request: Request) -> str:
    actor = _header(request, "X-Andes-Actor")
    if not actor:
        raise InternalAuthError("principal_required", "Human principal is required", status=400)
    if len(actor) > 80:
        raise InternalAuthError("principal_invalid", "Invalid principal", status=403)
    return actor


def error_response(exc: InternalAuthError):
    payload = {
        "ok": False,
        "error_code": exc.code,
        "message": exc.message,
        "error": {"code": exc.code, "message": exc.message},
    }
    return jsonify(payload), exc.status
