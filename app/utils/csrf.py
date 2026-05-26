from __future__ import annotations

import secrets
from hmac import compare_digest

from flask import request, session


CSRF_SESSION_KEY = "_csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"
CSRF_FORM_FIELD = "csrf_token"


def get_csrf_token() -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def rotate_csrf_token() -> str:
    token = secrets.token_urlsafe(32)
    session[CSRF_SESSION_KEY] = token
    return token


def validate_csrf_request() -> bool:
    expected = session.get(CSRF_SESSION_KEY)
    if not expected:
        return False
    provided = (
        request.headers.get(CSRF_HEADER_NAME)
        or request.form.get(CSRF_FORM_FIELD)
        or request.args.get(CSRF_FORM_FIELD)
    )
    if not provided:
        return False
    return compare_digest(str(expected), str(provided))
