"""FASE 8.1G — structural progress ledger for the AgentLoop.

Turn-scoped, RAM only. Not memory, not history, not a planner, not permissions.

The ledger answers exactly one question: *can this proposed READ call still move
the turn forward?* It never authorizes a tool — the allowlist stays in
``agent_schema`` (decision time), ``ToolRunner`` (execution time) and the Gateway
(ACL). A tool the ledger admits can still be rejected by any of those.

Progress is structural, never textual:

- ``coverage``  a requirement moved out of ``uncovered`` (covered or impossible);
- ``evidence``  a new, non-equivalent, useful evidence item was produced;
- ``none``      the call produced neither.

Non-progress is therefore *demonstrated*, not guessed: the same canonical call,
the same tool returning equivalent evidence, or alternation between tools that
never changes coverage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.assistant.orchestrator.agent_config import (
    MAX_BLOCKED_FINALS_NO_PROGRESS,
    MAX_EMPTY_FOLLOWUPS,
    MAX_NO_PROGRESS_STEPS,
    MAX_REJECTED_DECISIONS,
    MAX_SAME_CALL,
)
from app.assistant.orchestrator.evidence_store import EvidenceItem, canonical_call_key
from app.assistant.orchestrator.goal_coverage import GoalCoverage, covering_tools

if TYPE_CHECKING:  # pragma: no cover — typing only
    from app.assistant.orchestrator.evidence_store import EvidenceStore

PROGRESS_COVERAGE = "coverage"
PROGRESS_EVIDENCE = "evidence"
PROGRESS_NONE = "none"

# Admission outcomes. Kept as closed constants so traces stay enumerable.
ADMIT_NEW_TOOL = "admit_new_tool"
ADMIT_COVERS_UNCOVERED = "admit_covers_uncovered"
ADMIT_PRODUCTIVE_TOOL = "admit_productive_tool"
ADMIT_REFINE_AFTER_EMPTY = "admit_refine_after_empty"
REJECT_REPEAT_CALL = "repeat_call"
REJECT_TOOL_NO_PROGRESS = "tool_repeat_no_progress"
REJECT_EMPTY_NO_PROGRESS = "empty_repeat_no_progress"

ADMIT_REASONS = frozenset({ADMIT_NEW_TOOL, ADMIT_COVERS_UNCOVERED, ADMIT_PRODUCTIVE_TOOL,
                           ADMIT_REFINE_AFTER_EMPTY})
REJECT_REASONS = frozenset(
    {REJECT_REPEAT_CALL, REJECT_TOOL_NO_PROGRESS, REJECT_EMPTY_NO_PROGRESS}
)


def covers_uncovered_requirement(goal: GoalCoverage, tool: str) -> bool:
    """True when ``tool`` is a covering tool for some still-uncovered requirement.

    Reads GoalCoverage only; proposing or covering a requirement never grants a
    tool, and an impossible requirement is not uncovered.
    """
    name = str(tool or "")
    if not name:
        return False
    for req in goal.uncovered():
        if name in covering_tools(req.type):
            return True
    return False


@dataclass
class Admission:
    admitted: bool
    reason: str
    call_key: str
    tool: str = ""

    def as_safe_dict(self) -> dict[str, Any]:
        return {
            "admitted": bool(self.admitted),
            "reason": str(self.reason)[:40],
            "tool": str(self.tool)[:64] or None,
        }


@dataclass
class ToolRecord:
    """Per-tool history for THIS turn. No arguments, no values, no PII."""

    executions: int = 0
    progress_events: int = 0
    empty_results: int = 0
    failed_results: int = 0
    # Outcome of the MOST RECENT execution. A tool that was productive once but
    # now returns equivalent evidence is repeating, not progressing.
    last_progress: str = PROGRESS_NONE
    # FASE 8.2 — "cero resultados" no es lo mismo que "esta tool no aporta nada".
    # Un vacio es una respuesta legitima que admite exactamente un refinamiento.
    last_empty: bool = False

    @property
    def productive(self) -> bool:
        return self.last_progress != PROGRESS_NONE

    def as_safe_dict(self) -> dict[str, Any]:
        return {
            "executions": int(self.executions),
            "progress_events": int(self.progress_events),
            "empty_results": int(self.empty_results),
            "failed_results": int(self.failed_results),
            "last_progress": str(self.last_progress),
            "last_empty": bool(self.last_empty),
        }


@dataclass
class ProgressLedger:
    """Turn-scoped anti-thrashing accounting.

    ``call_keys`` is shared with ``TurnAgentState.loop_keys`` (same object), so
    the historical MAX_SAME_CALL guard and the ledger can never disagree.
    """

    call_keys: dict[str, int] = field(default_factory=dict)
    content_keys: set[str] = field(default_factory=set)
    tools: dict[str, ToolRecord] = field(default_factory=dict)
    consecutive_no_progress: int = 0
    consecutive_rejections: int = 0
    progress_events: int = 0
    no_progress_steps: int = 0
    rejections: int = 0
    last_progress: str = PROGRESS_NONE
    # FASE 8.1G.3 — a blocked final_answer is repetition too. The ledger used to
    # observe call_tool only, so a model that answered "final" over and over while
    # a requirement stayed uncovered burned the turn budget invisibly.
    consecutive_blocked_finals: int = 0
    blocked_finals: int = 0
    last_coverage_signature: str | None = None

    # ---------------------------------------------------------------- seeding

    def seed(self, store: EvidenceStore) -> None:
        """Register evidence that already exists (unit-test seeded turns).

        Seeded evidence counts as observed, never as progress: a turn that starts
        with evidence has not yet made progress *this* loop.
        """
        for item in store.items:
            self._register_item(item)

    def _register_item(self, item: EvidenceItem) -> None:
        fresh = bool(item.content_key) and item.content_key not in self.content_keys
        if item.call_key:
            self.call_keys[item.call_key] = self.call_keys.get(item.call_key, 0) + 1
        if item.content_key:
            self.content_keys.add(item.content_key)
        rec = self.tools.setdefault(str(item.tool or ""), ToolRecord())
        rec.executions += 1
        if item.empty:
            rec.empty_results += 1
        if not item.ok:
            rec.failed_results += 1
        # Seeded evidence has no measured step: an ok, non-empty, new payload is
        # treated as having contributed evidence, anything else as not.
        rec.last_progress = PROGRESS_EVIDENCE if (item.ok and not item.empty and fresh) else PROGRESS_NONE
        rec.last_empty = bool(item.ok and item.empty)

    # ------------------------------------------------------------- admission

    def call_count(self, call_key: str) -> int:
        return int(self.call_keys.get(call_key) or 0)

    def record(self, tool: str) -> ToolRecord:
        return self.tools.get(str(tool or "")) or ToolRecord()

    def admit(
        self,
        tool: str,
        arguments: dict[str, Any] | None,
        goal: GoalCoverage,
    ) -> Admission:
        """Decide whether a proposed call may reach ToolRunner.

        Rejects only *demonstrated repetition without progress*. A tool that is
        allowlisted, covers an uncovered requirement and has not run yet is always
        admitted — a different tool is never rejected merely for being a follow-up
        to an empty result. Conversely "different tool" alone never grants
        admission: a tool that already ran and produced nothing is rejected unless
        it can still cover something uncovered.
        """
        name = str(tool or "")
        key = canonical_call_key(name, arguments or {})
        if self.call_count(key) >= MAX_SAME_CALL:
            return Admission(False, REJECT_REPEAT_CALL, key, name)

        rec = self.tools.get(name)
        if rec is None or rec.executions <= 0:
            return Admission(True, ADMIT_NEW_TOOL, key, name)

        if covers_uncovered_requirement(goal, name):
            # The requirement this tool answers is still open: a retry with
            # different arguments is a legitimate attempt, bounded by
            # MAX_NO_PROGRESS_STEPS and the turn tool budget.
            return Admission(True, ADMIT_COVERS_UNCOVERED, key, name)

        if rec.empty_results >= MAX_EMPTY_FOLLOWUPS:
            return Admission(False, REJECT_EMPTY_NO_PROGRESS, key, name)
        if rec.last_empty:
            # Cero resultados con ok=True: la tool respondio, simplemente no habia
            # nada para ESOS argumentos. Refinar la consulta es la accion correcta
            # y el contador de vacios la acota a una sola vez.
            return Admission(True, ADMIT_REFINE_AFTER_EMPTY, key, name)
        if not rec.productive:
            return Admission(False, REJECT_TOOL_NO_PROGRESS, key, name)
        return Admission(True, ADMIT_PRODUCTIVE_TOOL, key, name)

    def record_admission(self) -> None:
        self.consecutive_rejections = 0

    def record_rejection(self, admission: Admission) -> None:
        self.rejections += 1
        self.consecutive_rejections += 1

    def rejections_exhausted(self) -> bool:
        return self.consecutive_rejections >= MAX_REJECTED_DECISIONS

    # -------------------------------------------------------- blocked finals

    def record_blocked_final(self, coverage_signature: str) -> bool:
        """Register one blocked final_answer; True when the threshold is reached.

        The counter tracks *consecutive blocks without coverage change*: if any
        requirement moved since the previous block, this is a new situation and the
        count restarts. Kept separate from ``consecutive_no_progress`` on purpose —
        that one bounds executed tool calls at 2, which would cut off the T07 runs
        that succeed on their third decision.
        """
        sig = str(coverage_signature)
        if self.last_coverage_signature is not None and sig != self.last_coverage_signature:
            self.consecutive_blocked_finals = 0
        self.last_coverage_signature = sig
        self.consecutive_blocked_finals += 1
        self.blocked_finals += 1
        return self.blocked_finals_exhausted()

    def note_coverage(self, coverage_signature: str) -> None:
        """Coverage moved through some other path (a tool ran): reset the block
        streak so a later block is judged on the new situation."""
        sig = str(coverage_signature)
        if self.last_coverage_signature is not None and sig != self.last_coverage_signature:
            self.consecutive_blocked_finals = 0
        self.last_coverage_signature = sig

    def blocked_finals_exhausted(self) -> bool:
        return self.consecutive_blocked_finals >= MAX_BLOCKED_FINALS_NO_PROGRESS

    # -------------------------------------------------------------- progress

    def classify(
        self,
        new_items: list[EvidenceItem],
        *,
        resolved_before: int,
        resolved_after: int,
    ) -> str:
        """Structural progress of one executed step. Never looks at reply text."""
        if int(resolved_after) > int(resolved_before):
            return PROGRESS_COVERAGE
        for item in new_items:
            if not item.ok or item.empty:
                continue
            if item.content_key and item.content_key not in self.content_keys:
                return PROGRESS_EVIDENCE
        return PROGRESS_NONE

    def record_execution(
        self,
        tool: str,
        new_items: list[EvidenceItem],
        *,
        resolved_before: int,
        resolved_after: int,
    ) -> str:
        """Register one executed call and return its progress kind."""
        progress = self.classify(
            new_items, resolved_before=resolved_before, resolved_after=resolved_after
        )
        name = str(tool or "")
        rec = self.tools.setdefault(name, ToolRecord())
        before_exec = rec.executions
        for item in new_items:
            self._register_item(item)
        if not new_items:
            # Nothing came back at all: still a step that produced no progress.
            rec.executions = before_exec + 1
        rec.last_progress = progress
        if progress != PROGRESS_NONE:
            rec.progress_events += 1
            self.progress_events += 1
            self.consecutive_no_progress = 0
        else:
            self.no_progress_steps += 1
            self.consecutive_no_progress += 1
        self.last_progress = progress
        return progress

    def no_progress_exhausted(self) -> bool:
        return self.consecutive_no_progress >= MAX_NO_PROGRESS_STEPS

    # ----------------------------------------------------------- observation

    def safe_snapshot(self) -> dict[str, Any]:
        """Enumerable counters only. No arguments, no values, no prompts."""
        return {
            "progress_events": int(self.progress_events),
            "no_progress_steps": int(self.no_progress_steps),
            "consecutive_no_progress": int(self.consecutive_no_progress),
            "rejections": int(self.rejections),
            "consecutive_rejections": int(self.consecutive_rejections),
            "blocked_finals": int(self.blocked_finals),
            "consecutive_blocked_finals": int(self.consecutive_blocked_finals),
            "last_progress": str(self.last_progress),
            "distinct_calls": len(self.call_keys),
            "distinct_evidence": len(self.content_keys),
            "tools": {k: v.as_safe_dict() for k, v in sorted(self.tools.items()) if k},
        }


def admission_note(admission: Admission, goal: GoalCoverage) -> str:
    """Hint fed back to the model. Names the blocked tool and what is still open."""
    missing = ",".join(r.type for r in goal.uncovered()) or "none"
    if admission.reason == REJECT_REPEAT_CALL:
        why = "esa llamada ya se ejecutó con los mismos argumentos"
    elif admission.reason == REJECT_EMPTY_NO_PROGRESS:
        why = "esa tool ya devolvió vacío y no cubre ningún requirement pendiente"
    else:
        why = "esa tool ya se ejecutó sin aportar evidencia nueva"
    return (
        f"call_tool bloqueado ({admission.tool or 'tool'}): {why}. "
        f"uncovered={missing}. Llama OTRA tool allowlisted que cubra el faltante, "
        "o declara unresolved con reason cerrado, o responde con lo que ya tienes."
    )
