"""FASE 10.3.2 — persistencia y ciclo de vida del Secret Vault.

POR QUE UNA BASE APARTE Y NO UNA TABLA MAS EN `andes.db`

El descubrimiento de 10.3.0 lo decidio, y no es una preferencia de estilo:

  - `gdrive_backup.py` sube un snapshot de `data/andes.db` a Google Drive.
  - `scripts/sync_db_to_render.py` lo empaqueta y lo manda a la nube.
  - `data/andes.db` esta VERSIONADO en git, hoy protegido solo por
    `skip-worktree`. Un `--no-skip-worktree` descuidado publicaria su
    contenido en GitHub.

Tres rutas por las que ese archivo sale de esta maquina. Meter criptograma ahi
seria multiplicar por tres los sitios donde alguien tiene que acordarse de
proteger algo. `vault.db` no entra en ninguna de las tres, y la KEK no entra en
`vault.db`: un backup filtrado no contiene ni criptograma, y un `vault.db`
filtrado no contiene nada legible.

DOS TABLAS, Y NO ES NORMALIZACION

`vault_secret` es la referencia: lo que circula por un listado, por un panel,
por una propuesta. `vault_secret_version` guarda el sobre. Que sean dos hace
que una consulta de metadata **fisicamente no pueda** traer criptograma,
porque no lo consulta. Una sola tabla con una columna que "no hay que
seleccionar" es una regla que alguien incumple el martes.

NO HAY `get_plaintext()`

Y es deliberado. El texto plano se entrega a un `consumer` que se pasa a
`with_current_plaintext`, y lo que vuelve es lo que ese consumer devuelva. Si
existiera una funcion publica que devolviera el secreto, la pregunta "quien
puede filtrarlo" tendria tantas respuestas como llamadores. Asi tiene una, y en
10.3.3 esa una sera el Broker.

EL CONTEXTO DE CIFRADO SE CONSTRUYE DESDE LA FILA

Nunca se acepta un `EncryptionContext` de quien llama. El AAD sale de
`secret_id`, `version`, `owner_actor` y `key_version` leidos de la base, asi
que un sobre movido a otra fila —o a otro dueño— falla al descifrar en vez de
entregar su contenido.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.assistant.vault.vault_crypto import (
    EncryptionContext,
    Envelope,
    VaultCryptoError,
    decrypt,
    encrypt,
    rewrap,
)
from app.assistant.vault.vault_keys import VaultKeyring, VaultKeyringError

logger = logging.getLogger(__name__)

ENV_DB_PATH = "ANDES_VAULT_DB"
DEFAULT_DB = Path("data/vault.db")

SCOPES = frozenset({"user"})          # `company` es 10.2.7 / 10.3.x, no existe
PROVIDERS = frozenset({"mercadolibre", "sii", "generico"})
PURPOSES = frozenset({"read_listings", "read_documents", "read_account"})

SECRET_STATUSES = frozenset({"active", "revoked", "expired"})
VERSION_STATUSES = frozenset({"active", "superseded", "revoked"})

MAX_NAME = 120
MAX_METADATA_CHARS = 2000

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS vault_secret (
    id TEXT PRIMARY KEY,
    owner_actor TEXT NOT NULL,
    scope TEXT NOT NULL,
    name TEXT NOT NULL,
    provider TEXT NOT NULL,
    purposes_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    current_version INTEGER,
    permission_epoch INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT,
    metadata_json TEXT
);

-- El sobre vive APARTE. Una consulta de metadata no lo toca.
CREATE TABLE IF NOT EXISTS vault_secret_version (
    secret_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    key_version INTEGER NOT NULL,
    envelope_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    created_by TEXT,
    superseded_at TEXT,
    revoked_at TEXT,
    PRIMARY KEY (secret_id, version),
    FOREIGN KEY (secret_id) REFERENCES vault_secret(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_vault_owner_status
    ON vault_secret(owner_actor, status);

CREATE INDEX IF NOT EXISTS ix_vault_version_key
    ON vault_secret_version(key_version);

-- Un nombre por dueño: sin esto, "la credencial de MercadoLibre" es ambigua
-- justo en la pantalla donde alguien decide usarla.
CREATE UNIQUE INDEX IF NOT EXISTS ux_vault_owner_name
    ON vault_secret(owner_actor, scope, name);
"""


