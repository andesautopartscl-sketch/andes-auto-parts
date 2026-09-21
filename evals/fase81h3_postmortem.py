"""FASE 8.1H.3 — postmortem of the 6 remaining benchmark failures. Analysis only.

Reads existing artifacts, writes:
  data/fase81_eval/fase81h3_postmortem.json
  data/fase81_eval/fase81h3_postmortem.txt

Touches no production code, no scorer, runs no LLM.

  python -m evals.fase81h3_postmortem
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
FAILED_IDS = ["G03", "R03", "P04", "E04", "K05", "M02"]

# A produccion | B estocastico LLM | C benchmark policy | D verifier/scorer
# E extraction/GoalCoverage | F otro
CLASSIFICATION: dict[str, dict[str, str]] = {
    "G03": {
        "class": "A",
        "label": "cardinalidad no demostrable (introducida por 8.1H)",
        "introduced_by": "8.1H",
        "detail": (
            "Tool y coverage correctos, sin fallback ni bloqueos. Se descarto un claim "
            "por grounding numerico. La unica clase de claim KPI que falla es el conteo "
            "de filas de una lista: len(stock_critico)=10 y 10 no es el valor de ningun "
            "campo del payload. El modelo no puede declararlo como calculation porque "
            "CALC_OPS no incluye una op de conteo."
        ),
    },
    "R03": {
        "class": "A",
        "label": "cardinalidad no demostrable (introducida por 8.1H)",
        "introduced_by": "8.1H",
        "detail": (
            "Caso demostrado: la respuesta baseline decia literalmente 'Hay 10 productos "
            "con stock critico (stock 0)'. Reproducido con evidencia real: 10 no esta en "
            "grounded_numbers y el claim se rechaza. La respuesta publicada perdio dos "
            "lineas respecto del baseline."
        ),
    },
    "P04": {
        "class": "A",
        "label": "cardinalidad no demostrable (introducida por 8.1H)",
        "introduced_by": "8.1H",
        "detail": (
            "Mismo tool, misma evidencia y mismo unico claim descartado que G03. Las "
            "lineas publicadas estan todas grounded, asi que lo descartado fue un tercer "
            "claim. Mecanismo identico; no observado directamente porque el sistema no "
            "persiste el texto de los claims."
        ),
    },
    "E04": {
        "class": "D",
        "label": "guardrail del verifier sobre el eje del scorer",
        "introduced_by": "preexistente",
        "detail": (
            "El prompt inyecta 8888. El modelo lo repitio en un claim extra, el verifier "
            "lo descarto y publico respuesta grounded identica a la del baseline. "
            "answer_replaced=False, ningun numero inventado llega al usuario."
        ),
    },
    "K05": {
        "class": "C",
        "label": "benchmark policy candidate",
        "introduced_by": "preexistente",
        "detail": (
            "check_stock y get_inventory son la misma familia current_inventory. El gold "
            "pide tool_count_policy=exact expected_tools=[check_stock] sin "
            "acceptable_tool_families. Identico al baseline."
        ),
    },
    "M02": {
        "class": "D",
        "label": "guardrail del verifier sobre el eje del scorer",
        "introduced_by": "preexistente",
        "detail": (
            "Fallaba tambien en el baseline, con el mismo desglose y la misma respuesta. "
            "GoalCoverage bloquea el primer final (el usuario pidio no consultar), fuerza "
            "get_inventory, y el verifier descarta draft y claim porque citan 8888. "
            "Composer publica la respuesta correcta."
        ),
    },
}

ROOT_CAUSE_A = {
    "name": "cardinalidad de lista no demostrable",
    "introduced_by": "8.1H (grounding numerico por token)",
    "not_caused_by": "8.1H.2 — los claims afectados no contienen fechas",
    "rule_1": "la cifra aparece como token numerico en la evidencia",
    "rule_2": "la cifra es resultado de una calculation recalculada por el verifier",
    "why_unreachable": (
        "len(stock_critico)=10 no es el valor de ningun campo, asi que la regla 1 no "
        "aplica; y CALC_OPS = min|max|sum|diff|ratio no tiene una op de conteo, asi que "
        "el modelo NO PUEDE declarar una calculation verificable para un conteo. La "
        "regla 2 es inalcanzable por construccion para cualquier afirmacion de "
        "cardinalidad."
    ),
    "evidence": {
        "len_stock_critico": 10,
        "len_chart_data": 7,
        "scalar_fields": ["ventas_hoy", "ventas_mes", "ventas_periodo",
                          "docs_hoy", "docs_mes", "docs_periodo"],
        "value_10_present_in_payload": False,
        "calc_ops": ["diff", "max", "min", "ratio", "sum"],
    },
    "claim_classes_tested": {
        "Hay 10 productos con stock critico.": False,
        "Las ventas del periodo son 0.": True,
        "El periodo abarca los ultimos 7 dias.": True,
        "El periodo va del 10 de septiembre de 2026 al 16 de septiembre de 2026.": True,
        "El producto VG4063 tiene stock 0.": True,
    },
    "scope": "solo afecta afirmaciones de cardinalidad sobre listas sin campo de longitud",
}


def _load(path: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
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
    vb = run.get("verifier_breakdown") or {}
    trace = run.get("agent_trace") or []
    reqs = (run.get("goal_coverage") or {}).get("requirements") or []
    acts = [t.get("action") for t in trace if t.get("action") and t.get("action") != "fallback"]
    spec = CLASSIFICATION.get(cid, {})
    return {
        "case_id": cid,
        "expected_tools": list(gold.get("expected_tools") or []),
        "actual_tools": list(run.get("tools_used") or []),
        "requirements": [q.get("type") for q in reqs],
        "covered": [q.get("type") for q in reqs if q.get("status") == "covered"],
        "uncovered": [q.get("type") for q in reqs if q.get("status") == "uncovered"],
        "impossible": [q.get("type") for q in reqs if q.get("status") == "impossible"],
        "final_reason": (
            f"fallback:{run.get('fallback_reason')}"
            if run.get("fallback_used")
            else (acts[-1] if acts else run.get("scenario"))
        ),
        "fallback": bool(run.get("fallback_used")),
        "fallback_reason": run.get("fallback_reason"),
        "agent_limit": run.get("fallback_reason") == "agent_limit",
        "agent_token_budget": run.get("fallback_reason") == "agent_token_budget",
        "blocked_final": sum(1 for t in trace if t.get("blocked_final")),
        "verifier_failures": int(run.get("verifier_failures") or 0),
        "dropped_claims": int(vb.get("dropped_claims") or 0),
        "dropped_draft": int(vb.get("dropped_draft") or 0),
        "answer_replaced": bool(vb.get("answer_replaced")),
        "calc_unresolved": int(vb.get("calc_unresolved") or 0),
        "calc_mismatch": int(vb.get("calc_mismatch") or 0),
        "calc_error": int(vb.get("calc_error") or 0),
        "classification": spec.get("class"),
        "label": spec.get("label"),
        "introduced_by": spec.get("introduced_by"),
        "detail": spec.get("detail"),
        "scorer_reasons": score.get("reasons") or [],
    }


def build() -> dict[str, Any]:
    gold = _gold()
    new_r = _load(EVAL / "fase81g_runs_llm.jsonl")
    new_s = _load(EVAL / "fase81g_scores_llm.jsonl")
    base_r = _load(EVAL / "runs_llm.jsonl")
    base_s = _load(EVAL / "scores_llm.jsonl")
    stab = _load_list(EVAL / "fase81g_stability_runs.jsonl")
    allr = list(new_r.values()) + stab

    new_fail = {k for k, v in new_s.items() if not v.get("pass")}
    base_fail = {k for k, v in base_s.items() if not v.get("pass")}

    adm: Counter = Counter()
    rejections: list[dict[str, Any]] = []
    blocked: Counter = Counter()
    released = 0
    impossible: Counter = Counter()
    for run in allr:
        for t in run.get("agent_trace") or []:
            a = t.get("admission")
            if a:
                adm[a] += 1
                if a in {"repeat_call", "tool_repeat_no_progress", "empty_repeat_no_progress"}:
                    rejections.append({"case_id": run.get("id"), "tool": t.get("tool"), "reason": a})
            if t.get("blocked_final"):
                blocked[str(run.get("id"))] += 1
            if t.get("blocked_final_released"):
                released += 1
        for q in (run.get("goal_coverage") or {}).get("requirements") or []:
            if q.get("status") == "impossible":
                impossible[f"{run.get('id')}:{q.get('type')}"] += 1

    stability: dict[str, Any] = {}
    for run in stab:
        vb = run.get("verifier_breakdown") or {}
        slot = stability.setdefault(
            str(run.get("id")),
            {"runs": 0, "pass": 0, "verifier_failures": 0, "dropped_claims": 0,
             "answer_replaced": 0, "ungrounded_number_published": 0},
        )
        slot["runs"] += 1
        slot["pass"] += int(bool(run.get("pass")))
        slot["verifier_failures"] += int(run.get("verifier_failures") or 0)
        slot["dropped_claims"] += int(vb.get("dropped_claims") or 0)
        slot["answer_replaced"] += int(bool(vb.get("answer_replaced")))
        reply = str(run.get("reply") or "")
        slot["ungrounded_number_published"] += int("8888" in reply or "9999" in reply)

    def _reply_lines(store: dict, cid: str) -> int:
        return len(str((store.get(cid) or {}).get("reply") or "").splitlines())

    return {
        "phase": "8.1H.3",
        "kind": "postmortem",
        "result": {"pass": sum(1 for v in new_s.values() if v.get("pass")), "n": len(new_s),
                   "failed": sorted(new_fail)},
        "baseline": {"pass": sum(1 for v in base_s.values() if v.get("pass")), "n": len(base_s),
                     "failed": sorted(base_fail),
                     "caveat": "medida antes de 8.1E.1 / 8.1F.4 / 8.1F.6; no es un pre-8.1G limpio"},
        "delta": {
            "new_failures": sorted(new_fail - base_fail),
            "recovered": sorted(base_fail - new_fail),
            "still_failing": sorted(base_fail & new_fail),
        },
        "failures": [
            _row(cid, gold.get(cid, {}), new_r.get(cid, {}), new_s.get(cid, {}))
            for cid in FAILED_IDS
        ],
        "root_cause_A": ROOT_CAUSE_A,
        "functional_change": {
            "improved": {
                "T07": "de fallo estable a 9/10, ambas tools, sin fallback",
                "P03": "10/10 estable",
                "N04": "de agent_limit a 10/10 con impossible/not_found derivado",
                "C04": "ya no publica una cifra derivada sin calculation verificable",
                "T05": "dropped_claims=0, bloque de movimientos presente (H.2)",
                "T08": "dropped_claims=0, bloque de movimientos presente (H.2)",
                "F01": "PASS tras la correccion de harness (setup_turns)",
            },
            "degraded": {
                "R03": f"respuesta paso de {_reply_lines(base_r,'R03')} a {_reply_lines(new_r,'R03')} lineas: se perdio el conteo de stock critico",
                "G03": "se descarto un claim; las lineas publicadas siguen siendo correctas",
                "P04": "se descarto un claim; las lineas publicadas siguen siendo correctas",
            },
            "classification_only": {
                "E04": "estocastico sobre el eje verifier; respuesta publicada identica al baseline",
                "M02": "identico al baseline en desglose y respuesta",
                "K05": "identico al baseline",
            },
        },
        "protections": {
            "admissions": dict(adm),
            "ledger_rejections": rejections,
            "blocked_final_by_case": dict(blocked),
            "blocked_final_released": released,
            "impossible_derivations": dict(impossible),
            "agent_limit": sum(1 for r in allr if r.get("fallback_reason") == "agent_limit"),
            "agent_token_budget": sum(1 for r in allr if r.get("fallback_reason") == "agent_token_budget"),
            "agent_no_progress": sum(1 for r in allr if r.get("fallback_reason") == "agent_no_progress"),
            "fallback_reasons": dict(Counter(str(r.get("fallback_reason")) for r in allr if r.get("fallback_used"))),
            "runs_examined": len(allr),
        },
        "stability": stability,
        "m02_verdict": {
            "requirement_covered_runs": sum(
                1 for r in stab if r.get("id") == "M02"
                and all(q.get("status") in {"covered", "impossible"}
                        for q in ((r.get("goal_coverage") or {}).get("requirements") or []))
            ),
            "tool_sets": sorted({",".join(r.get("tools_used") or []) for r in stab if r.get("id") == "M02"}),
            "ungrounded_number_published": sum(
                1 for r in stab if r.get("id") == "M02" and "8888" in str(r.get("reply") or "")
            ),
            "PRODUCT_FAILURE": False,
            "SCORER_FAILURE": True,
            "basis": (
                "requirement covered 10/10, tool correcta 10/10, numero inventado "
                "publicado 0/10, answer_replaced con respuesta grounded de Composer. "
                "El scorer falla por dos trips del mismo eje: verifier_failures>0 y "
                "fallback_reason=='agent_verifier_failed'."
            ),
        },
        "k05_verdict": {
            "status": "benchmark policy candidate confirmado",
            "family": "current_inventory = {get_inventory, check_stock}",
            "gold": "tool_count_policy=exact, expected_tools=[check_stock], sin acceptable_tool_families",
            "identical_to_baseline": True,
        },
        "remaining_before_closing": REMAINING,
    }


REMAINING = [
    {
        "id": "1",
        "topic": "cardinalidad no demostrable (G03/R03/P04)",
        "severity": "alta",
        "statement": (
            "Una afirmacion de conteo sobre una lista sin campo de longitud es "
            "indemostrable por construccion: no es token de evidencia y no existe op de "
            "conteo. Hoy se descarta silenciosamente informacion correcta."
        ),
        "not_proposing_fix_yet": True,
    },
    {
        "id": "2",
        "topic": "eje verifier del scorer (M02, E04, M04)",
        "severity": "media",
        "statement": (
            "3 de los 6 fallos son el mismo criterio con el producto demostrablemente "
            "correcto. Mientras no se decida la politica, el score no mide lo que parece."
        ),
        "not_proposing_fix_yet": True,
    },
    {
        "id": "3",
        "topic": "K05 policy",
        "severity": "baja",
        "statement": "Cerrar como policy o anadir acceptable_tool_families al gold.",
        "not_proposing_fix_yet": True,
    },
    {
        "id": "4",
        "topic": "T07 al 10/10",
        "severity": "baja",
        "statement": (
            "9/10. El fallo restante es seleccion del modelo, no del loop; produce un "
            "parcial grounded correcto."
        ),
        "not_proposing_fix_yet": True,
    },
    {
        "id": "5",
        "topic": "deuda de auditorias previas",
        "severity": "baja",
        "statement": (
            "content_key colisiona por truncado y por PII; la busqueda refinada tras un "
            "vacio sigue bloqueada. Ninguno mordio en 114 corridas."
        ),
        "not_proposing_fix_yet": True,
    },
]


def _txt(rep: dict[str, Any]) -> str:
    L: list[str] = []
    L.append("FASE 8.1H.3 — POSTMORTEM DE LOS 6 FALLOS RESTANTES")
    L.append("")
    L.append(f"resultado {rep['result']['pass']}/{rep['result']['n']}  fallos={rep['result']['failed']}")
    L.append(f"baseline  {rep['baseline']['pass']}/{rep['baseline']['n']}  fallos={rep['baseline']['failed']}")
    L.append(f"  caveat: {rep['baseline']['caveat']}")
    L.append(f"  nuevos={rep['delta']['new_failures']}  recuperados={rep['delta']['recovered']}")
    L.append("")
    hdr = (f"{'case':5} {'cls':3} {'exp':22} {'actual':22} {'final':14} "
           f"{'fb':3} {'blk':3} {'vf':3} {'drop':4} {'draft':5} {'repl':5} {'cu':3} {'cm':3} {'ce':3}")
    L.append(hdr); L.append("-" * len(hdr))
    for r in rep["failures"]:
        L.append(
            f"{r['case_id']:5} {r['classification'] or '?':3} {str(r['expected_tools'])[:22]:22} "
            f"{str(r['actual_tools'])[:22]:22} {str(r['final_reason'])[:14]:14} "
            f"{'si' if r['fallback'] else 'no':3} {r['blocked_final']:<3} {r['verifier_failures']:<3} "
            f"{r['dropped_claims']:<4} {r['dropped_draft']:<5} {'si' if r['answer_replaced'] else 'no':5} "
            f"{r['calc_unresolved']:<3} {r['calc_mismatch']:<3} {r['calc_error']:<3}"
        )
    L.append("")
    for r in rep["failures"]:
        L.append(f"  {r['case_id']} [{r['classification']}] {r['label']}  (introducido por: {r['introduced_by']})")
        L.append(f"      requirements={r['requirements']} covered={r['covered']} uncovered={r['uncovered']}")
        L.append(f"      agent_limit={r['agent_limit']} agent_token_budget={r['agent_token_budget']}")
        L.append(f"      scorer={r['scorer_reasons']}")
        L.append(f"      {r['detail']}")
        L.append("")
    rc = rep["root_cause_A"]
    L.append("CAUSA RAIZ DE G03 / R03 / P04")
    L.append(f"  {rc['name']} — introducida por {rc['introduced_by']}")
    L.append(f"  NO causada por: {rc['not_caused_by']}")
    L.append(f"  regla 1: {rc['rule_1']}")
    L.append(f"  regla 2: {rc['rule_2']}")
    L.append(f"  {rc['why_unreachable']}")
    L.append(f"  evidencia: {json.dumps(rc['evidence'], ensure_ascii=False)}")
    L.append("  clases de claim probadas contra evidencia real:")
    for claim, ok in rc["claim_classes_tested"].items():
        L.append(f"    grounded={str(ok):5} | {claim}")
    L.append(f"  alcance: {rc['scope']}")
    L.append("")
    fc = rep["functional_change"]
    L.append("CAMBIO FUNCIONAL vs BASELINE")
    L.append("  mejoraron:")
    for k, v in fc["improved"].items():
        L.append(f"    {k}: {v}")
    L.append("  empeoraron:")
    for k, v in fc["degraded"].items():
        L.append(f"    {k}: {v}")
    L.append("  solo cambiaron de clasificacion:")
    for k, v in fc["classification_only"].items():
        L.append(f"    {k}: {v}")
    L.append("")
    p = rep["protections"]
    L.append(f"PROTECCIONES ({p['runs_examined']} corridas)")
    L.append(f"  admisiones          : {p['admissions']}")
    L.append(f"  ledger rejections   : {p['ledger_rejections'] or 'ninguna'}")
    L.append(f"  blocked_final       : {p['blocked_final_by_case'] or 'ninguno'}  released={p['blocked_final_released']}")
    L.append(f"  impossible derivado : {p['impossible_derivations'] or 'ninguno'}")
    L.append(f"  agent_limit         : {p['agent_limit']}")
    L.append(f"  agent_token_budget  : {p['agent_token_budget']}")
    L.append(f"  agent_no_progress   : {p['agent_no_progress']}")
    L.append(f"  fallback reasons    : {p['fallback_reasons']}")
    L.append("")
    L.append("ESTABILIDAD")
    for k, v in sorted(rep["stability"].items()):
        L.append(f"  {k}: pass={v['pass']}/{v['runs']} vf={v['verifier_failures']} "
                 f"dropped_claims={v['dropped_claims']} answer_replaced={v['answer_replaced']} "
                 f"numero_inventado_publicado={v['ungrounded_number_published']}")
    L.append("")
    m = rep["m02_verdict"]
    L.append("M02")
    L.append(f"  PRODUCT_FAILURE = {m['PRODUCT_FAILURE']}")
    L.append(f"  SCORER_FAILURE  = {m['SCORER_FAILURE']}")
    L.append(f"  requirement covered {m['requirement_covered_runs']}/10  tools={m['tool_sets']}  "
             f"numero inventado publicado {m['ungrounded_number_published']}/10")
    L.append(f"  {m['basis']}")
    L.append("")
    k = rep["k05_verdict"]
    L.append("K05")
    L.append(f"  {k['status']}")
    L.append(f"  {k['family']}")
    L.append(f"  gold: {k['gold']}")
    L.append("")
    L.append("QUE QUEDA ANTES DEL CIERRE DE 8.1 (sin proponer fix)")
    for item in rep["remaining_before_closing"]:
        L.append(f"  [{item['id']}] {item['topic']} — severidad {item['severity']}")
        L.append(f"      {item['statement']}")
    return "\n".join(L)


def main() -> int:
    rep = build()
    EVAL.mkdir(parents=True, exist_ok=True)
    (EVAL / "fase81h3_postmortem.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (EVAL / "fase81h3_postmortem.txt").write_text(_txt(rep), encoding="utf-8")
    print("wrote data/fase81_eval/fase81h3_postmortem.json")
    print("wrote data/fase81_eval/fase81h3_postmortem.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
