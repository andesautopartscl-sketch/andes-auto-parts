"""FASE 8.1B — agent-facing argument contracts.

Validation authority remains arg_schema.validate_tool_args.
Descriptions match Gateway 0.11.0 registry. Do not import andes_agent.
"""
from __future__ import annotations

import re
from typing import Any

from app.assistant.orchestrator.arg_schema import CODE_RE, DATE_RE
from app.assistant.orchestrator.catalog import ALLOWED_TOOLS

# Closed contracts. required/optional keys must stay aligned with arg_schema.
TOOL_CONTRACTS: dict[str, dict[str, Any]] = {
    "search_catalog": {
        "description": "Search products in the catalog",
        "required": {"q": {"type": "string", "min": 2, "max": 80}},
        "optional": {"limit": {"type": "int", "min": 1, "max": 25}},
    },
    "get_product": {
        "description": "Get one product by code",
        "required": {"codigo": {"type": "string", "pattern": "product_code"}},
        "optional": {},
    },
    "get_inventory": {
        "description": "Get current inventory for a product",
        "required": {"codigo": {"type": "string", "pattern": "product_code"}},
        "optional": {
            "marca": {"type": "string"},
            "bodega": {"type": "string"},
        },
    },
    "check_stock": {
        "description": "Check availability for cart items (READ-ONLY)",
        "required": {"items": {"type": "array"}},
        "optional": {},
    },
    "get_stock_movements": {
        "description": "Read stock movements (kardex) for a product",
        "required": {"codigo": {"type": "string", "pattern": "product_code"}},
        "optional": {
            "fecha_desde": {"type": "date"},
            "fecha_hasta": {"type": "date"},
            "limit": {"type": "int", "min": 1, "max": 50},
        },
    },
    "get_ingresos": {
        "description": "Read warehouse receipts",
        "required": {"codigo": {"type": "string", "pattern": "product_code"}},
        "optional": {
            "fecha_desde": {"type": "date"},
            "fecha_hasta": {"type": "date"},
            "limit": {"type": "int", "min": 1, "max": 20},
            "proveedor": {"type": "string"},
            "numero": {"type": "string"},
        },
    },
    "get_purchase_orders": {
        "description": "Read supplier purchase orders",
        "required": {},
        "optional": {
            "numero": {"type": "string"},
            "codigo": {"type": "string"},
            "estado": {"type": "string"},
            "proveedor": {"type": "string"},
            "fecha_desde": {"type": "date"},
            "fecha_hasta": {"type": "date"},
            "limit": {"type": "int", "min": 1, "max": 20},
        },
    },
    "get_equivalences": {
        "description": "Cross-reference OEM / internal / alternative codes",
        "required": {},
        # Sin ancla la consulta devolveria un recorte arbitrario del catalogo que
        # el agente presentaria como "equivalencias". Se declara para que el
        # modelo lo sepa ANTES de llamar, no despues de que lo rechacen.
        "any_of": ("oem", "codigo"),
        "optional": {
            "oem": {"type": "string"},
            "codigo": {"type": "string", "pattern": "product_code"},
            "marca": {"type": "string"},
            "modelo": {"type": "string"},
            "limit": {"type": "int", "min": 1, "max": 20},
        },
    },
    "get_sales": {
        "description": "Read aggregated sales (net of credit notes)",
        "required": {},
        "optional": {
            "codigo": {"type": "string", "pattern": "product_code"},
            "cliente": {"type": "string"},
            "estado": {"type": "string"},
            "group_by": {"type": "string"},
            "fecha_desde": {"type": "date"},
            "fecha_hasta": {"type": "date"},
            "limit": {"type": "int", "min": 1, "max": 20},
        },
    },
    "get_orders": {
        "description": "Read customer orders (lines, states, volume)",
        "required": {},
        "optional": {
            "codigo": {"type": "string", "pattern": "product_code"},
            "cliente": {"type": "string"},
            "group_by": {"type": "string"},
            "fecha_desde": {"type": "date"},
            "fecha_hasta": {"type": "date"},
            "limit": {"type": "int", "min": 1, "max": 20},
        },
    },
    "get_customer": {
        "description": "Read customer directory",
        "required": {},
        "optional": {
            "q": {"type": "string"},
            "rut": {"type": "string"},
            "id": {"type": "int", "min": 1, "max": 2_147_483_647},
            "limit": {"type": "int", "min": 1, "max": 20},
        },
    },
    "get_supplier": {
        "description": "Read supplier directory",
        "required": {},
        "optional": {
            "q": {"type": "string"},
            "rut": {"type": "string"},
            "id": {"type": "int", "min": 1, "max": 2_147_483_647},
            "limit": {"type": "int", "min": 1, "max": 20},
        },
    },
    "get_dashboard_kpis": {
        "description": "Read dashboard KPI snapshot",
        "required": {},
        "optional": {
            "periodo": {"type": "string"},
            "fecha_desde": {"type": "date"},
            "fecha_hasta": {"type": "date"},
            "top_limit": {"type": "int", "min": 1, "max": 10},
            "stock_threshold": {"type": "int", "min": 0, "max": 10},
            "stock_limit": {"type": "int", "min": 1, "max": 20},
        },
    },
}