class VaultStoreError(Exception):
    """Mensaje fijo por codigo. Igual que en vault_crypto y por lo mismo:
    interpolar es como acaban los valores en los logs."""

    _MENSAJES = {
        "vault_locked": "El vault no puede abrirse: no hay clave utilizable.",
        "not_found": "No existe ese secreto.",
        "version_not_found": "No existe esa version.",
        "invalid_argument": "Argumento no valido.",
        "invalid_scope": "Scope no permitido.",
        "invalid_provider": "Proveedor no permitido.",
        "invalid_purpose": "Proposito no permitido.",
        "duplicate_name": "Ya existe un secreto con ese nombre.",
        "revoked": "El secreto esta revocado.",
        "expired": "El secreto esta caducado.",
        "no_active_version": "El secreto no tiene una version utilizable.",
        "corrupt_envelope": "El sobre almacenado no se puede interpretar.",
        "authentication_failed": "El sobre no se pudo autenticar.",
        "unknown_key_version": "No hay clave para la version del sobre.",
        "write_failed": "No se pudo escribir en el vault.",
        "confirmation_required": "El borrado definitivo exige confirmacion.",
    }

    def __init__(self, code: str):
        self.code = code if code in self._MENSAJES else "invalid_argument"
        super().__init__(self._MENSAJES[self.code])

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"VaultStoreError(code={self.code!r})"


def _ahora() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def vault_db_path(path: Path | str | None = None) -> Path:
    """Configurable para tests; por defecto `data/vault.db`.

    NO se reutiliza el URI de `create_app()` a proposito: esta fijado a
    `data/andes.db` sin variable que lo redirija, y colgarse de el haria
    imposible probar el aislamiento — que es justamente la propiedad que este
    modulo existe para tener.
    """
    if path is not None:
        return Path(path)
    return Path(os.environ.get(ENV_DB_PATH) or DEFAULT_DB)


@dataclass(frozen=True)
class SecretMetadata:
    """La referencia. Nunca lleva sobre, ni clave, ni texto plano."""

    id: str
    owner_actor: str
    scope: str
    name: str
    provider: str
    purposes: tuple[str, ...]
    status: str
    current_version: int | None
    permission_epoch: int | None
    created_at: str
    updated_at: str
    expires_at: str | None
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        d = {
            "id": self.id, "owner_actor": self.owner_actor, "scope": self.scope,
            "name": self.name, "provider": self.provider,
            "purposes": list(self.purposes), "status": self.status,
            "current_version": self.current_version,
            "permission_epoch": self.permission_epoch,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "expires_at": self.expires_at, "metadata": dict(self.metadata),
        }
        return d

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (f"SecretMetadata(id={self.id!r}, name={self.name!r}, "
                f"status={self.status!r}, v={self.current_version})")


@dataclass(frozen=True)
class VersionMetadata:
    """La version, SIN el sobre. Para inventario y rotacion."""

    secret_id: str
    version: int
    key_version: int
    status: str
    created_at: str
    created_by: str | None
    superseded_at: str | None
    revoked_at: str | None


def _valida_texto(valor: Any, *, maximo: int, code: str) -> str:
    if not isinstance(valor, str):
        raise VaultStoreError(code)
    v = valor.strip()
    if not v or len(v) > maximo or "\x00" in v:
        raise VaultStoreError(code)
    return v


