"""FASE 4 NL evaluation runner — local LLM or fake planner.

Usage:
  ANDES_ENV=local ANDES_ASSISTANT_NL_ENABLED=1 ANDES_ORCH_PLANNER=llm \\
    python -m evals.fase4_runner --mode llm

  python -m evals.fase4_runner --mode fake

Never prints API keys. Restores NL=0 / ORCH=fake on exit when --mode llm.
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

from evals.fase4_scorer import aggregate, beta_gate, score_case  # noqa: E402

DATASET = ROOT / "evals" / "fase4_nl_dataset.jsonl"
OUT_DIR = ROOT / "data" / "fase4_eval"


def _load_dataset(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rows.append(json.loads(line))
    return rows


def _ingest_key_file() -> None:
    path = (os.environ.get("ANDES_LLM_API_KEY_FILE") or "").strip()
    if not path:
        return
    p = Path(path)
    if not p.is_file():
        return
    try:
        raw = p.read_text(encoding="utf-8", errors="replace").strip()
        if raw and not (os.environ.get("ANDES_LLM_API_KEY") or "").strip():
            os.environ["ANDES_LLM_API_KEY"] = raw
    finally:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def _scrub(text: str) -> str:
    key = os.environ.get("ANDES_LLM_API_KEY") or ""
    tok = os.environ.get("ANDES_AGENT_SERVICE_TOKEN") or ""
    out = text
    if key:
        out = out.replace(key, "[REDACTED_KEY]")
    if tok:
        out = out.replace(tok, "[REDACTED_TOKEN]")
    return out


def _build_planner(mode: str):
    if mode == "fake":
        from app.assistant.orchestrator.planner import FakePlanner

        return FakePlanner()

    from app.assistant.orchestrator.llm.client import OpenAICompatibleClient
    from app.assistant.orchestrator.llm.config import load_llm_settings
    from app.assistant.orchestrator.planner_llm import LlmPlanner

    settings = load_llm_settings()
    if not settings.soft_llm_allowed:
        raise RuntimeError(
            "soft_llm_allowed=False — need NL=1, ORCH=llm, ANDES_ENV=local|staging, API key"
        )
    return LlmPlanner(OpenAICompatibleClient(settings))


def _run_one(*, gold: dict[str, Any], planner, invoke_gateway) -> dict[str, Any]:
    from app.assistant.orchestrator.service import run_orchestrator_chat

    plans: list[dict[str, Any]] = []
    gw: list[dict[str, Any]] = []
    original = planner.plan
    llm_calls = 0

    def capture(message: str, *, context=None):
        nonlocal llm_calls
        llm_calls += 1
        raw = original(message, context=context)
        plans.append(dict(raw) if isinstance(raw, dict) else {"_raw": raw})
        return raw

    planner.plan = capture  # type: ignore[method-assign]
    t0 = time.perf_counter()
    t_llm = 0

    def invoke(payload: dict[str, Any]):
        status, body = invoke_gateway(payload)
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        money = {k: data.get(k) for k in ("ventas_periodo", "ventas_hoy", "ventas_mes") if k in data}
        gw.append(
            {
                "tool": payload.get("tool"),
                "arguments": payload.get("arguments"),
                "http_status": status,
                "ok": body.get("ok"),
                "write": body.get("write"),
                "error_code": body.get("error_code"),
                "money_fields": money,
                "empty": bool(
                    isinstance(data.get("items"), list) and len(data.get("items") or []) == 0
                )
                if "items" in data
                else None,
            }
        )
        return status, body

    try:
        t_llm0 = time.perf_counter()
        result = run_orchestrator_chat(
            message=gold["prompt"],
            actor_user=gold.get("actor") or "albertadmin",
            conversation_id=f"fase4-{gold.get('id')}",
            invoke_fn=invoke,
            planner=planner,
        )
        t_llm = int((time.perf_counter() - t_llm0) * 1000)
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "error_code": "eval_exception", "message": _scrub(str(exc))[:240]}
        t_llm = int((time.perf_counter() - t0) * 1000)
    finally:
        planner.plan = original  # type: ignore[method-assign]

    total_ms = int((time.perf_counter() - t0) * 1000)
    final = plans[-1] if plans else {}
    steps = final.get("steps") if isinstance(final.get("steps"), list) else []
    tools = [str(s.get("tool")) for s in steps if isinstance(s, dict) and s.get("tool")]
    if not tools and result.get("tools_used"):
        tools = list(result.get("tools_used") or [])

    return {
        "id": gold.get("id"),
        "prompt": gold.get("prompt"),
        "actor": gold.get("actor"),
        "ok": result.get("ok"),
        "error_code": result.get("error_code"),
        "reply": result.get("reply") or result.get("message"),
        "needs_clarification": result.get("needs_clarification")
        or bool(final.get("needs_clarification")),
        "scenario": result.get("scenario") or final.get("scenario"),
        "grounded": result.get("grounded"),
        "tools_in_plan": tools,
        "tools_used": result.get("tools_used"),
        "steps": steps,
        "gateway_invokes": gw,
        "replan": len(plans) > 1,
        "llm_calls": llm_calls,
        "llm_latency_ms": t_llm,
        "latency_total_ms": total_ms,
        "correlation_id": result.get("correlation_id"),
        "final_plan": {k: final.get(k) for k in ("plan_id", "scenario", "needs_clarification", "reject")},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FASE 4 NL eval runner")
    parser.add_argument("--mode", choices=("llm", "fake"), default="fake")
    parser.add_argument("--dataset", default=str(DATASET))
    parser.add_argument("--ids", default="", help="Comma-separated case ids (optional)")
    parser.add_argument("--out-dir", default=str(OUT_DIR))
    args = parser.parse_args(argv)

    # Align with ERP/Gateway local config (.env). Never print token values.
    from app.utils.load_env import load_project_dotenv

    load_project_dotenv(force=False)
    os.environ.setdefault("ANDES_AGENT_URL", "http://127.0.0.1:5055")
    os.environ.setdefault("ANDES_ENV", "local")
    if not (os.environ.get("ANDES_AGENT_SERVICE_TOKEN") or "").strip():
        print(
            "FATAL: ANDES_AGENT_SERVICE_TOKEN missing — invoke_gateway returns "
            "agent_misconfigured. Set the same M2M token used by Gateway :5055 "
            "(e.g. in .env) before running eval."
        )
        return 2

    prev_nl = os.environ.get("ANDES_ASSISTANT_NL_ENABLED")
    prev_orch = os.environ.get("ANDES_ORCH_PLANNER")
    prev_env = os.environ.get("ANDES_ENV")

    try:
        if args.mode == "llm":
            _ingest_key_file()
            os.environ["ANDES_ENV"] = (os.environ.get("ANDES_ENV") or "local").strip() or "local"
            os.environ["ANDES_ASSISTANT_NL_ENABLED"] = "1"
            os.environ["ANDES_ORCH_PLANNER"] = "llm"
            os.environ.setdefault("ANDES_LLM_PROVIDER", "openai_compatible")
        else:
            os.environ["ANDES_ASSISTANT_NL_ENABLED"] = "0"
            os.environ["ANDES_ORCH_PLANNER"] = "fake"

        from app.assistant.routes import invoke_gateway

        planner = _build_planner(args.mode)
        cases = _load_dataset(Path(args.dataset))
        wanted = {x.strip() for x in args.ids.split(",") if x.strip()}
        if wanted:
            cases = [c for c in cases if c.get("id") in wanted]

        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        runs_path = out_dir / f"runs_{args.mode}.jsonl"
        scores_path = out_dir / f"scores_{args.mode}.jsonl"
        summary_path = out_dir / f"summary_{args.mode}.json"

        if runs_path.exists():
            runs_path.unlink()
        if scores_path.exists():
            scores_path.unlink()

        scores: list[dict[str, Any]] = []
        print(f"FASE4 mode={args.mode} cases={len(cases)}")
        for gold in cases:
            print(f"--- {gold.get('id')} ---")
            run = _run_one(gold=gold, planner=planner, invoke_gateway=invoke_gateway)
            scored = score_case(gold, run)
            scores.append(scored)
            line_run = _scrub(json.dumps(run, ensure_ascii=False))
            line_score = json.dumps(scored, ensure_ascii=False)
            with runs_path.open("a", encoding="utf-8") as fh:
                fh.write(line_run + "\n")
            with scores_path.open("a", encoding="utf-8") as fh:
                fh.write(line_score + "\n")
            print(
                json.dumps(
                    {
                        "id": scored["id"],
                        "pass": scored["pass"],
                        "tools": scored["tools_got"],
                        "reasons": scored["reasons"],
                        "latency_ms": scored["latency_total_ms"],
                    },
                    ensure_ascii=False,
                )
            )

        agg = aggregate(scores)
        gate = beta_gate(agg, scores)
        summary = {
            "mode": args.mode,
            "aggregate": agg,
            "beta_gate": gate,
            "failed": [s["id"] for s in scores if not s.get("pass")],
        }
        summary_path.write_text(_scrub(json.dumps(summary, ensure_ascii=False, indent=2)), encoding="utf-8")
        print("SUMMARY", json.dumps({"pass_rate": agg["pass_rate"], "go": gate["go"], "failed": summary["failed"]}, ensure_ascii=False))
        print("OUT", str(out_dir))
        return 0 if args.mode == "fake" else (0 if gate["go"] else 1)
    finally:
        # Always restore safe defaults after llm mode
        if args.mode == "llm":
            os.environ["ANDES_ASSISTANT_NL_ENABLED"] = "0"
            os.environ["ANDES_ORCH_PLANNER"] = "fake"
            os.environ.pop("ANDES_LLM_API_KEY", None)
        else:
            if prev_nl is not None:
                os.environ["ANDES_ASSISTANT_NL_ENABLED"] = prev_nl
            if prev_orch is not None:
                os.environ["ANDES_ORCH_PLANNER"] = prev_orch
            if prev_env is not None:
                os.environ["ANDES_ENV"] = prev_env
        print(
            "RESTORED",
            f"NL={os.environ.get('ANDES_ASSISTANT_NL_ENABLED')}",
            f"ORCH={os.environ.get('ANDES_ORCH_PLANNER')}",
        )


if __name__ == "__main__":
    raise SystemExit(main())
