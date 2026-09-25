"""FASE 10.3.4-B — el Executor HTTP. Donde la credencial sale a la red.

POR QUE `http.client` Y NO `requests` NI `urllib.request`

Las dos alternativas siguen redirecciones SOLAS. `urllib.request` monta un
`HTTPRedirectHandler` por defecto y `requests` trae `allow_redirects=True`. En
los dos casos la politica de redireccion es una opcion que hay que acordarse de
apagar, y una proteccion que depende de acordarse no es una proteccion.

`http.client` no sigue nada. Un 302 llega como un 302 y este modulo decide. La
consecuencia es que "nunca reenviamos la credencial a otro host" no es una
configuracion: es que el codigo que lo haria no existe.

Ademas deja leer el cuerpo a trozos, que es lo unico que permite imponer un
limite de tamano DE VERDAD —parando— en vez de leerlo entero y medirlo despues.

LO QUE ESTE MODULO NO PUEDE HACER

No sabe que secreto lleva dentro. Recibe una `PreparedRequest` ya sellada por
el Broker y no tiene forma de pedir una. Tampoco sanea el resultado: eso lo
hace el Broker, que es quien conoce el valor. Aqui solo se habla HTTP.
"""
from __future__ import annotations

import http.client
import logging
import re
import socket
import ssl
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.assistant.vault.vault_broker import ExecutionResult, PreparedRequest

logger = logging.getLogger(__name__)

# ── tiempos ────────────────────────────────────────────────────────────────
# Tres, no uno. Un solo timeout global deja pasar el caso peor: un proveedor
# que acepta la conexion y despues envia un byte cada cuatro segundos.
CONNECT_TIMEOUT = 5.0     # abrir el socket y el TLS
READ_TIMEOUT = 10.0       # sin datos nuevos durante este rato -> se corta
TOTAL_DEADLINE = 20.0     # techo absoluto de la operacion entera

# ── tamano ─────────────────────────────────────────────────────────────────
# 256 KiB. El razonamiento, no el numero: las acciones de esta fase son
# listados de metadatos —articulos, documentos, cuenta—. Una pagina de 50
# articulos de MercadoLibre ronda los 60-100 KB de JSON, asi que 256 KiB deja
# entre dos y cuatro veces de margen. Por arriba, el ERP corre en una instancia
# pequena y varias respuestas simultaneas de varios MB la tumbarian antes de
# que nadie viera un error util.
#
# Se aplica LEYENDO A TROZOS y parando. Leer entero y medir despues seria
# exactamente el fallo que este limite existe para impedir.
MAX_RESPONSE_BYTES = 256 * 1024
CHUNK = 16 * 1024

# ── metodos ────────────────────────────────────────────────────────────────
# Cerrado. POST esta porque hay APIs de BUSQUEDA que solo aceptan POST; no
# porque esta fase permita escribir. Lo que se puede hacer lo decide `ACTIONS`
# del Broker, y ahi todo es `read_*`.
METHODS = frozenset({"GET", "HEAD", "POST"})

# Cabeceras que este modulo nunca deja pasar, vengan de donde vengan.
#
# `Cookie` porque un secreto en una cookie es un secreto que el navegador
# reenvia solo. `Host`, `Content-Length`, `Transfer-Encoding` y `Connection`
# porque las controla el transporte y dejarlas al llamador abre request
# smuggling. `Proxy-*` porque irian a un salto intermedio, no al destino.
HEADERS_PROHIBIDAS = frozenset({
    "cookie", "set-cookie", "host", "content-length", "transfer-encoding",
    "connection", "upgrade", "expect", "te", "trailer",
})

NOMBRE_HEADER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,40}$")
_CONTROL_RE = re.compile(r"[\r\n\x00-\x1f]")

