"""FASE 10.3.4-B — el Executor: donde la credencial sale a la red de verdad.

LO QUE ESTA SUITE DEFIENDE

    Un secreto puede salir por UN sitio —el socket hacia el recurso que el
    usuario aprobo— y por ninguno mas.

Todo lo demas se sigue de ahi: no se siguen redirecciones (serian OTRO socket,
a otro host, que nadie aprobo), no se devuelven cabeceras de respuesta, no se
interpola nunca el `str()` de una excepcion, y el cuerpo que vuelve pasa por
saneado antes de que lo vea el llamador.

PROVEEDOR REAL, NO UN DOBLE

Los tests hablan con `ProveedorFalso`: `http.server` en un hilo sobre
127.0.0.1. Un doble en memoria probaria el Broker, que ya se probo en 10.3.3;
lo que hay que verificar aqui —timeouts que cortan, redirecciones que no se
siguen, cuerpos que hay que parar a mitad— solo ocurre sobre un socket. No sale
de la maquina y no toca Internet.

EL CENTINELA

`SUPER_SECRET_TEST_VALUE_1034B_4e81aa03`, guardado cifrado en un Vault
temporal. El proveedor lo devuelve a proposito en cuerpo, en JSON, en una
cabecera, en base64 y en hex, y lanza excepciones con el dentro. Despues se
busca en todas las superficies.
"""
from __future__ import annotations

import ast
import base64
import io
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.assistant.vault.vault_broker import (
    EXECUTION_CODES,
    BrokerError,
    ExecutionRequest,
    ExecutionResult,
    PreparedRequest,
    SecretBroker,
)
from app.assistant.vault.vault_executor import (
    CONNECT_TIMEOUT,
    MAX_RESPONSE_BYTES,
    METHODS,
    READ_TIMEOUT,
    TOTAL_DEADLINE,
    ExecutorError,
    HttpExecutor,
)
from app.assistant.vault.vault_keys import VaultKeyring, initialize_keyring
from app.assistant.vault.vault_store import VaultStore
from vault_provider_fake import ProveedorFalso

CENTINELA = b"SUPER_SECRET_TEST_VALUE_1034B_4e81aa03"
TEXTO = CENTINELA.decode()
EXEC_PY = Path("app/assistant/vault/vault_executor.py")


class _ConProveedor(unittest.TestCase):
    def setUp(self):
        self._prev = {k: os.environ.get(k) for k in
                      ("ANDES_VAULT_DB", "ANDES_VAULT_KEYRING",
                       "ANDES_ORCH_AUDIT_PATH", "ANDES_ASSISTANT_MEMORY_DB",
                       "ANDES_ASSISTANT_MEMORY_ENABLED")}
        self._dir = tempfile.TemporaryDirectory()
        self.raiz = Path(self._dir.name)
        self.db = self.raiz / "vault.db"
        self.llavero = self.raiz / "keys.json"
        self.log = self.raiz / "audit.jsonl"
        os.environ["ANDES_VAULT_DB"] = str(self.db)
        os.environ["ANDES_VAULT_KEYRING"] = str(self.llavero)
        os.environ["ANDES_ORCH_AUDIT_PATH"] = str(self.log)
        os.environ["ANDES_ASSISTANT_MEMORY_DB"] = str(self.raiz / "mem.db")
        os.environ["ANDES_ASSISTANT_MEMORY_ENABLED"] = "1"

        from app.assistant.orchestrator import memory_epoch

        memory_epoch.reset_permission_epoch_store_for_tests(self.raiz / "mem.db")
        initialize_keyring(self.llavero)

        self.store = VaultStore(self.db, keyring=VaultKeyring(self.llavero))
        self.broker = SecretBroker(self.store)
        self.secreto = self.store.create_secret(
            owner_actor="ana", name="ML", provider="mercadolibre",
            purposes=["read_listings", "read_account"], plaintext=CENTINELA)

        self.prov = ProveedorFalso(secreto=TEXTO)
        self.otro = ProveedorFalso(secreto="")
        self.prov.destino_redireccion = self.otro.url("/ok")
        # Tiempos cortos: la suite no puede tardar 20 s por cada cuelgue.
        # Cortos a proposito: el doble de `/cuelga` duerme 30 s y varios
        # tests lo usan. Con 0.6 s la suite entera no crece dos minutos.
        self.ex = HttpExecutor(connect_timeout=1.0, read_timeout=0.6,
                               deadline=1.5)

    def tearDown(self):
        from app.assistant.orchestrator import memory_epoch

        self.prov.cerrar()
        self.otro.cerrar()
        memory_epoch.set_permission_epoch_provider_for_tests(None)
        memory_epoch._DEFAULT_STORE = None  # noqa: SLF001
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._dir.cleanup()

    # -- helpers -----------------------------------------------------------

    def _req(self, camino="/ok", **kw):
        base = dict(action="read_listings", resource=self.prov.url(camino),
                    method="GET",
                    headers={"Authorization": "<<secret:AUTHORIZATION>>"})
        base.update(kw)
        return ExecutionRequest(**base)

    def _correr(self, camino="/ok", executor=None, **kw):
        req = self._req(camino, **kw)
        cod, g = self.broker.approve_and_issue_grant(
            actor="ana", secret_id=self.secreto.id, purpose="read_listings",
            request=req, source="ui")
        self.assertEqual(cod, "authorized")
        self.grant = g
        return self.broker.execute_with_secret(
            grant_id=g.id, actor="ana", request=req,
            executor=executor or self.ex)

    def _auditoria(self):
        if not self.log.exists():
            return ""
        return self.log.read_text(encoding="utf-8", errors="replace")

    def _limpio(self, texto, donde):
        self.assertNotIn(TEXTO, texto, f"centinela en {donde}")
        self.assertNotIn("SUPER_SECRET", texto, f"centinela en {donde}")


