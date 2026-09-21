"""FASE 8.1A AgentLoop stability harness — real LLM, safe counts only.

10× P1 + 5 equivalent prompts. Not a release gate.
Restores AGENT=0 NL=0 ORCH=fake. Never prints secrets or raw evidence.
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

OUT_DIR = ROOT / "data" / "fase81a_stability"

P1 = "Muéstrame los movimientos del 2404 y dime cuánto stock queda actualmente."
EQUIV = [
    ("p1", P1),
    ("p2", "Revisa los movimientos del 2404 y después dime cuánto stock hay."),
    ("p3", "Quiero los movimientos del 2404 más el stock actual."),
    ("p4", "Consulta movimientos y stock actual del 2404."),
    ("p5", "¿Qué movimientos ha tenido el 2404 y cuántas unidades quedan?"),
]


class _PlannerSpy:
    def __init__(self, inner: Any):
        self.inner = inner
        self.calls = 0

    def plan(self, message: str, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls += 1
        return self.inner.plan(message, context=context)


def _scrub(text: str) -> str:
    key = os.environ.get("ANDES_LLM_API_KEY") or ""
    tok = os.environ.get("ANDES_AGENT_SERVICE_TOKEN") or ""
    out = text
    if key:
        out = out.replace(key, "[REDACTED_KEY]")
    if tok:
        out = out.replace(tok, "[REDACTED_TOKEN]")
    return out


def _safe_record(
    *,
    prompt_id: str,
    run_index: int,
    out: dict[str, Any],
    planner_calls: int,
    latency_ms: int,
) -> dict[str, Any]:
    trace = list(out.get("agent_trace") or [])
    tools = [str(t) for t in (out.get("tools_used") or []) if t]
    call_tools = [t.get("tool") for t in trace if t.get("action") == "call_tool"]
    d2 = next((t for t in trace if t.get("decision_index") == 2), None)
    blocked = [t for t in trace if t.get("blocked_final")]
    two_tools = "get_stock_movements" in tools and "get_inventory" in tools
    one_tool = len(set(tools)) == 1
    premature = bool(blocked) or (
        any(t.get("action") == "final_answer" for t in trace) and not two_tools and one_tool
    )
    return {
        "prompt_id": prompt_id,
        "run_index": run_index,
        "ok": bool(out.get("ok")),
        "tools": tools,
        "n_tools": len(tools),
        "n_call_tool": len(call_tools),
        "two_tools": two_tools,
        "one_tool": one_tool,
        "premature_final": premature,
        "blocked_final_count": len(blocked),
        "verifier_pass": bool(out.get("ok")) and not bool(out.get("fallback_used")),
        "fallback": bool(out.get("fallback_used")),
        "fallback_reason": out.get("fallback_reason"),
        "planner_plan_called": planner_calls > 0,
        "decision_2_evidence_before": (d2 or {}).get("evidence_count_before"),
        "decision_2_after_e1": int((d2 or {}).get("evidence_count_before") or 0) >= 1,
        "goal_coverage": out.get("goal_coverage"),
        "correlation_id": out.get("correlation_id"),
        "latency_ms": latency_ms,
        "trace": [
            {
                "decision_index": t.get("decision_index"),
                "action": t.get("action"),
                "tool": t.get("tool"),
                "evidence_count_before": t.get("evidence_count_before"),
                "evidence_count_after": t.get("evidence_count_after"),
                "blocked_final": t.get("blocked_final"),
                "goal_covered_count": t.get("goal_covered_count"),
                "goal_uncovered_count": t.get("goal_uncovered_count"),
            }
            for t in trace
        ],
    }


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def _count(pred) -> int:
        return sum(1 for r in rows if pred(r))

    two = _count(lambda r: r.get("two_tools"))
    one = _count(lambda r: r.get("one_tool") and not r.get("two_tools"))
    return {
        "n": len(rows),
        "two_tools": two,
        "one_tool": one,
        "premature_final": _count(lambda r: r.get("premature_final")),
        "verifier_pass": _count(lambda r: r.get("verifier_pass")),
        "fallback": _count(lambda r: r.get("fallback")),
        "planner_plan_called": _count(lambda r: r.get("planner_plan_called")),
        "decision_2_after_e1": _count(lambda r: r.get("decision_2_after_e1") and r.get("two_tools")),
        "goal_both_covered": _count(
            lambda r: {
                x.get("type"): x.get("status")
                for x in ((r.get("goal_coverage") or {}).get("requirements") or [])
            }.get("stock_movements")
            == "covered"
            and {
                x.get("type"): x.get("status")
                for x in ((r.get("goal_coverage") or {}).get("requirements") or [])
            }.get("current_inventory")
            == "covered"
        ),
    }


def main() -> int:
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

    prev = {
        "ANDES_ASSISTANT_AGENT_ENABLED": os.environ.get("ANDES_ASSISTANT_AGENT_ENABLED"),
        "ANDES_ASSISTANT_NL_ENABLED": os.environ.get("ANDES_ASSISTANT_NL_ENABLED"),
        "ANDES_ORCH_PLANNER": os.environ.get("ANDES_ORCH_PLANNER"),
        "ANDES_ENV": os.environ.get("ANDES_ENV"),
    }
    os.environ["ANDES_ASSISTANT_AGENT_ENABLED"] = "1"
    os.environ["ANDES_ASSISTANT_NL_ENABLED"] = "1"
    os.environ["ANDES_ORCH_PLANNER"] = "llm"
    os.environ["ANDES_ENV"] = "local"
    os.environ.setdefault("ANDES_LLM_PROVIDER", "openai_compatible")

    try:
        from app.assistant.orchestrator.agent_config import agent_loop_allowed
        from app.assistant.orchestrator.factory import build_planner
        from app.assistant.orchestrator.service import run_orchestrator_chat
        from app.assistant.orchestrator.turn_store import TurnStore
        from app.assistant.routes import invoke_gateway

        if not agent_loop_allowed():
            print("FATAL: agent_loop_allowed=false after overlay")
            return 2

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        runs_path = OUT_DIR / "runs.jsonl"
        summary_path = OUT_DIR / "summary.json"
        if runs_path.exists():
            runs_path.unlink()

        jobs: list[tuple[str, str, int]] = [("p1", P1, i) for i in range(1, 11)]
        jobs.extend((pid, text, 1) for pid, text in EQUIV)

        rows: list[dict[str, Any]] = []
        print(f"fase81a_stability jobs={len(jobs)}")
        for prompt_id, text, run_index in jobs:
            spy = _PlannerSpy(build_planner())
            t0 = time.perf_counter()
            out = run_orchestrator_chat(
                message=text,
                actor_user="albertadmin",
                conversation_id=f"fase81a-{prompt_id}-{run_index}",
                invoke_fn=invoke_gateway,
                planner=spy,
                turn_store=TurnStore(),
            )
            rec = _safe_record(
                prompt_id=prompt_id,
                run_index=run_index,
                out=out,
                planner_calls=spy.calls,
                latency_ms=int((time.perf_counter() - t0) * 1000),
            )
            rows.append(rec)
            line = _scrub(json.dumps(rec, ensure_ascii=False))
            with runs_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            print(
                json.dumps(
                    {
                        "prompt_id": prompt_id,
                        "run": run_index,
                        "two_tools": rec["two_tools"],
                        "n_tools": rec["n_tools"],
                        "premature_final": rec["premature_final"],
                        "blocked_final": rec["blocked_final_count"],
                        "verifier_pass": rec["verifier_pass"],
                        "fallback": rec["fallback"],
                        "planner_plan_called": rec["planner_plan_called"],
                        "d2_after_e1": rec["decision_2_after_e1"],
                        "goal": rec.get("goal_coverage"),
                    },
                    ensure_ascii=False,
                )
            )

        repeats = [r for r in rows if r["prompt_id"] == "p1" and r["run_index"] <= 10]
        # First 10 jobs are the repeats; the 11th is also p1 as equivalent #1.
        repeats = rows[:10]
        equiv = rows[10:]
        summary = {
            "repeats_p1": _summarize(repeats),
            "equivalent_5": _summarize(equiv),
            "all": _summarize(rows),
            "equivalent_detail": [
                {
                    "prompt_id": r["prompt_id"],
                    "two_tools": r["two_tools"],
                    "tools": r["tools"],
                    "premature_final": r["premature_final"],
                    "blocked_final_count": r["blocked_final_count"],
                    "verifier_pass": r["verifier_pass"],
                    "fallback": r["fallback"],
                    "planner_plan_called": r["planner_plan_called"],
                    "decision_2_after_e1": r["decision_2_after_e1"],
                    "goal_coverage": r.get("goal_coverage"),
                }
                for r in equiv
            ],
        }
        summary_path.write_text(
            _scrub(json.dumps(summary, ensure_ascii=False, indent=2)),
            encoding="utf-8",
        )
        print("SUMMARY", json.dumps(summary["repeats_p1"], ensure_ascii=False))
        print("OUT", str(OUT_DIR))
        return 0
    finally:
        os.environ["ANDES_ASSISTANT_AGENT_ENABLED"] = "0"
        os.environ["ANDES_ASSISTANT_NL_ENABLED"] = "0"
        os.environ["ANDES_ORCH_PLANNER"] = "fake"
        if prev["ANDES_ENV"] is not None:
            os.environ["ANDES_ENV"] = prev["ANDES_ENV"]
        print(
            "RESTORED",
            f"AGENT={os.environ.get('ANDES_ASSISTANT_AGENT_ENABLED')}",
            f"NL={os.environ.get('ANDES_ASSISTANT_NL_ENABLED')}",
            f"ORCH={os.environ.get('ANDES_ORCH_PLANNER')}",
        )


if __name__ == "__main__":
    raise SystemExit(main())