_DIGIT_STR = re.compile(r"^\d+$")


def allowed_arg_keys(tool: str) -> frozenset[str]:
    spec = TOOL_CONTRACTS.get(tool) or {}
    keys = set(spec.get("required") or {}) | set(spec.get("optional") or {})
    return frozenset(keys)


def tool_arg_key_map() -> dict[str, frozenset[str]]:
    return {name: allowed_arg_keys(name) for name in ALLOWED_TOOLS}


# Claves de tipo string con ruta propia en normalize_agent_args: `codigo` se
# coacciona y valida contra CODE_RE, `q` se recorta y puede promoverse a codigo.
# Todas las demas strings del contrato son passthrough recortado.
_STRING_KEYS_WITH_OWN_PATH = frozenset({"codigo", "q"})


def passthrough_string_keys(tool: str) -> tuple[str, ...]:
    """Argumentos string que el normalizador copia tal cual, leidos del contrato.

    Se deriva en vez de enumerarse porque la enumeracion a mano ya fallo: omitia
    los cuatro argumentos de 8.6 y 8.8 y los borraba silenciosamente de la
    llamada.
    """
    spec = TOOL_CONTRACTS.get(tool) or {}
    fields = {**(spec.get("required") or {}), **(spec.get("optional") or {})}
    return tuple(sorted(
        key for key, meta in fields.items()
        if (meta or {}).get("type") == "string"
        and key not in _STRING_KEYS_WITH_OWN_PATH))


# FASE 9.1 — economia de prompt, sin perder una sola senal.
#
# El tipo por defecto es string, y anotarlo era redundante DOS veces: el schema
# de generacion corre en modo strict, asi que `q` ya esta fijado a
# ["string","null"] y el modelo no puede emitir otra cosa aunque el prompt calle.
# Decirselo ademas en el contrato gastaba tokens para repetir lo que el
# decodificador impone. Los tipos que NO son string se siguen anotando porque si
# informan: una fecha lleva formato y un int lleva rango.
#
# Medido sobre las 12 tools: 592 -> 480 tokens por decision (-112), que sobre
# MAX_AGENT_STEPS son 559 tokens de pico. Las descripciones NO se tocan: son la
# senal con la que el modelo elige tool, y la seleccion es la puerta mas
# puntuada del benchmark. Quitarlas ahorraba 155 tokens mas y no vale su riesgo.
_DEFAULT_ARG_TYPE = "string"


def _render_args(fields: dict[str, Any]) -> str:
    return ",".join(
        k if (v or {}).get("type") == _DEFAULT_ARG_TYPE else f"{k}:{(v or {}).get('type')}"
        for k, v in fields.items())


def format_contracts_for_prompt(only: Any = None) -> str:
    """`only` restringe a las tools que el modelo ve. Nombres y contratos se
    filtran juntos: una tool nombrada sin contrato es inllamable."""
    names = sorted(ALLOWED_TOOLS if only is None else (set(ALLOWED_TOOLS) & set(only)))
    lines = []
    for name in names:
        spec = TOOL_CONTRACTS[name]
        req = spec.get("required") or {}
        opt = spec.get("optional") or {}
        line = f"{name}: {spec['description']}."
        # Una clausula vacia ("required=none") no dice nada que el modelo no
        # deduzca de su ausencia, y cuesta lo mismo que una que si dice algo.
        if req:
            line += f" required={_render_args(req)}."
        if opt:
            line += f" optional={_render_args(opt)}."
        # FASE 8.9 — un requisito CONDICIONAL tiene que poder declararse. Antes
        # get_equivalences exigia 'oem' o 'codigo' en codigo imperativo mientras
        # el contrato decia required=none: el modelo llamaba sin ancla, el
        # validador rechazaba, y el aviso de reintento iba vacio porque
        # inspect_arg_fields solo mira `required`. Resultado medido con LLM real:
        # O01 y O04 agotaron los reintentos y cayeron a fallback SIN llamar a la
        # tool. El contrato mentia y el modelo no podia acertar.
        any_of = spec.get("any_of") or ()
        if any_of:
            line += f" requiere al menos uno de: {'|'.join(any_of)}."
        lines.append(line)
    return "\n".join(lines)


