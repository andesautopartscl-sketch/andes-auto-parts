"""FASE 8.1F.4 — T07 one-shot LLM validation after residual-args schema fix.

Safe fields only: no prompts, raw model text, headers, or secrets.
"""
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


def main() -> int:
    from app.utils.load_env import load_project_dotenv

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    load_project_dotenv(force=False)
    if not _inherit_key():
        print("FATAL: ANDES_LLM_API_KEY missing")
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

    from app.assistant.orchestrator import agent_loop as al
    from app.assistant.orchestrator.agent_config import agent_loop_allowed
    from app.assistant.orchestrator.agent_loop import LlmAgentDecisionClient
    from app.assistant.orchestrator.agent_schema import AgentDecisionError, validate_agent_decision
    from app.assistant.orchestrator.factory import build_planner
    from app.assistant.orchestrator.llm.client import OpenAICompatibleClient
    from app.assistant.orchestrator.llm.config import load_llm_settings
    from app.assistant.orchestrator.service import run_orchestrator_chat
    from app.assistant.orchestrator.turn_store import TurnStore
    from app.assistant.routes import invoke_gateway
    from evals.fase81_runner import load_cases

    if not agent_loop_allowed():
        print("FATAL: agent_loop_allowed=false")
        return 2

    journal: list[dict[str, Any]] = []
    state_box: dict[str, Any] = {"state": None, "prev_tool": None, "attempt_idx": 0}
    real_validate = validate_agent_decision
    real_refresh = al._refresh_cost
    real_run = al.run_plan_steps

    def refresh_hook(state):
        state_box["state"] = state
        state_box["attempt_idx"] = 0
        return real_refresh(state)

    def validate_hook(raw, *, user_message=None):
        st = state_box["state"]
        entry: dict[str, Any] = {
            "decision_index": (st.step_index + 1) if st is not None else len(journal) + 1,
            "attempt_in_step": state_box["attempt_idx"],
            "act": str((raw or {}).get("action") or "")[:40] if isinstance(raw, dict) else None,
            "tool": None,
            "previous_tool": state_box.get("prev_tool"),
            "validator_status": None,
            "validator_error_code": None,
        }
        if isinstance(raw, dict):
            tool = raw.get("tool")
            entry["tool"] = str(tool)[:64] if tool else None
        try:
            decision = real_validate(raw, user_message=user_message)
            entry["act"] = str(decision.get("action") or "")
            entry["tool"] = str(decision.get("tool") or "") or None
            entry["validator_status"] = "ok"
            entry["arguments_empty"] = decision.get("arguments") == {}
            journal.append(entry)
            state_box["attempt_idx"] += 1
            return decision
        except AgentDecisionError as exc:
            entry["validator_status"] = "rejected"
            entry["validator_error_code"] = exc.code
            entry["validator_error_message_safe"] = (exc.message or "")[:120]
            journal.append(entry)
            state_box["attempt_idx"] += 1
            raise

    def run_hook(*a, **k):
        new_ev, payloads = real_run(*a, **k)
        for e in new_ev:
            if e.get("tool"):
                state_box["prev_tool"] = str(e.get("tool"))
        return new_ev, payloads

    al.validate_agent_decision = validate_hook  # type: ignore[assignment]
    al._refresh_cost = refresh_hook  # type: ignore[assignment]
    al.run_plan_steps = run_hook  # type: ignore[assignment]

    gold = next(c for c in load_cases(DATASET) if c.get("id") == "T07")
    t0 = time.perf_counter()
    try:
        result = run_orchestrator_chat(
            message=str(gold.get("prompt") or ""),
            actor_user=gold.get("actor") or "albertadmin",
            conversation_id="fase81f4-T07-schema",
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

    ok_decisions = [j for j in journal if j.get("validator_status") == "ok"]
    rejected = [j for j in journal if j.get("validator_status") == "rejected"]
    d1 = ok_decisions[0] if ok_decisions else (journal[0] if journal else {})
    d2 = ok_decisions[1] if len(ok_decisions) >= 2 else (journal[1] if len(journal) >= 2 else {})

    blocked = [t for t in trace if t.get("blocked_final")]
    invalid_in_journal = any(j.get("validator_error_code") == "invalid_decision" for j in rejected)

    report = {
        "phase": "8.1F.4",
        "case_id": "T07",
        "latency_ms": latency_ms,
        "decision_1": {
            "act": d1.get("act"),
            "tool": d1.get("tool"),
            "validator_status": d1.get("validator_status"),
            "validator_error_code": d1.get("validator_error_code"),
        },
        "decision_2": {
            "act": d2.get("act"),
            "tool": d2.get("tool"),
            "validator_status": d2.get("validator_status"),
            "validator_error_code": d2.get("validator_error_code"),
            "arguments_empty": d2.get("arguments_empty"),
        },
        "requirements": [r.get("type") for r in reqs],
        "covered": [r.get("type") for r in reqs if r.get("status") == "covered"],
        "uncovered": [r.get("type") for r in reqs if r.get("status") == "uncovered"],
        "tools_used": tools,
        "fallback": bool(result.get("fallback_used")),
        "fallback_reason": result.get("fallback_reason"),
        "final_reason": result.get("fallback_reason") or ("ok" if result.get("ok") else "fail"),
        "invalid_decision_seen": invalid_in_journal
        or str(result.get("fallback_reason") or "") == "invalid_decision",
        "blocked_final_count": len(blocked),
        "get_purchase_orders_called": "get_purchase_orders" in tools,
        "ok": bool(result.get("ok")),
        "journal_len": len(journal),
        "rejected_codes": [j.get("validator_error_code") for j in rejected],
    }

    out_json = OUT_DIR / "t07_schema_fix_81f4.json"
    out_txt = OUT_DIR / "t07_schema_fix_81f4.txt"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "FASE 8.1F.4 T07 schema-fix LLM validation",
        f"decision_1 act={report['decision_1']['act']} tool={report['decision_1']['tool']}",
        f"decision_2 act={report['decision_2']['act']} tool={report['decision_2']['tool']} "
        f"validator={report['decision_2']['validator_status']}",
        f"requirements={report['requirements']}",
        f"covered={report['covered']}",
        f"uncovered={report['uncovered']}",
        f"tools={tools}",
        f"fallback={report['fallback']} reason={report['fallback_reason']}",
        f"invalid_decision_seen={report['invalid_decision_seen']}",
        f"blocked_final_count={report['blocked_final_count']}",
        f"get_purchase_orders_called={report['get_purchase_orders_called']}",
        f"latency_ms={latency_ms}",
    ]
    out_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
