"""FASE 8.1 — Evidence-aware AgentLoop flags and limits.

Default OFF. AgentLoop runs only when AGENT=1 AND LLM soft-enable is valid.
Never changes ToolRunner allowlist, WRITE_TOOLS, Gateway, or ERP ACL.
"""
from __future__ import annotations

import os

from app.assistant.orchestrator.llm.config import LlmSettings, load_llm_settings

MAX_TOOL_CALLS = 5
# FASE 8.2D — 6 seguia siendo un paso de ficcion. Con el ratio real de tokenizacion
# (CHARS_PER_TOKEN, medido) y la semantica real del guard (se evalua ANTES de cada
# decision sobre el acumulado, asi que siempre se concede un rebase), el techo de
# tokens financia 4 decisiones en el mejor caso. El camino mas largo por diseno
# —3 finales bloqueados + la tool que los resuelve + el final— son 5, que es
# tambien MAX_BLOCKED_FINALS_NO_PROGRESS + 2. Las dos restricciones se cruzan
# exactamente en 5, asi que 5 es el unico valor que no miente por ningun lado.
# BAJA el techo, no lo sube.
MAX_AGENT_STEPS = 5
MAX_SECONDS = 40.0
MAX_OUTPUT_TOKENS = 800
MAX_COST_PER_TURN = 0.05
MAX_TOOL_RESULT_CHARS = 4000
MAX_EVIDENCE_PROMPT_CHARS = 6000
MAX_SAME_CALL = 1
# FASE 8.1G — anti-thrashing is structural, not "one empty result ends the turn".
# MAX_EMPTY_FOLLOWUPS now bounds follow-up calls to the SAME tool that already
# returned empty and cannot cover any uncovered requirement. A different tool
# that covers an uncovered requirement is never blocked by it.
# 1 cerraba la tool en el PRIMER vacio, asi que "search_catalog q=filtro" sin
# resultados impedia reintentar con "filtro de aceite". 2 permite exactamente un
# refinamiento; si tambien vuelve vacio no hay progreso y el ledger corta.
MAX_EMPTY_FOLLOWUPS = 2
# Consecutive EXECUTED calls that yield neither coverage nor new evidence.
MAX_NO_PROGRESS_STEPS = 2
# Consecutive decisions refused by the progress ledger before giving up.
MAX_REJECTED_DECISIONS = 3
# Consecutive final_answer decisions blocked by GoalCoverage WITHOUT any coverage
# change. Measured on T07: successful runs call the second tool after exactly 2
# blocked finals, so 2 would cut off the cases that work. At 3 the model has
# demonstrated it will not call the covering tool, and a partial answer is the
# correct outcome — not a fallback.
MAX_BLOCKED_FINALS_NO_PROGRESS = 3
MAX_CONSECUTIVE_TOOL_ERRORS = 2
MAX_DECISION_RETRIES = 1
# When ANDES_LLM_COST_* is unset, cap in+out tokens instead of USD.
#
# FASE 8.6 — 8000 dejo de ser un numero y paso a ser una consecuencia.
#
# El problema medido con LLM real: T09 (3 tools + un final bloqueado) necesitaba
# 5 decisiones, gasto 9858 tokens y el guard lo tumbo con agent_token_budget
# aunque TODOS sus requirements estaban cubiertos. El techo estaba matando
# trabajo correcto.
#
# La relacion correcta es esta: MAX_AGENT_STEPS no es un numero libre, se deriva
# del camino mas largo por diseno (MAX_BLOCKED_FINALS_NO_PROGRESS + tool + final).
# Si el sistema declara que ese camino existe, el presupuesto tiene que pagarlo;
# si no, el limite declarado es decorativo y el fallback aparece en turnos
# legitimos. Derivacion: envolvente observada 8600 tokens en 4 decisiones (T09
# sin el bloque de analisis), mas una quinta decision de la misma forma (~2500)
# = ~11 100. 12 000 cubre eso con margen.
#
# No es "subir el techo porque molestaba": es alinearlo con lo que el propio
# sistema declara que puede hacer. El test correspondiente falla si alguien sube
# MAX_AGENT_STEPS sin subir esto, o encoge esto sin bajar aquello.
TOKEN_BUDGET_FALLBACK = 12000
# Caracteres por token medidos, no el 4 teorico. Derivado de la corrida 8.2C con
# LLM real: 3 decisiones de K06 suman 16 654 caracteres de prompt y el proveedor
# facturo 6 180 tokens -> 2.695. El 4 teorico infravaloraba el consumo un 48% y
# por eso toda cota construida sobre el salia optimista. Castellano + JSON
# tokenizan peor que prosa inglesa; si cambia el modelo hay que volver a medirlo.
CHARS_PER_TOKEN = 2.7
# Envoltura del user prompt sin evidencia (medida: 443 caracteres con goal_note y
# loop_note vacios; 600 deja margen para ambos).
USER_ENVELOPE_CHARS = 600