class VaultStore:
    """Almacen cifrado. Un archivo, dos tablas, y ninguna puerta al texto plano.

    NO decide autorizacion: no sabe quien pregunta ni con que permiso. El
    `owner_actor` se usa como PARTE DE LA IDENTIDAD del secreto —entra en el
    AAD— y para no devolver lo ajeno, pero quien puede usar que es del Broker.
    """

    def __init__(self, path: Path | str | None = None, *,
                 keyring: VaultKeyring | None = None):
        self.path = vault_db_path(path)
        self._keyring = keyring if keyring is not None else VaultKeyring()
        self._lock = threading.RLock()

    # -- conexion ---------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            # WAL solo en ESTE archivo. El resto del proyecto usa el journal
            # por defecto y no se toca; aqui vale la pena porque permite leer
            # metadata mientras una rotacion escribe, en vez de esperar al lock.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
        except BaseException:
            # Sobre un archivo corrupto el PRAGMA es lo primero que falla, y la
            # conexion quedaria abierta reteniendo el archivo. En Windows eso
            # ademas impide borrarlo: un `status()` sobre una base rota dejaria
            # el archivo bloqueado para siempre.
            conn.close()
            raise
        return conn

    def ensure_schema(self) -> None:
        with self._lock:
            conn = self.connect()
            try:
                conn.executescript(SCHEMA_SQL)
            finally:
                conn.close()

    # -- estado del vault -------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Distingue `vault vacio` de `vault inaccesible`, que es el punto.

        Confundirlos convierte una perdida de clave en una perdida de datos
        silenciosa: el sistema arrancaria diciendo "no hay secretos" cuando lo
        cierto es "no puedo abrirlos".
        """
        existe = self.path.exists()
        n = None
        if existe:
            try:
                self.ensure_schema()
                conn = self.connect()
                try:
                    n = int(conn.execute(
                        "SELECT COUNT(*) FROM vault_secret").fetchone()[0])
                finally:
                    conn.close()
            except sqlite3.Error:
                return {"state": "corrupt", "db_exists": True, "secrets": None,
                        "keyring": self._keyring.estado}
        estado_llavero = self._keyring.estado
        if estado_llavero != "open":
            return {
                "state": "locked" if (n or 0) > 0 or existe else "uninitialized",
                "db_exists": existe, "secrets": n, "keyring": estado_llavero,
            }
        return {"state": "open", "db_exists": existe, "secrets": n,
                "keyring": estado_llavero}

    def _exigir_abierto(self) -> None:
        try:
            self._keyring.exigir_abierto()
        except VaultKeyringError:
            raise VaultStoreError("vault_locked") from None

    # -- lectura de filas -------------------------------------------------

    def _fila_secreto(self, conn: sqlite3.Connection, secret_id: str,
                      owner: str | None) -> sqlite3.Row:
        sql = "SELECT * FROM vault_secret WHERE id = ?"
        params: list[Any] = [str(secret_id or "")]
        if owner is not None:
            sql += " AND owner_actor = ?"
            params.append(owner)
        fila = conn.execute(sql, params).fetchone()
        if fila is None:
            raise VaultStoreError("not_found")
        return fila

    @staticmethod
    def _meta(fila: sqlite3.Row) -> SecretMetadata:
        def _json(raw, default):
            try:
                return json.loads(raw) if raw else default
            except ValueError:
                return default

        return SecretMetadata(
            id=fila["id"], owner_actor=fila["owner_actor"], scope=fila["scope"],
            name=fila["name"], provider=fila["provider"],
            purposes=tuple(_json(fila["purposes_json"], [])),
            status=fila["status"], current_version=fila["current_version"],
            permission_epoch=fila["permission_epoch"],
            created_at=fila["created_at"], updated_at=fila["updated_at"],
            expires_at=fila["expires_at"],
            metadata=_json(fila["metadata_json"], {}))

    def _contexto(self, fila_secreto: sqlite3.Row, version: int,
                  key_version: int) -> EncryptionContext:
        """El AAD sale de la FILA, jamas de quien llama.

        Si alguien pudiera pasar su propio contexto, podria pedir el descifrado
        de un sobre ajeno declarando el dueño correcto — y el AAD dejaria de
        atar nada.
        """
        return EncryptionContext(
            secret_id=str(fila_secreto["id"]), version=int(version),
            owner=str(fila_secreto["owner_actor"]), key_version=int(key_version))

    # -- creacion ---------------------------------------------------------

    def create_secret(self, *, owner_actor: str, name: str, provider: str,
                      purposes: list[str] | tuple[str, ...], plaintext: bytes,
                      scope: str = "user", expires_at: str | None = None,
                      permission_epoch: int | None = None,
                      metadata: dict[str, Any] | None = None,
                      created_by: str | None = None) -> SecretMetadata:
        """`plaintext -> encrypt -> sobre -> fila`. El texto plano nunca toca SQL.

        Se cifra ANTES de abrir la transaccion: si el cifrado falla no se ha
        escrito nada, y el plaintext vive lo menos posible.
        """
        self._exigir_abierto()
        owner = _valida_texto(owner_actor, maximo=80, code="invalid_argument")
        nombre = _valida_texto(name, maximo=MAX_NAME, code="invalid_argument")
        sc = (scope or "").strip().lower()
        if sc not in SCOPES:
            raise VaultStoreError("invalid_scope")
        prov = (provider or "").strip().lower()
        if prov not in PROVIDERS:
            raise VaultStoreError("invalid_provider")
        props = tuple(dict.fromkeys(str(p).strip().lower() for p in (purposes or ())))
        if not props or any(p not in PURPOSES for p in props):
            raise VaultStoreError("invalid_purpose")
        if not isinstance(plaintext, (bytes, bytearray)):
            raise VaultStoreError("invalid_argument")
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        if len(meta_json) > MAX_METADATA_CHARS:
            raise VaultStoreError("invalid_argument")

        self.ensure_schema()
        secret_id = str(uuid.uuid4())
        key_version = self._keyring.current_key_version()
        ctx = EncryptionContext(secret_id=secret_id, version=1, owner=owner,
                                key_version=key_version)
        sobre = encrypt(bytes(plaintext), ctx, self._keyring)
        ahora = _ahora()

        with self._lock:
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute(
                        "INSERT INTO vault_secret (id, owner_actor, scope, name, "
                        "provider, purposes_json, status, current_version, "
                        "permission_epoch, created_at, updated_at, expires_at, "
                        "metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (secret_id, owner, sc, nombre, prov,
                         json.dumps(list(props), ensure_ascii=False), "active", 1,
                         permission_epoch, ahora, ahora, expires_at, meta_json))
                except sqlite3.IntegrityError:
                    conn.execute("ROLLBACK")
                    raise VaultStoreError("duplicate_name") from None
                conn.execute(
                    "INSERT INTO vault_secret_version (secret_id, version, "
                    "key_version, envelope_json, status, created_at, created_by) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (secret_id, 1, key_version, sobre.to_json(), "active",
                     ahora, created_by))
                conn.execute("COMMIT")
                return self._meta(self._fila_secreto(conn, secret_id, owner))
            except VaultStoreError:
                raise
            except sqlite3.Error:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise VaultStoreError("write_failed") from None
            finally:
                conn.close()

    def add_version(self, *, secret_id: str, owner_actor: str, plaintext: bytes,
                    created_by: str | None = None) -> SecretMetadata:
        """Valor nuevo = version nueva. La anterior NO se destruye.

        Conservarla es lo que permite rollback, trazabilidad y una transicion
        ordenada: sustituir en sitio dejaria al sistema sin forma de volver si
        la credencial nueva resulta estar mal.
        """
        self._exigir_abierto()
        if not isinstance(plaintext, (bytes, bytearray)):
            raise VaultStoreError("invalid_argument")
        self.ensure_schema()
        owner = (owner_actor or "").strip()

        with self._lock:
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                fila = self._fila_secreto(conn, secret_id, owner)
                if fila["status"] != "active":
                    conn.execute("ROLLBACK")
                    raise VaultStoreError(
                        "revoked" if fila["status"] == "revoked" else "expired")
                siguiente = int(conn.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM vault_secret_version "
                    "WHERE secret_id = ?", (fila["id"],)).fetchone()[0])
                key_version = self._keyring.current_key_version()
                ctx = self._contexto(fila, siguiente, key_version)
                sobre = encrypt(bytes(plaintext), ctx, self._keyring)
                ahora = _ahora()
                # La anterior pasa a `superseded`, no se borra ni se revoca:
                # revocar diria que alguien la retiro, y eso no paso.
                conn.execute(
                    "UPDATE vault_secret_version SET status = 'superseded', "
                    "superseded_at = ? WHERE secret_id = ? AND status = 'active'",
                    (ahora, fila["id"]))
                conn.execute(
                    "INSERT INTO vault_secret_version (secret_id, version, "
                    "key_version, envelope_json, status, created_at, created_by) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (fila["id"], siguiente, key_version, sobre.to_json(),
                     "active", ahora, created_by))
                # `current_version` se mueve en la MISMA transaccion que la
                # insercion: fuera de ella habria un instante con la metadata
                # apuntando a una version que aun no existe.
                conn.execute(
                    "UPDATE vault_secret SET current_version = ?, updated_at = ? "
                    "WHERE id = ?", (siguiente, ahora, fila["id"]))
                conn.execute("COMMIT")
                return self._meta(self._fila_secreto(conn, fila["id"], owner))
            except (VaultStoreError, VaultCryptoError):
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            except sqlite3.Error:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise VaultStoreError("write_failed") from None
            finally:
                conn.close()

    # -- lectura ----------------------------------------------------------

    def get_secret_metadata(self, *, secret_id: str,
                            owner_actor: str | None = None) -> SecretMetadata:
        self.ensure_schema()
        conn = self.connect()
        try:
            return self._meta(self._fila_secreto(conn, secret_id, owner_actor))
        finally:
            conn.close()

    def list_metadata(self, *, owner_actor: str | None = None,
                      status: str | None = None,
                      limit: int = 200) -> list[SecretMetadata]:
        """Inventario. NO toca `vault_secret_version`: no hay criptograma que
        filtrar porque no se consulta."""
        self.ensure_schema()
        conn = self.connect()
        try:
            sql = "SELECT * FROM vault_secret WHERE 1=1"
            params: list[Any] = []
            if owner_actor is not None:
                sql += " AND owner_actor = ?"
                params.append(owner_actor)
            if status is not None:
                sql += " AND status = ?"
                params.append(status)
            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(max(1, min(int(limit), 500)))
            return [self._meta(r) for r in conn.execute(sql, params)]
        finally:
            conn.close()

    def list_versions(self, *, secret_id: str,
                      owner_actor: str | None = None) -> list[VersionMetadata]:
        """Versiones SIN sobre."""
        self.ensure_schema()
        conn = self.connect()
        try:
            fila = self._fila_secreto(conn, secret_id, owner_actor)
            return [
                VersionMetadata(
                    secret_id=r["secret_id"], version=r["version"],
                    key_version=r["key_version"], status=r["status"],
                    created_at=r["created_at"], created_by=r["created_by"],
                    superseded_at=r["superseded_at"], revoked_at=r["revoked_at"])
                for r in conn.execute(
                    "SELECT * FROM vault_secret_version WHERE secret_id = ? "
                    "ORDER BY version", (fila["id"],))
            ]
        finally:
            conn.close()

    def get_current_version(self, *, secret_id: str,
                            owner_actor: str | None = None) -> VersionMetadata:
        """La version utilizable, o el motivo exacto por el que no la hay."""
        self.ensure_schema()
        conn = self.connect()
        try:
            fila = self._fila_secreto(conn, secret_id, owner_actor)
            self._exigir_utilizable(fila)
            v = conn.execute(
                "SELECT * FROM vault_secret_version WHERE secret_id = ? AND "
                "version = ?", (fila["id"], fila["current_version"])).fetchone()
            if v is None:
                raise VaultStoreError("no_active_version")
            if v["status"] != "active":
                # Una version revocada NO puede ser current: si la metadata lo
                # dice, la metadata esta mintiendo y se falla en vez de usarla.
                raise VaultStoreError("no_active_version")
            return VersionMetadata(
                secret_id=v["secret_id"], version=v["version"],
                key_version=v["key_version"], status=v["status"],
                created_at=v["created_at"], created_by=v["created_by"],
                superseded_at=v["superseded_at"], revoked_at=v["revoked_at"])
        finally:
            conn.close()

    def _exigir_utilizable(self, fila: sqlite3.Row) -> None:
        if fila["status"] == "revoked":
            raise VaultStoreError("revoked")
        # El TTL se evalua ANTES que el estado almacenado, igual que en la
        # memoria de 10.2: caducado es caducado aunque la columna diga `active`.
        exp = fila["expires_at"]
        if exp and str(exp) <= _ahora():
            raise VaultStoreError("expired")
        if fila["status"] == "expired":
            raise VaultStoreError("expired")
        if fila["current_version"] is None:
            raise VaultStoreError("no_active_version")

    # -- uso del texto plano ----------------------------------------------

    def with_current_plaintext(self, *, secret_id: str, owner_actor: str,
                               consumer: Callable[[bytes], Any]) -> Any:
        """Entrega el texto plano a `consumer` y devuelve LO QUE ESTE DEVUELVA.

        No hay `get_plaintext()` y no lo habra. Con una funcion que devolviera
        el secreto, "quien puede filtrarlo" tendria tantas respuestas como
        llamadores; asi tiene una, y en 10.3.3 esa una sera el Broker, que es
        quien inyecta y ejecuta sin que el valor salga de su marco.
        """
        self._exigir_abierto()
        if not callable(consumer):
            raise VaultStoreError("invalid_argument")
        self.ensure_schema()
        conn = self.connect()
        try:
            fila = self._fila_secreto(conn, secret_id, (owner_actor or "").strip())
            self._exigir_utilizable(fila)
            v = conn.execute(
                "SELECT * FROM vault_secret_version WHERE secret_id = ? AND "
                "version = ? AND status = 'active'",
                (fila["id"], fila["current_version"])).fetchone()
            if v is None:
                raise VaultStoreError("no_active_version")
            claro = self._descifrar(fila, v)
        finally:
            conn.close()
        try:
            return consumer(claro)
        finally:
            del claro

    def _descifrar(self, fila: sqlite3.Row, v: sqlite3.Row) -> bytes:
        try:
            sobre = Envelope.from_json(v["envelope_json"])
        except VaultCryptoError:
            raise VaultStoreError("corrupt_envelope") from None
        ctx = self._contexto(fila, int(v["version"]), int(v["key_version"]))
        try:
            return decrypt(sobre, ctx, self._keyring)
        except VaultCryptoError as exc:
            # Se traduce a codigos del store, y se corta la cadena: el traceback
            # de la libreria puede llevar buffers en sus marcos.
            code = {
                "authentication_failed": "authentication_failed",
                "unknown_key_version": "unknown_key_version",
            }.get(exc.code, "corrupt_envelope")
            raise VaultStoreError(code) from None

    # -- ciclo de vida ----------------------------------------------------

    def revoke_secret(self, *, secret_id: str, owner_actor: str | None = None
                      ) -> SecretMetadata:
        """Retira el secreto y TODAS sus versiones. No borra: revocar deja
        rastro, y borrar no."""
        return self._marcar(secret_id, owner_actor, "revoked")

    def expire_secret(self, *, secret_id: str, owner_actor: str | None = None
                      ) -> SecretMetadata:
        """Fin de ciclo de vida, no moderacion. Se distingue de `revoked`
        porque la causa es distinta y la respuesta al usuario tambien."""
        return self._marcar(secret_id, owner_actor, "expired")

    def _marcar(self, secret_id: str, owner: str | None, estado: str
                ) -> SecretMetadata:
        if estado not in SECRET_STATUSES:
            raise VaultStoreError("invalid_argument")
        self.ensure_schema()
        ahora = _ahora()
        with self._lock:
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                fila = self._fila_secreto(conn, secret_id, owner)
                conn.execute(
                    "UPDATE vault_secret SET status = ?, updated_at = ? WHERE id = ?",
                    (estado, ahora, fila["id"]))
                if estado == "revoked":
                    conn.execute(
                        "UPDATE vault_secret_version SET status = 'revoked', "
                        "revoked_at = ? WHERE secret_id = ? AND status != 'revoked'",
                        (ahora, fila["id"]))
                conn.execute("COMMIT")
                return self._meta(self._fila_secreto(conn, fila["id"], owner))
            except VaultStoreError:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            except sqlite3.Error:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise VaultStoreError("write_failed") from None
            finally:
                conn.close()

    def delete_secret(self, *, secret_id: str, owner_actor: str | None = None,
                      confirm: bool = False) -> bool:
        """Borrado REAL e irreversible. Exige `confirm=True`.

        10.3.0 distingue revocar de borrar: revocar conserva la fila y su
        historia; borrar la quita. La confirmacion explicita existe porque un
        borrado accidental aqui no se deshace con un `git revert`.
        """
        if not confirm:
            raise VaultStoreError("confirmation_required")
        self.ensure_schema()
        with self._lock:
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                fila = self._fila_secreto(conn, secret_id, owner_actor)
                conn.execute("DELETE FROM vault_secret_version WHERE secret_id = ?",
                             (fila["id"],))
                conn.execute("DELETE FROM vault_secret WHERE id = ?", (fila["id"],))
                conn.execute("COMMIT")
                return True
            except VaultStoreError:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            except sqlite3.Error:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise VaultStoreError("write_failed") from None
            finally:
                conn.close()

    # -- rotacion de KEK --------------------------------------------------

    def rotate_key_version(self, *, new_key_version: int,
                           secret_id: str | None = None) -> dict[str, Any]:
        """Reenvuelve los DEK con otra KEK. EL PAYLOAD NO SE TOCA.

        Cada version se reenvuelve en SU PROPIA transaccion. Si la rotacion se
        interrumpe, la base queda mixta pero consistente: cada fila dice con que
        KEK esta envuelta, y todas siguen abriendose mientras su clave exista.
        Una transaccion unica para todo daria "todo o nada" a cambio de que un
        fallo a la mitad dejara un lock largo y ninguna version migrada.
        """
        self._exigir_abierto()
        # Que la clave destino exista se comprueba ANTES de tocar nada: empezar
        # una rotacion hacia una clave inexistente migraria cero filas y dejaria
        # un informe que parece exito.
        self._keyring.get(new_key_version)
        self.ensure_schema()

        conn = self.connect()
        try:
            sql = ("SELECT v.*, s.owner_actor FROM vault_secret_version v "
                   "JOIN vault_secret s ON s.id = v.secret_id "
                   "WHERE v.key_version != ?")
            params: list[Any] = [int(new_key_version)]
            if secret_id is not None:
                sql += " AND v.secret_id = ?"
                params.append(secret_id)
            pendientes = list(conn.execute(sql + " ORDER BY v.secret_id, v.version",
                                           params))
        finally:
            conn.close()

        migradas, fallos = 0, []
        for v in pendientes:
            try:
                self._rotar_una(v, int(new_key_version))
                migradas += 1
            except (VaultStoreError, VaultCryptoError) as exc:
                fallos.append({"secret_id": v["secret_id"], "version": v["version"],
                               "code": getattr(exc, "code", "write_failed")})
        return {"candidatas": len(pendientes), "migradas": migradas,
                "fallidas": len(fallos), "detalle": fallos,
                "new_key_version": int(new_key_version)}

    def _rotar_una(self, v: sqlite3.Row, nueva: int) -> None:
        with self._lock:
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                fila = self._fila_secreto(conn, v["secret_id"], None)
                actual = conn.execute(
                    "SELECT * FROM vault_secret_version WHERE secret_id = ? AND "
                    "version = ?", (v["secret_id"], v["version"])).fetchone()
                if actual is None:
                    conn.execute("ROLLBACK")
                    raise VaultStoreError("version_not_found")
                # Se relee dentro de la transaccion: si otro la rotó entremedio,
                # esta pasada no tiene nada que hacer.
                if int(actual["key_version"]) == int(nueva):
                    conn.execute("ROLLBACK")
                    return
                try:
                    sobre = Envelope.from_json(actual["envelope_json"])
                except VaultCryptoError:
                    conn.execute("ROLLBACK")
                    raise VaultStoreError("corrupt_envelope") from None
                ctx = self._contexto(fila, int(actual["version"]),
                                     int(actual["key_version"]))
                nuevo_sobre, _ = rewrap(sobre, ctx, self._keyring, int(nueva))
                conn.execute(
                    "UPDATE vault_secret_version SET envelope_json = ?, "
                    "key_version = ? WHERE secret_id = ? AND version = ? "
                    "AND key_version = ?",
                    (nuevo_sobre.to_json(), int(nueva), actual["secret_id"],
                     actual["version"], actual["key_version"]))
                conn.execute("COMMIT")
            except (VaultStoreError, VaultCryptoError):
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            except sqlite3.Error:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise VaultStoreError("write_failed") from None
            finally:
                conn.close()

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"VaultStore(path={self.path.name!r}, keyring={self._keyring.estado!r})"
