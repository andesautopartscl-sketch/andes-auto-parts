"""FASE 10.2.3 — motor de aprobacion de memoria.

QUE ES Y QUE NO ES

Aprobar es un ACTO DE CONTROL, no una frase. 10.2.2 dejo que lo que el sistema
infiere naciera `suggested` y que solo lo `approved` llegara al modelo; sin este
motor, con la bandera encendida toda la memoria derivada se queda en `suggested`
para siempre y el asistente deja de aprender en vez de aprender con permiso.

EL LLM PUEDE PROPONER, NUNCA APROBAR

`SOURCES_HUMANAS` es una lista cerrada y el motor rechaza cualquier otro origen.
Un documento, una pagina o una respuesta del modelo que diga "approve" es TEXTO:
llega como dato, no como llamada. La unica forma de aprobar es un POST
autenticado a la ruta de aprobacion. Hay un test que afirma que ningun modulo
del orquestador importa este motor, para que la separacion no dependa de que
nadie lo cablee por descuido.

POR QUE APPROVE/REJECT Y NO UN CAMPO EN EL PUT

`PUT /api/memory/<id>` significa "corrige el contenido". Aprobar es otro acto.
Mezclarlos haria que una correccion de texto aprobara de paso —justo lo que el
store impide adentro, donde una actualizacion de valor no reetiqueta el estado.

LAS TRANSICIONES, Y POR QUE ESTAS

    suggested -> approved    el caso central
    suggested -> rejected    el caso central
    approved  -> approved    idempotente, sin escritura
    rejected  -> rejected    idempotente, sin escritura

    rejected  -> approved    EXIGE `reconsider=true`
        Rehabilitar lo que alguien ya descarto no puede pasar por el mismo
        gesto que aprobar una sugerencia nueva. El campo extra obliga a que
        quien lo haga sepa que esta revocando una decision previa.

    approved  -> rejected    EXIGE `revoke=true`
        Simetrico: retirar un permiso ya concedido no es "declinar una
        sugerencia". La revision pidio que esta operacion no fuera ambigua, y
        la forma de no serlo es que tenga su propio nombre.

    expired   -> *           TERMINAL, se rechaza
        `expired` es fin de ciclo de vida, no un estado de moderacion.
        Aprobarlo resucitaria algo cuya ventana ya cerro, y eso seria usar el
        estado para saltarse la puerta del TTL: exactamente la propiedad que
        10.2.2 se encarga de que sea imposible. Para volver a tenerla, se crea
        de nuevo.

Una memoria borrada o caducada por TTL ni siquiera se lee: el store la filtra
con el mismo criterio que `get_slot`, asi que responde `not_found`.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.assistant.orchestrator.audit import OrchestratorAudit
from app.assistant.orchestrator.memory_config import memory_enabled
from app.assistant.orchestrator.memory_epoch import (
    PermissionEpochProvider,
    resolve_actor_permission_epoch,
)
from app.assistant.orchestrator.memory_schema import memory_version
from app.assistant.orchestrator.memory_store import MemoryStore, get_default_memory_store

logger = logging.getLogger(__name__)

# Origenes HUMANOS. Cerrada a proposito: si manana alguien cablea el motor desde
# el orquestador, no hay un `source` que lo deje pasar.
SOURCES_HUMANAS = frozenset({"ui", "api"})

APPROVE = "approve"
REJECT = "reject"

# El motivo es texto libre del usuario: se acota, se limpia de saltos y se
# audita. Nunca se usa para decidir nada.
MAX_REASON = 200
_CONTROL_RE = re.compile(r"[\r\n\t\x00-\x1f]")


@dataclass
class ApprovalResult:
    ok: bool
    operation: str
    error_code: str | None = None
    message: str | None = None
    changed: bool = False
    previous_status: str | None = None
    new_status: str | None = None
    slot: dict[str, Any] | None = None
    version: str | None = None
    audit: dict[str, Any] = field(default_factory=dict)


_MENSAJES = {
    "memory_disabled": "Memoria deshabilitada.",
    "not_found": "Memoria no encontrada.",
    "invalid_source": "Origen no autorizado para aprobar.",
    "version_required": "Falta la version de la memoria.",
    # Mismo texto que muestra el panel: el servidor y la UI no pueden decir
    # cosas distintas sobre la misma condicion.
    "version_conflict": ("Esta memoria cambió desde que la abriste. "
                         "Actualízala para revisar su estado actual."),
    "status_conflict": ("Esta memoria cambió desde que la abriste. "
                        "Actualízala para revisar su estado actual."),
    "scope_mismatch": "Esta memoria pertenece a otra conversacion.",
    "expired_terminal": "Una memoria caducada no se aprueba ni se rechaza; vuelve a crearla.",
    "rejected_requires_reconsider": (
        "Esta memoria fue rechazada antes. Para rehabilitarla, reconsiderala "
        "explicitamente."),
    "approved_requires_revoke": (
        "Esta memoria ya estaba aprobada. Para retirarla, revocala "
        "explicitamente."),
    "write_failed": "No se pudo actualizar la memoria.",
}


def _limpia_motivo(raw: Any) -> str | None:
    if raw is None:
        return None
    texto = _CONTROL_RE.sub(" ", str(raw)).strip()
    return texto[:MAX_REASON] or None


def _transicion(operacion: str, actual: str, *, reconsider: bool,
                revoke: bool) -> tuple[str | None, str | None]:
    """(estado_destino, error). destino None con error None = idempotente."""
    if actual == "expired":
        return None, "expired_terminal"

    if operacion == APPROVE:
        if actual == "approved":
            return None, None
        if actual == "suggested":
            return "approved", None
        if actual == "rejected":
            return ("approved", None) if reconsider else (
                None, "rejected_requires_reconsider")
        return None, "not_found"

    if actual == "rejected":
        return None, None
    if actual == "suggested":
        return "rejected", None
    if actual == "approved":
        return ("rejected", None) if revoke else (None, "approved_requires_revoke")
    return None, "not_found"


def _auditar(audit: OrchestratorAudit | None, registro: dict[str, Any]) -> None:
    try:
        (audit or OrchestratorAudit()).write(registro)
    except Exception:  # noqa: BLE001 - auditar nunca puede tumbar la operacion
        logger.warning("assistant_memory approval audit failed")


def _resuelve(
    operacion: str,
    *,
    actor_user: str,
    slot_id: str,
    expected_version: str | None,
    reason: str | None,
    source: str,
    correlation_id: str | None,
    conversation_id: str | None,
    reconsider: bool,
    revoke: bool,
    store: MemoryStore | None,
    audit: OrchestratorAudit | None,
    epoch_provider: PermissionEpochProvider | None,
) -> ApprovalResult:
    actor = (actor_user or "").strip()
    motivo = _limpia_motivo(reason)
    origen = (source or "").strip().lower()
    corr = str(correlation_id or "")[:64] or None
    epoch_leido: int | None = None

    def _falla(code: str, *, previo: str | None = None,
               slot: dict[str, Any] | None = None) -> ApprovalResult:
        registro = _registro_auditoria(
            operacion=operacion, actor=actor, slot_id=slot_id, previo=previo,
            nuevo=None, motivo=motivo, origen=origen, corr=corr,
            epoch=epoch_leido, resultado=code, cambio=False)
        _auditar(audit, registro)
        return ApprovalResult(ok=False, operation=operacion, error_code=code,
                              message=_MENSAJES.get(code, "Operacion rechazada."),
                              previous_status=previo, slot=slot, audit=registro)

    if not memory_enabled():
        return _falla("memory_disabled")
    if origen not in SOURCES_HUMANAS:
        # Un origen que no sea humano ni siquiera llega a tocar la fila.
        return _falla("invalid_source")
    if not actor or not str(slot_id or "").strip():
        return _falla("not_found")
    if not expected_version:
        return _falla("version_required")

    # El epoch se lee para AUDITAR con que permisos se aprobo, no para decidir.
    # Aprobar no cambia permisos; el epoch sigue siendo quien decide, en la
    # lectura, si esa memoria puede usarse. Se resuelve DESPUES de las guardas
    # baratas para no molestar al proveedor por una peticion que ya se rechazo.
    res = resolve_actor_permission_epoch(actor, provider=epoch_provider)
    epoch_leido = int(res.epoch) if res.available and res.epoch is not None else None

    mem = store if store is not None else get_default_memory_store()
    actual = mem.get_slot(actor, str(slot_id)[:80])
    if not actual:
        return _falla("not_found")

    # Aislamiento de conversacion: una memoria de conversacion solo se modera
    # desde SU conversacion. Sin esto, un panel abierto en otra podria aprobar
    # algo que su usuario nunca vio en contexto.
    if str(actual.get("scope") or "") == "conversation":
        pedida = str(conversation_id or "").strip()[:80]
        if not pedida or pedida != str(actual.get("conversation_id") or ""):
            return _falla("scope_mismatch", previo=actual.get("status"))

    if memory_version(actual) != expected_version:
        return _falla("version_conflict", previo=actual.get("status"), slot=actual)

    previo = str(actual.get("status") or "approved")
    destino, error = _transicion(operacion, previo, reconsider=reconsider,
                                 revoke=revoke)
    if error:
        return _falla(error, previo=previo, slot=actual)

    if destino is None:
        # Idempotente: no se escribe, no se audita como cambio, y se devuelve
        # ok. Repetir la operacion no puede tener un efecto distinto.
        registro = _registro_auditoria(
            operacion=operacion, actor=actor, slot_id=actual.get("id"),
            previo=previo, nuevo=previo, motivo=motivo, origen=origen, corr=corr,
            epoch=epoch_leido, resultado="noop", cambio=False)
        _auditar(audit, registro)
        return ApprovalResult(ok=True, operation=operacion, changed=False,
                              previous_status=previo, new_status=previo,
                              slot=actual, version=memory_version(actual),
                              audit=registro)

    escrito = mem.set_status(
        actor_user=actor, slot_id=str(actual.get("id") or ""),
        new_status=destino, expected_version=expected_version,
        expected_status=previo, status_by=actor)

    if not escrito.get("ok"):
        code = str(escrito.get("error_code") or "write_failed")
        return _falla(code, previo=escrito.get("previous_status") or previo,
                      slot=escrito.get("slot"))

    nueva = escrito.get("slot") or actual
    registro = _registro_auditoria(
        operacion=operacion, actor=actor, slot_id=nueva.get("id"), previo=previo,
        nuevo=destino, motivo=motivo, origen=origen, corr=corr,
        epoch=epoch_leido, resultado="ok", cambio=bool(escrito.get("changed")))
    _auditar(audit, registro)
    return ApprovalResult(ok=True, operation=operacion,
                          changed=bool(escrito.get("changed")),
                          previous_status=previo, new_status=destino, slot=nueva,
                          version=memory_version(nueva), audit=registro)


def _registro_auditoria(*, operacion: str, actor: str, slot_id: Any,
                        previo: str | None, nuevo: str | None, motivo: str | None,
                        origen: str, corr: str | None, epoch: int | None,
                        resultado: str, cambio: bool) -> dict[str, Any]:
    """El registro de auditoria. Diez campos y NINGUN valor de memoria.

    Que memoria, quien, de que estado a cual, cuando, por que, desde donde, con
    que correlacion, con que epoch y como termino. El `value_json` no esta a
    proposito: para responder "quien dejo entrar esto" basta el id, y el
    contenido ya vive en la fila —duplicarlo en un log append-only solo
    multiplica los sitios donde puede filtrarse.
    """
    return {
        "event": "assistant_memory_status_change",
        "memory_id": str(slot_id or "")[:80],
        "actor_user": actor,
        "previous_status": previo,
        "new_status": nuevo,
        "reason": motivo,
        "source": origen,
        "correlation_id": corr,
        "permission_epoch": epoch,
        "operation": operacion,
        "result": resultado,
        "changed": bool(cambio),
    }


def approve_memory(
    *,
    actor_user: str,
    slot_id: str,
    expected_version: str,
    source: str,
    reason: str | None = None,
    correlation_id: str | None = None,
    conversation_id: str | None = None,
    reconsider: bool = False,
    store: MemoryStore | None = None,
    audit: OrchestratorAudit | None = None,
    epoch_provider: PermissionEpochProvider | None = None,
) -> ApprovalResult:
    """`suggested -> approved`. `rejected` solo con `reconsider=True`."""
    return _resuelve(
        APPROVE, actor_user=actor_user, slot_id=slot_id,
        expected_version=expected_version, reason=reason, source=source,
        correlation_id=correlation_id, conversation_id=conversation_id,
        reconsider=bool(reconsider), revoke=False, store=store, audit=audit,
        epoch_provider=epoch_provider)


def reject_memory(
    *,
    actor_user: str,
    slot_id: str,
    expected_version: str,
    source: str,
    reason: str | None = None,
    correlation_id: str | None = None,
    conversation_id: str | None = None,
    revoke: bool = False,
    store: MemoryStore | None = None,
    audit: OrchestratorAudit | None = None,
    epoch_provider: PermissionEpochProvider | None = None,
) -> ApprovalResult:
    """`suggested -> rejected`. `approved` solo con `revoke=True`."""
    return _resuelve(
        REJECT, actor_user=actor_user, slot_id=slot_id,
        expected_version=expected_version, reason=reason, source=source,
        correlation_id=correlation_id, conversation_id=conversation_id,
        reconsider=False, revoke=bool(revoke), store=store, audit=audit,
        epoch_provider=epoch_provider)
