"""Reject oversized / hostile natural-language input before planning."""
from __future__ import annotations

import re
from typing import Any

MAX_MESSAGE_LEN = 500
MIN_MESSAGE_LEN = 1

SQL_RE = re.compile(
    r"\b(SELECT|INSERT|UPDATE|DELETE|DROP|UNION|ALTER|EXEC|MERGE|TRUNCATE)\b",
    re.IGNORECASE,
)
SHELL_RE = re.compile(r"[;`|$]|&&|\|\||\b(curl|wget|bash|powershell|cmd\.exe)\b", re.IGNORECASE)
ENDPOINT_RE = re.compile(r"/internal/agent/|/v1/invoke|https?://", re.IGNORECASE)

WRITE_INTENT_RE = re.compile(
    r"\b(crea(r|ción)?|crear|anula(r)?|eliminar|borra(r)?|descuenta|descontar|"
    r"modifica(r)?|actualiza(r)?|guarda(r)?|inserta(r)?|escribir|write|"
    r"genera(r)?\s+oc|emitir\s+factura|reservar\s+stock)\b",
    re.IGNORECASE,
)


class InputGuardError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def guard_message(raw: Any) -> str:
    if raw is None:
        raise InputGuardError("invalid_args", "message is required")
    if not isinstance(raw, str):
        raise InputGuardError("invalid_args", "message must be a string")
    text = raw.strip()
    if len(text) < MIN_MESSAGE_LEN:
        raise InputGuardError("invalid_args", "message is empty")
    if len(text) > MAX_MESSAGE_LEN:
        raise InputGuardError("invalid_args", f"message exceeds {MAX_MESSAGE_LEN} characters")
    if SQL_RE.search(text):
        raise InputGuardError("invalid_args", "SQL is not allowed in messages")
    if ENDPOINT_RE.search(text):
        raise InputGuardError("invalid_args", "Endpoints and URLs are not allowed in messages")
    if SHELL_RE.search(text):
        raise InputGuardError("invalid_args", "Shell or command metacharacters are not allowed")
    return text


def detect_write_intent(text: str) -> bool:
    return bool(WRITE_INTENT_RE.search(text or ""))
