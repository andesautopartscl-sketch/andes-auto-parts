"""FASE 8.1H.2 — why did the verifier drop a claim? Diagnostic only.

Runs the named cases once each with the real LLM and prints, for every claim the
verifier discarded, ONLY these fields:

    case_id, claim_type, numeric_tokens, date_tokens,
    grounded_numbers, grounded_dates, drop_reason

Nothing is written to disk. Prompts, raw model responses, claim text, API keys,
headers and PII are never captured: the claim is reduced to the figures and dates
it asserts before anything is printed.

    python -m evals.fase81h2_dropped_claims                # T05,T08
    python -m evals.fase81h2_dropped_claims --ids C04,T05

Restores AGENT=0 / NL=0 / ORCH=fake on exit.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_IDS = "T05,T08"
MAX_SET_PRINT = 40


def _restore() -> None:
    os.environ["ANDES_ASSISTANT_AGENT_ENABLED"] = "0"
    os.environ["ANDES_ASSISTANT_NL_ENABLED"] = "0"
    os.environ["ANDES_ORCH_PLANNER"] = "fake"


def _drop_reason(
    text: str,
    blob: str,
    tool_names: set[str],
    numbers: set[str],
    dates: set[str],
) -> tuple[str, list[str], list[str]]:
    """Recompute the checks in order and name the first that fails.

    Returns (reason, ungrounded numeric tokens, ungrounded date tokens). Only
    derived facts leave this function — never the claim text.
    """
    from app.assistant.orchestrator import answer_verifier as av

    upper_blob = blob.upper()
    for token in av._CODE_RE.findall((text or "").upper()):
        if token in tool_names:
            continue
        if token not in upper_blob:
            return "code_not_in_evidence", [], []

    text_wo_dates, claim_dates = av.mask_dates(text or "")
    bad_dates = [d for d in claim_dates if not av._date_grounded(d, dates)]
    if bad_dates:
        return "date_not_in_evidence", [], bad_dates

    bad_nums = [
        t for t in av._claim_number_tokens(text_wo_dates)
        if av._norm_number(t) not in numbers
    ]
    if bad_nums:
        return "number_not_grounded", bad_nums, []

    for name in av.ALLOWED_TOOLS:
        if name in (text or "") and name not in blob and name.upper() not in tool_names:
            return "tool_not_in_evidence", [], []
    return "other", [], []


def _install_probe(records: list[dict[str, Any]], case_id: str):
    """Wrap _claim_grounded so every rejection is explained. No persistence."""
    from app.assistant.orchestrator import answer_verifier as av

    original = av._claim_grounded

    def spy(text, blob, tool_names, numbers=None, dates=None):
        ok = original(text, blob, tool_names, numbers, dates)
        if not ok:
            nums = numbers or set()
            dts = dates or set()
            text_wo_dates, claim_dates = av.mask_dates(text or "")
            reason, bad_nums, bad_dates = _drop_reason(text, blob, tool_names, nums, dts)
            records.append(
                {
                    "case_id": case_id,
                    "claim_type": "inferencia" if "INFEREN" in (text or "").upper() else "dato",
                    "numeric_tokens": av._claim_number_tokens(text_wo_dates),
                    "date_tokens": claim_dates,
                    "ungrounded_numeric_tokens": bad_nums,
                    "ungrounded_date_tokens": bad_dates,
                    "grounded_numbers": sorted(nums, key=lambda x: (len(x), x))[:MAX_SET_PRINT],
                    "grounded_dates": sorted(dts)[:MAX_SET_PRINT],
                    "drop_reason": reason,
                }
            )
        return ok

    av._claim_grounded = spy
    return original


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FASE 8.1H.2 dropped-claim diagnosis")
    parser.add_argument("--ids", default=DEFAULT_IDS)
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

    try:
        from app.assistant.orchestrator import answer_verifier as av
        from app.assistant.orchestrator.agent_config import agent_loop_allowed
        from evals.fase81_runner import load_cases
        from evals.fase81g_closure import DATASET, _run_case

        if not agent_loop_allowed():
            print("FATAL: agent_loop_allowed=false")
            return 2

        gold = {c["id"]: c for c in load_cases(DATASET)}
        wanted = [x.strip() for x in args.ids.split(",") if x.strip()]
        for cid in wanted:
            case = gold.get(cid)
            if case is None:
                print(f"{cid}: not in dataset")
                continue
            records: list[dict[str, Any]] = []
            original = _install_probe(records, cid)
            try:
                run = _run_case(case, "h2diag")
            finally:
                av._claim_grounded = original
            vb = run.get("verifier_breakdown") or {}
            print("=" * 72)
            print(f"{cid}  tools={run['tools_used']}  fallback={run['fallback_reason']}")
            print(f"  verifier_failures={run.get('verifier_failures')} "
                  f"dropped_claims={vb.get('dropped_claims', 0)} "
                  f"dropped_draft={vb.get('dropped_draft', 0)} "
                  f"answer_replaced={vb.get('answer_replaced', False)} "
                  f"calc_unresolved={vb.get('calc_unresolved', 0)} "
                  f"calc_mismatch={vb.get('calc_mismatch', 0)}")
            if not records:
                print("  no dropped claims")
            for rec in records:
                print("  --- dropped claim")
                for key in (
                    "case_id", "claim_type", "numeric_tokens", "date_tokens",
                    "ungrounded_numeric_tokens", "ungrounded_date_tokens",
                    "grounded_numbers", "grounded_dates", "drop_reason",
                ):
                    print(f"      {key} = {rec[key]}")
            movement_words = ("movimiento", "ingreso", "salida", "kardex")
            reply = (run.get("reply") or "").lower()
            print(f"  movements block present in reply: "
                  f"{any(w in reply for w in movement_words)}")
    finally:
        _restore()
        print(f"RESTORED AGENT={os.environ.get('ANDES_ASSISTANT_AGENT_ENABLED')} "
              f"NL={os.environ.get('ANDES_ASSISTANT_NL_ENABLED')} "
              f"ORCH={os.environ.get('ANDES_ORCH_PLANNER')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