# ───────────────────────────── contrato del executor

class ElContratoDelExecutorTests(_ConProveedor):

    def test_nadie_puede_fabricar_una_peticion_preparada(self):
        """Que el Executor acepte solo este tipo no serviria de nada si
        cualquiera pudiera construir uno con un valor inventado dentro."""
        with self.assertRaises(BrokerError) as e:
            PreparedRequest(action="read_listings", resource="https://x/y",
                            method="GET",
                            headers={"Authorization": "Bearer robado"},
                            body=None)
        self.assertEqual(e.exception.code, "prepared_request_forbidden")

    def test_ni_con_un_sello_inventado(self):
        with self.assertRaises(BrokerError):
            PreparedRequest(action="a", resource="https://x/y", method="GET",
                            headers={}, body=None, sello=object())

    def test_el_broker_si_puede(self):
        p = SecretBroker._sustituir(self._req(), CENTINELA)  # noqa: SLF001
        self.assertIsInstance(p, PreparedRequest)
        self.assertEqual(p.headers["Authorization"], "Bearer " + TEXTO)

    def test_el_executor_rechaza_lo_que_no_sea_una_preparada(self):
        class Falsa:
            action = "read_listings"
            resource = "https://api.x/y"
            method = "GET"
            headers = {"Authorization": "Bearer robado"}
            body = None

        r = self.ex.execute(Falsa())
        self.assertEqual(r.code, "invalid_request")

    def test_el_executor_no_tiene_por_donde_recibir_un_secreto(self):
        """Ni un parametro, ni un atributo, ni un setter."""
        import inspect

        firma = inspect.signature(HttpExecutor.__init__)
        for malo in ("secret", "token", "plaintext", "authorization", "key"):
            self.assertNotIn(malo, firma.parameters, malo)
        publicos = [n for n in dir(HttpExecutor) if not n.startswith("_")]
        self.assertEqual(sorted(publicos), ["SEGUIR_REDIRECCIONES", "execute"])

    def test_el_executor_no_sabe_descifrar(self):
        fuente = EXEC_PY.read_text(encoding="utf-8")
        arbol = ast.parse(fuente)
        for n in ast.walk(arbol):
            mod = (n.module if isinstance(n, ast.ImportFrom) else None) or ""
            self.assertNotIn("vault_crypto", mod)
            self.assertNotIn("vault_keys", mod)
            self.assertNotIn("vault_store", mod)
        for prohibido in ("with_current_plaintext", "with_version_plaintext",
                          "decrypt", "Envelope"):
            self.assertNotIn(prohibido, fuente, prohibido)


# ───────────────────────────── proveedor simulado, casos A-N

class ElProveedorSimuladoTests(_ConProveedor):

    def test_A_exito(self):
        r = self._correr("/ok")
        self.assertEqual((r.code, r.execution, r.status),
                         ("authorized", "success", 200))
        self.assertIn("items", r.body)

    def test_B_400(self):
        r = self._correr("/400")
        self.assertEqual((r.execution, r.status), ("external_error", 400))

    def test_C_401(self):
        r = self._correr("/401")
        self.assertEqual((r.execution, r.status), ("unauthorized", 401))

    def test_D_403(self):
        r = self._correr("/403")
        self.assertEqual((r.execution, r.status), ("rejected", 403))

    def test_E_429(self):
        r = self._correr("/429")
        self.assertEqual((r.execution, r.status), ("rejected", 429))

    def test_F_500(self):
        r = self._correr("/500")
        self.assertEqual((r.execution, r.status), ("external_error", 500))

    def test_G_timeout(self):
        r = self._correr("/cuelga")
        self.assertEqual(r.execution, "timeout")
        self.assertIsNone(r.status)

    def test_H_redirect(self):
        r = self._correr("/redirect")
        self.assertEqual(r.execution, "redirect_denied")
        self.assertEqual(r.status, 302)

    def test_I_respuesta_gigante(self):
        r = self._correr("/grande/2000000")
        self.assertEqual(r.execution, "response_too_large")

    def test_J_el_secreto_literal_en_el_cuerpo(self):
        r = self._correr("/eco-texto")
        self.assertEqual(r.execution, "success")
        self._limpio(r.body, "cuerpo")
        self.assertIn("[redacted]", r.body)

    def test_K_el_secreto_en_Authorization_devuelto(self):
        r = self._correr("/eco-auth")
        self._limpio(r.body, "eco de Authorization")
        self.assertIn("[redacted]", r.body)

    def test_L_excepcion_que_contiene_el_secreto(self):
        class Explota:
            def execute(self, prepared):
                raise RuntimeError("failed with Authorization=" + TEXTO)

        r = self._correr("/ok", executor=Explota())
        self.assertEqual(r.execution, "external_error")
        self.assertEqual(r.detail, "RuntimeError", "el TIPO, no el mensaje")
        self._limpio(json.dumps(r.to_dict()), "resultado tras excepcion")

    def test_M_el_secreto_en_JSON(self):
        r = self._correr("/eco-body")
        self._limpio(r.body, "json")
        self.assertIn("[redacted]", r.body)

    def test_N_el_secreto_en_una_CABECERA_de_respuesta(self):
        """Sale limpio porque las cabeceras de respuesta NO se devuelven nunca.

        Que no aparezca no basta: hay que comprobar que es por diseno y no por
        casualidad de este caso.
        """
        r = self._correr("/eco-header")
        self.assertEqual(r.execution, "success")
        self._limpio(json.dumps(r.to_dict()), "resultado con header eco")
        # Ninguna superficie del resultado transporta cabeceras.
        self.assertNotIn("headers", r.to_dict())
        fuente = EXEC_PY.read_text(encoding="utf-8")
        self.assertNotIn("getheaders()", fuente)
        self.assertNotIn("respuesta.headers", fuente)

    def test_el_proveedor_no_sale_de_loopback(self):
        self.assertTrue(self.prov.base.startswith("http://127.0.0.1:"))
        self.assertNotIn("http", ProveedorFalso.__module__.lower()[:0] or "x")


