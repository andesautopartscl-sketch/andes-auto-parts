"""FASE 10.3.4-A — la superficie HTTP del Vault. Un modulo aparte, a proposito.

POR QUE NO VIVE EN `app/assistant/routes.py`

Ahi estan la conversacion, el historial y la memoria: rutas que leen y escriben
texto que puede acabar en un log sin que pase nada. La ruta de ingestion no
puede permitirse eso, y mezclarla con las demas seria confiar en que nadie
anada nunca un decorador de traza al blueprint entero. Separarla hace que esa
decision sea visible.

LO QUE ESTA CAPA GARANTIZA

  1. El texto plano entra UNA vez, por UNA ruta, y no vuelve a salir jamas.
  2. Ninguna respuesta lleva el valor, ni sus ultimos caracteres, ni una huella
     suya. Un fingerprint del secreto parece inofensivo y no lo es: convierte
     un secreto de alta entropia en un oraculo para uno de baja.
  3. El actor sale de `session`. Nunca del cuerpo, nunca de la URL.
  4. Ninguna ruta de ingestion acepta parametros en la query string.

COMO SE EVITA QUE EL PLAINTEXT ACABE EN UN LOG

Se recorrieron los sitios por los que el cuerpo de una peticion puede escaparse
en este proyecto:

  access log de werkzeug   solo la linea de peticion — por eso el valor no
                           puede ir jamas en la URL, y se rechaza la query
                           string entera en las rutas de ingestion.
  log_user_navigation      solo GET, y salta `/api/`; registra `path` y `q`.
                           Otra razon para no tocar la query string.
  enforce_csrf             no lee el cuerpo.
  errorhandler(500)        `app.logger.exception` imprime la traza. Una traza
                           no vuelca locales, pero SI el `str()` de la cadena
                           de excepciones. Por eso aqui nada se relanza con su
                           causa: `from None`, mensaje fijo, y la ruta no deja
                           escapar ninguna excepcion.
  caches de flask          se lee con `get_data(cache=False)` y se parsea a
                           mano, para que Werkzeug no se quede el cuerpo
                           colgado del objeto `request` durante el resto del
                           ciclo.
  auditoria del ERP        `record_audit_event` recibe campos elegidos a mano.
  memoria del asistente    no se la toca: nada de esto pasa por Memory.

Lo que esta capa NO puede garantizar: un proxy inverso que registre cuerpos, o
un navegador con una extension que lea el formulario. Eso esta fuera del
proceso y se dice en el informe en vez de fingir que esta cubierto.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

from flask import Blueprint, jsonify, request, session

from app.assistant.vault.vault_broker import (
    ACTIONS,
    SECRET_SLOTS,
    BrokerError,
    ExecutionRequest,
    SecretBroker,
)
from app.assistant.vault.vault_store import (
    PROVIDERS,
    PURPOSES,
    VaultStore,
    VaultStoreError,
)
from app.utils.decorators import login_required

logger = logging.getLogger(__name__)

vault_bp = Blueprint("assistant_vault", __name__, url_prefix="/assistant/api/secrets")

MAX_PLAINTEXT_CHARS = 8192
MAX_NAME_CHARS = 80
EXPIRING_DAYS = 14

# Codigos del Store y del Broker -> HTTP. Fijos, y sin mensaje interpolado:
# interpolar es como acaban los valores en los logs.
_HTTP = {
    "not_found": 404, "revoked": 409, "expired": 409, "duplicate_name": 409,
    "vault_locked": 503, "invalid_argument": 400, "invalid_provider": 400,
    "invalid_purpose": 400, "invalid_scope": 400, "no_active_version": 409,
    "version_not_found": 404, "write_failed": 500, "corrupt_envelope": 500,
    "confirmation_required": 400, "invalid_request": 400,
    "invalid_action": 400, "invalid_source": 403, "invalid_purpose_arg": 400,
}
_MENSAJE = {
    "not_found": "No existe o no es tuyo.",
    "revoked": "Ese secreto está revocado.",
    "expired": "Ese secreto está expirado.",
    "duplicate_name": "Ya tienes un secreto con ese nombre.",
    "vault_locked": "El Vault no está accesible en este servidor.",
    "no_active_version": "No hay una versión activa.",
    "write_failed": "No se pudo guardar.",
    "corrupt_envelope": "El secreto no se puede descifrar.",
}


def _mensaje(code: str) -> str:
    return _MENSAJE.get(code, "Solicitud inválida.")


def _error(code: str, http: int | None = None):
    return jsonify(ok=False, error_code=code, message=_mensaje(code)), \
        (http or _HTTP.get(code, 400))


def _actor() -> str:
    """El actor SIEMPRE sale de la sesion.

    No hay un parametro que lo acepte, ni un fallback que lo lea del cuerpo.
    Un `owner` que viaje desde el navegador es un secreto ajeno a un clic de
    distancia.
    """
    return (session.get("user") or "").strip()


def _store() -> VaultStore:
    return VaultStore()


def _broker() -> SecretBroker:
    return SecretBroker(_store())


def _cuerpo() -> dict[str, Any]:
    """Lee el JSON SIN dejar que Werkzeug se quede una copia del cuerpo.

    `request.get_json()` cachea el texto y el dict en el objeto `request`, que
    sobrevive hasta el final del ciclo y pasa por todos los `after_request`.
    Para las rutas que no tocan secretos daria igual; para estas, no.
    """
    crudo = request.get_data(cache=False, as_text=True) or ""
    if len(crudo) > MAX_PLAINTEXT_CHARS * 2:
        raise ValueError from None
    try:
        datos = json.loads(crudo) if crudo.strip() else {}
    except ValueError:
        raise ValueError from None
    finally:
        del crudo
    if not isinstance(datos, dict):
        raise ValueError from None
    return datos


def _sin_query_string() -> bool:
    """Las rutas de ingestion no aceptan NADA en la URL.

    No porque un parametro sobrante haga dano por si mismo, sino porque la URL
    se registra en tres sitios distintos y la regla "aqui no entra nada por la
    URL" es comprobable, mientras que "aqui no entra el secreto por la URL" hay
    que creersela.
    """
    return not request.args


_CAMPOS_PROHIBIDOS = ("actor", "actor_user", "owner", "owner_actor", "scope",
                      "permission_epoch", "status", "created_by", "key_version",
                      "envelope", "envelope_json")


def _campo_prohibido(datos: dict[str, Any]) -> str | None:
    for k in _CAMPOS_PROHIBIDOS:
        if k in datos:
            return k
    return None


# ───────────────────────────── proyeccion segura

def _dias_hasta(iso: str | None) -> float | None:
    if not iso:
        return None
    from datetime import datetime, timezone

    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (t - datetime.now(timezone.utc)).total_seconds() / 86400.0


def _bucket(meta: Any) -> str:
    """ACTIVE / EXPIRING / REVOKED / EXPIRED, calculado, no almacenado.

    Un secreto cuya fecha ya paso sigue diciendo `active` en la fila hasta que
    alguien la barre. La pantalla no puede creerse eso.
    """
    if meta.status == "revoked":
        return "revoked"
    if meta.status == "expired":
        return "expired"
    d = _dias_hasta(meta.expires_at)
    if d is not None and d <= 0:
        return "expired"
    if d is not None and d <= EXPIRING_DAYS:
        return "expiring"
    return "active"


def _proyectar(meta: Any) -> dict[str, Any]:
    """Lo unico que puede salir de aqui hacia el navegador.

    Construido campo a campo. Nada de `meta.to_dict()` y quitar lo malo: una
    lista negra deja pasar lo que se anada manana.
    """
    return {
        "id": meta.id,
        "name": meta.name,
        "provider": meta.provider,
        "purposes": list(meta.purposes),
        "status": meta.status,
        "bucket": _bucket(meta),
        "current_version": meta.current_version,
        "created_at": meta.created_at,
        "updated_at": meta.updated_at,
        "expires_at": meta.expires_at,
        "created_by": meta.owner_actor,
        "scope": meta.scope,
    }


def _proyectar_version(v: Any) -> dict[str, Any]:
    return {
        "version": v.version, "status": v.status, "created_at": v.created_at,
        "created_by": v.created_by, "superseded_at": v.superseded_at,
        "revoked_at": v.revoked_at,
    }


def _proyectar_grant(g: Any) -> dict[str, Any]:
    d = g.to_dict()
    d["seconds_left"] = max(0, int((_dias_hasta(g.expires_at) or 0) * 86400)) \
        if g.status == "issued" else 0
    return d


# ───────────────────────────── inventario

@vault_bp.route("", methods=["GET"])
@login_required
def listar():
    actor = _actor()
    if not actor:
        return _error("unauthorized", 401)
    try:
        metas = _store().list_metadata(owner_actor=actor)
    except VaultStoreError as exc:
        return _error(exc.code)
    items = [_proyectar(m) for m in metas]
    conteo = {b: 0 for b in ("active", "expiring", "revoked", "expired")}
    for it in items:
        conteo[it["bucket"]] = conteo.get(it["bucket"], 0) + 1
    return jsonify(ok=True, items=items, counts=conteo)


@vault_bp.route("/config", methods=["GET"])
@login_required
def configuracion():
    """Estado del Vault y si este actor ya tiene algun secreto.

    Lo segundo decide si la pantalla ensena el aviso de la clave: el momento de
    decir que una perdida de KEK no se revierte es ANTES del primero, no
    despues.
    """
    actor = _actor()
    if not actor:
        return _error("unauthorized", 401)
    st = _store()
    try:
        estado = st.status()
        mios = len(st.list_metadata(owner_actor=actor)) \
            if estado.get("state") == "open" else 0
    except VaultStoreError as exc:
        return _error(exc.code)
    return jsonify(
        ok=True,
        vault_state=estado.get("state"),
        keyring=estado.get("keyring"),
        first_secret=(mios == 0),
        providers=sorted(PROVIDERS),
        purposes=sorted(PURPOSES),
        actions=sorted(ACTIONS),
        slots=sorted(SECRET_SLOTS),
    )


@vault_bp.route("/<secret_id>", methods=["GET"])
@login_required
def detalle(secret_id: str):
    actor = _actor()
    if not actor:
        return _error("unauthorized", 401)
    st = _store()
    try:
        meta = st.get_secret_metadata(secret_id=secret_id, owner_actor=actor)
        versiones = st.list_versions(secret_id=secret_id, owner_actor=actor)
    except VaultStoreError as exc:
        return _error(exc.code)
    try:
        uso = _broker().usage_history(actor=actor, secret_id=meta.id)
    except Exception:  # noqa: BLE001
        uso = []
    return jsonify(ok=True, item=_proyectar(meta),
                   versions=[_proyectar_version(v) for v in versiones],
                   usage=uso)


# ───────────────────────────── ingestion

def _ingerir(fn, **kwargs):
    """Envoltorio de las DOS rutas que ven texto plano.

    Todo lo que pueda fallar se traduce a un codigo fijo. Ninguna excepcion
    sale de aqui con su causa encadenada: el `__cause__` de una excepcion de
    sqlite o de la capa de cifrado puede llevar el buffer dentro, y el
    `errorhandler(500)` del ERP imprime la traza entera.
    """
    try:
        return fn(**kwargs)
    except VaultStoreError as exc:
        raise _Fallo(exc.code) from None
    except (ValueError, TypeError):
        raise _Fallo("invalid_argument") from None
    except Exception:  # noqa: BLE001
        # Ni el tipo: un tipo de excepcion de una libreria de terceros puede
        # ser lo bastante especifico como para decir algo del valor.
        logger.warning("vault ingest failed")
        raise _Fallo("write_failed") from None


class _Fallo(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@vault_bp.route("", methods=["POST"])
@login_required
def crear():
    """LA ruta de ingestion. La unica, junto con la de rotacion.

    No se reutiliza ningun manejador generico de formularios: un manejador
    generico es el sitio donde manana alguien anade un `logger.debug(payload)`
    porque en las otras veinte rutas no pasaba nada.
    """
    actor = _actor()
    if not actor:
        return _error("unauthorized", 401)
    if not _sin_query_string():
        return _error("query_string_forbidden", 400)
    try:
        datos = _cuerpo()
    except ValueError:
        return _error("invalid_json", 400)

    malo = _campo_prohibido(datos)
    if malo:
        return _error("forbidden_field", 400)

    nombre = str(datos.get("name") or "").strip()[:MAX_NAME_CHARS]
    proveedor = str(datos.get("provider") or "").strip().lower()
    propositos = datos.get("purposes")
    valor = datos.get("value")
    caduca = datos.get("expires_at") or None

    if not isinstance(valor, str) or not valor or len(valor) > MAX_PLAINTEXT_CHARS:
        datos.pop("value", None)
        del valor
        return _error("invalid_value", 400)
    if not nombre or proveedor not in PROVIDERS or not isinstance(propositos, list):
        datos.pop("value", None)
        del valor
        return _error("invalid_argument", 400)

    # A partir de aqui el valor vive en UNA variable local y se borra en el
    # `finally`. El diccionario que lo trajo se vacia ya.
    datos.pop("value", None)
    claro = valor.encode("utf-8")
    del valor
    try:
        meta = _ingerir(
            _store().create_secret, owner_actor=actor, name=nombre,
            provider=proveedor, purposes=[str(p) for p in propositos],
            plaintext=claro, expires_at=(str(caduca)[:40] if caduca else None),
            created_by=actor)
    except _Fallo as f:
        return _error(f.code)
    finally:
        del claro

    _auditar("vault_secret_created", actor, secret_id=meta.id,
             provider=meta.provider, name=meta.name)
    # La respuesta es metadata. No hay campo donde devolver el valor, ni
    # siquiera vacio: un campo `value: null` es una invitacion a rellenarlo.
    return jsonify(ok=True, item=_proyectar(meta)), 201


@vault_bp.route("/<secret_id>/rotate", methods=["POST"])
@login_required
def rotar(secret_id: str):
    """Version nueva. La anterior NO se borra: queda `superseded`."""
    actor = _actor()
    if not actor:
        return _error("unauthorized", 401)
    if not _sin_query_string():
        return _error("query_string_forbidden", 400)
    try:
        datos = _cuerpo()
    except ValueError:
        return _error("invalid_json", 400)
    if _campo_prohibido(datos):
        return _error("forbidden_field", 400)

    valor = datos.get("value")
    if not isinstance(valor, str) or not valor or len(valor) > MAX_PLAINTEXT_CHARS:
        datos.pop("value", None)
        del valor
        return _error("invalid_value", 400)
    datos.pop("value", None)
    claro = valor.encode("utf-8")
    del valor
    try:
        meta = _ingerir(_store().add_version, secret_id=secret_id,
                        owner_actor=actor, plaintext=claro, created_by=actor)
    except _Fallo as f:
        return _error(f.code)
    finally:
        del claro

    _auditar("vault_secret_rotated", actor, secret_id=meta.id,
             version=meta.current_version)
    return jsonify(ok=True, item=_proyectar(meta))


# ───────────────────────────── revocacion

@vault_bp.route("/<secret_id>/revoke", methods=["POST"])
@login_required
def revocar(secret_id: str):
    actor = _actor()
    if not actor:
        return _error("unauthorized", 401)
    try:
        meta = _store().revoke_secret(secret_id=secret_id, owner_actor=actor)
    except VaultStoreError as exc:
        return _error(exc.code)
    _auditar("vault_secret_revoked", actor, secret_id=meta.id)
    return jsonify(ok=True, item=_proyectar(meta))


# ───────────────────────────── aprobacion

@vault_bp.route("/approvals/preview", methods=["POST"])
@login_required
def previsualizar():
    """Lo que el usuario va a firmar. LA estructura, no una descripcion de ella.

    El diccionario que se devuelve es el MISMO que el Broker convierte en
    huella. La pantalla pinta sus campos y ensena la huella; al aprobar, el
    cliente devuelve la huella que mostro y el servidor la recalcula. Si no
    coinciden no se emite nada.

    Esto no es ceremonia: es la unica forma de que "lo que ves es lo que firmas"
    sea comprobable. Una vista construida aparte se separa de la huella en
    cuanto alguien anade un campo a una de las dos.
    """
    actor = _actor()
    if not actor:
        return _error("unauthorized", 401)
    try:
        datos = _cuerpo()
    except ValueError:
        return _error("invalid_json", 400)
    if _campo_prohibido(datos):
        return _error("forbidden_field", 400)

    try:
        peticion, meta, purpose = _peticion_de(datos, actor)
    except _Fallo as f:
        return _error(f.code)

    canonico = SecretBroker.canonical_approval(peticion.action,
                                               peticion.resource, peticion)
    return jsonify(
        ok=True,
        canonical=canonico,
        fingerprint=SecretBroker.fingerprint_of(canonico),
        purpose=purpose,
        secret={"id": meta.id, "name": meta.name, "provider": meta.provider,
                "current_version": meta.current_version},
        grant_ttl_seconds=_ttl(),
    )


@vault_bp.route("/approvals", methods=["POST"])
@login_required
def aprobar():
    """Emite el grant, pero solo si la huella coincide con la que se enseno."""
    actor = _actor()
    if not actor:
        return _error("unauthorized", 401)
    try:
        datos = _cuerpo()
    except ValueError:
        return _error("invalid_json", 400)
    if _campo_prohibido(datos):
        return _error("forbidden_field", 400)

    try:
        peticion, meta, purpose = _peticion_de(datos, actor)
    except _Fallo as f:
        return _error(f.code)

    mostrada = str(datos.get("fingerprint") or "").strip()
    real = SecretBroker.fingerprint_of(
        SecretBroker.canonical_approval(peticion.action, peticion.resource,
                                        peticion))
    if not mostrada or mostrada != real:
        # El usuario aprobo una pantalla que ya no describe esta operacion.
        return _error("fingerprint_mismatch", 409)

    try:
        codigo, grant = _broker().approve_and_issue_grant(
            actor=actor, secret_id=meta.id, purpose=purpose, request=peticion,
            source="ui", reason=str(datos.get("reason") or "")[:200] or None,
            correlation_id=str(uuid.uuid4()))
    except BrokerError as exc:
        return _error(exc.code)
    if codigo != "authorized" or grant is None:
        return jsonify(ok=False, error_code=codigo,
                       message="No se pudo autorizar el uso."), 409
    return jsonify(ok=True, grant=_proyectar_grant(grant),
                   fingerprint=real), 201


def _ttl() -> int:
    from app.assistant.vault.vault_broker import GRANT_TTL_SECONDS

    return GRANT_TTL_SECONDS


_RECURSO_OK = re.compile(r"^https://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]{3,300}$")


def _peticion_de(datos: dict[str, Any], actor: str):
    """Construye la `ExecutionRequest` desde el cuerpo, validandolo todo.

    Las cabeceras llegan como `{"Authorization": "AUTHORIZATION"}`: el cliente
    dice QUE HUECO va en QUE cabecera, nunca el texto del hueco ni su valor. El
    marcador lo escribe el servidor. Asi un cliente no puede colar un hueco
    inventado ni un valor literal disfrazado.
    """
    accion = str(datos.get("action") or "").strip()
    recurso = str(datos.get("resource") or "").strip()
    metodo = str(datos.get("method") or "GET").strip().upper()
    purpose = str(datos.get("purpose") or "").strip().lower()
    cabeceras_in = datos.get("headers") or {}
    con_cuerpo = bool(datos.get("has_body"))
    hueco_cuerpo = str(datos.get("body_slot") or "").strip().upper()
    secret_id = str(datos.get("secret_id") or "").strip()

    if accion not in ACTIONS or purpose not in PURPOSES or not secret_id:
        raise _Fallo("invalid_argument") from None
    if not _RECURSO_OK.match(recurso):
        raise _Fallo("invalid_resource") from None
    if not isinstance(cabeceras_in, dict) or len(cabeceras_in) > 12:
        raise _Fallo("invalid_argument") from None

    cabeceras: dict[str, str] = {}
    for clave, hueco in cabeceras_in.items():
        k = str(clave).strip()
        h = str(hueco).strip().upper()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,40}", k) or h not in SECRET_SLOTS:
            raise _Fallo("invalid_argument") from None
        cabeceras[k] = "<<secret:%s>>" % h

    cuerpo = None
    if con_cuerpo:
        if hueco_cuerpo not in SECRET_SLOTS:
            raise _Fallo("invalid_argument") from None
        cuerpo = "<<secret:%s>>" % hueco_cuerpo

    peticion = ExecutionRequest(action=accion, resource=recurso, method=metodo,
                                headers=cabeceras, body=cuerpo)
    try:
        SecretBroker(_store()).validate_request(peticion)
    except BrokerError as exc:
        raise _Fallo(exc.code) from None
    try:
        meta = _store().get_secret_metadata(secret_id=secret_id,
                                            owner_actor=actor)
    except VaultStoreError as exc:
        raise _Fallo(exc.code) from None
    # Un secreto revocado o vencido no llega a la pantalla de aprobacion. Pedir
    # permiso para algo que va a fallar de todos modos entrena al usuario a
    # decir que si sin mirar.
    if meta.status in ("revoked", "expired"):
        raise _Fallo(meta.status) from None
    return peticion, meta, purpose


# ───────────────────────────── grants

@vault_bp.route("/grants", methods=["GET"])
@login_required
def listar_grants():
    actor = _actor()
    if not actor:
        return _error("unauthorized", 401)
    try:
        grants = _broker().list_grants(actor=actor)
    except Exception:  # noqa: BLE001
        return _error("write_failed", 500)
    return jsonify(ok=True, items=[_proyectar_grant(g) for g in grants])


@vault_bp.route("/grants/<grant_id>/revoke", methods=["POST"])
@login_required
def revocar_grant(grant_id: str):
    actor = _actor()
    if not actor:
        return _error("unauthorized", 401)
    hecho = _broker().revoke_grant(str(grant_id or ""), actor=actor)
    if not hecho:
        return _error("not_found", 404)
    _auditar("vault_grant_revoked", actor, grant_id=str(grant_id))
    return jsonify(ok=True, grant_id=str(grant_id), status="revoked")


# ───────────────────────────── auditoria del ERP

def _auditar(evento: str, actor: str, **campos: Any) -> None:
    """Lista blanca hacia la auditoria del ERP. Nunca el cuerpo, nunca el valor.

    Se construye el detalle campo a campo por la misma razon que en el Broker:
    un `**payload` aqui seria el valor entero en una tabla que se exporta.
    """
    detalle = {
        "secret_id": campos.get("secret_id"),
        "grant_id": campos.get("grant_id"),
        "provider": campos.get("provider"),
        "name": campos.get("name"),
        "version": campos.get("version"),
    }
    try:
        from app.utils.audit_log import record_audit_event

        record_audit_event(evento, {k: v for k, v in detalle.items()
                                    if v is not None},
                           actor_usuario=actor)
    except Exception:  # noqa: BLE001
        logger.warning("vault audit soft-failed")
