"""FASE 10.2.4 — el panel de memoria.

LA PREGUNTA QUE EL PANEL EXISTE PARA CONTESTAR

    "¿Que va a recordar Andes si apruebo esto?"

Y la propiedad que hace que se pueda contestar sin riesgo: una memoria es
CONTENIDO NO CONFIABLE. Sale de lo que el usuario escribio y de lo que el
sistema dedujo, y la pantalla donde se revisa es, por definicion, la pantalla
donde ese contenido se mira de cerca. Si pudiera escribir markup, seria la
pantalla mas facil de atacar del asistente.

Por eso aqui se verifican tres cosas distintas:

  1. LA PROYECCION — que sale del servidor. Campo por campo y por tipo; un tipo
     no declarado no vuelca su valor. Sin `actor_user` ni `permission_epoch`.
  2. LA RUTA — quien puede pedirla, que devuelve, y que el CSRF la cubre.
  3. EL CLIENTE — que el renderizador no pueda escribir markup ni mandar los
     campos que decide el servidor.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from flask import Blueprint, Flask, jsonify, request

from app.assistant.orchestrator.memory_panel import (
    ORDEN_ESTADOS,
    estado_efectivo,
    panel_item,
    panel_payload,
)
from app.assistant.orchestrator.memory_schema import memory_version
from app.assistant.routes import assistant_bp
from app.utils.csrf import CSRF_SESSION_KEY, validate_csrf_request

PASADO = "2020-01-01T00:00:00Z"
FUTURO = "2099-01-01T00:00:00Z"


def _slot(**kw):
    base = dict(id="m1", actor_user="ana", scope="user", conversation_id=None,
                memory_type="preference", key="estilo",
                value={"answer_style": "brief"}, confidence=0.9,
                source="derived", permission_epoch=0, sensitivity="benign",
                status="suggested", status_changed_at=None, status_by=None,
                created_at="2026-09-01T10:00:00Z",
                updated_at="2026-09-01T10:00:00Z",
                expires_at=None, source_turn_id="t-1", meta={})
    base.update(kw)
    return base


# ───────────────────────────── 1. la proyeccion

class LaTarjetaExplicaQueSeVaARecordarTests(unittest.TestCase):

    def test_una_preferencia_se_lee_en_castellano(self):
        it = panel_item(_slot())
        self.assertEqual(it["title"], "Estilo de respuesta")
        self.assertEqual(it["fields"], [{"name": "Preferencia",
                                         "value": "Respuestas breves"}])

    def test_dice_por_que_fue_sugerida(self):
        self.assertIn("dedujo", panel_item(_slot(source="derived"))["why"])
        self.assertIn("pediste", panel_item(_slot(source="explicit"))["why"])
        self.assertIn("interfaz", panel_item(_slot(source="ui"))["why"])

    def test_una_entidad_frecuente_dice_cuantas_veces(self):
        it = panel_item(_slot(memory_type="frequent_entity",
                              value={"kind": "codigo", "value": "2404",
                                     "hit_count": 7}))
        self.assertEqual(it["fields"],
                         [{"name": "Código", "value": "2404"},
                          {"name": "Veces consultada", "value": "7"}])

    def test_una_ui_pref_no_muestra_un_booleano_crudo(self):
        self.assertEqual(panel_item(_slot(memory_type="ui_pref",
                                          value={"compact": True}))["fields"],
                         [{"name": "Vista compacta", "value": "Activada"}])

    def test_un_resumen_muestra_su_texto(self):
        it = panel_item(_slot(memory_type="conversation_summary",
                              value={"text": "Revisó stock de frenos",
                                     "tools": ["get_inventory"]}))
        self.assertEqual(it["fields"][0]["value"], "Revisó stock de frenos")

    def test_la_procedencia_viaja_hasta_la_tarjeta(self):
        it = panel_item(_slot(scope="conversation", conversation_id="c-7",
                              source_turn_id="turno-3"))
        self.assertEqual(it["scope"], "conversation")
        self.assertEqual(it["conversation_id"], "c-7")
        self.assertEqual(it["source_turn_id"], "turno-3")


class LaProyeccionFallaCerradaTests(unittest.TestCase):

    def test_un_tipo_desconocido_NO_vuelca_su_valor(self):
        """Agregar un tipo sin tocar el panel lo deja invisible, no filtrado."""
        it = panel_item(_slot(memory_type="tipo_del_futuro",
                              value={"secreto_operativo": "no mostrar"}))
        self.assertEqual(it["fields"], [])
        self.assertIn("no reconocido", " ".join(it["warnings"]))
        self.assertNotIn("no mostrar", json.dumps(it, ensure_ascii=False))

    def test_la_tarjeta_NO_lleva_actor_ni_epoch(self):
        it = panel_item(_slot())
        self.assertNotIn("actor_user", it)
        self.assertNotIn("permission_epoch", it)
        self.assertNotIn("meta", it)

    def test_nunca_viaja_el_valor_crudo(self):
        it = panel_item(_slot())
        self.assertNotIn("value", it)
        self.assertNotIn("value_json", it)

    def test_un_valor_con_contenido_prohibido_no_se_muestra_y_se_avisa(self):
        """Defensa en profundidad: aunque una fila vieja lo traiga, el panel no
        lo enseña Y dice que aprobarla no serviría de nada."""
        it = panel_item(_slot(memory_type="conversation_summary",
                              value={"text": "token Bearer abc123", "tools": []}))
        self.assertTrue(it["blocked"])
        self.assertEqual(it["fields"], [])
        self.assertNotIn("abc123", json.dumps(it, ensure_ascii=False))
        self.assertIn("no puede mostrar", " ".join(it["warnings"]))

    def test_una_contextual_avisa_de_que_depende_de_permisos(self):
        it = panel_item(_slot(sensitivity="contextual"))
        self.assertIn("permisos", " ".join(it["warnings"]))


class ElEstadoQueSeMuestraEsElEfectivoTests(unittest.TestCase):
    """El TTL corre ANTES que la puerta de estado. Mostrar `approved` sobre una
    memoria caducada seria mentir sobre lo que el sistema hace."""

    def test_una_approved_caducada_se_muestra_como_expirada(self):
        it = panel_item(_slot(status="approved", expires_at=PASADO))
        self.assertEqual(it["status"], "expired")
        self.assertEqual(it["stored_status"], "approved")
        self.assertIn("Caducó", " ".join(it["warnings"]))

    def test_una_approved_vigente_sigue_aprobada(self):
        self.assertEqual(
            panel_item(_slot(status="approved", expires_at=FUTURO))["status"],
            "approved")

    def test_estado_efectivo_es_una_funcion_publica(self):
        self.assertEqual(estado_efectivo(_slot(status="rejected")), "rejected")
        self.assertEqual(
            estado_efectivo(_slot(status="rejected", expires_at=PASADO)), "expired")

    def test_un_estado_invalido_cae_al_default(self):
        self.assertEqual(estado_efectivo(_slot(status="inventado")), "approved")


class LaBandejaOrdenaYCuentaTests(unittest.TestCase):

    def _bandeja(self):
        return panel_payload([
            _slot(id="a", status="approved"),
            _slot(id="b", status="suggested", created_at="2026-09-02T10:00:00Z"),
            _slot(id="c", status="rejected"),
            _slot(id="d", status="approved", expires_at=PASADO),
            _slot(id="e", status="suggested", created_at="2026-09-05T10:00:00Z"),
        ])

    def test_primero_lo_que_espera_decision(self):
        orden = [i["status"] for i in self._bandeja()["items"]]
        self.assertEqual(orden[:2], ["suggested", "suggested"])
        self.assertEqual(orden[-1], "expired")

    def test_dentro_de_un_estado_lo_mas_reciente_arriba(self):
        ids = [i["id"] for i in self._bandeja()["items"] if i["status"] == "suggested"]
        self.assertEqual(ids, ["e", "b"])

    def test_los_recuentos_usan_el_estado_efectivo(self):
        self.assertEqual(self._bandeja()["counts"],
                         {"suggested": 2, "approved": 1, "rejected": 1, "expired": 1})

    def test_el_orden_de_las_pestanas_lo_fija_el_servidor(self):
        self.assertEqual(self._bandeja()["order"], list(ORDEN_ESTADOS))
        self.assertEqual(ORDEN_ESTADOS,
                         ("suggested", "approved", "rejected", "expired"))


# ───────────────────────────── 2. la ruta

class _ApiBase(unittest.TestCase):
    CSRF = False

    def setUp(self):
        self._prev = {
            k: os.environ.get(k)
            for k in ("ANDES_ASSISTANT_MEMORY_ENABLED",
                      "ANDES_ASSISTANT_MEMORY_APPROVAL",
                      "ANDES_ASSISTANT_MEMORY_DB", "ANDES_ORCH_AUDIT_PATH")
        }
        self._dir = tempfile.TemporaryDirectory()
        ruta = Path(self._dir.name) / "mem.db"
        os.environ["ANDES_ASSISTANT_MEMORY_ENABLED"] = "1"
        os.environ["ANDES_ASSISTANT_MEMORY_APPROVAL"] = "1"
        os.environ["ANDES_ASSISTANT_MEMORY_DB"] = str(ruta)
        os.environ["ANDES_ORCH_AUDIT_PATH"] = str(Path(self._dir.name) / "a.jsonl")

        from app.assistant.orchestrator.memory_store import (
            MemoryStore,
            reset_default_memory_store_for_tests,
        )

        reset_default_memory_store_for_tests()
        self.s = MemoryStore(path=ruta)
        self.s.ensure_schema()

        app = Flask(__name__)
        app.config["TESTING"] = True
        app.config["SECRET_KEY"] = "test"
        app.register_blueprint(assistant_bp)
        auth = Blueprint("auth", __name__)
        auth.add_url_rule("/login", "login", lambda: "login")
        app.register_blueprint(auth)
        if self.CSRF:
            # El mismo guard global de app/__init__.py. Se replica aqui porque
            # la propiedad que hay que probar es que /assistant/api/ NO esta
            # exento, no que Flask sepa validar un token.
            @app.before_request
            def _csrf():
                if request.method in {"GET", "HEAD", "OPTIONS"}:
                    return None
                if not validate_csrf_request():
                    return jsonify(success=False,
                                   message="Token CSRF inválido o ausente"), 400
                return None
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

    def _login(self, user="albertadmin", csrf="csrf-test"):
        with self.client.session_transaction() as sess:
            sess["user"] = user
            sess[CSRF_SESSION_KEY] = csrf

    def _mem(self, status="suggested", actor="albertadmin", key="estilo", **kw):
        base = dict(actor_user=actor, scope="user", memory_type="preference",
                    key=key, value={"answer_style": "brief"}, status=status,
                    source="derived", verify_conversation=False)
        base.update(kw)
        fila = self.s.upsert(**base)
        self.assertIsNotNone(fila, f"no se pudo sembrar: {self.s.last_error}")
        return fila

    def _panel(self, **params):
        q = "&".join(f"{k}={v}" for k, v in params.items())
        return self.client.get("/assistant/api/memory/panel" + (f"?{q}" if q else ""))


class LaRutaDelPanelTests(_ApiBase):

    def test_devuelve_tarjetas_y_recuentos(self):
        self._login()
        self._mem("suggested", key="a")
        self._mem("approved", key="b")
        cuerpo = self._panel().get_json()
        self.assertTrue(cuerpo["ok"])
        self.assertEqual(len(cuerpo["items"]), 2)
        self.assertEqual(cuerpo["counts"]["suggested"], 1)
        self.assertEqual(cuerpo["counts"]["approved"], 1)

    def test_incluye_las_caducadas_que_el_listado_normal_oculta(self):
        """Sin esto la pestaña Expiradas no tendria nada que mostrar."""
        self._login()
        self._mem("approved", key="vieja", expires_at=PASADO)
        panel = self._panel().get_json()
        self.assertEqual(panel["counts"]["expired"], 1)
        # El listado de 7B.3 sigue ocultandolas: no se cambio su contrato.
        normal = self.client.get("/assistant/api/memory").get_json()
        self.assertEqual(normal["items"], [])

    def test_filtra_por_estado(self):
        self._login()
        self._mem("suggested", key="a")
        self._mem("rejected", key="b")
        cuerpo = self._panel(status="suggested").get_json()
        self.assertEqual(len(cuerpo["items"]), 1)
        self.assertEqual(cuerpo["items"][0]["status"], "suggested")
        # Los recuentos siguen siendo los de TODO, no los del filtro.
        self.assertEqual(cuerpo["counts"]["rejected"], 1)

    def test_un_estado_inventado_no_filtra_nada(self):
        self._login()
        self._mem("suggested")
        self.assertEqual(len(self._panel(status="inventado").get_json()["items"]), 1)

    def test_busca_sobre_lo_proyectado(self):
        self._login()
        self._mem("suggested", key="a", value={"answer_style": "brief"})
        self._mem("suggested", key="b", memory_type="frequent_entity",
                  value={"kind": "codigo", "value": "2404", "hit_count": 1})
        self.assertEqual(len(self._panel(q="2404").get_json()["items"]), 1)
        self.assertEqual(len(self._panel(q="breves").get_json()["items"]), 1)

    def test_la_busqueda_NO_alcanza_lo_que_el_panel_decidio_no_mostrar(self):
        """Buscar no puede ser una via para sacar a la luz un campo oculto."""
        self._login()
        self._mem("suggested", key="secreto_en_la_clave")
        self.assertEqual(self._panel(q="secreto").get_json()["items"], [])

    def test_la_version_viene_lista_para_aprobar(self):
        self._login()
        m = self._mem("suggested")
        item = self._panel().get_json()["items"][0]
        self.assertEqual(item["version"], memory_version(m))
        r = self.client.post(f"/assistant/api/memory/{m['id']}/approve",
                             json={"version": item["version"]})
        self.assertEqual(r.status_code, 200)

    def test_memoria_apagada_404(self):
        self._login()
        os.environ["ANDES_ASSISTANT_MEMORY_ENABLED"] = "0"
        self.assertEqual(self._panel().status_code, 404)


class ElPanelNoCruzaActorNiConversacionTests(_ApiBase):

    def test_solo_ve_lo_propio(self):
        self._login("albertadmin")
        self._mem("suggested", actor="otro", key="ajena")
        self.assertEqual(self._panel().get_json()["items"], [])

    def test_sin_sesion_no_hay_panel(self):
        self.assertEqual(self._panel().status_code, 302)

    def test_sesion_con_usuario_vacio_401(self):
        with self.client.session_transaction() as sess:
            sess["user"] = "   "
        self.assertEqual(self._panel().status_code, 401)

    def test_una_memoria_de_conversacion_solo_se_modera_desde_la_suya(self):
        self._login()
        m = self._mem("suggested", scope="conversation", conversation_id="c-1")
        r = self.client.post(f"/assistant/api/memory/{m['id']}/approve",
                             json={"version": memory_version(m),
                                   "conversation_id": "c-2"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.get_json()["error_code"], "scope_mismatch")

    def test_el_scope_company_no_existe_todavia(self):
        """`company` es 10.2.7. Hoy el esquema lo rechaza al escribir, asi que
        no hay forma de que una memoria de empresa llegue al panel."""
        from app.assistant.orchestrator.memory_schema import SCOPES

        self.assertEqual(SCOPES, {"user", "conversation"})
        self.assertIsNone(self.s.upsert(
            actor_user="albertadmin", scope="company", memory_type="preference",
            key="k", value={"answer_style": "brief"}, verify_conversation=False))
        self.assertIn("invalid_scope", self.s.last_error or "")


class LaBanderaDecideSiHayAlgoQueAprobarTests(_ApiBase):
    """OFF vs ON, visto desde el panel.

    El panel NO depende de `ANDES_ASSISTANT_MEMORY_APPROVAL`: listar, aprobar y
    rechazar funcionan en los dos brazos. Lo que la bandera decide es si hay
    algo pendiente: con OFF lo derivado nace `approved` y la bandeja de
    Pendientes esta vacia; con ON nace `suggested` y aparece ahi.

    Por eso el panel puede entregarse con la bandera apagada: no cambia nada
    hasta que se enciende, y cuando se encienda ya habra donde aprobar.
    """

    def _con(self, valor):
        os.environ["ANDES_ASSISTANT_MEMORY_APPROVAL"] = valor
        import importlib

        import app.assistant.orchestrator.memory_config as mc
        importlib.reload(mc)

    def test_OFF_no_deja_nada_pendiente(self):
        self._login()
        self._con("0")
        from app.assistant.orchestrator.memory_config import memory_approval_enabled

        self.assertFalse(memory_approval_enabled())
        # Lo que escribiria el escritor derivado con la bandera apagada.
        self._mem(status=None, key="derivada", source="derived")
        cuerpo = self._panel().get_json()
        self.assertEqual(cuerpo["counts"]["suggested"], 0)
        self.assertEqual(cuerpo["counts"]["approved"], 1)

    def test_ON_deja_la_derivada_esperando(self):
        self._login()
        self._con("1")
        self._mem(status="suggested", key="derivada", source="derived")
        cuerpo = self._panel().get_json()
        self.assertEqual(cuerpo["counts"]["suggested"], 1)
        self.assertEqual(cuerpo["counts"]["approved"], 0)

    def test_el_panel_funciona_en_LOS_DOS_brazos(self):
        """Aprobar no depende de la bandera: la puerta del selector si, el acto
        de aprobar no. Asi el panel puede entregarse antes de encenderla."""
        self._login()
        for valor in ("0", "1"):
            with self.subTest(bandera=valor):
                self._con(valor)
                m = self._mem(status="suggested", key=f"k{valor}")
                r = self.client.post(
                    f"/assistant/api/memory/{m['id']}/approve",
                    json={"version": memory_version(m)})
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.get_json()["item"]["status"], "approved")

    def test_con_OFF_el_selector_sigue_sin_filtrar(self):
        """La propiedad de 10.2.2 que no puede haberse movido."""
        from app.assistant.orchestrator.memory_selector import select_memory_hints

        self._con("0")
        self._mem(status="suggested", key="pendiente")
        sel = select_memory_hints(actor_user="albertadmin",
                                  conversation_id="c-1", store=self.s)
        self.assertEqual(sel.selected_count, 1)
        self.assertFalse(sel.approval_enforced)


class ElCsrfCubreLaModeracionTests(_ApiBase):
    CSRF = True

    def test_sin_token_no_se_aprueba(self):
        self._login()
        m = self._mem("suggested")
        r = self.client.post(f"/assistant/api/memory/{m['id']}/approve",
                             json={"version": memory_version(m)})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.s.get_slot("albertadmin", m["id"])["status"],
                         "suggested")

    def test_con_token_equivocado_tampoco(self):
        self._login(csrf="el-bueno")
        m = self._mem("suggested")
        r = self.client.post(f"/assistant/api/memory/{m['id']}/approve",
                             json={"version": memory_version(m)},
                             headers={"X-CSRF-Token": "el-malo"})
        self.assertEqual(r.status_code, 400)

    def test_con_el_token_correcto_si(self):
        self._login(csrf="el-bueno")
        m = self._mem("suggested")
        r = self.client.post(f"/assistant/api/memory/{m['id']}/approve",
                             json={"version": memory_version(m)},
                             headers={"X-CSRF-Token": "el-bueno"})
        self.assertEqual(r.status_code, 200)

    def test_leer_el_panel_no_necesita_token(self):
        self._login()
        self.assertEqual(self._panel().status_code, 200)

    def test_assistant_api_NO_esta_exento_del_guard_global(self):
        """La lista de exenciones de app/__init__.py decide esto de verdad."""
        src = Path("app/__init__.py").read_text(encoding="utf-8")
        guard = src[src.index("def enforce_csrf"):src.index("def enforce_csrf") + 1400]
        for exento in ("/internal/agent/", "admin.backups_sync"):
            self.assertIn(exento, guard)
        self.assertNotIn('startswith("/assistant/api/")\n            return None', guard)


# ───────────────────────────── 3. el cliente

class ElRenderizadorNoPuedeEscribirMarkupTests(unittest.TestCase):
    """La pantalla donde se revisa contenido no confiable es la que mas cuida
    tiene que tener. Mismo criterio que la vista estructurada de 8.4."""

    def _js(self, nombre="assistant_memory.js") -> str:
        return Path(f"app/static/js/{nombre}").read_text(encoding="utf-8")

    def test_nunca_usa_innerHTML_con_contenido(self):
        for nombre in ("assistant_memory.js", "assistant.js", "assistant_service.js"):
            with self.subTest(archivo=nombre):
                ofensores = [m for m in
                             re.findall(r"\.innerHTML\s*=\s*[^;]+", self._js(nombre))
                             if "''" not in m and '""' not in m]
                self.assertEqual(ofensores, [], f"innerHTML: {ofensores}")

    def test_no_usa_insertAdjacentHTML_ni_document_write(self):
        js = self._js()
        for prohibido in ("insertAdjacentHTML", "document.write", "outerHTML",
                          "new Function", "eval("):
            self.assertNotIn(prohibido, js, prohibido)

    def test_el_valor_de_una_memoria_entra_por_textContent(self):
        js = self._js()
        self.assertIn("node.textContent = String(text)", js)

    def test_la_plantilla_no_interpola_contenido_de_memoria(self):
        """El esqueleto es estatico: nada de Jinja pintando valores."""
        html = Path("app/templates/assistant/_memory.html").read_text(encoding="utf-8")
        self.assertNotIn("|safe", html)
        self.assertNotIn("{{ item", html)
        self.assertNotIn("{% for", html)


class ElClienteNoDecideLoQueDecideElServidorTests(unittest.TestCase):

    def _service(self) -> str:
        return Path("app/static/js/assistant_service.js").read_text(encoding="utf-8")

    def test_no_manda_los_campos_prohibidos(self):
        cuerpo = self._service()
        inicio = cuerpo.index("AssistantService.prototype.moderateMemory")
        bloque = cuerpo[inicio:inicio + 1400]
        for prohibido in ("actor_user", "status_by", "permission_epoch",
                          "sensitivity", "source:"):
            self.assertNotIn(prohibido, bloque, prohibido)

    def test_manda_el_token_csrf(self):
        self.assertIn("'X-CSRF-Token': csrfToken()", self._service())

    def test_nunca_reintenta_una_aprobacion_con_otra_version(self):
        """Reintentar con una version nueva seria aprobar algo que nadie vio."""
        js = Path("app/static/js/assistant_memory.js").read_text(encoding="utf-8")
        inicio = js.index("function conflicto")
        bloque = js[inicio:inicio + 900]
        self.assertNotIn("moderateMemory", bloque)
        self.assertNotIn("moderar(", bloque)
        self.assertIn("load()", bloque)

    def test_no_mueve_la_tarjeta_antes_de_que_el_servidor_confirme(self):
        js = Path("app/static/js/assistant_memory.js").read_text(encoding="utf-8")
        inicio = js.index("function moderar")
        bloque = js[inicio:js.index("function aplicarCambio")]
        # El cambio de estado ocurre solo en aplicarCambio, tras el ok.
        self.assertNotIn("copia.status =", bloque)
        self.assertIn("aplicarCambio(item, res)", bloque)


class LaUiSigueElPatronExistenteTests(unittest.TestCase):
    """No una segunda arquitectura: el mismo drawer, el mismo <aside>."""

    def test_el_panel_vive_dentro_del_drawer(self):
        conv = Path("app/templates/assistant/_conversation.html").read_text(encoding="utf-8")
        self.assertIn("assistant/_memory.html", conv)

    def test_hay_un_boton_en_la_cabecera_junto_al_historial(self):
        head = Path("app/templates/assistant/_header.html").read_text(encoding="utf-8")
        self.assertIn("ap-assistant-memory-btn", head)
        self.assertIn("ap-assistant-icon-btn", head)

    def test_una_sola_funcion_decide_que_vista_se_ve(self):
        """Dos sitios ocultando paneles es como se ven dos a la vez."""
        js = Path("app/static/js/assistant.js").read_text(encoding="utf-8")
        self.assertEqual(js.count("function applyView"), 1)
        bloque = js[js.index("function applyView"):js.index("function setMemoryOpen")]
        self.assertIn("memoryEl.hidden = !memory", bloque)
        self.assertIn("historyEl.hidden = !history", bloque)

    def test_las_clases_reusan_el_prefijo_del_asistente(self):
        css = Path("app/static/css/assistant.css").read_text(encoding="utf-8")
        self.assertIn(".ap-assistant-memory-card", css)
        self.assertIn("body.dark .ap-assistant-memory-card", css)
        self.assertIn("@media (max-width: 600px)", css)

    def test_el_script_se_carga_como_los_otros_dos(self):
        w = Path("app/templates/assistant/widget.html").read_text(encoding="utf-8")
        self.assertIn("js/assistant_memory.js", w)
        self.assertIn('data-memory-url="/assistant/api/memory"', w)


if __name__ == "__main__":
    unittest.main()