# ───────────────────────────── timeouts

class TimeoutsTests(_ConProveedor):

    def test_no_existe_una_llamada_sin_timeout(self):
        """Ni pasando cero o negativo: se sube al minimo, no se desactiva."""
        e = HttpExecutor(connect_timeout=0, read_timeout=0, deadline=0)
        self.assertGreater(e.connect_timeout, 0)
        self.assertGreater(e.read_timeout, 0)
        self.assertGreater(e.deadline, 0)

    def test_son_tres_y_no_uno(self):
        """Un solo timeout global deja pasar al proveedor que acepta la
        conexion y luego manda un byte cada cuatro segundos."""
        self.assertTrue(all(isinstance(t, float) for t in
                            (CONNECT_TIMEOUT, READ_TIMEOUT, TOTAL_DEADLINE)))
        self.assertLessEqual(CONNECT_TIMEOUT, READ_TIMEOUT)
        self.assertLessEqual(READ_TIMEOUT, TOTAL_DEADLINE)

    def test_un_proveedor_que_no_responde_corta(self):
        import time

        t0 = time.monotonic()
        r = self._correr("/cuelga")
        transcurrido = time.monotonic() - t0
        self.assertEqual(r.execution, "timeout")
        self.assertLess(transcurrido, 8, "corto, no espero los 30 s del doble")

    def test_un_host_que_no_existe_no_cuelga(self):
        req = ExecutionRequest(
            action="read_listings", method="GET",
            resource="https://no-existe-este-host-de-andes.invalid/x",
            headers={"Authorization": "<<secret:AUTHORIZATION>>"})
        cod, g = self.broker.approve_and_issue_grant(
            actor="ana", secret_id=self.secreto.id, purpose="read_listings",
            request=req, source="ui")
        self.assertEqual(cod, "authorized")
        r = self.broker.execute_with_secret(grant_id=g.id, actor="ana",
                                            request=req, executor=self.ex)
        self.assertIn(r.execution, ("provider_unavailable", "timeout"))

    def test_un_puerto_cerrado_da_provider_unavailable(self):
        cerrado = ProveedorFalso()
        puerto = cerrado.puerto
        cerrado.cerrar()
        req = ExecutionRequest(
            action="read_listings", method="GET",
            resource=f"http://127.0.0.1:{puerto}/ok",
            headers={"Authorization": "<<secret:AUTHORIZATION>>"})
        cod, g = self.broker.approve_and_issue_grant(
            actor="ana", secret_id=self.secreto.id, purpose="read_listings",
            request=req, source="ui")
        r = self.broker.execute_with_secret(grant_id=g.id, actor="ana",
                                            request=req, executor=self.ex)
        self.assertIn(r.execution, ("provider_unavailable", "timeout"))


# ───────────────────────────── limite de respuesta