def _resolved_period(user_message: str | None) -> Any:
    """La ventana que el mensaje nombra, o None. Detras del flag de 8.x.

    Import perezoso y envuelto: una pregunta no puede morir porque el resolutor
    tropiece con una expresion rara. Si algo falla, el sistema se comporta como
    antes del cambio.
    """
    if not user_message:
        return None
    try:
        from app.assistant.orchestrator.agent_config import period_resolution_enabled
        from app.assistant.orchestrator.period import resolve_period

        if not period_resolution_enabled():
            return None
        return resolve_period(user_message)
    except Exception:  # noqa: BLE001 — una ventana opcional no tumba el turno
        return None


def _coerce_codigo(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int) and value > 0:
        text = str(value)
        return text if CODE_RE.match(text) else None
    if isinstance(value, str):
        code = value.strip().upper()
        return code if CODE_RE.match(code) else None
    return None


def _coerce_int(value: Any, lo: int, hi: int) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    raw: Any = value
    if isinstance(value, str) and _DIGIT_STR.match(value.strip()):
        raw = int(value.strip())
    if isinstance(raw, float) and raw.is_integer():
        raw = int(raw)
    if not isinstance(raw, int):
        return None
    if raw < lo or raw > hi:
        return None
    return raw


def established_dates(context: Any) -> frozenset[str]:
    """Fechas que el SISTEMA ya establecio en turnos anteriores.

    FASE 8.x — tercera fuente legitima, y la que faltaba.

    `build_agent_context_note` lleva `periodo_desde`/`periodo_hasta` al prompt
    desde `resolved_entities`, y esos valores salen de la evidencia ERP de un
    turno previo (`conversation_context` los toma del campo `periodo` que
    devolvio `get_sales`). Es la procedencia mas solida que hay en el sistema.

    Medido: el prompt se los ofrecia al modelo, el modelo los usaba en el turno
    siguiente, y el normalizador los descartaba por no aparecer literalmente en
    el mensaje de ESE turno. La llamada salia sin filtro y la respuesta volvia a
    hablar de todo el historial — el fallo de V02 reapareciendo en multi-turno,
    esta vez provocado por el propio sistema, que dice una cosa y hace otra.

    No se inyectan: la ventana de un turno anterior es contextual, no una orden.
    "Y las ventas de todo el ano?" debe poder ensanchar. El modelo elige; el
    sistema deja de tirar su eleccion al suelo.
    """
    if not isinstance(context, dict):
        return frozenset()
    ents = context.get("resolved_entities")
    if not isinstance(ents, dict):
        return frozenset()
    out: set[str] = set()
    for key in ("periodo_desde", "periodo_hasta"):
        value = ents.get(key)
        if isinstance(value, str) and DATE_RE.match(value.strip()):
            out.add(value.strip())
    return frozenset(out)


