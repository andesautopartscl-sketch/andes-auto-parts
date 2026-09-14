"""Strict plan validation: allowlist, limits, cycles, WRITE rejection, arg schemas."""
from __future__ import annotations

from typing import Any

from app.assistant.orchestrator.arg_schema import ArgSchemaError, validate_tool_args
from app.assistant.orchestrator.bindings import BINDING_RE, _normalize_path
from app.assistant.orchestrator.catalog import (
    ALLOWED_BINDING_PATHS,
    ALLOWED_TOOLS,
    MAX_INVOKES,
    MAX_REPLANS,
    MAX_STEPS,
    WRITE_TOOLS,
)


class PlanValidationError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _as_plan(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PlanValidationError("invalid_plan", "Plan must be a JSON object")
    return raw


def _binding_path_allowed(path: str) -> bool:
    norm = _normalize_path(path)
    allowed_norm = {_normalize_path(p) for p in ALLOWED_BINDING_PATHS}
    return norm in allowed_norm or path.strip() in ALLOWED_BINDING_PATHS


def validate_plan(plan: Any, *, replan_count: int = 0) -> dict[str, Any]:
    if replan_count > MAX_REPLANS:
        raise PlanValidationError("invalid_plan", f"replan_count exceeds max {MAX_REPLANS}")

    plan = _as_plan(plan)

    allowed_top = {
        "plan_id",
        "user_intent",
        "steps",
        "answer_style",
        "needs_clarification",
        "reject",
        "reject_code",
        "reject_message",
        "scenario",
    }
    extra = set(plan.keys()) - allowed_top
    if extra:
        raise PlanValidationError("invalid_plan", f"Unknown plan field(s): {', '.join(sorted(extra))}")

    if plan.get("write") is True:
        raise PlanValidationError("write_not_allowed", "Plans must be read-only")

    if plan.get("reject"):
        return {
            "plan_id": str(plan.get("plan_id") or "rejected"),
            "user_intent": str(plan.get("user_intent") or ""),
            "steps": [],
            "answer_style": "operational",
            "reject": True,
            "reject_code": str(plan.get("reject_code") or "rejected"),
            "reject_message": str(plan.get("reject_message") or "Solicitud rechazada."),
            "scenario": plan.get("scenario"),
            "needs_clarification": False,
        }

    if plan.get("needs_clarification"):
        return {
            "plan_id": str(plan.get("plan_id") or "clarify"),
            "user_intent": str(plan.get("user_intent") or ""),
            "steps": [],
            "answer_style": "operational",
            "reject": False,
            "needs_clarification": True,
            "reject_message": str(plan.get("reject_message") or "Necesito más detalles para continuar."),
            "scenario": plan.get("scenario"),
        }

    steps = plan.get("steps")
    if not isinstance(steps, list):
        raise PlanValidationError("invalid_plan", "'steps' must be a list")
    if len(steps) == 0:
        raise PlanValidationError("invalid_plan", "Plan has no steps")
    if len(steps) > MAX_STEPS:
        raise PlanValidationError("invalid_plan", f"Plan exceeds max {MAX_STEPS} steps")
    if len(steps) > MAX_INVOKES:
        raise PlanValidationError("invalid_plan", f"Plan exceeds max {MAX_INVOKES} invokes")

    cleaned_steps: list[dict[str, Any]] = []
    seen_step_ids: set[int] = set()
    earlier_ids: set[int] = set()

    for idx, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise PlanValidationError("invalid_plan", "Each step must be an object")
        step_allowed = {"step", "tool", "arguments", "reason", "depends_on"}
        extra_step = set(step.keys()) - step_allowed
        if extra_step:
            raise PlanValidationError("invalid_plan", f"Unknown step field(s): {', '.join(sorted(extra_step))}")

        step_no = step.get("step", idx)
        if isinstance(step_no, bool) or not isinstance(step_no, int) or step_no < 1:
            raise PlanValidationError("invalid_plan", "step number must be a positive integer")
        if step_no in seen_step_ids:
            raise PlanValidationError("invalid_plan", f"Duplicate step id {step_no}")
        seen_step_ids.add(step_no)

        tool = str(step.get("tool") or "").strip()
        if not tool:
            raise PlanValidationError("invalid_plan", "step.tool is required")
        # Unknown tools first (except write-like prefixes)
        write_like = tool in WRITE_TOOLS or tool.lower().startswith(
            ("create_", "write_", "delete_", "update_", "insert_", "remove_")
        )
        if write_like:
            raise PlanValidationError("write_not_allowed", f"WRITE tool '{tool}' is forbidden")
        if tool not in ALLOWED_TOOLS:
            raise PlanValidationError("tool_not_allowed", f"Tool '{tool}' is not in the allowlist")

        arguments = step.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise PlanValidationError("invalid_args", "step.arguments must be an object")

        try:
            validate_tool_args(tool, arguments)
        except ArgSchemaError as exc:
            raise PlanValidationError(exc.code, exc.message) from exc

        depends_on = step.get("depends_on") or []
        if not isinstance(depends_on, list):
            raise PlanValidationError("invalid_plan", "depends_on must be a list")
        dep_ids: list[int] = []
        for dep in depends_on:
            if isinstance(dep, bool) or not isinstance(dep, int) or dep < 1:
                raise PlanValidationError("invalid_plan", "depends_on entries must be positive integers")
            if dep >= step_no:
                raise PlanValidationError("invalid_plan", "depends_on cannot reference current/future steps (cycle)")
            if dep not in earlier_ids:
                raise PlanValidationError("invalid_plan", f"depends_on references unknown step {dep}")
            dep_ids.append(dep)

        _check_bindings(arguments, step_no, set(dep_ids))

        cleaned_steps.append(
            {
                "step": step_no,
                "tool": tool,
                "arguments": dict(arguments),
                "reason": str(step.get("reason") or "")[:200],
                "depends_on": dep_ids,
            }
        )
        earlier_ids.add(step_no)

    style = str(plan.get("answer_style") or "operational").strip().lower()
    if style not in {"operational", "confidential", "financial"}:
        style = "operational"

    return {
        "plan_id": str(plan.get("plan_id") or "plan"),
        "user_intent": str(plan.get("user_intent") or "")[:200],
        "steps": cleaned_steps,
        "answer_style": style,
        "reject": False,
        "needs_clarification": False,
        "scenario": plan.get("scenario"),
    }


def _check_bindings(value: Any, step_no: int, allowed_steps: set[int]) -> None:
    if isinstance(value, str):
        match = BINDING_RE.match(value.strip())
        if not match:
            return
        ref = int(match.group(1))
        path = match.group(2)
        if ref >= step_no or ref not in allowed_steps:
            raise PlanValidationError(
                "invalid_plan", f"Binding $steps.{ref} is not allowed for step {step_no}"
            )
        if not _binding_path_allowed(path):
            raise PlanValidationError("invalid_plan", f"Binding path not allowlisted: {path}")
        return
    if isinstance(value, dict):
        for v in value.values():
            _check_bindings(v, step_no, allowed_steps)
    elif isinstance(value, list):
        for v in value:
            _check_bindings(v, step_no, allowed_steps)
