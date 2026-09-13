"""Deny-by-default execution policy."""
from __future__ import annotations

from andes_agent.tools.registry import ToolSpec, get_tool


class PolicyError(Exception):
    def __init__(self, code: str, message: str, status: int = 403):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def authorize_tool(name: str) -> ToolSpec:
    spec = get_tool(name)
    if spec is None:
        raise PolicyError("tool_not_allowed", "Tool no permitida")
    if spec.write:
        raise PolicyError("tool_not_allowed", "Write tools are denied by default")
    return spec
