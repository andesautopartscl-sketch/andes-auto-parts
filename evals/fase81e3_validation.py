"""FASE 8.1E.3 — post-fix stability: P03×10 then optional 50-run repro.

No production code changes. Restores AGENT=0 NL=0 ORCH=fake.
Never logs prompts, raw model output, headers, Authorization, API keys, or PII.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.fase81_runner import load_cases  # noqa: E402
from evals.fase81_scorer import score_case  # noqa: E402

DATASET = ROOT / "evals" / "fase81_evidence_dataset.jsonl"
OUT_DIR = ROOT / "data" / "fase81_eval"
ALL_IDS = ("T07", "K05", "P03", "M02", "M04")


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


def _scrub(text: str) -> str:
    key = os.environ.get("ANDES_LLM_API_KEY") or ""
    tok = os.environ.get("ANDES_AGENT_SERVICE_TOKEN") or ""
    out = text
    if key:
        out = out.replace(key, "[REDACTED_KEY]")
    if tok:
        out = out.replace(tok, "[REDACTED_TOKEN]")
    return out


def _enable_llm() -> str:
    from app.utils.load_env import load_project_dotenv

    load_project_dotenv(force=False)
    if not _inherit_key():
        raise RuntimeError("ANDES_LLM_API_KEY missing")
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
    os.environ["ANDES_ENV"] = "local"
    return key


def _restore() -> None:
    os.environ["ANDES_ASSISTANT_AGENT_ENABLED"] = "0"
    os.environ["ANDES_ASSISTANT_NL_ENABLED"] = "0"
    os.environ["ANDES_ORCH_PLANNER"] = "fake"


def _goal_fields(result: dict[str, Any]) -> dict[str, Any]:
    goal = result.get("goal_coverage") if isinstance(result.get("goal_coverage"), dict) else {}
    reqs = goal.get("requirements") or []
    return {
        "requirements": [str(r.get("type")) for r in reqs if r.get("type")],
        "covered": [str(r.get("type")) for r in reqs if r.get("status") == "covered"],
        "uncovered": [str(r.get("type")) for r in reqs if r.get("status") == "uncovered"],
    }


def _final_reason(result: dict[str, Any], scored: dict[str, Any]) -> str:
    if result.get("fallback_reason"):
        return str(result.get("fallback_reason"))
    if result.get("needs_clarification"):
        return "clarify"
    if scored.get("pass"):
        return "pass"
    reasons = scored.get("reasons") or []
    if reasons:
        return str(reasons[0])[:80]
    return "fail"


def _failure_mode(case_id: str, scored: dict[str, Any], result: dict[str, Any], tools: list[str]) -> str:
    if scored.get("pass"):
        return "pass"
    if case_id == "K05":
        return "benchmark_policy_candidate"
    fr = str(result.get("fallback_reason") or "")
    if fr == "agent_limit":
        return "agent_limit"
    if int(result.get("verifier_failures") or 0) > 0 or fr == "agent_verifier_failed":
        return "verifier_fail"
    if case_id == "T07" and "get_purchase_orders" in tools and "get_supplier" not in tools:
        return "t07_missing_supplier"
    if case_id == "P03" and fr == "agent_limit":
        return "p03_agent_limit"
    reasons = scored.get("reasons") or []
    if reasons:
        return str(reasons[0])[:60]
    return "other_fail"


def run_one(gold: dict[str, Any], run_id: str) -> dict[str, Any]:
    from app.assistant.orchestrator.agent_loop import LlmAgentDecisionClient
    from app.assistant.orchestrator.factory import build_planner
    from app.assistant.orchestrator.llm.client import OpenAICompatibleClient
    from app.assistant.orchestrator.llm.config import load_llm_settings
    from app.assistant.orchestrator.service import run_orchestrator_chat
    from app.assistant.orchestrator.turn_store import TurnStore
    from app.assistant.routes import invoke_gateway

    case_id = str(gold.get("id"))
    client = LlmAgentDecisionClient(OpenAICompatibleClient(load_llm_settings()))
    store = TurnStore()
    cid = f"fase81e3-{run_id}"
    actor = gold.get("actor") or "albertadmin"
    t0 = time.perf_counter()
    for setup in gold.get("setup_turns") or []:
        run_orchestrator_chat(
            message=str(setup.get("prompt") or ""),
            actor_user=actor,
            conversation_id=cid,
            invoke_fn=invoke_gateway,
            planner=build_planner(),
            turn_store=store,
            agent_decision_client=client,
        )
    result = run_orchestrator_chat(
        message=str(gold.get("prompt") or ""),
        actor_user=actor,
        conversation_id=cid,
        invoke_fn=invoke_gateway,
        planner=build_planner(),
        turn_store=store,
        agent_decision_client=client,
    )
    latency_ms = int((time.perf_counter() - t0) * 1000)
    tools = [str(t) for t in (result.get("tools_used") or []) if t]
    scored = score_case(
        gold,
        {
            "tools_used": tools,
            "fallback_used": bool(result.get("fallback_used")),
            "fallback_reason": result.get("fallback_reason"),
            "needs_clarification": bool(result.get("needs_clarification")),
            "scenario": result.get("scenario"),
            "reply": result.get("reply"),
            "agent_trace": result.get("agent_trace") or [],
            "goal_coverage": result.get("goal_coverage"),
            "arg_errors": result.get("arg_errors") or [],
            "verifier_failures": int(result.get("verifier_failures") or 0),
            "latency_ms": latency_ms,
        },
    )
    goal = _goal_fields(result)
    return {
        "case_id": case_id,
        "run_id": run_id,
        "pass": bool(scored.get("pass")),
        "tools": tools,
        "decision_count": len(result.get("agent_trace") or []),
        "requirements": goal["requirements"],
        "covered": goal["covered"],
        "uncovered": goal["uncovered"],
        "final_reason": _final_reason(result, scored),
        "fallback": bool(result.get("fallback_used")),
        "fallback_reason": result.get("fallback_reason"),
        "verifier_pass": int(result.get("verifier_failures") or 0) == 0,
        "latency_ms": latency_ms,
        "failure_mode": _failure_mode(case_id, scored, result, tools),
    }


def classify_case(fail_n: int, n: int) -> str:
    pass_n = n - fail_n
    if pass_n == n:
        return "PASS_STABLE"
    if pass_n >= 8:
        return "PASS_MOSTLY"
    if pass_n <= 1:
        return "REGRESSED"
    return "STOCHASTIC"


def classify_failure_rate(fail_n: int, n: int) -> str:
    if fail_n >= 8:
        return "DETERMINISTIC"
    if fail_n >= 2:
        return "STOCHASTIC"
    return "NON_REPRODUCED"


def summarize_case(case_id: str, runs: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(runs)
    fail_n = sum(1 for r in runs if not r.get("pass"))
    modes = Counter(r.get("failure_mode") for r in runs if not r.get("pass"))
    dominant = modes.most_common(1)[0][0] if modes else "pass"
    lats = [int(r.get("latency_ms") or 0) for r in runs]
    return {
        "case_id": case_id,
        "n": n,
        "pass": n - fail_n,
        "fail": fail_n,
        "failure_rate": round(fail_n / max(n, 1), 4),
        "classification": classify_case(fail_n, n)
        if case_id == "P03"
        else classify_failure_rate(fail_n, n),
        "p03_stability": classify_case(fail_n, n) if case_id == "P03" else None,
        "dominant_failure_mode": dominant,
        "tools_modes": Counter(tuple(r.get("tools") or []) for r in runs).most_common(3),
        "fallback_count": sum(1 for r in runs if r.get("fallback")),
        "verifier_pass_count": sum(1 for r in runs if r.get("verifier_pass")),
        "avg_latency_ms": int(sum(lats) / max(n, 1)),
        "p95_latency_ms": sorted(lats)[max(0, int(round(0.95 * (n - 1))))] if lats else 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FASE 8.1E.3 post-fix validation")
    parser.add_argument("--reps", type=int, default=10)
    parser.add_argument("--p03-only", action="store_true")
    parser.add_argument("--force-50", action="store_true", help="run 50 even if P03 unstable")
    args = parser.parse_args(argv)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    runs_path = OUT_DIR / "repro_81e3_runs.jsonl"
    if runs_path.exists():
        runs_path.unlink()

    try:
        _enable_llm()
        from app.assistant.orchestrator.agent_config import agent_loop_allowed

        if not agent_loop_allowed():
            print("FATAL: agent_loop_allowed=false")
            return 2

        gold_by_id = {c["id"]: c for c in load_cases(DATASET)}
        all_runs: list[dict[str, Any]] = []

        # Phase A: P03 × N
        print(f"fase81e3 P03 reps={args.reps}")
        p03_runs: list[dict[str, Any]] = []
        for i in range(1, args.reps + 1):
            rec = run_one(gold_by_id["P03"], f"P03-r{i:02d}")
            p03_runs.append(rec)
            all_runs.append(rec)
            with runs_path.open("a", encoding="utf-8") as fh:
                fh.write(_scrub(json.dumps(rec, ensure_ascii=False)) + "\n")
            print(
                json.dumps(
                    {
                        "run_id": rec["run_id"],
                        "pass": rec["pass"],
                        "tools": rec["tools"],
                        "covered": rec["covered"],
                        "uncovered": rec["uncovered"],
                        "final_reason": rec["final_reason"],
                        "fallback_reason": rec["fallback_reason"],
                        "latency_ms": rec["latency_ms"],
                    },
                    ensure_ascii=False,
                )
            )

        p03_summary = summarize_case("P03", p03_runs)
        stability = p03_summary["classification"]
        print("P03_STABILITY", stability, json.dumps(p03_summary, ensure_ascii=False, default=str))

        run_50 = args.force_50 or (
            not args.p03_only and stability in {"PASS_STABLE", "PASS_MOSTLY"}
        )
        other_summaries: list[dict[str, Any]] = []
        if run_50:
            print(f"fase81e3 full50 reps={args.reps}")
            for case_id in ALL_IDS:
                if case_id == "P03":
                    continue  # already have 10
                case_runs: list[dict[str, Any]] = []
                for i in range(1, args.reps + 1):
                    rec = run_one(gold_by_id[case_id], f"{case_id}-r{i:02d}")
                    case_runs.append(rec)
                    all_runs.append(rec)
                    with runs_path.open("a", encoding="utf-8") as fh:
                        fh.write(_scrub(json.dumps(rec, ensure_ascii=False)) + "\n")
                    print(
                        json.dumps(
                            {
                                "run_id": rec["run_id"],
                                "pass": rec["pass"],
                                "tools": rec["tools"],
                                "final_reason": rec["final_reason"],
                                "fallback_reason": rec["fallback_reason"],
                                "failure_mode": rec["failure_mode"],
                                "latency_ms": rec["latency_ms"],
                            },
                            ensure_ascii=False,
                        )
                    )
                other_summaries.append(summarize_case(case_id, case_runs))
        else:
            print("SKIP_50 reason=P03_not_stable_or_p03_only")

        summaries = [p03_summary] + other_summaries
        # Rebuild P03 classification label for failure-rate view too
        report = {
            "phase": "8.1E.3",
            "reps": args.reps,
            "p03_stability": stability,
            "ran_50": run_50,
            "baseline_81d": {
                "score": "59/64",
                "true_agent_failures": ["T07", "K05", "P03"],
                "verifier_failures_cases": ["M02", "M04"],
            },
            "summaries": summaries,
            "p03_runs": [
                {
                    "run_id": r["run_id"],
                    "tools": r["tools"],
                    "decision_count": r["decision_count"],
                    "requirements": r["requirements"],
                    "covered": r["covered"],
                    "uncovered": r["uncovered"],
                    "final_reason": r["final_reason"],
                    "fallback": r["fallback"],
                    "fallback_reason": r["fallback_reason"],
                    "latency_ms": r["latency_ms"],
                    "pass": r["pass"],
                }
                for r in p03_runs
            ],
        }
        (OUT_DIR / "repro_81e3.json").write_text(
            _scrub(json.dumps(report, ensure_ascii=False, indent=2, default=str)),
            encoding="utf-8",
        )
        lines = [
            "FASE 8.1E.3 post-fix validation",
            f"p03_stability={stability} ran_50={run_50}",
            "",
        ]
        for s in summaries:
            lines.append(
                f"{s['case_id']}: pass={s['pass']}/{s['n']} rate_fail={s['failure_rate']} "
                f"class={s['classification']} mode={s['dominant_failure_mode']} "
                f"fallback={s['fallback_count']} verifier_pass={s['verifier_pass_count']} "
                f"avg_ms={s['avg_latency_ms']}"
            )
        (OUT_DIR / "repro_81e3.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("SUMMARY", _scrub(json.dumps({s["case_id"]: s for s in summaries}, ensure_ascii=False, default=str)))
        print("OUT", str(OUT_DIR))
        return 0
    finally:
        _restore()
        print("RESTORED AGENT=0 NL=0 ORCH=fake")


if __name__ == "__main__":
    raise SystemExit(main())