def cost_is_priced() -> bool:
    """True cuando ANDES_LLM_COST_* permite estimar USD por turno.

    Medido sobre 114 turnos con LLM real: cost_est fue None en 114/114, es decir
    MAX_COST_PER_TURN nunca llego a evaluarse y el guard activo fue siempre el
    techo de tokens. Exponerlo evita razonar sobre un limite que no actua.
    """
    # FASE 8.2D — antes esto preguntaba por LlmSettings.cost_per_1k_in, un campo
    # que NUNCA existio: el hasattr devolvia False siempre, asi que la funcion
    # respondia "sin precios" aunque el operador los hubiera configurado, y
    # budget_snapshot declaraba token_budget como guard activo mientras el guard
    # real era el coste. Las tarifas viven en el entorno y las lee metrics.py;
    # esta funcion tiene que mirar la misma fuente y no otra.
    from app.assistant.orchestrator.metrics import _cost_per_1k

    return _cost_per_1k("input") is not None or _cost_per_1k("output") is not None


def _decisions_under_budget(pack_chars_per_step: int, output_tokens: int) -> int:
    """Cuantas decisiones concede el guard, simulandolo como se evalua de verdad.

    Dos correcciones sobre el modelo anterior, ambas en direccion pesimista→real:

    1. El guard se evalua ANTES de cada decision sobre el acumulado, no despues.
       Una decision se permite mientras lo YA gastado quepa, asi que el turno
       siempre se concede un rebase: con 8000 de techo, un acumulado de 7999
       autoriza una decision mas. Dividir techo entre coste unitario —lo que
       hacia la version anterior— ignora ese rebase y cuenta de menos.
    2. El pack de evidencia crece con cada tool ejecutada; no es ni constante ni
       cero. Se modela creciendo ``pack_chars_per_step`` por decision y topado
       en MAX_EVIDENCE_PROMPT_CHARS, que es lo que hace prompt_pack().
    """
    from app.assistant.orchestrator.llm.agent_prompts import build_agent_system_prompt

    system_chars = len(build_agent_system_prompt())
    spent = 0
    granted = 0
    # Cota dura: sin ella un pack de 0 y un techo enorme no terminarian.
    while granted < 64:
        if spent >= TOKEN_BUDGET_FALLBACK:
            break
        granted += 1
        pack = min(pack_chars_per_step * (granted - 1), MAX_EVIDENCE_PROMPT_CHARS)
        prompt_chars = system_chars + USER_ENVELOPE_CHARS + pack
        spent += int(prompt_chars / CHARS_PER_TOKEN) + output_tokens
    return max(1, granted)


def _max_decision_cost() -> int:
    """Coste de la decision mas cara posible: system + envoltura + pack lleno."""
    from app.assistant.orchestrator.llm.agent_prompts import build_agent_system_prompt

    chars = len(build_agent_system_prompt()) + USER_ENVELOPE_CHARS + MAX_EVIDENCE_PROMPT_CHARS
    return int(chars / CHARS_PER_TOKEN) + MAX_OUTPUT_TOKENS


def decisions_affordable() -> tuple[int, int]:
    """(peor caso, mejor caso) de decisiones que el techo de tokens puede pagar.

    Derivado, no adivinado, y calibrado: tamano real del system prompt, ratio de
    tokenizacion medido y la semantica real del guard. Sirve para que
    MAX_AGENT_STEPS no pueda alejarse en silencio de lo que el presupuesto paga.

    El "mejor caso" ya no es la ficcion de un turno que nunca recoge evidencia:
    aun sin pack, el system prompt se reenvia integro en cada decision y es la
    partida dominante del coste (~77% del prompt de un turno de 3 decisiones).
    """
    worst = _decisions_under_budget(MAX_TOOL_RESULT_CHARS, MAX_OUTPUT_TOKENS)
    best = _decisions_under_budget(0, 200)
    return worst, best