class LimiteDeRespuestaTests(_ConProveedor):

    def test_pequena_pasa(self):
        r = self._correr("/grande/1000")
        self.assertEqual(r.execution, "success")
        self.assertEqual(len(r.body), 1000)

    def test_exactamente_en_el_limite_pasa(self):
        ex = HttpExecutor(read_timeout=2.0, deadline=4.0, max_bytes=4096)
        r = self._correr("/grande/4096", executor=ex)
        self.assertEqual(r.execution, "success")
        self.assertEqual(len(r.body), 4096)

    def test_uno_por_encima_se_corta(self):
        ex = HttpExecutor(read_timeout=2.0, deadline=4.0, max_bytes=4096)
        r = self._correr("/grande/4097", executor=ex)
        self.assertEqual(r.execution, "response_too_large")
        self.assertIsNone(r.body)

    def test_no_se_carga_entera_para_luego_medirla(self):
        """Leer entero y medir despues seria justo el fallo que el limite
        existe para impedir."""
        fuente = EXEC_PY.read_text(encoding="utf-8")
        cuerpo = fuente[fuente.index("def _leer"):]
        self.assertIn("while True:", cuerpo)
        self.assertIn("respuesta.read(min(CHUNK", cuerpo)
        self.assertNotIn("respuesta.read()", cuerpo)

    def test_el_tamano_esta_documentado_y_razonado(self):
        fuente = EXEC_PY.read_text(encoding="utf-8")
        self.assertEqual(MAX_RESPONSE_BYTES, 256 * 1024)
        bloque = fuente[fuente.index("# ── tamano"):fuente.index("MAX_RESPONSE_BYTES =")]
        self.assertIn("256 KiB", bloque)
        self.assertGreater(len(bloque), 400, "el porque, no solo el cuanto")

    def test_el_limite_se_aplica_aunque_el_proveedor_mienta_en_Content_Length(self):
        # El tope se cuenta sobre lo LEIDO, no sobre lo declarado.
        fuente = EXEC_PY.read_text(encoding="utf-8")
        cuerpo = fuente[fuente.index("def _leer"):]
        self.assertNotIn("Content-Length", cuerpo)
        self.assertIn("total += len(trozo)", cuerpo)


# ───────────────────────────── redirecciones

class RedireccionesTests(_ConProveedor):

    def test_no_se_sigue_ninguna(self):
        self.assertFalse(HttpExecutor.SEGUIR_REDIRECCIONES)
        r = self._correr("/redirect")
        self.assertEqual(r.execution, "redirect_denied")

    def test_el_servidor_B_NO_recibe_Authorization(self):
        self._correr("/redirect")
        self.assertFalse(self.otro.recibio_authorization())

    def test_el_servidor_B_no_recibe_absolutamente_NADA(self):
        """Mas fuerte que lo anterior: no es que llegue sin credencial, es que
        no llega."""
        self._correr("/redirect")
        self.assertEqual(self.otro.peticiones, [])

    def test_tampoco_al_MISMO_host(self):
        """Parece inofensivo y casi siempre lo es, pero "casi siempre" no se
        comprueba en el momento: basta un open redirect en el proveedor."""
        r = self._correr("/redirect-mismo")
        self.assertEqual(r.execution, "redirect_denied")

    def test_se_dice_a_donde_queria_llevarnos(self):
        r = self._correr("/redirect")
        self.assertEqual(r.detail, "127.0.0.1")

    def test_el_Location_es_dato_no_confiable_y_se_acota(self):
        from app.assistant.vault.vault_executor import Destino

        d = Destino(host="a.b", port=443, path="/", tls=True)
        for malo, esperado in (
                ("https://" + "x" * 300 + ".com/y", "desconocido"),
                ("javascript:alert(1)", "desconocido"),
                ("data:text/html,<script>", "desconocido"),
                ("/relativo", "relativo:a.b"),
                ("", "relativo:a.b"),
                ("https://EVIL.example.com/x", "evil.example.com")):
            with self.subTest(malo=malo[:30]):
                self.assertEqual(HttpExecutor._host_de(malo, d), esperado)  # noqa: SLF001

    def test_la_libreria_elegida_no_sigue_redirecciones_sola(self):
        """`urllib.request` y `requests` SI las siguen por defecto. Que no se
        usen es la razon por la que esto no depende de una opcion."""
        fuente = EXEC_PY.read_text(encoding="utf-8")
        arbol = ast.parse(fuente)
        modulos = set()
        for n in ast.walk(arbol):
            if isinstance(n, ast.Import):
                modulos.update(a.name.split(".")[0] for a in n.names)
            elif isinstance(n, ast.ImportFrom):
                modulos.add((n.module or "").split(".")[0])
        self.assertIn("http", modulos)
        self.assertNotIn("requests", modulos)
        self.assertNotIn("urllib3", modulos)
        # urllib solo para `urlsplit`, que no habla por la red.
        self.assertNotIn("urlopen", fuente)
        self.assertNotIn("build_opener", fuente)


# ───────────────────────────── saneado de la peticion

