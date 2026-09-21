"""FASE 8.1C — deterministic failure diagnosis for the 64-case AgentLoop benchmark.

Reads prior LLM runs (no prompts/secrets). Classifies A–F with closed rules.
Not a release gate. Restores AGENT=0 NL=0 ORCH=fake only if it re-runs cases.
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

from app.assistant.orchestrator.catalog import ALLOWED_TOOLS  # noqa: E402
from app.assistant.orchestrator.goal_coverage import REQUIREMENT_TYPES  # noqa: E402

DATASET = ROOT / "evals" / "fase81_evidence_dataset.jsonl"
RUNS_DEFAULT = ROOT / "data" / "fase81_eval" / "runs_llm.jsonl"
SCORES_DEFAULT = ROOT / "data" / "fase81_eval" / "scores_llm.jsonl"
OUT_JSON = ROOT / "data" / "fase81_eval" / "failure_diagnosis.json"
OUT_TXT = ROOT / "data" / "fase81_eval" / "failure_diagnosis.txt"

FAILURE_CLASSES = ("A", "B", "C", "D", "E", "F")
LIMIT_REASONS = frozenset(
    {
        "agent_limit",
        "agent_timeout",
        "timeout",
        "agent_cost",
        "cost",
        "budget",
        "agent_limit_exceeded",
    }
)
RESOLVER_SCENARIOS = frozenset(
    {
        "context_reuse",
        "context_ambiguous",
        "context_expired",
        "context_empty",
        "ambiguous",
    }
)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rows.append(json.loads(line))
    return rows


def _tools(run: dict[str, Any]) -> list[str]:
    return [str(t) for t in (run.get("tools_used") or []) if t]


def _req_snapshot(run: dict[str, Any], gold: dict[str, Any]) -> tuple[int, int, list[str]]:
    snap = run.get("goal_coverage") if isinstance(run.get("goal_coverage"), dict) else {}
    reqs = list(snap.get("requirements") or [])
    if not reqs and gold.get("goal_types"):
        # Gold declared goals but AgentLoop never ran / no snapshot.
        return len(gold["goal_types"]), 0, list(gold["goal_types"])
    uncovered = [
        str(r.get("type"))
        for r in reqs
        if str(r.get("status") or "") == "uncovered" and r.get("type")
    ]
    covered = sum(1 for r in reqs if str(r.get("status") or "") in {"covered", "impossible"})
    return len(reqs), covered, uncovered


def _covering_tools(rtype: str) -> frozenset[str]:
    spec = REQUIREMENT_TYPES.get(rtype) or {}
    tools = spec.get("tools") or frozenset()
    return tools if isinstance(tools, frozenset) else frozenset(tools)


def _trace_decisions(run: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in run.get("agent_trace") or []:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or "")[:40]
        if not action:
            continue
        out.append(
            {
                "decision_index": int(item.get("decision_index") or 0),
                "act": action,
                "tool": (str(item.get("tool"))[:64] if item.get("tool") else None),
                "blocked_final": bool(item.get("blocked_final")),
                "evidence_count_before": int(item.get("evidence_count_before") or 0),
                "evidence_count_after": int(item.get("evidence_count_after") or 0),
            }
        )
    return out


def _final_reason(run: dict[str, Any], decisions: list[dict[str, Any]]) -> str:
    if run.get("fallback_used"):
        return f"fallback:{run.get('fallback_reason') or 'unknown'}"
    scenario = str(run.get("scenario") or "")
    if scenario in RESOLVER_SCENARIOS or scenario.startswith("context_"):
        return f"resolver:{scenario}"
    if run.get("needs_clarification"):
        return "clarify"
    finals = [d for d in decisions if d.get("act") == "final_answer"]
    blocked = [d for d in decisions if d.get("blocked_final")]
    if blocked and finals:
        return "final_after_blocked"
    if finals:
        return "final_answer"
    if any(d.get("act") == "clarify" for d in decisions):
        return "clarify"
    if any(d.get("act") == "reject" for d in decisions):
        return "reject"
    if not decisions:
        return "no_agent_loop"
    return "incomplete"


def _tool_match_ok(gold: dict[str, Any], actual: list[str]) -> bool:
    expected = list(gold.get("tools_expected") or [])
    mode = gold.get("tool_count") or "exact"
    if gold.get("expect_clarify"):
        return True  # clarify path scored separately
    if gold.get("expect_reject"):
        return not actual
    if mode == "min":
        return set(expected).issubset(set(actual))
    return set(actual) == set(expected)


def _is_eval_mismatch(gold: dict[str, Any], run: dict[str, Any], actual: list[str]) -> bool:
    """Closed F rules: dataset/scorer expectations diverge from AgentLoop+Resolver reality."""
    scenario = str(run.get("scenario") or "")
    decisions = _trace_decisions(run)
    expected = list(gold.get("tools_expected") or [])
    mode = gold.get("tool_count") or "exact"
    goal_types = list(gold.get("goal_types") or [])
    _, covered_n, uncovered = _req_snapshot(run, gold)
    req_n, _, _ = _req_snapshot(run, gold)

    # Resolver short-circuit: AgentLoop never decided.
    if (not decisions) and (
        scenario in RESOLVER_SCENARIOS
        or scenario.startswith("context_")
        or bool(run.get("needs_clarification"))
    ):
        if expected or gold.get("expect_clarify"):
            return True

    # Gold demanded extra tools beyond declared goal_types, but goals are covered.
    if goal_types and req_n > 0 and covered_n == req_n and not uncovered:
        needed: set[str] = set()
        for gt in goal_types:
            needed |= set(_covering_tools(gt))
        if needed and set(actual) and set(actual).issubset(needed):
            # Agent only used tools that cover declared goals.
            if mode == "min" and not set(expected).issubset(set(actual)):
                missing = set(expected) - set(actual)
                # Missing tools are either outside goal families, or sibling cover tools
                # for an already-covered goal (e.g. check_stock vs get_inventory).
                for miss in missing:
                    if miss not in needed:
                        return True
                    for gt in goal_types:
                        covering = _covering_tools(gt)
                        if miss in covering and (covering & set(actual)):
                            return True
            if mode == "exact" and set(actual) != set(expected):
                if set(expected) - needed:
                    return True
                if set(actual).issubset(needed) and set(expected).issubset(needed):
                    return True

    # Exact gold but agent used a proper superset that still includes expected.
    if mode == "exact" and expected and set(expected).issubset(set(actual)) and set(actual) != set(expected):
        return True

    # Correct tool invoked; goal stays uncovered (empty/not_found) but gold required covered.
    if goal_types and actual:
        snap = run.get("goal_coverage") if isinstance(run.get("goal_coverage"), dict) else {}
        status = {r.get("type"): r.get("status") for r in (snap.get("requirements") or [])}
        for gt in goal_types:
            if status.get(gt) == "uncovered":
                covering = _covering_tools(gt)
                if covering & set(actual) and _tool_match_ok(
                    {**gold, "goal_types": []}, actual
                ):
                    return True

    # expect_clarify but AgentLoop answered without tools/clarify flag.
    if gold.get("expect_clarify") and not run.get("needs_clarification"):
        if not actual and any(d.get("act") == "final_answer" for d in decisions):
            return True

    return False


def _is_premature_final(gold: dict[str, Any], run: dict[str, Any], uncovered: list[str]) -> bool:
    """True premature final. blocked_final alone is NOT premature."""
    decisions = _trace_decisions(run)
    if not decisions:
        return False
    finals = [d for d in decisions if d.get("act") == "final_answer"]
    if not finals:
        return False
    last_final = finals[-1]
    # A blocked_final row is not itself premature; only an unblocked final with gaps.
    if last_final.get("blocked_final"):
        return False
    if uncovered:
        # If the only finals were blocked and then a later final after still-uncovered:
        # that later final is premature only if covering tools remain unused.
        return True
    expected = list(gold.get("tools_expected") or [])
    actual = _tools(run)
    if len(expected) >= 2 and len(set(actual)) == 1 and not run.get("fallback_used"):
        # Multi-tool gold, single tool then final — premature vs gold multi requirement.
        # Only if gold.goal_types also imply more than one covering family.
        goal_types = list(gold.get("goal_types") or [])
        families = {gt for gt in goal_types}
        if len(families) >= 2:
            return True
    return False


def _is_uncovered_with_available_tool(
    uncovered: list[str], actual: list[str], run: dict[str, Any]
) -> bool:
    if not uncovered:
        return False
    used = set(actual)
    for rtype in uncovered:
        covering = _covering_tools(rtype) & ALLOWED_TOOLS
        if covering and not (covering & used):
            # Agent stopped (final/clarify/fallback/no more steps) without trying covering tool.
            decisions = _trace_decisions(run)
            if any(d.get("act") in {"final_answer", "clarify", "reject", "fallback"} for d in decisions):
                return True
            if not decisions and run.get("needs_clarification"):
                return True
    return False


def classify_failure(gold: dict[str, Any], run: dict[str, Any]) -> str:
    """Deterministic A–F. No LLM. Priority: E > D > F > B > C > A."""
    actual = _tools(run)
    _, _, uncovered = _req_snapshot(run, gold)
    reason = str(run.get("fallback_reason") or "")

    if run.get("fallback_used") and reason in LIMIT_REASONS:
        return "E"
    if run.get("timeout") or reason in {"agent_timeout", "timeout"}:
        return "E"

    if int(run.get("verifier_failures") or 0) > 0 or reason == "agent_verifier_failed":
        return "D"

    if _is_eval_mismatch(gold, run, actual):
        return "F"

    if _is_premature_final(gold, run, uncovered):
        return "B"

    if _is_uncovered_with_available_tool(uncovered, actual, run):
        return "C"

    if not _tool_match_ok(gold, actual):
        return "A"

    if gold.get("expect_clarify") and not (
        run.get("needs_clarification") or str(run.get("scenario") or "") == "context_reuse"
    ):
        return "A"

    # Fallback / residual tool issues
    if not _tool_match_ok(gold, actual):
        return "A"
    return "A"


def diagnose_case(
    gold: dict[str, Any],
    run: dict[str, Any],
    score: dict[str, Any] | None = None,
) -> dict[str, Any]:
    actual = _tools(run)
    expected = list(gold.get("tools_expected") or [])
    req_n, covered_n, uncovered = _req_snapshot(run, gold)
    decisions = _trace_decisions(run)
    failure_class = classify_failure(gold, run)
    blocked = sum(1 for d in decisions if d.get("blocked_final"))
    premature = _is_premature_final(gold, run, uncovered)
    verifier_pass = int(run.get("verifier_failures") or 0) == 0 and str(
        run.get("fallback_reason") or ""
    ) != "agent_verifier_failed"

    safe_decisions = [
        {"decision_index": d["decision_index"], "act": d["act"], "tool": d["tool"]}
        for d in decisions
        if d.get("act") != "fallback"
    ]
    final = {"reason": _final_reason(run, decisions)}
    if decisions and decisions[-1].get("act") in {"final_answer", "clarify", "reject", "fallback"}:
        final["act"] = decisions[-1]["act"]
        final["tool"] = decisions[-1].get("tool")

    return {
        "case_id": gold.get("id") or run.get("id"),
        "bucket": gold.get("bucket") or run.get("bucket"),
        "expected_tools": expected,
        "actual_tools": actual,
        "expected_requirement_count": int(req_n if req_n else len(gold.get("goal_types") or [])),
        "covered_requirement_count": int(covered_n),
        "uncovered_requirements": uncovered,
        "decision_count": len(decisions),
        "decisions": safe_decisions,
        "final": final,
        "final_reason": final["reason"],
        "verifier_pass": bool(verifier_pass),
        "fallback": bool(run.get("fallback_used")),
        "fallback_reason": run.get("fallback_reason"),
        "failure_class": failure_class,
        "blocked_final_count": blocked,
        "premature_final": bool(premature),
        "agent_steps": int(run.get("agent_steps") or len(decisions) or 0),
        "tool_calls": len(actual),
        "latency_ms": int(run.get("latency_ms") or 0),
        "score_pass": None if score is None else bool(score.get("pass")),
        "score_reasons": list((score or {}).get("reasons") or [])[:12],
        "real_agent_issue": failure_class in {"A", "B", "C", "D", "E"},
        "eval_dataset_issue": failure_class == "F",
    }


def diagnose_failures(
    *,
    dataset: Path = DATASET,
    runs_path: Path = RUNS_DEFAULT,
    scores_path: Path = SCORES_DEFAULT,
    only_failed: bool = True,
) -> dict[str, Any]:
    gold_rows = load_jsonl(dataset)
    gold_by_id = {str(r.get("id")): r for r in gold_rows}
    runs = {str(r.get("id")): r for r in load_jsonl(runs_path)}
    scores = {str(s.get("id")): s for s in load_jsonl(scores_path)}

    failed_ids = [
        sid for sid, s in scores.items() if s.get("pass") is False
    ]
    if not failed_ids and only_failed:
        # Fall back to summary if scores missing pass flags
        failed_ids = sorted(runs.keys())

    target_ids = failed_ids if only_failed else sorted(gold_by_id.keys())
    cases: list[dict[str, Any]] = []
    for case_id in target_ids:
        gold = gold_by_id.get(case_id)
        run = runs.get(case_id)
        if not gold or not run:
            continue
        if only_failed and scores.get(case_id, {}).get("pass") is True:
            continue
        cases.append(diagnose_case(gold, run, scores.get(case_id)))

    dist = Counter(c["failure_class"] for c in cases)
    real_n = sum(1 for c in cases if c.get("real_agent_issue"))
    eval_n = sum(1 for c in cases if c.get("eval_dataset_issue"))
    top = dist.most_common(1)[0][0] if dist else None

    return {
        "n_failures": len(cases),
        "distribution": {k: int(dist.get(k, 0)) for k in FAILURE_CLASSES},
        "real_agent_issues": real_n,
        "eval_dataset_issues": eval_n,
        "dominant_class": top,
        "cases": cases,
    }


def format_text_summary(report: dict[str, Any]) -> str:
    lines = [
        "FASE 8.1C failure diagnosis (deterministic)",
        f"failures={report.get('n_failures')}",
        f"distribution={json.dumps(report.get('distribution'), ensure_ascii=False)}",
        f"real_agent_issues={report.get('real_agent_issues')}",
        f"eval_dataset_issues={report.get('eval_dataset_issues')}",
        f"dominant_class={report.get('dominant_class')}",
        "",
        "case_id | bucket | class | expected -> actual | final_reason | premature | blocked",
        "-" * 88,
    ]
    for c in report.get("cases") or []:
        lines.append(
            f"{c.get('case_id')} | {c.get('bucket')} | {c.get('failure_class')} | "
            f"{c.get('expected_tools')} -> {c.get('actual_tools')} | "
            f"{c.get('final_reason')} | premature={c.get('premature_final')} | "
            f"blocked={c.get('blocked_final_count')}"
        )
    lines.append("")
    lines.append(
        "Next minimal change (suggestion only; not implemented): "
        "tighten follow-up/context cases (F bucket) and gold exact/min labels "
        "where GoalCoverage only requires one family; then address premature "
        "multi-family finals (B/C) for product+stock and supplier+OC."
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FASE 8.1C deterministic failure diagnosis")
    parser.add_argument("--dataset", default=str(DATASET))
    parser.add_argument("--runs", default=str(RUNS_DEFAULT))
    parser.add_argument("--scores", default=str(SCORES_DEFAULT))
    parser.add_argument("--out-json", default=str(OUT_JSON))
    parser.add_argument("--out-txt", default=str(OUT_TXT))
    parser.add_argument(
        "--rerun-failures",
        action="store_true",
        help="Re-run failed cases with real LLM before diagnosing (slow).",
    )
    args = parser.parse_args(argv)

    if args.rerun_failures:
        # Optional refresh path; default diagnosis reads existing LLM runs.
        os.environ["ANDES_ASSISTANT_AGENT_ENABLED"] = "1"
        os.environ["ANDES_ASSISTANT_NL_ENABLED"] = "1"
        os.environ["ANDES_ORCH_PLANNER"] = "llm"
        os.environ.setdefault("ANDES_ENV", "local")
        try:
            from evals.fase81_runner import main as bench_main

            failed = []
            scores = load_jsonl(Path(args.scores))
            failed = [str(s.get("id")) for s in scores if not s.get("pass")]
            if failed:
                rc = bench_main(["--ids", ",".join(failed)])
                if rc not in (0, None):
                    print(f"WARN rerun exit={rc}")
        finally:
            os.environ["ANDES_ASSISTANT_AGENT_ENABLED"] = "0"
            os.environ["ANDES_ASSISTANT_NL_ENABLED"] = "0"
            os.environ["ANDES_ORCH_PLANNER"] = "fake"
            print(
                "RESTORED",
                f"AGENT={os.environ.get('ANDES_ASSISTANT_AGENT_ENABLED')}",
                f"NL={os.environ.get('ANDES_ASSISTANT_NL_ENABLED')}",
                f"ORCH={os.environ.get('ANDES_ORCH_PLANNER')}",
            )

    report = diagnose_failures(
        dataset=Path(args.dataset),
        runs_path=Path(args.runs),
        scores_path=Path(args.scores),
        only_failed=True,
    )
    out_json = Path(args.out_json)
    out_txt = Path(args.out_txt)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    text = format_text_summary(report)
    out_txt.write_text(text, encoding="utf-8")
    print(text)
    print("OUT", str(out_json))
    print("OUT", str(out_txt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
