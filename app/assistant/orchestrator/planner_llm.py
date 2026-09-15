"""LlmPlanner — produces Plan JSON via OpenAI-compatible API; never executes tools."""
from __future__ import annotations

import json
from typing import Any

from app.assistant.orchestrator.input_guard import detect_write_intent
from app.assistant.orchestrator.llm.client import LlmClient, LlmError
from app.assistant.orchestrator.llm.prompts import build_system_prompt, build_user_prompt
from app.assistant.orchestrator.scenarios import SCENARIO_WRITE, scenario_fixtures


def _strip_nulls(value: Any) -> Any:
    """Remove nulls produced by OpenAI strict nullable fields before PlanValidator."""
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


class LlmPlanner:
    def __init__(self, client: LlmClient):
        self.client = client
        self.last_usage: dict[str, int] | None = None

    def plan(self, message: str, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
        context = context or {}
        self.last_usage = None

        # Hard reject WRITE before spending tokens
        if detect_write_intent(message):
            return dict(scenario_fixtures()[SCENARIO_WRITE])

        replan_error = None
        if context.get("replan"):
            replan_error = str(context.get("validation_error") or "invalid plan")

        summary_parts: list[str] = []
        if context.get("conversation_summary"):
            summary_parts.append(str(context["conversation_summary"]))
        resolved = context.get("resolved_entities")
        if isinstance(resolved, dict) and resolved:
            bits = []
            if resolved.get("codigo"):
                bits.append(f"codigo={resolved['codigo']}")
            codes = resolved.get("codigos") or []
            if isinstance(codes, list) and codes:
                bits.append("codigos=" + ",".join(str(c) for c in codes[:5]))
            if resolved.get("proveedor_nombre") or resolved.get("proveedor_q"):
                bits.append(
                    "proveedor="
                    + str(resolved.get("proveedor_nombre") or resolved.get("proveedor_q"))
                )
            if resolved.get("oc_numero"):
                bits.append(f"oc={resolved['oc_numero']}")
            if context.get("intent_hint"):
                bits.append(f"intent_hint={context['intent_hint']}")
            if bits:
                summary_parts.append("entidades_resueltas: " + "; ".join(bits))
        memory_hints = context.get("memory_hints")
        if isinstance(memory_hints, list) and memory_hints:
            # Compact JSON only — auxiliary preferences/entities; never permissions
            try:
                summary_parts.append(
                    "memory_hints: "
                    + json.dumps(memory_hints, ensure_ascii=False, separators=(",", ":"))
                )
            except (TypeError, ValueError):
                pass
        conversation_context = "\n".join(summary_parts) if summary_parts else None

        system = build_system_prompt()
        user = build_user_prompt(
            message,
            replan_error=replan_error,
            conversation_context=conversation_context,
        )

        try:
            raw = self.client.complete_plan_json(system=system, user=user)
            data = json.loads(raw)
        except LlmError:
            raise
        except json.JSONDecodeError as exc:
            raise LlmError("llm_invalid_json", f"Invalid JSON: {exc}") from exc

        usage = getattr(self.client, "last_usage", None)
        if isinstance(usage, dict):
            self.last_usage = {
                k: int(v)
                for k, v in usage.items()
                if k in {"prompt_tokens", "completion_tokens", "total_tokens"} and v is not None
            }

        if not isinstance(data, dict):
            raise LlmError("llm_invalid_json", "Plan must be a JSON object")
        return _strip_nulls(data)
