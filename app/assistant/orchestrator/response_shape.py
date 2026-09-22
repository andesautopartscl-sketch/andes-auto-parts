"""FASE 9.7 — que forma tiene una respuesta, deducido de lo que el turno hizo.

POR QUE EXISTE

En la primera prueba real con lenguaje natural el asistente contesto esto:

    usuario   : "Hola"
    asistente : "DATOS:
                 - Hola, en que puedo ayudarte hoy?"

La cabecera no es un adorno mal puesto: es una afirmacion. Dice "lo que sigue es
un dato observado del ERP". Un saludo no lo es. Y el mismo mecanismo publicaba
"INFERENCIA:" delante de frases que no inferian nada de ninguna evidencia.

DE DONDE SALEN LAS ETIQUETAS, Y POR QUE NO SE QUITAN SIN MAS

`ladder_sections` existe para que "necesitarias 24 unidades" y "hay 7 unidades"
no parezcan la misma clase de afirmacion. Esa propiedad es real y se conserva
entera. Lo que se corrige es su aplicacion: una etiqueta distingue UNAS de
OTRAS, asi que cuando solo hay una clase no distingue nada y solo anade
ceremonia. Con dos o mas clases, todas las etiquetas siguen.

QUE HACE ESTE MODULO

Clasifica. No decide contenido, no llama a nadie, no tiene estado: recibe lo que
el turno ya produjo —evidencia, clases de claim, banderas— y devuelve un nombre
y dos decisiones de presentacion. No es una segunda arquitectura porque no hay
nada que orquestar aqui: es una funcion.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

# Las ocho formas. El orden es el de especificidad con que se deciden.
RESPONSE_KINDS = (
    "error",
    "clarification",
    "greeting",
    "conversational_ack",
    "not_found",
    "multi_tool_answer",
    "data_answer",
    "factual_answer",
)

# Formas que NO hablan del ERP: ni etiquetas de escalera ni tarjetas.
_CONVERSATIONAL = frozenset({"greeting", "conversational_ack", "clarification"})

# Vocabulario cerrado de saludo. Cerrado a proposito, como el resto de
# detectores del sistema: lo que no esta aqui no es un saludo, es una pregunta.
_GREETINGS = (
    "hola", "holi", "buenas", "buen dia", "buenos dias", "buenas tardes",
    "buenas noches", "que tal", "que onda", "como estas", "como va",
    "como andas", "saludos", "hey", "buenas bro", "como vamos",
)
_GREETING_MAX_WORDS = 12


def _fold(text: str) -> str:
    norm = unicodedata.normalize("NFD", str(text or "").lower())
    return "".join(c for c in norm if unicodedata.category(c) != "Mn")


def looks_like_greeting(message: str) -> bool:
    """Un saludo es corto y empieza saludando.

    Se exige las DOS cosas. "Hola, cuanto stock tiene el 2404?" empieza con
    "hola" y no es un saludo: es una consulta con cortesia delante, y tratarla
    como charla le quitaria la tarjeta que si merece.
    """
    folded = _fold(message).strip()
    if not folded:
        return False
    if len(folded.split()) > _GREETING_MAX_WORDS:
        return False
    if not any(re.match(rf"^{re.escape(g)}\b", folded) for g in _GREETINGS):
        return False
    # Si ademas pregunta por algo concreto, manda la pregunta.
    return not re.search(r"\b\d{3,}\b", folded)


def claim_kinds(claims: Any) -> tuple[str, ...]:
    """Clases presentes, sin repetir y en orden de aparicion."""
    out: list[str] = []
    for claim in (claims or []):
        if not isinstance(claim, dict):
            continue
        kind = str(claim.get("kind") or "").strip()
        if kind and kind not in out:
            out.append(kind)
    return tuple(out)


def classify_response(
    *,
    message: str = "",
    evidence: Any = None,
    claims: Any = None,
    needs_clarification: bool = False,
    reject: bool = False,
    error_code: Any = None,
) -> str:
    """El nombre de la forma. Deterministico y derivado; no consulta nada."""
    items = [e for e in (evidence or []) if isinstance(e, dict)]
    if reject or error_code:
        return "error"
    fallidas = [e for e in items if not e.get("ok")]
    if fallidas and not any(e.get("ok") for e in items):
        # Todas las llamadas fallaron. `not_found` es un hecho del catalogo;
        # un permiso denegado o un servicio caido no lo son.
        codigos = {str(e.get("error_code") or "") for e in fallidas}
        return "not_found" if codigos <= {"not_found"} else "error"
    # El saludo se decide ANTES que la bandera de aclaracion, y es a proposito.
    #
    # Medido al estrenar este modulo: la instruccion de tono "un saludo se
    # contesta en UNA linea y sin cifras" hizo que el modelo emitiera cero
    # claims, la regla de 9.6 —final sin evidencia y sin claims es una peticion
    # de informacion— se disparo, y "Hola" quedo clasificado `clarification`.
    # La regla de 9.6 sigue siendo correcta para lo que cubre; lo que faltaba es
    # que un saludo reconocido NUNCA es una peticion de aclaracion, tenga o no
    # claims.
    if not items and looks_like_greeting(message):
        return "greeting"
    if needs_clarification:
        return "clarification"
    if not items:
        return "conversational_ack"
    con_datos = [e for e in items if e.get("ok")]
    if len({str(e.get("tool") or "") for e in con_datos}) >= 2:
        return "multi_tool_answer"
    if all(e.get("empty") for e in con_datos):
        return "not_found"
    # Una sola tool con filas es una respuesta de datos; sin filas que proyectar
    # es un hecho suelto ("son 2 unidades") y no necesita tarjeta.
    unico = con_datos[0] if con_datos else {}
    data = unico.get("data") if isinstance(unico.get("data"), dict) else {}
    tiene_filas = isinstance(data.get("items"), list) and bool(data.get("items"))
    return "data_answer" if tiene_filas else "factual_answer"


def should_label_sections(kinds: Any) -> bool:
    """Las etiquetas aparecen cuando hay algo que distinguir.

    Con una sola clase de claim no distinguen nada: "DATOS:" delante de una
    unica linea de datos no informa, solo ceremonia. Con dos o mas, TODAS se
    mantienen — esa es la propiedad que `ladder_sections` existe para proteger y
    aqui no se toca.
    """
    presentes = kinds if isinstance(kinds, tuple) else claim_kinds(kinds)
    return len([k for k in presentes if k]) >= 2


def wants_cards(kind: str) -> bool:
    """Una charla no lleva tarjetas. Una tarjeta vacia es peor que ninguna."""
    return str(kind) not in _CONVERSATIONAL


def is_conversational(kind: str) -> bool:
    return str(kind) in _CONVERSATIONAL