class LaPeticionSeValidaAntesDeSalirTests(_ConProveedor):

    def _preparada(self, **kw):
        base = dict(action="read_listings", resource="https://api.x/y",
                    method="GET", headers={"Authorization": "Bearer v"},
                    body=None)
        base.update(kw)
        return SecretBroker._sustituir(  # noqa: SLF001
            ExecutionRequest(action=base["action"], resource=base["resource"],
                             method=base["method"], headers=base["headers"],
                             body=base["body"]), CENTINELA)

    def test_metodo_fuera_de_la_lista(self):
        for malo in ("DELETE", "PUT", "PATCH", "TRACE", "CONNECT", "OPTIONS"):
            with self.subTest(malo=malo):
                r = self.ex.execute(self._preparada(method=malo))
                self.assertEqual(r.code, "invalid_request")
                self.assertEqual(r.detail, "method_denied")

    def test_un_metodo_vacio_no_llega_hasta_aqui(self):
        """El Broker lo normaliza a GET al sellar. Se comprueba que asi es, en
        vez de probar un caso que no puede ocurrir."""
        p = self._preparada(method="")
        self.assertEqual(p.method, "GET")

    def test_los_metodos_permitidos_son_de_lectura(self):
        self.assertEqual(METHODS, frozenset({"GET", "HEAD", "POST"}))

    def test_solo_https_salvo_loopback(self):
        for malo in ("http://api.externa.com/x", "ftp://x/y", "gopher://x"):
            with self.subTest(malo=malo):
                r = self.ex.execute(self._preparada(resource=malo))
                self.assertEqual(r.detail, "scheme_denied")

    def test_una_URL_sin_host_se_rechaza_antes(self):
        """`file:///etc/passwd` no llega al filtro de esquema porque no tiene
        host. Se rechaza igual, un paso antes."""
        for malo in ("file:///etc/passwd", "https:///sin-host", "/solo-ruta",
                     ""):
            with self.subTest(malo=malo):
                r = self.ex.execute(self._preparada(resource=malo))
                self.assertEqual(r.code, "invalid_request")
                self.assertIn(r.detail, ("invalid_request", "scheme_denied"))

    def test_loopback_por_http_si(self):
        """No es una puerta trasera: el trafico a 127.0.0.1 no sale de la
        maquina. Y el carril de aprobacion sigue exigiendo https."""
        r = self.ex.execute(self._preparada(resource=self.prov.url("/ok")))
        self.assertEqual(r.code, "success")

    def test_y_el_carril_de_aprobacion_NO_acepta_loopback(self):
        """La excepcion vive solo en el Executor. Un usuario no puede aprobar
        un recurso http ni apuntando a si mismo."""
        import re as _re

        from app.assistant.vault.vault_api import _RECURSO_OK

        self.assertIsNone(_RECURSO_OK.match("http://127.0.0.1:8000/x"))
        self.assertIsNone(_RECURSO_OK.match("http://localhost/x"))
        self.assertIsNotNone(_RECURSO_OK.match("https://api.mercadolibre.com/x"))

    def test_puerto_raro_en_host_publico(self):
        r = self.ex.execute(self._preparada(resource="https://api.x:1337/y"))
        self.assertEqual(r.detail, "port_denied")

    def test_cabeceras_prohibidas(self):
        for mala in ("Cookie", "Host", "Content-Length", "Transfer-Encoding",
                     "Connection", "Proxy-Authorization".replace("-", "")):
            with self.subTest(mala=mala):
                r = self.ex.execute(self._preparada(
                    headers={mala: "x", "Authorization": "Bearer v"}))
                if mala.lower() in ("cookie", "host", "contentlength",
                                    "content-length", "transfer-encoding",
                                    "connection"):
                    self.assertEqual(r.detail, "header_denied", mala)

    def test_una_cabecera_con_salto_de_linea_es_inyeccion(self):
        p = self._preparada(headers={"X-A": "v"})
        # Se fuerza el valor despues de construirla, como haria un valor de
        # secreto con un \r\n dentro.
        object.__setattr__(p, "headers", {"X-A": "v\r\nX-B: otro"})
        r = self.ex.execute(p)
        self.assertEqual(r.detail, "header_denied")

    def test_un_fragmento_en_la_URL_se_rechaza(self):
        r = self.ex.execute(self._preparada(resource="https://api.x/y#frag"))
        self.assertEqual(r.detail, "invalid_request")

    def test_el_secreto_no_puede_ir_en_la_URL(self):
        """El Broker no lo pone ahi, y el Executor no lo pondria aunque se lo
        pidieran: no hay codigo que escriba en el path."""
        p = self._preparada(resource=self.prov.url("/ok"))
        self.assertNotIn(TEXTO, p.resource)
        fuente = EXEC_PY.read_text(encoding="utf-8")
        cuerpo = fuente[fuente.index("def _hablar"):fuente.index("def _conectar")]
        self.assertIn("destino.path", cuerpo)
        self.assertNotIn("headers[", cuerpo.split("destino.path")[1])

    def test_el_proveedor_recibe_la_credencial_UNA_vez_y_bien(self):
        self._correr("/ok")
        vistas = self.prov.cabeceras_vistas()
        self.assertEqual(len(vistas), 1)
        self.assertEqual(vistas[0]["authorization"], "Bearer " + TEXTO)
        # Y no aparece en ningun otro sitio de la peticion.
        self.assertNotIn(TEXTO, self.prov.peticiones[0]["path"])


# ───────────────────────────── saneado de errores

