"""FASE 10.2.2 — medicion OFF vs ON de la puerta de aprobacion de memoria.

LA HIPOTESIS QUE HAY QUE FALSAR

    ANDES_ASSISTANT_MEMORY_APPROVAL=1 NO debe aumentar el prompt.
    Idealmente ON <= OFF en todos los escenarios.

POR QUE LA MEDICION PRINCIPAL ES DETERMINISTA

El bloque `<memory_hints>` del prompt es una funcion pura de los hints que
publica el selector: `json.dumps(hints)[:800]` mas un encabezado fijo. No
depende del mensaje, ni del modelo, ni del muestreo. Medirlo con turnos reales
del LLM le agregaria varianza de muestreo a una cantidad que no tiene varianza,
y la varianza es justo lo que haria discutible el resultado.

Asi que esta medicion es exacta, no estimada: construye el prompt REAL con
`build_agent_user_prompt` en los dos brazos y resta. El brazo con LLM
(`--llm`) existe para lo que si necesita turnos reales —tokens de proveedor,
max_total_tokens y latencia— y no para decidir la hipotesis.

SEIS ESCENARIOS, los que pidio la revision:

    aprobada | sugerida | ninguna | varias | expiradas | conflicto

EL ESCENARIO QUE IMPORTA es `conflicto`: dos memorias que se contradicen, una
aprobada y otra no. Es el unico donde la puerta no solo ahorra tokens sino que
cambia lo que el modelo puede concluir.

SEGURIDAD: no imprime valores de memoria ni secretos. Solo conteos y tamanos.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import statistics
import sys
import tempfile
import time
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "data" / "fase1022_approval"

# Tipos de turno con los que se comprueba que el bloque de memoria es
# independiente del turno. No son casos de gold: son formas de prompt.
TIPOS_DE_TURNO: dict[str, dict[str, Any]] = {
    "stock": {
        "mensaje": "cuanto stock hay del 2404",
        "evidencia": '[{"tool":"get_inventory","ok":true,"rows":1}]',
    },
    "periodo": {
        "mensaje": "ventas entre enero y marzo de 2025",
        "evidencia": '[{"tool":"get_sales","ok":true,"rows":12}]',
    },
    "primera_decision": {
        "mensaje": "necesito el detalle del producto 2417",
        "evidencia": "[]",
    },
    "aclaracion": {
        "mensaje": "y el anterior?",
        "evidencia": '[{"tool":"get_product","ok":false,"error":"not_found"}]',
    },
}


# ───────────────────────────── escenarios

def _pref(key: str, estilo: str, status: str | None, **kw: Any) -> dict[str, Any]:
    return dict(memory_type="preference", key=key,
                value={"answer_style": estilo}, status=status, **kw)


def _ui(key: str, compacto: bool, status: str | None, **kw: Any) -> dict[str, Any]:
    return dict(memory_type="ui_pref", key=key, value={"compact": compacto},
                status=status, **kw)


# Solo tipos `benign`. `frequent_entity` y `pinned_entity` son `contextual` y el
# epoch los cierra fuera de una request real: mediriamos el epoch, no la puerta.
ESCENARIOS: dict[str, list[dict[str, Any]]] = {
    "ninguna": [],
    "aprobada": [_pref("estilo", "brief", "approved")],
    "sugerida": [_pref("estilo", "brief", "suggested")],
    "varias": [
        _pref("estilo", "brief", "approved"),
        _ui("compacto", True, "approved"),
        _pref("estilo_inferido", "detailed", "suggested"),
        _pref("estilo_descartado", "operational", "rejected"),
    ],
    "expiradas": [
        _pref("estilo", "brief", "approved"),
        _pref("estilo_viejo", "detailed", "expired"),
        _ui("compacto_viejo", False, "expired"),
        # Caducada por TTL, no por estado: la otra puerta, que sigue mandando.
        _pref("estilo_ttl", "operational", "approved",
              expires_at="2020-01-01T00:00:00Z"),
    ],
    # Dos memorias que se contradicen. La aprobada dice `brief`; la inferida,
    # que nadie confirmo, dice lo contrario. Con la puerta abierta el modelo
    # recibe las dos y elige.
    "conflicto": [
        _pref("estilo", "brief", "approved"),
        _pref("estilo_inferido", "detailed", "suggested"),
    ],
}

# El mismo actor del benchmark. Uno inventado no tiene permisos en el gateway y
# TODOS los turnos caen en fallback: la primera corrida del brazo LLM midio eso
# —12 turnos identicos en 3912 prompt_tokens— y no la memoria.
ACTOR = "albertadmin"
CONVERSACION = "c-1022"


def _sembrar(ruta: pathlib.Path, slots: list[dict[str, Any]]) -> int:
    from app.assistant.orchestrator.memory_store import MemoryStore

    store = MemoryStore(path=ruta)
    store.ensure_schema()
    escritos = 0
    for slot in slots:
        fila = store.upsert(actor_user=ACTOR, scope="user",
                            conversation_id=None, verify_conversation=False,
                            **slot)
        if fila is None:
            raise RuntimeError(f"no se pudo sembrar {slot.get('key')!r}: "
                               f"{store.last_error}")
        escritos += 1
    return escritos


def _bloque_de_memoria(hints: list[dict[str, Any]]) -> str | None:
    """EXACTAMENTE lo que hace `agent_loop` antes de armar el prompt."""
    if not hints:
        return None
    try:
        return json.dumps(hints, ensure_ascii=False,
                          separators=(",", ":"))[:800]
    except (TypeError, ValueError):
        return None


def _prompt(tipo: str, memory_note: str | None) -> str:
    from app.assistant.orchestrator.llm.agent_prompts import build_agent_user_prompt

    caso = TIPOS_DE_TURNO[tipo]
    return build_agent_user_prompt(
        caso["mensaje"],
        evidence_pack=caso["evidencia"],
        memory_note=memory_note,
        context_note=None,
        loop_note=None,
        remaining_calls=2,
    )


def _estilos_publicados(hints: list[dict[str, Any]]) -> list[str]:
    """Los `answer_style` que el modelo recibiria. Sirve para ver el conflicto
    sin imprimir el contenido de ninguna memoria."""
    out = []
    for h in hints:
        v = h.get("value")
        if isinstance(v, dict) and v.get("answer_style"):
            out.append(str(v["answer_style"]))
    return out


def _un_brazo(nombre: str, slots: list[dict[str, Any]], *,
              encendida: bool, repeticiones: int) -> dict[str, Any]:
    from app.assistant.orchestrator.memory_selector import select_memory_hints

    os.environ["ANDES_ASSISTANT_MEMORY_APPROVAL"] = "1" if encendida else "0"

    with tempfile.TemporaryDirectory() as tmp:
        ruta = pathlib.Path(tmp) / "mem.db"
        sembradas = _sembrar(ruta, slots)

        from app.assistant.orchestrator.memory_store import MemoryStore
        store = MemoryStore(path=ruta)

        latencias: list[float] = []
        sel = None
        for _ in range(repeticiones):
            t0 = time.perf_counter()
            sel = select_memory_hints(actor_user=ACTOR,
                                      conversation_id=CONVERSACION, store=store)
            latencias.append((time.perf_counter() - t0) * 1000.0)

        assert sel is not None
        nota = _bloque_de_memoria(sel.hints)

        por_turno = {}
        for tipo in TIPOS_DE_TURNO:
            con = _prompt(tipo, nota)
            sin = _prompt(tipo, None)
            por_turno[tipo] = {
                "prompt_chars": len(con),
                "prompt_chars_sin_memoria": len(sin),
                "chars_agregados": len(con) - len(sin),
                "tokens_estimados": len(con) // 4,
            }

        deltas = {v["chars_agregados"] for v in por_turno.values()}

        return {
            "escenario": nombre,
            "approval": "ON" if encendida else "OFF",
            "memorias_sembradas": sembradas,
            "memorias_disponibles": sel.candidates_count,
            "seleccionadas": sel.selected_count,
            "tipos_seleccionados": list(sel.selected_types),
            "excluidas_por_estado": dict(sel.approval_excluded_by_status),
            "excluidas_por_la_puerta": sel.approval_excluded,
            "puerta_aplicada": sel.approval_enforced,
            "caducadas_por_ttl": sembradas - sel.candidates_count,
            "invalidadas_por_epoch": sel.memory_contextual_invalidated,
            "contexto_chars": sel.budget_chars,
            "memory_note_chars": len(nota or ""),
            "chars_agregados_al_prompt": sorted(deltas)[0] if deltas else 0,
            "delta_constante_entre_turnos": len(deltas) == 1,
            "por_tipo_de_turno": por_turno,
            "estilos_publicados": _estilos_publicados(sel.hints),
            "seleccion_ms_p50": round(statistics.median(latencias), 4),
            "seleccion_ms_max": round(max(latencias), 4),
            "telemetria_sombra": len(sel.approval_shadow),
            "tokens_que_la_puerta_evitaria": sum(
                int(e.get("estimated_tokens") or 0) for e in sel.approval_shadow),
        }


def medir_determinista(repeticiones: int) -> dict[str, Any]:
    filas = []
    for nombre, slots in ESCENARIOS.items():
        off = _un_brazo(nombre, slots, encendida=False, repeticiones=repeticiones)
        on = _un_brazo(nombre, slots, encendida=True, repeticiones=repeticiones)
        filas.append({
            "escenario": nombre,
            "OFF": off,
            "ON": on,
            "delta_chars_prompt": (on["chars_agregados_al_prompt"]
                                   - off["chars_agregados_al_prompt"]),
            "delta_seleccionadas": on["seleccionadas"] - off["seleccionadas"],
            "hipotesis_ON_no_aumenta": (on["chars_agregados_al_prompt"]
                                        <= off["chars_agregados_al_prompt"]),
            "conflicto_resuelto": (len(set(off["estilos_publicados"])) > 1
                                   and len(set(on["estilos_publicados"])) <= 1),
        })

    total_off = sum(f["OFF"]["chars_agregados_al_prompt"] for f in filas)
    total_on = sum(f["ON"]["chars_agregados_al_prompt"] for f in filas)
    return {
        "modo": "determinista",
        "repeticiones_por_brazo": repeticiones,
        "escenarios": filas,
        "veredicto": {
            "ON_nunca_aumenta_el_prompt": all(f["hipotesis_ON_no_aumenta"]
                                              for f in filas),
            "chars_totales_OFF": total_off,
            "chars_totales_ON": total_on,
            "ahorro_chars": total_off - total_on,
            "ahorro_pct": (round(100.0 * (total_off - total_on) / total_off, 1)
                           if total_off else 0.0),
            "delta_constante_entre_tipos_de_turno": all(
                f[b]["delta_constante_entre_turnos"] for f in filas
                for b in ("OFF", "ON")),
            "conflictos_resueltos_por_la_puerta": sum(
                1 for f in filas if f["conflicto_resuelto"]),
        },
    }


# ───────────────────────────── informe

def _tabla(rep: dict[str, Any]) -> str:
    L = ["", "FASE 10.2.2 — OFF vs ON de la puerta de aprobacion (determinista)",
         "=" * 78, "",
         f"{'escenario':<12} {'brazo':<5} {'disp':>4} {'sel':>4} {'excl':>5} "
         f"{'note':>5} {'prompt+':>8} {'p50 ms':>7}",
         "-" * 78]
    for f in rep["escenarios"]:
        for brazo in ("OFF", "ON"):
            b = f[brazo]
            L.append(f"{f['escenario']:<12} {brazo:<5} "
                     f"{b['memorias_disponibles']:>4} {b['seleccionadas']:>4} "
                     f"{b['excluidas_por_la_puerta']:>5} "
                     f"{b['memory_note_chars']:>5} "
                     f"{b['chars_agregados_al_prompt']:>8} "
                     f"{b['seleccion_ms_p50']:>7.3f}")
        L.append("-" * 78)

    v = rep["veredicto"]
    L += ["",
          f"  ON nunca aumenta el prompt  : {v['ON_nunca_aumenta_el_prompt']}",
          f"  chars al prompt  OFF -> ON  : {v['chars_totales_OFF']} -> "
          f"{v['chars_totales_ON']}   ({v['ahorro_pct']}% menos)",
          f"  delta constante por turno   : "
          f"{v['delta_constante_entre_tipos_de_turno']}",
          f"  conflictos que la puerta cierra: "
          f"{v['conflictos_resueltos_por_la_puerta']}", ""]

    conf = next(f for f in rep["escenarios"] if f["escenario"] == "conflicto")
    L += ["  escenario 'conflicto' — lo unico que no es solo tamano:",
          f"    OFF publica estilos {conf['OFF']['estilos_publicados']}  "
          f"(el modelo elige)",
          f"    ON  publica estilos {conf['ON']['estilos_publicados']}  "
          f"(solo lo aprobado)", ""]
    return "\n".join(L)


# ───────────────────────────── brazo con LLM real

def _clave_usable() -> bool:
    """Resuelve la credencial y verifica que SIRVE antes de gastar turnos.

    `.env` se carga con force=True y ahi la clave es una plantilla, asi que
    pisa a la del entorno de usuario. Se resuelve del ambito User de Windows y
    se fija SOLO en este proceso: nunca se escribe a disco ni se imprime. Del
    valor solo se reporta su forma.
    """
    import subprocess

    from evals.fase9_llm_preflight import BLOCKING, key_shape, preflight

    # `sin_clave` y `placeholder` son el mismo problema desde aqui: el proceso no
    # tiene una credencial usable. `.env` puede no definirla (sin_clave) o
    # definirla como plantilla (placeholder), y en los dos casos la real vive en
    # el ambito User de Windows.
    if key_shape().get("verdict") in ("placeholder", "sin_clave"):
        try:
            got = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "[Environment]::GetEnvironmentVariable("
                 "'ANDES_LLM_API_KEY','User')"],
                capture_output=True, text=True, timeout=30)
            clave = (got.stdout or "").strip()
        except Exception as exc:  # noqa: BLE001
            print(f"FATAL: no se pudo leer el ambito User: {type(exc).__name__}")
            return False
        if clave:
            os.environ["ANDES_LLM_API_KEY"] = clave
        del clave

    pre = preflight()
    forma = pre.get("key_shape") or {}
    print(f"preflight: veredicto={pre.get('verdict')} "
          f"longitud={forma.get('length')} "
          f"parece_plantilla={forma.get('looks_like_placeholder')}")
    # La lista de categorias bloqueantes es la de 9.x: reusarla evita que este
    # harness tenga su propia opinion sobre que es una credencial usable.
    if pre.get("verdict") in BLOCKING:
        print("*** brazo LLM ABORTADO ANTES DE GASTAR TURNOS ***")
        print(f"    diagnostico: {pre.get('verdict')} — {pre.get('advice', '')}")
        return False
    return True


def medir_con_llm(repeticiones: int,
                  tipos: tuple[str, ...] = ("stock",)) -> dict[str, Any]:
    """Turnos reales. Solo para tokens de proveedor, max_total_tokens y latencia:
    la hipotesis del prompt ya la decide el brazo determinista."""
    from app.assistant.orchestrator.factory import build_planner
    from app.assistant.orchestrator.memory_store import (
        reset_default_memory_store_for_tests,
    )
    from app.assistant.orchestrator.service import run_orchestrator_chat
    from app.assistant.orchestrator.turn_store import TurnStore
    from app.assistant.routes import invoke_gateway

    class _Sonda:
        def __init__(self) -> None:
            self.last: dict[str, Any] = {}

        def record_turn(self, metric: dict[str, Any], *,
                        persist: bool = True) -> dict[str, Any]:
            if isinstance(metric, dict):
                # El servicio publica la observabilidad de memoria en el metric
                # del turno, no en el resultado del chat. Leerla del resultado
                # devolvia None en los doce turnos de la primera corrida.
                self.last = {k: metric.get(k) for k in
                             ("prompt_tokens", "completion_tokens",
                              "total_tokens", "llm_latency_ms",
                              "memory_candidates", "memory_selected",
                              "memory_budget_chars", "memory_approval_enforced",
                              "memory_approval_excluded",
                              "memory_approval_shadow_count",
                              "fallback_used", "fallback_reason")}
            return metric

    filas = []
    for nombre, slots in ESCENARIOS.items():
        for encendida in (False, True):
            os.environ["ANDES_ASSISTANT_MEMORY_APPROVAL"] = "1" if encendida else "0"
            tmp = tempfile.TemporaryDirectory()
            ruta = pathlib.Path(tmp.name) / "mem.db"
            os.environ["ANDES_ASSISTANT_MEMORY_DB"] = str(ruta)
            # El servicio usa un MemoryStore singleton que se crea la PRIMERA
            # vez y ya no vuelve a leer la ruta. Sin este reset, los cinco
            # escenarios siguientes apuntan al temporal del primero —ya
            # borrado—, el `select` lanza, el servicio lo traga en su
            # soft-fail y la corrida entera reporta sel=0. Asi salio la
            # segunda corrida del brazo LLM.
            reset_default_memory_store_for_tests()
            _sembrar(ruta, slots)
            for tipo in tipos:
                caso = TIPOS_DE_TURNO[tipo]
                for rep in range(repeticiones):
                    sonda = _Sonda()
                    t0 = time.perf_counter()
                    res = run_orchestrator_chat(
                        message=caso["mensaje"],
                        actor_user=ACTOR,
                        conversation_id=f"{CONVERSACION}-{nombre}-{tipo}-{rep}",
                        invoke_fn=invoke_gateway,
                        planner=build_planner(),
                        turn_store=TurnStore(),
                        metrics_store=sonda,
                    )
                    obs = sonda.last
                    filas.append({
                        "escenario": nombre,
                        "approval": "ON" if encendida else "OFF",
                        "tipo_de_turno": tipo,
                        "rep": rep,
                        "memorias_disponibles": obs.get("memory_candidates"),
                        "seleccionadas": obs.get("memory_selected"),
                        "sombra": obs.get("memory_approval_shadow_count"),
                        "excluidas_por_la_puerta":
                            obs.get("memory_approval_excluded"),
                        "puerta_aplicada": obs.get("memory_approval_enforced"),
                        "contexto_chars": obs.get("memory_budget_chars"),
                        "prompt_tokens": obs.get("prompt_tokens"),
                        "completion_tokens": obs.get("completion_tokens"),
                        "total_tokens": obs.get("total_tokens"),
                        "llm_latency_ms": obs.get("llm_latency_ms"),
                        "latency_ms": int((time.perf_counter() - t0) * 1000),
                        "fallback": bool(res.get("fallback_used")),
                        "fallback_reason": obs.get("fallback_reason"),
                    })
                    print(f"  {nombre:<10} {'ON ' if encendida else 'OFF'} "
                          f"{tipo:<16} sel={obs.get('memory_selected')}"
                          f" excl={obs.get('memory_approval_excluded')}"
                          f" ctx={obs.get('memory_budget_chars')}"
                          f" ptok={obs.get('prompt_tokens')}"
                          f" tok={obs.get('total_tokens')}"
                          f"{' FALLBACK' if res.get('fallback_used') else ''}")
            tmp.cleanup()

    def _agg(brazo: str) -> dict[str, Any]:
        mios = [r for r in filas if r["approval"] == brazo]
        tok = [int(r["total_tokens"]) for r in mios if r.get("total_tokens")]
        ptok = [int(r["prompt_tokens"]) for r in mios if r.get("prompt_tokens")]
        lat = [int(r["latency_ms"]) for r in mios]
        return {
            "turnos": len(mios),
            "turnos_con_usage": len(tok),
            # Un turno en fallback no ejerce la ruta de memoria: contarlo como
            # medicion es lo que hizo que la primera corrida pareciera plana.
            "turnos_en_fallback": sum(1 for r in mios if r.get("fallback")),
            "turnos_con_memoria_publicada": sum(
                1 for r in mios if (r.get("seleccionadas") or 0) > 0),
            # Si la puerta no reporto, el bloque de memoria del servicio nunca
            # corrio y lo que sea que midamos no es esta unidad.
            "turnos_con_puerta_reportada": sum(
                1 for r in mios if r.get("puerta_aplicada") is not None),
            # Fallos DEL HARNESS, no del producto. Los tres sintomas por los
            # que las dos primeras corridas no median esta unidad:
            #   fallback      -> el actor no tenia permisos en el gateway
            #   sin puerta    -> el singleton apuntaba a un temporal borrado
            #   sin evidencia -> el turno nunca ejercio la ruta de memoria
            "fallos_del_harness": sum(
                1 for r in mios
                if r.get("fallback") or r.get("puerta_aplicada") is None),
            "avg_prompt_tokens": int(sum(ptok) / len(ptok)) if ptok else 0,
            "max_prompt_tokens": max(ptok) if ptok else 0,
            "avg_total_tokens": int(sum(tok) / len(tok)) if tok else 0,
            "max_total_tokens": max(tok) if tok else 0,
            "avg_latency_ms": int(sum(lat) / len(lat)) if lat else 0,
        }

    off, on = _agg("OFF"), _agg("ON")
    return {
        "modo": "llm",
        "turnos": filas,
        "resumen": {
            "OFF": off,
            "ON": on,
            "delta_max_total_tokens": on["max_total_tokens"] - off["max_total_tokens"],
            "delta_avg_prompt_tokens": on["avg_prompt_tokens"] - off["avg_prompt_tokens"],
            "delta_avg_total_tokens": on["avg_total_tokens"] - off["avg_total_tokens"],
            "fallos_del_harness": off["fallos_del_harness"] + on["fallos_del_harness"],
            "medicion_valida": (off["fallos_del_harness"] == 0
                                and on["fallos_del_harness"] == 0
                                and off["turnos_con_memoria_publicada"] > 0
                                and on["turnos_con_puerta_reportada"] > 0),
            "ON_no_aumenta_max_total_tokens":
                on["max_total_tokens"] <= off["max_total_tokens"],
        },
    }


# ───────────────────────────── entrada

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="FASE 10.2.2 OFF vs ON")
    ap.add_argument("--llm", action="store_true",
                    help="ademas, turnos reales (cuesta y no decide la hipotesis)")
    ap.add_argument("--reps", type=int, default=25,
                    help="repeticiones del selector por brazo (latencia)")
    ap.add_argument("--llm-reps", type=int, default=1)
    ap.add_argument("--llm-tipos", default="stock",
                    help="tipos de turno del brazo LLM, separados por coma. "
                         "Cada tipo multiplica el coste por 12 turnos.")
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    args = ap.parse_args(argv)

    previo = {k: os.environ.get(k) for k in
              ("ANDES_ASSISTANT_MEMORY_ENABLED", "ANDES_ASSISTANT_MEMORY_APPROVAL",
               "ANDES_ASSISTANT_MEMORY_DB", "ANDES_ASSISTANT_AGENT_ENABLED",
               "ANDES_ASSISTANT_NL_ENABLED", "ANDES_ORCH_PLANNER")}

    # Importar `app` ANTES de fijar banderas: `create_app()` recarga `.env` con
    # force=True y pisaria cualquier variable puesta antes. El mismo orden que
    # los tests de esta fase.
    from app.utils.load_env import load_project_dotenv  # noqa: F401
    import app  # noqa: F401

    os.environ["ANDES_ASSISTANT_MEMORY_ENABLED"] = "1"

    out_dir = pathlib.Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        rep = medir_determinista(args.reps)
        print(_tabla(rep))
        (out_dir / "determinista.json").write_text(
            json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
        (out_dir / "determinista.txt").write_text(_tabla(rep), encoding="utf-8")

        if args.llm:
            tipos = tuple(t.strip() for t in args.llm_tipos.split(",") if t.strip())
            desconocidos = [t for t in tipos if t not in TIPOS_DE_TURNO]
            if desconocidos:
                print(f"FATAL: tipos de turno desconocidos: {desconocidos}")
                return 2
            if not _clave_usable():
                return 2
            os.environ["ANDES_ASSISTANT_AGENT_ENABLED"] = "1"
            os.environ["ANDES_ASSISTANT_NL_ENABLED"] = "1"
            os.environ["ANDES_ORCH_PLANNER"] = "llm"
            print(f"\nbrazo LLM: {len(ESCENARIOS)} escenarios x {len(tipos)} "
                  f"tipos x 2 brazos x {args.llm_reps} rep = "
                  f"{len(ESCENARIOS) * len(tipos) * 2 * args.llm_reps} turnos")
            rep_llm = medir_con_llm(args.llm_reps, tipos)
            print(json.dumps(rep_llm["resumen"], ensure_ascii=False, indent=2))
            (out_dir / "llm.json").write_text(
                json.dumps(rep_llm, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        for k, v in previo.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        print(f"\nartefactos: {out_dir}")
        print("RESTAURADO: banderas de proceso devueltas a su valor previo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