def normalize_agent_args(
    tool: str,
    arguments: dict[str, Any],
    *,
    user_message: str | None = None,
    context: Any = None,
) -> dict[str, Any]:
    """Filter unknown keys, coerce safe types, never invent business defaults."""
    allowed = allowed_arg_keys(tool)
    raw = {k: v for k, v in (arguments or {}).items() if k in allowed or k == "q"}
    out: dict[str, Any] = {}

    if "codigo" in raw:
        code = _coerce_codigo(raw.get("codigo"))
        if code:
            out["codigo"] = code
        elif raw.get("codigo") not in (None, ""):
            out["codigo"] = raw["codigo"]
    elif tool in {
        "get_product",
        "get_inventory",
        "get_stock_movements",
        "get_ingresos",
        "get_purchase_orders",
    }:
        promoted = _coerce_codigo(raw.get("q"))
        if promoted:
            out["codigo"] = promoted

    if "q" in allowed and "q" in raw and isinstance(raw.get("q"), str) and raw["q"].strip():
        out["q"] = raw["q"].strip()

    # FASE 8.x — esta lista estaba escrita a mano y omitia `oem`, `modelo`,
    # `cliente` y `group_by`: los mismos cuatro que faltaban en el schema de
    # generacion, y por la misma razon. Hacer emitible a `oem` no habria servido
    # de nada, porque el normalizador lo tiraba un paso despues y el validador
    # seguia viendo la llamada sin ancla. Se deriva del contrato para que un
    # argumento nuevo funcione el dia que se declara.
    for key in passthrough_string_keys(tool):
        if key in raw and key in allowed and isinstance(raw.get(key), str) and raw[key].strip():
            out[key] = raw[key].strip()

    # FASE 8.x — una fecha vale si el usuario la escribio, o si el sistema la
    # DERIVO deterministamente de las palabras del propio usuario.
    #
    # La regla de antes ("solo si la fecha aparece literal en la pregunta") es
    # correcta contra alucinaciones y tenia un modo de fallo silencioso: ante
    # "las ventas de enero a marzo de 2026" descartaba la ventana, la llamada
    # salia sin filtro y el ERP devolvia documentos de abril. La pregunta
    # cambiaba de alcance sin que nadie lo dijera. `resolve_period` no relaja la
    # regla: anade una segunda fuente igual de auditable, porque lee el mensaje
    # del usuario y no la salida del modelo.
    period = _resolved_period(user_message)
    # Tres fuentes, las tres verificables por el sistema y ninguna del modelo:
    # lo que el usuario escribio, lo que el sistema derivo de sus palabras, y lo
    # que el ERP ya devolvio en un turno anterior.
    allowed_dates = (period.boundaries() if period else frozenset()) | established_dates(context)
    for key in ("fecha_desde", "fecha_hasta"):
        if key not in raw or key not in allowed:
            continue
        val = raw.get(key)
        if not isinstance(val, str) or not DATE_RE.match(val.strip()):
            continue
        date = val.strip()
        if user_message is not None and date not in user_message and date not in allowed_dates:
            continue
        out[key] = date

    # Si el usuario nombro una ventana y el modelo no la paso, la pone el
    # sistema. Solo en tools cuyo UNICO modo de acotar son las fechas: las que
    # declaran su propio `periodo` (get_dashboard_kpis) ya tienen vocabulario
    # propio y no se tocan. La condicion se deriva del contrato, no de una lista.
    if period is not None and "periodo" not in allowed:
        for key, value in period.as_args().items():
            if key in allowed and key not in out:
                out[key] = value

    spec = TOOL_CONTRACTS.get(tool) or {}
    all_fields = {**(spec.get("required") or {}), **(spec.get("optional") or {})}
    for key, meta in all_fields.items():
        if meta.get("type") != "int" or key not in raw:
            continue
        coerced = _coerce_int(raw.get(key), int(meta.get("min") or 0), int(meta.get("max") or 10**9))
        if coerced is not None:
            out[key] = coerced

    if tool == "check_stock" and isinstance(raw.get("items"), list):
        items = []
        for row in raw["items"]:
            if not isinstance(row, dict):
                continue
            code = _coerce_codigo(row.get("codigo"))
            qty = _coerce_int(row.get("cantidad"), 1, 10**9)
            if code and qty is not None:
                items.append({"codigo": code, "cantidad": qty})
        if items:
            out["items"] = items

    return {k: v for k, v in out.items() if v != "" and v is not None}


def inspect_arg_fields(tool: str, arguments: dict[str, Any]) -> dict[str, str]:
    """Structured field problems. No values, no PII."""
    spec = TOOL_CONTRACTS.get(tool)
    if spec is None:
        return {"tool": "unknown"}
    fields: dict[str, str] = {}
    args = arguments or {}
    extra = set(args) - allowed_arg_keys(tool) - {"q"}
    for key in sorted(extra):
        fields[key] = "unknown"
    any_of = spec.get("any_of") or ()
    if any_of and not any(args.get(k) not in (None, "", []) for k in any_of):
        # Sin esto el modelo reintenta a ciegas: sabe que fallo, no que le falta.
        fields["|".join(any_of)] = "one_required"
    for key, meta in (spec.get("required") or {}).items():
        if key not in args or args.get(key) in (None, "", []):
            fields[key] = "required"
            continue
        want = meta.get("type")
        got = args.get(key)
        if want == "string" and not isinstance(got, str):
            fields[key] = "type"
        elif want == "int" and (isinstance(got, bool) or not isinstance(got, int)):
            fields[key] = "type"
        elif want == "array" and not isinstance(got, list):
            fields[key] = "type"
        elif want == "date" and (not isinstance(got, str) or not DATE_RE.match(got.strip())):
            fields[key] = "type"
        elif meta.get("pattern") == "product_code" and isinstance(got, str) and not CODE_RE.match(got.strip().upper()):
            fields[key] = "type"
    if tool in {"get_customer", "get_supplier"}:
        if not args.get("q") and not args.get("rut") and "id" not in args:
            fields["q"] = "required"
    return fields


def structured_invalid_args(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "error": "invalid_args",
        "tool": str(tool or "")[:64],
        "fields": inspect_arg_fields(tool, arguments),
    }
