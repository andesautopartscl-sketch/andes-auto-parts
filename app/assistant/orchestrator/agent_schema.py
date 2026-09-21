"""FASE 8.1 — closed AgentLoop decision schema (Python-side authority)."""
from __future__ import annotations

import json
import re
from typing import Any

from app.assistant.orchestrator.analysis import AssumptionError, validate_assumptions
from app.assistant.orchestrator.arg_schema import ArgSchemaError, validate_tool_args
from app.assistant.orchestrator.catalog import ALLOWED_TOOLS, WRITE_TOOLS
from app.assistant.orchestrator.goal_coverage import parse_proposed_requirements, parse_unresolved
from app.assistant.orchestrator.tool_contracts import (
    normalize_agent_args,
    structured_invalid_args,
    tool_arg_key_map,
)

ACTIONS = frozenset({"call_tool", "final_answer", "clarify", "reject"})
# FASE 8.1I — "count" is the only op that reads a collection instead of numbers.
# 8.5 anade 'mul': sin multiplicacion "12 unidades/mes x 2 meses" no era
# expresable, asi que ninguna proyeccion podia recomputarse y toda cifra
# proyectada quedaba indistinguible de una alucinacion.
CALC_OPS = frozenset({"min", "max", "sum", "diff", "ratio", "count", "mul"})
# FASE 8.5 — la escalera analitica. 'dato' e 'inferencia' son los de 8.1 y su
# tratamiento no cambia. Los nuevos peldanos existen para que una cifra DERIVADA
# tenga donde vivir: antes una proyeccion solo podia declararse 'dato' (y la
# tumbaba el grounding, con razon) o 'inferencia' (y la tumbaba la regla de que
# una inferencia no lleva cifras). Sin peldanos, toda la mitad analitica del
# negocio era inalcanzable.
CLAIM_KINDS = frozenset({"dato", "inferencia", "calculo", "supuesto",
                         "proyeccion", "recomendacion"})
_DIGIT_RE = re.compile(r"\d")
_WRITE_PREFIXES = ("create_", "write_", "delete_", "update_", "insert_", "remove_")
# One decision = one action. Multi-step planner fields are forbidden.
_MULTI_STEP_KEYS = frozenset(
    {
        "steps",
        "n_steps",
        "plan",
        "plan_id",
        "bindings",
        "depends_on",
        "tools",
        "tool_calls",
    }
)
# Derived from tool_contracts (same keys as arg_schema). Do not hand-maintain a third copy.
_TOOL_ARG_KEYS = tool_arg_key_map()


