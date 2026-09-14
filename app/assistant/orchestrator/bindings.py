"""Resolve $steps.N.<allowlisted path> bindings from prior evidence."""
from __future__ import annotations

import re
from typing import Any

from app.assistant.orchestrator.catalog import ALLOWED_BINDING_PATHS

BINDING_RE = re.compile(r"^\$steps\.(\d+)\.(.+)$")


class BindingError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _normalize_path(path: str) -> str:
    # data.items[0].codigo -> data.items.0.codigo for allowlist check
    return re.sub(r"\[(\d+)\]", r".\1", path.strip())


def _path_allowed(path: str) -> bool:
    norm = _normalize_path(path)
    if norm in {_normalize_path(p) for p in ALLOWED_BINDING_PATHS}:
        return True
    # also accept the bracket form if listed
    return path.strip() in ALLOWED_BINDING_PATHS


def resolve_path(root: Any, path: str) -> Any:
    norm = _normalize_path(path)
    cur: Any = root
    for part in norm.split("."):
        if cur is None:
            raise BindingError(f"Binding path unresolved: {path}")
        if part.isdigit():
            idx = int(part)
            if not isinstance(cur, list) or idx >= len(cur):
                raise BindingError(f"Binding index out of range: {path}")
            cur = cur[idx]
        else:
            if not isinstance(cur, dict) or part not in cur:
                raise BindingError(f"Binding path missing key: {path}")
            cur = cur[part]
    return cur


def resolve_bindings(value: Any, step_results: dict[int, dict[str, Any]]) -> Any:
    if isinstance(value, str):
        match = BINDING_RE.match(value.strip())
        if not match:
            return value
        step_no = int(match.group(1))
        path = match.group(2)
        if not _path_allowed(path):
            raise BindingError(f"Binding path not allowlisted: {path}")
        if step_no not in step_results:
            raise BindingError(f"Binding refers to missing step {step_no}")
        return resolve_path(step_results[step_no], path)
    if isinstance(value, dict):
        return {k: resolve_bindings(v, step_results) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_bindings(v, step_results) for v in value]
    return value
