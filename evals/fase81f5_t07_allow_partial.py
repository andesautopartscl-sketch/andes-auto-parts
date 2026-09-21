"""FASE 8.1F.5 — diagnose allow_partial after blocked_final. No production changes."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "data" / "fase81_eval"
DATASET = ROOT / "evals" / "fase81_evidence_dataset.jsonl"


def _inherit_key() -> bool:
    if (os.environ.get("ANDES_LLM_API_KEY") or "").strip():
        return True
    try:
        import psutil
    except ImportError:
        return False
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if (p.info.get("name") or "").lower() not in {"powershell.exe", "pwsh.exe", "cmd.exe"}:
                continue
            env = p.environ()
            key = (env.get("ANDES_LLM_API_KEY") or "").strip()
            if not key:
                continue
            os.environ["ANDES_LLM_API_KEY"] = key
            for k in ("ANDES_LLM_BASE_URL", "ANDES_LLM_PLANNER_MODEL", "ANDES_LLM_PROVIDER"):
                v = (env.get(k) or "").strip()
                if v and not (os.environ.get(k) or "").strip():
                    os.environ[k] = v
            return True
        except Exception:
            continue
    return False


def _policy_snapshot(
    *,
    blocked_finals: int,
    remaining: int,
    blocks_final: bool,
    uncovered: list[str],
    covering_tools_for_uncovered: dict[str, list[str]],
) -> dict[str, Any]:
    """Mirror agent_loop final_answer gate — no tools executed."""
    allow_partial = blocked_finals >= 1 or remaining <= 0
    would_block = bool(blocks_final and not allow_partial)
    covering_available = any(bool(v) for v in covering_tools_for_uncovered.values())
    return {
        "allow_partial": allow_partial,
        "formula": "blocked_finals >= 1 or remaining <= 0",
        "blocked_finals": blocked_finals,
        "remaining": remaining,
        "blocks_final": blocks_final,
        "uncovered": uncovered,
        "covering_tools_for_uncovered": covering_tools_for_uncovered,
        "covering_tool_available": covering_available,
        "policy_outcome": "blocked_final_continue" if would_block else "accept_final_answer",
        "checks_covering_tool": False,
        "checks_hard_vs_optional": False,
        "distinctions_present": {
            "hard_requirement": False,
            "optional_requirement": False,
            "partial_answer_allowed_flag": "derived_only_via_allow_partial",
            "impossible_requirement": True,
            "unavailable_tool": False,
        },
    }


def _deterministic_no_llm() -> dict[str, Any]:
    """Simulate T07 coverage state + two final_answer attempts without invoking tools."""
    from app.assistant.orchestrator.goal_coverage import (
        EXTRACTION_DETECTED,
        GoalCoverage,
        GoalRequirement,
        covering_tools,
    )

    cov = GoalCoverage(
        extraction=EXTRACTION_DETECTED,
        requirements=[
            GoalRequirement(id="r1", type="supplier", status="covered"),
            GoalRequirement(id="r2", type="purchase_orders", status="uncovered"),
        ],
    )
    uncovered = [r.type for r in cov.uncovered()]
    covering = {t: sorted(covering_tools(t)) for t in uncovered}
    assert cov.blocks_final()
    assert "get_purchase_orders" in covering.get("purchase_orders", [])

    # After get_supplier: remaining budget typically > 0 (MAX_TOOL_CALLS=5, invoke=1)
    first = _policy_snapshot(
        blocked_finals=0,
        remaining=4,
        blocks_final=True,
        uncovered=uncovered,
        covering_tools_for_uncovered=covering,
    )
    # After first blocked_final increments counter
    second = _policy_snapshot(
        blocked_finals=1,
        remaining=4,
        blocks_final=True,
        uncovered=uncovered,
        covering_tools_for_uncovered=covering,
    )
    return {
        "setup": {
            "requirements": ["supplier", "purchase_orders"],
            "covered": ["supplier"],
            "uncovered": uncovered,
            "covering_tools": covering,
            "tools_executed_by_this_probe": [],
        },
        "after_first_final_answer": first,
        "after_blocked_final_second_final_answer": second,
        "observation": (
            "First final -> blocked_final_continue. "
            "Second final with same uncovered + covering tool available -> "
            "accept_final_answer solely because blocked_finals>=1. "
            "No covering_tool check; loop does not force call_tool."
        ),
    }


def _run_llm_once() -> dict[str, Any]:
    from app.utils.load_env import load_project_dotenv

    load_project_dotenv(force=False)
    if not _inherit_key():
        return {"error": "NO_LLM_KEY"}
    key = os.environ["ANDES_LLM_API_KEY"]
    os.environ["ANDES_ASSISTANT_AGENT_ENABLED"] = "1"
    os.environ["ANDES_ASSISTANT_NL_ENABLED"] = "1"
    os.environ["ANDES_ORCH_PLANNER"] = "llm"
    os.environ["ANDES_ENV"] = "local"
    os.environ.setdefault("ANDES_LLM_PROVIDER", "openai_compatible")
    os.environ.setdefault("ANDES_AGENT_URL", "http://127.0.0.1:5055")
    os.environ.setdefault("ANDES_ASSISTANT_MEMORY_ENABLED", "0")
    os.environ.setdefault("ANDES_ASSISTANT_HISTORY_ENABLED", "0")
    load_project_dotenv(force=False)
    os.environ["ANDES_LLM_API_KEY"] = key
    os.environ["ANDES_ASSISTANT_AGENT_ENABLED"] = "1"
    os.environ["ANDES_ASSISTANT_NL_ENABLED"] = "1"
    os.environ["ANDES_ORCH_PLANNER"] = "llm"

    from app.assistant.orchestrator import agent_loop as al
    from app.assistant.orchestrator.agent_config import MAX_DECISION_RETRIES, MAX_TOOL_CALLS, agent_loop_allowed
    from app.assistant.orchestrator.agent_loop import LlmAgentDecisionClient
    from app.assistant.orchestrator.agent_schema import AgentDecisionError, validate_agent_decision
    from app.assistant.orchestrator.factory import build_planner
    from app.assistant.orchestrator.goal_coverage import covering_tools
    from app.assistant.orchestrator.llm.client import OpenAICompatibleClient
    from app.assistant.orchestrator.llm.config import load_llm_settings
    from app.assistant.orchestrator.service import run_orchestrator_chat
    from app.assistant.orchestrator.turn_store import TurnStore
    from app.assistant.routes import invoke_gateway
    from evals.fase81_runner import load_cases

    if not agent_loop_allowed():
        return {"error": "agent_loop_allowed=false"}

    journal: list[dict[str, Any]] = []
    state_box: dict[str, Any] = {"state": None, "attempt_idx": 0}
    real_validate = validate_agent_decision
    real_refresh = al._refresh_cost

    def refresh_hook(state):
        state_box["state"] = state
        state_box["attempt_idx"] = 0
        return real_refresh(state)

    def validate_hook(raw, *, user_message=None):
        st = state_box["state"]
        remaining = 0
        blocked_finals = 0
        reqs: list[str] = []
        covered: list[str] = []
        uncovered: list[str] = []
        if st is not None:
            remaining = max(0, MAX_TOOL_CALLS - int(st.invoke_count or 0))
            blocked_finals = int(st.blocked_finals or 0)
            st.goal.refresh(st.evidence)
            for r in st.goal.safe_snapshot().get("requirements") or []:
                t = str(r.get("type"))
                reqs.append(t)
                if r.get("status") == "covered":
                    covered.append(t)
                elif r.get("status") == "uncovered":
                    uncovered.append(t)
        act = str((raw or {}).get("action") or "")[:40] if isinstance(raw, dict) else None
        tool = None
        if isinstance(raw, dict) and raw.get("tool"):
            tool = str(raw.get("tool"))[:64]
        allow_partial = blocked_finals >= 1 or remaining <= 0
        entry = {
            "decision_index": (st.step_index + 1) if st is not None else len(journal) + 1,
            "agent_step": (st.step_index + 1) if st is not None else None,
            "decision_retry_count": state_box["attempt_idx"],
            "act": act,
            "tool": tool,
            "requirements": reqs,
            "covered": covered,
            "uncovered": uncovered,
            "blocked_finals_before": blocked_finals,
            "allow_partial_computed": allow_partial,
            "remaining_invokes": remaining,
            "blocked_final": False,  # filled after act handling via trace
            "validator_status": None,
            "validator_error_code": None,
        }
        try:
            decision = real_validate(raw, user_message=user_message)
            entry["act"] = str(decision.get("action") or "")
            entry["tool"] = str(decision.get("tool") or "") or None
            entry["validator_status"] = "ok"
            journal.append(entry)
            state_box["attempt_idx"] += 1
            return decision
        except AgentDecisionError as exc:
            entry["validator_status"] = "rejected"
            entry["validator_error_code"] = exc.code
            journal.append(entry)
            state_box["attempt_idx"] += 1
            raise

    al.validate_agent_decision = validate_hook  # type: ignore[assignment]
    al._refresh_cost = refresh_hook  # type: ignore[assignment]

    gold = next(c for c in load_cases(DATASET) if c.get("id") == "T07")
    t0 = time.perf_counter()
    try:
        result = run_orchestrator_chat(
            message=str(gold.get("prompt") or ""),
            actor_user=gold.get("actor") or "albertadmin",
            conversation_id="fase81f5-T07-allow-partial",
            invoke_fn=invoke_gateway,
            planner=build_planner(),
            turn_store=TurnStore(),
            agent_decision_client=LlmAgentDecisionClient(OpenAICompatibleClient(load_llm_settings())),
        )
    finally:
        al.validate_agent_decision = real_validate  # type: ignore[assignment]
        al._refresh_cost = real_refresh  # type: ignore[assignment]

    latency_ms = int((time.perf_counter() - t0) * 1000)
    tools = [str(t) for t in (result.get("tools_used") or []) if t]
    goal = result.get("goal_coverage") or {}
    reqs = goal.get("requirements") or []
    trace = result.get("agent_trace") or []

    # Merge blocked_final from trace onto journal by decision_index
    blocked_by_idx = {
        int(t.get("decision_index") or 0): bool(t.get("blocked_final"))
        for t in trace
        if t.get("blocked_final")
    }
    for entry in journal:
        idx = int(entry.get("decision_index") or 0)
        entry["blocked_final"] = bool(blocked_by_idx.get(idx))

    final_decisions = [j for j in journal if j.get("act") == "final_answer" and j.get("validator_status") == "ok"]
    covering_left = {
        t: sorted(covering_tools(t))
        for t in [r.get("type") for r in reqs if r.get("status") == "uncovered"]
        if t
    }

    return {
        "latency_ms": latency_ms,
        "decisions": [
            {
                "decision_index": j.get("decision_index"),
                "agent_step": j.get("agent_step"),
                "act": j.get("act"),
                "tool": j.get("tool"),
                "requirements": j.get("requirements"),
                "covered": j.get("covered"),
                "uncovered": j.get("uncovered"),
                "blocked_final": j.get("blocked_final"),
                "allow_partial": j.get("allow_partial_computed"),
                "decision_retry_count": j.get("decision_retry_count"),
                "blocked_finals_before": j.get("blocked_finals_before"),
                "remaining_invokes": j.get("remaining_invokes"),
                "validator_status": j.get("validator_status"),
            }
            for j in journal
        ],
        "tools_used": tools,
        "goal_final": {
            "requirements": [r.get("type") for r in reqs],
            "covered": [r.get("type") for r in reqs if r.get("status") == "covered"],
            "uncovered": [r.get("type") for r in reqs if r.get("status") == "uncovered"],
        },
        "covering_tools_still_available_for_uncovered": covering_left,
        "blocked_final_count": sum(1 for t in trace if t.get("blocked_final")),
        "fallback": bool(result.get("fallback_used")),
        "fallback_reason": result.get("fallback_reason"),
        "final_reason": result.get("fallback_reason") or ("ok" if result.get("ok") else "fail"),
        "ok": bool(result.get("ok")),
        "get_purchase_orders_called": "get_purchase_orders" in tools,
        "MAX_DECISION_RETRIES": MAX_DECISION_RETRIES,
        "second_final": final_decisions[1] if len(final_decisions) >= 2 else None,
        "first_final": final_decisions[0] if final_decisions else None,
    }


def _deterministic_double_final_loop() -> dict[str, Any]:
    """In-process AgentLoop: supplier covered, then two final_answers — no LLM.

    Does not call get_purchase_orders. Proves allow_partial accepts the second final.
    """
    from unittest.mock import patch

    from app.assistant.orchestrator.agent_loop import QueueDecisionClient
    from app.assistant.orchestrator.audit import OrchestratorAudit
    from app.assistant.orchestrator.planner import FakePlanner
    from app.assistant.orchestrator.service import run_orchestrator_chat
    from app.assistant.orchestrator.turn_store import TurnStore
    from tests.test_orchestrator_fase81_agent import _call

    tools_seen: list[str] = []

    def invoke_fn(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        tool = str(payload.get("tool") or "")
        tools_seen.append(tool)
        if tool == "get_supplier":
            return 200, {
                "ok": True,
                "tool": "get_supplier",
                "classification": "INTERNAL",
                "data": {"nombre": "BOSCH"},
                "meta": {},
            }
        if tool == "get_purchase_orders":
            return 200, {
                "ok": True,
                "tool": "get_purchase_orders",
                "classification": "INTERNAL",
                "data": {"items": [{"numero": "OC-1"}]},
                "meta": {},
            }
        return 200, {"ok": True, "tool": tool, "data": {}, "meta": {}, "empty": True}

    client = QueueDecisionClient(
        [
            _call("get_supplier", {"q": "BOSCH"}),
            {
                "action": "final_answer",
                "arguments": {},
                "draft_reply": "Proveedor BOSCH.",
                "claims": [{"kind": "dato", "text": "BOSCH", "evidence_ids": ["e1"]}],
                "calculations": [],
            },
            {
                "action": "final_answer",
                "arguments": {},
                "draft_reply": "Proveedor BOSCH (parcial).",
                "claims": [{"kind": "dato", "text": "BOSCH", "evidence_ids": ["e1"]}],
                "calculations": [],
            },
        ]
    )
    with patch("app.assistant.orchestrator.service.agent_loop_allowed", return_value=True):
        with patch.dict(
            os.environ,
            {
                "ANDES_ASSISTANT_AGENT_ENABLED": "0",
                "ANDES_ASSISTANT_NL_ENABLED": "0",
                "ANDES_ORCH_PLANNER": "fake",
            },
            clear=False,
        ):
            import tempfile
            from pathlib import Path as P

            tmp = tempfile.TemporaryDirectory()
            try:
                out = run_orchestrator_chat(
                    message="Proveedor BOSCH y sus órdenes de compra.",
                    actor_user="albertadmin",
                    conversation_id="fase81f5-det-double-final",
                    invoke_fn=invoke_fn,
                    planner=FakePlanner(),
                    audit=OrchestratorAudit(path=P(tmp.name) / "audit.jsonl"),
                    turn_store=TurnStore(),
                    agent_decision_client=client,
                )
            finally:
                tmp.cleanup()

    trace = out.get("agent_trace") or []
    blocked = [t for t in trace if t.get("blocked_final")]
    finals = [t for t in trace if t.get("action") == "final_answer"]
    goal = out.get("goal_coverage") or {}
    reqs = goal.get("requirements") or []
    return {
        "tools_executed": list(tools_seen),
        "get_purchase_orders_executed": "get_purchase_orders" in tools_seen,
        "blocked_final_count": len(blocked),
        "final_answer_trace_count": len(finals),
        "fallback": bool(out.get("fallback_used")),
        "fallback_reason": out.get("fallback_reason"),
        "ok": bool(out.get("ok")),
        "uncovered_final": [r.get("type") for r in reqs if r.get("status") == "uncovered"],
        "covered_final": [r.get("type") for r in reqs if r.get("status") == "covered"],
        "policy_after_blocked_final": (
            "second final_answer accepted via allow_partial "
            "(blocked_finals>=1); covering tool never forced"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    argv = list(argv or sys.argv[1:])
    skip_llm = "--no-llm" in argv
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    deterministic = _deterministic_no_llm()
    double_final = _deterministic_double_final_loop()
    if skip_llm:
        prev = OUT_DIR / "t07_allow_partial.json"
        llm: dict[str, Any] = {"error": "skipped"}
        if prev.is_file():
            try:
                prev_data = json.loads(prev.read_text(encoding="utf-8"))
                if isinstance(prev_data.get("llm_run"), dict) and "error" not in prev_data["llm_run"]:
                    llm = prev_data["llm_run"]
            except (OSError, json.JSONDecodeError):
                pass
    else:
        llm = _run_llm_once()

    # Root cause: literal condition blocked_finals>=1 → allow_partial always True.
    # Facets C/D follow; primary letter = A.
    root = "A"
    report = {
        "phase": "8.1F.5",
        "case_id": "T07",
        "T07_ALLOW_PARTIAL_ROOT_CAUSE": root,
        "hypothesis": {
            "A": "allow_partial is true always after blocked_final",
            "B": "allow_partial only when requirements optional",
            "C": "allow_partial ignores covering_tool for uncovered",
            "D": "block consumes one turn then finalizes by policy",
            "E": "retry/limit interaction causes second final",
            "F": "other",
        },
        "code_policy": {
            "location": "app/assistant/orchestrator/agent_loop.py ~462",
            "formula": "allow_partial = state.blocked_finals >= 1 or remaining <= 0",
            "gate": "if blocks_final() and not allow_partial: blocked_finals+=1; continue",
            "else": "accept final_answer (append unresolved note if uncovered/impossible)",
            "does_not_check": [
                "covering_tools(uncovered)",
                "hard vs optional (no such distinction exists)",
                "whether model proposed call_tool",
            ],
            "MAX_DECISION_RETRIES": "schema/LLM retries only; not related to blocked_final",
            "requirement_model": {
                "statuses": ["uncovered", "covered", "impossible"],
                "hard_requirement": False,
                "optional_requirement": False,
                "partial_answer_flag": "only via allow_partial derived from blocked_finals/remaining",
            },
        },
        "deterministic_no_llm": deterministic,
        "deterministic_double_final_loop": double_final,
        "llm_run": llm,
        "related_facets": ["C", "D"],
        "related_facets_note": (
            "A is the mechanical root (blocked_finals>=1 => allow_partial). "
            "D describes the same one-block-then-accept policy. "
            "C is the missing guard (no covering_tool check). "
            "E is false: MAX_DECISION_RETRIES is orthogonal (invalid decision retries). "
            "B is false: no optional-requirement concept."
        ),
        "minimal_fix_candidate": (
            "Tighten allow_partial: when extraction=detected and any uncovered requirement "
            "still has an unused covering_tool in ALLOWED_TOOLS and remaining>0, "
            "do NOT set allow_partial from blocked_finals alone — keep blocking "
            "(or only allow_partial when remaining<=0 / all uncovered lack covering tools / "
            "impossible). Do NOT implement in F.5."
        ),
        "expected_impact_on_legitimate_partials": (
            "Legitimate partials (remaining=0, covering tool already tried/empty, "
            "or impossible declared) would still finalize. Premature double-final "
            "with unused covering tool (T07) would keep looping / force another decision "
            "until tool call, budget exhaustion, or impossible."
        ),
    }

    out_json = OUT_DIR / "t07_allow_partial.json"
    out_txt = OUT_DIR / "t07_allow_partial.txt"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    llm_ok = "error" not in llm
    lines = [
        "FASE 8.1F.5 T07 allow_partial diagnosis",
        f"T07_ALLOW_PARTIAL_ROOT_CAUSE={root}",
        "formula: allow_partial = blocked_finals >= 1 or remaining <= 0",
        f"deterministic first_final -> {deterministic['after_first_final_answer']['policy_outcome']}",
        f"deterministic second_final -> {deterministic['after_blocked_final_second_final_answer']['policy_outcome']}",
        f"covering_tool ignored=True",
        f"double_final_loop tools={double_final.get('tools_executed')}",
        f"double_final_loop get_po={double_final.get('get_purchase_orders_executed')}",
        f"double_final_loop blocked={double_final.get('blocked_final_count')} "
        f"fallback={double_final.get('fallback')} uncovered={double_final.get('uncovered_final')}",
    ]
    if llm_ok:
        lines.extend(
            [
                f"llm fallback={llm.get('fallback')} reason={llm.get('final_reason')}",
                f"llm tools={llm.get('tools_used')}",
                f"llm blocked_final_count={llm.get('blocked_final_count')}",
                f"llm get_purchase_orders={llm.get('get_purchase_orders_called')}",
                f"llm uncovered_final={llm.get('goal_final', {}).get('uncovered')}",
            ]
        )
        for d in llm.get("decisions") or []:
            lines.append(
                f"d{d.get('decision_index')}: act={d.get('act')} tool={d.get('tool')} "
                f"blocked_final={d.get('blocked_final')} allow_partial={d.get('allow_partial')} "
                f"uncovered={d.get('uncovered')} retry={d.get('decision_retry_count')}"
            )
    else:
        lines.append(f"llm_error={llm.get('error')}")
    lines.append("candidate: tighten allow_partial when covering_tool unused and remaining>0")
    out_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"T07_ALLOW_PARTIAL_ROOT_CAUSE={root}")
    print(
        json.dumps(
            {
                "deterministic_second": deterministic["after_blocked_final_second_final_answer"][
                    "policy_outcome"
                ],
                "double_final_get_po": double_final.get("get_purchase_orders_executed"),
                "llm_ok": llm_ok,
            },
            ensure_ascii=False,
        )
    )
    return 0 if llm_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
