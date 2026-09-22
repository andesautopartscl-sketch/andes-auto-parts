"""FASE 9 — preflight del proveedor LLM: una llamada, un diagnostico.

QUE LO MOTIVA, MEDIDO

El 2026-09-21 una sesion exporto la cadena literal ``TU_KEY_YA_EXISTENTE`` como
clave. ``_llm_ready()`` solo comprobaba que la variable no estuviera vacia, asi
que devolvio True; el cliente colapsaba el 401 en ``llm_unavailable``; y el
arnes ejecuto **354 turnos** marcandolos "sin medir". Nadie dijo en ningun
momento "tus credenciales estan mal".

Dos defectos, no uno:

1. **Disponible != utilizable.** Una comprobacion de presencia acepta un
   placeholder. La unica forma de saber si una credencial sirve es usarla.
2. **Todo fallo del proveedor se llamaba igual.** Timeout, cuota agotada, 429 y
   401 caian en el mismo codigo, asi que el artefacto no podia distinguir
   "repite la corrida" de "arregla tu credencial".

QUE HACE ESTO

Una sola llamada, la mas barata posible, y una clasificacion explicita:

    ok               el proveedor responde y el modelo existe
    sin_clave        la variable no esta puesta
    placeholder      la variable tiene forma de plantilla, no de credencial
    auth_invalid     el proveedor RECHAZO la credencial  -> no se arregla esperando
    quota_exhausted  sin saldo                            -> no se arregla esperando
    rate_limited     429                                  -> reintentar mas tarde
    timeout / red    no hubo respuesta                    -> reintentar mas tarde
    modelo           el modelo configurado no existe

La clave NUNCA se imprime, ni entera ni parcial. Solo se reporta su forma:
longitud, si tiene espacios y si encaja con un patron de plantilla.

    python evals/fase9_llm_preflight.py
    python evals/fase9_llm_preflight.py --json
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

ENV_VAR = "ANDES_LLM_API_KEY"

# Formas que delatan una plantilla en vez de una credencial. La lista es corta a
# proposito: esto NO es un validador de claves, es un filtro de despistes obvios
# para no gastar una llamada —ni cientos de turnos— en algo que no puede servir.
_PLACEHOLDER_MARKERS = (
    "tu_key", "tu-key", "your_key", "your-key", "yourkey", "api_key_here",
    "changeme", "change_me", "replace", "placeholder", "xxxx", "<", ">",
    "dummy", "ejemplo", "example", "test_key",
)
_MIN_PLAUSIBLE_LEN = 20


def key_shape() -> dict[str, Any]:
    """Forma de la credencial, nunca su valor."""
    raw = os.environ.get(ENV_VAR)
    if raw is None:
        return {"present": False, "verdict": "sin_clave"}
    value = raw.strip()
    if not value:
        return {"present": False, "verdict": "sin_clave"}
    low = value.lower()
    parece = any(m in low for m in _PLACEHOLDER_MARKERS) or len(value) < _MIN_PLAUSIBLE_LEN
    return {
        "present": True,
        "length": len(value),
        "has_whitespace": value != raw or any(c.isspace() for c in value),
        "looks_like_placeholder": parece,
        "verdict": "placeholder" if parece else "forma_plausible",
    }


def _classify(exc: Exception) -> tuple[str, str]:
    """Traduce el fallo a una categoria accionable."""
    from app.assistant.orchestrator.llm.client import LlmError

    if isinstance(exc, LlmError):
        code = exc.code or ""
        return {
            "llm_auth_invalid": ("auth_invalid",
                                 "el proveedor RECHAZO la credencial"),
            "llm_quota_exhausted": ("quota_exhausted",
                                    "la cuenta no tiene saldo"),
            "llm_rate_limited": ("rate_limited",
                                 "429: reintentar mas tarde"),
            "llm_invalid_json": ("ok_con_formato",
                                 "respondio, pero el JSON no encaja"),
        }.get(code, ("proveedor_no_disponible", "sin respuesta util del proveedor"))
    texto = str(exc).lower()
    if "model" in texto and ("not found" in texto or "does not exist" in texto):
        return "modelo_inexistente", "el modelo configurado no existe"
    return "error_inesperado", type(exc).__name__


def preflight() -> dict[str, Any]:
    """Una llamada minima. Barata a proposito: lo que se prueba es la puerta."""
    from evals.fase81g_closure import _base_env

    _base_env()
    forma = key_shape()
    out: dict[str, Any] = {"env_var": ENV_VAR, "key_shape": forma}

    if forma["verdict"] == "sin_clave":
        out["verdict"] = "sin_clave"
        out["advice"] = (f"{ENV_VAR} no esta definida en este proceso. "
                         "Exportala en la shell desde la que ejecutas.")
        return out
    if forma["verdict"] == "placeholder":
        # No se gasta la llamada: una plantilla no puede funcionar.
        out["verdict"] = "placeholder"
        out["advice"] = (
            f"{ENV_VAR} tiene forma de plantilla (longitud {forma['length']}), "
            "no de credencial. Ninguna corrida con esto medira nada.")
        return out

    from app.assistant.orchestrator.llm.client import OpenAICompatibleClient
    from app.assistant.orchestrator.llm.config import load_llm_settings

    settings = load_llm_settings()
    out["model"] = getattr(settings, "planner_model", None)
    inicio = time.perf_counter()
    try:
        client = OpenAICompatibleClient(settings)
        # El prompt mas corto que sigue ejerciendo la ruta real: system + user +
        # structured output. No mide calidad; mide que la puerta abre.
        client.complete_plan_json(
            system='Responde solo JSON: {"plan_id":"p","user_intent":"ping",'
                   '"steps":[],"answer_style":"operational",'
                   '"needs_clarification":false,"reject":false,"reject_code":null,'
                   '"reject_message":null,"scenario":null,"reuse_prior_evidence":null}',
            user="ping")
        out["verdict"] = "ok"
        out["advice"] = "el proveedor responde. Las corridas pueden medir."
    except Exception as exc:  # noqa: BLE001 - el diagnostico ES el resultado
        categoria, detalle = _classify(exc)
        out["verdict"] = categoria
        out["detail"] = detalle
        out["advice"] = {
            "auth_invalid": "La credencial no sirve. Esperar no la arregla.",
            "quota_exhausted": "Sin saldo. Esperar no lo arregla.",
            "rate_limited": "Limitado ahora mismo. Reintentar mas tarde.",
            "proveedor_no_disponible": "Sin respuesta. Reintentar mas tarde.",
            "modelo_inexistente": "Revisa el modelo configurado.",
            "ok_con_formato": "La puerta abre; el formato de respuesta fallo.",
        }.get(categoria, "Revisar el detalle.")
    finally:
        out["latency_ms"] = int((time.perf_counter() - inicio) * 1000)
    return out


# Categorias con las que NO tiene sentido arrancar una corrida larga.
BLOCKING = frozenset({"sin_clave", "placeholder", "auth_invalid",
                      "quota_exhausted", "modelo_inexistente"})


def require_usable_provider(*, context: str) -> dict[str, Any] | None:
    """Puerta para corridas largas. Devuelve el diagnostico si hay que abortar.

    Pensada para llamarse ANTES de gastar turnos: 354 de ellos se fueron por no
    tener esta funcion.
    """
    resultado = preflight()
    if resultado["verdict"] in BLOCKING:
        print(f"\n*** {context}: ABORTADO ANTES DE GASTAR TURNOS ***")
        print(f"    diagnostico: {resultado['verdict']}")
        print(f"    {resultado.get('advice')}")
        forma = resultado.get("key_shape") or {}
        if forma.get("present"):
            print(f"    forma de la clave: longitud={forma.get('length')} "
                  f"espacios={forma.get('has_whitespace')} "
                  f"parece_plantilla={forma.get('looks_like_placeholder')}")
        return resultado
    if resultado["verdict"] != "ok":
        print(f"\n[{context}] aviso: {resultado['verdict']} — "
              f"{resultado.get('advice')}")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="preflight del proveedor LLM")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    resultado = preflight()
    if args.json:
        print(json.dumps(resultado, ensure_ascii=False, indent=2))
        return 0 if resultado["verdict"] == "ok" else 1

    forma = resultado["key_shape"]
    print(f"variable   : {resultado['env_var']}")
    if forma.get("present"):
        print(f"forma      : longitud={forma['length']} "
              f"espacios={forma['has_whitespace']} "
              f"parece_plantilla={forma['looks_like_placeholder']}")
    else:
        print("forma      : AUSENTE")
    if resultado.get("model"):
        print(f"modelo     : {resultado['model']}")
    print(f"latencia   : {resultado.get('latency_ms', 0)} ms")
    print(f"VEREDICTO  : {resultado['verdict']}")
    if resultado.get("detail"):
        print(f"detalle    : {resultado['detail']}")
    print(f"accion     : {resultado.get('advice')}")
    return 0 if resultado["verdict"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
