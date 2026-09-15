"""Top-level orchestrator chat runner."""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Callable

from app.assistant.orchestrator.audit import OrchestratorAudit, message_hash
from app.assistant.orchestrator.catalog import MAX_REPLANS
from app.assistant.orchestrator.composer import compose_answer
from app.assistant.orchestrator.conversation_context import (
    KIND_CLARIFY,
    KIND_PLAN_HINTS,
    KIND_REUSE,
    ConversationResolver,
    ResolveResult,
    build_redacted_summary,
    extract_entities_from_evidence,
    merge_entities,
)
from app.assistant.orchestrator.factory import build_planner
from app.assistant.orchestrator.history_config import history_enabled
from app.assistant.orchestrator.history_store import HistoryStore, get_default_history_store
from app.assistant.orchestrator.input_guard import InputGuardError, detect_write_intent, guard_message
from app.assistant.orchestrator.llm.client import LlmError
from app.assistant.orchestrator.memory_config import memory_enabled
from app.assistant.orchestrator.memory_epoch import PermissionEpochProvider
from app.assistant.orchestrator.memory_selector import select_memory_hints
from app.assistant.orchestrator.memory_store import MemoryStore
from app.assistant.orchestrator.metrics import (
    MetricsStore,
    build_turn_metric,
    get_default_metrics_store,
)
from app.assistant.orchestrator.plan_validator import PlanValidationError, validate_plan
from app.assistant.orchestrator.planner import Planner
from app.assistant.orchestrator.tool_runner import ToolRunnerError, run_plan_steps
from app.assistant.orchestrator.turn_store import TurnStore, get_default_turn_store

InvokeFn = Callable[[dict[str, Any]], tuple[int, dict[str, Any]]]


def _clarify_plan(reason: str, *, scenario: str = "ambiguous") -> dict[str, Any]:
    return {
        "plan_id": "llm-clarify",
        "user_intent": "replan",
        "answer_style": "operational",
        "needs_clarification": True,
        "reject_message": reason,
        "scenario": scenario,
        "steps": [],
    }


