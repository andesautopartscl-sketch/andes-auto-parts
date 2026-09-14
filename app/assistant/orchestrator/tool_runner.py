"""Execute validated plan steps via BFF invoke_gateway only."""
from __future__ import annotations

import time
from typing import Any, Callable

from app.assistant.orchestrator.arg_schema import ArgSchemaError, validate_tool_args
from app.assistant.orchestrator.bindings import BindingError, resolve_bindings
from app.assistant.orchestrator.catalog import ALLOWED_TOOLS, MAX_INVOKES
from app.assistant.orchestrator.normalizer import normalize_tool_result

InvokeFn = Callable[[dict[str, Any]], tuple[int, dict[str, Any]]]


class ToolRunnerError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def run_plan_steps(
    plan: dict[str, Any],
    *,
    actor_user: str,
    conversation_id: str,
    invoke_fn: InvokeFn,
) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    """Returns (evidence list, step payloads by step number for bindings)."""
    if not (actor_user or "").strip():
        raise ToolRunnerError("principal_required", "actor_user is required")

    evidence: list[dict[str, Any]] = []
    step_payloads: dict[int, dict[str, Any]] = {}
    invokes = 0

    for step in plan.get("steps") or []:
        if invokes >= MAX_INVOKES:
            raise ToolRunnerError("limit_exceeded", f"Exceeded max {MAX_INVOKES} invokes")

        tool = str(step.get("tool") or "")
        if tool not in ALLOWED_TOOLS:
            raise ToolRunnerError("tool_not_allowed", f"Tool '{tool}' is not allowed")

        deps = step.get("depends_on") or []
        for dep in deps:
            prev = evidence_by_step(evidence, dep)
            if prev is None:
                raise ToolRunnerError("dependency_missing", f"Missing dependency step {dep}")
            if not prev.get("ok"):
                evidence.append(
                    {
                        "step": step["step"],
                        "tool": tool,
                        "ok": False,
                        "skipped": True,
                        "error_code": "dependency_failed",
                        "message": f"Skipped because step {dep} failed",
                        "data": {},
                        "meta": {},
                        "classification": "INTERNAL",
                        "empty": True,
                        "latency_ms": 0,
                        "status": 0,
                        "finance_redacted": False,
                        "stock_omitted": False,
                    }
                )
                return evidence, step_payloads
            if prev.get("empty"):
                evidence.append(
                    {
                        "step": step["step"],
                        "tool": tool,
                        "ok": False,
                        "skipped": True,
                        "error_code": "dependency_empty",
                        "message": f"Skipped because step {dep} returned no results",
                        "data": {},
                        "meta": {},
                        "classification": "INTERNAL",
                        "empty": True,
                        "latency_ms": 0,
                        "status": 0,
                        "finance_redacted": False,
                        "stock_omitted": False,
                    }
                )
                return evidence, step_payloads

        try:
            arguments = resolve_bindings(step.get("arguments") or {}, step_payloads)
            arguments = validate_tool_args(tool, arguments)
        except BindingError as exc:
            evidence.append(_err_evidence(step, "binding_error", exc.message))
            return evidence, step_payloads
        except ArgSchemaError as exc:
            evidence.append(_err_evidence(step, exc.code, exc.message))
            return evidence, step_payloads

        payload = {
            "agent_id": "andes-assistant",
            "conversation_id": str(conversation_id or "")[:80],
            "actor_user": actor_user.strip(),
            "tool": tool,
            "arguments": arguments,
        }
        for forbidden in ("Authorization", "authorization", "cookie", "Cookie", "token", "password"):
            if forbidden in payload:
                raise ToolRunnerError("invalid_args", f"Forbidden payload key '{forbidden}'")

        t0 = time.perf_counter()
        try:
            status, body = invoke_fn(payload)
        except Exception as exc:  # noqa: BLE001 — map transport failures
            latency_ms = int((time.perf_counter() - t0) * 1000)
            normalized = normalize_tool_result(
                503,
                {"ok": False, "error_code": "agent_unavailable", "message": str(exc)[:200]},
            )
            normalized["step"] = step["step"]
            normalized["tool"] = tool
            normalized["latency_ms"] = latency_ms
            evidence.append(normalized)
            return evidence, step_payloads

        latency_ms = int((time.perf_counter() - t0) * 1000)
        invokes += 1

        if not isinstance(status, int):
            status = 502
        normalized = normalize_tool_result(status, body)
        normalized["step"] = step["step"]
        normalized["tool"] = tool
        normalized["latency_ms"] = latency_ms
        evidence.append(normalized)

        step_payloads[step["step"]] = {
            "ok": normalized["ok"],
            "data": normalized.get("data") or {},
            "meta": normalized.get("meta") or {},
            "tool": tool,
        }

        if not normalized["ok"]:
            return evidence, step_payloads

    return evidence, step_payloads


def evidence_by_step(evidence: list[dict[str, Any]], step_no: int) -> dict[str, Any] | None:
    for item in evidence:
        if item.get("step") == step_no:
            return item
    return None


def _err_evidence(step: dict[str, Any], code: str, message: str) -> dict[str, Any]:
    return {
        "step": step["step"],
        "tool": step["tool"],
        "ok": False,
        "error_code": code,
        "message": message,
        "data": {},
        "meta": {},
        "classification": "INTERNAL",
        "empty": True,
        "latency_ms": 0,
        "status": 400,
        "finance_redacted": False,
        "stock_omitted": False,
        "write": False,
    }
