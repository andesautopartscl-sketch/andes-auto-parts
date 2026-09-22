"""Top-level orchestrator chat runner."""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Callable

from app.assistant.orchestrator.audit import OrchestratorAudit, message_hash
from app.assistant.orchestrator.agent_config import agent_loop_allowed
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
from app.assistant.orchestrator.memory_derived import apply_derived_memory
from app.assistant.orchestrator.memory_epoch import PermissionEpochProvider
from app.assistant.orchestrator.memory_explicit import apply_chat_explicit_memory
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


def _card_scope(agent_obs: dict[str, Any]) -> set[str] | None:
    """La evidencia que sostiene el texto publicado, o None para no acotar.

    FASE 9.8 — `build_answer_view` acepta `scope` justamente para esto y se
    llamaba sin el. Demostrado con el store cargado: con get_product (marca
    MAXUS) y get_inventory (marca BOSCH), la vista publicaba una tarjeta con
    BOSCH aunque el texto solo citara la ficha. Eso es una segunda verdad por la
    interfaz, que es exactamente lo que el docstring de la vista prohibe.

    DOS GUARDAS, y las dos existen para que esto solo pueda QUITAR tarjetas que
    se demuestran no citadas, nunca dejar la vista vacia:

    1. si ningun claim declaro `evidence_ids`, no hay nada que demostrar y no se
       acota — el comportamiento queda como estaba;
    2. si el compositor tomo el relevo, el texto publicado ya NO sale de los
       claims sino de toda la evidencia, asi que acotarlo lo dejaria mas estrecho
       que el propio texto.
    """
    desglose = agent_obs.get("verifier_breakdown") or {}
    if desglose.get("answer_replaced") or agent_obs.get("fallback_used"):
        return None
    citados = {str(x) for x in (desglose.get("cited_evidence_ids") or []) if x}
    return citados or None


