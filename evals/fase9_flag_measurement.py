"""FASE 9 — cerrar la deuda de medicion de las banderas, con un diseno dirigido.

EL PROBLEMA

Tres capacidades apagadas y sin una sola medicion con modelo: ANALYSIS (8.5),
PERIOD_RESOLUTION (8.x) y ORDERS (9.2). El diseno obvio —factorial completo—
cuesta 2^3 x 76 = 608 turnos, y el proveedor se agoto a ~411 en veinticinco
minutos: la corrida no llegaria al final y produciria justo el artefacto que 8.x
enseno a desconfiar, una tanda de casillas vacias con aspecto de regresion.

LA MEDICION QUE REDISENA EL EXPERIMENTO

El radio de impacto de las tres banderas es radicalmente asimetrico:

    ANALYSIS   3/76 casos    el bloque analitico solo entra si analytical_intent
    PERIOD     2/76 casos    resolve_period dispara en 7, y 5 son KPI (sin inyeccion)
    ORDERS    76/76 casos    anade una linea de contrato a TODOS los prompts

Asi que ORDERS necesita el benchmark entero y las otras dos NO: fuera de su
radio, el prompt y los argumentos son identicos con la bandera encendida o
apagada. Dirigir el experimento baja el coste de 608 a ~192 turnos.

POR QUE ESTO NO ES PEREZA

Restringir la medicion solo vale si la neutralidad fuera del radio se DEMUESTRA,
no se asume. Por eso el instrumento hace primero una comprobacion determinista,
caso por caso, sobre las 76 preguntas: con la bandera encendida y apagada,
¿cambia algo para los casos que dice no tocar? Si cambia aunque sea uno, el radio
declarado es falso y la medicion dirigida NO es valida — y el runner se niega a
seguir en vez de producir un numero bonito.

Es el mismo principio que `measurement_valid`: una cifra que no midio lo que dice
haber medido es peor que ninguna cifra.

    python evals/fase9_flag_measurement.py --flag analysis
    python evals/fase9_flag_measurement.py --flag period --runs 5
    python evals/fase9_flag_measurement.py --flag orders     (dice que necesita --bench)
    python evals/fase9_flag_measurement.py --report
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_RUNS = 8


# --------------------------------------------------------------- radios

def _analysis_cases(cases: list[dict[str, Any]]) -> list[str]:
    from app.assistant.orchestrator.analysis import analytical_intent

    return [c["id"] for c in cases if analytical_intent(c.get("prompt") or "")]


def _period_cases(cases: list[dict[str, Any]]) -> list[str]:
    """Dispara resolve_period Y la tool acepta fechas sin tener `periodo` propio.

    get_dashboard_kpis queda fuera por contrato, no por lista: tiene vocabulario
    de periodo propio y la inyeccion la salta.
    """
    from app.assistant.orchestrator.period import resolve_period
    from app.assistant.orchestrator.tool_contracts import allowed_arg_keys

    out = []
    for case in cases:
        if not resolve_period(case.get("prompt") or ""):
            continue
        tools = set(case.get("expected_tools") or [])
        for familia in (case.get("acceptable_tool_families") or {}).values():
            tools |= set(familia)
        if any("fecha_desde" in allowed_arg_keys(t) and "periodo" not in allowed_arg_keys(t)
               for t in tools):
            out.append(case["id"])
    return out


FLAGS: dict[str, dict[str, Any]] = {
    "analysis": {
        "env": "ANDES_ASSISTANT_ANALYSIS_ENABLED",
        "radius": _analysis_cases,
        "question": "¿el modelo USA la escalera cuando puede? Nunca se ha medido.",
    },
    "period": {
        "env": "ANDES_ASSISTANT_PERIOD_RESOLUTION",
        "radius": _period_cases,
        "question": "¿la ventana derivada llega a la tool y la respuesta es cierta?",
    },
    "orders": {
        "env": "ANDES_ASSISTANT_ORDERS_ENABLED",
        "radius": None,  # global: no hay radio que dirigir
        "question": "¿anadir la tool al prompt cambia la seleccion en algun caso?",
    },
}


# ------------------------------------------------- neutralidad fuera del radio

def _reload_config() -> None:
    import app.assistant.orchestrator.agent_config as ac
    import app.assistant.orchestrator.llm.agent_prompts as ap

    importlib.reload(ac)
    importlib.reload(ap)


def _case_tools(case: dict[str, Any]) -> list[str]:
    tools = set(case.get("expected_tools") or [])
    for familia in (case.get("acceptable_tool_families") or {}).values():
        tools |= set(familia)
    return sorted(tools)


def _fingerprint(prompt_text: str, case: dict[str, Any]) -> tuple[int, str]:
    """Lo que el modelo recibiria PARA ESTE CASO: el prompt de sistema y los
    argumentos que el normalizador dejaria pasar para las tools que este caso
    llama de verdad.

    La primera version normalizaba contra `get_sales` para todos los casos, y el
    verificador declaro el radio de PERIOD invalido: los cinco casos de KPI
    "cambiaban" porque la huella les inyectaba una ventana en una tool que ellos
    no usan. El radio era correcto; la huella medía otra cosa. Vale la pena
    dejarlo escrito: un verificador que se equivoca hacia el lado seguro —negarse
    a medir— es el comportamiento que se quiere.
    """
    from app.assistant.orchestrator.tool_contracts import normalize_agent_args

    mensaje = case.get("prompt") or ""
    args = {}
    for tool in _case_tools(case):
        args[tool] = normalize_agent_args(
            tool, {"fecha_desde": "2026-01-01", "fecha_hasta": "2026-03-31"},
            user_message=mensaje)
    return len(prompt_text), json.dumps(args, sort_keys=True)


def verify_radius(flag: str, cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Demuestra —o refuta— que la bandera no toca nada fuera de su radio.

    Sin esto, dirigir el experimento seria una suposicion disfrazada de ahorro.
    """
    spec = FLAGS[flag]
    if spec["radius"] is None:
        return {"targeted": False,
                "reason": "radio global: la bandera cambia el prompt de todos los casos"}

    dentro = set(spec["radius"](cases))
    previo = os.environ.get(spec["env"])
    huellas: dict[str, dict[str, Any]] = {}
    try:
        for estado in ("0", "1"):
            os.environ[spec["env"]] = estado
            _reload_config()
            from app.assistant.orchestrator.analysis import analytical_intent
            from app.assistant.orchestrator.llm.agent_prompts import (
                build_agent_system_prompt)

            for case in cases:
                mensaje = case.get("prompt") or ""
                prompt = build_agent_system_prompt(
                    analytical=analytical_intent(mensaje))
                huellas.setdefault(case["id"], {})[estado] = _fingerprint(prompt, case)
    finally:
        if previo is None:
            os.environ.pop(spec["env"], None)
        else:
            os.environ[spec["env"]] = previo
        _reload_config()

    cambian = {cid for cid, h in huellas.items() if h["0"] != h["1"]}
    fuera_que_cambian = sorted(cambian - dentro)
    dentro_que_no_cambian = sorted(dentro - cambian)
    return {
        "targeted": True,
        "radius": sorted(dentro),
        "cases_that_change": sorted(cambian),
        "outside_radius_that_change": fuera_que_cambian,
        "inside_radius_that_do_not_change": dentro_que_no_cambian,
        # La condicion que legitima dirigir: nada fuera del radio se mueve.
        "radius_is_sound": not fuera_que_cambian,
    }


