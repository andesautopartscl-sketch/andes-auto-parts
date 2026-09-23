"""FASE 10.3.3 — Secret Broker: la frontera de autorizacion y uso del Vault.

EL INVARIANTE DEL QUE CUELGA TODO LO DEMAS

    EL BROKER NO DEVUELVE TEXTO PLANO. A NADIE.

No hay `get_plaintext`, ni `resolve_secret`, ni `read_secret`. El Broker
descifra, sustituye, EJECUTA, y devuelve el resultado saneado. El texto plano
existe unicamente dentro del marco de la funcion que lo usa.

Si devolviera el valor, la pregunta "quien puede filtrarlo" tendria tantas
respuestas como llamadores. Asi tiene una, y es este modulo. Es el mismo patron
que el BFF del ERP ya usa con el token M2M: el navegador nunca lo ve porque el
servidor lo inyecta.

TRES ENTIDADES QUE NO SON LA MISMA

    APPROVAL   la decision humana. Durable, auditable.
    GRANT      su token consumible. Efimero, de un solo uso.
    SECRET     el material protegido, en el Vault.

Colapsarlas seria convertir un "si" en acceso indefinido. Una aprobacion no da
acceso: emite un grant que caduca en minutos y se gasta una vez.

CONSUMIR VA ANTES DE EJECUTAR, Y ES UNA DECISION

Se reclama el grant —atomicamente— y despues se llama al proveedor. Al reves,
dos hilos podrian pasar la validacion, ejecutar los dos, y pelearse despues por
consumir: la garantia "exactamente una ejecucion" exige reclamar antes del
efecto.

El precio esta asumido: si el ejecutor falla, el grant queda gastado y hay que
volver a aprobar. Para una credencial es la direccion segura — una llamada
fallida puede haber tenido efecto igualmente, y regalar un reintento gratis es
regalar una segunda ejecucion.

LO QUE NO HAY AQUI

Ni rutas HTTP, ni interfaz, ni proveedor real, ni accion de escritura. El
Broker es un servicio interno; la aprobacion se emite por una API controlada
para pruebas. La interfaz es 10.3.4.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from app.assistant.orchestrator.audit import OrchestratorAudit
# Reutilizado a proposito, no copiado: es LA lista de origenes humanos del
# sistema, la misma que 10.2.3 usa para moderar memoria. Duplicarla seria
# crear el segundo sistema de aprobacion que esta fase prohibe.
from app.assistant.orchestrator.memory_approval import MAX_REASON, SOURCES_HUMANAS
from app.assistant.orchestrator.memory_epoch import resolve_actor_permission_epoch
from app.assistant.vault.vault_store import (
    PURPOSES,
    VaultStore,
    VaultStoreError,
)

logger = logging.getLogger(__name__)

# ── contrato de resultados: EXACTAMENTE los de 10.3.0, sin sinonimos ────────
AUTHORIZATION_CODES = (
    "authorized",
    "approval_required",
    "not_found",
    "revoked",
    "expired",
    "actor_mismatch",
    "permission_epoch_conflict",
    "purpose_mismatch",
    "version_conflict",
    "grant_consumed",
    "invalid_grant",
)

EXECUTION_CODES = (
    "success", "external_error", "timeout", "rejected", "unauthorized",
    "invalid_request",
)

GRANT_STATUSES = ("issued", "consumed", "expired", "revoked")

# Acciones permitidas. Cerrado: todas READ-ONLY en esta fase. Una accion de
# escritura entra cuando exista el carril completo, no antes.
ACTIONS = frozenset({"read_listings", "read_documents", "read_account"})

# Los huecos que el Broker sabe rellenar, y COMO los rellena. Que el formato lo
# decida el Broker y no quien llama es lo que impide un
# `<<secret:cualquier_cosa>>` con un molde arbitrario.
SECRET_SLOTS: dict[str, str] = {
    "AUTHORIZATION": "Bearer {valor}",
    "TOKEN": "{valor}",
    "API_KEY": "{valor}",
}

PLACEHOLDER_RE = re.compile(r"<<secret:([A-Z_]{1,32})>>")

GRANT_TTL_SECONDS = 180          # minutos, no horas: un grant es para AHORA
MAX_RESOURCE = 300
MAX_HEADER_VALUE = 4000
MAX_BODY_CHARS = 20000

_CONTROL_RE = re.compile(r"[\r\n\t\x00-\x1f]")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS vault_approval (
    id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    secret_id TEXT NOT NULL,
    purpose TEXT NOT NULL,
    action TEXT NOT NULL,
    resource TEXT NOT NULL,
    action_fingerprint TEXT NOT NULL,
    source TEXT NOT NULL,
    reason TEXT,
    correlation_id TEXT,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS vault_grant (
    id TEXT PRIMARY KEY,
    approval_id TEXT NOT NULL,
    secret_id TEXT NOT NULL,
    secret_version INTEGER NOT NULL,
    actor TEXT NOT NULL,
    purpose TEXT NOT NULL,
    action TEXT NOT NULL,
    resource TEXT NOT NULL,
    action_fingerprint TEXT NOT NULL,
    permission_epoch INTEGER,
    nonce TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'issued',
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    correlation_id TEXT,
    FOREIGN KEY (approval_id) REFERENCES vault_approval(id)
);

CREATE INDEX IF NOT EXISTS ix_grant_status ON vault_grant(status, expires_at);
CREATE INDEX IF NOT EXISTS ix_grant_secret ON vault_grant(secret_id);
"""


