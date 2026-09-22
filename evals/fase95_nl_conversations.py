"""FASE 9.5 — conversacion natural sobre la arquitectura que ya existe.

QUE PRUEBA ESTO

Que activar NL no necesita una segunda arquitectura. El guion entra por la MISMA
puerta que usa `/assistant/api/chat`:

    run_orchestrator_chat(message=..., actor_user=..., invoke_fn=invoke_gateway)

que es literalmente la llamada de `app/assistant/routes.py:api_chat`. Si estas
conversaciones funcionan, funcionan por el camino real: Gateway, permisos,
evidencia, provenance, validator, verifier, limites y auditoria incluidos.

EL ORDEN DE ARRANQUE IMPORTA, Y ES UN HALLAZGO

`import app` ejecuta `create_app()` a nivel de modulo (app/__init__.py:1878), y
create_app llama `load_project_dotenv()` con force=True. El `.env` del proyecto
declara ANDES_ASSISTANT_NL_ENABLED=0 y ANDES_ORCH_PLANNER=fake, asi que PISA lo
que traiga el proceso. Exportar la variable en la shell NO enciende NL.

Por eso aqui las banderas se ponen DESPUES de importar `app` — el mismo patron
que usan todos los evals de 8.x. Se restauran al terminar.

    python evals/fase95_nl_conversations.py
    python evals/fase95_nl_conversations.py --json
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

# --- 1. el paquete PRIMERO: create_app corre aqui y el .env gana.
import app as _app_package  # noqa: F401,E402  (el import ES el efecto)

# --- 2. y solo ahora las banderas del entorno de prueba.
NL_ENV = {
    "ANDES_ENV": "local",
    "ANDES_ASSISTANT_NL_ENABLED": "1",
    "ANDES_ORCH_PLANNER": "llm",
    "ANDES_ASSISTANT_AGENT_ENABLED": "1",
    "ANDES_AGENT_URL": "http://127.0.0.1:5055",
    # Fuera del alcance de 9.5: se dejan como esten.
    "ANDES_ASSISTANT_MEMORY_ENABLED": "0",
    "ANDES_ASSISTANT_HISTORY_ENABLED": "0",
}

OUT_DIR = ROOT / "data" / "fase81_eval"


class _TurnProbe:
    """Captura el metric del turno. Conteos y nombres, nunca secretos."""

    CLAVES = (
        "prompt_tokens", "completion_tokens", "total_tokens",
        "llm_latency_ms", "total_latency_ms", "tool_latency_ms",
        "tools_used", "tool_selected", "tools_count", "tool_calls",
        "invoke_count", "replan_count", "agent_steps",
        "evidence_size_chars", "verifier_failures", "retries",
        "fallback_used", "fallback_reason", "permission_denied",
        "needs_clarification", "loop_detected", "timeout", "alerts",
        "scenario", "error_code", "classification", "agent_enabled",
        "cost_estimated_usd", "agent_unavailable", "success",
    )

    def __init__(self) -> None:
        self.last: dict[str, Any] = {}
        self.raw_keys: list[str] = []

    def record_turn(self, metric: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
        if isinstance(metric, dict):
            self.raw_keys = sorted(metric)
            self.last = {k: metric.get(k) for k in self.CLAVES if k in metric}
        return metric


# --------------------------------------------------------------- los 10 casos

CASOS: tuple[dict[str, Any], ...] = (
    {"id": "N01", "que": "saludo minimo",
     "turnos": ["Hola"],
     "espera": "conversacional, SIN herramientas"},
    {"id": "N02", "que": "saludo coloquial + pregunta abierta de arranque",
     "turnos": ["Buenos dias bro, como estamos comenzando?"],
     "espera": "conversacional o aclaracion; no debe inventar cifras"},
    {"id": "N03", "que": "pregunta vaga de negocio",
     "turnos": ["Como estamos con las ventas?"],
     "espera": "una tool de ventas/KPIs, o aclaracion de periodo"},
    {"id": "N04", "que": "consulta concreta de stock",
     "turnos": ["Muestrame el stock del producto 2404."],
     "espera": "get_inventory con codigo=2404"},
    {"id": "N05", "que": "ventana natural (el caso V02)",
     "turnos": ["Tuvimos ventas entre enero y marzo de 2026?"],
     "espera": "get_sales; con PERIOD ON debe acotar la ventana"},
    {"id": "N06", "que": "necesita DOS herramientas",
     "turnos": ["Cuanto stock tiene el 2404 y que movimientos ha registrado?"],
     "espera": "get_inventory + get_stock_movements"},
    {"id": "N07", "que": "el dato no existe",
     "turnos": ["Cuanto stock tiene el producto 77777?"],
     "espera": "no encontrado explicito, sin inventar"},
    {"id": "N08", "que": "ambigua: deberia pedir aclaracion",
     "turnos": ["Cuanto nos queda?"],
     "espera": "needs_clarification=True, idealmente 0 tools"},
    {"id": "N05B", "que": "ventana natural con dias explicitos",
     "turnos": ["Tuvimos ventas entre el 1 de enero y el 31 de marzo de 2026?"],
     "espera": "get_sales con 2026-01-01..2026-03-31 cuando PERIOD esta ON"},
    {"id": "N09", "que": "segundo turno dependiente del primero",
     "turnos": ["Muestrame el producto 2404.", "Y cuanto stock tiene?"],
     "espera": "el 2o turno resuelve la anafora sin repetir el codigo"},
    {"id": "N11", "que": "cadena anaforica de tres turnos",
     "turnos": ["Muestrame el producto 2404.", "Y la marca?", "Y cuanto tenemos?"],
     "espera": "cada turno usa el contexto solo cuando es inequivoco"},
)


# ------------------------------------------------------------------ ejecucion

def _entorno_nl() -> dict[str, str | None]:
    previo = {k: os.environ.get(k) for k in NL_ENV}
    os.environ.update(NL_ENV)
    import importlib

    import app.assistant.orchestrator.agent_config as ac
    import app.assistant.orchestrator.llm.agent_prompts as ap
    importlib.reload(ac)
    importlib.reload(ap)
    return previo


def _restaurar(previo: dict[str, str | None]) -> None:
    """Devolver, nunca `pop`. Medido en 8.x: un test que hacia pop() reetiqueto
    una corrida entera de otro brazo."""
    for clave, valor in previo.items():
        if valor is None:
            os.environ.pop(clave, None)
        else:
            os.environ[clave] = valor


def _ejecutar_caso(caso: dict[str, Any]) -> dict[str, Any]:
    from app.assistant.orchestrator.factory import build_planner
    from app.assistant.orchestrator.service import run_orchestrator_chat
    from app.assistant.orchestrator.turn_store import TurnStore
    from app.assistant.routes import invoke_gateway

    planner = build_planner()          # UNA instancia para toda la conversacion
    store = TurnStore()                # UN store: es lo que hace posible N09
    cid = f"fase95-{caso['id']}"
    actor = str(caso.get("actor") or "albertadmin")

    turnos: list[dict[str, Any]] = []
    for i, mensaje in enumerate(caso["turnos"], start=1):
        sonda = _TurnProbe()
        # Los ARGUMENTOS solo son observables aqui: es el payload exacto que sale
        # hacia el Gateway. Tambien deja ver que `actor_user` lo pone el servidor
        # y que el navegador no participa en elegir la tool.
        llamadas: list[dict[str, Any]] = []

        def _espia(payload: dict[str, Any],
                   _registro=llamadas) -> tuple[int, dict[str, Any]]:
            estado, cuerpo = invoke_gateway(payload)
            _registro.append({
                "tool": payload.get("tool"),
                "arguments": payload.get("arguments"),
                "actor_user": payload.get("actor_user"),
                "status": estado,
                "ok": bool(isinstance(cuerpo, dict) and cuerpo.get("ok")),
                "error_code": (cuerpo or {}).get("error_code")
                if isinstance(cuerpo, dict) else None,
                "empty": bool(isinstance(cuerpo, dict)
                              and not ((cuerpo.get("data") or {}).get("items")
                                       or (cuerpo.get("data") or {}).get("total_stock"))),
            })
            return estado, cuerpo

        t0 = time.perf_counter()
        r = run_orchestrator_chat(
            message=mensaje,
            actor_user=actor,
            conversation_id=cid,
            invoke_fn=_espia,
            planner=planner,
            turn_store=store,
            metrics_store=sonda,
        )
        latencia = int((time.perf_counter() - t0) * 1000)
        # FASE 9.7 — forma de la respuesta, tarjetas, y cuanto del texto repite
        # lo que la tarjeta ya muestra. La duplicacion se mide, no se opina.
        vista = r.get("view") or None
        valores: list[str] = []
        for bloque in (vista or {}).get("blocks", []):
            for tarjeta in bloque.get("cards", []):
                valores += [str(f.get("value")) for f in tarjeta.get("fields", [])
                            if f.get("value") not in (None, "")]
        texto = str(r.get("reply") or "")
        repetidos = [v for v in valores if v and v in texto]

        turnos.append({
            "n": i,
            "mensaje": mensaje,
            "reply": str(r.get("reply") or ""),
            "response_kind": r.get("response_kind"),
            "view_present": bool(vista),
            "view_cards": sum(len(b.get("cards") or []) for b in (vista or {}).get("blocks", [])),
            "card_values": len(valores),
            "card_values_repeated_in_text": len(repetidos),
            "tiene_cabecera": any(h in texto for h in
                                  ("DATOS:", "INFERENCIA:", "CÁLCULOS:", "SUPUESTOS:",
                                   "PROYECCIÓN:", "RECOMENDACIÓN:")),
            "ok": bool(r.get("ok")),
            "tools_used": list(r.get("tools_used") or []),
            "grounded": r.get("grounded"),
            "needs_clarification": bool(r.get("needs_clarification")),
            "scenario": r.get("scenario"),
            "classification": r.get("classification"),
            "error_code": r.get("error_code"),
            "latency_ms": latencia,
            "gateway_calls": llamadas,
            "metric": sonda.last,
            "metric_keys": sonda.raw_keys,
        })
    return {"id": caso["id"], "que": caso["que"], "espera": caso["espera"],
            "turnos": turnos}


def _probe_slash_sigue_viva() -> dict[str, Any]:
    """N10 — un slash command no pasa por NL, ni antes ni despues.

    Dos comprobaciones independientes:
      1. el backend: /assistant/api/invoke exige una tool del allowlist y la
         resuelve por el Gateway, sin tocar el planner;
      2. el frontend: assistant_service.js parsea los slash ANTES de mirar
         `soft_llm_ready`, asi que encender NL no puede desviarlos.
    """
    from app.assistant.routes import ALLOWED_BROWSER_TOOLS, invoke_gateway

    status, body = invoke_gateway({
        "agent_id": "andes-assistant",
        "conversation_id": "fase95-N10",
        "actor_user": "albertadmin",
        "tool": "get_inventory",
        "arguments": {"codigo": "2404"},
    })
    js = (ROOT / "app" / "static" / "js" / "assistant_service.js").read_text(
        encoding="utf-8", errors="replace")
    i_slash = js.find("parseKpis(trimmed)")
    i_nl = js.find("caps.soft_llm_ready")
    return {
        "invoke_status": status,
        "invoke_ok": bool(isinstance(body, dict) and body.get("ok")),
        "tool": "get_inventory",
        "browser_allowlist": sorted(ALLOWED_BROWSER_TOOLS),
        "nl_no_esta_en_el_allowlist_del_navegador": "get_sales" not in ALLOWED_BROWSER_TOOLS,
        "js_slash_antes_que_nl": bool(0 <= i_slash < i_nl),
        "data_keys": sorted((body or {}).get("data") or {}) if isinstance(body, dict) else [],
    }


def _capacidades() -> dict[str, Any]:
    from app.assistant.orchestrator.agent_config import agent_loop_allowed
    from app.assistant.orchestrator.llm.config import load_llm_settings

    s = load_llm_settings()
    caps = dict(s.public_capabilities())
    caps["agent_loop_allowed"] = agent_loop_allowed()
    return caps


def main() -> int:
    parser = argparse.ArgumentParser(description="FASE 9.5 — conversaciones NL")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--period", action="store_true",
                        help="ademas, repetir N05 con PERIOD_RESOLUTION=1")
    args = parser.parse_args()

    previo = _entorno_nl()
    try:
        from evals.fase9_llm_preflight import require_usable_provider

        if require_usable_provider(context="fase95 NL") is not None:
            return 2

        caps = _capacidades()
        if not caps.get("soft_llm_ready"):
            print("NL NO quedo activo:", caps)
            return 2
        print(f"capacidades: {caps}\n")

        resultados = [_ejecutar_caso(c) for c in CASOS]
        slash = _probe_slash_sigue_viva()

        extra = None
        if args.period:
            prev_pr = os.environ.get("ANDES_ASSISTANT_PERIOD_RESOLUTION")
            os.environ["ANDES_ASSISTANT_PERIOD_RESOLUTION"] = "1"
            try:
                import importlib

                import app.assistant.orchestrator.agent_config as ac
                importlib.reload(ac)
                # FASE 9.6 — la verificacion critica no es el texto final: es el
                # payload que sale hacia el Gateway. Una respuesta puede sonar
                # bien sobre una ventana que nunca se consulto, y eso ya paso.
                extra = [
                    _ejecutar_caso({**next(c for c in CASOS if c["id"] == base),
                                    "id": f"{base}-PR1"})
                    for base in ("N05", "N05B")
                ]
            finally:
                if prev_pr is None:
                    os.environ.pop("ANDES_ASSISTANT_PERIOD_RESOLUTION", None)
                else:
                    os.environ["ANDES_ASSISTANT_PERIOD_RESOLUTION"] = prev_pr

        salida = {"capabilities": caps, "cases": resultados,
                  "slash_probe": slash, "period_on_cases": extra,
                  "gateway_args_expected": {"fecha_desde": "2026-01-01",
                                            "fecha_hasta": "2026-03-31"}}
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        destino = OUT_DIR / "fase97_nl_conversations.json"
        destino.write_text(json.dumps(salida, ensure_ascii=False, indent=2),
                           encoding="utf-8")
        if args.json:
            print(json.dumps(salida, ensure_ascii=False, indent=2))
        else:
            _render(salida)
        print(f"\nescrito -> {destino}")
        return 0
    finally:
        _restaurar(previo)


def _render(salida: dict[str, Any]) -> None:
    for caso in salida["cases"]:
        print(f"===== {caso['id']}  {caso['que']}")
        for t in caso["turnos"]:
            m = t.get("metric") or {}
            print(f"  [{t['n']}] {t['mensaje']}")
            print(f"      tools={t['tools_used'] or '-'}  aclaracion={t['needs_clarification']}"
                  f"  grounded={t['grounded']}  lat={t['latency_ms']}ms"
                  f"  tok={m.get('total_tokens')}")
            if m.get("fallback_used"):
                print(f"      FALLBACK: {m.get('fallback_reason')}")
            respuesta = t["reply"].replace("\n", " | ")
            print(f"      -> {respuesta[:220]}")
    print(f"\n===== N10 slash: {salida['slash_probe']}")


if __name__ == "__main__":
    raise SystemExit(main())
