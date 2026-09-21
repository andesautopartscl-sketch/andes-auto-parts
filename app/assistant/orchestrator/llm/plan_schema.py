"""JSON Schema for orchestrator Plan — OpenAI strict + PlanValidator allowlist."""
from __future__ import annotations

import copy

from app.assistant.orchestrator.arg_schema import ArgSchemaError, validate_tool_args
from app.assistant.orchestrator.catalog import ALLOWED_TOOLS, MAX_STEPS
from app.assistant.orchestrator.tool_contracts import TOOL_CONTRACTS

# OpenAI strict mode requires additionalProperties=false and every property listed in required.
# Optional values use nullable unions; LlmPlanner strips nulls before PlanValidator.

# Tipo del contrato -> tipo JSON del schema estricto. Una fecha viaja como
# string (DATE_RE valida la forma); la union con null es lo que hace opcional a
# una clave, porque strict obliga a listarlas TODAS en `required`.
_SCALAR_TYPES: dict[str, list[str]] = {
    "string": ["string", "null"],
    "date": ["string", "null"],
    "int": ["integer", "null"],
}

# Argumentos estructurados: el contrato solo puede decir "array", y un array
# desnudo dejaria al modelo enviar filas que el Gateway no sabe leer. La forma
# vive aqui y el builder la usa tal cual en vez de derivarla.
_STRUCTURED_PROPS: dict[str, dict] = {
    "items": {
        "type": ["array", "null"],
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["codigo", "cantidad", "marca", "bodega"],
            "properties": {
                "codigo": {"type": "string"},
                "cantidad": {"type": "integer"},
                "marca": {"type": ["string", "null"]},
                "bodega": {"type": ["string", "null"]},
            },
        },
    },
}


def _build_argument_props() -> dict[str, dict]:
    """Deriva el juego de argumentos emitibles desde TOOL_CONTRACTS.

    Antes esta tabla se escribia a mano, y esa es exactamente la razon por la
    que 8.6 y 8.8 anadieron `oem`, `modelo`, `cliente` y `group_by` al contrato
    sin anadirlos aqui. Con `strict=True` y `additionalProperties=False` el
    proveedor restringe la decodificacion al schema: una clave ausente no es
    "opcional", es IMPOSIBLE de emitir.

    Medido con LLM real (tercera A/B, 2026-09-20): O01 y O04 preguntan por un
    OEM, el modelo elige `get_equivalences` correctamente, el prompt le dice
    "requiere al menos uno de: oem|codigo" — y `oem` no existia en el schema.
    Dos reintentos, ninguna llamada, fallback. O02 y O03 preguntan por `codigo`,
    que si era emitible, y pasan. La particion es 4/4 en los dos brazos.

    Derivar en vez de copiar cierra la clase entera: una tool nueva que declare
    un argumento nuevo lo obtiene aqui el mismo dia.
    """
    props: dict[str, dict] = {}
    declared_type: dict[str, str] = {}
    for tool in sorted(ALLOWED_TOOLS):
        spec = TOOL_CONTRACTS.get(tool) or {}
        fields = {**(spec.get("required") or {}), **(spec.get("optional") or {})}
        for key, meta in sorted(fields.items()):
            kind = str((meta or {}).get("type") or "")
            # Una clave compartida por dos tools con tipos distintos no se puede
            # representar en un schema unico: es un defecto del contrato, no del
            # schema, y callarlo produciria un tipo arbitrario segun el orden.
            if key in declared_type and declared_type[key] != kind:
                raise RuntimeError(
                    f"tipo en conflicto para '{key}': "
                    f"{declared_type[key]} vs {kind} (tool {tool})")
            declared_type[key] = kind
            if key in _STRUCTURED_PROPS:
                props[key] = copy.deepcopy(_STRUCTURED_PROPS[key])
                continue
            json_type = _SCALAR_TYPES.get(kind)
            if json_type is None:
                raise RuntimeError(
                    f"tipo de contrato sin correspondencia en el schema: "
                    f"'{kind}' ({tool}.{key})")
            props[key] = {"type": list(json_type)}
    return props


_ARGUMENT_PROPS: dict = _build_argument_props()

PLAN_JSON_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "plan_id",
        "user_intent",
        "steps",
        "answer_style",
        "needs_clarification",
        "reject",
        "reject_code",
        "reject_message",
        "scenario",
        "reuse_prior_evidence",
    ],
    "properties": {
        "plan_id": {"type": "string"},
        "user_intent": {"type": "string"},
        "answer_style": {
            "type": "string",
            "enum": ["operational", "confidential", "financial"],
        },
        "needs_clarification": {"type": "boolean"},
        "reject": {"type": "boolean"},
        "reject_code": {"type": ["string", "null"]},
        "reject_message": {"type": ["string", "null"]},
        "scenario": {"type": ["string", "null"]},
        "reuse_prior_evidence": {"type": ["boolean", "null"]},
        "steps": {
            "type": "array",
            "maxItems": MAX_STEPS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["step", "tool", "arguments", "reason", "depends_on"],
                "properties": {
                    "step": {"type": "integer", "minimum": 1},
                    "tool": {"type": "string", "enum": sorted(ALLOWED_TOOLS)},
                    "arguments": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": sorted(_ARGUMENT_PROPS.keys()),
                        "properties": _ARGUMENT_PROPS,
                    },
                    "reason": {"type": ["string", "null"]},
                    "depends_on": {
                        "type": ["array", "null"],
                        "items": {"type": "integer", "minimum": 1},
                    },
                },
            },
        },
    },
}


