"""FASE 10.3.1 parte B — `permission_epoch` cableado a mutaciones reales.

LA CORRECCION QUE ABRE ESTA UNIDAD

10.3.0 afirmo que el bump "no tiene ninguna llamada real". **Era falso**: se
busco `bump_actor_permission_epoch` en vez de la puerta publica,
`_notify_assistant_permission_epoch` -> `notify_permission_context_changed`,
que ya estaba cableada en tres sitios (crear por API, editar por API, editar por
formulario). Lo que si faltaba eran seis mutaciones mas, y un hueco dentro de un
sitio ya cableado.

POR QUE EL BUMP VA DESPUES DEL COMMIT

El epoch vive en la MISMA base SQLite que el ERP pero por otra conexion, con
`BEGIN IMMEDIATE`. Llamarlo con la transaccion de SQLAlchemy abierta haria que
esa segunda conexion esperase el lock de escritura hasta agotar su timeout y
fallase en silencio. No hay transaccion comun disponible: son dos conexiones
sobre un archivo. El precio es una ventana entre commit y bump, y por eso el
resultado se audita.

QUE PRUEBA CADA CAPA

  semantica del store   comportamiento real contra SQLite temporal
  el helper             que bumpea Y audita, y que nunca levanta
  las rutas             comportamiento real, con la capa de datos simulada
  el cableado           por AST, que ninguna mutacion se quedo sin hook
"""
from __future__ import annotations

import ast
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.assistant.orchestrator.memory_epoch import (
    SqlitePermissionEpochStore,
    bump_actor_permission_epoch,
    notify_permission_context_changed,
)

RUTAS = Path("app/seguridad/routes.py")


class _ConEpochTemporal(unittest.TestCase):
    """Base propia, nunca la del proyecto: estos tests no tocan andes.db."""

    def setUp(self):
        self._prev = {k: os.environ.get(k) for k in
                      ("ANDES_ASSISTANT_MEMORY_ENABLED",
                       "ANDES_ASSISTANT_MEMORY_DB")}
        self._dir = tempfile.TemporaryDirectory()
        self.ruta = Path(self._dir.name) / "epoch.db"
        os.environ["ANDES_ASSISTANT_MEMORY_ENABLED"] = "1"
        os.environ["ANDES_ASSISTANT_MEMORY_DB"] = str(self.ruta)
        from app.assistant.orchestrator import memory_epoch

        memory_epoch._DEFAULT_STORE = None  # noqa: SLF001
        self.store = SqlitePermissionEpochStore(self.ruta)
        self.store.ensure_schema()

    def tearDown(self):
        from app.assistant.orchestrator import memory_epoch

        memory_epoch._DEFAULT_STORE = None  # noqa: SLF001
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._dir.cleanup()

    def epoch(self, actor: str) -> int:
        return self.store.get_or_init(actor)


# ───────────────────────────── semantica del almacen