class BrokerError(Exception):
    """Mensaje fijo por codigo, como en el resto del vault."""

    _MENSAJES = {
        "invalid_request": "La peticion de ejecucion no es valida.",
        "invalid_source": "Origen no autorizado para aprobar.",
        "invalid_purpose": "Proposito no permitido.",
        "invalid_action": "Accion no permitida.",
        "not_found": "No existe ese secreto.",
        "write_failed": "No se pudo registrar la operacion.",
    }

    def __init__(self, code: str):
        self.code = code if code in self._MENSAJES else "invalid_request"
        super().__init__(self._MENSAJES[self.code])

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"BrokerError(code={self.code!r})"


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _limpia(raw: Any, maximo: int) -> str | None:
    if raw is None:
        return None
    t = _CONTROL_RE.sub(" ", str(raw)).strip()
    return t[:maximo] or None


# ───────────────────────────── la peticion de ejecucion

@dataclass(frozen=True)
class ExecutionRequest:
    """Lo que el llamador prepara. NUNCA lleva el secreto: lleva huecos.

    Por eso el orquestador puede construirla, registrarla y hasta loguearla
    entera sin riesgo: no tiene el valor y no puede tenerlo.

    Los huecos solo valen en `headers` y `body`. En la URL no: un secreto en una
    query string acaba en el log del proxy, en el historial y en el `Referer`,
    y eso ya no se puede retirar.
    """

    action: str
    resource: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    body: str | None = None

    def slots(self) -> tuple[str, ...]:
        texto = " ".join(list(self.headers.values()) + [self.body or ""])
        return tuple(dict.fromkeys(PLACEHOLDER_RE.findall(texto)))

    def to_public_dict(self) -> dict[str, Any]:
        """Seguro para auditar: son huecos, no valores."""
        return {"action": self.action, "resource": self.resource,
                "method": self.method, "slots": list(self.slots())}


@dataclass(frozen=True)
class PreparedRequest:
    """La peticion CON el secreto ya puesto. Solo existe dentro del Broker.

    No tiene `to_dict` ni `__repr__` util a proposito: cualquier volcado de
    este objeto seria un volcado del secreto.
    """

    action: str
    resource: str
    method: str
    headers: dict[str, str]
    body: str | None

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"PreparedRequest(action={self.action!r}, method={self.method!r})"


@dataclass(frozen=True)
class ExecutionResult:
    """Lo que devuelve el ejecutor. Pasa por saneado antes de salir."""

    code: str                      # uno de EXECUTION_CODES
    status: int | None = None
    body: str | None = None
    detail: str | None = None


class Executor(Protocol):
    """Abstraccion estrecha a proposito.

    No se reutiliza `tool_runner.run_plan_steps`: ese invoca el gateway
    READ-ONLY del propio ERP con el token M2M y no sabe nada de credenciales de
    terceros. Reutilizarlo arrastraria el orquestador dentro del Broker, que es
    justo la dependencia que esta fase existe para impedir.
    """

    def execute(self, prepared: PreparedRequest) -> ExecutionResult:
        ...


# ───────────────────────────── resultados

@dataclass(frozen=True)
class Grant:
    """El token consumible. NUNCA lleva el secreto ni el sobre."""

    id: str
    approval_id: str
    secret_id: str
    secret_version: int
    actor: str
    purpose: str
    action: str
    resource: str
    permission_epoch: int | None
    status: str
    issued_at: str
    expires_at: str
    consumed_at: str | None
    correlation_id: str | None

    def to_dict(self) -> dict[str, Any]:
        # `nonce` y `action_fingerprint` quedan fuera: son material de
        # anti-replay, no informacion para quien recibe el grant.
        return {
            "id": self.id, "approval_id": self.approval_id,
            "secret_id": self.secret_id, "secret_version": self.secret_version,
            "actor": self.actor, "purpose": self.purpose, "action": self.action,
            "resource": self.resource, "permission_epoch": self.permission_epoch,
            "status": self.status, "issued_at": self.issued_at,
            "expires_at": self.expires_at, "consumed_at": self.consumed_at,
            "correlation_id": self.correlation_id,
        }

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (f"Grant(id={self.id!r}, secret_id={self.secret_id!r}, "
                f"v={self.secret_version}, status={self.status!r})")