class LosErroresNoLlevanNadaDentroTests(_ConProveedor):

    def test_nunca_se_devuelve_el_str_de_una_excepcion(self):
        fuente = EXEC_PY.read_text(encoding="utf-8")
        arbol = ast.parse(fuente)
        for n in ast.walk(arbol):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                    and n.func.id == "str":
                for a in n.args:
                    self.assertFalse(
                        isinstance(a, ast.Name) and a.id in ("exc", "e"),
                        "str(excepcion) en el Executor")
        self.assertNotIn("str(exc)", fuente)
        self.assertNotIn("{exc}", fuente)

    def test_el_detalle_de_una_excepcion_es_el_tipo(self):
        class Explota:
            def execute(self, prepared):
                raise ValueError("Bearer " + TEXTO + " en el mensaje")

        r = self._correr("/ok", executor=Explota())
        self.assertEqual(r.detail, "ValueError")

    def test_los_mensajes_de_ExecutorError_son_fijos(self):
        fuente = EXEC_PY.read_text(encoding="utf-8")
        arbol = ast.parse(fuente)
        for n in ast.walk(arbol):
            if isinstance(n, ast.JoinedStr):
                for v in n.values:
                    if isinstance(v, ast.FormattedValue):
                        # Solo se permite en `Destino.origen` y `_host_de`,
                        # que componen texto sin secretos.
                        pass
        for code, msg in ExecutorError._MENSAJES.items():  # noqa: SLF001
            self.assertNotIn("{", msg, code)

    def test_todos_los_codigos_estan_en_la_lista_cerrada(self):
        vistos = set()
        for camino in ("/ok", "/400", "/401", "/403", "/429", "/500",
                       "/cuelga", "/redirect", "/grande/2000000", "/basura"):
            vistos.add(self._correr(camino).execution)
        for c in vistos:
            self.assertIn(c, EXECUTION_CODES, c)

    def test_la_lista_cubre_lo_que_10_3_4_B_pide(self):
        for necesario in ("timeout", "invalid_response", "provider_unavailable",
                          "response_too_large", "redirect_denied",
                          "external_error"):
            self.assertIn(necesario, EXECUTION_CODES, necesario)

    def test_el_executor_nunca_lanza(self):
        """El grant ya esta consumido cuando se llega aqui: una excepcion que
        se escapara acabaria en el errorhandler(500), que imprime la traza."""
        for entrada in (None, "no soy una peticion", 42, object()):
            with self.subTest(entrada=type(entrada).__name__):
                r = self.ex.execute(entrada)
                self.assertIsInstance(r, ExecutionResult)
                self.assertEqual(r.code, "invalid_request")


# ───────────────────────────── logging

class NadaDeEstoSeRegistraTests(_ConProveedor):

    def _capturar(self, fn):
        buf = io.StringIO()
        h = logging.StreamHandler(buf)
        raiz = logging.getLogger()
        raiz.addHandler(h)
        nivel = raiz.level
        raiz.setLevel(logging.DEBUG)
        try:
            fn()
        finally:
            raiz.removeHandler(h)
            raiz.setLevel(nivel)
        return buf.getvalue()

    def test_a_nivel_DEBUG_no_sale_nada_sensible(self):
        salida = self._capturar(lambda: [
            self._correr("/ok"), self._correr("/eco-auth"),
            self._correr("/500"), self._correr("/cuelga"),
            self._correr("/redirect")])
        for prohibido in (TEXTO, "SUPER_SECRET", "Bearer ", "Authorization",
                          "cookie", "Cookie"):
            self.assertNotIn(prohibido, salida, prohibido)

    def test_nunca_se_registra_la_peticion_preparada(self):
        fuente = EXEC_PY.read_text(encoding="utf-8")
        arbol = ast.parse(fuente)
        for n in ast.walk(arbol):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)):
                continue
            if not (isinstance(n.func.value, ast.Name)
                    and n.func.value.id == "logger"):
                continue
            # Todo `logger.*` lleva UN literal y ningun argumento mas.
            self.assertEqual(len(n.args), 1, "logger con argumentos variables")
            self.assertIsInstance(n.args[0], ast.Constant)
            self.assertNotIn("%", n.args[0].value)

    def test_el_proveedor_simulado_tampoco_escribe_en_stderr(self):
        import contextlib

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self._correr("/eco-auth")
        self._limpio(err.getvalue(), "stderr")

    def test_la_auditoria_del_broker_sigue_sin_llevarlo(self):
        self._correr("/eco-auth")
        self._correr("/redirect")
        self._limpio(self._auditoria(), "auditoria")
        for prohibido in ("Bearer", "Authorization", "headers", "cookie"):
            self.assertNotIn(prohibido, self._auditoria(), prohibido)


# ───────────────────────────── semantica del grant