# ------------------------------------------------------------- metricas

def _ladder_metrics(run: dict[str, Any], score: dict[str, Any]) -> dict[str, Any]:
    analysis = score.get("analysis") or {}
    return {
        "used_ladder": bool(analysis.get("used_ladder")),
        "rungs_used": list(analysis.get("rungs_used") or []),
        "dropped_claims": int(analysis.get("dropped_claims") or 0),
    }


def _period_metrics(run: dict[str, Any], score: dict[str, Any]) -> dict[str, Any]:
    reply = str(run.get("reply") or "")
    return {
        "reply_states_a_window": any(m in reply for m in ("-01-", "-02-", "-03-",
                                                          "enero", "marzo")),
        "reply_states_no_filter": "sin filtro de fecha" in reply,
    }


METRICS = {"analysis": _ladder_metrics, "period": _period_metrics,
           "orders": lambda run, score: {}}


# ------------------------------------------------------------------ runner

def measure(flag: str, runs: int) -> int:
    from evals.fase81_scorer import score_case
    from evals.fase81g_closure import (
        OUT_DIR, _base_env, _enable_agent, _llm_ready, _load_cases,
        _restore_safe_env, _run_case, arm_id)

    spec = FLAGS[flag]
    _base_env()
    cases = _load_cases()

    radio = verify_radius(flag, cases)
    if not radio["targeted"]:
        print(f"'{flag}' tiene radio GLOBAL: {radio['reason']}.")
        print("No se puede dirigir. Mide con el benchmark completo en los dos")
        print("brazos:  ANDES_ASSISTANT_ORDERS_ENABLED=0|1  ...closure.py --bench")
        return 2
    if not radio["radius_is_sound"]:
        print("RADIO NO VALIDO — la bandera toca casos que dice no tocar:")
        print("  ", radio["outside_radius_that_change"])
        print("Una medicion dirigida aqui mediria otra cosa. Usa el benchmark completo.")
        return 1

    objetivo = radio["radius"]
    print(f"radio verificado: {objetivo}  ({len(objetivo)}/{len(cases)} casos)")
    print(f"coste: {len(objetivo)} casos x {runs} repeticiones x 2 brazos = "
          f"{len(objetivo) * runs * 2} turnos")

    # FASE 9 — preflight ANTES de gastar un solo turno. Comprobar presencia no
    # basta: el 2026-09-21 una sesion exporto la cadena "TU_KEY_YA_EXISTENTE",
    # `_llm_ready()` dijo que si, y se quemaron 354 turnos marcandolos "sin
    # medir" sin que nadie dijera que el problema era la credencial.
    from evals.fase9_llm_preflight import require_usable_provider

    bloqueo = require_usable_provider(context=f"medicion de '{flag}'")
    if bloqueo is not None:
        print("    el radio quedo verificado; la medicion NO se ejecuta.")
        return 2
    if not _llm_ready():
        print("SKIP: no hay clave LLM en este entorno. El diseno queda verificado; "
              "ejecuta desde la shell donde ANDES_LLM_API_KEY esta definida.")
        return 2
    if not _enable_agent():
        print("SKIP: agent_loop_allowed=false")
        return 2

    por_id = {c["id"]: c for c in cases}
    previo = os.environ.get(spec["env"])
    resultados: dict[str, Any] = {"flag": flag, "runs": runs, "radius": objetivo,
                                  "radius_check": radio, "arms": {}}
    try:
        for estado in ("0", "1"):
            os.environ[spec["env"]] = estado
            _reload_config()
            brazo = arm_id()
            filas: dict[str, list[dict[str, Any]]] = {}
            for cid in objetivo:
                gold = por_id[cid]
                for i in range(runs):
                    run = _run_case(gold, f"f9{i}")
                    score = score_case(gold, run)
                    fila = {
                        "pass": bool(score.get("pass")),
                        "unmeasured": bool(score.get("unmeasured")),
                        "tools": run.get("tools_used") or [],
                        "fallback_reason": run.get("fallback_reason"),
                        "reply": (run.get("reply") or "")[:200],
                        "latency_ms": run.get("latency_ms"),
                        "total_tokens": run.get("total_tokens"),
                    }
                    fila.update(METRICS[flag](run, score))
                    filas.setdefault(cid, []).append(fila)
                    print(f"  [{spec['env'].split('_')[-1]}={estado}] {cid} "
                          f"{i + 1}/{runs} pass={fila['pass']} "
                          f"{'SIN MEDIR' if fila['unmeasured'] else ''}")
            resultados["arms"][estado] = {"arm": brazo, "cases": filas}
    finally:
        if previo is None:
            os.environ.pop(spec["env"], None)
        else:
            os.environ[spec["env"]] = previo
        _reload_config()
        _restore_safe_env()

    out = Path(OUT_DIR) / f"fase9_flag_{flag}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(resultados, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\
escrito -> {out}\
")
    return render(resultados)


