"""FASE 8.1 — optional AgentLoop. Never calls Gateway directly; ToolRunner only."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.assistant.orchestrator.agent_config import (
    MAX_AGENT_STEPS,
    MAX_CONSECUTIVE_TOOL_ERRORS,
    MAX_DECISION_RETRIES,
    MAX_SECONDS,
    MAX_TOOL_CALLS,
    budget_exceeded,
    capability_router_enabled,
)
from app.assistant.orchestrator.agent_progress import (
    PROGRESS_NONE,
    REJECT_REPEAT_CALL,
    ProgressLedger,
    admission_note,
)
from app.assistant.orchestrator.agent_schema import AgentDecisionError, validate_agent_decision
from app.assistant.orchestrator.analysis import analytical_intent
from app.assistant.orchestrator.answer_sufficiency import analyze_sufficiency
from app.assistant.orchestrator.answer_verifier import verify_agent_answer
from app.assistant.orchestrator.catalog import MAX_INVOKES
from app.assistant.orchestrator.composer import compose_answer
from app.assistant.orchestrator.evidence_store import EvidenceItem, EvidenceStore
from app.assistant.orchestrator.goal_coverage import GoalCoverage, covering_tools
from app.assistant.orchestrator.llm.agent_prompts import (
    build_agent_context_note,
    build_agent_system_prompt,
    build_agent_user_prompt,
)
from app.assistant.orchestrator.llm.client import LlmError
from app.assistant.orchestrator.metrics import estimate_cost_usd
from app.assistant.orchestrator.plan_validator import PlanValidationError, validate_plan
from app.assistant.orchestrator.tool_runner import ToolRunnerError, run_plan_steps

logger = logging.getLogger(__name__)

InvokeFn = Callable[[dict[str, Any]], tuple[int, dict[str, Any]]]


def has_unused_covering_for_uncovered(goal: GoalCoverage, evidence: EvidenceStore) -> bool:
    """True when an uncovered requirement still has an unused allowlisted covering tool.

    Progress protection: if every covering tool for an uncovered req was already
    attempted this turn, that req does not keep blocking partial answers.
    Impossible requirements are not uncovered and are ignored here.
    """
    used = {str(item.tool) for item in evidence.items if item.tool}
    for req in goal.uncovered():
        tools = covering_tools(req.type)
        if not tools:
            continue
        if tools - used:
            return True
    return False


def allow_partial_final(
    goal: GoalCoverage,
    evidence: EvidenceStore,
    *,
    remaining: int,
) -> bool:
    """FASE 8.1F.6 — partial finals only when nothing resolvable remains.

    ``blocked_finals`` alone must NOT enable partial while unused covering tools exist.
    """
    if int(remaining) <= 0:
        return True
    return not has_unused_covering_for_uncovered(goal, evidence)


@dataclass
class TurnAgentState:
    correlation_id: str
    actor_user: str
    conversation_id: str
    step_index: int = 0
    decisions: list[dict[str, Any]] = field(default_factory=list)
    evidence: EvidenceStore = field(default_factory=EvidenceStore)
    invoke_count: int = 0
    started_at: float = 0.0
    deadline: float = 0.0
    token_in: int = 0
    token_out: int = 0
    # FASE 8.2D — prompt servido desde cache de prefijo del proveedor. Solo se
    # observa: no participa en budget_exceeded. Ver agent_config.CHARS_PER_TOKEN.
    token_cached: int = 0
    cost_est: float | None = None
    loop_keys: dict[str, int] = field(default_factory=dict)
    fallback_used: bool = False
    final_answer: str = ""
    consecutive_errors: int = 0
    loop_detected: bool = False
    timeout: bool = False
    retries: int = 0
    verifier_failures: int = 0
    fallback_reason: str | None = None
    trace: list[dict[str, Any]] = field(default_factory=list)
    llm_latency_ms: int = 0
    goal: GoalCoverage = field(default_factory=GoalCoverage)
    blocked_finals: int = 0
    coverage_hint: str | None = None
    arg_errors: list[dict[str, Any]] = field(default_factory=list)
    ledger: ProgressLedger = field(default_factory=ProgressLedger)
    no_progress_detected: bool = False
    rejected_decisions: int = 0
    blocked_final_released: bool = False
    verifier_breakdown: dict[str, Any] = field(default_factory=dict)
    # FASE 8.3 — utilidad de la respuesta. Solo conteos; ver answer_sufficiency.
    sufficiency: dict[str, Any] = field(default_factory=dict)
    # FASE 8.6 — turno analitico. Se decide UNA vez, deterministicamente, sobre el
    # mensaje del usuario: no puede cambiar a mitad del turno ni depende del modelo.
    analytical: bool = False
    # FASE 10.1 — que capacidades vio el modelo y por que. Nombres y
    # razones, nunca contenido.
    capability_obs: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentLoopResult:
    reply: str
    classification: str
    raw_evidence: list[dict[str, Any]]
    fallback_used: bool
    fallback_reason: str | None
    needs_clarification: bool
    reject: bool
    scenario: str | None
    grounded: bool
    state: TurnAgentState


class QueueDecisionClient:
    """Test helper: returns queued decisions. Not used in production."""

    def __init__(self, decisions: list[Any]):
        self._q = list(decisions)
        self.last_usage: dict[str, int] | None = None
        self.calls = 0

    def complete_decision(self, *, system: str, user: str) -> dict[str, Any]:
        self.calls += 1
        if "ANDES_AGENT_SERVICE_TOKEN" in system or "ANDES_AGENT_SERVICE_TOKEN" in user:
            raise LlmError("invalid_args", "Prompt must not contain secrets")
        if not self._q:
            raise LlmError("llm_invalid_json", "No queued agent decision")
        item = self._q.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, str):
            return json.loads(item)
        return dict(item)


class LlmAgentDecisionClient:
    """Asks the LLM for a single-act AgentDecision. Reuses OpenAICompatibleClient."""

    def __init__(self, llm_client: Any):
        self.client = llm_client
        self.last_usage: dict[str, int] | None = None

    def complete_decision(self, *, system: str, user: str,
                          analytical: bool = False) -> dict[str, Any]:
        from app.assistant.orchestrator.llm.plan_schema import agent_decision_response_format

        raw = self.client.complete_plan_json(
            system=system,
            user=user,
            response_format=agent_decision_response_format(analytical=analytical),
        )
        self.last_usage = getattr(self.client, "last_usage", None)
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(parsed, dict):
            raise LlmError("llm_invalid_json", "Agent decision must be a JSON object")
        return parsed


AgentDecisionClient = LlmAgentDecisionClient


def empty_agent_plan() -> dict[str, Any]:
    """Stub plan for Composer fallback. Not a multi-step LlmPlanner plan."""
    return {
        "plan_id": "agent-loop",
        "user_intent": "agent",
        "answer_style": "operational",
        "scenario": "agent_loop",
        "steps": [],
        "needs_clarification": False,
        "reject": False,
        "reuse_prior_evidence": False,
    }


def _safe_trace_entry(
    *,
    decision_index: int,
    action: str | None,
    tool: str | None,
    evidence_count_before: int,
    evidence_count_after: int,
    correlation_id: str,
    latency_ms: int,
    fallback: bool,
    goal_covered_count: int = 0,
    goal_uncovered_count: int = 0,
    blocked_final: bool = False,
    admission: str | None = None,
    progress: str | None = None,
    blocked_final_released: bool = False,
) -> dict[str, Any]:
    return {
        "decision_index": int(decision_index),
        "action": str(action or "")[:40] or None,
        "tool": (str(tool)[:64] if tool else None),
        "evidence_count_before": int(evidence_count_before),
        "evidence_count_after": int(evidence_count_after),
        "correlation_id": str(correlation_id or "")[:80],
        "latency_ms": int(latency_ms),
        "fallback": bool(fallback),
        "goal_covered_count": int(goal_covered_count),
        "goal_uncovered_count": int(goal_uncovered_count),
        "blocked_final": bool(blocked_final),
        "admission": (str(admission)[:40] if admission else None),
        "progress": (str(progress)[:20] if progress else None),
        "blocked_final_released": bool(blocked_final_released),
    }


def _one_step_plan(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return validate_plan(
        {
            "plan_id": "agent-step",
            "user_intent": "agent_call_tool",
            "answer_style": "operational",
            "steps": [
                {
                    "step": 1,
                    "tool": tool,
                    "arguments": arguments,
                    "reason": "agent_loop",
                    "depends_on": [],
                }
            ],
        }
    )


def _composer_fallback(
    *,
    plan: dict[str, Any],
    raw_evidence: list[dict[str, Any]],
    state: TurnAgentState,
    reason: str,
    reject_message: str | None = None,
) -> AgentLoopResult:
    state.fallback_used = True
    state.fallback_reason = reason
    composed = compose_answer(plan=plan, evidence=raw_evidence, reject_message=reject_message)
    state.final_answer = str(composed.get("reply") or "")
    state.trace.append(
        _safe_trace_entry(
            decision_index=state.step_index,
            action="fallback",
            tool=None,
            evidence_count_before=len(state.evidence.items),
            evidence_count_after=len(state.evidence.items),
            correlation_id=state.correlation_id,
            latency_ms=0,
            fallback=True,
        )
    )
    return AgentLoopResult(
        reply=state.final_answer,
        classification=str(composed.get("classification") or "INTERNAL"),
        raw_evidence=raw_evidence,
        fallback_used=True,
        fallback_reason=reason,
        needs_clarification=bool(plan.get("needs_clarification")),
        reject=bool(plan.get("reject")),
        scenario=plan.get("scenario"),
        grounded=True,
        state=state,
    )


def _ingest_plan_evidence(
    store: EvidenceStore,
    plan: dict[str, Any],
    evidence: list[dict[str, Any]],
    correlation_id: str,
) -> list[EvidenceItem]:
    steps = list(plan.get("steps") or [])
    by_step = {int(s.get("step")): s for s in steps if isinstance(s, dict) and s.get("step") is not None}
    created: list[EvidenceItem] = []
    for item in evidence:
        step_no = item.get("step")
        args: dict[str, Any] = {}
        if isinstance(step_no, int) and step_no in by_step:
            raw_args = by_step[step_no].get("arguments")
            if isinstance(raw_args, dict):
                args = dict(raw_args)
        created.append(
            store.add_from_tool_result(
                tool=str(item.get("tool") or ""),
                arguments=args,
                result=item,
                correlation_id=correlation_id,
            )
        )
    return created


def _refresh_cost(state: TurnAgentState) -> None:
    state.cost_est = estimate_cost_usd(state.token_in, state.token_out)


def _complete_decision(client: Any, *, system: str, user: str,
                       analytical: bool) -> Any:
    """Pasa `analytical` solo a clientes que lo acepten.

    QueueDecisionClient y los dobles de test tienen firma propia; obligarlos a
    aceptar un kwarg nuevo romperia toda la bateria determinista sin ganar nada.
    """
    try:
        return client.complete_decision(system=system, user=user, analytical=analytical)
    except TypeError:
        return client.complete_decision(system=system, user=user)


def _add_usage(state: TurnAgentState, usage: dict[str, int] | None) -> None:
    if not usage:
        return
    try:
        state.token_in += int(usage.get("prompt_tokens") or 0)
        state.token_out += int(usage.get("completion_tokens") or 0)
        state.token_cached += int(usage.get("cached_tokens") or 0)
    except (TypeError, ValueError):
        return
    _refresh_cost(state)


def continue_agent_loop(
    *,
    message: str,
    actor_user: str,
    conversation_id: str,
    correlation_id: str,
    initial_plan: dict[str, Any] | None,
    initial_evidence: list[dict[str, Any]] | None,
    invoke_fn: InvokeFn,
    decision_client: Any,
    memory_hints: list[Any] | None = None,
    context: dict[str, Any] | None = None,
    usage_totals: dict[str, int] | None = None,
    started_at: float | None = None,
    max_seconds: float | None = None,
    max_tool_calls: int | None = None,
) -> AgentLoopResult:
    """Reactive AgentLoop: one AgentDecision per turn, then at most one ToolRunner invoke.

    Production (AGENT=1) starts with empty evidence. initial_evidence is only for
    unit tests (loop/timeout). Never invokes Gateway itself.
    """
    now0 = time.perf_counter() if started_at is None else float(started_at)
    budget = MAX_TOOL_CALLS if max_tool_calls is None else int(max_tool_calls)
    plan = initial_plan if isinstance(initial_plan, dict) and initial_plan else empty_agent_plan()
    seed_evidence = list(initial_evidence or [])
    # FASE 10.1 — que herramientas ve el modelo en este turno. Se decide UNA vez
    # (el mensaje no cambia entre decisiones) y se aplica a las 5, que es donde
    # esta el ahorro: el system se reenvia entero cada vez. Apagado por bandera,
    # el comportamiento es byte a byte el de 9.x.
    capacidades = None
    router_obs: dict[str, Any] = {}
    if capability_router_enabled():
        from app.assistant.orchestrator.agent_config import model_facing_tools
        from app.assistant.orchestrator.capability_router import select_capabilities
        from app.assistant.orchestrator.catalog import ALLOWED_TOOLS as _TODAS

        _dec = select_capabilities(message, available=model_facing_tools(_TODAS))
        capacidades = _dec.selected
        router_obs = _dec.observation()

    state = TurnAgentState(
        correlation_id=correlation_id,
        actor_user=actor_user,
        conversation_id=conversation_id,
        started_at=now0,
        deadline=now0 + float(MAX_SECONDS if max_seconds is None else max_seconds),
        evidence=EvidenceStore(correlation_id=correlation_id),
        invoke_count=len([e for e in seed_evidence if e.get("tool")]),
        token_in=int((usage_totals or {}).get("prompt_tokens") or 0),
        token_out=int((usage_totals or {}).get("completion_tokens") or 0),
        token_cached=int((usage_totals or {}).get("cached_tokens") or 0),
        goal=GoalCoverage.from_message(message),
        # Determinista y fijado una sola vez: ni el modelo ni la evidencia pueden
        # convertir un turno normal en analitico a mitad de camino.
        analytical=analytical_intent(message),
    )
    state.capability_obs = router_obs
    _refresh_cost(state)
    state.ledger = ProgressLedger(call_keys=state.loop_keys)
    raw_evidence = list(seed_evidence)
    if seed_evidence:
        _ingest_plan_evidence(state.evidence, plan, seed_evidence, correlation_id)
        state.ledger.seed(state.evidence)
    state.goal.refresh(state.evidence)

    memory_note = None
    if memory_hints:
        try:
            memory_note = json.dumps(memory_hints, ensure_ascii=False, separators=(",", ":"))[:800]
        except (TypeError, ValueError):
            memory_note = None
    context_note = build_agent_context_note(context)

    def _time_up() -> bool:
        if time.perf_counter() >= state.deadline:
            state.timeout = True
            return True
        return False

    while state.step_index < MAX_AGENT_STEPS:
        if _time_up():
            return _composer_fallback(
                plan=plan, raw_evidence=raw_evidence, state=state, reason="agent_timeout"
            )
        if budget_exceeded(
            prompt_tokens=state.token_in,
            completion_tokens=state.token_out,
            cost_est=state.cost_est,
        ):
            # Observability only: this is the token/cost ceiling, which is a very
            # different failure from running out of tool invokes. Budgets unchanged.
            return _composer_fallback(
                plan=plan,
                raw_evidence=raw_evidence,
                state=state,
                reason="agent_token_budget",
            )

        remaining = max(0, budget - state.invoke_count)
        state.goal.refresh(state.evidence)
        system = build_agent_system_prompt(
            analytical=state.analytical, capabilities=capacidades)
        decision: dict[str, Any] | None = None
        last_err: str | None = None
        ev_before = len(state.evidence.items)
        t_dec = time.perf_counter()
        retry_note = None
        for attempt in range(1 + MAX_DECISION_RETRIES):
            notes = []
            if state.loop_detected:
                notes.append("loop_detected=true; no repitas la misma tool+args.")
            if state.coverage_hint:
                notes.append(state.coverage_hint)
            if retry_note:
                notes.append(retry_note)
            user = build_agent_user_prompt(
                message,
                evidence_pack=state.evidence.prompt_pack(),
                memory_note=memory_note,
                context_note=context_note,
                loop_note=(" ".join(notes) if notes else None),
                remaining_calls=remaining,
                goal_note=state.goal.prompt_pack(),
            )
            try:
                raw_decision = _complete_decision(
                    decision_client, system=system, user=user,
                    analytical=state.analytical)
                _add_usage(state, getattr(decision_client, "last_usage", None))
                if usage_totals is not None:
                    usage_totals["prompt_tokens"] = state.token_in
                    usage_totals["completion_tokens"] = state.token_out
                    usage_totals["total_tokens"] = state.token_in + state.token_out
                    usage_totals["cached_tokens"] = state.token_cached
                decision = validate_agent_decision(
                    raw_decision, user_message=message, context=context)
                break
            except (AgentDecisionError, LlmError, json.JSONDecodeError, TypeError, ValueError) as exc:
                last_err = getattr(exc, "code", None) or type(exc).__name__
                state.retries += 1
                details = getattr(exc, "details", None)
                if isinstance(details, dict) and details.get("error") == "invalid_args":
                    retry_note = json.dumps(details, ensure_ascii=False, separators=(",", ":"))
                    state.arg_errors.append(
                        {
                            "error": "invalid_args",
                            "tool": str(details.get("tool") or "")[:64],
                            "fields": details.get("fields") or {},
                        }
                    )
                else:
                    retry_note = (
                        "La decisión anterior fue inválida. Devuelve UN acto "
                        "(call_tool con una sola tool, o final_answer). "
                        "Sin steps, sin tools[], sin plan. calculations=[] o items con id+op+inputs+result."
                    )
                if attempt >= MAX_DECISION_RETRIES:
                    return _composer_fallback(
                        plan=plan,
                        raw_evidence=raw_evidence,
                        state=state,
                        reason=str(last_err or "agent_invalid_decision"),
                    )
        assert decision is not None
        state.goal.merge_proposed(decision.get("proposed_requirements"))
        state.goal.refresh(state.evidence)
        dec_ms = int((time.perf_counter() - t_dec) * 1000)
        state.llm_latency_ms += dec_ms
        state.step_index += 1
        state.decisions.append({"action": decision["action"], "tool": decision.get("tool")})
        state.trace.append(
            _safe_trace_entry(
                decision_index=state.step_index,
                action=str(decision["action"]),
                tool=decision.get("tool") or None,
                evidence_count_before=ev_before,
                evidence_count_after=ev_before,
                correlation_id=correlation_id,
                latency_ms=dec_ms,
                fallback=False,
                goal_covered_count=len(state.goal.covered()),
                goal_uncovered_count=len(state.goal.uncovered()),
            )
        )

        if decision["action"] in {"clarify", "reject"}:
            msg = decision.get("reject_message") or decision.get("draft_reply") or (
                "Necesito más detalles para continuar."
                if decision["action"] == "clarify"
                else "Solicitud rechazada."
            )
            plan = {
                "answer_style": "operational",
                "needs_clarification": decision["action"] == "clarify",
                "reject": decision["action"] == "reject",
                "reject_message": msg,
                "scenario": "agent_" + decision["action"],
            }
            composed = compose_answer(plan=plan, evidence=[], reject_message=msg)
            state.final_answer = str(composed.get("reply") or msg)
            return AgentLoopResult(
                reply=state.final_answer,
                classification="INTERNAL",
                raw_evidence=raw_evidence,
                fallback_used=False,
                fallback_reason=None,
                needs_clarification=decision["action"] == "clarify",
                reject=decision["action"] == "reject",
                scenario=plan["scenario"],
                grounded=True,
                state=state,
            )

        if decision["action"] == "final_answer":
            state.goal.apply_unresolved(
                decision.get("unresolved"),
                store=state.evidence,
                remaining_calls=remaining,
            )
            state.goal.refresh(state.evidence)
            allow_partial = allow_partial_final(
                state.goal, state.evidence, remaining=remaining
            )
            if state.goal.blocks_final() and not allow_partial:
                # FASE 8.1G.3 — repeating final_answer while nothing moves is
                # repetition without progress, and each block costs a full model
                # round-trip. Once the model has demonstrated it will not call the
                # covering tool, release the answer as a partial (with the
                # "No pude resolver" note) instead of burning the turn into a
                # fallback. Budgets and loop protection are untouched.
                exhausted = state.ledger.record_blocked_final(
                    state.goal.coverage_signature()
                )
                state.blocked_finals += 1
                if state.trace:
                    state.trace[-1]["blocked_final"] = True
                    state.trace[-1]["goal_uncovered_count"] = len(state.goal.uncovered())
                    state.trace[-1]["goal_covered_count"] = len(state.goal.covered())
                if not exhausted:
                    state.coverage_hint = state.goal.blocked_note(state.evidence)
                    continue
                state.blocked_final_released = True
                state.coverage_hint = None
                if state.trace:
                    state.trace[-1]["blocked_final_released"] = True
            verified = verify_agent_answer(
                store=state.evidence,
                decision=decision,
                raw_evidence=raw_evidence,
                plan=plan,
            )
            state.verifier_failures += int(verified.failures)
            state.verifier_breakdown = verified.breakdown()
            if verified.used_composer_fallback:
                state.fallback_used = True
                state.fallback_reason = "agent_verifier_failed"
            reply = verified.reply
            if state.goal.uncovered() or state.goal.impossible():
                note = state.goal.unresolved_user_note()
                if note and note not in reply:
                    reply = (reply.rstrip() + "\n\n" + note) if reply.strip() else note
            state.final_answer = reply
            # FASE 8.3 — suficiencia en modo OBSERVACION: se mide sobre la
            # respuesta ya verificada y no la altera. Va despues del verifier a
            # proposito: lo que interesa medir es lo que el usuario recibe, no lo
            # que el modelo propuso, y el verifier puede haber quitado claims.
            try:
                state.sufficiency = analyze_sufficiency(
                    store=state.evidence, goal=state.goal,
                    reply=reply, question=message,
                ).safe_snapshot()
            except Exception:  # noqa: BLE001 — una metrica no puede tumbar el turno
                state.sufficiency = {}
            # FASE 9.6 (D2) — la senal estructurada tiene que decir lo que el
            # turno HIZO.
            #
            # Medido en N08 ("Cuanto nos queda?"): el asistente pidio aclaracion
            # correctamente y no llamo ninguna tool, pero el turno reportaba
            # needs_clarification=False. La accion `clarify` existe y pone la
            # bandera bien; el modelo simplemente no la eligio — el prompt la
            # enumera y nunca dice cuando usarla. Reescribir el prompt es la
            # clase de cambio que ya costo T07 (9/10 -> 0/10) en 8.1I.2, asi que
            # la senal se deriva de la estructura en vez de pedirsela al modelo.
            #
            # Una respuesta final SIN evidencia y SIN claims no afirma nada y no
            # se apoya en nada: por construccion no es una respuesta, es una
            # peticion de mas informacion. Los saludos de N01/N02 NO caen aqui
            # porque si emiten claims. No se mira el texto: "Hola, en que puedo
            # ayudarte?" tambien termina en interrogacion, y una heuristica
            # sobre el signo los marcaria a los tres.
            sin_evidencia = not raw_evidence
            sin_claims = not (decision.get("claims") or [])
            return AgentLoopResult(
                reply=reply,
                classification=verified.classification,
                raw_evidence=raw_evidence,
                fallback_used=state.fallback_used,
                fallback_reason=state.fallback_reason,
                needs_clarification=bool(sin_evidencia and sin_claims),
                reject=False,
                scenario=plan.get("scenario"),
                grounded=True,
                state=state,
            )

        # call_tool
        if remaining <= 0:
            return _composer_fallback(
                plan=plan, raw_evidence=raw_evidence, state=state, reason="agent_limit"
            )

        # FASE 8.1G — structural admission. Rejects only demonstrated repetition
        # without progress; a rejection costs a decision, never the whole turn.
        admission = state.ledger.admit(
            decision["tool"], decision["arguments"], state.goal
        )
        if state.trace:
            state.trace[-1]["admission"] = admission.reason
        if not admission.admitted:
            state.ledger.record_rejection(admission)
            state.rejected_decisions = state.ledger.consecutive_rejections
            if admission.reason == REJECT_REPEAT_CALL:
                state.loop_detected = True
            else:
                state.no_progress_detected = True
            state.coverage_hint = admission_note(admission, state.goal)
            if state.ledger.rejections_exhausted():
                return _composer_fallback(
                    plan=plan,
                    raw_evidence=raw_evidence,
                    state=state,
                    reason="agent_no_progress",
                )
            continue
        state.ledger.record_admission()
        state.rejected_decisions = 0
        # El hint describe una situacion que esta llamada va a cambiar: dejarlo
        # pegado contamina las decisiones siguientes con guia ya obsoleta.
        state.coverage_hint = None

        try:
            plan_one = _one_step_plan(decision["tool"], decision["arguments"])
        except (PlanValidationError, AgentDecisionError) as exc:
            code = getattr(exc, "code", "invalid_plan")
            return _composer_fallback(
                plan=plan, raw_evidence=raw_evidence, state=state, reason=str(code)
            )

        try:
            new_ev, _payloads = run_plan_steps(
                plan_one,
                actor_user=actor_user,
                conversation_id=conversation_id,
                invoke_fn=invoke_fn,
                max_invokes=min(1, MAX_INVOKES),
            )
        except ToolRunnerError as exc:
            state.consecutive_errors += 1
            # Sin esto la llamada no queda registrada y MAX_SAME_CALL no la ve: el
            # modelo puede repetir la misma llamada fallida hasta agotar el tope de
            # errores. Contarla mantiene una sola regla de repeticion.
            state.ledger.call_keys[admission.call_key] = (
                state.ledger.call_count(admission.call_key) + 1
            )
            if state.consecutive_errors >= MAX_CONSECUTIVE_TOOL_ERRORS:
                return _composer_fallback(
                    plan=plan, raw_evidence=raw_evidence, state=state, reason=exc.code
                )
            continue

        state.invoke_count += len([e for e in new_ev if e.get("tool")])
        raw_evidence.extend(new_ev)
        resolved_before = len(state.goal.covered()) + len(state.goal.impossible())
        new_items = _ingest_plan_evidence(state.evidence, plan_one, new_ev, correlation_id)
        state.goal.refresh(state.evidence)
        resolved_after = len(state.goal.covered()) + len(state.goal.impossible())
        progress = state.ledger.record_execution(
            decision["tool"],
            new_items,
            resolved_before=resolved_before,
            resolved_after=resolved_after,
        )
        state.ledger.note_coverage(state.goal.coverage_signature())
        if state.trace:
            state.trace[-1]["evidence_count_after"] = len(state.evidence.items)
            state.trace[-1]["progress"] = progress
            state.trace[-1]["goal_covered_count"] = len(state.goal.covered())
            state.trace[-1]["goal_uncovered_count"] = len(state.goal.uncovered())
        failed = any(not e.get("ok") for e in new_ev)
        empty = any(e.get("empty") for e in new_ev)
        if failed:
            state.consecutive_errors += 1
            if state.consecutive_errors >= MAX_CONSECUTIVE_TOOL_ERRORS:
                return _composer_fallback(
                    plan=plan, raw_evidence=raw_evidence, state=state, reason="tool_error"
                )
        else:
            state.consecutive_errors = 0
        if progress == PROGRESS_NONE and state.ledger.no_progress_exhausted():
            state.no_progress_detected = True
            return _composer_fallback(
                plan=plan,
                raw_evidence=raw_evidence,
                state=state,
                reason="agent_no_progress",
            )

    return _composer_fallback(
        plan=plan, raw_evidence=raw_evidence, state=state, reason="agent_limit"
    )