# Solo estos puertos. Uno arbitrario no es inseguro por si mismo, pero un
# `resource` que apunta a un puerto raro casi siempre significa que alguien
# construyo la URL mal.
PUERTOS = frozenset({443, 8443})
PUERTOS_LOOPBACK_OK = True  # en loopback vale cualquiera; ver `_destino`

_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


@dataclass(frozen=True)
class Destino:
    host: str
    port: int
    path: str
    tls: bool

    @property
    def origen(self) -> str:
        return f"{'https' if self.tls else 'http'}://{self.host}:{self.port}"


class ExecutorError(Exception):
    """Mensaje fijo por codigo. Nunca interpola nada de la peticion."""

    _MENSAJES = {
        "invalid_request": "La peticion preparada no es ejecutable.",
        "scheme_denied": "Solo https, salvo loopback.",
        "port_denied": "Puerto no permitido.",
        "header_denied": "Cabecera no permitida.",
        "method_denied": "Metodo no permitido.",
    }

    def __init__(self, code: str):
        self.code = code if code in self._MENSAJES else "invalid_request"
        super().__init__(self._MENSAJES[self.code])

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"ExecutorError(code={self.code!r})"


def _destino(resource: str) -> Destino:
    """Valida la URL y la parte. Aqui se decide a donde se puede hablar.

    https siempre, con UNA excepcion: loopback por http. No es una puerta
    trasera para pruebas —el carril de aprobacion exige https y hay un test que
    lo comprueba—, es que el trafico a 127.0.0.1 no sale de la maquina y no hay
    transporte que interceptar. La excepcion se paga una vez, aqui, escrita.
    """
    try:
        u = urlsplit(resource or "")
    except ValueError:
        raise ExecutorError("invalid_request") from None
    host = (u.hostname or "").strip()
    if not host or u.fragment:
        # Un fragmento no viaja en la peticion, pero su presencia significa que
        # quien construyo la URL cree que si: mejor pararlo.
        raise ExecutorError("invalid_request") from None
    if u.query and "<<secret:" in u.query:  # pragma: no cover - ya filtrado
        raise ExecutorError("invalid_request") from None

    loopback = host.lower() in _LOOPBACK
    if u.scheme == "https":
        tls = True
    elif u.scheme == "http" and loopback:
        tls = False
    else:
        raise ExecutorError("scheme_denied") from None

    port = u.port or (443 if tls else 80)
    if not loopback and port not in PUERTOS:
        raise ExecutorError("port_denied") from None

    camino = u.path or "/"
    if u.query:
        camino = f"{camino}?{u.query}"
    return Destino(host=host, port=int(port), path=camino, tls=tls)


def _valida_cabeceras(headers: dict[str, str]) -> None:
    for k, v in (headers or {}).items():
        if not NOMBRE_HEADER_RE.match(str(k)):
            raise ExecutorError("header_denied") from None
        if str(k).lower() in HEADERS_PROHIBIDAS:
            raise ExecutorError("header_denied") from None
        if _CONTROL_RE.search(str(v)):
            # Un \r\n en un valor es inyeccion de cabecera. Como el valor puede
            # SER el secreto, no se dice cual ni se registra.
            raise ExecutorError("header_denied") from None


