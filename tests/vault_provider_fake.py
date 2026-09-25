"""FASE 10.3.4-B — proveedor simulado. Solo para tests, nunca importado por `app`.

POR QUE UN SERVIDOR DE VERDAD Y NO UN DOBLE EN MEMORIA

Un doble que devuelve `ExecutionResult` no prueba el Executor: prueba el
Broker, que ya estaba probado en 10.3.3. Lo que 10.3.4-B necesita verificar
—timeouts reales, redirecciones que no se siguen, cuerpos que hay que cortar a
mitad de lectura— solo ocurre sobre un socket.

Asi que esto es `http.server` en un hilo, sobre 127.0.0.1, con puerto 0 para
que el sistema elija uno libre. No sale de la maquina y no toca Internet.

REGISTRA LO QUE RECIBE

`peticiones` guarda metodo, camino y CABECERAS de cada llamada. Es lo que
permite la prueba que da sentido a toda la politica de redireccion: el servidor
B puede demostrar que NO recibio `Authorization` porque sabe exactamente lo que
le llego.

Ese registro contiene el secreto cuando la prueba se lo manda a proposito. Vive
en memoria, en el proceso del test, y muere con el servidor.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class ProveedorFalso:
    """Servidor local con un guion por camino.

    Los caminos son los casos A-N que pide la fase. Cada uno existe para una
    prueba concreta; no hay ninguno "por si acaso".
    """

    def __init__(self, *, secreto: str = "", eco: str = ""):
        self.secreto = secreto
        self.eco = eco or secreto
        self.peticiones: list[dict] = []
        self._lock = threading.Lock()
        proveedor = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            # Silencio: el log por defecto de http.server escribe la linea de
            # peticion en stderr, y los tests de fuga miran stderr.
            def log_message(self, *a):  # noqa: D102
                return

            def _registrar(self):
                with proveedor._lock:  # noqa: SLF001
                    proveedor.peticiones.append({
                        "method": self.command,
                        "path": self.path,
                        "headers": {k.lower(): v
                                    for k, v in self.headers.items()},
                    })

            def _responder(self, codigo, cuerpo=b"", extra=None):
                self.send_response(codigo)
                self.send_header("Content-Length", str(len(cuerpo)))
                self.send_header("Content-Type", "application/json")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if cuerpo:
                    self.wfile.write(cuerpo)

            def do_POST(self):  # noqa: N802
                largo = int(self.headers.get("Content-Length") or 0)
                if largo:
                    self.rfile.read(largo)
                self.do_GET()

            def do_GET(self):  # noqa: N802, C901
                self._registrar()
                camino = self.path.split("?")[0]
                s = proveedor.eco

                # A — exito limpio
                if camino == "/ok":
                    return self._responder(200, b'{"items": [1, 2, 3]}')

                # B..F — la familia de errores del proveedor
                if camino == "/400":
                    return self._responder(400, b'{"error": "bad request"}')
                if camino == "/401":
                    return self._responder(401, b'{"error": "unauthorized"}')
                if camino == "/403":
                    return self._responder(403, b'{"error": "forbidden"}')
                if camino == "/429":
                    return self._responder(429, b'{"error": "rate limited"}')
                if camino == "/500":
                    return self._responder(500, b'{"error": "boom"}')

                # G — no responde nunca (el Executor tiene que cortar)
                if camino == "/cuelga":
                    time.sleep(30)
                    return self._responder(200, b"tarde")

                # H — redireccion a OTRO host
                if camino == "/redirect":
                    otro = proveedor.destino_redireccion or "http://127.0.0.1:1/b"
                    return self._responder(302, b"", {"Location": otro})
                if camino == "/redirect-mismo":
                    return self._responder(302, b"", {"Location": "/ok"})

                # I — respuestas de tamano controlado
                if camino.startswith("/grande"):
                    try:
                        n = int(camino.rsplit("/", 1)[-1])
                    except ValueError:
                        n = 1024 * 1024
                    return self._responder(200, b"A" * n)

                # J/M — el secreto literal en el cuerpo
                if camino == "/eco-body":
                    return self._responder(
                        200, json.dumps({"token": s}).encode())
                if camino == "/eco-texto":
                    return self._responder(200, f"valor={s}".encode())

                # K — el secreto tal y como se lo mandamos
                if camino == "/eco-auth":
                    recibido = self.headers.get("Authorization") or "(ninguna)"
                    return self._responder(
                        200, json.dumps({"visto": recibido}).encode())

                # N — el secreto en una CABECERA de respuesta
                if camino == "/eco-header":
                    return self._responder(200, b'{"ok": true}',
                                           {"X-Debug-Token": s})

                # codificado, para el saneado secundario
                if camino == "/eco-b64":
                    import base64

                    b64 = base64.b64encode(s.encode()).decode()
                    return self._responder(200, json.dumps({"t": b64}).encode())
                if camino == "/eco-hex":
                    return self._responder(
                        200, json.dumps({"t": s.encode().hex()}).encode())

                # respuesta que no es JSON ni nada util
                if camino == "/basura":
                    return self._responder(200, b"\xff\xfe\x00 no soy utf-8")

                return self._responder(404, b'{"error": "no existe"}')

        self.destino_redireccion = ""
        self._servidor = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._hilo = threading.Thread(target=self._servidor.serve_forever,
                                      daemon=True)
        self._hilo.start()

    @property
    def puerto(self) -> int:
        return self._servidor.server_address[1]

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.puerto}"

    def url(self, camino: str) -> str:
        return self.base + camino

    def recibio_authorization(self) -> bool:
        with self._lock:
            return any("authorization" in p["headers"] for p in self.peticiones)

    def cabeceras_vistas(self) -> list[dict]:
        with self._lock:
            return [dict(p["headers"]) for p in self.peticiones]

    def cerrar(self):
        try:
            self._servidor.shutdown()
            self._servidor.server_close()
        except Exception:  # noqa: BLE001
            pass
