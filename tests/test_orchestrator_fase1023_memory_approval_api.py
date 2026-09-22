"""FASE 10.2.3 — la superficie HTTP de approve/reject.

DOS RUTAS CON NOMBRE PROPIO, y no un campo mas en el PUT.

`PUT /api/memory/<id>` significa "corrige el contenido". Aprobar es otro acto.
Si fueran lo mismo, una correccion de texto podria aprobar de paso — que es
exactamente lo que el store impide adentro, donde actualizar un valor no
reetiqueta el estado. Aqui se verifica que la separacion tambien exista en la
API, no solo en el store.

LO QUE EL CLIENTE NO PUEDE DECIDIR

El actor lo pone la sesion. El `source` lo pone el servidor: esta es LA ruta
humana autorizada, y el cliente no puede declararse otra cosa. La `version` es
lo unico que el cliente aporta, y es justamente la prueba de que esta aprobando
la memoria que vio.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from flask import Blueprint, Flask

from app.assistant.orchestrator.memory_schema import memory_version
from app.assistant.routes import assistant_bp
from app.utils.csrf import CSRF_SESSION_KEY


class _ApiBase(unittest.TestCase):
    def setUp(self):
        self._prev = {
            k: os.environ.get(k)
            for k in ("ANDES_ASSISTANT_MEMORY_ENABLED",
                      "ANDES_ASSISTANT_MEMORY_APPROVAL",
                      "ANDES_ASSISTANT_MEMORY_DB",
                      "ANDES_ORCH_AUDIT_PATH")
        }
        self._dir = tempfile.TemporaryDirectory()
        ruta = Path(self._dir.name) / "mem.db"
        self.auditoria = Path(self._dir.name) / "audit.jsonl"
        os.environ["ANDES_ASSISTANT_MEMORY_ENABLED"] = "1"
        os.environ["ANDES_ASSISTANT_MEMORY_APPROVAL"] = "1"
        os.environ["ANDES_ASSISTANT_MEMORY_DB"] = str(ruta)
        os.environ["ANDES_ORCH_AUDIT_PATH"] = str(self.auditoria)

        from app.assistant.orchestrator.memory_store import (
            MemoryStore,
            reset_default_memory_store_for_tests,
        )

        # El store del servicio es un singleton que se crea una vez y ya no
        # vuelve a leer la ruta. Sin este reset, las rutas hablarian con la base
        # de otro test. Lo aprendimos midiendo 10.2.2.
        reset_default_memory_store_for_tests()
        self.s = MemoryStore(path=ruta)
        self.s.ensure_schema()

        app = Flask(__name__)
        app.config["TESTING"] = True
        app.config["SECRET_KEY"] = "test"
        app.register_blueprint(assistant_bp)
        # `login_required` redirige a `auth.login` cuando no hay sesion —es el
        # comportamiento de todo el ERP, no de estas rutas—, asi que el destino
        # tiene que existir para que `url_for` pueda construirlo.
        auth = Blueprint("auth", __name__)
        auth.add_url_rule("/login", "login", lambda: "login")
        app.register_blueprint(auth)
        self.client = app.test_client()

    def tearDown(self):
        from app.assistant.orchestrator.memory_store import (
            reset_default_memory_store_for_tests,
        )

        reset_default_memory_store_for_tests()
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._dir.cleanup()

    def _login(self, user="albertadmin"):
        with self.client.session_transaction() as sess:
            sess["user"] = user
            sess[CSRF_SESSION_KEY] = "csrf-test"

    def _mem(self, status="suggested", actor="albertadmin", key="estilo", **kw):
        base = dict(actor_user=actor, scope="user", memory_type="preference",
                    key=key, value={"answer_style": "brief"}, status=status,
                    source="derived", verify_conversation=False)
        base.update(kw)
        fila = self.s.upsert(**base)
        self.assertIsNotNone(fila, f"no se pudo sembrar: {self.s.last_error}")
        return fila

    def _post(self, slot_id, op, **cuerpo):
        return self.client.post(f"/assistant/api/memory/{slot_id}/{op}",
                                json=cuerpo)

    def _auditoria(self):
        if not self.auditoria.exists():
            return []
        return [json.loads(l) for l in
                self.auditoria.read_text(encoding="utf-8").splitlines() if l.strip()]


class LasRutasHacenLoQueDicenTests(_ApiBase):

    def test_approve_devuelve_el_estado_nuevo_y_la_version_nueva(self):
        self._login()
        m = self._mem("suggested")
        r = self._post(m["id"], "approve", version=memory_version(m))
        self.assertEqual(r.status_code, 200)
        cuerpo = r.get_json()
        self.assertTrue(cuerpo["ok"])
        self.assertTrue(cuerpo["changed"])
        self.assertEqual(cuerpo["previous_status"], "suggested")
        self.assertEqual(cuerpo["item"]["status"], "approved")
        self.assertNotEqual(cuerpo["item"]["version"], memory_version(m))

    def test_reject_igual(self):
        self._login()
        m = self._mem("suggested")
        cuerpo = self._post(m["id"], "reject",
                            version=memory_version(m)).get_json()
        self.assertEqual(cuerpo["item"]["status"], "rejected")

    def test_la_respuesta_NO_lleva_el_valor_de_la_memoria(self):
        self._login()
        m = self._mem("suggested", value={"answer_style": "operational"})
        texto = self._post(m["id"], "approve",
                           version=memory_version(m)).get_data(as_text=True)
        self.assertNotIn("operational", texto)
        self.assertNotIn("answer_style", texto)

    def test_el_listado_entrega_la_version_para_poder_aprobar(self):
        """Sin esto el panel no tendria como demostrar que memoria vio."""
        self._login()
        m = self._mem("suggested")
        items = self.client.get("/assistant/api/memory").get_json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["version"], memory_version(m))
        self.assertEqual(items[0]["status"], "suggested")
        # Y esa version, tal cual, sirve para aprobar.
        self.assertEqual(
            self._post(m["id"], "approve", version=items[0]["version"]).status_code,
            200)

    def test_el_correlation_id_vuelve_en_la_respuesta(self):
        self._login()
        m = self._mem("suggested")
        cuerpo = self._post(m["id"], "approve", version=memory_version(m),
                            correlation_id="corr-abc").get_json()
        self.assertEqual(cuerpo["correlation_id"], "corr-abc")

    def test_sin_correlation_id_el_servidor_pone_uno(self):
        self._login()
        m = self._mem("suggested")
        cuerpo = self._post(m["id"], "approve",
                            version=memory_version(m)).get_json()
        self.assertTrue(cuerpo["correlation_id"])


class LosCodigosHttpDicenQueHacerTests(_ApiBase):

    def test_sin_sesion_no_se_aprueba(self):
        """302, no 401: `login_required` redirige, como en todo el ERP. Lo que
        importa aqui es que la memoria no se movio."""
        m = self._mem("suggested")
        r = self._post(m["id"], "approve", version=memory_version(m))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.s.get_slot("albertadmin", m["id"])["status"],
                         "suggested")

    def test_sesion_con_usuario_vacio_401(self):
        """La segunda guarda, la de la propia ruta."""
        with self.client.session_transaction() as sess:
            sess["user"] = "   "
        m = self._mem("suggested")
        r = self._post(m["id"], "approve", version=memory_version(m))
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.get_json()["error_code"], "unauthorized")

    def test_sin_version_400(self):
        self._login()
        m = self._mem("suggested")
        r = self._post(m["id"], "approve")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error_code"], "version_required")

    def test_version_vieja_409(self):
        self._login()
        m = self._mem("suggested")
        vieja = memory_version(m)
        self._post(m["id"], "approve", version=vieja)
        r = self._post(m["id"], "reject", version=vieja)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["error_code"], "version_conflict")

    def test_memoria_ajena_404(self):
        self._login("albertadmin")
        m = self._mem("suggested", actor="otro")
        r = self._post(m["id"], "approve", version=memory_version(m))
        self.assertEqual(r.status_code, 404)

    def test_inexistente_404(self):
        self._login()
        self.assertEqual(
            self._post("no-existe", "approve", version="x").status_code, 404)

    def test_expired_409_con_explicacion(self):
        self._login()
        m = self._mem("expired")
        r = self._post(m["id"], "approve", version=memory_version(m))
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["error_code"], "expired_terminal")

    def test_rejected_409_y_dice_el_estado_actual(self):
        self._login()
        m = self._mem("rejected")
        r = self._post(m["id"], "approve", version=memory_version(m))
        self.assertEqual(r.status_code, 409)
        cuerpo = r.get_json()
        self.assertEqual(cuerpo["error_code"], "rejected_requires_reconsider")
        self.assertEqual(cuerpo["status_actual"], "rejected")

    def test_rejected_con_reconsider_200(self):
        self._login()
        m = self._mem("rejected")
        r = self._post(m["id"], "approve", version=memory_version(m),
                       reconsider=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["item"]["status"], "approved")

    def test_approved_con_revoke_200(self):
        self._login()
        m = self._mem("approved")
        r = self._post(m["id"], "reject", version=memory_version(m), revoke=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["item"]["status"], "rejected")

    def test_memoria_apagada_404(self):
        self._login()
        m = self._mem("suggested")
        v = memory_version(m)
        os.environ["ANDES_ASSISTANT_MEMORY_ENABLED"] = "0"
        r = self._post(m["id"], "approve", version=v)
        self.assertEqual(r.status_code, 404)


class ElClienteNoDecideQuienNiDesdeDondeTests(_ApiBase):

    def test_no_puede_declarar_el_actor(self):
        self._login("albertadmin")
        m = self._mem("suggested", actor="otro")
        r = self._post(m["id"], "approve", version=memory_version(m),
                       actor_user="otro")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error_code"], "forbidden_field")

    def test_no_puede_declarar_el_origen(self):
        """Si el cliente pudiera poner `source`, la lista cerrada del motor no
        serviria de nada."""
        self._login()
        m = self._mem("suggested")
        r = self._post(m["id"], "approve", version=memory_version(m),
                       source="api")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error_code"], "forbidden_field")

    def test_no_puede_fijar_el_estado_a_mano(self):
        self._login()
        m = self._mem("suggested")
        for campo in ("status", "status_by", "permission_epoch", "sensitivity"):
            with self.subTest(campo=campo):
                r = self._post(m["id"], "approve", version=memory_version(m),
                               **{campo: "x"})
                self.assertEqual(r.get_json()["error_code"], "forbidden_field")

    def test_status_by_sale_de_la_sesion(self):
        self._login("albertadmin")
        m = self._mem("suggested")
        self._post(m["id"], "approve", version=memory_version(m))
        self.assertEqual(
            self.s.get_slot("albertadmin", m["id"])["status_by"], "albertadmin")


class ElPutNoAprueba(_ApiBase):
    """La separacion entre corregir contenido y aprobar, verificada en la API."""

    def test_un_PUT_con_status_no_cambia_el_estado(self):
        self._login()
        m = self._mem("suggested")
        r = self.client.put(f"/assistant/api/memory/{m['id']}",
                            json={"value": {"answer_style": "detailed"}})
        self.assertEqual(r.status_code, 200)
        fresca = self.s.get_slot("albertadmin", m["id"])
        self.assertEqual(fresca["status"], "suggested")
        self.assertEqual(fresca["value"]["answer_style"], "detailed")

    def test_y_deja_la_version_invalidada(self):
        """Corregir el contenido invalida el testigo: aprobar despues exige
        volver a leer, porque ya no es la memoria que se vio."""
        self._login()
        m = self._mem("suggested")
        vieja = memory_version(m)
        self.client.put(f"/assistant/api/memory/{m['id']}",
                        json={"value": {"answer_style": "detailed"}})
        r = self._post(m["id"], "approve", version=vieja)
        self.assertEqual(r.status_code, 409)


class LaAuditoriaHttpTests(_ApiBase):

    def test_cada_decision_deja_registro(self):
        self._login()
        m = self._mem("suggested")
        self._post(m["id"], "approve", version=memory_version(m),
                   reason="la uso todos los dias", correlation_id="corr-1")
        registros = [r for r in self._auditoria()
                     if r.get("event") == "assistant_memory_status_change"]
        self.assertEqual(len(registros), 1)
        r = registros[0]
        self.assertEqual(r["actor_user"], "albertadmin")
        self.assertEqual(r["source"], "ui")
        self.assertEqual(r["correlation_id"], "corr-1")
        self.assertEqual(r["result"], "ok")

    def test_un_intento_bloqueado_tambien_deja_registro(self):
        self._login()
        m = self._mem("expired")
        self._post(m["id"], "approve", version=memory_version(m))
        registros = self._auditoria()
        self.assertEqual(len(registros), 1)
        self.assertEqual(registros[0]["result"], "expired_terminal")


if __name__ == "__main__":
    unittest.main()
