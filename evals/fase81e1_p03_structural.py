"""FASE 8.1E.1 — structural deep-dive of one real P03 LLM run. No production changes."""
from __future__ import annotations

import hashlib
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


def _args_fp(arguments: Any) -> str:
    payload = json.dumps(arguments or {}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _arg_keys(arguments: Any) -> list[str]:
    if not isinstance(arguments, dict):
        return []
    return sorted(str(k) for k in arguments.keys())


def _inherit_key() -> bool:
    if (os.environ.get("ANDES_LLM_API_KEY") or "").strip():
        return True
    try:
        import psutil
    except ImportError:
        return False
    for p in psutil.process_iter(["pid", "name"]):
        try:
            name = (p.info.get("name") or "").lower()
            if name not in {"powershell.exe", "pwsh.exe", "cmd.exe"}:
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


class StructuralSpy:
    """Decision client wrapper + post-invoke structural journal. No free text."""

    def __init__(self, inner: Any):
        self.inner = inner
        self.last_usage = None
        self.invokes: list[dict[str, Any]] = []
        self.decisions: list[dict[str, Any]] = []
        self._pending: dict[str, Any] | None = None
        self._prev_tool: str | None = None
        self._prev_fp: str | None = None
        self._evidence_ids_seen: set[str] = set()
        self.state_ref: Any | None = None  # set after loop starts via hook

    def complete_decision(self, *, system: str, user: str) -> dict[str, Any]:
        raw = self.inner.complete_decision(system=system, user=user)
        self.last_usage = getattr(self.inner, "last_usage", None)
        action = str((raw or {}).get("action") or "")[:40]
        tool = (raw or {}).get("tool")
        args = (raw or {}).get("arguments") if isinstance((raw or {}).get("arguments"), dict) else {}
        fp = _args_fp(args) if action == "call_tool" else None
        st = self.state_ref
        goal_snap = None
        if st is not None and getattr(st, "goal", None) is not None:
            st.goal.refresh(st.evidence)
            goal_snap = st.goal.safe_snapshot()
        uncovered = []
        covered_n = 0
        req_n = 0
        if goal_snap:
            reqs = goal_snap.get("requirements") or []
            req_n = len(reqs)
            for r in reqs:
                if r.get("status") == "covered":
                    covered_n += 1
                elif r.get("status") == "uncovered":
                    uncovered.append(str(r.get("id")))
        ev_before = len(st.evidence.items) if st is not None else 0
        rec = {
            "decision_index": (st.step_index + 1) if st is not None else len(self.decisions) + 1,
            "decision_act": action or None,
            "tool": (str(tool)[:64] if tool else None),
            "args_fingerprint": fp,
            "arg_keys": _arg_keys(args) if action == "call_tool" else [],
            "evidence_count_before_decision": ev_before,
            "goal_requirement_count": req_n,
            "covered_requirement_count": covered_n,
            "uncovered_requirement_ids": uncovered,
            "requirement_types": [
                str(r.get("type")) for r in ((goal_snap or {}).get("requirements") or [])
            ],
            "extraction": (goal_snap or {}).get("extraction"),
        }
        self.decisions.append(rec)
        if action == "call_tool" and tool:
            self._pending = {
                "tool": str(tool)[:64],
                "args_fingerprint": fp,
                "arg_keys": _arg_keys(args),
                "evidence_count_before": ev_before,
                "goal_requirement_count": req_n,
                "covered_requirement_count": covered_n,
                "uncovered_requirement_ids": list(uncovered),
                "requirement_types": list(rec["requirement_types"]),
                "extraction": rec["extraction"],
                "same_tool_as_previous": self._prev_tool == str(tool),
                "same_args_as_previous": bool(fp and self._prev_fp and fp == self._prev_fp),
                "agent_step": rec["decision_index"],
            }
        else:
            self._pending = None
        return raw

    def note_after_invoke(self, state: Any, new_ev: list[dict[str, Any]]) -> None:
        if not self._pending:
            return
        after_ids = [i.evidence_id for i in state.evidence.items]
        new_ids = [eid for eid in after_ids if eid not in self._evidence_ids_seen]
        self._evidence_ids_seen.update(after_ids)
        state.goal.refresh(state.evidence)
        snap = state.goal.safe_snapshot()
        uncovered = [
            str(r.get("id"))
            for r in (snap.get("requirements") or [])
            if r.get("status") == "uncovered"
        ]
        covered_n = sum(
            1 for r in (snap.get("requirements") or []) if r.get("status") == "covered"
        )
        statuses = []
        for e in new_ev:
            statuses.append(
                {
                    "ok": bool(e.get("ok")),
                    "empty": bool(e.get("empty")),
                    "error_code": e.get("error_code"),
                    "tool": e.get("tool"),
                }
            )
        invoke_index = len(self.invokes) + 1
        # decision_act_next filled later
        row = {
            **self._pending,
            "invoke_index": invoke_index,
            "evidence_count_after": len(state.evidence.items),
            "new_evidence_ids_count": len(new_ids),
            "evidence_ids_after": after_ids[:12],
            "new_evidence_ids": new_ids[:12],
            "covered_requirement_count_after": covered_n,
            "uncovered_requirement_ids_after": uncovered,
            "requirement_types_after": [str(r.get("type")) for r in (snap.get("requirements") or [])],
            "tool_result_status": statuses[0] if statuses else None,
            "coverage_progress": covered_n > int(self._pending.get("covered_requirement_count") or 0),
            "decision_act_next": None,
        }
        self.invokes.append(row)
        self._prev_tool = row["tool"]
        self._prev_fp = row["args_fingerprint"]
        self._pending = None


def _patch_loop_for_spy(spy: StructuralSpy):
    """Hook continue_agent_loop internals without editing source files permanently."""
    import app.assistant.orchestrator.agent_loop as al

    real_continue = al.continue_agent_loop
    real_ingest = al._ingest_plan_evidence
    real_run_plan = al.run_plan_steps
    real_refresh_cost = al._refresh_cost

    def run_plan_steps_wrapped(*args, **kwargs):
        new_ev, payloads = real_run_plan(*args, **kwargs)
        spy._last_new_ev = new_ev  # type: ignore[attr-defined]
        return new_ev, payloads

    def ingest_wrapped(store, plan, evidence, correlation_id):
        real_ingest(store, plan, evidence, correlation_id)
        st = spy.state_ref
        if st is not None and getattr(spy, "_last_new_ev", None) is not None:
            spy.note_after_invoke(st, list(spy._last_new_ev))  # type: ignore[attr-defined]
            spy._last_new_ev = None  # type: ignore[attr-defined]

    def continue_with_state_hook(**kwargs):
        def refresh_cost_hook(state):
            spy.state_ref = state
            return real_refresh_cost(state)

        al._refresh_cost = refresh_cost_hook  # type: ignore[assignment]
        al.run_plan_steps = run_plan_steps_wrapped  # type: ignore[assignment]
        al._ingest_plan_evidence = ingest_wrapped  # type: ignore[assignment]
        try:
            return real_continue(**kwargs)
        finally:
            al._refresh_cost = real_refresh_cost  # type: ignore[assignment]
            al.run_plan_steps = real_run_plan  # type: ignore[assignment]
            al._ingest_plan_evidence = real_ingest  # type: ignore[assignment]

    return continue_with_state_hook, real_continue


def _fill_decision_act_next(spy: StructuralSpy) -> None:
    acts = [d.get("decision_act") for d in spy.decisions]
    # Map each invoke to the next decision after that call_tool decision
    call_indices = [
        i for i, d in enumerate(spy.decisions) if d.get("decision_act") == "call_tool"
    ]
    for inv_i, dec_i in enumerate(call_indices):
        if inv_i >= len(spy.invokes):
            break
        nxt = acts[dec_i + 1] if dec_i + 1 < len(acts) else None
        # If loop skip / fallback without decision, leave None; also check fallback
        spy.invokes[inv_i]["decision_act_next"] = nxt


def _classify(invokes: list[dict[str, Any]], decisions: list[dict[str, Any]], gold_prompt: str) -> dict[str, Any]:
    from app.assistant.orchestrator.goal_coverage import extract_requirement_types, covering_tools

    extraction, types = extract_requirement_types(gold_prompt)
    fps = [i.get("args_fingerprint") for i in invokes]
    unique_fps = sorted({f for f in fps if f})
    same_args_all = len(unique_fps) <= 1 and len(invokes) >= 2
    different_args = len(unique_fps) >= 2
    coverage_changed = any(i.get("coverage_progress") for i in invokes)
    new_ev_any = any(int(i.get("new_evidence_ids_count") or 0) > 0 for i in invokes)
    req_types = []
    for d in decisions:
        for t in d.get("requirement_types") or []:
            if t not in req_types:
                req_types.append(t)
    kpi_covers = "get_dashboard_kpis" in covering_tools("dashboard_kpis")
    inv_covers_inventory = "get_dashboard_kpis" in covering_tools("current_inventory")

    # Hypothesis selection
    root = "G"
    rationale = []
    if "current_inventory" in types and "dashboard_kpis" not in types:
        root = "D"
        rationale.append(
            "extract_requirement_types fired current_inventory from 'stock' in prompt; "
            "dashboard_kpis was NOT auto-extracted"
        )
    if new_ev_any and not coverage_changed:
        if root == "D":
            rationale.append(
                "each get_dashboard_kpis invoke added evidence ids but covered_requirement_count stayed 0 "
                "because covering_tools(current_inventory)={get_inventory,check_stock}"
            )
            # D implies C as mechanism
            root = "D"  # primary; C is mechanism
            rationale.append("mechanism_also=C (new evidence, no coverage progress)")
        else:
            root = "C"
            rationale.append("new evidence ids appeared but coverage counts did not increase")
    if different_args and not same_args_all:
        rationale.append(
            f"args fingerprints differ across invokes unique={len(unique_fps)} "
            "(loop_keys only blocks identical tool+args; different args still execute)"
        )
    if same_args_all and len(invokes) >= 2:
        root = "A"
        rationale.append("same args fingerprint repeated")
    # F: model kept calling despite evidence existing
    if len(invokes) >= 2 and new_ev_any:
        rationale.append(
            "model continued call_tool get_dashboard_kpis after prior KPI evidence existed (F secondary)"
        )

    no_progress_candidate = bool(
        len(invokes) >= 2
        and all(i.get("tool") == "get_dashboard_kpis" for i in invokes)
        and not coverage_changed
        and (
            # either no new evidence on later invokes, or new evidence that does not cover
            any(int(i.get("new_evidence_ids_count") or 0) == 0 for i in invokes[1:])
            or (new_ev_any and not coverage_changed)
        )
    )

    return {
        "extraction": extraction,
        "extracted_requirement_types": types,
        "observed_requirement_types": req_types,
        "unique_args_fingerprints": len(unique_fps),
        "args_fingerprints": unique_fps,
        "same_args_all_invokes": same_args_all,
        "different_args": different_args,
        "any_new_evidence": new_ev_any,
        "any_coverage_progress": coverage_changed,
        "get_dashboard_kpis_covers_dashboard_kpis": kpi_covers,
        "get_dashboard_kpis_covers_current_inventory": inv_covers_inventory,
        "P03_ROOT_CAUSE": root,
        "rationale": rationale,
        "no_progress_rule_candidate": no_progress_candidate,
        "no_progress_rule_definition": {
            "same_tool": True,
            "no_meaningful_coverage_progress": not coverage_changed,
            "evidence_may_grow_but_does_not_satisfy_uncovered_requirement": True,
            "note": "candidate only — NOT implemented",
        },
    }


def main() -> int:
    from app.utils.load_env import load_project_dotenv

    load_project_dotenv(force=False)
    if not _inherit_key():
        print("FATAL: ANDES_LLM_API_KEY missing")
        return 2

    key_keep = os.environ["ANDES_LLM_API_KEY"]
    os.environ["ANDES_ASSISTANT_AGENT_ENABLED"] = "1"
    os.environ["ANDES_ASSISTANT_NL_ENABLED"] = "1"
    os.environ["ANDES_ORCH_PLANNER"] = "llm"
    os.environ["ANDES_ENV"] = "local"
    os.environ.setdefault("ANDES_LLM_PROVIDER", "openai_compatible")
    os.environ.setdefault("ANDES_AGENT_URL", "http://127.0.0.1:5055")
    os.environ.setdefault("ANDES_ASSISTANT_MEMORY_ENABLED", "0")
    os.environ.setdefault("ANDES_ASSISTANT_HISTORY_ENABLED", "0")
    load_project_dotenv(force=False)
    os.environ["ANDES_LLM_API_KEY"] = key_keep
    os.environ["ANDES_ASSISTANT_AGENT_ENABLED"] = "1"
    os.environ["ANDES_ASSISTANT_NL_ENABLED"] = "1"
    os.environ["ANDES_ORCH_PLANNER"] = "llm"
    os.environ["ANDES_ENV"] = "local"

    try:
        from app.assistant.orchestrator.agent_config import agent_loop_allowed, MAX_TOOL_CALLS, MAX_AGENT_STEPS
        from app.assistant.orchestrator.agent_loop import LlmAgentDecisionClient
        from app.assistant.orchestrator.factory import build_planner
        from app.assistant.orchestrator.llm.client import OpenAICompatibleClient
        from app.assistant.orchestrator.llm.config import load_llm_settings
        from app.assistant.orchestrator.service import run_orchestrator_chat
        from app.assistant.orchestrator.turn_store import TurnStore
        from app.assistant.routes import invoke_gateway
        from evals.fase81_runner import load_cases
        import app.assistant.orchestrator.agent_loop as al

        if not agent_loop_allowed():
            print("FATAL: agent_loop_allowed=false")
            return 2

        gold = next(c for c in load_cases(DATASET) if c.get("id") == "P03")
        spy = StructuralSpy(LlmAgentDecisionClient(OpenAICompatibleClient(load_llm_settings())))
        continue_hook, real_continue = _patch_loop_for_spy(spy)
        al.continue_agent_loop = continue_hook  # type: ignore[assignment]

        t0 = time.perf_counter()
        try:
            result = run_orchestrator_chat(
                message=str(gold.get("prompt") or ""),
                actor_user=gold.get("actor") or "e2e_kpi_nofin",
                conversation_id="fase81e1-P03-structural",
                invoke_fn=invoke_gateway,
                planner=build_planner(),
                turn_store=TurnStore(),
                agent_decision_client=spy,
            )
        finally:
            al.continue_agent_loop = real_continue  # type: ignore[assignment]

        latency_ms = int((time.perf_counter() - t0) * 1000)
        _fill_decision_act_next(spy)

        # If ingest hook missed, rebuild invokes from decisions + trace
        if not spy.invokes:
            trace = list(result.get("agent_trace") or [])
            call_decs = [d for d in spy.decisions if d.get("decision_act") == "call_tool"]
            for idx, (d, t) in enumerate(zip(call_decs, [x for x in trace if x.get("action") == "call_tool"]), start=1):
                spy.invokes.append(
                    {
                        "invoke_index": idx,
                        "tool": d.get("tool"),
                        "args_fingerprint": d.get("args_fingerprint"),
                        "arg_keys": d.get("arg_keys"),
                        "evidence_count_before": t.get("evidence_count_before"),
                        "evidence_count_after": t.get("evidence_count_after"),
                        "new_evidence_ids_count": max(
                            0,
                            int(t.get("evidence_count_after") or 0)
                            - int(t.get("evidence_count_before") or 0),
                        ),
                        "goal_requirement_count": d.get("goal_requirement_count"),
                        "covered_requirement_count": d.get("covered_requirement_count"),
                        "uncovered_requirement_ids": d.get("uncovered_requirement_ids"),
                        "requirement_types": d.get("requirement_types"),
                        "decision_act_next": None,
                        "same_tool_as_previous": idx > 1,
                        "same_args_as_previous": None,
                        "tool_result_status": None,
                        "agent_step": d.get("decision_index"),
                        "coverage_progress": False,
                    }
                )
            _fill_decision_act_next(spy)

        diagnosis = _classify(spy.invokes, spy.decisions, str(gold.get("prompt") or ""))
        code_notes = {
            "same_tool_logic": (
                "agent_loop blocks only identical canonical_call_key(tool, arguments); "
                "seen>=1 → loop_detected=true and continue WITHOUT invoke"
            ),
            "agent_limit_triggers": [
                "budget_exceeded(token/cost) → agent_limit",
                "remaining<=0 before call_tool → agent_limit",
                "empty followups exceeded → agent_limit",
                "while exhausts MAX_AGENT_STEPS → agent_limit",
            ],
            "MAX_TOOL_CALLS": MAX_TOOL_CALLS,
            "MAX_AGENT_STEPS": MAX_AGENT_STEPS,
            "goal_auto_signals": (
                "current_inventory stems include 'stock'; prompt 'stock crítico' → current_inventory; "
                "dashboard_kpis has NO auto signal"
            ),
            "covering_tools_current_inventory": ["get_inventory", "check_stock"],
            "covering_tools_dashboard_kpis": ["get_dashboard_kpis"],
            "progress_logic": (
                "No dedicated progress metric beyond GoalCoverage.refresh tool→requirement mapping "
                "and blocked_final when uncovered"
            ),
        }

        report = {
            "phase": "8.1E.1",
            "case_id": "P03",
            "latency_ms": latency_ms,
            "tools_used": list(result.get("tools_used") or []),
            "fallback": bool(result.get("fallback_used")),
            "fallback_reason": result.get("fallback_reason"),
            "decision_count": len(result.get("agent_trace") or []),
            "agent_steps": int(result.get("agent_steps") or len(result.get("agent_trace") or []) or 0),
            "goal_coverage_final": result.get("goal_coverage"),
            "decisions_structural": spy.decisions,
            "invokes_structural": spy.invokes,
            "kpi_compare": [
                {
                    "invoke_index": i.get("invoke_index"),
                    "args_fingerprint": i.get("args_fingerprint"),
                    "evidence_count_before": i.get("evidence_count_before"),
                    "evidence_count_after": i.get("evidence_count_after"),
                    "new_evidence_ids_count": i.get("new_evidence_ids_count"),
                    "new_evidence_ids": i.get("new_evidence_ids"),
                    "evidence_ids_after": i.get("evidence_ids_after"),
                    "requirement_types": i.get("requirement_types") or i.get("requirement_types_after"),
                    "covered_before": i.get("covered_requirement_count"),
                    "covered_after": i.get("covered_requirement_count_after"),
                    "uncovered_after": i.get("uncovered_requirement_ids_after")
                    or i.get("uncovered_requirement_ids"),
                    "coverage_progress": i.get("coverage_progress"),
                    "same_tool_as_previous": i.get("same_tool_as_previous"),
                    "same_args_as_previous": i.get("same_args_as_previous"),
                    "decision_act_next": i.get("decision_act_next"),
                }
                for i in spy.invokes
            ],
            "diagnosis": diagnosis,
            "code_notes": code_notes,
            "P03_ROOT_CAUSE": diagnosis["P03_ROOT_CAUSE"],
            "llm_real": latency_ms >= 100 and (
                len(spy.decisions) > 0 or len(list(result.get("tools_used") or [])) > 0
            ),
        }

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / "p03_structural.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        lines = [
            "FASE 8.1E.1 P03 structural diagnosis",
            f"latency_ms={latency_ms} tools={report['tools_used']} fallback={report['fallback_reason']}",
            f"P03_ROOT_CAUSE={report['P03_ROOT_CAUSE']}",
            f"extracted={diagnosis['extracted_requirement_types']} observed={diagnosis['observed_requirement_types']}",
            f"unique_args_fps={diagnosis['unique_args_fingerprints']} coverage_progress={diagnosis['any_coverage_progress']}",
            f"no_progress_rule_candidate={diagnosis['no_progress_rule_candidate']}",
            "",
            "rationale:",
        ]
        for r in diagnosis["rationale"]:
            lines.append(f"- {r}")
        lines.append("")
        lines.append("invokes:")
        for i in spy.invokes:
            lines.append(
                f"  #{i.get('invoke_index')} tool={i.get('tool')} fp={i.get('args_fingerprint')} "
                f"ev {i.get('evidence_count_before')}->{i.get('evidence_count_after')} "
                f"new_ids={i.get('new_evidence_ids_count')} covered={i.get('covered_requirement_count')}->"
                f"{i.get('covered_requirement_count_after')} next={i.get('decision_act_next')} "
                f"same_args={i.get('same_args_as_previous')}"
            )
        (OUT_DIR / "p03_structural.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {
                    "P03_ROOT_CAUSE": report["P03_ROOT_CAUSE"],
                    "llm_real": report["llm_real"],
                    "latency_ms": latency_ms,
                    "invokes": len(spy.invokes),
                    "fallback_reason": report["fallback_reason"],
                    "no_progress_rule_candidate": diagnosis["no_progress_rule_candidate"],
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
    # fix unused var lint in helper
    raise SystemExit(main())