def plan_response_format() -> dict:
    """OpenAI-compatible response_format for structured Plan JSON."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "andes_orchestrator_plan",
            "strict": True,
            "schema": PLAN_JSON_SCHEMA,
        },
    }


AGENT_DECISION_JSON_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "action",
        "tool",
        "arguments",
        "reason",
        "draft_reply",
        "claims",
        "calculations",
        "proposed_requirements",
        "unresolved",
    ],
    "properties": {
        "action": {
            "type": "string",
            "enum": ["call_tool", "final_answer", "clarify", "reject"],
        },
        "tool": {"type": ["string", "null"]},
        "arguments": {
            "type": "object",
            "additionalProperties": False,
            "required": sorted(_ARGUMENT_PROPS.keys()),
            "properties": _ARGUMENT_PROPS,
        },
        "reason": {"type": ["string", "null"]},
        "draft_reply": {"type": ["string", "null"]},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "text", "evidence_ids", "justification"],
                "properties": {
                    "kind": {"type": "string", "enum": ["dato", "inferencia"]},
                    "text": {"type": "string"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "justification": {"type": ["string", "null"]},
                },
            },
        },
        "calculations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "op", "inputs", "result"],
                "properties": {
                    "id": {"type": "string"},
                    "op": {"type": "string", "enum": ["min", "max", "sum", "diff", "ratio", "count"]},
                    "inputs": {"type": "array", "items": {"type": "string"}},
                    "result": {"type": "number"},
                },
            },
        },
        "proposed_requirements": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "type"],
                "properties": {
                    "id": {"type": ["string", "null"]},
                    "type": {"type": "string"},
                },
            },
        },
        "unresolved": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "type", "reason"],
                "properties": {
                    "id": {"type": ["string", "null"]},
                    "type": {"type": "string"},
                    "reason": {
                        "type": "string",
                        "enum": [
                            "empty",
                            "permission",
                            "not_found",
                            "tool_error",
                            "cannot_resolve",
                        ],
                    },
                },
            },
        },
    },
}


def _analysis_schema() -> dict:
    """AGENT_DECISION_JSON_SCHEMA ampliado con la escalera analitica de 8.5.

    Se construye por copia y NO se muta el schema base: con el flag apagado, lo
    que viaja al proveedor tiene que ser byte a byte lo de siempre.
    """
    import copy

    from app.assistant.orchestrator.analysis import ASSUMPTION_KINDS

    schema = copy.deepcopy(AGENT_DECISION_JSON_SCHEMA)
    props = schema["properties"]
    props["claims"]["items"]["properties"]["kind"]["enum"] = [
        "dato", "inferencia", "calculo", "supuesto", "proyeccion", "recomendacion"]
    props["claims"]["items"]["properties"]["assumption_ids"] = {
        "type": "array", "items": {"type": "string"}}
    props["claims"]["items"]["required"] = [
        "kind", "text", "evidence_ids", "justification", "assumption_ids"]
    props["calculations"]["items"]["properties"]["op"]["enum"] = [
        "min", "max", "sum", "diff", "ratio", "count", "mul"]
    props["assumptions"] = {
        "type": "array",
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["id", "kind", "value", "basis"],
            "properties": {
                "id": {"type": "string"},
                "kind": {"type": "string", "enum": sorted(ASSUMPTION_KINDS)},
                "value": {"type": "number"},
                "basis": {"type": "string", "enum": ["user_request", "default"]},
            },
        },
    }
    schema["required"] = list(schema.get("required") or []) + ["assumptions"]
    return schema


def agent_decision_response_format(*, analytical: bool = False) -> dict:
    """OpenAI-compatible response_format for a single-act AgentDecision.

    El schema ampliado viaja SOLO en un turno analitico. Con strict=True el
    proveedor exige toda propiedad listada en `required`, asi que ampliarlo
    siempre obligaba a emitir `assumptions` y `assumption_ids` en CADA decision,
    aunque la pregunta fuera "Stock del 2404". Eso no era una ampliacion pasiva:
    cambiaba todos los turnos.
    """
    from app.assistant.orchestrator.agent_config import analysis_enabled

    schema = (_analysis_schema() if (analytical and analysis_enabled())
              else AGENT_DECISION_JSON_SCHEMA)
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "andes_agent_decision",
            "strict": True,
            "schema": schema,
        },
    }


# --------------------------------------------------------------- drift 8.x
# Muestras por tipo para sondear al validador. No se leen sus reglas de negocio:
# solo interesa si la clave es CONOCIDA, y `_require_only` lo dice con un
# mensaje propio. "X" pasa CODE_RE, asi que sirve tambien donde el contrato
# pide product_code.
_PROBE_VALUES: dict[str, object] = {
    "string": "X",
    "date": "2026-01-01",
    "int": 1,
    "array": [{"codigo": "X", "cantidad": 1}],
}

# El validador acepta a proposito claves que el contrato NO declara al modelo.
# `tipos` es la unica: dejar que el modelo elija que cuenta como venta le
# permitiria meter orden_compra y volcar el signo del resultado. Se declara aqui
# para que la excepcion sea explicita y no pueda crecer en silencio.
INTERNAL_ONLY_ARGS: dict[str, frozenset[str]] = {
    "get_sales": frozenset({"tipos"}),
}

_UNKNOWN_ARG_MARK = "unknown argument"


def contract_schema_drift() -> list[dict[str, str]]:
    """Desalineaciones entre contrato, schema, normalizador y validador.

    Cuatro ejes — los cuatro eslabones por los que pasa un argumento entre que
    el prompt lo anuncia y el Gateway lo recibe. Cada uno puede romper una tool
    en produccion sin que ninguna prueba determinista lo note, porque cada lado
    es coherente consigo mismo:

    1. contrato -> schema    un argumento que el prompt anuncia y el modelo no
                             puede emitir. Es el defecto de O01/O04.
    2. contrato -> normalizador  un argumento emitido que normalize_agent_args
                             descarta antes de validar. Se encontro arreglando
                             el eje 1: `oem` tambien estaba fuera de la lista de
                             passthrough, asi que hacerlo emitible por si solo no
                             habria desbloqueado nada.
    3. contrato -> validador un argumento anunciado que arg_schema rechaza como
                             desconocido.
    4. schema -> contrato    una propiedad emitible que ninguna tool declara:
                             clave muerta que el modelo puede rellenar y nadie
                             lee. Se comprueba contra la UNION de los contratos,
                             porque `arguments` es un objeto compartido por todas
                             las tools: que `oem` no pertenezca a get_sales no es
                             drift, es el diseno.

    Devuelve filas {tool, arg, side, detail} — vacia cuando todo cuadra.
    """
    from app.assistant.orchestrator.tool_contracts import normalize_agent_args
    findings: list[dict[str, str]] = []
    emittable = set(AGENT_DECISION_JSON_SCHEMA["properties"]["arguments"]["properties"])
    union: set[str] = set()

    for tool in sorted(ALLOWED_TOOLS):
        spec = TOOL_CONTRACTS.get(tool) or {}
        fields = {**(spec.get("required") or {}), **(spec.get("optional") or {})}
        # El ancla condicional se declara una sola vez, en `any_of`, y tiene que
        # ser tan emitible como cualquier otro argumento: es el unico que el
        # modelo esta OBLIGADO a usar.
        anchors = tuple(spec.get("any_of") or ())
        for key in anchors:
            if key not in fields:
                findings.append({"tool": tool, "arg": key, "side": "contract",
                                 "detail": "ancla en any_of que no es un argumento declarado"})
        union |= set(fields)

        for key in sorted(fields):
            if key not in emittable:
                findings.append({
                    "tool": tool, "arg": key, "side": "generation_schema",
                    "detail": "declarado en tool_contracts y NO emitible "
                              "(strict + additionalProperties=false)"})
            meta = fields[key] or {}
            kind = str(meta.get("type") or "")
            probe = _PROBE_VALUES.get(kind)
            if kind == "int":
                # Dentro del rango que declara el propio contrato: un sondeo
                # fuera de rango mediria la validacion, no la correspondencia.
                probe = max(1, int(meta.get("min") or 1))
            if probe is None:
                findings.append({"tool": tool, "arg": key, "side": "contract",
                                 "detail": f"tipo '{kind}' sin muestra para sondear"})
                continue
            # user_message=None: si no, el normalizador descarta toda fecha que
            # no aparezca literalmente en la pregunta, y eso es una regla de
            # antialucinacion, no un desajuste de contrato.
            if key not in normalize_agent_args(tool, {key: probe}, user_message=None):
                findings.append({
                    "tool": tool, "arg": key, "side": "normalizer",
                    "detail": "declarado en tool_contracts y descartado por "
                              "normalize_agent_args antes de validar"})
            try:
                validate_tool_args(tool, {key: probe})
            except ArgSchemaError as exc:
                if _UNKNOWN_ARG_MARK in str(exc).lower():
                    findings.append({
                        "tool": tool, "arg": key, "side": "validator",
                        "detail": "declarado en tool_contracts y rechazado por "
                                  "arg_schema como desconocido"})
            except Exception:  # noqa: BLE001 - un fallo de sondeo no es drift
                pass

    for key in sorted(emittable - union):
        findings.append({"tool": "*", "arg": key, "side": "contract",
                         "detail": "emitible por el schema y no declarado por "
                                   "ninguna tool: clave muerta"})
    return findings


def format_drift(findings: list[dict[str, str]]) -> str:
    """Diagnostico legible: tool + argumento + lado donde falta."""
    if not findings:
        return "sin drift"
    return "\n".join(
        f"  {row['tool']}.{row['arg']}  falta en: {row['side']}  — {row['detail']}"
        for row in findings)
