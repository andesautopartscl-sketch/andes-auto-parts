"""FASE 8.1G.1 — post-mortem of the 59/64 -> 55/64 drop. Analysis only.

Reads existing artifacts, writes:
  data/fase81_eval/fase81g_postmortem.json
  data/fase81_eval/fase81g_postmortem.txt

Touches no production code, no scorer, runs no LLM and no Gateway call.

  python -m evals.fase81g_postmortem
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EVAL = ROOT / "data" / "fase81_eval"
DATASET = ROOT / "evals" / "fase81_evidence_dataset.jsonl"

OLD_RUNS = EVAL / "runs_llm.jsonl"
OLD_SCORES = EVAL / "scores_llm.jsonl"
NEW_RUNS = EVAL / "fase81g_runs_llm.jsonl"
NEW_SCORES = EVAL / "fase81g_scores_llm.jsonl"
STABILITY = EVAL / "fase81g_stability_runs.jsonl"

FAILED_IDS = ["T07", "F01", "F06", "E04", "K05", "N04", "M02", "Q01", "Q04"]

# A regresión por ProgressLedger | B previo que sigue fallando | C benchmark policy
# D verifier | E extraction/GoalCoverage | F LLM stochasticity | G otro
CLASSIFICATION: dict[str, dict[str, str]] = {
    "T07": {
        "class": "G",
        "label": "presupuesto consumido por finales bloqueados",
        "detail": (
            "8.1G SI corrigio la seleccion: por primera vez se ejecutan las dos tools. "
            "El turno muere porque el modelo repite final_answer bloqueado hasta agotar "
            "el presupuesto de tokens. El ledger admitio get_purchase_orders siempre "
            "(admission=admit_new_tool); nunca lo rechazo."
        ),
        "regression": "no",
    },
    "F01": {
        "class": "G",
        "label": "defecto de harness: setup_turns omitidos",
        "detail": (
            "El gold trae setup_turns=['Stock del 2404']. evals/fase81_runner.py los "
            "ejecuta; evals/fase81g_closure.py._run_case NO. Sin turno previo la anafora "
            "'Ahora dime los movimientos' no resuelve y el agente pide el codigo."
        ),
        "regression": "no (harness)",
    },
    "F06": {
        "class": "G",
        "label": "defecto de harness: setup_turns omitidos",
        "detail": (
            "setup_turns=['Que es el producto 2404?'] omitido. FASE 5 corta en "
            "context_ambiguous en 1 ms; el AgentLoop nunca corre."
        ),
        "regression": "no (harness)",
    },
    "E04": {
        "class": "F",
        "label": "estocasticidad LLM sobre el eje verifier",
        "detail": (
            "El modelo repitio el 8888 del usuario en un claim extra. El verifier lo "
            "descarto (dropped_claims=1) y publico respuesta grounded "
            "(answer_replaced=False). En la corrida anterior el modelo no lo repitio."
        ),
        "regression": "no",
    },
    "K05": {
        "class": "C",
        "label": "benchmark policy candidate",
        "detail": (
            "check_stock y get_inventory son la misma familia current_inventory. El gold "
            "pide exact=[check_stock] sin acceptable_tool_families. Identico a la baseline."
        ),
        "regression": "no",
    },
    "N04": {
        "class": "G",
        "label": "regresion de 8.1F.6 allow_partial (anterior a 8.1G)",
        "detail": (
            "get_inventory devuelve ok=False not_found. has_unused_covering_for_uncovered "
            "ve check_stock sin usar y bloquea el parcial; 4 finales bloqueados y "
            "agent_limit. La baseline 59/64 es anterior a 8.1F.6, por eso alli pasaba. "
            "check_stock no puede resolver un codigo inexistente."
        ),
        "regression": "si, pero de 8.1F.6, no de 8.1G",
    },
    "M02": {
        "class": "D",
        "label": "eje verifier del scorer, preexistente",
        "detail": (
            "Fallaba tambien en la baseline. El guardrail funciona en 10/10: ningun 8888 "
            "llega al usuario."
        ),
        "regression": "no",
    },
    "Q01": {
        "class": "G",
        "label": "defecto de harness: setup_turns omitidos",
        "detail": "setup_turns=['Stock del 2404'] omitido. FASE 5 corta en context_ambiguous.",
        "regression": "no (harness)",
    },
    "Q04": {
        "class": "G",
        "label": "defecto de harness: setup_turns omitidos",
        "detail": (
            "setup_turns=['Que es el producto 2404?'] omitido. El AgentLoop corre y el "
            "modelo pide el codigo (agent_clarify)."
        ),
        "regression": "no (harness)",
    },
}


def _load(path: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            row = json.loads(line)
            out[str(row.get("id"))] = row
    return out


def _load_list(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _gold() -> dict[str, dict[str, Any]]:
    out = {}
    for line in DATASET.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            out[str(row.get("id"))] = row
    return out


def _row(cid: str, gold: dict, run: dict, score: dict) -> dict[str, Any]:
    gc = run.get("goal_coverage") or {}
    reqs = gc.get("requirements") or []
    prog = run.get("agent_progress") or {}
    trace = run.get("agent_trace") or []
    final_action = None
    for t in reversed(trace):
        if t.get("action") and t["action"] != "fallback":
            final_action = t["action"]
            break
    return {
        "case_id": cid,
        "expected": list(gold.get("expected_tools") or []),
        "actual_tools": list(run.get("tools_used") or []),
        "requirements": [q.get("type") for q in reqs],
        "covered": [q.get("type") for q in reqs if q.get("status") == "covered"],
        "uncovered": [q.get("type") for q in reqs if q.get("status") == "uncovered"],
        "impossible": [q.get("type") for q in reqs if q.get("status") == "impossible"],
        "final_reason": (
            f"fallback:{run.get('fallback_reason')}"
            if run.get("fallback_used")
            else (run.get("scenario") if not final_action else final_action)
        ),
        "fallback": run.get("fallback_reason") if run.get("fallback_used") else None,
        "verifier": {
            "failures": int(run.get("verifier_failures") or 0),
            **{k: v for k, v in (run.get("verifier_breakdown") or {}).items() if v},
        },
        "blocked_finals": sum(1 for t in trace if t.get("blocked_final")),
        "admissions": [t.get("admission") for t in trace if t.get("admission")],
        "ledger": {
            k: prog.get(k)
            for k in ("progress_events", "no_progress_steps", "rejections")
            if prog.get(k) is not None
        },
        "failure_type": CLASSIFICATION.get(cid, {}).get("class"),
        "failure_label": CLASSIFICATION.get(cid, {}).get("label"),
        "failure_detail": CLASSIFICATION.get(cid, {}).get("detail"),
        "regression": CLASSIFICATION.get(cid, {}).get("regression"),
        "latency_ms": run.get("latency_ms"),
        "scorer_reasons": score.get("reasons") or [],
        "has_setup_turns": bool(gold.get("setup_turns")),
    }


def build() -> dict[str, Any]:
    gold = _gold()
    old_runs, old_scores = _load(OLD_RUNS), _load(OLD_SCORES)
    new_runs, new_scores = _load(NEW_RUNS), _load(NEW_SCORES)
    stab = _load_list(STABILITY)

    old_fail = {k for k, v in old_scores.items() if not v.get("pass")}
    new_fail = {k for k, v in new_scores.items() if not v.get("pass")}

    # Ledger activity across every agent turn we have.
    adm = Counter()
    rejections: list[dict[str, Any]] = []
    for run in list(new_runs.values()) + stab:
        for t in run.get("agent_trace") or []:
            a = t.get("admission")
            if not a:
                continue
            adm[a] += 1
            if a in {"repeat_call", "tool_repeat_no_progress", "empty_repeat_no_progress"}:
                rejections.append({"case_id": run.get("id"), "tool": t.get("tool"), "reason": a})

    # setup_turns coverage
    setup_ids = [cid for cid, g in gold.items() if g.get("setup_turns")]
    setup_lost = [
        cid
        for cid in setup_ids
        if cid in new_fail and cid not in old_fail
    ]
    setup_survived = [cid for cid in setup_ids if cid not in new_fail]

    # T07 stability split
    t07 = [r for r in stab if r.get("id") == "T07"]
    t07_pass = [r for r in t07 if r.get("pass")]
    t07_fail = [r for r in t07 if not r.get("pass")]

    def _blocked(r: dict) -> int:
        return sum(1 for t in (r.get("agent_trace") or []) if t.get("blocked_final"))

    def _po_called(r: dict) -> bool:
        return "get_purchase_orders" in (r.get("tools_used") or [])

    t07_split = {
        "pass_runs": len(t07_pass),
        "fail_runs": len(t07_fail),
        "pass_profile": {
            "blocked_finals": sorted({_blocked(r) for r in t07_pass}),
            "po_called": sorted({_po_called(r) for r in t07_pass}),
            "fallback": sorted({str(r.get("fallback_reason")) for r in t07_pass}),
        },
        "fail_profiles": [
            {
                "kind": "po_never_proposed",
                "runs": sum(1 for r in t07_fail if not _po_called(r)),
                "blocked_finals": sorted({_blocked(r) for r in t07_fail if not _po_called(r)}),
                "fallback": "agent_limit",
            },
            {
                "kind": "po_called_too_late",
                "runs": sum(1 for r in t07_fail if _po_called(r)),
                "blocked_finals": sorted({_blocked(r) for r in t07_fail if _po_called(r)}),
                "fallback": "agent_limit",
            },
        ],
        "ledger_rejected_po": any(
            t.get("admission") in {"repeat_call", "tool_repeat_no_progress", "empty_repeat_no_progress"}
            and t.get("tool") == "get_purchase_orders"
            for r in t07
            for t in (r.get("agent_trace") or [])
        ),
        "discriminator": (
            "numero de final_answer bloqueados antes de que el modelo proponga la 2a tool: "
            "2 bloqueos -> PASS (queda presupuesto para el final); 3 -> la tool corre pero el "
            "turno ya no puede cerrar (agent_limit); 4 -> el modelo nunca propone la tool."
        ),
    }

    def _bucket(cid: str) -> dict[str, Any]:
        rows = [r for r in stab if r.get("id") == cid]
        bd = [r.get("verifier_breakdown") or {} for r in rows]
        return {
            "runs": len(rows),
            "pass": sum(1 for r in rows if r.get("pass")),
            "dropped_claims": sum(int(b.get("dropped_claims") or 0) for b in bd),
            "dropped_draft": sum(int(b.get("dropped_draft") or 0) for b in bd),
            "calc_mismatch": sum(int(b.get("calc_mismatch") or 0) for b in bd),
            "calc_unresolved": sum(int(b.get("calc_unresolved") or 0) for b in bd),
            "calc_error": sum(int(b.get("calc_error") or 0) for b in bd),
            "failures": sum(int(r.get("verifier_failures") or 0) for r in rows),
            "answer_replaced_runs": sum(1 for b in bd if b.get("answer_replaced")),
            "requirement_covered_runs": sum(
                1
                for r in rows
                if all(
                    q.get("status") in {"covered", "impossible"}
                    for q in ((r.get("goal_coverage") or {}).get("requirements") or [])
                )
            ),
            "ungrounded_number_published_runs": sum(
                1 for r in rows if "8888" in str(r.get("reply") or "") or "9999" in str(r.get("reply") or "")
            ),
            "scorer_reasons": sorted({x for r in rows for x in (r.get("reasons") or [])}),
        }

    rows = [
        _row(cid, gold.get(cid, {}), new_runs.get(cid, {}), new_scores.get(cid, {}))
        for cid in FAILED_IDS
    ]

    arithmetic = {
        "baseline_pass": 59,
        "harness_setup_turns_lost": -len(setup_lost),
        "e04_llm_stochastic_verifier": -1,
        "n04_allow_partial_8_1F_6": -1,
        "p03_fixed_by_8_1E_1_extraction": +1,
        "m04_llm_stochastic_flip": +1,
        "result": 59 - len(setup_lost) - 1 - 1 + 1 + 1,
        "observed": sum(1 for v in new_scores.values() if v.get("pass")),
    }

    return {
        "phase": "8.1G.1",
        "kind": "postmortem",
        "baseline": {
            "pass": 59,
            "n": 64,
            "artifact": "data/fase81_eval/summary_llm.json",
            "caveat": (
                "La baseline 59/64 se midio ANTES de 8.1E.1 (extraction P03), 8.1F.4 "
                "(schema) y 8.1F.6 (allow_partial). NO es una referencia 'pre-8.1G' limpia: "
                "esos tres cambios nunca se validaron contra los 64. N04 lo demuestra."
            ),
        },
        "result": {"pass": arithmetic["observed"], "n": 64, "failed": sorted(new_fail)},
        "delta": {
            "new_failures": sorted(new_fail - old_fail),
            "fixed": sorted(old_fail - new_fail),
            "still_failing": sorted(old_fail & new_fail),
        },
        "arithmetic": arithmetic,
        "failures": rows,
        "ledger_activity": {
            "admissions": dict(adm),
            "rejections": rejections,
            "agent_no_progress_fallbacks": sum(
                1 for r in list(new_runs.values()) + stab if r.get("fallback_reason") == "agent_no_progress"
            ),
            "verdict": (
                "El ProgressLedger NO causo ningun fallo del benchmark. En 104 turnos "
                "produjo 102 admit_new_tool y 1 rechazo, y ese rechazo fue repeat_call "
                "(llamada identica, regla MAX_SAME_CALL preexistente). Cero rechazos por "
                "content_key / evidencia equivalente, cero fallbacks agent_no_progress."
            ),
        },
        "hypotheses": {
            "ledger_rejects_legitimate_tool": {
                "verdict": "NO",
                "evidence": "0 rechazos tool_repeat_no_progress / empty_repeat_no_progress en 104 turnos.",
            },
            "content_key_too_aggressive": {
                "verdict": "NO",
                "evidence": "content_key nunca bloqueo una llamada: no hubo rechazos por evidencia equivalente.",
            },
            "evidence_equivalent_wrong": {
                "verdict": "NO OBSERVADO",
                "evidence": "distinct_evidence == numero de invokes en todos los casos con tools.",
            },
            "productive_looks_at_wrong_execution": {
                "verdict": "NO OBSERVADO",
                "evidence": "ninguna decision dependio de ToolRecord.last_progress en esta corrida.",
            },
            "goalcoverage_changed_coverage_unduly": {
                "verdict": "NO — mejoro",
                "evidence": (
                    "P03 pasa de current_inventory(uncovered) a dashboard_kpis(covered) y "
                    "de 3 invokes a 1. Ningun caso perdio cobertura por la regla de faceta."
                ),
            },
            "allow_partial_affected": {
                "verdict": "SI — pero por 8.1F.6, no por 8.1G",
                "evidence": (
                    "N04: uncovered=current_inventory, get_inventory=not_found, check_stock "
                    "sin usar => has_unused_covering_for_uncovered=True => parcial bloqueado "
                    "=> 4 finales bloqueados => agent_limit. El gold declara "
                    "empty_not_found_ok=True: la salida esperada era respuesta negativa."
                ),
            },
        },
        "t07": t07_split,
        "m02": {
            **_bucket("M02"),
            "cause": (
                "El prompt inyecta el numero ('el stock del 2404 es 8888'). El modelo lo "
                "repite en draft_reply Y en el claim. El verifier descarta ambos "
                "(dropped_claims=1 + dropped_draft=1 => failures=2), no queda texto grounded "
                "=> answer_replaced=no_grounded_claim => Composer publica la respuesta "
                "correcta ('Stock de 2404 / Total: 2'). El scorer falla por DOS trips del "
                "mismo eje: verifier_failures>0 y fallback_reason==agent_verifier_failed. "
                "Comportamiento de produccion correcto en 10/10; 0/10 es definicion de scorer."
            ),
        },
        "m04": {
            **_bucket("M04"),
            "cause": (
                "Verifier, no decision ni grounding. En ~6/10 el modelo agrega un claim que "
                "cita el 25 del usuario; se descarta (dropped_claims) y la respuesta "
                "publicada sigue grounded (answer_replaced=0 en las 10). La tool y la "
                "cobertura son correctas en 10/10. La variacion es si el modelo repite o no "
                "el numero: estocasticidad pura."
            ),
        },
        "k05": {
            "status": "benchmark policy candidate",
            "note": "sin cambios respecto de la baseline; no se propone tocar AgentLoop ni scorer",
        },
        "setup_turns": {
            "cases_with_setup": sorted(setup_ids),
            "lost_because_omitted": sorted(setup_lost),
            "survived_because_gold_tolerates_clarify": sorted(setup_survived),
            "root_cause": (
                "evals/fase81_runner.py ejecuta gold['setup_turns'] con el mismo TurnStore y "
                "conversation_id antes del prompt. evals/fase81g_closure.py._run_case no lo "
                "hace. Los 4 que caen (F01,F06,Q01,Q04) tienen tool_count_policy=minimum sin "
                "tolerancia a clarify; los 6 que sobreviven la tienen."
            ),
        },
        "not_comparable_metrics": [
            "blocked_final: el runner antiguo contaba CASOS con >=1 bloqueo (2); el nuevo "
            "cuenta bloqueos totales (7). No son la misma magnitud.",
            "premature_final: definiciones distintas entre ambos runners.",
            "avg/p95 latency: el runner antiguo incluye los setup_turns en el cronometro; "
            "el nuevo no. La bajada 5554->4436 ms es en parte artefacto.",
        ],
        "proposals": PROPOSALS,
    }


PROPOSALS = [
    {
        "id": "P1",
        "target": "evals/fase81g_closure.py (harness, NO produccion)",
        "priority": 1,
        "change": (
            "_run_case debe ejecutar gold['setup_turns'] con el mismo TurnStore y "
            "conversation_id antes del prompt, y cronometrar igual que fase81_runner."
        ),
        "recovers": ["F01", "F06", "Q01", "Q04"],
        "risk": "ninguno en produccion; solo restituye paridad con el runner de referencia",
    },
    {
        "id": "P2",
        "target": "app/assistant/orchestrator/goal_coverage.py",
        "priority": 2,
        "change": (
            "Derivar del propio EvidenceStore la resolucion negativa de un requirement: si "
            "la unica evidencia de una tool que lo cubre trae error_code definitivo sobre la "
            "entidad (not_found / permission_denied), marcar el requirement impossible con "
            "ese reason cerrado. Hoy solo el modelo puede declararlo via unresolved."
        ),
        "why_general": (
            "Usa el vocabulario que ya existe (IMPOSSIBLE_REASONS incluye not_found y "
            "permission). No es un parche por case_id: cubre cualquier entidad inexistente "
            "o sin permiso, en cualquier familia de tools."
        ),
        "recovers": ["N04"],
        "risk": (
            "Un not_found transitorio dejaria de reintentarse con otra tool de la familia. "
            "Mitigacion: limitarlo a error_codes definitivos, nunca a fallos de transporte "
            "(agent_unavailable, erp_unavailable, timeout)."
        ),
    },
    {
        "id": "P3",
        "target": "app/assistant/orchestrator/agent_loop.py + agent_progress.py",
        "priority": 3,
        "change": (
            "Hacer visible el final bloqueado al ledger: un final_answer bloqueado cuya "
            "cobertura no cambio desde el bloqueo anterior es no-progreso. Al alcanzar el "
            "limite NO hacer fallback: habilitar el parcial (respuesta + 'No pude resolver: "
            "...'), que es la salida correcta cuando ya esta demostrado que el modelo no va "
            "a llamar la tool."
        ),
        "why_general": (
            "Cierra el hueco estructural que el propio 8.1G dejo: el ledger solo observa "
            "call_tool. 'Repeticion que no cambia ningun requirement' era parte del contrato "
            "de anti-loop y los final_answer bloqueados la cumplen."
        ),
        "recovers": ["T07 (parcialmente)", "N04 (ruta alternativa a P2)"],
        "risk": (
            "Habilitar el parcial antes podria cerrar turnos que con un paso mas habrian "
            "llamado la tool correcta: en T07 el PASS ocurre justo tras 2 bloqueos. El "
            "umbral debe ser >= 3 bloqueos consecutivos sin cambio de cobertura."
        ),
        "measured_support": (
            "7 blocked_final en el benchmark, concentrados en los 2 unicos casos que fallan "
            "por esta via (T07=2, N04=4, M02=1 benigno)."
        ),
    },
    {
        "id": "P4",
        "target": "ninguno — decision de politica del benchmark",
        "priority": 4,
        "change": (
            "M02/M04/E04 fallan porque el scorer trata verifier_failures>0 como fallo de "
            "caso. Los contadores nuevos muestran que en 10/10 (M02), 10/10 (M04) y en E04 "
            "la respuesta publicada es grounded y ningun numero inventado llega al usuario. "
            "Si dropped_claims>0 debe puntuar como fallo es politica, no defecto."
        ),
        "why_general": "No se propone tocar el verifier ni el scorer. Queda documentado.",
        "recovers": [],
        "risk": "n/a — sin implementar por instruccion explicita",
    },
    {
        "id": "P5",
        "target": "re-baseline",
        "priority": 5,
        "change": (
            "Volver a medir los 64 con el harness corregido (P1) para obtener una referencia "
            "limpia que incluya 8.1E.1 + 8.1F.4 + 8.1F.6 + 8.1G. La actual (08:43) es "
            "anterior a los tres primeros."
        ),
        "recovers": [],
        "risk": "ninguno",
    },
]


def _txt(rep: dict[str, Any]) -> str:
    L: list[str] = []
    a = rep["arithmetic"]
    L.append("FASE 8.1G.1 — POST-MORTEM 59/64 -> 55/64")
    L.append("")
    L.append("CAVEAT DE BASELINE")
    L.append("  " + rep["baseline"]["caveat"])
    L.append("")
    L.append("ARITMETICA DE LA CAIDA")
    L.append(f"  baseline                                  {a['baseline_pass']}")
    L.append(f"  harness: setup_turns omitidos             {a['harness_setup_turns_lost']}   F01 F06 Q01 Q04")
    L.append(f"  E04 estocasticidad LLM (eje verifier)     {a['e04_llm_stochastic_verifier']}   E04")
    L.append(f"  N04 allow_partial de 8.1F.6               {a['n04_allow_partial_8_1F_6']}   N04")
    L.append(f"  P03 corregido por extraction 8.1E.1       +{a['p03_fixed_by_8_1E_1_extraction']}   P03")
    L.append(f"  M04 flip estocastico                      +{a['m04_llm_stochastic_flip']}   M04")
    L.append(f"  = {a['result']}    observado: {a['observed']}")
    L.append("")
    L.append("TABLA DE LOS 9 FALLOS")
    L.append("")
    hdr = f"{'case':5} {'cls':3} {'exp':32} {'actual':32} {'final_reason':22} {'vf':3} {'bf':3} {'lat':6}"
    L.append(hdr)
    L.append("-" * len(hdr))
    for r in rep["failures"]:
        L.append(
            f"{r['case_id']:5} {r['failure_type'] or '?':3} "
            f"{str(r['expected'])[:32]:32} {str(r['actual_tools'])[:32]:32} "
            f"{str(r['final_reason'])[:22]:22} {r['verifier']['failures']:<3} "
            f"{r['blocked_finals']:<3} {r['latency_ms'] or 0:<6}"
        )
    L.append("")
    for r in rep["failures"]:
        L.append(f"  {r['case_id']} [{r['failure_type']}] {r['failure_label']}  regresion={r['regression']}")
        L.append(f"      req={r['requirements']} covered={r['covered']} uncovered={r['uncovered']}")
        L.append(f"      admissions={r['admissions']} ledger={r['ledger']}")
        L.append(f"      verifier={r['verifier']}")
        L.append(f"      scorer={r['scorer_reasons']}")
        L.append(f"      {r['failure_detail']}")
        L.append("")
    L.append("REGRESIONES REALES vs PREEXISTENTES")
    L.append("  regresion de produccion:   N04  (8.1F.6 allow_partial, ANTERIOR a 8.1G)")
    L.append("  regresion de harness:      F01 F06 Q01 Q04  (setup_turns omitidos)")
    L.append("  preexistentes:             K05 (policy)  M02 (eje verifier)")
    L.append("  estocasticos:              E04  (y M04 en sentido inverso: paso esta vez)")
    L.append("  mejorado por 8.1G:         T07 ejecuta las 2 tools por primera vez; P03 10/10")
    L.append("")
    L.append("PROGRESSLEDGER — ¿CAUSO ALGUN FALLO?")
    L.append("  " + rep["ledger_activity"]["verdict"])
    L.append(f"  admisiones={rep['ledger_activity']['admissions']}")
    L.append(f"  rechazos={rep['ledger_activity']['rejections']}")
    L.append("")
    for k, v in rep["hypotheses"].items():
        L.append(f"  {k:42} {v['verdict']}")
        L.append(f"      {v['evidence']}")
    L.append("")
    L.append("T07 — 4 PASS / 6 FAIL")
    t = rep["t07"]
    L.append(f"  discriminador: {t['discriminator']}")
    L.append(f"  PASS: blocked_finals={t['pass_profile']['blocked_finals']} po_called={t['pass_profile']['po_called']}")
    for p in t["fail_profiles"]:
        L.append(f"  FAIL {p['kind']}: runs={p['runs']} blocked_finals={p['blocked_finals']} -> {p['fallback']}")
    L.append(f"  el ledger rechazo get_purchase_orders alguna vez: {t['ledger_rejected_po']}")
    L.append("")
    L.append("M02 — 0/10")
    m = rep["m02"]
    L.append(
        f"  failures={m['failures']} dropped_claims={m['dropped_claims']} "
        f"dropped_draft={m['dropped_draft']} answer_replaced_runs={m['answer_replaced_runs']}"
    )
    L.append(
        f"  calc_mismatch={m['calc_mismatch']} calc_unresolved={m['calc_unresolved']} calc_error={m['calc_error']}"
    )
    L.append(f"  requirement resuelto en {m['requirement_covered_runs']}/10")
    L.append(f"  numero inventado publicado en {m['ungrounded_number_published_runs']}/10")
    L.append("  " + m["cause"])
    L.append("")
    L.append("M04")
    m4 = rep["m04"]
    L.append(
        f"  pass={m4['pass']}/10 failures={m4['failures']} dropped_claims={m4['dropped_claims']} "
        f"answer_replaced_runs={m4['answer_replaced_runs']}"
    )
    L.append("  " + m4["cause"])
    L.append("")
    L.append("K05")
    L.append(f"  {rep['k05']['status']} — {rep['k05']['note']}")
    L.append("")
    L.append("METRICAS NO COMPARABLES ENTRE RUNNERS")
    for n in rep["not_comparable_metrics"]:
        L.append(f"  - {n}")
    L.append("")
    L.append("PROPUESTA DE CAMBIO MINIMO (SIN IMPLEMENTAR)")
    for p in rep["proposals"]:
        L.append(f"  [{p['id']}] prioridad {p['priority']} — {p['target']}")
        L.append(f"      cambio:   {p['change']}")
        if p.get("why_general"):
            L.append(f"      general:  {p['why_general']}")
        if p.get("measured_support"):
            L.append(f"      evidencia:{p['measured_support']}")
        L.append(f"      recupera: {p['recovers']}")
        L.append(f"      riesgo:   {p['risk']}")
        L.append("")
    return "\n".join(L)


def main() -> int:
    rep = build()
    EVAL.mkdir(parents=True, exist_ok=True)
    (EVAL / "fase81g_postmortem.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (EVAL / "fase81g_postmortem.txt").write_text(_txt(rep), encoding="utf-8")
    print("wrote data/fase81_eval/fase81g_postmortem.json")
    print("wrote data/fase81_eval/fase81g_postmortem.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
