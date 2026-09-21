"""FASE 8.1I.5 — A/B causal del prompt de cardinalidad. Diagnostico, no fix.

Corre los mismos casos con el prompt ACTUAL (A) y con la redaccion anterior a
8.1I.2 (B), y compara. El prompt SIEMPRE queda restaurado: el contenido original
se guarda en memoria y en un backup fuera del repo, y se reescribe en un finally
que cubre excepciones y Ctrl-C. Al terminar se verifica igualdad byte a byte.

    python -m evals.fase81i5_prompt_ab --verify-only     # prueba swap+restore sin LLM
    python -m evals.fase81i5_prompt_ab                   # A/B real: T07,C04 x5

Campos capturados: solo estructura (los mismos de fase81i1_count_usage).
Nunca prompts completos, respuestas crudas, claims, evidencia, API keys ni PII.

Escribe data/fase81_eval/prompt_ab_diagnosis.{json,txt} y restaura
AGENT=0 / NL=0 / ORCH=fake.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EVAL = ROOT / "data" / "fase81_eval"
PROMPT_FILE = ROOT / "app" / "assistant" / "orchestrator" / "llm" / "agent_prompts.py"
DEFAULT_IDS = "T07,C04"

# Las DOS ediciones de 8.1I.2, exactamente. Revertir = actual -> anterior.
EDITS: list[tuple[str, str]] = [
    (
        "Toda cifra, código, fecha, porcentaje y nombre de tool debe estar en <evidence> o en "
        "calculations. Una CANTIDAD de elementos NUNCA está en <evidence>: contar lo que ves no "
        "la respalda.",
        "Toda cifra, código, fecha, porcentaje y nombre de tool debe estar en <evidence> o en "
        "calculations.",
    ),
    (
        'OBLIGATORIO: si vas a decir cuántos elementos tiene una lista, declara count con UN '
        'input que apunte a esa lista en evidence (ej. inputs=["e1.data.stock_critico"]). Sin ese '
        'count NO escribas la cifra: di "varios"/"algunos" o describe los elementos sin cantidad. '
        'count solo sobre listas: nunca sobre objetos, strings ni escalares.',
        'Para decir CUÁNTOS elementos hay, declara count con UN input que apunte a la lista en '
        'evidence (ej. inputs=["e1.data.stock_critico"]). Sin ese count, la cantidad no está '
        'respaldada.',
    ),
]


def _restore_env() -> None:
    os.environ["ANDES_ASSISTANT_AGENT_ENABLED"] = "0"
    os.environ["ANDES_ASSISTANT_NL_ENABLED"] = "0"
    os.environ["ANDES_ORCH_PLANNER"] = "fake"


def _reload_prompts() -> None:
    """SYSTEM_AGENT es constante de modulo: hay que recargarlo para que el cambio
    tenga efecto. reload reutiliza el __dict__, asi que quien ya importo
    build_agent_system_prompt sigue viendo el valor actualizado."""
    import app.assistant.orchestrator.llm.agent_prompts as mod

    importlib.reload(mod)


def _apply_variant_b(current: str) -> str:
    out = current
    for new_text, old_text in EDITS:
        if new_text not in out:
            raise SystemExit(
                "ABORTADO: no encuentro una de las ediciones de 8.1I.2 en el prompt. "
                "El archivo cambió; revisa antes de correr el A/B."
            )
        out = out.replace(new_text, old_text, 1)
    return out


def _variant_fingerprint() -> dict[str, Any]:
    from app.assistant.orchestrator.llm.agent_prompts import build_agent_system_prompt

    prompt = build_agent_system_prompt()
    return {
        "chars": len(prompt),
        "lines": len(prompt.splitlines()),
        "has_OBLIGATORIO": "OBLIGATORIO" in prompt,
        "has_CANTIDAD_rule": "CANTIDAD de elementos NUNCA" in prompt,
        "mentions_count": "count" in prompt,
    }


# ------------------------------------------------------------------ captura


def _install_probe(records: list[dict[str, Any]], case_id: str):
    from app.assistant.orchestrator import agent_loop as al

    original = al.validate_agent_decision
    counter = {"i": 0}

    def spy(raw: Any, *, user_message: str | None = None) -> dict[str, Any]:
        counter["i"] += 1
        index = counter["i"]
        raw_calcs = []
        if isinstance(raw, dict) and isinstance(raw.get("calculations"), list):
            raw_calcs = [c for c in raw["calculations"] if isinstance(c, dict)]
        try:
            out = original(raw, user_message=user_message)
        except Exception as exc:  # noqa: BLE001 — record then re-raise
            records.append(
                {
                    "case_id": case_id, "decision_index": index,
                    "act": str((raw or {}).get("action") or "")[:20] if isinstance(raw, dict) else None,
                    "tool": (str((raw or {}).get("tool"))[:64]
                             if isinstance(raw, dict) and raw.get("tool") else None),
                    "calculation_present": bool(raw_calcs),
                    "calculation_op": [str(c.get("op") or "")[:16] for c in raw_calcs],
                    "calculation_input_count": [len(c.get("inputs") or []) for c in raw_calcs],
                    "calculation_result": [c.get("result") for c in raw_calcs],
                    "schema_rejected": getattr(exc, "code", None) or type(exc).__name__,
                }
            )
            raise
        calcs = out.get("calculations") or []
        records.append(
            {
                "case_id": case_id, "decision_index": index,
                "act": str(out.get("action") or "")[:20],
                "tool": (str(out.get("tool"))[:64] if out.get("tool") else None),
                "calculation_present": bool(calcs),
                "calculation_op": [str(c.get("op") or "")[:16] for c in calcs],
                "calculation_input_count": [len(c.get("inputs") or []) for c in calcs],
                "calculation_result": [c.get("result") for c in calcs],
                "schema_rejected": None,
            }
        )
        return out

    al.validate_agent_decision = spy
    return original


def _summarize(cid: str, attempt: int, run: dict[str, Any],
               decisions: list[dict[str, Any]]) -> dict[str, Any]:
    vb = run.get("verifier_breakdown") or {}
    pg = run.get("agent_progress") or {}
    trace = run.get("agent_trace") or []
    reqs = (run.get("goal_coverage") or {}).get("requirements") or []
    acts = [t.get("action") for t in trace if t.get("action") and t.get("action") != "fallback"]
    return {
        "case_id": cid, "run": attempt,
        "tools_used": list(run.get("tools_used") or []),
        "requirements": [q.get("type") for q in reqs],
        "covered": [q.get("type") for q in reqs if q.get("status") == "covered"],
        "uncovered": [q.get("type") for q in reqs if q.get("status") == "uncovered"],
        "signature": [
            f"{t.get('action')}:{t.get('tool') or '-'}"
            f"{':blocked' if t.get('blocked_final') else ''}"
            f"{':released' if t.get('blocked_final_released') else ''}"
            for t in trace
        ],
        "blocked_final": sum(1 for t in trace if t.get("blocked_final")),
        "consecutive_blocked_finals": pg.get("consecutive_blocked_finals"),
        "ledger_rejected": [t.get("admission") for t in trace
                            if t.get("admission") in {"repeat_call", "tool_repeat_no_progress",
                                                      "empty_repeat_no_progress"}],
        "calculation_present": any(d["calculation_present"] for d in decisions),
        "calculation_op": [op for d in decisions for op in d["calculation_op"]],
        "calculation_input_count": [n for d in decisions for n in d["calculation_input_count"]],
        "calculation_result": [r for d in decisions for r in d["calculation_result"]],
        "verifier_status": "PASS" if int(run.get("verifier_failures") or 0) == 0 else "FAIL",
        "verifier_failures": int(run.get("verifier_failures") or 0),
        "calc_unresolved": int(vb.get("calc_unresolved") or 0),
        "calc_mismatch": int(vb.get("calc_mismatch") or 0),
        "calc_error": int(vb.get("calc_error") or 0),
        "dropped_claims": int(vb.get("dropped_claims") or 0),
        "answer_replaced": bool(vb.get("answer_replaced")),
        "final_reason": (f"fallback:{run.get('fallback_reason')}" if run.get("fallback_used")
                         else (acts[-1] if acts else run.get("scenario"))),
        "fallback": bool(run.get("fallback_used")),
        "pass": bool(run.get("pass")),
    }


def _run_variant(label: str, ids: list[str], runs: int) -> list[dict[str, Any]]:
    from app.assistant.orchestrator import agent_loop as al
    from evals.fase81_runner import load_cases
    from evals.fase81_scorer import score_case
    from evals.fase81g_closure import DATASET, _run_case

    gold = {c["id"]: c for c in load_cases(DATASET)}
    rows: list[dict[str, Any]] = []
    for cid in ids:
        case = gold.get(cid)
        if case is None:
            print(f"  {cid}: no esta en el dataset")
            continue
        for attempt in range(runs):
            decisions: list[dict[str, Any]] = []
            original = _install_probe(decisions, cid)
            try:
                run = _run_case(case, f"ab{label}-{attempt}")
            finally:
                al.validate_agent_decision = original
            run["pass"] = bool(score_case(case, run).get("pass"))
            row = _summarize(cid, attempt, run, decisions)
            row["variant"] = label
            rows.append(row)
            print(f"  [{label}] {cid} #{attempt} pass={row['pass']} tools={row['tools_used']} "
                  f"calc={row['calculation_op']} vf={row['verifier_failures']} "
                  f"cu={row['calc_unresolved']} blocked={row['blocked_final']}")
    return rows


# -------------------------------------------------------------------- report


def _agg_t07(rows: list[dict[str, Any]]) -> dict[str, Any]:
    t = [r for r in rows if r["case_id"] == "T07"]
    if not t:
        return {}
    sigs = Counter(tuple(r["signature"]) for r in t)
    return {
        "runs": len(t),
        "pass": sum(1 for r in t if r["pass"]),
        "runs_proposing_get_purchase_orders": sum(
            1 for r in t if "get_purchase_orders" in r["tools_used"]),
        "runs_only_get_supplier": sum(1 for r in t if r["tools_used"] == ["get_supplier"]),
        "runs_with_blocked_final": sum(1 for r in t if r["blocked_final"]),
        "runs_with_fallback": sum(1 for r in t if r["fallback"]),
        "distinct_signatures": len(sigs),
        "signatures": {" > ".join(k): v for k, v in sigs.items()},
    }


def _agg_c04(rows: list[dict[str, Any]]) -> dict[str, Any]:
    c = [r for r in rows if r["case_id"] == "C04"]
    if not c:
        return {}
    return {
        "runs": len(c),
        "pass": sum(1 for r in c if r["pass"]),
        "runs_with_calculation": sum(1 for r in c if r["calculation_present"]),
        "calculation_ops": dict(Counter(op for r in c for op in r["calculation_op"])),
        "calculation_input_counts": dict(Counter(n for r in c for n in r["calculation_input_count"])),
        "calculation_results": [r["calculation_result"] for r in c],
        "verifier_failures_total": sum(r["verifier_failures"] for r in c),
        "calc_unresolved_total": sum(r["calc_unresolved"] for r in c),
        "calc_mismatch_total": sum(r["calc_mismatch"] for r in c),
        "calc_error_total": sum(r["calc_error"] for r in c),
        "dropped_claims_total": sum(r["dropped_claims"] for r in c),
        "answer_replaced_runs": sum(1 for r in c if r["answer_replaced"]),
    }


def _conclude(a: dict[str, Any], b: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    ta, tb = a.get("t07") or {}, b.get("t07") or {}
    if ta and tb:
        pa = ta["runs_proposing_get_purchase_orders"]
        pb = tb["runs_proposing_get_purchase_orders"]
        if pa <= 1 and pb >= max(2, tb["runs"] // 2):
            out["T07"] = ("A: evidencia fuerte de regresion causada por 8.1I.2 "
                          f"(propone la 2a tool {pa}/{ta['runs']} con prompt actual vs "
                          f"{pb}/{tb['runs']} con el anterior)")
        elif pa == pb:
            out["T07"] = ("C: mismo comportamiento en ambas variantes; no atribuir causalidad "
                          "al prompt, seguir investigando")
        else:
            out["T07"] = (f"parcial: {pa}/{ta['runs']} vs {pb}/{tb['runs']}; "
                          "diferencia no concluyente con este n")
    ca, cb = a.get("c04") or {}, b.get("c04") or {}
    if ca and cb:
        if ca.get("calc_unresolved_total", 0) > 0 and cb.get("calc_unresolved_total", 0) == 0:
            out["C04"] = ("B: evidencia fuerte de que 8.1I.2 induce la calculation problematica "
                          f"(calc_unresolved {ca['calc_unresolved_total']} vs 0)")
        elif ca.get("calc_unresolved_total", 0) == cb.get("calc_unresolved_total", 0):
            out["C04"] = "C: igual en ambas variantes; no atribuir causalidad al prompt"
        else:
            out["C04"] = (f"parcial: calc_unresolved {ca.get('calc_unresolved_total')} vs "
                          f"{cb.get('calc_unresolved_total')}")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FASE 8.1I.5 prompt A/B")
    parser.add_argument("--ids", default=DEFAULT_IDS)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--verify-only", action="store_true",
                        help="prueba el swap y la restauracion sin LLM")
    args = parser.parse_args(argv)
    ids = [x.strip() for x in args.ids.split(",") if x.strip()]

    original_bytes = PROMPT_FILE.read_bytes()
    backup = Path(tempfile.gettempdir()) / "fase81i5_agent_prompts.bak"
    backup.write_bytes(original_bytes)
    print(f"backup del prompt en {backup}")

    report: dict[str, Any] = {"phase": "8.1I.5", "ids": ids, "runs_per_case": args.runs}
    try:
        current_text = original_bytes.decode("utf-8")
        variant_b_text = _apply_variant_b(current_text)
        if variant_b_text == current_text:
            raise SystemExit("ABORTADO: la variante B es identica a la actual")

        if args.verify_only:
            PROMPT_FILE.write_bytes(variant_b_text.encode("utf-8"))
            _reload_prompts()
            report["variant_b_fingerprint"] = _variant_fingerprint()
            print("  variante B activa:", report["variant_b_fingerprint"])
            return 0

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

        from app.assistant.orchestrator.agent_config import agent_loop_allowed

        if not agent_loop_allowed():
            print("FATAL: agent_loop_allowed=false")
            return 2

        print("EXPERIMENTO A — prompt ACTUAL (8.1I.2)")
        _reload_prompts()
        report["variant_a_fingerprint"] = _variant_fingerprint()
        rows_a = _run_variant("A", ids, args.runs)

        print("EXPERIMENTO B — redaccion ANTERIOR a 8.1I.2 (temporal)")
        PROMPT_FILE.write_bytes(variant_b_text.encode("utf-8"))
        _reload_prompts()
        report["variant_b_fingerprint"] = _variant_fingerprint()
        rows_b = _run_variant("B", ids, args.runs)

        report["A"] = {"rows": rows_a, "t07": _agg_t07(rows_a), "c04": _agg_c04(rows_a)}
        report["B"] = {"rows": rows_b, "t07": _agg_t07(rows_b), "c04": _agg_c04(rows_b)}
        report["conclusion"] = _conclude(report["A"], report["B"])
    finally:
        PROMPT_FILE.write_bytes(original_bytes)
        restored_ok = PROMPT_FILE.read_bytes() == original_bytes
        try:
            _reload_prompts()
        except Exception:  # noqa: BLE001 — restoration of bytes is what matters
            pass
        _restore_env()
        report["prompt_restored_byte_identical"] = restored_ok
        print(f"PROMPT RESTAURADO byte-identico={restored_ok}")
        print(f"RESTORED AGENT={os.environ.get('ANDES_ASSISTANT_AGENT_ENABLED')} "
              f"NL={os.environ.get('ANDES_ASSISTANT_NL_ENABLED')} "
              f"ORCH={os.environ.get('ANDES_ORCH_PLANNER')}")
        if not restored_ok:
            print(f"ATENCION: restaura manualmente desde {backup}")

    if "A" in report:
        EVAL.mkdir(parents=True, exist_ok=True)
        (EVAL / "prompt_ab_diagnosis.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        L = ["FASE 8.1I.5 - A/B DEL PROMPT DE CARDINALIDAD", ""]
        L.append(f"A = prompt actual (8.1I.2)   {report['variant_a_fingerprint']}")
        L.append(f"B = redaccion anterior       {report['variant_b_fingerprint']}")
        L.append("")
        for name, agg in (("T07", "t07"), ("C04", "c04")):
            a, b = report["A"].get(agg) or {}, report["B"].get(agg) or {}
            if not a:
                continue
            L.append(f"{name}")
            keys = sorted(set(a) | set(b))
            width = max(len(k) for k in keys)
            L.append(f"  {'campo'.ljust(width)}  {'A (actual)':<28} {'B (anterior)':<28}")
            for k in keys:
                L.append(f"  {k.ljust(width)}  {str(a.get(k)):<28} {str(b.get(k)):<28}")
            L.append("")
        L.append("CONCLUSION")
        for k, v in (report.get("conclusion") or {}).items():
            L.append(f"  {k}: {v}")
        L.append("")
        L.append(f"prompt_restored_byte_identical = {report['prompt_restored_byte_identical']}")
        (EVAL / "prompt_ab_diagnosis.txt").write_text("\n".join(L), encoding="utf-8")
        print("wrote data/fase81_eval/prompt_ab_diagnosis.json / .txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