def budget_snapshot() -> dict[str, object]:
    """Los limites y cual de ellos manda. Sin secretos; apto para audit/metrica."""
    worst, best = decisions_affordable()
    priced = cost_is_priced()
    return {
        "max_agent_steps": MAX_AGENT_STEPS,
        "max_tool_calls": MAX_TOOL_CALLS,
        "max_seconds": MAX_SECONDS,
        "max_evidence_prompt_chars": MAX_EVIDENCE_PROMPT_CHARS,
        "max_tool_result_chars": MAX_TOOL_RESULT_CHARS,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "token_budget_fallback": TOKEN_BUDGET_FALLBACK,
        "max_cost_per_turn": MAX_COST_PER_TURN,
        "cost_priced": priced,
        "active_guard": "cost_per_turn" if priced else "token_budget",
        "decisions_affordable_worst": worst,
        "decisions_affordable_best": best,
        "chars_per_token": CHARS_PER_TOKEN,
        # El guard se evalua ANTES de cada decision sobre el acumulado, asi que
        # autoriza una decision mas mientras lo gastado quepa: el gasto real de un
        # turno puede terminar POR ENCIMA del techo declarado. Medido con LLM
        # real: T07 cerro en 8214 y T09 en 8614 con TOKEN_BUDGET_FALLBACK=8000,
        # y ambos eran correctos. Declarar solo 8000 haria parecer un desbordo lo
        # que es el funcionamiento previsto.
        "effective_token_ceiling": TOKEN_BUDGET_FALLBACK + _max_decision_cost(),
        # Si los pasos declarados superan lo que el techo paga ni en el mejor
        # caso, el limite declarado es decorativo y hay que verlo en el audit.
        "steps_exceed_budget": MAX_AGENT_STEPS > best,
    }


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def provenance_enforced() -> bool:
    """FASE 8.2 — claim provenance. Default OFF (observation mode).

    A claim carries ``evidence_ids``; until now nobody checked that its figures
    actually came from the evidence it cites. Enforcing that immediately would
    drop claims that are factually right but sloppily attributed, so the check
    runs in observation mode first: violations are counted and reported, the
    claim is not discarded. Flip ANDES_ASSISTANT_PROVENANCE_ENFORCE=1 once the
    measured violation rate says enforcing is safe.
    """
    return _env_bool("ANDES_ASSISTANT_PROVENANCE_ENFORCE", False)


def analysis_enabled() -> bool:
    """FASE 8.5 — escalera analitica (dato/calculo/supuesto/proyeccion/recomendacion).

    Default OFF, y esa decision viene de una medicion, no de prudencia generica.
    La corrida con LLM real posterior a 8.5 mostro CERO usos de la escalera, y la
    causa no era el modelo: el structured output schema fijaba kind a
    dato|inferencia y op a las seis operaciones viejas, sin propiedad
    'assumptions' y con additionalProperties=False. El modelo era FISICAMENTE
    incapaz de emitir un peldano nuevo; la corrida no midio la capacidad, midio
    la reja.

    Con el flag apagado el prompt y el schema son byte a byte los de antes, asi
    que el riesgo en produccion es cero. Encendido, habilita la capacidad y
    permite medir su impacto en A/B contra la misma corrida. Se activa cuando la
    medicion diga que el modelo la usa bien, no antes.
    """
    return _env_bool("ANDES_ASSISTANT_ANALYSIS_ENABLED", False)


def period_resolution_enabled() -> bool:
    """FASE 8.x — traduccion determinista de periodos naturales a fechas.

    Por que hace falta, medido contra el ERP real: "las ventas de enero a marzo
    de 2026" devuelve CERO, pero el sistema no podia expresar esa ventana, asi
    que la llamada salia sin filtro y traia dos documentos de 2026-04-07. Abril.
    Cifras grounded, respuesta falsa. En el dataset, 7 de 76 preguntas (9,2%)
    nombran el periodo en lenguaje natural y solo UNA usa formato ISO.

    Por que apagado por defecto: cambia los argumentos con los que se llama a las
    tools en esas 7 preguntas, y por tanto las respuestas. Este proyecto ya pago
    una vez el precio de un cambio no medido —una reescritura de prompt tumbo T07
    de 9/10 a 0/10 en 8.1I.2— asi que una capacidad entra por una puerta que se
    puede abrir, cerrar y medir en A/B, y se enciende cuando la medicion lo
    respalde.

    Apagado, ``resolve_period`` no se llama: el comportamiento es byte a byte el
    de antes. El modulo en si no depende del flag y sus pruebas deterministas
    corren siempre.
    """
    return _env_bool("ANDES_ASSISTANT_PERIOD_RESOLUTION", False)


def agent_enabled() -> bool:
    """Raw flag. Default 0. Does not imply the loop will run."""
    return _env_bool("ANDES_ASSISTANT_AGENT_ENABLED", False)


def agent_loop_allowed(settings: LlmSettings | None = None) -> bool:
    """True only when AGENT=1 and the same soft-enable gate as LlmPlanner.

    If AGENT=1 but NL/ORCH/env/key are not valid: False → current orchestrator path.
    """
    if not agent_enabled():
        return False
    s = settings or load_llm_settings()
    return bool(s.soft_llm_allowed)


def budget_exceeded(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    cost_est: float | None,
) -> bool:
    if cost_est is not None and float(cost_est) >= MAX_COST_PER_TURN:
        return True
    if cost_est is None and (int(prompt_tokens) + int(completion_tokens)) >= TOKEN_BUDGET_FALLBACK:
        return True
    return False
