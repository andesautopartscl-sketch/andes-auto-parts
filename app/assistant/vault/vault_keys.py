"""FASE 10.3.2 — de donde salen las KEK. Implementa el `KeyResolver` de 10.3.1.

LA REGLA QUE ESTE MODULO EXISTE PARA CUMPLIR

La clave maestra no puede vivir en `vault.db`, ni en `andes.db`, ni en git, ni
en logs, ni en la auditoria, ni en la memoria del asistente, ni en un prompt,
ni en el navegador. Guardar la clave junto al cofre no es cifrado: es dar dos
pasos para llegar a lo mismo.

DOS FUENTES, EN ESTE ORDEN

  1. ENTORNO — `ANDES_VAULT_KEK_V1`, `..._V2`, ... en base64. Es lo que usa un
     despliegue que inyecta secretos por variable, y no toca disco.
  2. LLAVERO EN DISCO — `data/.vault_keys.json`, fuera de git y fuera de los
     backups. Es lo que hace que el vault sobreviva a un reinicio en local.

El entorno gana por version: se puede inyectar la v2 sin borrar el archivo.

POR QUE NO `.env`

`.env` es el archivo que mas se copia, se comparte y se mira por encima del
hombro, y en este proyecto ya guarda otros secretos de servicio. Que el vault
dependiera de el anularia la separacion que justifica tener `vault.db` aparte.

FALLAR CERRADO, Y DISTINGUIR POR QUE

`vault_locked` no es lo mismo que `vault vacio`. Si hay llavero ilegible o no
hay clave, el vault **no arranca como si estuviera vacio**: eso convertiria una
perdida de clave en una perdida de datos silenciosa, y la diferencia entre "no
tengo secretos" y "no puedo abrirlos" es justo la que hay que poder ver.

Y NUNCA se genera una KEK nueva sobre un llavero que ya existe: crear es una
operacion explicita (`initialize_keyring`), jamas un efecto secundario de leer.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import secrets
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.assistant.vault.vault_crypto import KEY_BYTES, VaultCryptoError, validate_key

logger = logging.getLogger(__name__)

ENV_PREFIX = "ANDES_VAULT_KEK_V"
ENV_CURRENT = "ANDES_VAULT_KEK_CURRENT"
ENV_KEYRING_PATH = "ANDES_VAULT_KEYRING"

DEFAULT_KEYRING = Path("data/.vault_keys.json")

_ENV_RE = re.compile(rf"^{re.escape(ENV_PREFIX)}(\d+)$")

ESTADOS = (
    "open",              # hay al menos una clave utilizable
    "uninitialized",     # no hay llavero y nunca lo hubo
    "unreadable",        # existe pero no se puede leer o parsear
    "insecure",          # existe pero cualquiera del sistema puede leerlo
    "empty",             # existe, se lee, y no tiene ninguna clave
)


class VaultKeyringError(Exception):
    """Problema de arranque del llavero. Mensaje fijo, como en vault_crypto."""

    _MENSAJES = {
        "vault_locked": "El vault no puede abrirse: no hay clave utilizable.",
        "keyring_unreadable": "El llavero existe pero no se puede leer.",
        "keyring_insecure": "El llavero es legible por otros usuarios.",
        "keyring_exists": "El llavero ya existe; no se sobrescribe.",
        "invalid_key_material": "El material de clave no tiene la forma esperada.",
        "no_current_version": "El llavero no declara una version actual.",
    }

    def __init__(self, code: str):
        self.code = code if code in self._MENSAJES else "vault_locked"
        super().__init__(self._MENSAJES[self.code])

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"VaultKeyringError(code={self.code!r})"


# ───────────────────────────── proteccion del archivo

@dataclass(frozen=True)
class ProteccionArchivo:
    """Lo que de verdad protege al llavero, dicho sin adornos.

    En POSIX el modo del archivo es un control efectivo y se comprueba. En
    Windows `os.chmod` solo alterna el bit de solo-lectura —medido: un archivo
    queda en 0o666 despues de pedir 0o600—, asi que el modo NO es un control de
    acceso. Lo que si lo es son las ACL de NTFS, y por eso se intenta
    restringirlas al crear. El resultado se REPORTA, no se supone.
    """

    plataforma: str
    modo: str | None
    mecanismo: str
    efectivo: bool
    detalle: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "plataforma": self.plataforma, "modo": self.modo,
            "mecanismo": self.mecanismo, "efectivo": self.efectivo,
            "detalle": self.detalle,
        }


def _es_windows() -> bool:
    return os.name == "nt"


def proteger_archivo(ruta: Path) -> ProteccionArchivo:
    """Restringe el acceso lo que la plataforma permita, y dice cuanto es."""
    if not ruta.exists():
        return ProteccionArchivo("desconocida", None, "ninguno", False,
                                 "el archivo no existe")
    if not _es_windows():
        try:
            os.chmod(ruta, 0o600)
            modo = stat.S_IMODE(ruta.stat().st_mode)
            ok = modo == 0o600
            return ProteccionArchivo(
                "posix", oct(modo), "modo de archivo", ok,
                "solo el dueño puede leer" if ok
                else "el modo no quedo en 0600")
        except OSError as exc:  # noqa: BLE001
            return ProteccionArchivo("posix", None, "modo de archivo", False,
                                     f"chmod fallo: {type(exc).__name__}")

    # Windows: el modo no sirve. Se intenta ACL, y se informa del resultado.
    usuario = os.environ.get("USERNAME") or ""
    if not usuario:
        return ProteccionArchivo(
            "windows", oct(stat.S_IMODE(ruta.stat().st_mode)), "ninguno", False,
            "sin USERNAME no se pueden fijar ACL; protege la cuenta, no el archivo")
    try:
        r = subprocess.run(
            ["icacls", str(ruta), "/inheritance:r", "/grant:r", f"{usuario}:F"],
            capture_output=True, text=True, timeout=30)
        ok = r.returncode == 0
        return ProteccionArchivo(
            "windows", oct(stat.S_IMODE(ruta.stat().st_mode)), "ACL de NTFS", ok,
            "herencia cortada; solo el dueño tiene acceso" if ok
            else "icacls no pudo aplicar la ACL; protege la cuenta de usuario")
    except (OSError, subprocess.SubprocessError) as exc:  # noqa: BLE001
        return ProteccionArchivo(
            "windows", None, "ninguno", False,
            f"icacls no disponible ({type(exc).__name__}); protege la cuenta")


def es_legible_por_otros(ruta: Path) -> bool:
    """Solo tiene respuesta fiable en POSIX.

    En Windows se devuelve False porque el modo no significa nada ahi, y
    devolver True bloquearia el vault por una medida que no mide nada. La
    proteccion real en Windows se reporta en `ProteccionArchivo`.
    """
    if _es_windows() or not ruta.exists():
        return False
    try:
        modo = stat.S_IMODE(ruta.stat().st_mode)
    except OSError:
        return False
    return bool(modo & (stat.S_IRGRP | stat.S_IROTH))


# ───────────────────────────── el llavero

def keyring_path(path: Path | str | None = None) -> Path:
    if path is not None:
        return Path(path)
    return Path(os.environ.get(ENV_KEYRING_PATH) or DEFAULT_KEYRING)


def _claves_del_entorno() -> dict[int, bytes]:
    out: dict[int, bytes] = {}
    for nombre, valor in os.environ.items():
        m = _ENV_RE.match(nombre)
        if not m or not (valor or "").strip():
            continue
        try:
            cruda = base64.b64decode(valor.strip(), validate=True)
            out[int(m.group(1))] = validate_key(cruda)
        except (binascii.Error, ValueError, VaultCryptoError):
            # Una variable mal formada se ignora en silencio A PROPOSITO: el
            # mensaje diria algo sobre el material de clave, y el arranque
            # fallara igual mas abajo con `vault_locked` si no queda ninguna.
            logger.warning("vault keyring: %s ignorada por formato", nombre)
    return out


class VaultKeyring:
    """El `KeyResolver` real. Resuelve una KEK por `key_version`.

    Cumple el protocolo de `vault_crypto`: `get(version) -> 32 bytes`, o
    `VaultCryptoError("unknown_key_version")`. Los problemas de ARRANQUE
    —llavero ilegible, inseguro, ausente— son otra cosa y levantan
    `VaultKeyringError`, porque no son el mismo problema y no se arreglan igual.
    """

    def __init__(self, path: Path | str | None = None, *,
                 allow_env: bool = True):
        self.path = keyring_path(path)
        self._allow_env = allow_env
        self._archivo: dict[str, Any] | None = None
        self._estado = "uninitialized"
        self._detalle = ""
        self._cargar()

    # -- carga ------------------------------------------------------------

    def _cargar(self) -> None:
        self._archivo = None
        if not self.path.exists():
            self._estado = "uninitialized"
            self._detalle = "no hay archivo de llavero"
            return
        if es_legible_por_otros(self.path):
            # Fallar cerrado: un llavero que otros pueden leer no es un llavero.
            self._estado = "insecure"
            self._detalle = "el archivo es legible por grupo u otros"
            return
        try:
            datos = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._estado = "unreadable"
            self._detalle = "no se pudo leer o parsear el archivo"
            return
        if not isinstance(datos, dict) or not isinstance(datos.get("keys"), dict):
            self._estado = "unreadable"
            self._detalle = "el archivo no tiene la forma esperada"
            return
        self._archivo = datos
        self._estado = "open" if self._versiones_archivo() else "empty"
        self._detalle = ""

    def _versiones_archivo(self) -> dict[int, bytes]:
        if not self._archivo:
            return {}
        out: dict[int, bytes] = {}
        for k, v in (self._archivo.get("keys") or {}).items():
            try:
                out[int(k)] = validate_key(base64.b64decode(str(v), validate=True))
            except (binascii.Error, ValueError, TypeError, VaultCryptoError):
                logger.warning("vault keyring: version %r ignorada por formato", k)
        return out

    # -- estado -----------------------------------------------------------

    @property
    def estado(self) -> str:
        """`open` | `uninitialized` | `unreadable` | `insecure` | `empty`.

        Los cinco existen porque la accion que sigue es distinta en cada uno, y
        confundir `uninitialized` con `empty` es como una perdida de clave se
        convierte en una perdida de datos.
        """
        if self._estado == "open":
            return "open"
        # El entorno puede abrir un vault aunque no haya archivo.
        if self._allow_env and _claves_del_entorno():
            return "open"
        return self._estado

    def esta_abierto(self) -> bool:
        return self.estado == "open"

    def exigir_abierto(self) -> None:
        if self.esta_abierto():
            return
        estado = self.estado
        raise VaultKeyringError(
            "keyring_insecure" if estado == "insecure"
            else "keyring_unreadable" if estado == "unreadable"
            else "vault_locked")

    def versions(self) -> tuple[int, ...]:
        disponibles = set(self._versiones_archivo())
        if self._allow_env:
            disponibles |= set(_claves_del_entorno())
        return tuple(sorted(disponibles))

    def current_key_version(self) -> int:
        """La version con la que se cifra lo NUEVO. Lo viejo sigue con la suya."""
        crudo = (os.environ.get(ENV_CURRENT) or "").strip() if self._allow_env else ""
        if crudo:
            try:
                v = int(crudo)
            except ValueError:
                raise VaultKeyringError("no_current_version") from None
            if v in self.versions():
                return v
            raise VaultKeyringError("no_current_version")
        if self._archivo and self._archivo.get("current") is not None:
            try:
                v = int(self._archivo["current"])
            except (TypeError, ValueError):
                raise VaultKeyringError("no_current_version") from None
            if v in self.versions():
                return v
            raise VaultKeyringError("no_current_version")
        vs = self.versions()
        if not vs:
            raise VaultKeyringError("vault_locked")
        return vs[-1]

    # -- el contrato de KeyResolver ---------------------------------------

    def get(self, key_version: int) -> bytes:
        """32 bytes, o `VaultCryptoError("unknown_key_version")`.

        El entorno gana sobre el archivo para la misma version: permite inyectar
        una clave sin tocar disco.
        """
        if not isinstance(key_version, int) or isinstance(key_version, bool):
            raise VaultCryptoError("unknown_key_version")
        if self._allow_env:
            k = _claves_del_entorno().get(key_version)
            if k is not None:
                return k
        k = self._versiones_archivo().get(key_version)
        if k is None:
            raise VaultCryptoError("unknown_key_version")
        return k

    def protection(self) -> ProteccionArchivo:
        return proteger_archivo(self.path) if self.path.exists() else \
            ProteccionArchivo("desconocida", None, "ninguno", False,
                              "no hay archivo de llavero")

    def __repr__(self) -> str:  # pragma: no cover - trivial
        # Versiones si; material de clave nunca. Ni la ruta completa hace falta
        # para diagnosticar, pero ayuda y no es secreta.
        return (f"VaultKeyring(estado={self.estado!r}, "
                f"versiones={self.versions()}, path={self.path.name!r})")


# ───────────────────────────── creacion y rotacion del llavero

def initialize_keyring(path: Path | str | None = None, *,
                       first_version: int = 1) -> VaultKeyring:
    """Crea el llavero con una clave nueva. Operacion EXPLICITA.

    Nunca ocurre como efecto secundario de leer, y nunca sobrescribe uno
    existente: generar una KEK nueva sobre un vault con datos convertiria todos
    sus secretos en ruido indistinguible de "aun no hay nada".
    """
    ruta = keyring_path(path)
    if ruta.exists():
        raise VaultKeyringError("keyring_exists")
    ruta.parent.mkdir(parents=True, exist_ok=True)
    datos = {
        "version_formato": 1,
        "current": int(first_version),
        "keys": {str(int(first_version)):
                 base64.b64encode(secrets.token_bytes(KEY_BYTES)).decode("ascii")},
    }
    # Se crea vacio y se protege ANTES de escribir la clave: si se escribiera
    # primero, habria un instante con material de clave en un archivo abierto
    # de par en par.
    ruta.touch(mode=0o600, exist_ok=False)
    proteger_archivo(ruta)
    ruta.write_text(json.dumps(datos, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    proteger_archivo(ruta)
    return VaultKeyring(ruta)


def add_key_version(path: Path | str | None = None, *,
                    key_version: int, make_current: bool = False) -> VaultKeyring:
    """Añade una KEK nueva. No borra ninguna: rotar es sumar, no sustituir.

    Retirar la vieja es una decision aparte, y solo es segura cuando ninguna
    fila sigue envuelta con ella — eso lo sabe el Store, no el llavero.
    """
    ruta = keyring_path(path)
    llavero = VaultKeyring(ruta)
    llavero.exigir_abierto()
    if not isinstance(key_version, int) or isinstance(key_version, bool) \
            or key_version < 1:
        raise VaultKeyringError("invalid_key_material")
    datos = dict(llavero._archivo or {"version_formato": 1, "keys": {}})  # noqa: SLF001
    claves = dict(datos.get("keys") or {})
    claves[str(key_version)] = base64.b64encode(
        secrets.token_bytes(KEY_BYTES)).decode("ascii")
    datos["keys"] = claves
    if make_current:
        datos["current"] = int(key_version)
    ruta.write_text(json.dumps(datos, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    proteger_archivo(ruta)
    return VaultKeyring(ruta)


def describe_key_protection(path: Path | str | None = None) -> dict[str, Any]:
    """Para operar: que protege al llavero y cuanto de eso es real aqui."""
    ruta = keyring_path(path)
    p = proteger_archivo(ruta)
    return {
        "ruta": str(ruta),
        "existe": ruta.exists(),
        "plataforma": sys.platform,
        **p.to_dict(),
        "advertencia": (
            "En Windows el modo de archivo NO es un control de acceso: "
            "os.chmod solo alterna el bit de solo-lectura. Lo que protege es "
            "la cuenta de usuario y, si icacls pudo aplicarla, la ACL de NTFS."
            if _es_windows() else
            "En POSIX el modo 0600 es un control efectivo."),
    }
