"""FASE 10.1 — que contexto entra al prompt de ESTE turno, y por que.

QUE RESUELVE

Hoy el contexto auxiliar se arma en `service.py` juntando lo que haya:
resumen de conversacion, ultimas tools, entidades resueltas y hints de memoria.
Cada pieza tiene su propio selector y ninguna tiene presupuesto. Mientras solo
haya cuatro piezas eso funciona. La Fase 10 propone anadir agenda, tareas y
documentos, y entonces deja de funcionar: el maximo medido es 11 764 / 12 000
tokens y la holgura son 236.

Esta capa no reemplaza a ningun selector. Los envuelve y anade tres cosas que
hoy no existen:

    1. PRESUPUESTO por bloque, en caracteres, aplicado.
    2. TRAZA: por que entro cada bloque y por que quedo fuera cada otro.
    3. INVARIANTES de seguridad comprobadas en un solo sitio.

LO QUE NO PROMETE

Con `MEMORY_ENABLED=0` y `HISTORY_ENABLED=0` —la configuracion del benchmark de
76 casos— no hay contexto que recortar, asi que esta capa **no reduce tokens
ahi**. Su valor es preventivo y se mide aparte, con memoria e historial
encendidos. Decirlo importa: un router que dijera "mejora el benchmark" estaria
midiendo otra cosa.

LOS INVARIANTES

Se comprueban aunque los stores ya filtren, y a proposito: son la ultima linea
antes del prompt, y es el unico punto por el que pasa todo el contexto.

    · nada de otro actor
    · nada de otra conversacion
    · ninguna memoria que no este aprobada
    · ningun secreto, por construccion (no hay fuente de secretos aqui)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Presupuesto en CARACTERES, que es lo que se puede medir sin tokenizar. La
# relacion usada en el resto del sistema es ~4 ch/token (agent_config).
BUDGETS: dict[str, int] = {
    "conversation_summary": 400,
    "last_tools": 120,
    "resolved_entities": 200,
    "intent_hint": 40,
    "memory_hints": 800,
}

# Orden de sacrificio cuando el presupuesto total aprieta: se cae primero lo
# que menos cambia la respuesta. Las entidades resueltas van al final porque
# son las que sostienen la anafora, y perderlas rompe "y cuanto stock tiene?".
PRIORITY: tuple[str, ...] = (
    "resolved_entities",
    "intent_hint",
    "memory_hints",
    "last_tools",
    "conversation_summary",
)

# Techo total del bloque de contexto auxiliar. No es el techo del turno: es lo
# que este router se compromete a no superar, para que las capas de Fase 10
# tengan un presupuesto contra el que negociar en vez de crecer sin limite.
TOTAL_BUDGET_CHARS = 1400

# Claves que jamas pueden viajar en el contexto. Ninguna existe hoy; la lista
# esta aqui para que anadir Vault en 10.8 no requiera acordarse de este fichero.
FORBIDDEN_KEYS = frozenset({
    "secret", "secrets", "token", "api_key", "apikey", "password",
    "credential", "credentials", "private_key", "vault",
})

REASON_OK = "incluido"
REASON_EMPTY = "vacio"
REASON_NO_BUDGET = "sin_presupuesto"
REASON_FORBIDDEN = "clave_prohibida"
REASON_FOREIGN_ACTOR = "actor_ajeno"
REASON_FOREIGN_CONVERSATION = "conversacion_ajena"
REASON_UNAPPROVED = "memoria_no_aprobada"

# Una memoria entra solo si esta activa. Hoy ningun hint trae `status`, asi que
# esto no cambia nada; cuando 10.4 introduzca el estado `suggested`, la puerta
# ya esta puesta y una memoria sugerida no podra llegar al prompt por olvido.
APPROVED_STATUS = frozenset({"active", ""})


@dataclass
class ContextDecision:
    """Lo que entro, lo que no, y cuanto costo. Nombres y tamanos, no contenido."""

    context: dict[str, Any] = field(default_factory=dict)
    included: dict[str, int] = field(default_factory=dict)      # bloque -> chars
    excluded: dict[str, str] = field(default_factory=dict)      # bloque -> razon
    total_chars: int = 0

    def observation(self) -> dict[str, Any]:
        return {
            "context_included": dict(self.included),
            "context_excluded": dict(self.excluded),
            "context_chars": int(self.total_chars),
            "context_estimated_tokens": int(self.total_chars // 4),
        }

    def why(self, block: str) -> str:
        """Responde "¿por que este bloque entro o quedo fuera?"."""
        if block in self.included:
            return f"{REASON_OK} ({self.included[block]} ch)"
        return self.excluded.get(block, "no_presente")


def _size(value: Any) -> int:
    import json

    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    try:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError):
        return len(str(value))


def _has_forbidden_key(value: Any) -> bool:
    if isinstance(value, dict):
        for k, v in value.items():
            if str(k).strip().lower() in FORBIDDEN_KEYS:
                return True
            if _has_forbidden_key(v):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_has_forbidden_key(v) for v in value)
    return False


def _approved_hints(hints: Any, *, actor: str, conversation_id: str) -> tuple[list, str | None]:
    """Filtra los hints de memoria. Devuelve (aprobados, razon_de_exclusion)."""
    if not isinstance(hints, list) or not hints:
        return [], REASON_EMPTY
    fuera: str | None = None
    ok = []
    for h in hints:
        if not isinstance(h, dict):
            continue
        estado = str(h.get("status") or "").strip().lower()
        if estado not in APPROVED_STATUS:
            fuera = REASON_UNAPPROVED
            continue
        duenno = str(h.get("actor_user") or "").strip()
        if duenno and duenno != actor:
            fuera = REASON_FOREIGN_ACTOR
            continue
        conv = str(h.get("conversation_id") or "").strip()
        if conv and conversation_id and conv != conversation_id:
            fuera = REASON_FOREIGN_CONVERSATION
            continue
        if _has_forbidden_key(h):
            fuera = REASON_FORBIDDEN
            continue
        ok.append(h)
    return ok, (fuera if not ok else None)


def select_context(
    context: dict[str, Any] | None,
    *,
    actor: str,
    conversation_id: str = "",
) -> ContextDecision:
    """El contexto que este turno puede ver, con su traza.

    Solo QUITA. Nunca introduce un bloque que el llamador no haya puesto, de modo
    que no puede inventar contexto ni alcanzar una fuente a la que el servicio no
    tenga ya acceso.
    """
    original = dict(context or {})
    decision = ContextDecision()

    # `force_scenario` es control de pruebas, no contexto: pasa sin presupuesto.
    salida: dict[str, Any] = {}
    if "force_scenario" in original:
        salida["force_scenario"] = original["force_scenario"]

    restante = TOTAL_BUDGET_CHARS
    for bloque in PRIORITY:
        if bloque not in original:
            continue
        valor = original[bloque]

        if bloque == "memory_hints":
            valor, razon = _approved_hints(valor, actor=actor,
                                           conversation_id=conversation_id)
            if not valor:
                decision.excluded[bloque] = razon or REASON_EMPTY
                continue

        if _has_forbidden_key(valor) or str(bloque).lower() in FORBIDDEN_KEYS:
            decision.excluded[bloque] = REASON_FORBIDDEN
            continue

        tam = _size(valor)
        if tam == 0:
            decision.excluded[bloque] = REASON_EMPTY
            continue

        techo = min(BUDGETS.get(bloque, 200), restante)
        if techo <= 0:
            decision.excluded[bloque] = REASON_NO_BUDGET
            continue
        if tam > techo:
            if isinstance(valor, str):
                valor = valor[:techo]
                tam = len(valor)
            elif isinstance(valor, list):
                # Una lista se recorta por elementos, nunca por la mitad de uno.
                # Medido al estrenar este router: descartar la lista entera
                # tiraba TODA la memoria por pasarse un poco del techo, cuando
                # los primeros hints —los que el selector ya priorizo— si
                # cabian. Perder lo que cabe es peor que recortar.
                recortada: list[Any] = []
                for elem in valor:
                    if _size(recortada + [elem]) > techo:
                        break
                    recortada.append(elem)
                if not recortada:
                    decision.excluded[bloque] = REASON_NO_BUDGET
                    continue
                valor, tam = recortada, _size(recortada)
            else:
                # Un dict no se corta: sus claves se sostienen entre si.
                decision.excluded[bloque] = REASON_NO_BUDGET
                continue

        salida[bloque] = valor
        decision.included[bloque] = tam
        decision.total_chars += tam
        restante -= tam

    # Cualquier clave que el llamador anada y este router no conozca queda fuera
    # por defecto. Es lo que hace que anadir agenda o documentos en 10.6/10.10
    # exija declararlas aqui, con su presupuesto, en vez de colarse.
    for clave in original:
        if clave in salida or clave in decision.excluded or clave == "force_scenario":
            continue
        decision.excluded[clave] = "bloque_no_declarado"

    decision.context = salida
    return decision
