"""FASE 8.1B evidence benchmark — real LLM AgentLoop. Not a release gate.

Restores AGENT=0 NL=0 ORCH=fake. Never prints secrets.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.fase81_scorer import aggregate, score_case  # noqa: E402

DATASET = ROOT / "evals" / "fase81_evidence_dataset.jsonl"
OUT_DIR = ROOT / "data" / "fase81_eval"


def load_cases(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rows.append(json.loads(line))
    return rows


def _scrub(text: str) -> str:
    key = os.environ.get("ANDES_LLM_API_KEY") or ""
    tok = os.environ.get("ANDES_AGENT_SERVICE_TOKEN") or ""
    out = text
    if key:
        out = out.replace(key, "[REDACTED_KEY]")
    if tok:
        out = out.replace(tok, "[REDACTED_TOKEN]")
    return out


class _PlannerSpy:
    def __init__(self, inner: Any):
        self.inner = inner
        self.calls = 0

    def plan(self, message: str, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls += 1
        return self.inner.plan(message, context=context)


def _safe_run(gold: dict[str, Any], result: dict[str, Any], *, latency_ms: int, planner_calls: int) -> dict[str, Any]:
    return {
        "id": gold.get("id"),
        "bucket": gold.get("bucket"),
        "ok": result.get("ok"),
        "tools_used": list(result.get("tools_used") or []),
        "fallback_used": bool(result.get("fallback_used")),
        "fallback_reason": result.get("fallback_reason"),
        "needs_clarification": bool(result.get("needs_clarification")),
        "scenario": result.get("scenario"),
        "reply": result.get("reply"),
        "correlation_id": result.get("correlation_id"),
        "agent_trace": result.get("agent_trace") or [],
        "goal_coverage": result.get("goal_coverage"),
        "arg_errors": result.get("arg_errors") or [],
        "verifier_failures": int(result.get("verifier_failures") or 0),
        "verifier_breakdown": result.get("verifier_breakdown") or {},
        "agent_progress": result.get("agent_progress") or {},
        "planner_plan_called": planner_calls > 0,
        "latency_ms": latency_ms,
        "cost_est": result.get("cost_est"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FASE 8.1B AgentLoop LLM benchmark")
    parser.add_argument("--dataset", default=str(DATASET))
    parser.add_argument("--ids", default="")
    parser.add_argument("--out-dir", default=str(OUT_DIR))
    args = parser.parse_args(argv)

    from app.utils.load_env import load_project_dotenv

    load_project_dotenv(force=False)
    os.environ.setdefault("ANDES_AGENT_URL", "http://127.0.0.1:5055")
    os.environ.setdefault("ANDES_ENV", "local")
    if not (os.environ.get("ANDES_AGENT_SERVICE_TOKEN") or "").strip():
        print("FATAL: ANDES_AGENT_SERVICE_TOKEN missing")
        return 2
    if not (os.environ.get("ANDES_LLM_API_KEY") or "").strip():
        print("FATAL: ANDES_LLM_API_KEY missing")
        return 2

    os.environ["ANDES_ASSISTANT_AGENT_ENABLED"] = "1"
    os.environ["ANDES_ASSISTANT_NL_ENABLED"] = "1"
    os.environ["ANDES_ORCH_PLANNER"] = "llm"
    os.environ["ANDES_ENV"] = "local"
    os.environ.setdefault("ANDES_LLM_PROVIDER", "openai_compatible")
    os.environ.setdefault("ANDES_ASSISTANT_MEMORY_ENABLED", "0")
    os.environ.setdefault("ANDES_ASSISTANT_HISTORY_ENABLED", "0")

    try:
        from app.assistant.orchestrator.agent_config import agent_loop_allowed
        from app.assistant.orchestrator.factory import build_planner
        from app.assistant.orchestrator.service import run_orchestrator_chat
        from app.assistant.orchestrator.turn_store import TurnStore
        from app.assistant.routes import invoke_gateway

        if not agent_loop_allowed():
            print("FATAL: agent_loop_allowed=false")
            return 2

        cases = load_cases(Path(args.dataset))
        wanted = {x.strip() for x in args.ids.split(",") if x.strip()}
        if wanted:
            cases = [c for c in cases if c.get("id") in wanted]
        if len(cases) != 64 and not wanted:
            print(f"WARN dataset_n={len(cases)} expected=64")

        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        runs_path = out_dir / "runs_llm.jsonl"
        scores_path = out_dir / "scores_llm.jsonl"
        summary_path = out_dir / "summary_llm.json"
        if runs_path.exists():
            runs_path.unlink()
        if scores_path.exists():
            scores_path.unlink()

        scores: list[dict[str, Any]] = []
        blocked_final = 0
        premature = 0
        print(f"fase81b mode=llm cases={len(cases)}")
        for gold in cases:
            spy = _PlannerSpy(build_planner())
            store = TurnStore()
            cid = f"fase81b-{gold.get('id')}"
            actor = gold.get("actor") or "albertadmin"
            t0 = time.perf_counter()
            for setup in gold.get("setup_turns") or []:
                run_orchestrator_chat(
                    message=str(setup.get("prompt") or ""),
                    actor_user=actor,
                    conversation_id=cid,
                    invoke_fn=invoke_gateway,
                    planner=spy,
                    turn_store=store,
                )
            result = run_orchestrator_chat(
                message=str(gold.get("prompt") or ""),
                actor_user=actor,
                conversation_id=cid,
                invoke_fn=invoke_gateway,
                planner=spy,
                turn_store=store,
            )
            rec = _safe_run(
                gold,
                result,
                latency_ms=int((time.perf_counter() - t0) * 1000),
                planner_calls=spy.calls,
            )
            scored = score_case(gold, rec)
            if any(t.get("blocked_final") for t in rec.get("agent_trace") or []):
                blocked_final += 1
            expected = list(gold.get("expected_tools") or gold.get("tools_expected") or [])
            got = list(rec.get("tools_used") or [])
            if len(expected) >= 2 and len(got) == 1 and not rec.get("fallback_used"):
                premature += 1
            scores.append(scored)
            with runs_path.open("a", encoding="utf-8") as fh:
                fh.write(_scrub(json.dumps(rec, ensure_ascii=False)) + "\n")
            with scores_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(scored, ensure_ascii=False) + "\n")
            print(
                json.dumps(
                    {
                        "id": scored["id"],
                        "pass": scored["pass"],
                        "tools": scored["tools_got"],
                        "fallback": scored.get("fallback_reason"),
                        "reasons": scored["reasons"],
                    },
                    ensure_ascii=False,
                )
            )

        agg = aggregate(scores)
        summary = {
            "mode": "llm",
            "gate": "not_ready",
            "phase": "8.1D",
            "aggregate": agg,
            "premature_final": premature,
            "blocked_final": blocked_final,
            "failed": [s["id"] for s in scores if not s.get("pass")],
            "agent_true_failures": [s["id"] for s in scores if s.get("agent_true_failure")],
            "evaluation_false_negatives": [
                s["id"] for s in scores if s.get("evaluation_false_negative")
            ],
        }
        summary_path.write_text(
            _scrub(json.dumps(summary, ensure_ascii=False, indent=2)),
            encoding="utf-8",
        )
        print(
            "SUMMARY",
            json.dumps(
                {
                    "pass_rate": agg["pass_rate"],
                    "failed": summary["failed"],
                    "agent_true_failures": summary["agent_true_failures"],
                    "evaluation_false_negatives": summary["evaluation_false_negatives"],
                    "invalid_args": agg["invalid_args"],
                },
                ensure_ascii=False,
            ),
        )
        print("OUT", str(out_dir))
        return 0
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


if __name__ == "__main__":
    raise SystemExit(main())
