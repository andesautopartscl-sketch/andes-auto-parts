"""Top-level orchestrator chat runner."""
from __future__ import annotations

import time
import uuid
from typing import Any, Callable

from app.assistant.orchestrator.audit import OrchestratorAudit, message_hash
from app.assistant.orchestrator.catalog import MAX_REPLANS
from app.assistant.orchestrator.composer import compose_answer
from app.assistant.orchestrator.factory import build_planner
from app.assistant.orchestrator.input_guard import InputGuardError, guard_message
from app.assistant.orchestrator.llm.client import LlmError
from app.assistant.orchestrator.plan_validator import PlanValidationError, validate_plan
from app.assistant.orchestrator.planner import Planner
from app.assistant.orchestrator.tool_runner import ToolRunnerError, run_plan_steps

InvokeFn = Callable[[dict[str, Any]], tuple[int, dict[str, Any]]]


def _clarify_plan(reason: str) -> dict[str, Any]:
    return {
        "plan_id": "llm-clarify",
        "user_intent": "replan",
        "answer_style": "operational",
        "needs_clarification": True,
        "reject_message": f"No pude armar un plan válido ({reason}). ¿Puedes reformular?",
        "scenario": "ambiguous",
        "steps": [],
    }


def _audit_base(
    *,
    actor: str,
    conversation_id: str,
    correlation_id: str,
    message_hash_value: str,
) -> dict[str, Any]:
    return {
        "actor_user": actor,
        "conversation_id": conversation_id,
        "correlation_id": correlation_id,
        "message_hash": message_hash_value,
    }