def render(data: dict[str, Any]) -> int:
    flag = data.get("flag")
    runs = data.get("runs")
    print(f"===== bandera '{flag}'   repeticiones={runs}   "
          f"pregunta: {FLAGS[flag]['question']}")
    vacias = 0
    for cid in data.get("radius") or []:
        linea = [f"  {cid:5}"]
        for estado in ("0", "1"):
            filas = ((data.get("arms") or {}).get(estado) or {}).get("cases", {}).get(cid, [])
            if not filas:
                continue
            n = len(filas)
            vacias += sum(1 for f in filas if f.get("unmeasured"))
            medidas = [f for f in filas if not f.get("unmeasured")]
            if not medidas:
                linea.append(f"[{estado}] SIN MEDIR")
                continue
            tasa = sum(1 for f in medidas if f["pass"]) / len(medidas)
            extra = ""
            if flag == "analysis":
                usos = sum(1 for f in medidas if f.get("used_ladder"))
                extra = f" escalera={usos}/{len(medidas)}"
            linea.append(f"[{estado}] pass={tasa:.2f} ({len(medidas)}/{n}){extra}")
        print("  ".join(linea))
    if vacias:
        print("")  # linea en blanco
        print(f"AVISO: {vacias} corridas sin respuesta del modelo. "
              f"Esa parte NO se midio.")
    if flag == "analysis":
        # Cuantas corridas del brazo ENCENDIDO llegaron a medirse, y de esas
        # cuantas usaron la escalera. Las dos cifras hacen falta: la primera
        # version solo contaba los usos, asi que con CERO corridas medidas
        # concluia "el modelo no la elige" — una lectura sobre la nada, y el
        # mismo defecto de clase que este proyecto lleva catorce correcciones
        # eliminando. Sin medicion no hay lectura.
        encendido = ((data.get("arms") or {}).get("1") or {}).get("cases", {})
        medidas = [f for filas in encendido.values() for f in filas
                   if not f.get("unmeasured")]
        usos = sum(1 for f in medidas if f.get("used_ladder"))
        print("")  # linea en blanco
        print("--- lectura ---")
        if not medidas:
            print("  SIN LECTURA: cero corridas medidas con la capacidad encendida.")
            print("  No se puede afirmar nada sobre el uso de la escalera — ni que")
            print("  se use ni que no. Repite la medicion con un proveedor que")
            print("  responda.")
        elif usos == 0:
            print(f"  La escalera NO se uso en ninguna de las {len(medidas)} corridas")
            print("  medidas con la capacidad encendida. No es un fallo del schema")
            print("  (8.x verifica que los peldanos son emitibles): es que el modelo")
            print("  no la elige. Construir recomendaciones encima seria construir")
            print("  sobre un cimiento que no sostiene nada.")
        else:
            print(f"  La escalera se uso en {usos} de {len(medidas)} corridas medidas.")
            print("  Revisar los peldanos y si las proyecciones declararon sus")
            print("  assumption_ids.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="FASE 9 — medicion de banderas")
    parser.add_argument("--flag", choices=sorted(FLAGS))
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--radius-only", action="store_true",
                        help="solo verificar el radio, sin LLM")
    args = parser.parse_args()

    from evals.fase81g_closure import OUT_DIR, _base_env, _load_cases

    if args.report:
        salida = 0
        for flag in sorted(FLAGS):
            path = Path(OUT_DIR) / f"fase9_flag_{flag}.json"
            if path.exists():
                render(json.loads(path.read_text(encoding="utf-8")))
                print()
            else:
                print(f"'{flag}': sin medir todavia ({path.name} no existe)\
")
        return salida

    if not args.flag:
        parser.error("indica --flag o --report")

    if args.radius_only:
        _base_env()
        radio = verify_radius(args.flag, _load_cases())
        print(json.dumps(radio, ensure_ascii=False, indent=2))
        return 0

    return measure(args.flag, args.runs)


if __name__ == "__main__":
    raise SystemExit(main())