class ElEpochSeComportaComoUnContadorSeguroTests(_ConEpochTemporal):

    def test_un_cambio_efectivo_incrementa_exactamente_uno(self):
        antes = self.epoch("ana")
        self.assertEqual(self.store.bump("ana"), antes + 1)
        self.assertEqual(self.epoch("ana"), antes + 1)

    def test_no_incrementa_solo_por_leerlo(self):
        """Resolver el epoch en cada turno del asistente no puede moverlo."""
        inicial = self.epoch("ana")
        for _ in range(20):
            self.epoch("ana")
        self.assertEqual(self.epoch("ana"), inicial)

    def test_persiste_entre_instancias(self):
        self.store.bump("ana")
        self.store.bump("ana")
        esperado = self.epoch("ana")
        otra = SqlitePermissionEpochStore(self.ruta)
        self.assertEqual(otra.get_or_init("ana"), esperado)

    def test_sobrevive_a_un_reinicio_del_proceso(self):
        """Sin variable global: el valor esta en disco, no en memoria."""
        import subprocess
        import sys

        self.store.bump("ana")
        self.store.bump("ana")
        esperado = self.epoch("ana")
        guion = (
            "import sys; sys.path.insert(0, r'%s')\n"
            "from app.assistant.orchestrator.memory_epoch import "
            "SqlitePermissionEpochStore\n"
            "print(SqlitePermissionEpochStore(r'%s').get_or_init('ana'))\n"
        ) % (Path.cwd(), self.ruta)
        out = subprocess.run([sys.executable, "-c", guion], capture_output=True,
                             text=True, timeout=180,
                             env={**os.environ, "ANDES_ASSISTANT_MEMORY_ENABLED": "1"})
        self.assertEqual(out.returncode, 0, out.stderr[-500:])
        self.assertEqual(int(out.stdout.strip().splitlines()[-1]), esperado)

    def test_varios_actores_estan_aislados(self):
        for _ in range(3):
            self.store.bump("ana")
        self.store.bump("beto")
        self.assertEqual(self.epoch("ana"), self.epoch("beto") + 2)
        self.assertNotEqual(self.epoch("ana"), self.epoch("carla"))

    def test_bumpear_a_uno_no_mueve_a_los_demas(self):
        base_beto = self.epoch("beto")
        self.store.bump("ana")
        self.assertEqual(self.epoch("beto"), base_beto)

    def test_dos_operaciones_concurrentes_no_se_pisan(self):
        """`BEGIN IMMEDIATE` serializa: N bumps son exactamente +N, nunca menos.
        Perder uno significaria dejar viva una autorizacion que debia morir."""
        inicial = self.epoch("ana")
        errores: list[Exception] = []

        def golpear():
            try:
                for _ in range(10):
                    self.store.bump("ana")
            except Exception as exc:  # noqa: BLE001
                errores.append(exc)

        hilos = [threading.Thread(target=golpear) for _ in range(4)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()
        self.assertEqual(errores, [])
        self.assertEqual(self.epoch("ana"), inicial + 40)

    def test_un_actor_vacio_no_bumpea_nada(self):
        for vacio in ("", "   ", None):
            with self.subTest(vacio=vacio):
                self.assertIsNone(bump_actor_permission_epoch(vacio))

    def test_la_puerta_publica_devuelve_el_epoch_nuevo(self):
        """FASE 10.3.1 — el valor existe para poder auditarlo."""
        antes = self.epoch("ana")
        self.assertEqual(notify_permission_context_changed("ana"), antes + 1)

    def test_la_puerta_publica_nunca_levanta(self):
        """Un fallo del asistente no puede tumbar un flujo de seguridad."""
        with patch("app.assistant.orchestrator.memory_epoch."
                   "bump_actor_permission_epoch", side_effect=RuntimeError("boom")):
            self.assertIsNone(notify_permission_context_changed("ana"))


# ───────────────────────────── el helper de seguridad

class ElHelperBumpeaYAuditaTests(_ConEpochTemporal):

    def _llamar(self, actor, **kw):
        import app.seguridad.routes as sr

        registrados: list[tuple] = []
        with patch.object(sr, "record_audit_event",
                          side_effect=lambda *a, **k: registrados.append((a, k))), \
             patch.object(sr, "session", {"user": "admin"}):
            sr._notify_assistant_permission_epoch(actor, **kw)  # noqa: SLF001
        return registrados

    def test_bumpea_y_deja_evento(self):
        antes = self.epoch("ana")
        eventos = self._llamar("ana", motivo="usuario_desactivado")
        self.assertEqual(self.epoch("ana"), antes + 1)
        self.assertEqual(len(eventos), 1)
        (accion, detalle), kwargs = eventos[0]
        self.assertEqual(accion, "assistant_permission_epoch_bump")
        self.assertEqual(detalle["actor_afectado"], "ana")
        self.assertEqual(detalle["motivo"], "usuario_desactivado")
        self.assertEqual(detalle["epoch_nuevo"], antes + 1)
        self.assertEqual(detalle["resultado"], "ok")
        self.assertEqual(kwargs["actor_usuario"], "admin")

    def test_un_bump_que_no_ocurre_tambien_deja_rastro(self):
        """La ventana entre commit y bump es el riesgo asumido; que un bump
        fallido se pierda en un warning lo volveria invisible."""
        import app.seguridad.routes as sr

        with patch("app.assistant.orchestrator.memory_epoch."
                   "bump_actor_permission_epoch", return_value=None):
            eventos = self._llamar("ana")
        self.assertEqual(eventos[0][0][1]["resultado"], "sin_efecto")
        self.assertIsNone(eventos[0][0][1]["epoch_nuevo"])

    def test_un_actor_vacio_no_hace_nada_ni_audita(self):
        for vacio in ("", None, "   "):
            with self.subTest(vacio=vacio):
                self.assertEqual(self._llamar(vacio), [])

    def test_nunca_levanta_aunque_falle_la_auditoria(self):
        import app.seguridad.routes as sr

        antes = self.epoch("ana")
        with patch.object(sr, "record_audit_event", side_effect=RuntimeError("x")), \
             patch.object(sr, "session", {"user": "admin"}):
            sr._notify_assistant_permission_epoch("ana")  # noqa: SLF001
        # El bump ocurrio igual: auditar es secundario al control.
        self.assertEqual(self.epoch("ana"), antes + 1)

    def test_el_evento_no_lleva_secretos(self):
        eventos = self._llamar("ana")
        texto = repr(eventos)
        for prohibido in ("password", "token", "hash", "secret"):
            self.assertNotIn(prohibido, texto.lower())


# ───────────────────────────── las rutas, con la capa de datos simulada

def _cuerpo(ruta):
    """La funcion sin decorar.

    `@admin_required` usa el `session` real de Flask y necesita contexto de
    peticion. Lo que esta unidad prueba es la logica de la ruta —a quien
    invalida y cuando—, no el decorador, que tiene su propio comportamiento y
    no cambio en 10.3.1. `functools.wraps` deja el original en `__wrapped__`.
    """
    return getattr(ruta, "__wrapped__", ruta)


class _Usuario:
    """Lo minimo que las rutas tocan. No es el modelo: es lo que usan."""

    def __init__(self, id=1, usuario="ana", activo=True,
                 bloqueado_seguridad=False, rol=None):
        self.id = id
        self.usuario = usuario
        self.activo = activo
        self.bloqueado_seguridad = bloqueado_seguridad
        self.bloqueado_at = None
        self.intentos_fallidos = 0
        self.rol = rol


class _FakeQuery:
    def __init__(self, user):
        self._user = user

    def get(self, _id):
        return self._user

    def get_or_404(self, _id):
        return self._user


class LasRutasQueMutanAutorizacionBumpeanTests(_ConEpochTemporal):
    """Comportamiento real de las rutas. La capa de datos se simula porque el
    URI de SQLAlchemy esta fijado a `data/andes.db` en `create_app()` y no hay
    variable de entorno para redirigirlo — limitacion documentada, no inventada.
    """

    def _ejecutar(self, nombre, user, **extra):
        import app.seguridad.routes as sr

        bumpeados: list[tuple[str, str]] = []

        class _Sesion:
            def __init__(self):
                self._d = {"user": "admin", "usuario_id": 99, "rol": "superadmin"}

            def get(self, k, d=None):
                return self._d.get(k, d)

        class _DB:
            class session:  # noqa: N801
                @staticmethod
                def commit():
                    pass

                @staticmethod
                def rollback():
                    pass

                @staticmethod
                def delete(_obj):
                    pass

        with patch.object(sr, "Usuario") as U, \
             patch.object(sr, "db", _DB), \
             patch.object(sr, "session", _Sesion()), \
             patch.object(sr, "has_permission", return_value=True), \
             patch.object(sr, "_purge_usuario_dependencies", lambda *_a: None), \
             patch.object(sr, "delete_user_photo_file", lambda *_a: None), \
             patch.object(sr, "_get_delete_context", lambda: (99, "admin", 2)), \
             patch.object(sr, "_can_delete_user", lambda *a, **k: (True, "")), \
             patch.object(
                 sr, "_notify_assistant_permission_epoch",
                 side_effect=lambda actor, *, motivo="permisos_actualizados":
                     bumpeados.append((actor, motivo))), \
             patch.object(sr, "redirect", lambda *_a, **_k: "redirect"),              patch.object(sr, "jsonify", lambda *a, **k: (a, k)):
            U.query = _FakeQuery(user)
            _cuerpo(getattr(sr, nombre))(user.id, **extra)
        return bumpeados

    # --- toggle: siempre es un cambio efectivo ---

    def test_toggle_api_desactivar_bumpea(self):
        u = _Usuario(usuario="ana", activo=True)
        self.assertEqual(self._ejecutar("api_toggle_usuario", u),
                         [("ana", "usuario_desactivado")])

    def test_toggle_api_activar_bumpea(self):
        u = _Usuario(usuario="ana", activo=False)
        self.assertEqual(self._ejecutar("api_toggle_usuario", u),
                         [("ana", "usuario_activado")])

    def test_toggle_formulario_bumpea_igual(self):
        u = _Usuario(usuario="ana", activo=True)
        self.assertEqual(self._ejecutar("toggle_usuario", u),
                         [("ana", "usuario_desactivado")])

    def test_el_actor_bumpeado_es_el_AFECTADO_no_el_administrador(self):
        """`admin` es quien opera; `ana` es quien pierde acceso."""
        u = _Usuario(usuario="ana", activo=True)
        actores = [a for a, _ in self._ejecutar("api_toggle_usuario", u)]
        self.assertEqual(actores, ["ana"])
        self.assertNotIn("admin", actores)

    # --- unlock: solo si devuelve acceso de verdad ---

    def test_unlock_que_desbloquea_bumpea(self):
        u = _Usuario(usuario="ana", activo=True, bloqueado_seguridad=True)
        self.assertEqual(self._ejecutar("api_unlock_usuario", u),
                         [("ana", "usuario_desbloqueado")])

    def test_unlock_que_reactiva_bumpea(self):
        u = _Usuario(usuario="ana", activo=False, bloqueado_seguridad=False)
        self.assertEqual(self._ejecutar("api_unlock_usuario", u),
                         [("ana", "usuario_desbloqueado")])

    def test_unlock_sobre_un_usuario_que_ya_estaba_bien_NO_bumpea(self):
        """Operacion sin cambio efectivo: reintentar no puede inventar bumps."""
        u = _Usuario(usuario="ana", activo=True, bloqueado_seguridad=False)
        self.assertEqual(self._ejecutar("api_unlock_usuario", u), [])

    def test_unlock_repetido_solo_bumpea_la_primera_vez(self):
        u = _Usuario(usuario="ana", activo=True, bloqueado_seguridad=True)
        primero = self._ejecutar("api_unlock_usuario", u)
        segundo = self._ejecutar("api_unlock_usuario", u)
        self.assertEqual(len(primero), 1)
        self.assertEqual(segundo, [])

    # --- borrado ---

    def test_eliminar_api_bumpea_con_el_nombre_capturado_antes(self):
        u = _Usuario(usuario="ana")
        self.assertEqual(self._ejecutar("api_eliminar_usuario", u),
                         [("ana", "usuario_eliminado")])

    def test_eliminar_formulario_bumpea_igual(self):
        u = _Usuario(usuario="ana")
        self.assertEqual(self._ejecutar("eliminar_usuario", u),
                         [("ana", "usuario_eliminado")])

    # --- el actor equivocado ---

    def test_un_usuario_que_no_existe_no_bumpea_a_nadie(self):
        import app.seguridad.routes as sr

        bumpeados = []

        class _Sesion(dict):
            def get(self, k, d=None):
                return {"user": "admin", "usuario_id": 99}.get(k, d)

        with patch.object(sr, "Usuario") as U, \
             patch.object(sr, "has_permission", return_value=True), \
             patch.object(sr, "session", _Sesion()), \
             patch.object(sr, "jsonify", lambda *a, **k: (a, k)),              patch.object(sr, "_notify_assistant_permission_epoch",
                          side_effect=lambda *a, **k: bumpeados.append(a)):
            U.query = _FakeQuery(None)
            _cuerpo(sr.api_toggle_usuario)(123)
        self.assertEqual(bumpeados, [])


# ───────────────────────────── el cableado, por AST

class NingunaMutacionSeQuedoSinHookTests(unittest.TestCase):
    """Por AST y no por grep: el nombre del helper aparece tambien en su propia
    definicion y en comentarios."""

    def setUp(self):
        self.arbol = ast.parse(RUTAS.read_text(encoding="utf-8"))
        self.funciones = {n.name: n for n in ast.walk(self.arbol)
                          if isinstance(n, ast.FunctionDef)}

    def _llama_al_hook(self, nombre: str) -> bool:
        cuerpo = self.funciones[nombre]
        for nodo in ast.walk(cuerpo):
            if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name) \
                    and nodo.func.id == "_notify_assistant_permission_epoch":
                return True
        return False

    def test_todas_las_mutaciones_de_autorizacion_tienen_hook(self):
        for ruta in ("api_crear_usuario", "api_editar_usuario",
                     "api_toggle_usuario", "api_unlock_usuario",
                     "api_eliminar_usuario", "nuevo_usuario",
                     "editar_usuario", "toggle_usuario", "eliminar_usuario"):
            with self.subTest(ruta=ruta):
                self.assertTrue(self._llama_al_hook(ruta),
                                f"{ruta} muta autorizacion y no invalida nada")

    def test_el_login_NO_bumpea(self):
        """Iniciar sesion no cambia ninguna autorizacion. Si bumpeara, cada
        login invalidaria la memoria contextual del usuario."""
        for ruta in ("login", "logout"):
            if ruta in self.funciones:
                with self.subTest(ruta=ruta):
                    self.assertFalse(self._llama_al_hook(ruta))

    def test_ninguna_ruta_de_solo_lectura_bumpea(self):
        for ruta in ("usuarios", "api_usuarios", "api_roles",
                     "api_obtener_usuario"):
            if ruta in self.funciones:
                with self.subTest(ruta=ruta):
                    self.assertFalse(self._llama_al_hook(ruta))

    def test_el_bump_va_DESPUES_del_commit(self):
        """Antes del commit, la segunda conexion esperaria el lock de escritura
        de SQLAlchemy hasta agotar su timeout y fallaria en silencio."""
        fuente = RUTAS.read_text(encoding="utf-8")
        for nombre in ("api_toggle_usuario", "api_unlock_usuario",
                       "toggle_usuario"):
            with self.subTest(ruta=nombre):
                inicio = fuente.index(f"def {nombre}(")
                cuerpo = fuente[inicio:inicio + 2600]
                i_commit = cuerpo.index("db.session.commit()")
                i_bump = cuerpo.index("_notify_assistant_permission_epoch")
                self.assertLess(i_commit, i_bump)

    def test_el_nombre_a_invalidar_se_captura_antes_de_borrar(self):
        """Despues del delete el objeto ya no sirve para saber a quien tocaba."""
        fuente = RUTAS.read_text(encoding="utf-8")
        for nombre in ("api_eliminar_usuario", "eliminar_usuario"):
            with self.subTest(ruta=nombre):
                inicio = fuente.index(f"def {nombre}(")
                cuerpo = fuente[inicio:inicio + 2600]
                self.assertLess(cuerpo.index("nombre_borrado ="),
                                cuerpo.index("db.session.delete"))

    def test_editar_por_API_marca_el_cambio_de_activo(self):
        """El hueco que 10.3.1 cerro: por esa via solo rol y permisos marcaban
        el flag, asi que desactivar a alguien no invalidaba nada."""
        fuente = RUTAS.read_text(encoding="utf-8")
        inicio = fuente.index("def api_editar_usuario(")
        cuerpo = fuente[inicio:inicio + 9000]
        self.assertIn("prev_activo", cuerpo)
        self.assertIn("if bool(user.activo) != prev_activo:", cuerpo)