def run_orchestrator_chat(
    *,
    message: Any,
    actor_user: str,
    conversation_id: str = "",
    invoke_fn: InvokeFn,
    planner: Planner | None = None,
    audit: OrchestratorAudit | None = None,
    force_scenario: str | None = None,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    actor = (actor_user or "").strip()
    correlation_id = (correlation_id or "").strip() or str(uuid.uuid4())
    if not actor:
        return {
            "ok": False,
            "error_code": "unauthorized",
            "message": "Debe iniciar sesión.",
            "http_status": 401,
            "correlation_id": correlation_id,
        }

    audit = audit or OrchestratorAudit()
    planner = planner or build_planner()
    conversation_id = str(conversation_id or "")[:80]
    llm_latency_ms = 0

    def _elapsed_ms() -> int:
        return int((time.perf_counter() - t0) * 1000)

    try:
        text = guard_message(message)
    except InputGuardError as exc:
        audit.write(
            {
                **_audit_base(
                    actor=actor,
                    conversation_id=conversation_id,
                    correlation_id=correlation_id,
                    message_hash_value=message_hash(str(message or "")),
                ),
                "error_code": exc.code,
                "phase": "input_guard",
                "ok": False,
                "total_latency_ms": _elapsed_ms(),
            }
        )
        return {
            "ok": False,
            "error_code": exc.code,
            "message": exc.message,
            "http_status": 400,
            "correlation_id": correlation_id,
        }

    context: dict[str, Any] = {}
    if force_scenario:
        context["force_scenario"] = force_scenario

    replan_count = 0
    validation_error: str | None = None
    plan: dict[str, Any] | None = None

    while True:
        ctx = dict(context)
        if replan_count > 0:
            ctx["replan"] = True
            ctx["validation_error"] = validation_error or "invalid plan"

        try:
            t_llm = time.perf_counter()
            plan_raw = planner.plan(text, context=ctx)
            llm_latency_ms += int((time.perf_counter() - t_llm) * 1000)
        except LlmError as exc:
            if exc.code == "llm_invalid_json":
                if replan_count >= MAX_REPLANS:
                    plan = validate_plan(_clarify_plan(exc.message), replan_count=replan_count)
                    break
                replan_count += 1
                validation_error = exc.message
                continue
            audit.write(
                {
                    **_audit_base(
                        actor=actor,
                        conversation_id=conversation_id,
                        correlation_id=correlation_id,
                        message_hash_value=message_hash(text),
                    ),
                    "error_code": exc.code,
                    "phase": "llm_planner",
                    "replan_count": replan_count,
                    "ok": False,
                    "llm_latency_ms": llm_latency_ms,
                    "total_latency_ms": _elapsed_ms(),
                }
            )
            return {
                "ok": False,
                "error_code": "llm_unavailable",
                "message": (
                    "El asistente en lenguaje natural no está disponible. "
                    "Usa comandos /buscar, /stock, /kpis, etc."
                ),
                "http_status": 503,
                "correlation_id": correlation_id,
            }

        try:
            plan = validate_plan(plan_raw, replan_count=replan_count)
            break
        except PlanValidationError as exc:
            if exc.code == "write_not_allowed":
                audit.write(
                    {
                        **_audit_base(
                            actor=actor,
                            conversation_id=conversation_id,
                            correlation_id=correlation_id,
                            message_hash_value=message_hash(text),
                        ),
                        "error_code": exc.code,
                        "phase": "plan_validator",
                        "ok": True,
                        "classification": "INTERNAL",
                        "llm_latency_ms": llm_latency_ms,
                        "total_latency_ms": _elapsed_ms(),
                    }
                )
                return {
                    "ok": True,
                    "reply": "Solo puedo consultar información; no puedo crear, anular ni modificar datos.",
                    "error_code": None,
                    "tools_used": [],
                    "classification": "INTERNAL",
                    "scenario": "write_reject",
                    "grounded": True,
                    "http_status": 200,
                    "correlation_id": correlation_id,
                }
            if replan_count >= MAX_REPLANS:
                audit.write(
                    {
                        **_audit_base(
                            actor=actor,
                            conversation_id=conversation_id,
                            correlation_id=correlation_id,
                            message_hash_value=message_hash(text),
                        ),
                        "error_code": exc.code,
                        "phase": "plan_validator",
                        "replan_count": replan_count,
                        "ok": False,
                        "llm_latency_ms": llm_latency_ms,
                        "total_latency_ms": _elapsed_ms(),
                    }
                )
                return {
                    "ok": False,
                    "error_code": exc.code,
                    "message": exc.message,
                    "http_status": 400,
                    "correlation_id": correlation_id,
                }
            replan_count += 1
            validation_error = exc.message
            continue

    assert plan is not None

    if plan.get("reject") or plan.get("needs_clarification"):
        composed = compose_answer(plan=plan, evidence=[], reject_message=plan.get("reject_message"))
        audit.write(
            {
                **_audit_base(
                    actor=actor,
                    conversation_id=conversation_id,
                    correlation_id=correlation_id,
                    message_hash_value=message_hash(text),
                ),
                "scenario": plan.get("scenario"),
                "reject": bool(plan.get("reject")),
                "needs_clarification": bool(plan.get("needs_clarification")),
                "tools": [],
                "tool_calls": [],
                "replan_count": replan_count,
                "classification": composed.get("classification"),
                "ok": True,
                "llm_latency_ms": llm_latency_ms,
                "total_latency_ms": _elapsed_ms(),
            }
        )
        return {
            "ok": True,
            "reply": composed["reply"],
            "tools_used": [],
            "classification": composed["classification"],
            "scenario": plan.get("scenario"),
            "grounded": True,
            "needs_clarification": bool(plan.get("needs_clarification")),
            "http_status": 200,
            "correlation_id": correlation_id,
        }

    try:
        evidence, _payloads = run_plan_steps(
            plan,
            actor_user=actor,
            conversation_id=conversation_id,
            invoke_fn=invoke_fn,
        )
    except ToolRunnerError as exc:
        audit.write(
            {
                **_audit_base(
                    actor=actor,
                    conversation_id=conversation_id,
                    correlation_id=correlation_id,
                    message_hash_value=message_hash(text),
                ),
                "error_code": exc.code,
                "phase": "tool_runner",
                "ok": False,
                "llm_latency_ms": llm_latency_ms,
                "total_latency_ms": _elapsed_ms(),
            }
        )
        return {
            "ok": False,
            "error_code": exc.code,
            "message": exc.message,
            "http_status": 400,
            "correlation_id": correlation_id,
        }

    composed = compose_answer(plan=plan, evidence=evidence)
    tools_used = [e.get("tool") for e in evidence if e.get("tool")]
    tool_calls = [
        {
            "tool": e.get("tool"),
            "ok": e.get("ok"),
            "error_code": e.get("error_code"),
            "latency_ms": e.get("latency_ms"),
        }
        for e in evidence
    ]
    audit.write(
        {
            **_audit_base(
                actor=actor,
                conversation_id=conversation_id,
                correlation_id=correlation_id,
                message_hash_value=message_hash(text),
            ),
            "scenario": plan.get("scenario"),
            "plan_id": plan.get("plan_id"),
            "tools": tools_used,
            "tool_calls": tool_calls,
            "steps": tool_calls,
            "classification": composed["classification"],
            "replan_count": replan_count,
            "ok": True,
            "llm_latency_ms": llm_latency_ms,
            "total_latency_ms": _elapsed_ms(),
        }
    )

    return {
        "ok": True,
        "reply": composed["reply"],
        "tools_used": tools_used,
        "evidence_summary": [
            {
                "tool": e.get("tool"),
                "ok": e.get("ok"),
                "empty": e.get("empty"),
                "error_code": e.get("error_code"),
                "classification": e.get("classification"),
            }
            for e in evidence
        ],
        "classification": composed["classification"],
        "scenario": plan.get("scenario"),
        "grounded": True,
        "http_status": 200,
        "correlation_id": correlation_id,
    }
