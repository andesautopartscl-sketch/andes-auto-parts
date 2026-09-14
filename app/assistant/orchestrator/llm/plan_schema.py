"""JSON Schema for orchestrator Plan — OpenAI strict + PlanValidator allowlist."""
from __future__ import annotations

from app.assistant.orchestrator.catalog import ALLOWED_TOOLS, MAX_STEPS

# OpenAI strict mode requires additionalProperties=false and every property listed in required.
# Optional values use nullable unions; LlmPlanner strips nulls before PlanValidator.

_ARGUMENT_PROPS: dict = {
    "q": {"type": ["string", "null"]},
    "limit": {"type": ["integer", "null"]},
    "codigo": {"type": ["string", "null"]},
    "marca": {"type": ["string", "null"]},
    "bodega": {"type": ["string", "null"]},
    "fecha_desde": {"type": ["string", "null"]},
    "fecha_hasta": {"type": ["string", "null"]},
    "periodo": {"type": ["string", "null"]},
    "numero": {"type": ["string", "null"]},
    "estado": {"type": ["string", "null"]},
    "proveedor": {"type": ["string", "null"]},
    "rut": {"type": ["string", "null"]},
    "id": {"type": ["integer", "null"]},
    "top_limit": {"type": ["integer", "null"]},
    "stock_threshold": {"type": ["integer", "null"]},
    "stock_limit": {"type": ["integer", "null"]},
    "items": {
        "type": ["array", "null"],
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["codigo", "cantidad", "marca", "bodega"],
            "properties": {
                "codigo": {"type": "string"},
                "cantidad": {"type": "integer"},
                "marca": {"type": ["string", "null"]},
                "bodega": {"type": ["string", "null"]},
            },
        },
    },
}

PLAN_JSON_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "plan_id",
        "user_intent",
        "steps",
        "answer_style",
        "needs_clarification",
        "reject",
        "reject_code",
        "reject_message",
        "scenario",
    ],
    "properties": {
        "plan_id": {"type": "string"},
        "user_intent": {"type": "string"},
        "answer_style": {
            "type": "string",
            "enum": ["operational", "confidential", "financial"],
        },
        "needs_clarification": {"type": "boolean"},
        "reject": {"type": "boolean"},
        "reject_code": {"type": ["string", "null"]},
        "reject_message": {"type": ["string", "null"]},
        "scenario": {"type": ["string", "null"]},
        "steps": {
            "type": "array",
            "maxItems": MAX_STEPS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["step", "tool", "arguments", "reason", "depends_on"],
                "properties": {
                    "step": {"type": "integer", "minimum": 1},
                    "tool": {"type": "string", "enum": sorted(ALLOWED_TOOLS)},
                    "arguments": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": sorted(_ARGUMENT_PROPS.keys()),
                        "properties": _ARGUMENT_PROPS,
                    },
                    "reason": {"type": ["string", "null"]},
                    "depends_on": {
                        "type": ["array", "null"],
                        "items": {"type": "integer", "minimum": 1},
                    },
                },
            },
        },
    },
}


def plan_response_format() -> dict:
    """OpenAI-compatible response_format for structured Plan JSON."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "andes_orchestrator_plan",
            "strict": True,
            "schema": PLAN_JSON_SCHEMA,
        },
    }
