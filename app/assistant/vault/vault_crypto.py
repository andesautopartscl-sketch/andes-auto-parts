"""FASE 10.3.1 — envelope encryption para el Secret Vault. Modulo PURO.

QUE SABE ESTE MODULO

Bytes, claves y contexto. Nada mas. No importa Flask, ni SQLAlchemy, ni
sqlite3, ni sesiones, ni usuarios, ni el orquestador, ni el Approval Engine.
Esa pobreza es la funcionalidad: un modulo sin actor, sin sesion y sin HTTP no
tiene superficie de ataque, y se puede probar de forma exhaustiva antes de que
exista ningun camino para que un secreto real entre al sistema.

QUE NO DECIDE

No decide quien puede usar un secreto —eso es el Broker—, ni donde viven las
claves —eso es el `KeyResolver` que le pasen—, ni ejecuta nada hacia fuera.

EL SOBRE

    KEK (por version)
     └── envuelve un DEK aleatorio de 32 bytes, unico por cifrado
          └── cifra el payload con AES-256-GCM

Rotar la KEK es re-envolver DEKs: operaciones de 32 bytes, sin tocar un byte de
criptograma y sin que ningun texto plano vuelva a memoria. Sin envelope, rotar
obligaria a descifrar y recifrar cada secreto entero.

EL AAD ATA EL SOBRE A SU FILA

Los datos asociados son `secret_id|version|owner|key_version`. AES-GCM los
autentica sin cifrarlos, asi que un criptograma copiado a la fila de otro
secreto —o de otro dueño— FALLA al descifrar en lugar de entregar su contenido.
Eso convierte un error de programacion en la capa de datos en un fallo ruidoso
en vez de una fuga silenciosa.

LOS ERRORES NO LLEVAN SECRETOS

`VaultCryptoError` tiene un `code` de un vocabulario cerrado y un mensaje fijo.
Ni texto plano, ni criptograma, ni material de clave, ni siquiera longitudes:
una longitud es informacion sobre el secreto. Todo descifrado se hace con
`raise ... from None` para que el traceback no encadene la excepcion de la
libreria, que si podria llevar buffers en sus marcos.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
from dataclasses import dataclass
from typing import Any, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Version del FORMATO del sobre, no de las claves. Sube solo si cambia la
# estructura de una forma que un lector viejo no pueda entender.
ENVELOPE_VERSION = 1

ALGORITHM = "AES-256-GCM"

KEY_BYTES = 32   # AES-256
NONCE_BYTES = 12  # 96 bits: el tamaño para el que GCM esta especificado
DEK_BYTES = 32

# Cota superior generosa para un secreto. Una credencial son decenas de bytes;
# un megabyte no es una credencial, es otra cosa que alguien metio por error.
MAX_PLAINTEXT_BYTES = 64 * 1024

# Separador del AAD. La barra vertical no aparece en un UUID ni en un entero, y
# los campos se validan para que nadie pueda inyectarla y desplazar el contexto.
_AAD_SEP = "|"

CODES = (
    "invalid_envelope",
    "unsupported_version",
    "unknown_key_version",
    "authentication_failed",
    "invalid_context",
    "invalid_key_size",
    "plaintext_too_large",
)


class VaultCryptoError(Exception):
    """Error de criptografia del vault. NUNCA lleva secretos en el mensaje.

    El mensaje es fijo por codigo, no interpolado: interpolar es como acaban
    los valores en los logs. Quien necesite diagnosticar tiene el `code`.
    """

    _MENSAJES = {
        "invalid_envelope": "El sobre no tiene la forma esperada.",
        "unsupported_version": "Version de sobre no soportada.",
        "unknown_key_version": "No hay clave para esa version.",
        "authentication_failed": "El sobre no se pudo autenticar.",
        "invalid_context": "El contexto de cifrado no es valido.",
        "invalid_key_size": "La clave no tiene el tamaño requerido.",
        "plaintext_too_large": "El contenido excede el tamaño permitido.",
    }

    def __init__(self, code: str):
        self.code = code if code in CODES else "invalid_envelope"
        super().__init__(self._MENSAJES[self.code])

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"VaultCryptoError(code={self.code!r})"


# ───────────────────────────── contexto

@dataclass(frozen=True)
class EncryptionContext:
    """Lo que ata un criptograma a su fila. Va al AAD, no al ciphertext.

    Los cuatro campos son los de 10.3.0 y ninguno sobra:

      `secret_id`    a que secreto pertenece
      `version`      a que version de ese secreto
      `owner`        de quien es — sin esto, mover una fila entre actores
                     seria indetectable para la criptografia
      `key_version`  con que KEK esta envuelto el DEK

    Va congelado porque el mismo objeto se usa al cifrar y al descifrar: si
    pudiera mutar entremedio, el AAD dejaria de ser una constante del sobre.
    """

    secret_id: str
    version: int
    owner: str
    key_version: int

    def __post_init__(self) -> None:
        for nombre in ("secret_id", "owner"):
            valor = getattr(self, nombre)
            if not isinstance(valor, str) or not valor.strip():
                raise VaultCryptoError("invalid_context")
            # El separador dentro de un campo permitiria construir dos
            # contextos distintos con el mismo AAD. Se prohibe, no se escapa:
            # escapar es una regla mas que alguien puede implementar al reves.
            if _AAD_SEP in valor or "\x00" in valor:
                raise VaultCryptoError("invalid_context")
        for nombre in ("version", "key_version"):
            valor = getattr(self, nombre)
            if not isinstance(valor, int) or isinstance(valor, bool) or valor < 1:
                raise VaultCryptoError("invalid_context")

    def aad(self) -> bytes:
        """`secret_id|version|owner|key_version`, exactamente el de 10.3.0."""
        return _AAD_SEP.join((
            self.secret_id,
            str(self.version),
            self.owner,
            str(self.key_version),
        )).encode("utf-8")

    def __repr__(self) -> str:  # pragma: no cover - trivial
        # No hay secreto aqui, pero el habito de no volcar objetos del vault en
        # un log se establece en el primer modulo o no se establece.
        return (f"EncryptionContext(secret_id={self.secret_id!r}, "
                f"version={self.version}, key_version={self.key_version})")


# ───────────────────────────── resolucion de claves

class KeyResolver(Protocol):
    """De donde salen las KEK. Este modulo NO lo sabe y no debe saberlo.

    Quien implemente esto en 10.3.2 decidira entorno, archivo o lo que sea.
    Aqui solo se exige el contrato: una version entera devuelve 32 bytes, o
    levanta `VaultCryptoError("unknown_key_version")`.
    """

    def get(self, key_version: int) -> bytes:
        ...


class StaticKeyResolver:
    """Resolver en memoria. Para tests y para arranque; nunca persiste nada.

    Existe aqui —y no en los tests— porque el contrato del `KeyResolver` merece
    una implementacion de referencia que demuestre que es implementable sin
    tocar disco.
    """

    def __init__(self, keys: dict[int, bytes] | None = None):
        self._keys: dict[int, bytes] = {}
        for version, key in (keys or {}).items():
            self.add(version, key)

    def add(self, key_version: int, key: bytes) -> None:
        if not isinstance(key_version, int) or isinstance(key_version, bool) \
                or key_version < 1:
            raise VaultCryptoError("invalid_context")
        validate_key(key)
        self._keys[key_version] = bytes(key)

    def get(self, key_version: int) -> bytes:
        try:
            return self._keys[key_version]
        except (KeyError, TypeError):
            raise VaultCryptoError("unknown_key_version") from None

    def versions(self) -> tuple[int, ...]:
        return tuple(sorted(self._keys))

    def __repr__(self) -> str:  # pragma: no cover - trivial
        # Las versiones no son secretas; las claves, si. Nunca las dos.
        return f"StaticKeyResolver(versions={self.versions()})"


def generate_key() -> bytes:
    """Una KEK nueva. `secrets`, no `random`: el segundo es predecible."""
    return secrets.token_bytes(KEY_BYTES)


def validate_key(key: Any) -> bytes:
    if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_BYTES:
        raise VaultCryptoError("invalid_key_size")
    return bytes(key)


# ───────────────────────────── el sobre

def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(raw: Any) -> bytes:
    if not isinstance(raw, str):
        raise VaultCryptoError("invalid_envelope")
    try:
        return base64.b64decode(raw.encode("ascii"), validate=True)
    except Exception:
        raise VaultCryptoError("invalid_envelope") from None


@dataclass(frozen=True)
class Envelope:
    """Lo que se guarda. Todo publico salvo, obviamente, lo que va cifrado.

    `wrapped_dek` es el DEK cifrado con la KEK; `ciphertext` es el payload
    cifrado con el DEK. `cryptography` **no separa el tag**: lo concatena al
    final del ciphertext. Se documenta aqui para que nadie busque un campo
    `tag` que no existe y concluya que falta autenticacion.
    """

    envelope_version: int
    algorithm: str
    key_version: int
    dek_nonce: str      # base64
    wrapped_dek: str    # base64, tag incluido al final
    nonce: str          # base64
    ciphertext: str     # base64, tag incluido al final

    def to_dict(self) -> dict[str, Any]:
        return {
            "envelope_version": self.envelope_version,
            "algorithm": self.algorithm,
            "key_version": self.key_version,
            "dek_nonce": self.dek_nonce,
            "wrapped_dek": self.wrapped_dek,
            "nonce": self.nonce,
            "ciphertext": self.ciphertext,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))

    @staticmethod
    def from_dict(raw: Any) -> "Envelope":
        if not isinstance(raw, dict):
            raise VaultCryptoError("invalid_envelope")
        try:
            ver = raw["envelope_version"]
            alg = raw["algorithm"]
            kver = raw["key_version"]
        except (KeyError, TypeError):
            raise VaultCryptoError("invalid_envelope") from None
        if not isinstance(ver, int) or isinstance(ver, bool):
            raise VaultCryptoError("invalid_envelope")
        # La version se comprueba ANTES que el resto: un sobre del futuro puede
        # tener otros campos, y rechazarlo por "le falta nonce" seria mentir
        # sobre la causa.
        if ver != ENVELOPE_VERSION:
            raise VaultCryptoError("unsupported_version")
        if alg != ALGORITHM:
            raise VaultCryptoError("invalid_envelope")
        if not isinstance(kver, int) or isinstance(kver, bool) or kver < 1:
            raise VaultCryptoError("invalid_envelope")
        for campo in ("dek_nonce", "wrapped_dek", "nonce", "ciphertext"):
            if not isinstance(raw.get(campo), str):
                raise VaultCryptoError("invalid_envelope")
        return Envelope(
            envelope_version=ver, algorithm=alg, key_version=kver,
            dek_nonce=raw["dek_nonce"], wrapped_dek=raw["wrapped_dek"],
            nonce=raw["nonce"], ciphertext=raw["ciphertext"])

    @staticmethod
    def from_json(raw: Any) -> "Envelope":
        if not isinstance(raw, str):
            raise VaultCryptoError("invalid_envelope")
        try:
            datos = json.loads(raw)
        except ValueError:
            raise VaultCryptoError("invalid_envelope") from None
        return Envelope.from_dict(datos)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        # Ni criptograma ni longitudes: una longitud es informacion sobre el
        # secreto, y un repr acaba en un log antes o despues.
        return (f"Envelope(v={self.envelope_version}, alg={self.algorithm!r}, "
                f"key_version={self.key_version})")


# ───────────────────────────── cifrar / descifrar

def encrypt(plaintext: bytes, context: EncryptionContext,
            resolver: KeyResolver) -> Envelope:
    """Sobre nuevo. NONCE NUEVO Y ALEATORIO EN CADA LLAMADA, sin excepcion.

    Reutilizar un nonce con la misma clave en GCM no degrada la seguridad: la
    destruye —permite recuperar el keystream y falsificar mensajes—. Por eso no
    hay parametro para fijarlo ni siquiera en tests: el unico modo de que no se
    repita es que nadie pueda elegirlo.

    El DEK tambien es nuevo por llamada, asi que cada payload se cifra con una
    clave que solo el usa. Dos cifrados del mismo texto no se parecen en nada.
    """
    if not isinstance(plaintext, (bytes, bytearray)):
        raise VaultCryptoError("invalid_envelope")
    if len(plaintext) > MAX_PLAINTEXT_BYTES:
        raise VaultCryptoError("plaintext_too_large")
    if not isinstance(context, EncryptionContext):
        raise VaultCryptoError("invalid_context")

    kek = validate_key(resolver.get(context.key_version))
    aad = context.aad()

    dek = secrets.token_bytes(DEK_BYTES)
    try:
        dek_nonce = os.urandom(NONCE_BYTES)
        wrapped = AESGCM(kek).encrypt(dek_nonce, dek, aad)

        nonce = os.urandom(NONCE_BYTES)
        ciphertext = AESGCM(dek).encrypt(nonce, bytes(plaintext), aad)
    except VaultCryptoError:
        raise
    except Exception:
        # Sin `from`: la excepcion de la libreria puede llevar buffers en sus
        # marcos, y encadenarla los pondria en el traceback.
        raise VaultCryptoError("invalid_envelope") from None
    finally:
        del dek

    return Envelope(
        envelope_version=ENVELOPE_VERSION, algorithm=ALGORITHM,
        key_version=context.key_version,
        dek_nonce=_b64(dek_nonce), wrapped_dek=_b64(wrapped),
        nonce=_b64(nonce), ciphertext=_b64(ciphertext))


def decrypt(envelope: Envelope | dict[str, Any] | str,
            context: EncryptionContext, resolver: KeyResolver) -> bytes:
    """Texto plano, o `VaultCryptoError`. Nunca un resultado a medias.

    El contexto tiene que ser EL MISMO que al cifrar. Si cambio cualquiera de
    sus cuatro campos —o el criptograma, o el nonce, o el tag— GCM falla la
    autenticacion y esto levanta `authentication_failed`. Un unico codigo para
    todos esos casos a proposito: distinguir "tag malo" de "AAD malo" le diria
    a quien esta probando por donde seguir.
    """
    if isinstance(envelope, Envelope):
        sobre = envelope
    elif isinstance(envelope, dict):
        sobre = Envelope.from_dict(envelope)
    else:
        sobre = Envelope.from_json(envelope)

    if not isinstance(context, EncryptionContext):
        raise VaultCryptoError("invalid_context")
    # El sobre dice con que KEK se envolvio; el contexto dice con cual se cree
    # que fue. Si no coinciden, el AAD tampoco coincidiria — pero fallar aqui
    # con un codigo claro es mas util que un `authentication_failed` generico.
    if sobre.key_version != context.key_version:
        raise VaultCryptoError("invalid_context")

    kek = validate_key(resolver.get(sobre.key_version))
    aad = context.aad()

    dek_nonce = _unb64(sobre.dek_nonce)
    wrapped = _unb64(sobre.wrapped_dek)
    nonce = _unb64(sobre.nonce)
    ciphertext = _unb64(sobre.ciphertext)
    if len(dek_nonce) != NONCE_BYTES or len(nonce) != NONCE_BYTES:
        raise VaultCryptoError("invalid_envelope")

    dek = None
    try:
        dek = AESGCM(kek).decrypt(dek_nonce, wrapped, aad)
        if len(dek) != DEK_BYTES:
            raise VaultCryptoError("invalid_envelope")
        return AESGCM(dek).decrypt(nonce, ciphertext, aad)
    except InvalidTag:
        raise VaultCryptoError("authentication_failed") from None
    except VaultCryptoError:
        raise
    except Exception:
        raise VaultCryptoError("invalid_envelope") from None
    finally:
        # Python no permite borrar un `str` ni un `bytes` de forma fiable; esto
        # solo suelta la referencia y acorta la ventana. No se promete mas.
        del dek


def rewrap(envelope: Envelope, context: EncryptionContext,
           resolver: KeyResolver, new_key_version: int) -> tuple[Envelope, EncryptionContext]:
    """Reenvuelve el DEK con otra KEK. EL PAYLOAD NO SE TOCA.

    Es la rotacion de 10.3.0: el texto plano nunca vuelve a memoria, solo 32
    bytes de DEK. Se devuelve tambien el contexto nuevo porque `key_version`
    forma parte del AAD: rotar cambia el AAD, y por tanto hay que re-autenticar
    el DEK bajo el contexto nuevo.

    El almacenamiento —que filas actualizar, en que transaccion, que hacer si
    la rotacion se interrumpe— es de 10.3.2. Aqui solo esta la transformacion.
    """
    if not isinstance(envelope, Envelope):
        raise VaultCryptoError("invalid_envelope")
    if not isinstance(new_key_version, int) or isinstance(new_key_version, bool) \
            or new_key_version < 1:
        raise VaultCryptoError("invalid_context")
    if envelope.key_version != context.key_version:
        raise VaultCryptoError("invalid_context")

    kek_vieja = validate_key(resolver.get(envelope.key_version))
    kek_nueva = validate_key(resolver.get(new_key_version))

    contexto_nuevo = EncryptionContext(
        secret_id=context.secret_id, version=context.version,
        owner=context.owner, key_version=new_key_version)

    dek = None
    try:
        dek = AESGCM(kek_vieja).decrypt(
            _unb64(envelope.dek_nonce), _unb64(envelope.wrapped_dek), context.aad())
    except InvalidTag:
        raise VaultCryptoError("authentication_failed") from None
    except VaultCryptoError:
        raise
    except Exception:
        raise VaultCryptoError("invalid_envelope") from None

    try:
        # El payload sigue cifrado con el MISMO DEK, pero su AAD cambio: hay que
        # recifrarlo para que siga atado a su contexto. El plaintext pasa por
        # memoria aqui, y es el unico punto donde eso ocurre en una rotacion.
        aad_nuevo = contexto_nuevo.aad()
        plano = AESGCM(dek).decrypt(
            _unb64(envelope.nonce), _unb64(envelope.ciphertext), context.aad())
        nonce_nuevo = os.urandom(NONCE_BYTES)
        ciphertext_nuevo = AESGCM(dek).encrypt(nonce_nuevo, plano, aad_nuevo)

        dek_nonce_nuevo = os.urandom(NONCE_BYTES)
        wrapped_nuevo = AESGCM(kek_nueva).encrypt(dek_nonce_nuevo, dek, aad_nuevo)
    except InvalidTag:
        raise VaultCryptoError("authentication_failed") from None
    except VaultCryptoError:
        raise
    except Exception:
        raise VaultCryptoError("invalid_envelope") from None
    finally:
        del dek
        plano = None

    return Envelope(
        envelope_version=ENVELOPE_VERSION, algorithm=ALGORITHM,
        key_version=new_key_version,
        dek_nonce=_b64(dek_nonce_nuevo), wrapped_dek=_b64(wrapped_nuevo),
        nonce=_b64(nonce_nuevo), ciphertext=_b64(ciphertext_nuevo)), contexto_nuevo
