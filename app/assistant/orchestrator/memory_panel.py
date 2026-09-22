"""FASE 10.2.4 — proyeccion de memoria para el panel.

LA PREGUNTA QUE ESTA CAPA TIENE QUE CONTESTAR

    "¿Que va a recordar Andes si apruebo esto?"

Una fila de `assistant_memory_slot` no la contesta: dice `preference /
answer_style / {"answer_style":"brief"}`. Este modulo la traduce, y de paso
decide QUE sale del servidor hacia el navegador.

POR QUE NO SE REUSA `GET /api/memory` TAL CUAL

`_public` incluye `actor_user` y `permission_epoch` —metadata interna que el
panel no necesita y que no tiene por que viajar al navegador— y el valor crudo
de cada memoria. Aqui se proyecta campo por campo: cada tipo declara que se
muestra, y un tipo que no este declarado NO muestra su valor. Fallar cerrado
significa que agregar un tipo nuevo sin tocar este archivo lo deja invisible,
no filtrado.

NADA DE ESTO ES MARKUP

Se devuelven pares nombre/valor en texto plano. El cliente los pone en el DOM
con `textContent`. El valor de una memoria es contenido NO CONFIABLE —sale de
lo que el usuario escribio y de lo que el sistema dedujo—, y la unica razon por
la que puede mostrarse es que nunca se interpreta como HTML.

EL ESTADO EFECTIVO NO ES SIEMPRE EL ALMACENADO

El TTL corre antes que la puerta de estado: una memoria `approved` con
`expires_at` pasado no llega al modelo. El panel tiene que decir la verdad
operativa, asi que esa memoria aparece como caducada, con la razon a la vista.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
from datetime import datetime, timezone
from typing import Any

from app.assistant.orchestrator.memory_schema import DEFAULT_STATUS, memory_version
from app.assistant.orchestrator.memory_selector import hint_contains_prohibited

logger = logging.getLogger(__name__)

# FASE 10.2.5 — de donde sale la linea "Pendiente -> Aprobada".
#
# NO de una columna nueva. Guardar `previous_status` en la fila seria inventar
# estado persistente para pintar una linea, y duplicaria lo que la auditoria ya
# registra con mas detalle. La transicion se PROYECTA del evento
# `assistant_memory_status_change`, que es la fuente que ya existe y la unica
# que puede sostener la afirmacion.
#
# Se lee solo la COLA del archivo: es append-only y crece sin limite, asi que
# cargarlo entero para pintar un panel seria una fuga de memoria con forma de
# funcionalidad.
AUDIT_TAIL_BYTES = 256 * 1024

EVENTO_TRANSICION = "assistant_memory_status_change"

# Lo unico que sale del registro de auditoria hacia el navegador. El resto del
# evento —correlation_id, permission_epoch, source— es diagnostico interno.
CAMPOS_TRANSICION = ("previous_status", "new_status", "actor_user", "ts", "result")

# Orden de la bandeja: primero lo que espera una decision.
ORDEN_ESTADOS = ("suggested", "approved", "rejected", "expired")

_ESTILOS = {
    "brief": "Respuestas breves",
    "detailed": "Respuestas detalladas",
    "operational": "Respuestas operativas",
}

_CLASES = {
    "codigo": "Código",
    "sku": "Código",
    "producto_id": "Producto",
    "proveedor_id": "Proveedor",
    "cliente_id": "Cliente",
    "bodega_id": "Bodega",
    "proveedor_q": "Búsqueda de proveedor",
    "oc": "Orden de compra",
}

# De donde salio. Es la respuesta a "¿por que me estan proponiendo esto?".
_PORQUE = {
    "derived": "Andes lo dedujo de tus consultas.",
    "explicit": "Se lo pediste tú en la conversación.",
    "ui": "Lo guardaste desde la interfaz.",
}

_TITULOS = {
    "preference": "Estilo de respuesta",
    "ui_pref": "Preferencia de interfaz",
    "frequent_entity": "Referencia que consultas seguido",
    "pinned_entity": "Referencia fijada",
    "conversation_summary": "Resumen de una conversación",
}


def _ruta_auditoria() -> pathlib.Path:
    """La misma que usa `OrchestratorAudit`."""
    return pathlib.Path(os.environ.get("ANDES_ORCH_AUDIT_PATH")
                        or "data/orchestrator_audit.jsonl")


def ultimas_transiciones(actor: str,
                         ruta: pathlib.Path | None = None) -> dict[str, dict[str, Any]]:
    """La ultima transicion REAL de cada memoria, leida de la auditoria.

    Solo cuentan los cambios que ocurrieron: `result=ok` y `changed=true`. Un
    intento bloqueado esta en el log —y debe estarlo— pero no es historia del
    registro: pintarlo diria que algo cambio cuando no cambio nada.

    Se filtra por actor a proposito. Hoy solo el dueno puede moderar su propia
    memoria, asi que la comprobacion es redundante; si eso cambiara, esta linea
    hace que el panel deje de mostrar la transicion en vez de enseñar la de otro.
    """
    archivo = ruta or _ruta_auditoria()
    try:
        if not archivo.exists():
            return {}
        tam = archivo.stat().st_size
        with archivo.open("rb") as fh:
            if tam > AUDIT_TAIL_BYTES:
                fh.seek(tam - AUDIT_TAIL_BYTES)
                fh.readline()  # la primera linea puede venir cortada
            crudo = fh.read().decode("utf-8", errors="replace")
    except OSError as exc:  # noqa: BLE001 - el panel nunca cae por culpa del log
        logger.warning("memory_panel: no se pudo leer la auditoria: %s",
                       type(exc).__name__)
        return {}

    out: dict[str, dict[str, Any]] = {}
    for linea in crudo.splitlines():
        linea = linea.strip()
        if not linea or EVENTO_TRANSICION not in linea:
            continue
        try:
            r = json.loads(linea)
        except ValueError:
            continue
        if r.get("event") != EVENTO_TRANSICION:
            continue
        if r.get("result") != "ok" or not r.get("changed"):
            continue
        if str(r.get("actor_user") or "") != actor:
            continue
        mid = str(r.get("memory_id") or "")
        if not mid:
            continue
        # El archivo esta en orden cronologico: la ultima gana.
        out[mid] = {k: r.get(k) for k in CAMPOS_TRANSICION}
    return out


def origen_de_aprobacion(slot: dict[str, Any]) -> str:
    """Como llego esta memoria a su estado. Tres casos, y no son lo mismo.

    `human`     alguien la modero con el motor de aprobacion: hay `status_by`.
    `migration` viene de antes de que existiera la columna `status`. La
                migracion de 10.2.1 puso `approved` y no toco las marcas, asi
                que `status_changed_at` quedo en NULL. Nadie la aprobo, y la
                interfaz no puede insinuar que si.
    `system`    la escribio el sistema con ese estado y nadie la ha moderado.
                Con APPROVAL=0 es el caso normal de lo derivado.
    """
    if slot.get("status_by"):
        return "human"
    if not slot.get("status_changed_at"):
        return "migration"
    return "system"


_ORIGEN_TEXTO = {
    "migration": ("Disponible desde la migración inicial: ya estaba en uso "
                  "cuando se añadió el control de aprobación. Nadie la aprobó."),
    "system": "La guardó el asistente y nadie la ha revisado todavía.",
}


def _ahora() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _invertido(valor: str) -> str:
    """Clave que ordena fechas ISO de forma descendente con un sort ascendente.
    Mismo truco que el ranking del selector, para no tener dos criterios."""
    if not valor:
        return "~"
    return "".join(chr(0x10FFFF - ord(ch)) for ch in valor)


def _caducada(slot: dict[str, Any], ahora: str) -> bool:
    exp = str(slot.get("expires_at") or "")
    return bool(exp) and exp <= ahora


def estado_efectivo(slot: dict[str, Any], ahora: str | None = None) -> str:
    """Lo que el selector hara con ella, no lo que dice la columna.

    El TTL corre ANTES que la puerta de estado, asi que una `approved` caducada
    ya no llega al modelo. Mostrarla como aprobada seria mentir sobre el sistema.
    """
    if _caducada(slot, ahora or _ahora()):
        return "expired"
    estado = str(slot.get("status") or DEFAULT_STATUS).strip().lower()
    return estado if estado in ORDEN_ESTADOS else DEFAULT_STATUS


def _campos(slot: dict[str, Any]) -> list[dict[str, str]]:
    """Campo por campo y por tipo. Un tipo no declarado no muestra su valor."""
    tipo = str(slot.get("memory_type") or "").strip().lower()
    valor = slot.get("value") if isinstance(slot.get("value"), dict) else {}

    if tipo == "preference":
        estilo = str(valor.get("answer_style") or "")
        return [{"name": "Preferencia",
                 "value": _ESTILOS.get(estilo, estilo or "—")}]

    if tipo == "ui_pref":
        return [{"name": "Vista compacta",
                 "value": "Activada" if valor.get("compact") else "Desactivada"}]

    if tipo in {"frequent_entity", "pinned_entity"}:
        clase = str(valor.get("kind") or "").strip().lower()
        campos = [{"name": _CLASES.get(clase, "Referencia"),
                   "value": str(valor.get("value") or "—")}]
        veces = valor.get("hit_count")
        if veces:
            campos.append({"name": "Veces consultada", "value": str(veces)})
        return campos

    if tipo == "conversation_summary":
        campos = [{"name": "Resumen", "value": str(valor.get("text") or "—")}]
        herramientas = valor.get("tools")
        if isinstance(herramientas, list) and herramientas:
            campos.append({"name": "Consultas usadas",
                           "value": ", ".join(str(t) for t in herramientas[:3])})
        return campos

    # Tipo desconocido: se anuncia, no se vuelca.
    return []


def panel_item(slot: dict[str, Any], ahora: str | None = None,
               transiciones: dict[str, dict[str, Any]] | None = None
               ) -> dict[str, Any]:
    """Una tarjeta. Sin `actor_user`, sin `permission_epoch`, sin valor crudo."""
    ahora = ahora or _ahora()
    tipo = str(slot.get("memory_type") or "").strip().lower()
    fuente = str(slot.get("source") or "").strip().lower()
    efectivo = estado_efectivo(slot, ahora)

    # Defensa en profundidad. El sanitizador ya rechaza secretos al escribir,
    # pero si una fila vieja trae contenido que el selector descartaria, el
    # panel no lo muestra Y avisa de que aprobarla no serviria de nada.
    bloqueada = hint_contains_prohibited(
        {"type": tipo, "key": str(slot.get("key") or ""),
         "value": slot.get("value") if isinstance(slot.get("value"), dict) else {}})

    campos = [] if bloqueada else _campos(slot)

    avisos: list[str] = []
    if bloqueada:
        avisos.append("Contiene datos que Andes no puede mostrar ni usar. "
                      "Aunque la apruebes, no llegará al asistente.")
    elif not campos:
        avisos.append("Tipo de memoria no reconocido por este panel. "
                      "No se muestra su contenido.")
    if efectivo == "expired" and _caducada(slot, ahora):
        avisos.append("Caducó por antigüedad. Ya no se usa, y no puede "
                      "aprobarse: habría que volver a crearla.")
    if str(slot.get("sensitivity") or "") == "contextual":
        avisos.append("Depende de tus permisos actuales: si cambian, "
                      "Andes deja de usarla.")

    origen = origen_de_aprobacion(slot)

    return {
        "id": slot.get("id"),
        "version": memory_version(slot),
        "title": _TITULOS.get(tipo, "Memoria"),
        "fields": campos,
        "why": _PORQUE.get(fuente, "Origen no identificado."),
        "status": efectivo,
        "stored_status": str(slot.get("status") or DEFAULT_STATUS),
        "scope": str(slot.get("scope") or ""),
        "conversation_id": slot.get("conversation_id"),
        "source_turn_id": slot.get("source_turn_id"),
        "source": fuente,
        "confidence": slot.get("confidence"),
        "created_at": slot.get("created_at"),
        "expires_at": slot.get("expires_at"),
        # Ultima accion: quien y cuando. El estado ANTERIOR no vive en la fila
        # —solo en la auditoria, que no se expone—, asi que el panel no lo
        # inventa: muestra el actual y quien lo dejo asi.
        "status_changed_at": slot.get("status_changed_at"),
        "status_by": slot.get("status_by"),
        # FASE 10.2.5 — como llego a su estado, y de donde a donde vino.
        "approval_origin": origen,
        "origin_note": _ORIGEN_TEXTO.get(origen),
        "last_transition": (transiciones or {}).get(str(slot.get("id") or "")),
        "warnings": avisos,
        "blocked": bloqueada,
    }


def panel_payload(slots: list[dict[str, Any]], ahora: str | None = None, *,
                  actor: str = "",
                  transiciones: dict[str, dict[str, Any]] | None = None
                  ) -> dict[str, Any]:
    """La bandeja completa: tarjetas ordenadas y el recuento por estado."""
    ahora = ahora or _ahora()
    if transiciones is None:
        transiciones = ultimas_transiciones(actor) if actor else {}
    items = [panel_item(s, ahora, transiciones) for s in slots
             if isinstance(s, dict)]

    conteo = {e: 0 for e in ORDEN_ESTADOS}
    for it in items:
        conteo[it["status"]] = conteo.get(it["status"], 0) + 1

    # Primero lo que espera decision; dentro de cada estado, lo mas reciente
    # arriba, que es lo que el usuario acaba de generar conversando.
    prioridad = {e: i for i, e in enumerate(ORDEN_ESTADOS)}
    items.sort(key=lambda it: (prioridad.get(it["status"], 9),
                               _invertido(str(it.get("created_at") or ""))))
    return {"items": items, "counts": conteo, "order": list(ORDEN_ESTADOS)}