class LoQueNoTieneRutaEjecutableTests(unittest.TestCase):
    """Limitaciones reales, documentadas en vez de inventadas."""

    def test_no_hay_ruta_para_editar_o_borrar_un_ROL(self):
        """Los permisos se asignan por usuario. Un rol solo se lee (`api_roles`)
        y se siembra en `init_roles`; no hay endpoint que cambie lo que un rol
        concede, asi que no hay nada que cablear ahi todavia.

        Cuando exista, tendra que bumpear a TODOS los usuarios de ese rol, y eso
        es una unidad propia: un bump masivo no es el mismo problema que uno.
        """
        fuente = RUTAS.read_text(encoding="utf-8")
        arbol = ast.parse(fuente)
        nombres = {n.name for n in ast.walk(arbol) if isinstance(n, ast.FunctionDef)}
        for inexistente in ("api_editar_rol", "api_crear_rol", "api_eliminar_rol",
                            "api_rol_permisos"):
            self.assertNotIn(inexistente, nombres)

    def test_el_URI_de_la_base_no_es_configurable_por_entorno(self):
        """Por eso las pruebas de ruta simulan la capa de datos: `create_app()`
        fija `sqlite:///data/andes.db` sin variable que lo redirija, y montar la
        base real en un test la tocaria de verdad."""
        init = Path("app/__init__.py").read_text(encoding="utf-8")
        i = init.index('app.config["SQLALCHEMY_DATABASE_URI"]')
        linea = init[i:init.index("\n", i)]
        self.assertIn("DB_PATH", linea)
        self.assertNotIn("environ", linea)


if __name__ == "__main__":
    unittest.main()
