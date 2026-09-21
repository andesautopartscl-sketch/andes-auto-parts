"""FASE 8.1E — reproducibility of real agent failures (LLM).

No production behavior changes. Restores AGENT=0 NL=0 ORCH=fake.
Never logs prompts, raw model text, headers, Authorization, API keys, or PII.
"""
from __future__ import annotations

import argparse
import hashlib
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
TARGET_IDS = ("T07", "K05", "P03", "M02", "M04")
DEFAULT_REPS = 10


def _scrub(text: str) -> str:
    key = os.environ.get("ANDES_LLM_API_KEY") or ""
    tok = os.environ.get("ANDES_AGENT_SERVICE_TOKEN") or ""
    out = text
    if key:
        out = out.replace(key, "[REDACTED_KEY]")
    if tok:
        out = out.replace(tok, "[REDACTED_TOKEN]")
    return out


def _args_fingerprint(arguments: Any) -> str:
    payload = json.dumps(arguments or {}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _arg_keys(arguments: Any) -> list[str]:
    if not isinstance(arguments, dict):
        return []
    return sorted(str(k) for k in arguments.keys())


class _DecisionSpy:
    """Wraps decision client; keeps only structural fields (no free text)."""

    def __init__(self, inner: Any):
        self.inner = inner
        self.last_usage: dict[str, int] | None = None
        self.events: list[dict[str, Any]] = []

    def complete_decision(self, *, system: str, user: str) -> dict[str, Any]:
        raw = self.inner.complete_decision(system=system, user=user)
        self.last_usage = getattr(self.inner, "last_usage", None)
        action = str((raw or {}).get("action") or "")[:40]
        tool = (raw or {}).get("tool")
        args = (raw or {}).get("arguments") if isinstance((raw or {}).get("arguments"), dict) else {}
        proposed = (raw or {}).get("proposed_requirements") or []
        prop_types: list[str] = []
        if isinstance(proposed, list):
            for item in proposed:
                if isinstance(item, dict) and item.get("type"):
                    prop_types.append(str(item.get("type"))[:64])
                elif isinstance(item, str):
                    prop_types.append(item[:64])
        claims = (raw or {}).get("claims") or []
        calcs = (raw or {}).get("calculations") or []
        self.events.append(
            {
                "action": action or None,
                "tool": (str(tool)[:64] if tool else None),
                "args_fp": _args_fingerprint(args) if action == "call_tool" else None,
                "arg_keys": _arg_keys(args) if action == "call_tool" else [],
                "proposed_requirement_types": prop_types[:8],
                "claims_count": len(claims) if isinstance(claims, list) else 0,
                "calculations_count": len(calcs) if isinstance(calcs, list) else 0,
                "has_draft_reply": bool(str((raw or {}).get("draft_reply") or "").strip()),
            }
        )
        return raw


class _VerifierProbe:
    """Classifies verifier failure modes without storing claim/reply text."""

    def __init__(self) -> None:
        self.last: dict[str, Any] | None = None

    def install(self) -> Any:
        import app.assistant.orchestrator.answer_verifier as av
        import app.assistant.orchestrator.agent_loop as al

        probe = self
        original = av.verify_agent_answer

        def wrapped(*, store, decision, raw_evidence=None, plan=None):
            failures = 0
            modes: list[str] = []
            calc_ok: list[dict[str, Any]] = []
            for calc in decision.get("calculations") or []:
                try:
                    recomputed = av.recompute_calculation(store, calc)
                except Exception:
                    failures += 1
                    modes.append("calculation_recompute_error")
                    continue
                claimed = float(calc.get("result") or 0)
                if abs(recomputed - claimed) > max(av._EPS, abs(recomputed) * 1e-6):
                    failures += 1
                    modes.append("calculation_mismatch")
                    continue
                calc_ok.append({**calc, "result": recomputed})

            blob = av._evidence_blob(store, [c.get("result") for c in calc_ok])
            tool_names = {str(i.tool or "").upper() for i in store.items}
            datos: list[str] = []
            inferencias: list[str] = []
            claim_ungrounded = 0
            for claim in decision.get("claims") or []:
                text = str(claim.get("text") or "").strip()
                if not text:
                    continue
                kind = claim.get("kind")
                if kind == "inferencia" and av._NUM_RE.search(text):
                    if not av._claim_grounded(text, blob, tool_names):
                        failures += 1
                        claim_ungrounded += 1
                        modes.append("claim_ungrounded_inferencia_number")
                        continue
                    inferencias.append(text)
                    continue
                if not av._claim_grounded(text, blob, tool_names):
                    failures += 1
                    claim_ungrounded += 1
                    modes.append("claim_ungrounded")
                    continue
                if kind == "dato":
                    datos.append(text)
                else:
                    inferencias.append(text)

            draft = str(decision.get("draft_reply") or "").strip()
            if draft and not (datos or inferencias):
                if av._claim_grounded(draft, blob, tool_names):
                    datos.append(draft)
                else:
                    failures += 1
                    modes.append("draft_ungrounded")

            parts: list[str] = []
            if datos:
                parts.append("DATOS:")
                parts.extend(f"- {line}" for line in datos)
            if inferencias:
                parts.append("INFERENCIA:")
                parts.extend(f"- {line}" for line in inferencias)
            reply = "\n".join(parts).strip()

            composer_evidence = raw_evidence if raw_evidence else av._composer_evidence(store)
            used_composer = False
            if not reply:
                failures = max(failures, 1)
                modes.append("empty_reply_composer_fallback")
                used_composer = True
            elif av._finance_null_zero(reply, store):
                failures += 1
                modes.append("finance_null_as_zero")
                used_composer = True
            else:
                reply = av._scrub_leaked_pii(reply, composer_evidence)
                if not av.reply_contains_only_evidence_values(reply, composer_evidence):
                    calc_blob = blob
                    if not av._claim_grounded(reply, calc_blob, tool_names):
                        failures += 1
                        modes.append("reply_values_not_in_evidence")
                        used_composer = True

            # Evidence structural signals (no payload).
            evidence_ids = [str(i.evidence_id) for i in store.items]
            probe.last = {
                "failures": failures,
                "modes": modes,
                "claim_ungrounded_count": claim_ungrounded,
                "claims_count": len(decision.get("claims") or []),
                "calculations_count": len(decision.get("calculations") or []),
                "evidence_count": len(store.items),
                "evidence_tools": [str(i.tool) for i in store.items],
                "evidence_ids_count": len(evidence_ids),
                "evidence_ids_unique": len(set(evidence_ids)),
                "used_composer_fallback": used_composer,
            }
            return original(
                store=store, decision=decision, raw_evidence=raw_evidence, plan=plan
            )

        av.verify_agent_answer = wrapped  # type: ignore[assignment]
        al.verify_agent_answer = wrapped  # type: ignore[assignment]
        return original

    def uninstall(self, original: Any) -> None:
        import app.assistant.orchestrator.answer_verifier as av
        import app.assistant.orchestrator.agent_loop as al

        av.verify_agent_answer = original  # type: ignore[assignment]
        al.verify_agent_answer = original  # type: ignore[assignment]


def _max_consecutive_same(tools: list[str]) -> int:
    if not tools:
        return 0
    best = cur = 1
    for i in range(1, len(tools)):
        if tools[i] == tools[i - 1]:
            cur += 1
            best = max(best, cur)
        else:
            cur = 1
    return best


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


def _failure_mode(
    case_id: str,
    *,
    scored: dict[str, Any],
    tools: list[str],
    expected: list[str],
    fallback_reason: str | None,
    verifier_failures: int,
    spy_events: list[dict[str, Any]],
    p03_extra: dict[str, Any] | None,
    verifier_probe: dict[str, Any] | None,
) -> str:
    if scored.get("pass"):
        return "pass"
    if case_id == "K05":
        if tools and set(tools) != set(expected):
            return "benchmark_policy_sibling_tool"
        return "benchmark_policy_other"
    if case_id == "P03":
        if fallback_reason == "agent_limit":
            extra = p03_extra or {}
            same_args = bool(extra.get("same_args_repeated"))
            if same_args:
                return "p03_same_tool_same_args_loop"
            if int(extra.get("unique_tools") or 0) == 1 and int(extra.get("consecutive_same_tool_count") or 0) >= 2:
                return "p03_same_tool_different_args_loop"
            return "p03_agent_limit"
        return f"p03_other:{fallback_reason or 'fail'}"
    if case_id == "T07":
        proposed = []
        for ev in spy_events:
            if ev.get("tool"):
                proposed.append(ev["tool"])
        if "get_supplier" in proposed:
            return "t07_supplier_proposed_then_lost"
        if "get_purchase_orders" in tools and "get_supplier" not in tools:
            if any(ev.get("action") == "final_answer" for ev in spy_events):
                return "t07_partial_oc_then_final"
            return "t07_missing_supplier"
        return "t07_other"
    if case_id in {"M02", "M04"}:
        if verifier_failures > 0 or fallback_reason == "agent_verifier_failed":
            modes = (verifier_probe or {}).get("modes") or []
            if "calculation_mismatch" in modes:
                return "m_calc_mismatch"
            if "calculation_recompute_error" in modes:
                return "m_calc_recompute"
            if any("ungrounded" in m for m in modes):
                return "m_claim_ungrounded"
            if "reply_values_not_in_evidence" in modes:
                return "m_reply_not_in_evidence"
            if "finance_null_as_zero" in modes:
                return "m_finance_null_zero"
            if "empty_reply_composer_fallback" in modes:
                return "m_empty_reply"
            return "m_verifier_fail"
        return "m_other"
    return "other"


def _p03_structural(spy_events: list[dict[str, Any]], tools: list[str], trace: list[dict[str, Any]]) -> dict[str, Any]:
    call_events = [e for e in spy_events if e.get("action") == "call_tool" and e.get("tool")]
    fps = [e.get("args_fp") for e in call_events if e.get("args_fp")]
    unique_fps = sorted(set(fps))
    decision_reasons_structural: list[str] = []
    for e in spy_events:
        if e.get("action") == "call_tool":
            decision_reasons_structural.append(
                f"call_tool:{e.get('tool')}:args_fp={e.get('args_fp')}:keys={','.join(e.get('arg_keys') or [])}"
            )
        elif e.get("action") == "final_answer":
            decision_reasons_structural.append(
                f"final_answer:claims={e.get('claims_count')}:calcs={e.get('calculations_count')}"
            )
        elif e.get("action"):
            decision_reasons_structural.append(str(e.get("action")))

    ev_deltas = []
    for t in trace:
        if t.get("action") == "call_tool":
            before = int(t.get("evidence_count_before") or 0)
            after = int(t.get("evidence_count_after") or 0)
            ev_deltas.append(after - before)
    goal_deltas = []
    prev_cov = None
    for t in trace:
        cov = (int(t.get("goal_covered_count") or 0), int(t.get("goal_uncovered_count") or 0))
        if prev_cov is not None and cov != prev_cov:
            goal_deltas.append({"from": prev_cov, "to": cov})
        prev_cov = cov

    same_args_repeated = len(fps) >= 2 and len(unique_fps) == 1
    different_args_same_tool = (
        len({e.get("tool") for e in call_events}) == 1
        and len(unique_fps) >= 2
    )
    evidence_unchanged_on_repeat = any(d == 0 for d in ev_deltas[1:]) if len(ev_deltas) > 1 else False

    return {
        "tool_sequence": list(tools),
        "unique_tools": len(set(tools)),
        "consecutive_same_tool_count": _max_consecutive_same(tools),
        "decision_reasons_structural": decision_reasons_structural[:20],
        "unique_args_fingerprints": len(unique_fps),
        "same_args_repeated": same_args_repeated,
        "different_args_same_tool": different_args_same_tool,
        "evidence_deltas_on_calls": ev_deltas[:10],
        "evidence_unchanged_on_repeat_call": evidence_unchanged_on_repeat,
        "goal_coverage_changes": goal_deltas[:8],
        "proposed_requirement_types": [
            t for e in spy_events for t in (e.get("proposed_requirement_types") or [])
        ][:12],
    }


def classify_from_fails(fail_n: int, n: int) -> str:
    if fail_n >= 8:
        return "DETERMINISTIC"
    if fail_n >= 2:
        return "STOCHASTIC"
    return "NON_REPRODUCED"


def _diagnose_case(case_id: str, runs: list[dict[str, Any]]) -> dict[str, Any]:
    modes = Counter(r.get("failure_mode") for r in runs if not r.get("pass"))
    dominant = modes.most_common(1)[0][0] if modes else "pass"
    fail_n = sum(1 for r in runs if not r.get("pass"))
    classification = classify_from_fails(fail_n, len(runs))
    failure_rate = round(fail_n / max(len(runs), 1), 4)

    if case_id == "K05":
        return {
            "case_id": case_id,
            "failure_rate": failure_rate,
            "classification": classification,
            "dominant_failure_mode": dominant or "pass",
            "recommended_next_action": "benchmark_policy_candidate — do not change AgentLoop; consider family policy for check_stock↔get_inventory only if product intent allows",
            "diagnosis_code": "benchmark_policy_candidate",
            "diagnosis_label": "K05 is a gold/exact-tool policy choice, not a production AgentLoop priority",
        }

    if case_id == "P03":
        extras = [r.get("p03") for r in runs if r.get("p03")]
        same_args = sum(1 for e in extras if e and e.get("same_args_repeated"))
        diff_args = sum(1 for e in extras if e and e.get("different_args_same_tool"))
        ev_flat = sum(1 for e in extras if e and e.get("evidence_unchanged_on_repeat_call"))
        goal_chg = sum(1 for e in extras if e and e.get("goal_coverage_changes"))
        if same_args >= max(1, fail_n // 2):
            code, label = "A", "mismo tool + mismos args repetidos"
        elif diff_args >= max(1, fail_n // 2):
            code, label = "B", "mismo tool + args diferentes"
        elif ev_flat >= max(1, fail_n // 2):
            code, label = "C", "tool correcta pero EvidenceStore no cambia"
        elif goal_chg == 0 and fail_n:
            code, label = "E", "GoalCoverage no cambia (o no hay segundo requirement)"
        else:
            code, label = "F", "otra / mixto"
        # Refine: if B and evidence grows each call → D more than C
        if code == "B":
            grew = sum(
                1
                for e in extras
                if e and any(d > 0 for d in (e.get("evidence_deltas_on_calls") or []))
            )
            if grew >= max(1, fail_n // 2):
                code, label = "B", "mismo tool + args diferentes (evidencia sí cambia; policy no corta el loop)"
                # Could also be D
                if fail_n and grew:
                    label = "B (+D): mismo tool+args distintos; evidencia crece pero decision policy no reconoce progreso suficiente para final"
        action = (
            "minimal AgentLoop fix: stop/force-final when same tool repeats with non-progressing KPI period "
            "OR after N identical-tool invokes even if args fingerprint differs slightly"
            if classification == "DETERMINISTIC"
            else "gather more signal; do not change production yet"
        )
        return {
            "case_id": case_id,
            "failure_rate": failure_rate,
            "classification": classification,
            "dominant_failure_mode": dominant,
            "recommended_next_action": action,
            "diagnosis_code": code,
            "diagnosis_label": label,
            "structural_counts": {
                "same_args_repeated_runs": same_args,
                "different_args_same_tool_runs": diff_args,
                "evidence_unchanged_repeat_runs": ev_flat,
                "goal_change_runs": goal_chg,
            },
        }

    if case_id == "T07":
        supplier_proposed = 0
        schema_reject_supplier = 0
        partial_then_final = 0
        for r in runs:
            tools_prop = [e.get("tool") for e in (r.get("spy_events") or []) if e.get("tool")]
            if "get_supplier" in tools_prop:
                supplier_proposed += 1
            if any(
                err.get("tool") == "get_supplier"
                for err in (r.get("arg_errors") or [])
            ):
                schema_reject_supplier += 1
            if r.get("failure_mode") == "t07_partial_oc_then_final":
                partial_then_final += 1
        goal_snaps = [r.get("goal_coverage") for r in runs if r.get("goal_coverage")]
        # requirements types across runs
        req_types = Counter()
        for snap in goal_snaps:
            for req in (snap or {}).get("requirements") or []:
                req_types[str(req.get("type"))] += 1
        if supplier_proposed == 0 and schema_reject_supplier == 0:
            code, label = "A", "modelo nunca propone get_supplier"
        elif schema_reject_supplier:
            code, label = "B", "get_supplier propuesto pero schema/args lo rechazó"
        elif partial_then_final >= max(1, fail_n // 2):
            code, label = "C", "get_purchase_orders satisface parcialmente y el modelo finaliza"
        elif "supplier" not in req_types and "purchase_orders" in "".join(req_types.keys()):
            code, label = "D", "GoalCoverage no detecta el segundo requirement (supplier)"
        else:
            # empty goal types often → D contributing to C
            if not req_types or all(t in {"unknown", "purchase_orders", "stock_movements", "current_inventory"} for t in req_types):
                if "supplier" not in req_types:
                    code, label = "C+D", "parcial OC + GoalCoverage sin requirement supplier"
                else:
                    code, label = "C", "get_purchase_orders parcial + final prematuro"
            else:
                code, label = "E", "otro"
        action = (
            "minimal AgentLoop/goal fix: extract dual requirements (supplier+purchase_orders) and block final until both covered"
            if classification in {"DETERMINISTIC", "STOCHASTIC"}
            else "observe; low priority if NON_REPRODUCED"
        )
        return {
            "case_id": case_id,
            "failure_rate": failure_rate,
            "classification": classification,
            "dominant_failure_mode": dominant,
            "recommended_next_action": action,
            "diagnosis_code": code,
            "diagnosis_label": label,
            "structural_counts": {
                "supplier_proposed_runs": supplier_proposed,
                "schema_reject_supplier_runs": schema_reject_supplier,
                "partial_then_final_runs": partial_then_final,
                "goal_requirement_types": dict(req_types),
            },
        }

    if case_id in {"M02", "M04"}:
        mode_counts = Counter()
        for r in runs:
            for m in ((r.get("verifier_probe") or {}).get("modes") or []):
                mode_counts[m] += 1
        top = mode_counts.most_common(1)[0][0] if mode_counts else dominant
        if "claim_ungrounded" in top or "draft_ungrounded" in top or "ungrounded" in top:
            code, label = "A", "claim no grounded"
        elif "calculation_mismatch" in top:
            code, label = "C", "calculation mismatch"
        elif "reply_values_not_in_evidence" in top:
            # could be E (strict) or F (bad agent reply)
            # If tool correct and evidence present → often E or F
            code, label = "E/F", "reply values not in evidence (strict verifier vs agent wording)"
        elif "empty_reply" in top:
            code, label = "F", "respuesta del agente incorrecta/vacía"
        else:
            code, label = "F", f"verifier/agent issue: {top}"
        # Prefer finer labels from aggregate
        if mode_counts.get("claim_ungrounded", 0) or mode_counts.get("claim_ungrounded_inferencia_number", 0):
            code, label = "A", "claim no grounded"
        elif mode_counts.get("calculation_mismatch", 0):
            code, label = "C", "calculation mismatch"
        elif mode_counts.get("reply_values_not_in_evidence", 0):
            code, label = "E", "verifier demasiado estricto o wording no alineado a evidence blob"
        return {
            "case_id": case_id,
            "failure_rate": failure_rate,
            "classification": classification,
            "dominant_failure_mode": dominant,
            "recommended_next_action": "do not change verifier yet; if DETERMINISTIC, inspect claim grounding vs memory-hint pollution",
            "diagnosis_code": code,
            "diagnosis_label": label,
            "verifier_mode_counts": dict(mode_counts),
        }

    return {
        "case_id": case_id,
        "failure_rate": failure_rate,
        "classification": classification,
        "dominant_failure_mode": dominant,
        "recommended_next_action": "investigate",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FASE 8.1E failure reproducibility")
    parser.add_argument("--reps", type=int, default=DEFAULT_REPS)
    parser.add_argument("--ids", default=",".join(TARGET_IDS))
    parser.add_argument("--dataset", default=str(DATASET))
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

    probe = _VerifierProbe()
    original_verify = None
    try:
        from app.assistant.orchestrator.agent_config import agent_loop_allowed
        from app.assistant.orchestrator.agent_loop import LlmAgentDecisionClient
        from app.assistant.orchestrator.factory import build_planner
        from app.assistant.orchestrator.llm.client import OpenAICompatibleClient
        from app.assistant.orchestrator.llm.config import load_llm_settings
        from app.assistant.orchestrator.service import run_orchestrator_chat
        from app.assistant.orchestrator.turn_store import TurnStore
        from app.assistant.routes import invoke_gateway

        if not agent_loop_allowed():
            print("FATAL: agent_loop_allowed=false")
            return 2

        original_verify = probe.install()
        wanted = {x.strip() for x in args.ids.split(",") if x.strip()}
        gold_by_id = {c["id"]: c for c in load_cases(Path(args.dataset)) if c.get("id") in wanted}
        missing = wanted - set(gold_by_id)
        if missing:
            print(f"FATAL missing cases: {sorted(missing)}")
            return 2

        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        runs_path = out_dir / "repro_81e_runs.jsonl"
        if runs_path.exists():
            runs_path.unlink()

        all_runs: list[dict[str, Any]] = []
        print(f"fase81e repro reps={args.reps} cases={sorted(wanted)}")

        for case_id in [c for c in TARGET_IDS if c in wanted]:
            gold = gold_by_id[case_id]
            expected = list(gold.get("expected_tools") or [])
            for rep in range(1, args.reps + 1):
                spy = _DecisionSpy(LlmAgentDecisionClient(OpenAICompatibleClient(load_llm_settings())))
                probe.last = None
                store = TurnStore()
                cid = f"fase81e-{case_id}-r{rep}"
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
                        agent_decision_client=spy,
                    )
                result = run_orchestrator_chat(
                    message=str(gold.get("prompt") or ""),
                    actor_user=actor,
                    conversation_id=cid,
                    invoke_fn=invoke_gateway,
                    planner=build_planner(),
                    turn_store=store,
                    agent_decision_client=spy,
                )
                latency_ms = int((time.perf_counter() - t0) * 1000)
                tools = [str(t) for t in (result.get("tools_used") or []) if t]
                trace = list(result.get("agent_trace") or [])
                scored = score_case(
                    gold,
                    {
                        "tools_used": tools,
                        "fallback_used": bool(result.get("fallback_used")),
                        "fallback_reason": result.get("fallback_reason"),
                        "needs_clarification": bool(result.get("needs_clarification")),
                        "scenario": result.get("scenario"),
                        "reply": result.get("reply"),
                        "agent_trace": trace,
                        "goal_coverage": result.get("goal_coverage"),
                        "arg_errors": result.get("arg_errors") or [],
                        "verifier_failures": int(result.get("verifier_failures") or 0),
                        "latency_ms": latency_ms,
                    },
                )
                blocked = any(bool(t.get("blocked_final")) for t in trace)
                premature = (
                    len(expected) >= 2
                    and len(tools) == 1
                    and not result.get("fallback_used")
                )
                p03_extra = None
                if case_id == "P03":
                    p03_extra = _p03_structural(spy.events, tools, trace)
                mode = _failure_mode(
                    case_id,
                    scored=scored,
                    tools=tools,
                    expected=expected,
                    fallback_reason=result.get("fallback_reason"),
                    verifier_failures=int(result.get("verifier_failures") or 0),
                    spy_events=spy.events,
                    p03_extra=p03_extra,
                    verifier_probe=probe.last,
                )
                rec = {
                    "case_id": case_id,
                    "run_id": f"{case_id}-r{rep:02d}",
                    "pass": bool(scored.get("pass")),
                    "tools": tools,
                    "decision_count": len(trace),
                    "final_reason": _final_reason(result, scored),
                    "blocked_final": blocked,
                    "premature_final": premature,
                    "verifier_pass": int(result.get("verifier_failures") or 0) == 0,
                    "fallback": bool(result.get("fallback_used")),
                    "fallback_reason": result.get("fallback_reason"),
                    "agent_steps": int(result.get("agent_steps") or len(trace) or 0),
                    "latency_ms": latency_ms,
                    "failure_mode": mode,
                    "arg_errors": [
                        {
                            "error": e.get("error"),
                            "tool": e.get("tool"),
                            "fields": e.get("fields"),
                        }
                        for e in (result.get("arg_errors") or [])
                    ],
                    "spy_events": spy.events,
                    "goal_coverage": result.get("goal_coverage"),
                    "verifier_probe": probe.last,
                }
                if p03_extra is not None:
                    rec["p03"] = p03_extra
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
                            "failure_mode": mode,
                            "latency_ms": latency_ms,
                        },
                        ensure_ascii=False,
                    )
                )

        summaries = []
        for case_id in [c for c in TARGET_IDS if c in wanted]:
            case_runs = [r for r in all_runs if r["case_id"] == case_id]
            summaries.append(_diagnose_case(case_id, case_runs))

        report = {
            "phase": "8.1E",
            "reps": args.reps,
            "cases": list(wanted),
            "n_runs": len(all_runs),
            "summaries": summaries,
            "matrix": {
                s["case_id"]: {
                    "failure_rate": s["failure_rate"],
                    "classification": s["classification"],
                    "dominant_failure_mode": s["dominant_failure_mode"],
                    "diagnosis_code": s.get("diagnosis_code"),
                    "diagnosis_label": s.get("diagnosis_label"),
                    "recommended_next_action": s["recommended_next_action"],
                }
                for s in summaries
            },
        }
        json_path = out_dir / "repro_81e.json"
        txt_path = out_dir / "repro_81e.txt"
        json_path.write_text(
            _scrub(json.dumps(report, ensure_ascii=False, indent=2)), encoding="utf-8"
        )
        lines = [
            "FASE 8.1E reproducibility",
            f"reps={args.reps} n_runs={len(all_runs)}",
            "",
        ]
        for s in summaries:
            lines.append(
                f"{s['case_id']}: rate={s['failure_rate']} class={s['classification']} "
                f"mode={s['dominant_failure_mode']} diag={s.get('diagnosis_code')}:{s.get('diagnosis_label')}"
            )
            lines.append(f"  next: {s['recommended_next_action']}")
            lines.append("")
        txt_path.write_text("\n".join(lines), encoding="utf-8")
        print("SUMMARY", _scrub(json.dumps(report["matrix"], ensure_ascii=False)))
        print("OUT", str(out_dir))
        return 0
    finally:
        if original_verify is not None:
            probe.uninstall(original_verify)
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