class HttpExecutor:
    """Implementa el `Executor` del Broker. Habla HTTP y nada mas.

    No acepta una URL ni unas cabeceras sueltas: acepta la `PreparedRequest`
    que el Broker sello. Cualquiera puede instanciarlo; nadie puede darle un
    secreto, porque no hay parametro por donde.
    """

    def __init__(self, *, connect_timeout: float = CONNECT_TIMEOUT,
                 read_timeout: float = READ_TIMEOUT,
                 deadline: float = TOTAL_DEADLINE,
                 max_bytes: int = MAX_RESPONSE_BYTES,
                 ssl_context: ssl.SSLContext | None = None):
        # Ninguno puede ser None ni 0: una llamada sin timeout es una llamada
        # que puede quedarse colgada para siempre, y con ella el hilo.
        self.connect_timeout = max(0.1, float(connect_timeout))
        self.read_timeout = max(0.1, float(read_timeout))
        self.deadline = max(0.2, float(deadline))
        self.max_bytes = max(1024, int(max_bytes))
        self._ssl = ssl_context

    # -- politica de redireccion -------------------------------------------
    #
    # NO SE SIGUE NINGUNA. Ni al mismo host.
    #
    # Seguir una redireccion es dejar que el proveedor decida a donde mandamos
    # la credencial. Al mismo host parece inofensivo —y casi siempre lo es—
    # pero "casi siempre" no se puede comprobar en el momento de la llamada:
    # basta un open redirect en el proveedor para que "el mismo host" acabe
    # siendo otro. La aprobacion del usuario nombraba UN recurso; mandar la
    # credencial a otro sitio es ejecutar algo que nadie aprobo.
    #
    # El llamador recibe `redirect_denied` y el host de destino, para que una
    # persona decida si aprueba el nuevo recurso.
    SEGUIR_REDIRECCIONES = False

    def execute(self, prepared: PreparedRequest) -> ExecutionResult:
        """Unica entrada. Devuelve SIEMPRE un `ExecutionResult`, nunca lanza.

        Que no lance es deliberado: el Broker ya consumio el grant antes de
        llamar aqui, y una excepcion que se escapara acabaria en el
        `errorhandler(500)` del ERP, que imprime la traza.
        """
        if not isinstance(prepared, PreparedRequest):
            # El Broker es el unico que los fabrica. Si llega otra cosa, quien
            # llama se ha saltado el carril.
            return ExecutionResult(code="invalid_request")

        inicio = time.monotonic()
        try:
            metodo = (prepared.method or "").upper()
            if metodo not in METHODS:
                raise ExecutorError("method_denied")
            destino = _destino(prepared.resource)
            _valida_cabeceras(prepared.headers or {})
        except ExecutorError as exc:
            return ExecutionResult(code="invalid_request", detail=exc.code)

        try:
            return self._hablar(prepared, destino, metodo, inicio)
        except (socket.timeout, TimeoutError):
            return ExecutionResult(code="timeout")
        except (ConnectionError, socket.gaierror, OSError):
            # Nombre que no resuelve, conexion rechazada, red caida. El
            # proveedor no esta; no es culpa de la peticion.
            return ExecutionResult(code="provider_unavailable")
        except ssl.SSLError:
            return ExecutionResult(code="provider_unavailable",
                                   detail="tls")
        except http.client.HTTPException:
            # Respuesta que no es HTTP valido.
            return ExecutionResult(code="invalid_response")
        except Exception as exc:  # noqa: BLE001
            # El TIPO, nunca el mensaje: el `str()` de una excepcion de una
            # libreria puede llevar la peticion entera —cabeceras incluidas—
            # dentro.
            logger.warning("vault executor failed")
            return ExecutionResult(code="external_error",
                                   detail=type(exc).__name__)

    # -- la llamada --------------------------------------------------------

    def _restante(self, inicio: float) -> float:
        queda = self.deadline - (time.monotonic() - inicio)
        if queda <= 0:
            raise TimeoutError
        return queda

    def _hablar(self, prepared: PreparedRequest, destino: Destino,
                metodo: str, inicio: float) -> ExecutionResult:
        cuerpo = (prepared.body or "").encode("utf-8") if prepared.body else None
        cabeceras = dict(prepared.headers or {})
        # Puestas por nosotros, no por quien llama: `Connection: close` para no
        # dejar el socket vivo con la credencial ya enviada, y `Accept-Encoding:
        # identity` porque un cuerpo comprimido se mide despues de descomprimir
        # y eso rompe el limite de tamano.
        cabeceras["Connection"] = "close"
        cabeceras["Accept-Encoding"] = "identity"
        if cuerpo is not None:
            cabeceras["Content-Length"] = str(len(cuerpo))

        conexion = self._conectar(destino, min(self.connect_timeout,
                                               self._restante(inicio)))
        try:
            if conexion.sock is not None:
                conexion.sock.settimeout(min(self.read_timeout,
                                             self._restante(inicio)))
            conexion.request(metodo, destino.path, body=cuerpo,
                             headers=cabeceras)
            # A partir de aqui la credencial ya salio. Se borra la copia local
            # para que no siga en el marco mientras se lee la respuesta.
            cabeceras.clear()
            del cuerpo

            respuesta = conexion.getresponse()
            return self._leer(respuesta, destino, inicio)
        finally:
            try:
                conexion.close()
            except Exception:  # noqa: BLE001
                pass

    def _conectar(self, destino: Destino, timeout: float):
        if not destino.tls:
            return http.client.HTTPConnection(destino.host, destino.port,
                                              timeout=timeout)
        contexto = self._ssl or ssl.create_default_context()
        return http.client.HTTPSConnection(destino.host, destino.port,
                                           timeout=timeout, context=contexto)

    def _leer(self, respuesta, destino: Destino,
              inicio: float) -> ExecutionResult:
        estado = int(respuesta.status)

        # -- redireccion: no se sigue, y se dice a donde queria llevarnos ----
        if estado in (301, 302, 303, 307, 308):
            destino_nuevo = respuesta.getheader("Location") or ""
            return ExecutionResult(
                code="redirect_denied", status=estado,
                detail=self._host_de(destino_nuevo, destino))

        # -- cuerpo, a trozos y con tope -------------------------------------
        trozos: list[bytes] = []
        total = 0
        while True:
            self._restante(inicio)
            trozo = respuesta.read(min(CHUNK, self.max_bytes - total + 1))
            if not trozo:
                break
            total += len(trozo)
            if total > self.max_bytes:
                # Se para AQUI. No se guarda lo leido ni se intenta recortar:
                # media respuesta es basura y ademas ya ocupa memoria.
                return ExecutionResult(code="response_too_large", status=estado)
            trozos.append(trozo)

        try:
            texto = b"".join(trozos).decode("utf-8", errors="replace")
        finally:
            trozos.clear()

        if estado == 401:
            return ExecutionResult(code="unauthorized", status=estado,
                                   body=texto)
        if estado == 403:
            return ExecutionResult(code="rejected", status=estado, body=texto)
        if estado == 429:
            # Merece su propio codigo para el llamador, pero no tenemos uno en
            # la lista cerrada: es un rechazo del proveedor, con su status.
            return ExecutionResult(code="rejected", status=estado, body=texto)
        if 400 <= estado < 600:
            return ExecutionResult(code="external_error", status=estado,
                                   body=texto)
        return ExecutionResult(code="success", status=estado, body=texto)

    @staticmethod
    def _host_de(location: str, origen: Destino) -> str:
        """Solo el host del destino, y solo si parece un host.

        `Location` lo escribe el proveedor: es dato no confiable. Se devuelve
        acotado para que una persona pueda decidir, no para que un programa lo
        siga.
        """
        try:
            u = urlsplit((location or "").strip())
        except ValueError:
            return "desconocido"
        if u.scheme and u.scheme.lower() not in ("http", "https"):
            # `javascript:`, `data:`, `file:`... No son relativos ni son un
            # host: son otra cosa, y llamarlos "relativo" seria mentir en el
            # unico dato que esta pantalla le da a una persona para decidir.
            return "desconocido"
        host = (u.hostname or "").strip().lower()
        if not host:
            return f"relativo:{origen.host}"
        if not re.match(r"^[a-z0-9.\-]{1,120}$", host):
            return "desconocido"
        return host


__all__ = ["HttpExecutor", "ExecutorError", "MAX_RESPONSE_BYTES",
           "CONNECT_TIMEOUT", "READ_TIMEOUT", "TOTAL_DEADLINE", "METHODS"]
