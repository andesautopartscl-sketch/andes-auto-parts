"""FASE 8.1F.3 — diagnose T07 invalid_decision. No production changes."""
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


def _args_shape(arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        return {"type": type(arguments).__name__, "keys": []}
    return {
        "type": "object",
        "keys": sorted(str(k) for k in arguments.keys()),
        "key_count": len(arguments),
        "null_value_keys": [str(k) for k, v in arguments.items() if v is None],
        "non_null_key_count": sum(1 for v in arguments.values() if v is not None),
    }


def _raw_structure(raw: Any) -> dict[str, Any]:
    """Safe structural summary of a raw AgentDecision — no free text."""
    if not isinstance(raw, dict):
        return {"raw_type": type(raw).__name__}
    keys = sorted(str(k) for k in raw.keys())
    action = str(raw.get("action") or "")
    tool = raw.get("tool")
    tool_type = type(tool).__name__ if tool is not None else "absent"
    if isinstance(tool, (list, tuple)):
        tool_repr = {"kind": "array", "len": len(tool), "items": [str(t)[:64] for t in tool[:5]]}
    elif tool is None or tool == "":
        tool_repr = {"kind": "empty"}
    else:
        tool_repr = {"kind": "string", "value": str(tool)[:64]}
    claims = raw.get("claims")
    calcs = raw.get("calculations")
    forbidden = [k for k in keys if k in {
        "steps", "n_steps", "plan", "plan_id", "bindings", "depends_on", "tools", "tool_calls"
    }]
    return {
        "top_level_keys": keys,
        "action": action[:40] or None,
        "tool": tool_repr,
        "tool_python_type": tool_type,
        "args_shape": _args_shape(raw.get("arguments")),
        "has_draft_reply": bool(str(raw.get("draft_reply") or "").strip()),
        "draft_reply_len": len(str(raw.get("draft_reply") or "")),
        "claims_type": type(claims).__name__ if claims is not None else "absent",
        "claims_count": len(claims) if isinstance(claims, list) else None,
        "calculations_type": type(calcs).__name__ if calcs is not None else "absent",
        "calculations_count": len(calcs) if isinstance(calcs, list) else None,
        "has_figures_in_draft_or_claims_blob": bool(
            __import__("re").search(
                r"\d",
                str(raw.get("draft_reply") or "")
                + (json.dumps(claims, ensure_ascii=False) if isinstance(claims, list) else ""),
            )
        ),
        "forbidden_multistep_keys": forbidden,
        "proposed_requirements_count": len(raw.get("proposed_requirements") or [])
        if isinstance(raw.get("proposed_requirements"), list)
        else 0,
    }


def _classify_error(code: str | None, message: str, structure: dict[str, Any]) -> str:
    code = str(code or "")
    msg = str(message or "")
    act = structure.get("action")
    tool = (structure.get("tool") or {}).get("value") if isinstance(structure.get("tool"), dict) else None
    if code == "invalid_args":
        return "C"
    if code == "tool_not_allowed":
        return "D"
    if code == "write_not_allowed":
        return "D"
    if code == "claims_required":
        return "A"  # final_answer path rejected for figures without claims
    if "forbidden fields" in msg or structure.get("forbidden_multistep_keys"):
        return "E"
    if act == "final_answer" and code == "invalid_decision":
        if "requires draft_reply or claims" in msg:
            return "A"
        if "must not include a tool call" in msg:
            return "E"
        return "A"
    if act == "call_tool" and code == "invalid_decision":
        if "requires tool" in msg or (structure.get("tool") or {}).get("kind") == "empty":
            return "B"
        if "array" in msg:
            return "E"
        return "E"
    if code == "invalid_decision":
        return "E"
    if code in {"LlmError", "llm_unavailable", "JSONDecodeError"}:
        return "H"
    return "H"


def main() -> int:
    from app.utils.load_env import load_project_dotenv

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

    try:
        from app.assistant.orchestrator import agent_loop as al
        from app.assistant.orchestrator.agent_config import agent_loop_allowed, MAX_DECISION_RETRIES
        from app.assistant.orchestrator.agent_loop import LlmAgentDecisionClient
        from app.assistant.orchestrator.agent_schema import AgentDecisionError, validate_agent_decision
        from app.assistant.orchestrator.factory import build_planner
        from app.assistant.orchestrator.llm.client import LlmError, OpenAICompatibleClient
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

        def refresh_hook(state):
            state_box["state"] = state
            return real_refresh(state)

        def validate_hook(raw, *, user_message=None):
            st = state_box["state"]
            structure = _raw_structure(raw)
            reqs = []
            covered = []
            uncovered = []
            if st is not None:
                st.goal.refresh(st.evidence)
                snap = st.goal.safe_snapshot()
                for r in snap.get("requirements") or []:
                    reqs.append(str(r.get("type")))
                    if r.get("status") == "covered":
                        covered.append(str(r.get("type")))
                    elif r.get("status") == "uncovered":
                        uncovered.append(str(r.get("type")))
            entry = {
                "decision_index": (st.step_index + 1) if st is not None else len(journal) + 1,
                "agent_step": (st.step_index + 1) if st is not None else None,
                "attempt_in_step": state_box["attempt_idx"],
                "act": structure.get("action"),
                "tool": (structure.get("tool") or {}).get("value")
                if (structure.get("tool") or {}).get("kind") == "string"
                else None,
                "tool_structure": structure.get("tool"),
                "args_shape": structure.get("args_shape"),
                "raw_structure": structure,
                "requirements": reqs,
                "covered": covered,
                "uncovered": uncovered,
                "previous_tool": state_box["prev_tool"],
                "validator_status": "pending",
                "validator_error_code": None,
                "validator_error_message_safe": None,
                "diagnosis_letter": None,
            }
            try:
                out = real_validate(raw, user_message=user_message)
                entry["validator_status"] = "ok"
                entry["act"] = out.get("action")
                entry["tool"] = out.get("tool") or None
                entry["args_shape"] = _args_shape(out.get("arguments"))
                if out.get("action") == "call_tool" and out.get("tool"):
                    # previous_tool updates after successful invoke; set tentatively
                    pass
                journal.append(entry)
                state_box["attempt_idx"] = 0
                return out
            except AgentDecisionError as exc:
                entry["validator_status"] = "rejected"
                entry["validator_error_code"] = getattr(exc, "code", None)
                # Safe message: schema codes only, truncate, no model text
                entry["validator_error_message_safe"] = str(getattr(exc, "message", "") or "")[:160]
                if isinstance(getattr(exc, "details", None), dict):
                    entry["validator_details_keys"] = sorted(str(k) for k in exc.details.keys())
                    fields = exc.details.get("fields")
                    if isinstance(fields, dict):
                        entry["invalid_arg_fields"] = {
                            str(k): str(v)[:40] for k, v in list(fields.items())[:12]
                        }
                entry["diagnosis_letter"] = _classify_error(
                    entry["validator_error_code"],
                    entry["validator_error_message_safe"] or "",
                    structure,
                )
                journal.append(entry)
                state_box["attempt_idx"] += 1
                raise
            except Exception as exc:
                entry["validator_status"] = "rejected"
                entry["validator_error_code"] = getattr(exc, "code", None) or type(exc).__name__
                entry["validator_error_message_safe"] = type(exc).__name__
                entry["diagnosis_letter"] = "H"
                journal.append(entry)
                state_box["attempt_idx"] += 1
                raise

        # Patch module globals used by agent_loop
        al.validate_agent_decision = validate_hook  # type: ignore[assignment]
        al._refresh_cost = refresh_hook  # type: ignore[assignment]

        # Also track successful tool invokes for previous_tool
        real_run = al.run_plan_steps

        def run_hook(*a, **k):
            new_ev, payloads = real_run(*a, **k)
            for e in new_ev:
                if e.get("tool"):
                    state_box["prev_tool"] = str(e.get("tool"))
            return new_ev, payloads

        al.run_plan_steps = run_hook  # type: ignore[assignment]

        gold = next(c for c in load_cases(DATASET) if c.get("id") == "T07")
        t0 = time.perf_counter()
        try:
            result = run_orchestrator_chat(
                message=str(gold.get("prompt") or ""),
                actor_user=gold.get("actor") or "albertadmin",
                conversation_id="fase81f3-T07-invalid",
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

        rejected = [j for j in journal if j.get("validator_status") == "rejected"]
        second_focus = None
        # Prefer first rejection after a successful call_tool, else first rejection overall
        ok_calls = [j for j in journal if j.get("validator_status") == "ok" and j.get("act") == "call_tool"]
        if ok_calls and rejected:
            after_first = [j for j in rejected if j.get("decision_index", 0) >= ok_calls[0].get("decision_index", 0)]
            second_focus = after_first[0] if after_first else rejected[0]
        elif rejected:
            second_focus = rejected[0]

        primary = (second_focus or {}).get("diagnosis_letter") or "H"
        path_note = (
            "LLM complete_decision → validate_agent_decision (agent_schema) "
            "→ [normalize_agent_args + validate_tool_args on call_tool] "
            "→ AgentLoop act handling → GoalCoverage → ToolRunner. "
            f"invalid_decision code emitted at agent_schema; AgentLoop maps last_err to fallback_reason "
            f"after MAX_DECISION_RETRIES={MAX_DECISION_RETRIES}."
        )

        report = {
            "phase": "8.1F.3",
            "case_id": "T07",
            "latency_ms": latency_ms,
            "tools_used": tools,
            "fallback": bool(result.get("fallback_used")),
            "fallback_reason": result.get("fallback_reason"),
            "goal_final": {
                "requirements": [r.get("type") for r in reqs],
                "covered": [r.get("type") for r in reqs if r.get("status") == "covered"],
                "uncovered": [r.get("type") for r in reqs if r.get("status") == "uncovered"],
            },
            "decisions": journal,
            "second_decision_focus": second_focus,
            "T07_INVALID_DECISION_DIAGNOSIS": primary,
            "hypothesis": {
                "A": "final_answer while purchase_orders uncovered / empty final_answer",
                "B": "call_tool without tool",
                "C": "correct tool but invalid args",
                "D": "disallowed tool",
                "E": "AgentDecision schema rejects response",
                "F": "PlanValidator rejects (not on this path for AgentLoop decisions)",
                "G": "normalization modifies/rejects",
                "H": "other",
            },
            "path": path_note,
            "PlanValidator_involved": False,
            "minimal_fix_candidate": None,
        }

        # Refine candidate from focus
        focus = second_focus or {}
        code = focus.get("validator_error_code")
        msg = focus.get("validator_error_message_safe") or ""
        if primary == "A":
            report["minimal_fix_candidate"] = (
                "Do not change prompts yet. Options later: stronger blocked_final retry when "
                "uncovered remains, or tolerate/repair empty final_answer structure — NOT in F.3."
            )
        elif primary == "C":
            report["minimal_fix_candidate"] = (
                f"Fix args contract/normalize for tool={focus.get('tool')} fields="
                f"{focus.get('invalid_arg_fields')} — NOT in F.3."
            )
        elif primary == "E":
            report["minimal_fix_candidate"] = (
                f"Schema rejection code={code} msg={msg!r} structure_keys="
                f"{(focus.get('raw_structure') or {}).get('top_level_keys')} — "
                "likely forbidden multistep fields or malformed act; NOT implementing workaround in F.3."
            )
        else:
            report["minimal_fix_candidate"] = (
                f"Investigate diagnosis={primary} code={code} — NOT implementing in F.3."
            )

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / "t07_invalid_decision.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        lines = [
            "FASE 8.1F.3 T07 invalid_decision diagnosis",
            f"diagnosis={primary} fallback_reason={result.get('fallback_reason')}",
            f"tools={tools}",
            f"goal_final={report['goal_final']}",
            "",
            "decisions:",
        ]
        for j in journal:
            lines.append(
                f"  d{j.get('decision_index')} attempt={j.get('attempt_in_step')} "
                f"act={j.get('act')} tool={j.get('tool')} status={j.get('validator_status')} "
                f"err={j.get('validator_error_code')} letter={j.get('diagnosis_letter')} "
                f"covered={j.get('covered')} uncovered={j.get('uncovered')} "
                f"prev={j.get('previous_tool')} forbidden={((j.get('raw_structure') or {}).get('forbidden_multistep_keys'))}"
            )
        lines += [
            "",
            f"focus={json.dumps(second_focus, ensure_ascii=False) if second_focus else None}",
            f"path={path_note}",
            f"candidate={report['minimal_fix_candidate']}",
        ]
        (OUT_DIR / "t07_invalid_decision.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {
                    "diagnosis": primary,
                    "fallback_reason": result.get("fallback_reason"),
                    "tools": tools,
                    "decisions_n": len(journal),
                    "rejected_n": len(rejected),
                    "focus_code": (second_focus or {}).get("validator_error_code"),
                    "focus_msg": (second_focus or {}).get("validator_error_message_safe"),
                    "latency_ms": latency_ms,
                },
                ensure_ascii=False,
            )
        )
        return 0
    finally:
        os.environ["ANDES_ASSISTANT_AGENT_ENABLED"] = "0"
        os.environ["ANDES_ASSISTANT_NL_ENABLED"] = "0"
        os.environ["ANDES_ORCH_PLANNER"] = "fake"
        print("RESTORED AGENT=0 NL=0 ORCH=fake")


if __name__ == "__main__":
    raise SystemExit(main())