def _reuse_plan() -> dict[str, Any]:
    return {
        "plan_id": "reuse-prior",
        "user_intent": "reuse_prior_evidence",
        "answer_style": "operational",
        "reuse_prior_evidence": True,
        "reject": False,
        "needs_clarification": False,
        "scenario": "context_reuse",
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


def _append_turn(
    store: TurnStore,
    *,
    actor: str,
    conversation_id: str,
    text: str,
    tools_used: list[Any],
    scenario: Any,
    evidence: list[dict[str, Any]],
    reply: str,
    history_store: HistoryStore | None = None,
    persist_history: bool = False,
    correlation_id: str | None = None,
    classification: str | None = None,
    flags: dict[str, Any] | None = None,
    llm_latency_ms: int = 0,
    total_latency_ms: int = 0,
    planner_mode: str | None = None,
) -> None:
    fresh = extract_entities_from_evidence(evidence)
    prev_turns = store.get(actor, conversation_id)
    if prev_turns:
        hist = [
            t.get("entities") if isinstance(t.get("entities"), dict) else None
            for t in prev_turns
        ]
        entities = merge_entities(*hist, fresh)
    else:
        entities = fresh
    turn_payload = {
        "message_hash": message_hash(text),
        "tools_used": tools_used,
        "scenario": scenario,
        "entities": entities,
        "evidence": evidence,
        "reply_excerpt": (reply or "")[:200],
        "classification": classification,
        "correlation_id": correlation_id,
        "flags": flags or {},
        "llm_latency_ms": llm_latency_ms,
        "total_latency_ms": total_latency_ms,
        "planner_mode": planner_mode,
    }
    store.append(actor, conversation_id, turn_payload)
    if persist_history and history_store is not None:
        try:
            history_store.append_turn(actor, conversation_id, turn_payload)
        except Exception:
            # Never break chat on history persistence failures
            pass


def _resolve_history_conversation(
    *,
    actor: str,
    raw_conversation_id: str,
    history: HistoryStore | None,
) -> tuple[str, bool]:
    """Return (conversation_id, persist_history).

    When history flag is OFF → identical to prior behavior (client id as-is).
    When ON → server UUID via HistoryStore; DB failure falls back to RAM-only id.
    """
    raw = str(raw_conversation_id or "")[:80]
    if not history_enabled() or history is None:
        return raw, False

    try:
        if raw:
            existing = history.get_conversation(actor, raw)
            if existing:
                return str(existing["id"]), True
            ensured = history.ensure_conversation(actor, client_conversation_id=raw)
        else:
            ensured = history.ensure_conversation(actor)
        if ensured and ensured.get("id"):
            return str(ensured["id"]), True
    except Exception:
        pass
    # DB unavailable: continue with RAM-only conversation id
    return raw or str(uuid.uuid4()), False


def _hydrate_turns_from_history(
    *,
    store: TurnStore,
    history: HistoryStore,
    actor: str,
    conversation_id: str,
) -> None:
    if store.get(actor, conversation_id):
        return
    try:
        hydrated = history.recent_turns_for_resolver(actor, conversation_id, limit=6)
        if hydrated:
            store.replace(actor, conversation_id, hydrated)
    except Exception:
        pass


def _planner_mode_label(planner: Any) -> str:
    name = type(planner).__name__
    if "Llm" in name:
        return "llm"
    if "Fake" in name:
        return "fake"
    return name.lower()[:32]


def _accumulate_usage(totals: dict[str, int], planner: Any) -> None:
    usage = getattr(planner, "last_usage", None)
    if not isinstance(usage, dict):
        return
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        val = usage.get(key)
        if val is None:
            continue
        try:
            totals[key] = int(totals.get(key) or 0) + int(val)
        except (TypeError, ValueError):
            continue


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
    turn_store: TurnStore | None = None,
    resolver: ConversationResolver | None = None,
    metrics_store: MetricsStore | None = None,
    history_store: HistoryStore | None = None,
    memory_store: MemoryStore | None = None,
    memory_epoch_provider: PermissionEpochProvider | None = None,
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
    store = turn_store if turn_store is not None else get_default_turn_store()
    resolver = resolver or ConversationResolver()
    metrics = metrics_store if metrics_store is not None else get_default_metrics_store()
    history = history_store if history_store is not None else (
        get_default_history_store() if history_enabled() else None
    )
    conversation_id, persist_history = _resolve_history_conversation(
        actor=actor,
        raw_conversation_id=str(conversation_id or ""),
        history=history,
    )
    if persist_history and history is not None:
        _hydrate_turns_from_history(
            store=store,
            history=history,
            actor=actor,
            conversation_id=conversation_id,
        )
    llm_latency_ms = 0
    usage_totals: dict[str, int] = {}
    planner_mode = _planner_mode_label(planner)
    memory_obs: dict[str, Any] = {
        "memory_candidates": 0,
        "memory_selected": 0,
        "memory_budget_chars": 0,
        "memory_types": [],
    }

    def _elapsed_ms() -> int:
        return int((time.perf_counter() - t0) * 1000)

    def _finish(result: dict[str, Any]) -> dict[str, Any]:
        result.setdefault("conversation_id", conversation_id)
        return result

    def _save_turn(
        *,
        text: str,
        tools_used: list[Any],
        scenario: Any,
        evidence: list[dict[str, Any]],
        reply: str,
        classification: str | None = None,
        flags: dict[str, Any] | None = None,
    ) -> None:
        _append_turn(
            store,
            actor=actor,
            conversation_id=conversation_id,
            text=text,
            tools_used=tools_used,
            scenario=scenario,
            evidence=evidence,
            reply=reply,
            history_store=history,
            persist_history=persist_history,
            correlation_id=correlation_id,
            classification=classification,
            flags=flags,
            llm_latency_ms=llm_latency_ms,
            total_latency_ms=_elapsed_ms(),
            planner_mode=planner_mode,
        )

    def _emit_metric(
        *,
        ok: bool,
        message_hash_value: str,
        error_code: str | None = None,
        phase: str | None = None,
        scenario: str | None = None,
        classification: str | None = None,
        tools_used: list[str] | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        needs_clarification: bool = False,
        reuse_prior_evidence: bool = False,
        replan_count: int = 0,
    ) -> None:
        try:
            metric = build_turn_metric(
                actor_user=actor,
                conversation_id=conversation_id,
                correlation_id=correlation_id,
                message_hash_value=message_hash_value,
                ok=ok,
                error_code=error_code,
                phase=phase,
                scenario=scenario,
                classification=classification,
                tools_used=tools_used,
                tool_calls=tool_calls,
                needs_clarification=needs_clarification,
                reuse_prior_evidence=reuse_prior_evidence,
                replan_count=replan_count,
                llm_latency_ms=llm_latency_ms,
                total_latency_ms=_elapsed_ms(),
                prompt_tokens=usage_totals.get("prompt_tokens"),
                completion_tokens=usage_totals.get("completion_tokens"),
                total_tokens=usage_totals.get("total_tokens"),
                planner_mode=planner_mode,
                memory_candidates=int(memory_obs.get("memory_candidates") or 0),
                memory_selected=int(memory_obs.get("memory_selected") or 0),
                memory_budget_chars=int(memory_obs.get("memory_budget_chars") or 0),
                memory_types=list(memory_obs.get("memory_types") or []),
            )
            metrics.record_turn(metric)
        except Exception:
            # Metrics must never break chat
            pass

    try:
        text = guard_message(message)
    except InputGuardError as exc:
        mh = message_hash(str(message or ""))
        audit.write(
            {
                **_audit_base(
                    actor=actor,
                    conversation_id=conversation_id,
                    correlation_id=correlation_id,
                    message_hash_value=mh,
                ),
                "error_code": exc.code,
                "phase": "input_guard",
                "ok": False,
                "total_latency_ms": _elapsed_ms(),
            }
        )
        _emit_metric(
            ok=False,
            message_hash_value=mh,
            error_code=exc.code,
            phase="input_guard",
        )
        return _finish({
            "ok": False,
            "error_code": exc.code,
            "message": exc.message,
            "http_status": 400,
            "correlation_id": correlation_id,
        })

    mh = message_hash(text)

    # WRITE never gains power from conversational references.
    if detect_write_intent(text):
        audit.write(
            {
                **_audit_base(
                    actor=actor,
                    conversation_id=conversation_id,
                    correlation_id=correlation_id,
                    message_hash_value=mh,
                ),
                "error_code": "write_not_allowed",
                "phase": "write_guard",
                "ok": True,
                "classification": "INTERNAL",
                "scenario": "write_reject",
                "tools": [],
                "llm_latency_ms": 0,
                "total_latency_ms": _elapsed_ms(),
            }
        )
        _emit_metric(
            ok=True,
            message_hash_value=mh,
            error_code=None,
            phase="write_guard",
            scenario="write_reject",
            classification="INTERNAL",
            tools_used=[],
        )
        return _finish({
            "ok": True,
            "reply": "Solo puedo consultar información; no puedo crear, anular ni modificar datos.",
            "error_code": None,
            "tools_used": [],
            "classification": "INTERNAL",
            "scenario": "write_reject",
            "grounded": True,
            "http_status": 200,
            "correlation_id": correlation_id,
        })

    turns = store.get(actor, conversation_id)
    resolved: ResolveResult = resolver.resolve(text, turns)

    if resolved.kind == KIND_CLARIFY:
        plan = validate_plan(
            _clarify_plan(
                resolved.clarify_message or "Necesito más detalles para continuar.",
                scenario="context_ambiguous",
            )
        )
        composed = compose_answer(plan=plan, evidence=[], reject_message=plan.get("reject_message"))
        audit.write(
            {
                **_audit_base(
                    actor=actor,
                    conversation_id=conversation_id,
                    correlation_id=correlation_id,
                    message_hash_value=mh,
                ),
                "scenario": plan.get("scenario"),
                "needs_clarification": True,
                "tools": [],
                "tool_calls": [],
                "ok": True,
                "classification": composed.get("classification"),
                "total_latency_ms": _elapsed_ms(),
            }
        )
        _emit_metric(
            ok=True,
            message_hash_value=mh,
            scenario=str(plan.get("scenario") or ""),
            classification=composed.get("classification"),
            tools_used=[],
            needs_clarification=True,
        )
        _save_turn(
            text=text,
            tools_used=[],
            scenario=plan.get("scenario"),
            evidence=[],
            reply=composed["reply"],
        )
        return _finish({
            "ok": True,
            "reply": composed["reply"],
            "tools_used": [],
            "classification": composed["classification"],
            "scenario": plan.get("scenario"),
            "grounded": True,
            "needs_clarification": True,
            "http_status": 200,
            "correlation_id": correlation_id,
        })

    if resolved.kind == KIND_REUSE:
        plan = validate_plan(_reuse_plan())
        evidence = list(resolved.prior_evidence or [])
        composed = compose_answer(plan=plan, evidence=evidence)
        audit.write(
            {
                **_audit_base(
                    actor=actor,
                    conversation_id=conversation_id,
                    correlation_id=correlation_id,
                    message_hash_value=mh,
                ),
                "scenario": plan.get("scenario"),
                "reuse_prior_evidence": True,
                "tools": [],
                "tool_calls": [],
                "ok": True,
                "classification": composed.get("classification"),
                "total_latency_ms": _elapsed_ms(),
            }
        )
        _emit_metric(
            ok=True,
            message_hash_value=mh,
            scenario=str(plan.get("scenario") or ""),
            classification=composed.get("classification"),
            tools_used=[],
            reuse_prior_evidence=True,
        )
        _save_turn(
            text=text,
            tools_used=[],
            scenario=plan.get("scenario"),
            evidence=evidence,
            reply=composed["reply"],
        )
        return _finish({
            "ok": True,
            "reply": composed["reply"],
            "tools_used": [],
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
            "reuse_prior_evidence": True,
            "http_status": 200,
            "correlation_id": correlation_id,
        })

    context: dict[str, Any] = {}
    if force_scenario:
        context["force_scenario"] = force_scenario
    summary = build_redacted_summary(turns)
    if summary:
        context["conversation_summary"] = summary
    if turns:
        context["last_tools"] = [
            t
            for turn in turns
            for t in (turn.get("tools_used") or [])
        ][-6:]
    if resolved.kind == KIND_PLAN_HINTS:
        context["resolved_entities"] = dict(resolved.entities or {})
        if resolved.intent_hint:
            context["intent_hint"] = resolved.intent_hint

    # FASE 7B.2 — controlled memory read (auxiliary only; never authority)
    if memory_enabled():
        try:
            selection = select_memory_hints(
                actor_user=actor,
                conversation_id=conversation_id,
                store=memory_store,
                epoch_provider=memory_epoch_provider,
            )
            memory_obs["memory_candidates"] = int(selection.candidates_count)
            memory_obs["memory_selected"] = int(selection.selected_count)
            memory_obs["memory_budget_chars"] = int(selection.budget_chars)
            memory_obs["memory_types"] = list(selection.selected_types)
            if selection.hints:
                context["memory_hints"] = selection.hints
        except Exception:
            logging.getLogger(__name__).warning("assistant_memory selection soft-failed")
            memory_obs["memory_candidates"] = 0
            memory_obs["memory_selected"] = 0
            memory_obs["memory_budget_chars"] = 0
            memory_obs["memory_types"] = []

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
            _accumulate_usage(usage_totals, planner)
        except LlmError as exc:
            if exc.code == "llm_invalid_json":
                if replan_count >= MAX_REPLANS:
                    plan = validate_plan(_clarify_plan(f"No pude armar un plan válido ({exc.message}). ¿Puedes reformular?"))
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
                        message_hash_value=mh,
                    ),
                    "error_code": exc.code,
                    "phase": "llm_planner",
                    "replan_count": replan_count,
                    "ok": False,
                    "llm_latency_ms": llm_latency_ms,
                    "total_latency_ms": _elapsed_ms(),
                }
            )
            _emit_metric(
                ok=False,
                message_hash_value=mh,
                error_code=exc.code,
                phase="llm_planner",
                replan_count=replan_count,
            )
            return _finish({
                "ok": False,
                "error_code": "llm_unavailable",
                "message": (
                    "El asistente en lenguaje natural no está disponible. "
                    "Usa comandos /buscar, /stock, /kpis, etc."
                ),
                "http_status": 503,
                "correlation_id": correlation_id,
            })

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
                            message_hash_value=mh,
                        ),
                        "error_code": exc.code,
                        "phase": "plan_validator",
                        "ok": True,
                        "classification": "INTERNAL",
                        "llm_latency_ms": llm_latency_ms,
                        "total_latency_ms": _elapsed_ms(),
                    }
                )
                _emit_metric(
                    ok=True,
                    message_hash_value=mh,
                    phase="plan_validator",
                    scenario="write_reject",
                    classification="INTERNAL",
                    tools_used=[],
                    replan_count=replan_count,
                )
                return _finish({
                    "ok": True,
                    "reply": "Solo puedo consultar información; no puedo crear, anular ni modificar datos.",
                    "error_code": None,
                    "tools_used": [],
                    "classification": "INTERNAL",
                    "scenario": "write_reject",
                    "grounded": True,
                    "http_status": 200,
                    "correlation_id": correlation_id,
                })
            if replan_count >= MAX_REPLANS:
                audit.write(
                    {
                        **_audit_base(
                            actor=actor,
                            conversation_id=conversation_id,
                            correlation_id=correlation_id,
                            message_hash_value=mh,
                        ),
                        "error_code": exc.code,
                        "phase": "plan_validator",
                        "replan_count": replan_count,
                        "ok": False,
                        "llm_latency_ms": llm_latency_ms,
                        "total_latency_ms": _elapsed_ms(),
                    }
                )
                _emit_metric(
                    ok=False,
                    message_hash_value=mh,
                    error_code=exc.code,
                    phase="plan_validator",
                    replan_count=replan_count,
                )
                return _finish({
                    "ok": False,
                    "error_code": exc.code,
                    "message": exc.message,
                    "http_status": 400,
                    "correlation_id": correlation_id,
                })
            replan_count += 1
            validation_error = exc.message
            continue

    assert plan is not None

    if plan.get("reuse_prior_evidence"):
        from app.assistant.orchestrator.conversation_context import (
            _merged_entities,
            evidence_for_tool,
        )

        evidence = list(resolved.prior_evidence or [])
        if not evidence:
            codigo = None
            if isinstance(resolved.entities, dict):
                codigo = resolved.entities.get("codigo")
            if not codigo and turns:
                codigo = _merged_entities(turns).get("codigo")
            for tool_name in (
                "get_inventory",
                "get_stock_movements",
                "get_supplier",
                "search_catalog",
            ):
                evidence = evidence_for_tool(turns, tool_name, codigo=codigo)
                if evidence:
                    break

        if evidence:
            composed = compose_answer(plan=validate_plan(_reuse_plan()), evidence=evidence)
            _emit_metric(
                ok=True,
                message_hash_value=mh,
                scenario="context_reuse",
                classification=composed.get("classification"),
                tools_used=[],
                reuse_prior_evidence=True,
                replan_count=replan_count,
            )
            _save_turn(
            text=text,
            tools_used=[],
            scenario="context_reuse",
            evidence=evidence,
            reply=composed["reply"],
        )
            return _finish({
                "ok": True,
                "reply": composed["reply"],
                "tools_used": [],
                "classification": composed["classification"],
                "scenario": "context_reuse",
                "grounded": True,
                "reuse_prior_evidence": True,
                "http_status": 200,
                "correlation_id": correlation_id,
            })

        # No recoverable evidence: if we have plan hints for a tool, run that instead
        if resolved.kind == KIND_PLAN_HINTS and resolved.intent_hint == "inventory":
            codigo = str((resolved.entities or {}).get("codigo") or "").strip().upper()
            if codigo:
                plan = validate_plan(
                    {
                        "plan_id": "ctx-inventory-fallback",
                        "user_intent": f"Stock de {codigo}",
                        "answer_style": "operational",
                        "scenario": "inventory_only",
                        "steps": [
                            {
                                "step": 1,
                                "tool": "get_inventory",
                                "arguments": {"codigo": codigo},
                                "reason": "follow-up stock desde contexto",
                            }
                        ],
                    },
                    replan_count=replan_count,
                )
                # fall through to tool runner
            else:
                plan = validate_plan(
                    _clarify_plan(
                        "No tengo evidencia anterior para reutilizar. ¿Qué quieres consultar?"
                    )
                )
                composed = compose_answer(
                    plan=plan, evidence=[], reject_message=plan.get("reject_message")
                )
                _emit_metric(
                    ok=True,
                    message_hash_value=mh,
                    scenario=str(plan.get("scenario") or ""),
                    classification=composed.get("classification"),
                    tools_used=[],
                    needs_clarification=True,
                    replan_count=replan_count,
                )
                return _finish({
                    "ok": True,
                    "reply": composed["reply"],
                    "tools_used": [],
                    "classification": composed["classification"],
                    "scenario": plan.get("scenario"),
                    "grounded": True,
                    "needs_clarification": True,
                    "http_status": 200,
                    "correlation_id": correlation_id,
                })
        else:
            plan = validate_plan(
                _clarify_plan(
                    "No tengo evidencia anterior para reutilizar. ¿Qué quieres consultar?"
                )
            )
            composed = compose_answer(
                plan=plan, evidence=[], reject_message=plan.get("reject_message")
            )
            _emit_metric(
                ok=True,
                message_hash_value=mh,
                scenario=str(plan.get("scenario") or ""),
                classification=composed.get("classification"),
                tools_used=[],
                needs_clarification=True,
                replan_count=replan_count,
            )
            return _finish({
                "ok": True,
                "reply": composed["reply"],
                "tools_used": [],
                "classification": composed["classification"],
                "scenario": plan.get("scenario"),
                "grounded": True,
                "needs_clarification": True,
                "http_status": 200,
                "correlation_id": correlation_id,
            })

    if plan.get("reject") or plan.get("needs_clarification"):
        composed = compose_answer(plan=plan, evidence=[], reject_message=plan.get("reject_message"))
        audit.write(
            {
                **_audit_base(
                    actor=actor,
                    conversation_id=conversation_id,
                    correlation_id=correlation_id,
                    message_hash_value=mh,
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
        _emit_metric(
            ok=True,
            message_hash_value=mh,
            scenario=str(plan.get("scenario") or ""),
            classification=composed.get("classification"),
            tools_used=[],
            needs_clarification=bool(plan.get("needs_clarification")),
            replan_count=replan_count,
        )
        _save_turn(
            text=text,
            tools_used=[],
            scenario=plan.get("scenario"),
            evidence=[],
            reply=composed["reply"],
        )
        return _finish({
            "ok": True,
            "reply": composed["reply"],
            "tools_used": [],
            "classification": composed["classification"],
            "scenario": plan.get("scenario"),
            "grounded": True,
            "needs_clarification": bool(plan.get("needs_clarification")),
            "http_status": 200,
            "correlation_id": correlation_id,
        })

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
                    message_hash_value=mh,
                ),
                "error_code": exc.code,
                "phase": "tool_runner",
                "ok": False,
                "llm_latency_ms": llm_latency_ms,
                "total_latency_ms": _elapsed_ms(),
            }
        )
        _emit_metric(
            ok=False,
            message_hash_value=mh,
            error_code=exc.code,
            phase="tool_runner",
            replan_count=replan_count,
        )
        return _finish({
            "ok": False,
            "error_code": exc.code,
            "message": exc.message,
            "http_status": 400,
            "correlation_id": correlation_id,
        })

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
                message_hash_value=mh,
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
            "memory_candidates": memory_obs.get("memory_candidates"),
            "memory_selected": memory_obs.get("memory_selected"),
            "memory_budget_chars": memory_obs.get("memory_budget_chars"),
            "memory_types": list(memory_obs.get("memory_types") or []),
        }
    )
    _emit_metric(
        ok=True,
        message_hash_value=mh,
        scenario=str(plan.get("scenario") or ""),
        classification=composed.get("classification"),
        tools_used=[str(t) for t in tools_used if t],
        tool_calls=tool_calls,
        replan_count=replan_count,
    )

    _save_turn(
            text=text,
            tools_used=tools_used,
            scenario=plan.get("scenario"),
            evidence=evidence,
            reply=composed["reply"],
        )

    return _finish({
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
    })