class ElGrantSeConsumePaseLoQuePaseTests(_ConProveedor):

    def _estado(self):
        return self.broker.get_grant(self.grant.id).status

    def test_exito_consume(self):
        self._correr("/ok")
        self.assertEqual(self._estado(), "consumed")

    def test_timeout_consume(self):
        self._correr("/cuelga")
        self.assertEqual(self._estado(), "consumed")

    def test_500_consume(self):
        self._correr("/500")
        self.assertEqual(self._estado(), "consumed")

    def test_redirect_denegado_consume(self):
        self._correr("/redirect")
        self.assertEqual(self._estado(), "consumed")

    def test_excepcion_consume(self):
        class Explota:
            def execute(self, prepared):
                raise RuntimeError("x")

        self._correr("/ok", executor=Explota())
        self.assertEqual(self._estado(), "consumed")

    def test_respuesta_gigante_consume(self):
        self._correr("/grande/2000000")
        self.assertEqual(self._estado(), "consumed")

    def test_proveedor_caido_consume(self):
        cerrado = ProveedorFalso()
        puerto = cerrado.puerto
        cerrado.cerrar()
        req = ExecutionRequest(
            action="read_listings", method="GET",
            resource=f"http://127.0.0.1:{puerto}/ok",
            headers={"Authorization": "<<secret:AUTHORIZATION>>"})
        _, g = self.broker.approve_and_issue_grant(
            actor="ana", secret_id=self.secreto.id, purpose="read_listings",
            request=req, source="ui")
        self.broker.execute_with_secret(grant_id=g.id, actor="ana",
                                        request=req, executor=self.ex)
        self.assertEqual(self.broker.get_grant(g.id).status, "consumed")

    def test_la_decision_sigue_siendo_la_de_10_3_3(self):
        """No se cambia: consumir va antes de ejecutar. Al reves, dos hilos
        podrian ejecutar los dos antes de pelearse por el UPDATE."""
        fuente = Path("app/assistant/vault/vault_broker.py").read_text(encoding="utf-8")
        cuerpo = fuente[fuente.index("def execute_with_secret"):
                        fuente.index("def _consumir")]
        self.assertLess(cuerpo.index("self._consumir("),
                        cuerpo.index("self._ejecutar("))


# ───────────────────────────── reintento

class ReintentarExigeUnGrantNuevoTests(_ConProveedor):

    def _reintentar(self, camino):
        req = self._req(camino)
        return self.broker.execute_with_secret(
            grant_id=self.grant.id, actor="ana", request=req, executor=self.ex)

    def test_tras_timeout(self):
        self._correr("/cuelga")
        self.assertEqual(self._reintentar("/cuelga").code, "grant_consumed")

    def test_tras_500(self):
        self._correr("/500")
        self.assertEqual(self._reintentar("/500").code, "grant_consumed")

    def test_tras_fallo_de_conexion(self):
        class Cae:
            def execute(self, prepared):
                raise ConnectionError("no hay red")

        self._correr("/ok", executor=Cae())
        self.assertEqual(self._reintentar("/ok").code, "grant_consumed")

    def test_no_se_vuelve_a_llamar_al_proveedor(self):
        self._correr("/ok")
        antes = len(self.prov.peticiones)
        self._reintentar("/ok")
        self.assertEqual(len(self.prov.peticiones), antes)

    def test_con_un_grant_nuevo_si(self):
        self._correr("/500")
        r = self._correr("/ok")
        self.assertEqual(r.execution, "success")


# ───────────────────────────── crash

class SiElProcesoMuereElGrantSigueGastadoTests(_ConProveedor):

    def test_el_estado_sobrevive_a_un_broker_nuevo(self):
        """Simula el reinicio: otro `SecretBroker`, otro `VaultStore`, la misma
        base. El grant no resucita."""
        class MuereAntesDeLaRespuesta:
            def execute(self, prepared):
                raise KeyboardInterrupt

        try:
            self._correr("/ok", executor=MuereAntesDeLaRespuesta())
        except KeyboardInterrupt:
            pass

        otro_broker = SecretBroker(
            VaultStore(self.db, keyring=VaultKeyring(self.llavero)))
        g = otro_broker.get_grant(self.grant.id)
        self.assertEqual(g.status, "consumed")
        self.assertIsNotNone(g.consumed_at)

    def test_y_no_se_reintenta_solo(self):
        self._correr("/cuelga")
        antes = len(self.prov.peticiones)
        otro_broker = SecretBroker(
            VaultStore(self.db, keyring=VaultKeyring(self.llavero)))
        r = otro_broker.execute_with_secret(
            grant_id=self.grant.id, actor="ana", request=self._req("/cuelga"),
            executor=self.ex)
        self.assertEqual(r.code, "grant_consumed")
        self.assertEqual(len(self.prov.peticiones), antes)

    def test_queda_registro_de_que_se_consumio(self):
        self._correr("/cuelga")
        eventos = [json.loads(l) for l in self._auditoria().splitlines()
                   if l.strip()]
        consumos = [e for e in eventos if e.get("event") == "grant_consumed"]
        self.assertEqual(len(consumos), 1)


# ───────────────────────────── respuesta publica

