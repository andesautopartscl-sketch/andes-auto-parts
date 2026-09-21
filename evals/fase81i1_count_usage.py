"""FASE 8.1I.1 — does the model reach for `count`? Diagnostic only.

Runs the named cases with the real LLM and records, per decision, ONLY:

    case_id, decision_index, act, tool, calculation_present, calculation_op,
    calculation_input_count, calculation_result, requirements, covered,
    uncovered, verifier_pass, dropped_claims, dropped_draft, answer_replaced,
    final_reason, fallback

Prompts, raw model responses, claim text, evidence values, API keys, headers and
PII are never captured. The calculation is reduced to its op, its arity and its
numeric result; input PATHS are counted, never stored.

    python -m evals.fase81i1_count_usage                    # G03,R03,P04
    python -m evals.fase81i1_count_usage --ids G03 --runs 3

Writes data/fase81_eval/count_llm_diagnosis.{json,txt} and restores
AGENT=0 / NL=0 / ORCH=fake on exit.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EVAL = ROOT / "data" / "fase81_eval"
DEFAULT_IDS = "G03,R03,P04"

# A model never proposes a calculation | B schema rejects it | C verifier rejects
# it | D count runs but the claim leans on the wrong path | E other
CLASSES = {
    "A": "el modelo NO propone calculation",
    "B": "propone count pero el schema lo rechaza",
    "C": "propone count, el schema acepta, el verifier lo rechaza",
    "D": "count funciona pero el claim usa otro path/evidencia",
    "E": "otra",
}


def _restore() -> None:
    os.environ["ANDES_ASSISTANT_AGENT_ENABLED"] = "0"
    os.environ["ANDES_ASSISTANT_NL_ENABLED"] = "0"
    os.environ["ANDES_ORCH_PLANNER"] = "fake"


def _install_probe(records: list[dict[str, Any]], case_id: str):
    """Record the shape of every decision. Never the text."""
    from app.assistant.orchestrator import agent_loop as al

    original = al.validate_agent_decision
    counter = {"i": 0}

    def spy(raw: Any, *, user_message: str | None = None) -> dict[str, Any]:
        counter["i"] += 1
        index = counter["i"]
        raw_calcs = []
        if isinstance(raw, dict) and isinstance(raw.get("calculations"), list):
            raw_calcs = [c for c in raw["calculations"] if isinstance(c, dict)]
        rejected = None
        try:
            out = original(raw, user_message=user_message)
        except Exception as exc:  # noqa: BLE001 — record then re-raise
            rejected = getattr(exc, "code", None) or type(exc).__name__
            records.append(
                {
                    "case_id": case_id,
                    "decision_index": index,
                    "act": str((raw or {}).get("action") or "")[:20] if isinstance(raw, dict) else None,
                    "tool": (str((raw or {}).get("tool"))[:64] if isinstance(raw, dict) and raw.get("tool") else None),
                    "calculation_present": bool(raw_calcs),
                    "calculation_op": [str(c.get("op") or "")[:16] for c in raw_calcs],
                    "calculation_input_count": [len(c.get("inputs") or []) for c in raw_calcs],
                    "calculation_result": [c.get("result") for c in raw_calcs],
                    "schema_rejected": rejected,
                }
            )
            raise
        records.append(
            {
                "case_id": case_id,
                "decision_index": index,
                "act": str(out.get("action") or "")[:20],
                "tool": (str(out.get("tool"))[:64] if out.get("tool") else None),
                "calculation_present": bool(out.get("calculations")),
                "calculation_op": [str(c.get("op") or "")[:16] for c in (out.get("calculations") or [])],
                "calculation_input_count": [len(c.get("inputs") or []) for c in (out.get("calculations") or [])],
                "calculation_result": [c.get("result") for c in (out.get("calculations") or [])],
                "schema_rejected": None,
                "raw_calculation_present": bool(raw_calcs),
                "raw_calculation_op": [str(c.get("op") or "")[:16] for c in raw_calcs],
            }
        )
        return out

    al.validate_agent_decision = spy
    return original


def _classify(decisions: list[dict[str, Any]], run: dict[str, Any]) -> str:
    vb = run.get("verifier_breakdown") or {}
    used_count = any("count" in (d.get("calculation_op") or []) for d in decisions)
    proposed_raw = any("count" in (d.get("raw_calculation_op") or []) for d in decisions)
    schema_rejected = any(d.get("schema_rejected") for d in decisions)
    if not proposed_raw and not used_count:
        return "A"
    if proposed_raw and schema_rejected:
        return "B"
    if used_count and (vb.get("calc_mismatch") or vb.get("calc_unresolved") or vb.get("calc_error")):
        return "C"
    if used_count and vb.get("dropped_claims"):
        return "D"
    return "E"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FASE 8.1I.1 count usage diagnosis")
    parser.add_argument("--ids", default=DEFAULT_IDS)
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args(argv)

    from app.utils.load_env import load_project_dotenv

    load_project_dotenv(force=False)
    os.environ.setdefault("ANDES_AGENT_URL", "http://127.0.0.1:5055")
    os.environ.setdefault("ANDES_ENV", "local")
    os.environ.setdefault("ANDES_ASSISTANT_MEMORY_ENABLED", "0")
    os.environ.setdefault("ANDES_ASSISTANT_HISTORY_ENABLED", "0")
    if not (os.environ.get("ANDES_LLM_API_KEY") or "").strip():
        print("FATAL: ANDES_LLM_API_KEY missing")
        return 2
    if not (os.environ.get("ANDES_AGENT_SERVICE_TOKEN") or "").strip():
        print("FATAL: ANDES_AGENT_SERVICE_TOKEN missing")
        return 2

    os.environ["ANDES_ASSISTANT_AGENT_ENABLED"] = "1"
    os.environ["ANDES_ASSISTANT_NL_ENABLED"] = "1"
    os.environ["ANDES_ORCH_PLANNER"] = "llm"
    os.environ.setdefault("ANDES_LLM_PROVIDER", "openai_compatible")

    report: dict[str, Any] = {"phase": "8.1I.1", "classes": CLASSES, "cases": {}, "decisions": []}
    try:
        from app.assistant.orchestrator import agent_loop as al
        from app.assistant.orchestrator.agent_config import agent_loop_allowed
        from evals.fase81_runner import load_cases
        from evals.fase81g_closure import DATASET, _run_case

        if not agent_loop_allowed():
            print("FATAL: agent_loop_allowed=false")
            return 2
        gold = {c["id"]: c for c in load_cases(DATASET)}
        for cid in [x.strip() for x in args.ids.split(",") if x.strip()]:
            case = gold.get(cid)
            if case is None:
                print(f"{cid}: not in dataset")
                continue
            rows: list[dict[str, Any]] = []
            for attempt in range(max(1, args.runs)):
                decisions: list[dict[str, Any]] = []
                original = _install_probe(decisions, cid)
                try:
                    run = _run_case(case, f"i1-{attempt}")
                finally:
                    al.validate_agent_decision = original
                vb = run.get("verifier_breakdown") or {}
                pg = run.get("agent_progress") or {}
                trace = run.get("agent_trace") or []
                reqs = (run.get("goal_coverage") or {}).get("requirements") or []
                acts = [t.get("action") for t in trace
                        if t.get("action") and t.get("action") != "fallback"]
                # FASE 8.1I.4 — loop-level structure, so a diagnosis can tell a model
                # that never proposes a tool from a loop that refuses to run it.
                blocked = [t for t in trace if t.get("blocked_final")]
                summary = {
                    "case_id": cid,
                    "attempt": attempt,
                    "requirements": [q.get("type") for q in reqs],
                    "covered": [q.get("type") for q in reqs if q.get("status") == "covered"],
                    "uncovered": [q.get("type") for q in reqs if q.get("status") == "uncovered"],
                    "impossible": [q.get("type") for q in reqs if q.get("status") == "impossible"],
                    "goal_progress": {
                        "progress_events": pg.get("progress_events"),
                        "no_progress_steps": pg.get("no_progress_steps"),
                        "last_progress": pg.get("last_progress"),
                    },
                    "blocked_final": len(blocked),
                    "blocked_final_reason": ("goal_coverage_uncovered" if blocked else None),
                    "consecutive_blocked_finals": pg.get("consecutive_blocked_finals"),
                    "blocked_final_released": any(t.get("blocked_final_released") for t in trace),
                    "ledger_admitted": [t.get("admission") for t in trace if t.get("admission")],
                    "ledger_rejected": [t.get("admission") for t in trace
                                        if t.get("admission") in {"repeat_call",
                                                                  "tool_repeat_no_progress",
                                                                  "empty_repeat_no_progress"}],
                    "tools_proposed": [t.get("tool") for t in trace if t.get("tool")],
                    "verifier_status": ("PASS" if int(run.get("verifier_failures") or 0) == 0 else "FAIL"),
                    "verifier_failures": int(run.get("verifier_failures") or 0),
                    "verifier_pass": int(run.get("verifier_failures") or 0) == 0,
                    "dropped_claims": int(vb.get("dropped_claims") or 0),
                    "dropped_draft": int(vb.get("dropped_draft") or 0),
                    "answer_replaced": bool(vb.get("answer_replaced")),
                    "calc_unresolved": int(vb.get("calc_unresolved") or 0),
                    "calc_mismatch": int(vb.get("calc_mismatch") or 0),
                    "calc_error": int(vb.get("calc_error") or 0),
                    "final_reason": (f"fallback:{run.get('fallback_reason')}"
                                     if run.get("fallback_used") else (acts[-1] if acts else run.get("scenario"))),
                    "fallback": bool(run.get("fallback_used")),
                    "decisions": decisions,
                    "classification": _classify(decisions, run),
                }
                rows.append(summary)
                report["decisions"].extend(decisions)
                print(f"  {cid} attempt {attempt}: class={summary['classification']} "
                      f"ops={[d.get('calculation_op') for d in decisions]} "
                      f"drop={summary['dropped_claims']} verifier_pass={summary['verifier_pass']}")
            report["cases"][cid] = rows

        total = len(report["decisions"])
        with_calc = sum(1 for d in report["decisions"] if d.get("calculation_present"))
        with_count = sum(1 for d in report["decisions"] if "count" in (d.get("calculation_op") or []))
        finals = [d for d in report["decisions"] if d.get("act") == "final_answer"]
        report["usage"] = {
            "decisions_total": total,
            "final_answer_decisions": len(finals),
            "decisions_with_any_calculation": with_calc,
            "decisions_with_count": with_count,
            "pct_of_finals_using_count": (
                round(100.0 * sum(1 for d in finals if "count" in (d.get("calculation_op") or []))
                      / len(finals), 1) if finals else 0.0
            ),
            "class_distribution": dict(Counter(
                r["classification"] for rows in report["cases"].values() for r in rows
            )),
        }
        EVAL.mkdir(parents=True, exist_ok=True)
        (EVAL / "count_llm_diagnosis.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = [f"FASE 8.1I.1 — uso de count ({args.ids})", ""]
        for key, value in report["usage"].items():
            lines.append(f"  {key} = {value}")
        lines.append("")
        for cid, rows in report["cases"].items():
            for r in rows:
                lines.append(f"{cid} #{r['attempt']} class={r['classification']} "
                             f"verifier_pass={r['verifier_pass']} drop={r['dropped_claims']} "
                             f"final={r['final_reason']}")
                for d in r["decisions"]:
                    lines.append(f"    d{d['decision_index']} act={d['act']} tool={d['tool']} "
                                 f"calc={d['calculation_present']} ops={d['calculation_op']} "
                                 f"inputs={d['calculation_input_count']} results={d['calculation_result']} "
                                 f"schema_rejected={d.get('schema_rejected')}")
        (EVAL / "count_llm_diagnosis.txt").write_text("\n".join(lines), encoding="utf-8")
        print("wrote data/fase81_eval/count_llm_diagnosis.json")
        print("wrote data/fase81_eval/count_llm_diagnosis.txt")
    finally:
        _restore()
        print(f"RESTORED AGENT={os.environ.get('ANDES_ASSISTANT_AGENT_ENABLED')} "
              f"NL={os.environ.get('ANDES_ASSISTANT_NL_ENABLED')} "
              f"ORCH={os.environ.get('ANDES_ORCH_PLANNER')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