@dataclass(frozen=True)
class BrokerResult:
    """Dos ejes: si se autorizo, y si la ejecucion salio.

    `code` es siempre uno de los once de 10.3.0. `execution` solo existe cuando
    `code == "authorized"`: preguntar como fue la llamada cuando no hubo llamada
    no tiene respuesta.
    """

    code: str
    execution: str | None = None
    status: int | None = None
    body: str | None = None
    detail: str | None = None
    grant_id: str | None = None
    correlation_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.code == "authorized" and self.execution == "success"

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "execution": self.execution,
                "status": self.status, "body": self.body, "detail": self.detail,
                "grant_id": self.grant_id, "correlation_id": self.correlation_id}


# ───────────────────────────── saneado

def sanitize_result(result: ExecutionResult, secreto: bytes) -> ExecutionResult:
    """Quita el secreto de lo que devuelve el proveedor.

    Existe porque un servicio externo puede devolverlo: en un eco de la
    peticion, en un mensaje de error, en una cabecera reflejada. Es defensa en
    profundidad —lo principal es que el valor no salga del Broker—, pero es la
    unica capa que puede atrapar a un tercero indiscreto.

    Sustituye ocurrencias LITERALES. Un valor troceado o recodificado se le
    escapa, y eso se dice aqui en vez de fingir que no.
    """
    if not secreto:
        return result
    try:
        aguja = secreto.decode("utf-8")
    except UnicodeDecodeError:
        aguja = None
    import base64

    agujas = [a for a in (aguja, base64.b64encode(secreto).decode("ascii"),
                          secreto.hex()) if a]

    def _limpiar(texto: str | None) -> str | None:
        if not texto:
            return texto
        for a in agujas:
            if a and a in texto:
                texto = texto.replace(a, "[redacted]")
        return texto

    return ExecutionResult(code=result.code, status=result.status,
                           body=_limpiar(result.body),
                           detail=_limpiar(result.detail))


# ───────────────────────────── el Broker