class AgentDecisionError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def _strip_nulls(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if item is None:
                continue
            cleaned = _strip_nulls(item)
            if cleaned is None:
                continue
            out[key] = cleaned
        return out
    if isinstance(value, list):
        return [_strip_nulls(item) for item in value if item is not None]
    return value


def _text_has_figures(text: str) -> bool:
    return bool(_DIGIT_RE.search(text or ""))


def _is_write_tool(tool: str) -> bool:
    t = (tool or "").strip().lower()
    if t in WRITE_TOOLS:
        return True
    return t.startswith(_WRITE_PREFIXES)


def validate_agent_decision(raw: Any, *, user_message: str | None = None,
                            context: Any = None) -> dict[str, Any]:
    raw = _strip_nulls(raw)
    if not isinstance(raw, dict):
        raise AgentDecisionError("invalid_decision", "Decision must be a JSON object")

    extra_plan = sorted(k for k in raw.keys() if str(k) in _MULTI_STEP_KEYS)
    if extra_plan:
        raise AgentDecisionError(
            "invalid_decision",
            "AgentDecision is a single act; forbidden fields: " + ", ".join(extra_plan),
        )

    action = str(raw.get("action") or "").strip()
    if action not in ACTIONS:
        raise AgentDecisionError("invalid_decision", f"Unknown action '{action}'")

    raw_tool = raw.get("tool")
    if isinstance(raw_tool, (list, tuple)):
        raise AgentDecisionError("invalid_decision", "call_tool allows exactly one tool, not an array")
    tool = str(raw_tool or "").strip()
    arguments = raw.get("arguments")
    if arguments is None:
        arguments = {}
    if arguments != {} and not isinstance(arguments, dict):
        raise AgentDecisionError("invalid_args", "arguments must be an object")
    arguments = dict(arguments) if isinstance(arguments, dict) else {}

    reason = str(raw.get("reason") or "")[:300]
    draft_reply = str(raw.get("draft_reply") or "")
    claims_in = raw.get("claims") if "claims" in raw else []
    calcs_in = raw.get("calculations") if "calculations" in raw else []
    if claims_in is None:
        claims_in = []
    if calcs_in is None:
        calcs_in = []
    if not isinstance(claims_in, list):
        raise AgentDecisionError("invalid_decision", "claims must be a list")
    if not isinstance(calcs_in, list):
        raise AgentDecisionError("invalid_decision", "calculations must be a list")

    claims: list[dict[str, Any]] = []
    for claim in claims_in:
        if not isinstance(claim, dict):
            continue
        kind = str(claim.get("kind") or "").strip().lower()
        if kind not in CLAIM_KINDS:
            raise AgentDecisionError(
                "invalid_decision",
                "claim.kind must be one of " + "|".join(sorted(CLAIM_KINDS)))
        eids = claim.get("evidence_ids") or claim.get("evidence_refs") or []
        if not isinstance(eids, list):
            eids = []
        claims.append(
            {
                "kind": kind,
                # Una proyeccion cuelga de supuestos: sin ellos no es una
                # proyeccion, es una cifra sin respaldo.
                "assumption_ids": [
                    str(x) for x in (claim.get("assumption_ids") or []) if x][:6],
                "calculation_ids": [
                    str(x) for x in (claim.get("calculation_ids") or []) if x][:6],
                "text": str(claim.get("text") or "")[:500],
                "evidence_ids": [str(x) for x in eids if x][:12],
                "justification": str(claim.get("justification") or "")[:300],
            }
        )

    try:
        assumptions = validate_assumptions(raw.get("assumptions"), question=user_message or "")
    except AssumptionError as exc:
        # Se rechaza la decision entera: dejar pasar la proyeccion sin su
        # supuesto publicaria una cifra derivada sin su condicional, que es peor
        # que no responder.
        raise AgentDecisionError("invalid_decision", f"assumption rejected: {exc}") from exc
    proposed_requirements = parse_proposed_requirements(raw.get("proposed_requirements"))
    unresolved = parse_unresolved(raw.get("unresolved"))

    calculations: list[dict[str, Any]] = []
    for calc in calcs_in:
        if not isinstance(calc, dict):
            raise AgentDecisionError("invalid_decision", "each calculation must be an object")
        cid = str(calc.get("id") or "").strip()
        if not cid:
            raise AgentDecisionError("invalid_decision", "calculation.id is required")
        op = str(calc.get("op") or "").strip().lower()
        if op not in CALC_OPS:
            raise AgentDecisionError("invalid_decision", f"calculation.op '{op}' is not allowed")
        inputs = calc.get("inputs")
        if not isinstance(inputs, list) or not inputs:
            raise AgentDecisionError("invalid_decision", "calculation.inputs required")
        if op in {"diff", "ratio"} and len(inputs) != 2:
            raise AgentDecisionError("invalid_decision", f"{op} requires exactly 2 inputs")
        if op == "count" and len(inputs) != 1:
            raise AgentDecisionError("invalid_decision", "count requires exactly 1 input")
        if "result" not in calc:
            raise AgentDecisionError("invalid_decision", "calculation.result is required")
        try:
            result_f = float(calc.get("result"))
        except (TypeError, ValueError) as exc:
            raise AgentDecisionError("invalid_decision", "calculation.result must be numeric") from exc
        calculations.append(
            {
                "id": cid[:32],
                "op": op,
                "inputs": [str(x) for x in inputs][:20],
                "result": result_f,
            }
        )

    if action == "call_tool":
        if not tool:
            raise AgentDecisionError("invalid_decision", "call_tool requires tool")
        if _is_write_tool(tool):
            raise AgentDecisionError("write_not_allowed", f"WRITE tool '{tool}' is forbidden")
        if tool not in ALLOWED_TOOLS:
            raise AgentDecisionError("tool_not_allowed", f"Tool '{tool}' is not in the allowlist")
        try:
            arguments = normalize_agent_args(tool, arguments,
                                             user_message=user_message,
                                             context=context)
            try:
                arguments = validate_tool_args(tool, arguments)
            except ArgSchemaError:
                core = {}
                if "codigo" in arguments:
                    core["codigo"] = arguments["codigo"]
                if "q" in arguments:
                    core["q"] = arguments["q"]
                arguments = validate_tool_args(tool, core)
        except ArgSchemaError as exc:
            raise AgentDecisionError(
                exc.code or "invalid_args",
                exc.message,
                details=structured_invalid_args(tool, arguments),
            ) from exc
        return {
            "action": action,
            "tool": tool,
            "arguments": arguments,
            "reason": reason,
            "draft_reply": "",
            "claims": [],
            "calculations": [],
            "proposed_requirements": proposed_requirements,
            "unresolved": unresolved,
        }

    # Non-call_tool acts never carry a tool call.
    # Contract (FASE 8.1F.4):
    # - Residual union-bag ``arguments`` (e.g. q/limit) are discarded → {}.
    # - A non-empty ``tool`` name is rejected (not silently kept or executed).
    # GoalCoverage still evaluates uncovered requirements after a valid final_answer.
    if tool:
        raise AgentDecisionError("invalid_decision", f"{action} must not include a tool call")
    tool = ""
    arguments = {}

    if action == "final_answer":
        blob = draft_reply + json.dumps(claims, ensure_ascii=False)
        if _text_has_figures(blob) and not claims:
            raise AgentDecisionError(
                "claims_required",
                "final_answer with figures requires claims grounded in evidence",
            )
        if not draft_reply.strip() and not claims:
            raise AgentDecisionError("invalid_decision", "final_answer requires draft_reply or claims")
        return {
            "action": action,
            "tool": tool,
            "arguments": arguments,
            "reason": reason,
            "draft_reply": draft_reply[:4000],
            "claims": claims,
            "calculations": calculations,
            "assumptions": [a.as_dict() for a in assumptions],
            "proposed_requirements": proposed_requirements,
            "unresolved": unresolved,
        }

    # clarify / reject
    return {
        "action": action,
        "tool": tool,
        "arguments": arguments,
        "reason": reason,
        "draft_reply": str(raw.get("draft_reply") or raw.get("reject_message") or "")[:500],
        "claims": [],
        "calculations": [],
        "reject_message": str(raw.get("reject_message") or raw.get("draft_reply") or "")[:500],
        "proposed_requirements": proposed_requirements,
        "unresolved": unresolved,
    }
