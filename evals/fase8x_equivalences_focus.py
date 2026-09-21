"""FASE 8.x — validacion LLM FOCALIZADA por conjuntos, en los dos brazos.

Dos conjuntos, dos preguntas distintas:

**equivalences** (O01..O04) — el experimento minimo que distingue "arreglado" de
"no arreglado" en la cadena de argumentos. O01 y O04 preguntan por un OEM y
necesitan `oem`; O02 y O03 preguntan por un codigo interno y necesitan `codigo`.
En la tercera A/B real la particion fue exacta: los dos que necesitaban la clave
inemitible murieron, los dos que necesitaban la emitible pasaron.

**regression** — los casos que no pueden empeorar: T02, T07, T09, P03, M02, M04,
N04, mas V01..V03 porque `get_sales` cambio de forma (ahora declara su alcance
siempre) y O02/O03 porque `get_equivalences` cambio su rama de vacio.

Una corrida focalizada NO cierra la fase: no dice nada de los otros casos. Sirve
para confirmar el mecanismo y descartar regresion antes de gastar un benchmark
completo.

No reimplementa nada: importa `_run_case` y `score_case` del closure, que son la
misma ruta que usa el benchmark. Una segunda implementacion podria discrepar, y
entonces la medicion no mediria el producto.

    ANDES_ASSISTANT_ANALYSIS_ENABLED=0 python evals/fase8x_equivalences_focus.py
    ANDES_ASSISTANT_ANALYSIS_ENABLED=1 python evals/fase8x_equivalences_focus.py
    python evals/fase8x_equivalences_focus.py --set regression
    python evals/fase8x_equivalences_focus.py --compare

La clave vive en el entorno de quien ejecuta. Este script no la lee ni la
imprime: solo comprueba que el closure la considere disponible.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SETS: dict[str, tuple[str, ...]] = {
    "equivalences": ("O01", "O02", "O03", "O04"),
    # T02/T07/T09 multi-tool, P03/M02/M04/N04 memoria y grounding, V/O los
    # buckets cuyas tools cambiaron de forma en esta fase.
    "regression": ("T02", "T07", "T09", "P03", "M02", "M04", "N04",
                   "V01", "V02", "V03", "O02", "O03"),
}


def _trace(run: dict[str, Any]) -> list[tuple[Any, Any, Any]]:
    return [(t.get("decision_index"), t.get("action"), t.get("tool"))
            for t in (run.get("agent_trace") or [])]


def _out_path(out_dir: Path, name: str, arm: str) -> Path:
    return out_dir / f"fase8x_focus_{name}.{arm}.json"


def compare(name: str) -> int:
    """Diff OFF vs ON sobre los ficheros ya escritos por los dos brazos."""
    from evals.fase81g_closure import OUT_DIR

    # Los brazos se descubren en disco: con tres dimensiones (an/pv/pr), fijar
    # dos nombres a mano dejaria fuera justo el que se acaba de anadir.
    found = sorted(Path(OUT_DIR).glob(f"fase8x_focus_{name}.*.json"))
    if len(found) < 2:
        print(f"hacen falta al menos dos brazos; en disco: "
              f"{[f.name for f in found] or 'ninguno'}")
        return 1
    arms: dict[str, dict[str, Any]] = {}
    for path in found:
        arm = path.name.split(".")[-2]
        arms[arm] = {c["id"]: c for c in json.loads(
            path.read_text(encoding="utf-8"))["cases"]}
    names = sorted(arms)
    base = names[0]
    ids = [i for i in SETS[name] if i in arms[base]]
    print(f"===== {name}")
    for arm in names:
        print(f"  {arm}: {sum(1 for i in ids if arms[arm].get(i, {}).get('pass'))}/{len(ids)}")
    for case_id in ids:
        verdicts = {a: arms[a].get(case_id, {}).get("pass") for a in names}
        flag = "  <-- DIFIEREN" if len(set(verdicts.values())) > 1 else ""
        print(f"\n--- {case_id}  {verdicts}{flag}")
        print(f"    {arms[base][case_id].get('prompt')}")
        for arm, row in ((a, arms[a][case_id]) for a in names if case_id in arms[a]):
            print(f"    [{arm}] tools={row['tools_used']} retries={row['retries']} "
                  f"fb={row['fallback_reason']} kind={row['failure_kind']}")
            print(f"         decisiones={row['decisions']}")
            if row["arg_errors"]:
                print(f"         arg_errors={row['arg_errors']}")
            print(f"         reply={(row['reply'] or '')[:160]!r}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="FASE 8.x validacion LLM focalizada")
    parser.add_argument("--set", dest="name", default="equivalences",
                        choices=sorted(SETS), help="conjunto de casos a ejecutar")
    parser.add_argument("--compare", action="store_true",
                        help="solo comparar los ficheros ya escritos")
    args = parser.parse_args()

    if args.compare:
        return compare(args.name)

    from evals.fase81_scorer import score_case
    from evals.fase81g_closure import (
        OUT_DIR, _base_env, _enable_agent, _llm_ready, _load_cases,
        _restore_safe_env, _run_case, arm_id)

    _base_env()
    if not _llm_ready():
        print("SKIP: no hay clave LLM en este entorno. Ejecuta desde la shell "
              "donde ANDES_LLM_API_KEY esta definida.")
        return 2
    if not _enable_agent():
        print("SKIP: agent_loop_allowed=false")
        return 2

    arm = arm_id()
    wanted = SETS[args.name]
    cases = {c["id"]: c for c in _load_cases() if c.get("id") in wanted}
    missing = [i for i in wanted if i not in cases]
    if missing:
        print(f"ERROR: casos ausentes del dataset: {missing}")
        return 1

    rows: list[dict[str, Any]] = []
    try:
        for case_id in wanted:
            gold = cases[case_id]
            run = _run_case(gold, "focus")
            score = score_case(gold, run)
            rows.append({
                "id": case_id,
                "arm": arm,
                "prompt": gold.get("prompt"),
                "expected_tools": gold.get("expected_tools"),
                "tool_count_policy": gold.get("tool_count_policy"),
                "pass": score.get("pass"),
                "tools_used": run.get("tools_used"),
                "scenario": run.get("scenario"),
                "fallback_used": run.get("fallback_used"),
                "fallback_reason": run.get("fallback_reason"),
                "arg_errors": run.get("arg_errors"),
                "retries": len(run.get("arg_errors") or []),
                "decisions": _trace(run),
                "goal_coverage": run.get("goal_coverage"),
                "reply": (run.get("reply") or "")[:400],
                "reasons": score.get("reasons"),
                "failure_kind": score.get("failure_kind"),
                "product_failure": score.get("product_failure"),
                "verifier_failures": run.get("verifier_failures"),
                "latency_ms": run.get("latency_ms"),
                "total_tokens": run.get("total_tokens"),
            })
            print(f"  {case_id} pass={score.get('pass')} tools={run.get('tools_used')} "
                  f"fb={run.get('fallback_reason')} retries={len(run.get('arg_errors') or [])}")
    finally:
        _restore_safe_env()

    out_dir = Path(OUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = _out_path(out_dir, args.name, arm)
    out.write_text(json.dumps(
        {"set": args.name, "arm": arm, "cases": rows,
         "pass": sum(1 for r in rows if r["pass"]), "n": len(rows)},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{args.name} {arm}: {sum(1 for r in rows if r['pass'])}/{len(rows)}  ->  {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