def _answer_view_dict(
    *,
    plan: dict[str, Any],
    evidence: list[dict[str, Any]],
    correlation_id: str,
    store: Any | None = None,
    scope: set[str] | None = None,
) -> dict[str, Any] | None:
    """FASE 8.4 — proyeccion estructurada de la evidencia ya verificada.

    Se construye en LAS DOS rutas, agente y planner, a proposito: AGENT=0 es el
    default desplegado, asi que una experiencia estructurada que solo existiera
    con el agente encendido naceria muerta.

    Nunca puede tumbar un turno: si la proyeccion falla, la respuesta de texto
    sale igual y simplemente no hay tarjetas.
    """
    try:
        from app.assistant.orchestrator.answer_view import build_answer_view
        from app.assistant.orchestrator.evidence_store import EvidenceStore

        if store is None:
            from app.assistant.orchestrator.agent_loop import _ingest_plan_evidence

            store = EvidenceStore()
            _ingest_plan_evidence(store, plan, evidence, correlation_id)
        view = build_answer_view(store, scope=scope)
        return None if view.is_empty() else view.as_dict()
    except Exception:  # noqa: BLE001 — la vista es aditiva, jamas obligatoria
        logging.getLogger(__name__).debug("answer_view soft-failed", exc_info=True)
        return None


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
    agent_decision_client: Any | None = None,
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
        "memory_write_attempt": False,
        "memory_write_success": False,
        "memory_write_rejected": False,
        "memory_write_reason": None,
        "memory_write_type": None,
        "memory_write_scope": None,
        "permission_epoch_read": None,
        "memory_contextual_invalidated": 0,
        "memory_contextual_selected": 0,
        "permission_epoch_error": False,
        "derived_candidates": 0,
        "derived_accepted": 0,
        "derived_rejected": 0,
        "derived_reject_reason": None,
        "derived_type": None,
        "derived_scope": None,
        "derived_confidence": None,
    }
    agent_obs: dict[str, Any] = {
        "agent_enabled": False,
        "agent_steps": 0,
        "evidence_size_chars": 0,
        "verifier_failures": 0,
        "retries": 0,
        "loop_detected": False,
        "timeout": False,
        "fallback_used": False,
        "fallback_reason": None,
        "agent_trace": [],
        "goal_coverage": None,
        "arg_errors": [],
        "budget": None,
        "token_economics": None,
        "evidence_degradation": None,
        "sufficiency": None,
        "verifier_breakdown": None,
        "agent_progress": None,
    }

    def _elapsed_ms() -> int:
        return int((time.perf_counter() - t0) * 1000)

    def _finish(result: dict[str, Any]) -> dict[str, Any]:
        result.setdefault("conversation_id", conversation_id)
        # FASE 9.7 — la forma se declara en UN sitio. Hay catorce puntos de
        # salida en esta funcion y varios son atajos conversacionales
        # —aclaracion, rechazo de escritura, reutilizacion de contexto—, que son
        # justo los turnos que 9.7 existe para describir. Ponerlo en cada
        # `return` deja huecos: medido, los dos turnos anaforicos de N11 salian
        # con el campo vacio. Derivarlo aqui no puede tener huecos.
        if "response_kind" not in result:
            if not result.get("ok", True) or result.get("error_code"):
                # La salida mas temprana ocurre ANTES de que exista el mensaje
                # saneado: no hay nada que clasificar y la forma ya se sabe.
                result["response_kind"] = "error"
            else:
                try:
                    from app.assistant.orchestrator.response_shape import (
                        classify_response)

                    result["response_kind"] = classify_response(
                        message=text,
                        evidence=[],
                        needs_clarification=bool(result.get("needs_clarification")),
                        reject=bool(result.get("scenario") == "write_reject"),
                    )
                except Exception:  # noqa: BLE001 — una etiqueta no tumba un turno
                    logging.getLogger(__name__).debug(
                        "response_shape soft-failed", exc_info=True)
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
                memory_write_attempt=bool(memory_obs.get("memory_write_attempt")),
                memory_write_success=bool(memory_obs.get("memory_write_success")),
                memory_write_rejected=bool(memory_obs.get("memory_write_rejected")),
                memory_write_reason=(
                    str(memory_obs.get("memory_write_reason"))[:80]
                    if memory_obs.get("memory_write_reason")
                    else None
                ),
                memory_write_type=(
                    str(memory_obs.get("memory_write_type"))[:40]
                    if memory_obs.get("memory_write_type")
                    else None
                ),
                memory_write_scope=(
                    str(memory_obs.get("memory_write_scope"))[:20]
                    if memory_obs.get("memory_write_scope")
                    else None
                ),
                permission_epoch_read=(
                    int(memory_obs["permission_epoch_read"])
                    if memory_obs.get("permission_epoch_read") is not None
                    else None
                ),
                memory_contextual_invalidated=int(
                    memory_obs.get("memory_contextual_invalidated") or 0
                ),
                memory_contextual_selected=int(
                    memory_obs.get("memory_contextual_selected") or 0
                ),
                permission_epoch_error=bool(memory_obs.get("permission_epoch_error")),
                derived_candidates=int(memory_obs.get("derived_candidates") or 0),
                derived_accepted=int(memory_obs.get("derived_accepted") or 0),
                derived_rejected=int(memory_obs.get("derived_rejected") or 0),
                derived_reject_reason=(
                    str(memory_obs.get("derived_reject_reason"))[:80]
                    if memory_obs.get("derived_reject_reason")
                    else None
                ),
                derived_type=(
                    str(memory_obs.get("derived_type"))[:40]
                    if memory_obs.get("derived_type")
                    else None
                ),
                derived_scope=(
                    str(memory_obs.get("derived_scope"))[:20]
                    if memory_obs.get("derived_scope")
                    else None
                ),
                derived_confidence=(
                    float(memory_obs["derived_confidence"])
                    if memory_obs.get("derived_confidence") is not None
                    else None
                ),
                agent_enabled=bool(agent_obs.get("agent_enabled")),
                agent_steps=int(agent_obs.get("agent_steps") or 0),
                tool_latency_ms=[
                    int(c.get("latency_ms"))
                    for c in (tool_calls or [])
                    if isinstance(c, dict) and c.get("latency_ms") is not None
                ],
                evidence_size_chars=int(agent_obs.get("evidence_size_chars") or 0),
                verifier_failures=int(agent_obs.get("verifier_failures") or 0),
                retries=int(agent_obs.get("retries") or 0),
                loop_detected=bool(agent_obs.get("loop_detected")),
                timeout=bool(agent_obs.get("timeout")),
                fallback_used=bool(agent_obs.get("fallback_used")),
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

    # FASE 7B.3 — explicit memory write (auxiliary; never ToolRunner / permissions)
    text_for_plan = text
    mem_write_note: str | None = None
    try:
        text_for_plan, write_res = apply_chat_explicit_memory(
            message=text,
            actor_user=actor,
            conversation_id=conversation_id,
            store=memory_store,
        )
    except Exception:
        logging.getLogger(__name__).warning("assistant_memory explicit chat path soft-failed")
        write_res = None
        text_for_plan = text

    if write_res is not None:
        memory_obs["memory_write_attempt"] = True
        memory_obs["memory_write_type"] = write_res.memory_type
        memory_obs["memory_write_scope"] = write_res.scope
        memory_obs["memory_write_success"] = bool(write_res.ok)
        memory_obs["memory_write_rejected"] = bool(write_res.rejected)
        memory_obs["memory_write_reason"] = write_res.error_code
        if write_res.ok:
            mem_write_note = write_res.confirmation
        else:
            mem_write_note = write_res.message or "No pude guardar la memoria."

        # Memory-only turn: confirm or report failure; do not invent success
        if not (text_for_plan or "").strip():
            reply = mem_write_note or (
                "Listo, lo recordaré." if write_res.ok else "No pude guardar la memoria."
            )
            audit.write(
                {
                    **_audit_base(
                        actor=actor,
                        conversation_id=conversation_id,
                        correlation_id=correlation_id,
                        message_hash_value=mh,
                    ),
                    "phase": "memory_explicit",
                    "ok": True,
                    "memory_write_attempt": True,
                    "memory_write_success": bool(write_res.ok),
                    "memory_write_rejected": bool(write_res.rejected),
                    "memory_write_reason": write_res.error_code,
                    "memory_write_type": write_res.memory_type,
                    "memory_write_scope": write_res.scope,
                    "tools": [],
                    "total_latency_ms": _elapsed_ms(),
                }
            )
            _emit_metric(
                ok=True,
                message_hash_value=mh,
                phase="memory_explicit",
                scenario="memory_explicit",
                classification="INTERNAL",
                tools_used=[],
            )
            _save_turn(
                text=text,
                tools_used=[],
                scenario="memory_explicit",
                evidence=[],
                reply=reply,
                classification="INTERNAL",
                flags={"memory_explicit": True, "memory_write_ok": bool(write_res.ok)},
            )
            return _finish({
                "ok": True,
                "reply": reply,
                "error_code": None if write_res.ok else write_res.error_code,
                "tools_used": [],
                "classification": "INTERNAL",
                "scenario": "memory_explicit",
                "grounded": True,
                "http_status": 200,
                "correlation_id": correlation_id,
                "memory_saved": bool(write_res.ok),
            })

    text = text_for_plan or text

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
        # Do not apply_derived_memory: reused evidence is not a new qualified hit.
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
            memory_obs["permission_epoch_read"] = selection.permission_epoch_read
            memory_obs["memory_contextual_invalidated"] = int(
                selection.memory_contextual_invalidated
            )
            memory_obs["memory_contextual_selected"] = int(
                selection.memory_contextual_selected
            )
            memory_obs["permission_epoch_error"] = bool(selection.permission_epoch_error)
            if selection.hints:
                context["memory_hints"] = selection.hints
        except Exception:
            logging.getLogger(__name__).warning("assistant_memory selection soft-failed")
            memory_obs["memory_candidates"] = 0
            memory_obs["memory_selected"] = 0
            memory_obs["memory_budget_chars"] = 0
            memory_obs["memory_types"] = []
            memory_obs["permission_epoch_error"] = True
            memory_obs["memory_contextual_invalidated"] = 0
            memory_obs["memory_contextual_selected"] = 0
            memory_obs["permission_epoch_read"] = None

    replan_count = 0
    validation_error: str | None = None
    plan: dict[str, Any] | None = None
    evidence: list[dict[str, Any]] = []

    if agent_loop_allowed():
        from app.assistant.orchestrator.agent_loop import empty_agent_plan

        plan = empty_agent_plan()

    while plan is None:
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
                # FASE 9 — se propaga el codigo REAL. Aplanarlo todo en
                # "llm_unavailable" borraba la diferencia entre una credencial
                # rechazada y un proveedor caido, y por eso 354 turnos con una
                # clave placeholder se leyeron como "el proveedor no responde".
                # El mensaje al usuario no cambia: el codigo es para diagnostico.
                "error_code": exc.code or "llm_unavailable",
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
            # Do not apply_derived_memory: reused evidence is not a new qualified hit.
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

    if agent_loop_allowed():
        evidence = []
    else:
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

    composed: dict[str, Any]
    if agent_loop_allowed():
        try:
            from app.assistant.orchestrator.agent_loop import (
                LlmAgentDecisionClient,
                continue_agent_loop,
            )
            from app.assistant.orchestrator.llm.client import OpenAICompatibleClient
            from app.assistant.orchestrator.llm.config import load_llm_settings

            client = agent_decision_client
            if client is None:
                client = LlmAgentDecisionClient(OpenAICompatibleClient(load_llm_settings()))
            loop_out = continue_agent_loop(
                message=text,
                actor_user=actor,
                conversation_id=conversation_id,
                correlation_id=correlation_id,
                initial_plan=plan,
                initial_evidence=evidence,
                invoke_fn=invoke_fn,
                decision_client=client,
                memory_hints=(
                    context.get("memory_hints")
                    if isinstance(context.get("memory_hints"), list)
                    else None
                ),
                context=context,
                usage_totals=usage_totals,
            )
            evidence = loop_out.raw_evidence
            composed = {
                "reply": loop_out.reply,
                "classification": loop_out.classification,
                "grounded": True,
            }
            llm_latency_ms += int(loop_out.state.llm_latency_ms or 0)
            agent_obs["agent_enabled"] = True
            agent_obs["agent_steps"] = int(loop_out.state.step_index)
            agent_obs["evidence_size_chars"] = int(loop_out.state.evidence.size_chars())
            agent_obs["verifier_failures"] = int(loop_out.state.verifier_failures)
            agent_obs["retries"] = int(loop_out.state.retries)
            agent_obs["loop_detected"] = bool(loop_out.state.loop_detected)
            agent_obs["timeout"] = bool(loop_out.state.timeout)
            agent_obs["fallback_used"] = bool(loop_out.fallback_used)
            agent_obs["fallback_reason"] = loop_out.fallback_reason
            agent_obs["agent_trace"] = list(loop_out.state.trace)
            agent_obs["goal_coverage"] = loop_out.state.goal.safe_snapshot()
            agent_obs["arg_errors"] = list(loop_out.state.arg_errors)
            agent_obs["verifier_breakdown"] = dict(loop_out.state.verifier_breakdown or {})
            agent_obs["agent_progress"] = loop_out.state.ledger.safe_snapshot()
            from app.assistant.orchestrator.agent_config import budget_snapshot

            agent_obs["budget"] = budget_snapshot()
            # FASE 8.2D — economia real del turno. Solo conteos: ni prompts, ni
            # respuestas, ni secretos. Sirve para saber cuanto del prompt se
            # reenvio y cuanto de eso el proveedor sirvio desde cache.
            # OJO: NO llamar a esto "evidence_summary". Esa clave ya existe en la
            # respuesta publica del orquestador y es una LISTA de evidencias por
            # herramienta (lineas ~709 y ~1337). Reutilizar el nombre metia un
            # dict donde los consumidores esperan una lista, y como era una clave
            # duplicada dentro del MISMO literal, Python se quedaba en silencio
            # con la ultima: la lista publica desaparecia en la ruta del agente.
            agent_obs["evidence_degradation"] = loop_out.state.evidence.safe_summary()
            agent_obs["sufficiency"] = dict(loop_out.state.sufficiency or {})
            # La vista se proyecta del store del propio loop: es la evidencia que
            # el verifier tuvo delante, no una segunda lectura del payload crudo.
            agent_obs["evidence_store"] = loop_out.state.evidence
            agent_obs["token_economics"] = {
                "prompt_tokens": int(loop_out.state.token_in),
                "completion_tokens": int(loop_out.state.token_out),
                "cached_tokens": int(loop_out.state.token_cached),
                "decisions": len(loop_out.state.decisions),
                "invokes": int(loop_out.state.invoke_count),
            }
            if loop_out.needs_clarification:
                plan["needs_clarification"] = True
            if loop_out.scenario:
                plan["scenario"] = loop_out.scenario
        except Exception:
            logging.getLogger(__name__).warning("assistant_agent_loop soft-failed; composer fallback")
            composed = compose_answer(plan=plan, evidence=evidence)
            agent_obs["agent_enabled"] = True
            agent_obs["fallback_used"] = True
    else:
        composed = compose_answer(plan=plan, evidence=evidence)
    if mem_write_note:
        base_reply = (composed.get("reply") or "").rstrip()
        composed["reply"] = f"{base_reply}\n\n{mem_write_note}" if base_reply else mem_write_note
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
    _save_turn(
            text=text,
            tools_used=tools_used,
            scenario=plan.get("scenario"),
            evidence=evidence,
            reply=composed["reply"],
        )

    # FASE 7B.5 — derived memory (post-turn, best-effort; chat already composed)
    try:
        derived = apply_derived_memory(
            message=text,
            actor_user=actor,
            conversation_id=conversation_id,
            evidence=evidence,
            turns=store.get(actor, conversation_id),
            store=memory_store,
            tools_used=tools_used,
            reuse_prior_evidence=False,
        )
        memory_obs["derived_candidates"] = int(derived.candidates)
        memory_obs["derived_accepted"] = int(derived.accepted)
        memory_obs["derived_rejected"] = int(derived.rejected)
        memory_obs["derived_reject_reason"] = derived.reject_reason
        memory_obs["derived_type"] = derived.memory_type
        memory_obs["derived_scope"] = derived.scope
        memory_obs["derived_confidence"] = derived.confidence
    except Exception:
        logging.getLogger(__name__).warning("assistant_memory derived chat path soft-failed")
        memory_obs["derived_reject_reason"] = "derived_failed"

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
            "memory_write_attempt": memory_obs.get("memory_write_attempt"),
            "memory_write_success": memory_obs.get("memory_write_success"),
            "memory_write_rejected": memory_obs.get("memory_write_rejected"),
            "memory_write_reason": memory_obs.get("memory_write_reason"),
            "memory_write_type": memory_obs.get("memory_write_type"),
            "memory_write_scope": memory_obs.get("memory_write_scope"),
            "permission_epoch_read": memory_obs.get("permission_epoch_read"),
            "memory_contextual_invalidated": memory_obs.get("memory_contextual_invalidated"),
            "memory_contextual_selected": memory_obs.get("memory_contextual_selected"),
            "permission_epoch_error": memory_obs.get("permission_epoch_error"),
            "derived_candidates": memory_obs.get("derived_candidates"),
            "derived_accepted": memory_obs.get("derived_accepted"),
            "derived_rejected": memory_obs.get("derived_rejected"),
            "derived_reject_reason": memory_obs.get("derived_reject_reason"),
            "derived_type": memory_obs.get("derived_type"),
            "derived_scope": memory_obs.get("derived_scope"),
            "agent_enabled": agent_obs.get("agent_enabled"),
            "agent_steps": agent_obs.get("agent_steps"),
            "fallback_used": agent_obs.get("fallback_used"),
            # Sin la razon el audit dice QUE hubo fallback pero no POR QUE, y
            # agent_limit / agent_token_budget / agent_no_progress / tool_error /
            # agent_verifier_failed son diagnosticos muy distintos.
            "fallback_reason": agent_obs.get("fallback_reason"),
            "loop_detected": agent_obs.get("loop_detected"),
            "timeout": agent_obs.get("timeout"),
            "verifier_failures": agent_obs.get("verifier_failures"),
            "agent_trace": list(agent_obs.get("agent_trace") or []),
            "goal_coverage": agent_obs.get("goal_coverage"),
            "arg_errors": list(agent_obs.get("arg_errors") or []),
            "verifier_breakdown": agent_obs.get("verifier_breakdown"),
            "agent_progress": agent_obs.get("agent_progress"),
            "budget": agent_obs.get("budget"),
            "token_economics": agent_obs.get("token_economics"),
            "evidence_degradation": agent_obs.get("evidence_degradation"),
            "sufficiency": agent_obs.get("sufficiency"),
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

    # FASE 9.7 — que forma tiene esta respuesta. Se DEDUCE de lo que el turno ya
    # hizo (evidencia, banderas, mensaje); no decide contenido ni llama a nadie.
    # Sirve para dos cosas concretas: no colgar tarjetas de una charla, y que la
    # forma sea observable en metricas en vez de adivinarse desde el texto.
    try:
        from app.assistant.orchestrator.response_shape import (
            classify_response, wants_cards)

        response_kind = classify_response(
            message=text,
            evidence=evidence,
            needs_clarification=bool(plan.get("needs_clarification")),
            reject=bool(plan.get("reject")),
            error_code=None,
        )
    except Exception:  # noqa: BLE001 — una etiqueta no puede tumbar un turno
        logging.getLogger(__name__).debug("response_shape soft-failed", exc_info=True)
        response_kind, wants_cards = "data_answer", (lambda _k: True)

    return _finish({
        "ok": True,
        "reply": composed["reply"],
        "response_kind": response_kind,
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
        "agent_enabled": bool(agent_obs.get("agent_enabled")),
        "fallback_used": bool(agent_obs.get("fallback_used")),
        "fallback_reason": agent_obs.get("fallback_reason"),
        "loop_detected": bool(agent_obs.get("loop_detected")),
        "verifier_failures": int(agent_obs.get("verifier_failures") or 0),
        "agent_trace": list(agent_obs.get("agent_trace") or []),
        "goal_coverage": agent_obs.get("goal_coverage"),
        "arg_errors": list(agent_obs.get("arg_errors") or []),
        "verifier_breakdown": agent_obs.get("verifier_breakdown"),
        "agent_progress": agent_obs.get("agent_progress"),
        "budget": agent_obs.get("budget"),
        "token_economics": agent_obs.get("token_economics"),
        "evidence_degradation": agent_obs.get("evidence_degradation"),
        "sufficiency": agent_obs.get("sufficiency"),
        # Una charla no lleva tarjetas: un saludo con una ficha de producto
        # colgando es la version visual del "DATOS:" delante de "Hola".
        "view": _answer_view_dict(
            plan=plan, evidence=evidence, correlation_id=correlation_id,
            store=agent_obs.get("evidence_store"),
            scope=_card_scope(agent_obs),
        ) if wants_cards(response_kind) else None,
    })