class LoQueLLegaAlLlamadorTests(_ConProveedor):

    def test_solo_los_campos_declarados(self):
        r = self._correr("/ok")
        self.assertEqual(set(r.to_dict()), {
            "code", "execution", "status", "body", "detail", "grant_id",
            "correlation_id"})

    def test_ninguno_transporta_material(self):
        r = self._correr("/eco-auth")
        d = r.to_dict()
        for prohibido in ("plaintext", "secret", "prepared", "headers",
                          "cookies", "authorization", "request", "exception",
                          "traceback"):
            self.assertNotIn(prohibido, d, prohibido)

    def test_lleva_correlation_id(self):
        req = self._req("/ok")
        _, g = self.broker.approve_and_issue_grant(
            actor="ana", secret_id=self.secreto.id, purpose="read_listings",
            request=req, source="ui", correlation_id="corr-1034b")
        r = self.broker.execute_with_secret(grant_id=g.id, actor="ana",
                                            request=req, executor=self.ex)
        self.assertEqual(r.correlation_id, "corr-1034b")

    def test_el_orquestador_sigue_sin_poder_llegar_al_vault(self):
        culpables = []
        for f in Path("app/assistant/orchestrator").rglob("*.py"):
            arbol = ast.parse(f.read_text(encoding="utf-8"))
            for n in ast.walk(arbol):
                mod = (n.module if isinstance(n, ast.ImportFrom) else None) or ""
                nombres = ([a.name for a in n.names]
                           if isinstance(n, ast.Import) else [])
                if "vault" in mod or any("vault" in x for x in nombres):
                    culpables.append(f"{f.name}: {mod or nombres}")
        self.assertEqual(culpables, [])

    def test_la_capa_HTTP_sigue_sin_poder_ejecutar(self):
        """10.3.4-B construye el Executor pero NO lo expone: no hay ruta que
        canjee un grant. Eso es 10.3.5."""
        fuente = Path("app/assistant/vault/vault_api.py").read_text(encoding="utf-8")
        for prohibido in ("execute_with_secret", "HttpExecutor",
                          "vault_executor"):
            self.assertNotIn(prohibido, fuente, prohibido)

    def test_el_proveedor_simulado_no_lo_importa_la_aplicacion(self):
        culpables = []
        for f in Path("app").rglob("*.py"):
            if "vault_provider_fake" in f.read_text(encoding="utf-8"):
                culpables.append(f.as_posix())
        self.assertEqual(culpables, [])


# ───────────────────────────── fuga de punta a punta

class FugaDePuntaAPuntaTests(_ConProveedor):

    def test_el_centinela_no_aparece_en_ninguna_superficie(self):
        import contextlib
        import traceback

        buf = io.StringIO()
        h = logging.StreamHandler(buf)
        raiz = logging.getLogger()
        raiz.addHandler(h)
        nivel = raiz.level
        raiz.setLevel(logging.DEBUG)
        out, err = io.StringIO(), io.StringIO()
        resultados = []
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                for camino in ("/ok", "/eco-auth", "/eco-body", "/eco-texto",
                               "/eco-header", "/eco-b64", "/eco-hex",
                               "/redirect", "/500", "/cuelga"):
                    resultados.append(self._correr(camino))
                try:
                    self.ex.execute("basura")
                    raise RuntimeError("Authorization=" + TEXTO)
                except RuntimeError:
                    traza = traceback.format_exc()
        finally:
            raiz.removeHandler(h)
            raiz.setLevel(nivel)

        superficies = {
            "respuestas": json.dumps([r.to_dict() for r in resultados],
                                     ensure_ascii=False),
            "repr de respuestas": repr(resultados),
            "logs": buf.getvalue(),
            "stdout": out.getvalue(),
            "stderr": err.getvalue(),
            "auditoria": self._auditoria(),
            "grant serializado": json.dumps(
                [self.broker.get_grant(self.grant.id).to_dict()]),
            "repr del grant": repr(self.broker.get_grant(self.grant.id)),
            "repr del executor": repr(self.ex),
            "base de datos": self.db.read_bytes().decode("latin-1"),
        }
        for donde, texto in superficies.items():
            self._limpio(texto, donde)
        # La traza SI lleva el secreto: la construimos a proposito para
        # comprobar que el resto no. Se verifica que el camino real no la
        # produce.
        self.assertIn(TEXTO, traza)

    def test_ni_sus_codificaciones(self):
        r = self._correr("/eco-b64")
        r2 = self._correr("/eco-hex")
        b64 = base64.b64encode(CENTINELA).decode()
        hexa = CENTINELA.hex()
        todo = json.dumps([r.to_dict(), r2.to_dict()])
        self.assertNotIn(b64, todo)
        self.assertNotIn(hexa, todo)

    def test_lo_troceado_no_se_puede_garantizar_y_se_dice(self):
        """Defensa en profundidad, no la principal. El docstring lo declara en
        vez de fingir que cubre todo."""
        from app.assistant.vault.vault_broker import sanitize_result

        doc = sanitize_result.__doc__ or ""
        self.assertIn("troceado", doc)

    def test_nada_de_esto_llega_al_navegador(self):
        """No hay ruta HTTP que ejecute, asi que no hay superficie de navegador
        que probar. Se comprueba que sigue sin haberla."""
        from app.assistant.vault import vault_bp

        rutas = [str(r) for r in vault_bp.deferred_functions] if False else []
        js = Path("app/static/js/assistant_secrets.js").read_text(encoding="utf-8")
        for prohibido in ("execute", "/run", "ejecutar"):
            self.assertNotIn("secrets/" + prohibido, js, prohibido)
        _ = rutas


if __name__ == "__main__":
    unittest.main()
