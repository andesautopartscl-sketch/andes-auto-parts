"""FASE 8.x — experimento de causa raiz para A01. No es un benchmark.

QUE SE SABE, MEDIDO

A01 pregunta "Con los movimientos del 2404, cuanto stock deberia tener para dos
meses?". En la tercera A/B real el modelo respondio, con sus palabras y en LOS
DOS BRAZOS, "los movimientos del 24/04": leyo el codigo de producto como una
fecha, pidio el codigo y no llamo a ninguna tool. Mientras tanto A02, A03, K03,
E06 y T08 resuelven el MISMO codigo y pasan.

QUE NO SE SABE

Por que. Lo unico observado es la lectura "24/04" y su independencia del flag
analitico. Todo lo demas —que sea la ambiguedad del numero, la falta de un ancla
nominal, el marco de proyeccion, la adyacencia a "movimientos"— son hipotesis
que nadie ha separado. Un arreglo construido sobre cualquiera de ellas seria un
arreglo sobre una corazonada.

POR QUE NO SE PARCHEO YA

El arreglo evidente era exponerle al modelo el token que ``CODE_LIKE_RE`` ya
detecta. Medido sobre el dataset: ese patron detecta ``8888`` en E04 y M02 —la
cifra falsa que esos casos existen para rechazar— y ``2026`` en V02, que es un
ano. Habria inyectado la alucinacion que el benchmark mide.

EL EXPERIMENTO

Factorial de una variable por vez, misma pregunta de negocio en todas, con dos
controles que hoy pasan. La variable dependiente NO es el veredicto del scorer:
es ``codigo_resuelto`` — si alguna tool se llamo con el codigo correcto. Eso
aisla el fenomeno en vez de mezclarlo con politica de tools y verifier.

    A01_original          la pregunta tal cual
    A01_codigo_no_fecha   identica, con T3311RC (81 movimientos, imposible de
                          leer como fecha). DISCRIMINADOR: si esta resuelve y la
                          original no, la causa ES la ambiguedad del numero.
    A01_ancla_nominal     "del producto 2404" — anade el sustantivo
    A01_sin_horizonte     quita "para dos meses" — el marco de proyeccion
    A01_reordenado        aleja el codigo de "movimientos"
    A02_control           pasa hoy
    A03_control           pasa hoy, y tambien lleva horizonte temporal

Con repeticiones, porque una sola corrida no distingue causa de varianza.

    ANDES_ASSISTANT_ANALYSIS_ENABLED=0 python evals/fase8x_a01_experiment.py
    python evals/fase8x_a01_experiment.py --runs 8
    python evals/fase8x_a01_experiment.py --report

LO QUE HARIA FALTA SI EL DISCRIMINADOR CONFIRMA LA AMBIGUEDAD

Un diseno seguro, NO implementado y NO decidido hasta ver los datos: en el aviso
de reintento —solo cuando el modelo ya eligio una tool que exige ``codigo`` y no
lo mando— ofrecer los tokens del mensaje que EXISTEN en el catalogo. La
pertenencia al catalogo es la red que le faltaba a ``CODE_LIKE_RE``: 8888 no es
un producto. Nada de esto se construye antes de que el experimento hable.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_RUNS = 5

# (id, pregunta, codigo esperado, es_control)
VARIANTS: tuple[tuple[str, str, str, bool], ...] = (
    ("A01_original",
     "Con los movimientos del 2404, ¿cuánto stock debería tener para dos meses?",
     "2404", False),
    ("A01_codigo_no_fecha",
     "Con los movimientos del T3311RC, ¿cuánto stock debería tener para dos meses?",
     "T3311RC", False),
    ("A01_ancla_nominal",
     "Con los movimientos del producto 2404, ¿cuánto stock debería tener para dos meses?",
     "2404", False),
    ("A01_sin_horizonte",
     "Con los movimientos del 2404, ¿cuánto stock hay?",
     "2404", False),
    ("A01_reordenado",
     "¿Cuánto stock debería tener del 2404 para dos meses, según sus movimientos?",
     "2404", False),
    ("A02_control",
     "¿Cuál es la cobertura de stock del 2404 según sus movimientos?",
     "2404", True),
    ("A03_control",
     "Según los movimientos y el stock actual del 2404, ¿cuánto faltaría "
     "para cubrir tres meses?",
     "2404", True),
)

# Como se veria el codigo si el modelo lo leyera como fecha. Solo aplica a 2404.
_DATE_READINGS = ("24/04", "24-04", "24 de abril", "2404-", "/04/")


def _misread_as_date(reply: str, codigo: str) -> bool:
    if codigo != "2404":
        return False
    low = (reply or "").lower()
    return any(mark in low for mark in _DATE_READINGS)


def _resolved_code(run: dict[str, Any], codigo: str) -> bool:
    """La variable dependiente: alguna tool se llamo con el codigo correcto.

    Se lee de arg_errors al reves — si el codigo nunca llego, el loop o bien
    pidio aclaracion o bien fue rechazado por falta de codigo. Las tools
    ejecutadas son la prueba positiva.
    """
    tools = run.get("tools_used") or []
    if not tools:
        return False
    # Una tool de producto solo se ejecuta si el codigo paso el validador.
    return any(t in {"get_stock_movements", "get_inventory", "get_product"}
               for t in tools)


def _asked_for_the_code(run: dict[str, Any]) -> bool:
    return str(run.get("scenario") or "") == "agent_clarify"


def main() -> int:
    parser = argparse.ArgumentParser(description="FASE 8.x experimento A01")
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    parser.add_argument("--report", action="store_true",
                        help="solo leer el fichero ya escrito")
    args = parser.parse_args()

    from evals.fase81g_closure import OUT_DIR, arm_id

    out_dir = Path(OUT_DIR)
    out = out_dir / f"fase8x_a01_experiment.{arm_id()}.json"

    if args.report:
        if not out.exists():
            print(f"no hay resultados en {out}")
            return 1
        return _render(json.loads(out.read_text(encoding="utf-8")))

    from evals.fase81_scorer import score_case
    from evals.fase81g_closure import (
        _base_env, _enable_agent, _llm_ready, _load_cases, _restore_safe_env,
        _run_case)

    _base_env()
    if not _llm_ready():
        print("SKIP: no hay clave LLM en este entorno. Ejecuta desde la shell "
              "donde ANDES_LLM_API_KEY esta definida.")
        return 2
    if not _enable_agent():
        print("SKIP: agent_loop_allowed=false")
        return 2

    # El gold de A01 da la politica y las tools esperadas; solo se cambia el
    # texto, que es la variable del experimento.
    base = next((c for c in _load_cases() if c.get("id") == "A01"), None)
    if base is None:
        print("ERROR: A01 no esta en el dataset")
        return 1

    results: dict[str, Any] = {"arm": arm_id(), "runs": args.runs, "variants": {}}
    try:
        for vid, prompt, codigo, is_control in VARIANTS:
            gold = dict(base)
            gold["id"] = vid
            gold["prompt"] = prompt
            rows = []
            for i in range(args.runs):
                run = _run_case(gold, f"a01x{i}")
                score = score_case(gold, run)
                rows.append({
                    "pass": bool(score.get("pass")),
                    "codigo_resuelto": _resolved_code(run, codigo),
                    "pidio_el_codigo": _asked_for_the_code(run),
                    "leido_como_fecha": _misread_as_date(run.get("reply") or "", codigo),
                    "tools": run.get("tools_used") or [],
                    "scenario": run.get("scenario"),
                    "reply": (run.get("reply") or "")[:200],
                    "latency_ms": run.get("latency_ms"),
                })
                print(f"  {vid} {i + 1}/{args.runs} "
                      f"codigo_resuelto={rows[-1]['codigo_resuelto']} "
                      f"fecha={rows[-1]['leido_como_fecha']}")
            n = len(rows)
            results["variants"][vid] = {
                "prompt": prompt,
                "codigo": codigo,
                "control": is_control,
                "runs": rows,
                "codigo_resuelto_rate": round(
                    sum(r["codigo_resuelto"] for r in rows) / n, 3),
                "pidio_codigo_rate": round(
                    sum(r["pidio_el_codigo"] for r in rows) / n, 3),
                "leido_como_fecha_rate": round(
                    sum(r["leido_como_fecha"] for r in rows) / n, 3),
                "pass_rate": round(sum(r["pass"] for r in rows) / n, 3),
                "avg_latency_ms": int(statistics.mean(
                    [int(r["latency_ms"] or 0) for r in rows])),
            }
    finally:
        _restore_safe_env()

    out_dir.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nescrito -> {out}\n")
    return _render(results)


def _render(results: dict[str, Any]) -> int:
    variants = results.get("variants") or {}
    print(f"===== experimento A01   brazo={results.get('arm')}  "
          f"repeticiones={results.get('runs')}")
    print(f"{'variante':24} {'codigo_ok':>10} {'pidio':>7} {'fecha':>7} {'pass':>6}")
    for vid, row in variants.items():
        mark = " (control)" if row.get("control") else ""
        print(f"{vid:24} {row['codigo_resuelto_rate']:>10} "
              f"{row['pidio_codigo_rate']:>7} {row['leido_como_fecha_rate']:>7} "
              f"{row['pass_rate']:>6}{mark}")

    orig = variants.get("A01_original") or {}
    disc = variants.get("A01_codigo_no_fecha") or {}
    if not orig or not disc:
        return 0
    print("\n--- lectura del discriminador ---")
    o, d = orig["codigo_resuelto_rate"], disc["codigo_resuelto_rate"]
    if d >= 0.8 and o <= 0.2:
        print("  La ambiguedad del numero ES la causa: con un codigo que no puede")
        print("  leerse como fecha, el mismo enunciado resuelve. Es accionable.")
    elif d <= 0.2 and o <= 0.2:
        print("  La ambiguedad del numero NO es la causa: falla igual con un codigo")
        print("  inequivoco. Mirar A01_ancla_nominal y A01_sin_horizonte.")
    elif 0.2 < o < 0.8:
        print("  A01_original es INESTABLE con la misma configuracion: varianza del")
        print("  modelo, no comportamiento determinista. Un arreglo aqui perseguiria")
        print("  ruido; subir las repeticiones antes de concluir.")
    else:
        print("  Resultado mixto: ninguna hipotesis queda aislada. No concluir.")
    for vid in ("A02_control", "A03_control"):
        row = variants.get(vid) or {}
        if row and row.get("codigo_resuelto_rate", 1) < 0.8:
            print(f"  AVISO: {vid} deberia resolver y no lo hace "
                  f"({row['codigo_resuelto_rate']}). El experimento no es valido: "
                  f"algo cambio fuera de la variable estudiada.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
