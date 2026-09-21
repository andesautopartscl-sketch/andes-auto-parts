"""FASE 8.1F.6 — T07 LLM validation after allow_partial fix. Safe fields only."""
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


def _run_once(conversation_id: str) -> dict[str, Any]:
    from app.assistant.orchestrator import agent_loop as al
    from app.assistant.orchestrator.agent_config import MAX_TOOL_CALLS, agent_loop_allowed
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
        reqs: list[str] = []
        covered: list[str] = []
        uncovered: list[str] = []
        allow_p = None
        if st is not None:
            remaining = max(0, MAX_TOOL_CALLS - int(st.invoke_count or 0))
            st.goal.refresh(st.evidence)
            allow_p = allow_partial_final(st.goal, st.evidence, remaining=remaining)
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
        entry = {
            "decision_index": (st.step_index + 1) if st is not None else len(journal) + 1,
            "agent_step": (st.step_index + 1) if st is not None else None,
            "act": act,
            "tool": tool,
            "requirements": reqs,
            "covered": covered,
            "uncovered": uncovered,
            "allow_partial": allow_p,
            "blocked_final": False,
            "remaining_invokes": remaining,
        }
        try:
            decision = real_validate(raw, user_message=user_message)
            entry["act"] = str(decision.get("action") or "")
            entry["tool"] = str(decision.get("tool") or "") or None
            journal.append(entry)
            state_box["attempt_idx"] += 1
            return decision
        except AgentDecisionError as exc:
            entry["act"] = act
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
            conversation_id=conversation_id,
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
    blocked_idx = {int(t.get("decision_index") or 0) for t in trace if t.get("blocked_final")}
    for entry in journal:
        entry["blocked_final"] = int(entry.get("decision_index") or 0) in blocked_idx

    covered = [r.get("type") for r in reqs if r.get("status") == "covered"]
    uncovered = [r.get("type") for r in reqs if r.get("status") == "uncovered"]
    both = set(covered) >= {"supplier", "purchase_orders"} and not uncovered
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
                "allow_partial": j.get("allow_partial"),
            }
            for j in journal
        ],
        "tools_used": tools,
        "get_purchase_orders_count": tools.count("get_purchase_orders"),
        "requirements": [r.get("type") for r in reqs],
        "covered": covered,
        "uncovered": uncovered,
        "blocked_final_count": len(blocked_idx),
        "fallback": bool(result.get("fallback_used")),
        "fallback_reason": result.get("fallback_reason"),
        "final_reason": result.get("fallback_reason") or ("ok" if result.get("ok") else "fail"),
        "ok": bool(result.get("ok")),
        "both_covered": both,
        "premature_partial": bool(
            result.get("ok")
            and not result.get("fallback_used")
            and "purchase_orders" in uncovered
            and "get_purchase_orders" not in tools
        ),
    }


def main() -> int:
    from app.utils.load_env import load_project_dotenv

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    load_project_dotenv(force=False)
    if not _inherit_key():
        print("FATAL: NO_LLM_KEY")
        return 2
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

    single = _run_once("fase81f6-T07-once")
    report: dict[str, Any] = {
        "phase": "8.1F.6",
        "case_id": "T07",
        "single": single,
        "x10": None,
    }

    gate = (
        not single.get("error")
        and not single.get("premature_partial")
        and single.get("get_purchase_orders_count", 0) >= 1
        and single.get("both_covered")
    )
    if gate:
        runs = []
        for i in range(10):
            runs.append(_run_once(f"fase81f6-T07-x10-{i+1:02d}"))
        report["x10"] = {
            "n": 10,
            "both_covered": sum(1 for r in runs if r.get("both_covered")),
            "get_purchase_orders_runs": sum(1 for r in runs if r.get("get_purchase_orders_count", 0) >= 1),
            "premature_partial": sum(1 for r in runs if r.get("premature_partial")),
            "fallback": sum(1 for r in runs if r.get("fallback")),
            "runs": [
                {
                    "both_covered": r.get("both_covered"),
                    "tools_used": r.get("tools_used"),
                    "fallback": r.get("fallback"),
                    "fallback_reason": r.get("fallback_reason"),
                    "uncovered": r.get("uncovered"),
                    "latency_ms": r.get("latency_ms"),
                }
                for r in runs
            ],
        }

    out_json = OUT_DIR / "t07_allow_partial_fix_81f6.json"
    out_txt = OUT_DIR / "t07_allow_partial_fix_81f6.txt"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "FASE 8.1F.6 T07 allow_partial fix validation",
        f"gate_x10={gate}",
        f"both_covered={single.get('both_covered')}",
        f"tools={single.get('tools_used')}",
        f"get_po_count={single.get('get_purchase_orders_count')}",
        f"uncovered={single.get('uncovered')}",
        f"blocked_final_count={single.get('blocked_final_count')}",
        f"fallback={single.get('fallback')} reason={single.get('final_reason')}",
        f"premature_partial={single.get('premature_partial')}",
        f"latency_ms={single.get('latency_ms')}",
    ]
    for d in single.get("decisions") or []:
        lines.append(
            f"d{d.get('decision_index')}: act={d.get('act')} tool={d.get('tool')} "
            f"blocked_final={d.get('blocked_final')} allow_partial={d.get('allow_partial')} "
            f"uncovered={d.get('uncovered')}"
        )
    if report["x10"]:
        x = report["x10"]
        lines.append(
            f"x10 both_covered={x['both_covered']}/10 get_po={x['get_purchase_orders_runs']}/10 "
            f"premature={x['premature_partial']}/10 fallback={x['fallback']}/10"
        )
    out_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"gate_x10": gate, "both_covered": single.get("both_covered"), "tools": single.get("tools_used")}, ensure_ascii=False))
    return 0 if not single.get("error") else 2


if __name__ == "__main__":
    raise SystemExit(main())