class SecretBroker:
    """La unica puerta al uso de un secreto.

    No sabe de HTTP ni de interfaz. Recibe una aprobacion humana, emite un
    grant, y despues canjea ese grant ejecutando una accion concreta.
    """

    def __init__(self, store: VaultStore | None = None, *,
                 audit: OrchestratorAudit | None = None,
                 epoch_provider: Any = None):
        self._store = store if store is not None else VaultStore()
        self._audit = audit if audit is not None else OrchestratorAudit()
        self._epoch_provider = epoch_provider
        self._lock = threading.RLock()

    # -- infraestructura --------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        return self._store.connect()

    def ensure_schema(self) -> None:
        """Tablas propias en el MISMO `vault.db`. El store de 10.3.2 no se toca:
        las dos partes usan `CREATE TABLE IF NOT EXISTS` sobre un archivo que ya
        esta fuera de git y fuera de los backups."""
        self._store.ensure_schema()
        with self._lock:
            conn = self._conn()
            try:
                conn.executescript(SCHEMA_SQL)
            finally:
                conn.close()

    def _epoch(self, actor: str) -> int | None:
        r = resolve_actor_permission_epoch(actor, provider=self._epoch_provider)
        return int(r.epoch) if r.available and r.epoch is not None else None

    @staticmethod
    def canonical_approval(action: str, resource: str,
                           request: ExecutionRequest | None) -> dict[str, Any]:
        """Que se aprueba, exactamente. LA estructura, no una descripcion.

        Cubre metodo, recurso y la FORMA de la peticion —cabeceras y huecos—,
        no su contenido secreto. Aprobar "la accion X" y ejecutar X' es la
        amenaza D de 10.3.0, y esto es lo que la cierra.

        FASE 10.3.4-A — es publica porque la pantalla de aprobacion tiene que
        enseñar esto y nada mas que esto. Si la vista se construyera aparte,
        acabaria ensenando una cosa mientras se firma otra: no por malicia,
        sino porque dos representaciones de lo mismo se separan con el tiempo.
        Aqui hay UNA, y la huella es su sha256.

        No lleva valores: `header_keys` son claves sin valor y `slots` son
        nombres de hueco. Por eso este diccionario puede viajar al navegador.
        """
        return {
            "action": action, "resource": resource,
            "method": (request.method if request else "").upper(),
            "header_keys": sorted((request.headers or {}) if request else {}),
            "slots": list(request.slots()) if request else [],
            "has_body": bool(request and request.body),
        }

    @staticmethod
    def canonical_bytes(canonico: dict[str, Any]) -> bytes:
        return json.dumps(canonico, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")

    @classmethod
    def fingerprint_of(cls, canonico: dict[str, Any]) -> str:
        """La huella de una estructura canonica ya construida.

        Que la pantalla pueda calcular la huella de lo que muestra es lo que
        convierte "lo que ves es lo que firmas" en algo comprobable en vez de
        una promesa.
        """
        import hashlib

        return hashlib.sha256(cls.canonical_bytes(canonico)).hexdigest()[:32]

    @classmethod
    def _huella(cls, action: str, resource: str,
                request: ExecutionRequest | None) -> str:
        return cls.fingerprint_of(cls.canonical_approval(action, resource,
                                                         request))

    def _auditar(self, evento: str, **campos: Any) -> None:
        """Lista blanca, campo a campo. Nunca `**kwargs` de quien llama.

        Un `**kwargs` en un evento de auditoria es como acaba un header entero
        —o un cuerpo— en un log append-only del que ya no se saca.
        """
        registro = {
            "event": evento,
            "grant_id": campos.get("grant_id"),
            "approval_id": campos.get("approval_id"),
            "actor_user": campos.get("actor"),
            "secret_id": campos.get("secret_id"),
            "secret_version": campos.get("secret_version"),
            "action": campos.get("action"),
            "purpose": campos.get("purpose"),
            "resource": campos.get("resource"),
            "correlation_id": campos.get("correlation_id"),
            "permission_epoch": campos.get("permission_epoch"),
            "result": campos.get("result"),
            "execution": campos.get("execution"),
        }
        try:
            self._audit.write(registro)
        except Exception:  # noqa: BLE001 - auditar no puede tumbar la operacion
            logger.warning("vault_broker: no se pudo auditar %s", evento)

    # -- validacion de la peticion ----------------------------------------

    def validate_request(self, request: ExecutionRequest) -> tuple[str, ...]:
        """Comprueba la FORMA antes de tocar nada. Devuelve los huecos usados."""
        if not isinstance(request, ExecutionRequest):
            raise BrokerError("invalid_request")
        if request.action not in ACTIONS:
            raise BrokerError("invalid_action")
        recurso = _limpia(request.resource, MAX_RESOURCE)
        if not recurso:
            raise BrokerError("invalid_request")
        if PLACEHOLDER_RE.search(request.resource or ""):
            # Un secreto en la URL acaba en el log del proxy y en el `Referer`.
            raise BrokerError("invalid_request")
        if (request.method or "").upper() not in ("GET", "POST", "PUT", "DELETE"):
            raise BrokerError("invalid_request")
        if not isinstance(request.headers, dict):
            raise BrokerError("invalid_request")
        for k, v in request.headers.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise BrokerError("invalid_request")
            if len(v) > MAX_HEADER_VALUE:
                raise BrokerError("invalid_request")
        if request.body is not None:
            if not isinstance(request.body, str) or len(request.body) > MAX_BODY_CHARS:
                raise BrokerError("invalid_request")

        texto = " ".join(list(request.headers.values()) + [request.body or ""])
        # Cualquier `<<secret:...>>` con un nombre que no este declarado se
        # rechaza. Sin esto, `<<secret:cualquier_cosa>>` pediria un molde
        # arbitrario y el Broker seria una plantilla con acceso a credenciales.
        for nombre in PLACEHOLDER_RE.findall(texto):
            if nombre not in SECRET_SLOTS:
                raise BrokerError("invalid_request")
        if "<<secret:" in texto and not PLACEHOLDER_RE.search(texto):
            raise BrokerError("invalid_request")
        huecos = request.slots()
        if not huecos:
            # Un grant que no inyecta nada es un grant para nada.
            raise BrokerError("invalid_request")
        return huecos

    # -- aprobacion ---------------------------------------------------------

    def approve_and_issue_grant(
        self, *, actor: str, secret_id: str, purpose: str,
        request: ExecutionRequest, source: str,
        reason: str | None = None, correlation_id: str | None = None,
        ttl_seconds: int = GRANT_TTL_SECONDS,
    ) -> tuple[str, Grant] | tuple[str, None]:
        """La decision humana, y el token que emite. Devuelve `(codigo, grant)`.

        `source` es la lista cerrada de 10.2.3: aprobar es un acto de la
        interfaz autorizada, no una frase que el modelo pueda emitir. Un
        documento que diga "aprueba esto" llega como dato, no como llamada.
        """
        self.ensure_schema()
        actor = (actor or "").strip()
        origen = (source or "").strip().lower()
        corr = _limpia(correlation_id, 64)
        motivo = _limpia(reason, MAX_REASON)

        if origen not in SOURCES_HUMANAS:
            self._auditar("grant_denied", actor=actor, secret_id=secret_id,
                          purpose=purpose, correlation_id=corr,
                          result="invalid_source")
            raise BrokerError("invalid_source")
        purpose = (purpose or "").strip().lower()
        if purpose not in PURPOSES:
            self._auditar("grant_denied", actor=actor, secret_id=secret_id,
                          correlation_id=corr, result="invalid_purpose")
            raise BrokerError("invalid_purpose")
        self.validate_request(request)

        # El secreto tiene que existir, ser de este actor, estar utilizable Y
        # declarar ese proposito. Aprobar algo que ya no se puede usar seria
        # emitir un grant nacido muerto.
        try:
            meta = self._store.get_secret_metadata(secret_id=secret_id,
                                                   owner_actor=actor)
        except VaultStoreError as exc:
            self._auditar("grant_denied", actor=actor, secret_id=secret_id,
                          purpose=purpose, correlation_id=corr,
                          result=exc.code)
            return (exc.code if exc.code in AUTHORIZATION_CODES else "not_found",
                    None)
        codigo = self._estado_utilizable(meta)
        if codigo != "authorized":
            self._auditar("grant_denied", actor=actor, secret_id=secret_id,
                          purpose=purpose, correlation_id=corr, result=codigo)
            return codigo, None
        if purpose not in meta.purposes:
            self._auditar("grant_denied", actor=actor, secret_id=secret_id,
                          purpose=purpose, correlation_id=corr,
                          result="purpose_mismatch")
            return "purpose_mismatch", None

        ahora = _ahora()
        epoch = self._epoch(actor)
        if epoch is None:
            # Sin fuente de epoch no hay forma de enterarse de que los permisos
            # del actor cambiaron entre la aprobacion y el uso, que es lo unico
            # que este campo existe para detectar. Emitir igualmente seria
            # degradar el modelo en silencio: se falla cerrado.
            self._auditar("grant_denied", actor=actor, secret_id=secret_id,
                          purpose=purpose, correlation_id=corr,
                          result="permission_epoch_unavailable")
            return "permission_epoch_conflict", None
        huella = self._huella(request.action, request.resource, request)
        approval_id = str(uuid.uuid4())
        grant_id = str(uuid.uuid4())
        ttl = max(1, min(int(ttl_seconds), 3600))

        with self._lock:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO vault_approval (id, actor, secret_id, purpose, "
                    "action, resource, action_fingerprint, source, reason, "
                    "correlation_id, created_at, status) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (approval_id, actor, meta.id, purpose, request.action,
                     request.resource, huella, origen, motivo, corr,
                     _iso(ahora), "active"))
                conn.execute(
                    "INSERT INTO vault_grant (id, approval_id, secret_id, "
                    "secret_version, actor, purpose, action, resource, "
                    "action_fingerprint, permission_epoch, nonce, status, "
                    "issued_at, expires_at, consumed_at, correlation_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)",
                    (grant_id, approval_id, meta.id, int(meta.current_version),
                     actor, purpose, request.action, request.resource, huella,
                     epoch, secrets.token_urlsafe(16), "issued", _iso(ahora),
                     _iso(ahora + timedelta(seconds=ttl)), corr))
                conn.execute("COMMIT")
            except sqlite3.Error:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise BrokerError("write_failed") from None
            finally:
                conn.close()

        grant = self.get_grant(grant_id)
        self._auditar("grant_issued", grant_id=grant_id, approval_id=approval_id,
                      actor=actor, secret_id=meta.id,
                      secret_version=meta.current_version, action=request.action,
                      purpose=purpose, resource=request.resource,
                      correlation_id=corr, permission_epoch=epoch, result="ok")
        return "authorized", grant

    @staticmethod
    def _estado_utilizable(meta: Any) -> str:
        if meta.status == "revoked":
            return "revoked"
        if meta.status == "expired":
            return "expired"
        if meta.expires_at and str(meta.expires_at) <= _iso(_ahora()):
            return "expired"
        if meta.current_version is None:
            return "not_found"
        return "authorized"

    def get_grant(self, grant_id: str) -> Grant | None:
        self.ensure_schema()
        conn = self._conn()
        try:
            r = conn.execute("SELECT * FROM vault_grant WHERE id = ?",
                             (str(grant_id or ""),)).fetchone()
        finally:
            conn.close()
        if r is None:
            return None
        return self._grant_de_fila(r)

    @staticmethod
    def _grant_de_fila(r: sqlite3.Row) -> Grant:
        return Grant(
            id=r["id"], approval_id=r["approval_id"], secret_id=r["secret_id"],
            secret_version=r["secret_version"], actor=r["actor"],
            purpose=r["purpose"], action=r["action"], resource=r["resource"],
            permission_epoch=r["permission_epoch"], status=r["status"],
            issued_at=r["issued_at"], expires_at=r["expires_at"],
            consumed_at=r["consumed_at"], correlation_id=r["correlation_id"])

    def list_grants(self, *, actor: str, secret_id: str | None = None,
                    limit: int = 50) -> list[Grant]:
        """Los grants de UN actor. Metadatos, nunca material.

        FASE 10.3.4-A — la pantalla necesita ensenar que hay vivo ahora mismo.
        El filtro por actor esta en el SQL, no en quien llama: una lista que se
        filtra despues es una lista que alguna vez se devuelve entera.
        """
        self.ensure_schema()
        a = (actor or "").strip()
        if not a:
            return []
        conn = self._conn()
        try:
            sql = "SELECT * FROM vault_grant WHERE actor = ?"
            params: list[Any] = [a]
            if secret_id:
                sql += " AND secret_id = ?"
                params.append(str(secret_id))
            sql += " ORDER BY issued_at DESC LIMIT ?"
            params.append(max(1, min(int(limit), 200)))
            filas = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        return [self._grant_de_fila(r) for r in filas]

    def usage_history(self, *, actor: str, secret_id: str,
                      limit: int = 20) -> list[dict[str, Any]]:
        """Historial de uso de un secreto: quien autorizo que, y como acabo.

        Sale de `vault_grant` unida a `vault_approval` —las dos tablas del
        Broker— y no del log de auditoria: el log es append-only y puede estar
        rotado, mientras que estas filas son el estado real. Campos elegidos a
        mano; aqui no hay `SELECT *` que viaje al navegador.
        """
        self.ensure_schema()
        a = (actor or "").strip()
        if not a or not secret_id:
            return []
        conn = self._conn()
        try:
            filas = conn.execute(
                "SELECT g.id, g.status, g.action, g.purpose, g.resource, "
                "       g.secret_version, g.issued_at, g.expires_at, "
                "       g.consumed_at, ap.source, ap.actor AS aprobado_por "
                "  FROM vault_grant g "
                "  JOIN vault_approval ap ON ap.id = g.approval_id "
                " WHERE g.actor = ? AND g.secret_id = ? "
                " ORDER BY g.issued_at DESC LIMIT ?",
                (a, str(secret_id), max(1, min(int(limit), 100)))).fetchall()
        finally:
            conn.close()
        return [{
            "grant_id": r["id"], "status": r["status"], "action": r["action"],
            "purpose": r["purpose"], "resource": r["resource"],
            "secret_version": r["secret_version"], "issued_at": r["issued_at"],
            "expires_at": r["expires_at"], "consumed_at": r["consumed_at"],
            "approved_by": r["aprobado_por"], "approval_source": r["source"],
        } for r in filas]

    def revoke_grant(self, grant_id: str, *, actor: str | None = None) -> bool:
        """Mata un grant vivo. Con `actor`, solo si es suyo —en el SQL."""
        self.ensure_schema()
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                if actor is not None:
                    cur = conn.execute(
                        "UPDATE vault_grant SET status='revoked' WHERE id=? "
                        "AND status='issued' AND actor=?",
                        (str(grant_id or ""), (actor or "").strip()))
                else:
                    cur = conn.execute(
                        "UPDATE vault_grant SET status='revoked' WHERE id=? AND "
                        "status='issued'", (str(grant_id or ""),))
                conn.execute("COMMIT")
                return bool(cur.rowcount)
            except sqlite3.Error:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                return False
            finally:
                conn.close()

    # -- el canje ----------------------------------------------------------

    def execute_with_secret(self, *, grant_id: str, actor: str,
                            request: ExecutionRequest,
                            executor: Executor) -> BrokerResult:
        """Canjea el grant y ejecuta. El secreto no sale de aqui.

        Orden, y cada paso esta donde esta por una razon:

          1. validar la FORMA de la peticion — antes de tocar la base
          2. en UNA transaccion: releer grant y secreto, revalidar A-L, y
             CONSUMIR atomicamente
          3. fuera de la transaccion: descifrar la version pinchada, sustituir,
             ejecutar
          4. sanear el resultado y auditar

        Revalidar dentro de la transaccion es lo que impide que el secreto se
        revoque entre la comprobacion y el consumo. Y descifrar la version
        PINCHADA —no "la actual"— es lo que impide ejecutar con una credencial
        distinta de la aprobada si alguien roto entremedio.
        """
        self.ensure_schema()
        actor = (actor or "").strip()
        try:
            self.validate_request(request)
        except BrokerError:
            self._auditar("grant_denied", grant_id=grant_id, actor=actor,
                          result="invalid_grant")
            return BrokerResult(code="invalid_grant", grant_id=grant_id)

        codigo, datos = self._consumir(grant_id, actor, request)
        corr = (datos or {}).get("correlation_id")
        if codigo != "authorized":
            self._auditar("grant_denied", grant_id=grant_id, actor=actor,
                          secret_id=(datos or {}).get("secret_id"),
                          secret_version=(datos or {}).get("secret_version"),
                          action=request.action, purpose=(datos or {}).get("purpose"),
                          resource=request.resource, correlation_id=corr,
                          permission_epoch=(datos or {}).get("permission_epoch"),
                          result=codigo)
            return BrokerResult(code=codigo, grant_id=grant_id,
                                correlation_id=corr)

        self._auditar("grant_consumed", grant_id=grant_id, actor=actor,
                      secret_id=datos["secret_id"],
                      secret_version=datos["secret_version"],
                      action=request.action, purpose=datos["purpose"],
                      resource=request.resource, correlation_id=corr,
                      permission_epoch=datos["permission_epoch"], result="ok")

        # A partir de aqui el grant YA esta gastado. Si algo falla, no se
        # devuelve: volver a intentar exige otra aprobacion, que es la direccion
        # segura para una credencial.
        return self._ejecutar(datos, actor, request, executor, corr)

    def _consumir(self, grant_id: str, actor: str,
                  request: ExecutionRequest) -> tuple[str, dict[str, Any] | None]:
        """Revalidacion completa + consumo, todo en una transaccion.

        El consumo es `UPDATE ... WHERE status='issued'` y se mira `rowcount`:
        comprobar con un SELECT y actualizar despues dejaria la ventana por la
        que dos hilos gastan el mismo grant.
        """
        huella = self._huella(request.action, request.resource, request)
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                g = conn.execute("SELECT * FROM vault_grant WHERE id = ?",
                                 (str(grant_id or ""),)).fetchone()
                if g is None:
                    conn.execute("ROLLBACK")
                    return "invalid_grant", None

                datos = {
                    "secret_id": g["secret_id"],
                    "secret_version": g["secret_version"],
                    "purpose": g["purpose"],
                    "permission_epoch": g["permission_epoch"],
                    "correlation_id": g["correlation_id"],
                    "approval_id": g["approval_id"],
                }

                # L. estado del grant
                if g["status"] == "consumed":
                    conn.execute("ROLLBACK")
                    return "grant_consumed", datos
                if g["status"] in ("revoked", "expired"):
                    conn.execute("ROLLBACK")
                    return "invalid_grant", datos
                # F. caducidad del grant: se calcula, no se cree lo almacenado
                if str(g["expires_at"]) <= _iso(_ahora()):
                    conn.execute(
                        "UPDATE vault_grant SET status='expired' WHERE id=? AND "
                        "status='issued'", (g["id"],))
                    conn.execute("COMMIT")
                    return "expired", datos
                # A. actor
                if str(g["actor"]) != actor:
                    conn.execute("ROLLBACK")
                    return "actor_mismatch", datos
                # H/I/J. proposito, accion y recurso, por igualdad
                if str(g["purpose"]) != str(g["purpose"]).lower() or \
                        str(g["action"]) != request.action or \
                        str(g["resource"]) != request.resource:
                    conn.execute("ROLLBACK")
                    return "purpose_mismatch", datos
                # La huella cubre la FORMA de la peticion: aprobar X y ejecutar
                # X' es la amenaza D.
                if str(g["action_fingerprint"]) != huella:
                    conn.execute("ROLLBACK")
                    return "purpose_mismatch", datos
                # K. la aprobacion sigue viva
                a = conn.execute("SELECT * FROM vault_approval WHERE id = ?",
                                 (g["approval_id"],)).fetchone()
                if a is None or a["status"] != "active":
                    conn.execute("ROLLBACK")
                    return "approval_required", datos

                # B/C/D/E. el secreto, releido AHORA
                s = conn.execute(
                    "SELECT * FROM vault_secret WHERE id = ? AND owner_actor = ?",
                    (g["secret_id"], actor)).fetchone()
                if s is None:
                    conn.execute("ROLLBACK")
                    return "not_found", datos
                if s["status"] == "revoked":
                    conn.execute("ROLLBACK")
                    return "revoked", datos
                if s["status"] == "expired" or (
                        s["expires_at"] and str(s["expires_at"]) <= _iso(_ahora())):
                    conn.execute("ROLLBACK")
                    return "expired", datos
                if int(s["current_version"] or 0) != int(g["secret_version"]):
                    conn.execute("ROLLBACK")
                    return "version_conflict", datos
                if str(g["purpose"]) not in json.loads(s["purposes_json"] or "[]"):
                    conn.execute("ROLLBACK")
                    return "purpose_mismatch", datos

                # G. epoch: se resuelve AHORA y se compara con el del grant
                actual = self._epoch(actor)
                if actual is None or actual != g["permission_epoch"]:
                    conn.execute("ROLLBACK")
                    return "permission_epoch_conflict", datos

                cur = conn.execute(
                    "UPDATE vault_grant SET status='consumed', consumed_at=? "
                    "WHERE id=? AND status='issued'", (_iso(_ahora()), g["id"]))
                if not cur.rowcount:
                    conn.execute("ROLLBACK")
                    return "grant_consumed", datos
                conn.execute("COMMIT")
                return "authorized", datos
            except sqlite3.Error:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                return "invalid_grant", None
            finally:
                conn.close()

    def _ejecutar(self, datos: dict[str, Any], actor: str,
                  request: ExecutionRequest, executor: Executor,
                  corr: str | None) -> BrokerResult:
        resultado: dict[str, Any] = {}

        def _con_el_secreto(claro: bytes) -> None:
            """El unico sitio del proceso donde existe el texto plano.

            Se sustituye, se ejecuta y se sanea aqui dentro. Lo que sale de esta
            funcion ya no lo contiene.
            """
            preparada = self._sustituir(request, claro)
            try:
                bruto = executor.execute(preparada)
            except TimeoutError:
                bruto = ExecutionResult(code="timeout")
            except Exception as exc:  # noqa: BLE001
                # El detalle es el TIPO, no el mensaje: un mensaje de una
                # libreria externa puede llevar la peticion entera dentro.
                bruto = ExecutionResult(code="external_error",
                                        detail=type(exc).__name__)
            if not isinstance(bruto, ExecutionResult):
                bruto = ExecutionResult(code="invalid_request")
            if bruto.code not in EXECUTION_CODES:
                bruto = ExecutionResult(code="external_error",
                                        status=bruto.status)
            limpio = sanitize_result(bruto, claro)
            resultado["r"] = limpio

        try:
            self._store.with_version_plaintext(
                secret_id=datos["secret_id"], owner_actor=actor,
                version=int(datos["secret_version"]), consumer=_con_el_secreto)
        except VaultStoreError as exc:
            # El grant ya esta gastado: se revoco o roto entre el consumo y el
            # descifrado. Se falla cerrado y queda registrado.
            codigo = {"revoked": "revoked", "expired": "expired",
                      "no_active_version": "version_conflict",
                      "not_found": "not_found"}.get(exc.code, "invalid_grant")
            self._auditar("secret_use_failed", grant_id=None, actor=actor,
                          secret_id=datos["secret_id"],
                          secret_version=datos["secret_version"],
                          action=request.action, purpose=datos["purpose"],
                          resource=request.resource, correlation_id=corr,
                          permission_epoch=datos["permission_epoch"],
                          result=codigo)
            return BrokerResult(code=codigo, correlation_id=corr)

        r: ExecutionResult = resultado["r"]
        evento = "secret_use_success" if r.code == "success" else "secret_use_failed"
        self._auditar(evento, actor=actor, secret_id=datos["secret_id"],
                      secret_version=datos["secret_version"],
                      action=request.action, purpose=datos["purpose"],
                      resource=request.resource, correlation_id=corr,
                      permission_epoch=datos["permission_epoch"],
                      result="ok" if r.code == "success" else r.code,
                      execution=r.code)
        return BrokerResult(code="authorized", execution=r.code, status=r.status,
                            body=r.body, detail=r.detail, correlation_id=corr)

    @staticmethod
    def _sustituir(request: ExecutionRequest, claro: bytes) -> PreparedRequest:
        """Rellena los huecos. SOLO en cabeceras y cuerpo, nunca en la URL.

        El molde de cada hueco lo decide `SECRET_SLOTS`, no quien llama: asi
        `<<secret:AUTHORIZATION>>` siempre produce un `Bearer`, y nadie puede
        pedir el valor pelado donde no toca.
        """
        try:
            valor = claro.decode("utf-8")
        except UnicodeDecodeError:
            import base64

            valor = base64.b64encode(claro).decode("ascii")

        def _rellenar(texto: str) -> str:
            def _uno(m: re.Match[str]) -> str:
                molde = SECRET_SLOTS.get(m.group(1))
                if molde is None:  # pragma: no cover - ya validado antes
                    raise BrokerError("invalid_request")
                return molde.format(valor=valor)

            return PLACEHOLDER_RE.sub(_uno, texto)

        return PreparedRequest(
            action=request.action, resource=request.resource,
            method=(request.method or "GET").upper(),
            headers={k: _rellenar(v) for k, v in (request.headers or {}).items()},
            body=_rellenar(request.body) if request.body else None)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"SecretBroker(store={self._store!r})"
