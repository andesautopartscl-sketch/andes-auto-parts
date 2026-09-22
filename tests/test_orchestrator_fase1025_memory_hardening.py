"""FASE 10.2.5 — endurecimiento del panel de memoria.

TRES COSAS, Y LAS TRES SON SOBRE DECIR LA VERDAD

1. LA TRANSICION VIENE DE LA AUDITORIA, no de una columna nueva.
   Guardar `previous_status` en la fila seria inventar estado persistente para
   pintar una linea, y ademas duplicaria peor lo que el evento
   `assistant_memory_status_change` ya registra. La proyeccion sobrevive a una
   recarga porque la fuente es el log, no la sesion del navegador.

2. EL MODAL EXISTE PARA QUE LA DECISION SE TOME MIRANDO EL CONTENIDO.
   `window.confirm` no puede enseñar que se esta aprobando; `window.prompt`
   devuelve texto sin limite. El dialogo propio muestra los mismos campos de la
   tarjeta y acota el motivo a lo que el servidor acepta.

3. UNA MEMORIA MIGRADA NO FUE APROBADA POR NADIE.
   Las cinco filas reales que venian de antes de 10.2.1 tienen `status=approved`
   con `status_by` y `status_changed_at` en NULL. Eso no es un dato faltante: es
   el hecho de que nadie las aprobo, porque el control de aprobacion no existia.
   La interfaz tiene que decir eso y no otra cosa.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from flask import Blueprint, Flask

from app.assistant.orchestrator.memory_panel import (
    AUDIT_TAIL_BYTES,
    CAMPOS_TRANSICION,
    origen_de_aprobacion,
    panel_item,
    panel_payload,
    ultimas_transiciones,
)
from app.assistant.orchestrator.memory_schema import memory_version
from app.assistant.routes import assistant_bp
from app.utils.csrf import CSRF_SESSION_KEY

JS = Path("app/static/js/assistant_memory.js")
HTML_DRAWER = Path("app/templates/assistant/_drawer.html")
HTML_PANEL = Path("app/templates/assistant/_memory.html")


def _slot(**kw):
    base = dict(id="m1", actor_user="ana", scope="user", conversation_id=None,
                memory_type="preference", key="estilo",
                value={"answer_style": "brief"}, confidence=0.9,
                source="derived", permission_epoch=0, sensitivity="benign",
                status="approved", status_changed_at="2026-09-20T10:00:00Z",
                status_by="ana", created_at="2026-09-01T10:00:00Z",
                updated_at="2026-09-01T10:00:00Z", expires_at=None,
                source_turn_id=None, meta={})
    base.update(kw)
    return base


def _evento(memory_id="m1", actor="ana", previo="suggested", nuevo="approved",
            result="ok", changed=True, ts="2026-09-20T10:00:00Z", **kw):
    r = {"event": "assistant_memory_status_change", "memory_id": memory_id,
         "actor_user": actor, "previous_status": previo, "new_status": nuevo,
         "reason": None, "source": "ui", "correlation_id": "c-1",
         "permission_epoch": 2, "operation": "approve", "result": result,
         "changed": changed, "ts": ts}
    r.update(kw)
    return json.dumps(r, ensure_ascii=False)


class _ConAuditoria(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.log = Path(self._dir.name) / "audit.jsonl"

    def tearDown(self):
        self._dir.cleanup()

    def _escribir(self, *lineas):
        self.log.write_text("\n".join(lineas) + "\n", encoding="utf-8")


# ───────────────────────────── 1. la transicion sale del log

class LaTransicionSaleDeLaAuditoriaTests(_ConAuditoria):

    def test_suggested_a_approved(self):
        self._escribir(_evento())
        t = ultimas_transiciones("ana", self.log)
        self.assertEqual(t["m1"]["previous_status"], "suggested")
        self.assertEqual(t["m1"]["new_status"], "approved")

    def test_suggested_a_rejected(self):
        self._escribir(_evento(nuevo="rejected", operation="reject"))
        self.assertEqual(ultimas_transiciones("ana", self.log)["m1"]["new_status"],
                         "rejected")

    def test_approved_a_rejected_con_revoke(self):
        self._escribir(_evento(previo="approved", nuevo="rejected",
                               operation="reject"))
        t = ultimas_transiciones("ana", self.log)["m1"]
        self.assertEqual((t["previous_status"], t["new_status"]),
                         ("approved", "rejected"))

    def test_rejected_a_approved_con_reconsider(self):
        self._escribir(_evento(previo="rejected", nuevo="approved"))
        t = ultimas_transiciones("ana", self.log)["m1"]
        self.assertEqual((t["previous_status"], t["new_status"]),
                         ("rejected", "approved"))

    def test_la_ultima_gana(self):
        self._escribir(_evento(ts="2026-09-20T10:00:00Z"),
                       _evento(previo="approved", nuevo="rejected",
                               ts="2026-09-21T10:00:00Z"))
        self.assertEqual(ultimas_transiciones("ana", self.log)["m1"]["new_status"],
                         "rejected")

    def test_un_conflicto_de_version_NO_es_historia(self):
        """Pintar un intento bloqueado diria que algo cambio cuando no cambio."""
        self._escribir(_evento(result="version_conflict", changed=False))
        self.assertEqual(ultimas_transiciones("ana", self.log), {})

    def test_una_operacion_idempotente_tampoco(self):
        self._escribir(_evento(result="noop", changed=False))
        self.assertEqual(ultimas_transiciones("ana", self.log), {})

    def test_no_se_muestra_la_transicion_de_otro_actor(self):
        self._escribir(_evento(actor="beto"))
        self.assertEqual(ultimas_transiciones("ana", self.log), {})

    def test_solo_salen_los_campos_declarados(self):
        """El evento lleva correlation_id, source y epoch: eso es diagnostico
        interno y no tiene por que viajar al navegador."""
        self._escribir(_evento())
        entrada = ultimas_transiciones("ana", self.log)["m1"]
        self.assertEqual(set(entrada), set(CAMPOS_TRANSICION))
        for interno in ("correlation_id", "permission_epoch", "source", "reason"):
            self.assertNotIn(interno, entrada)

    def test_una_memoria_sin_historial_no_aparece(self):
        self._escribir(_evento(memory_id="otra"))
        self.assertNotIn("m1", ultimas_transiciones("ana", self.log))

    def test_un_log_inexistente_no_rompe_nada(self):
        self.assertEqual(ultimas_transiciones("ana", self.log / "no-existe"), {})

    def test_una_linea_corrupta_no_rompe_nada(self):
        self.log.write_text("{no es json\n" + _evento() + "\n", encoding="utf-8")
        self.assertIn("m1", ultimas_transiciones("ana", self.log))

    def test_solo_se_lee_la_COLA_del_log(self):
        """Es append-only y crece sin limite: cargarlo entero para pintar un
        panel seria una fuga de memoria con forma de funcionalidad."""
        relleno = "\n".join(_evento(memory_id=f"viejo{i}")
                            for i in range(AUDIT_TAIL_BYTES // 200 + 400))
        self.log.write_text(relleno + "\n" + _evento(memory_id="reciente") + "\n",
                            encoding="utf-8")
        self.assertGreater(self.log.stat().st_size, AUDIT_TAIL_BYTES)
        t = ultimas_transiciones("ana", self.log)
        self.assertIn("reciente", t)
        self.assertNotIn("viejo0", t)

    def test_la_tarjeta_la_lleva(self):
        it = panel_item(_slot(), transiciones={"m1": {"previous_status": "suggested",
                                                      "new_status": "approved",
                                                      "actor_user": "ana",
                                                      "ts": "2026-09-20T10:00:00Z",
                                                      "result": "ok"}})
        self.assertEqual(it["last_transition"]["previous_status"], "suggested")

    def test_sin_transicion_el_campo_es_None_no_inventado(self):
        self.assertIsNone(panel_item(_slot(), transiciones={})["last_transition"])


# ───────────────────────────── 2. origen de la aprobacion

class UnaMemoriaMigradaNoFueAprobadaPorNadieTests(unittest.TestCase):

    def test_A_aprobada_por_una_persona(self):
        it = panel_item(_slot(status_by="ana",
                              status_changed_at="2026-09-20T10:00:00Z"))
        self.assertEqual(it["approval_origin"], "human")
        self.assertIsNone(it["origin_note"])

    def test_B_migrada_con_ambos_NULL(self):
        """La firma exacta de las cinco filas reales de 10.2.1."""
        it = panel_item(_slot(status="approved", status_by=None,
                              status_changed_at=None))
        self.assertEqual(it["approval_origin"], "migration")
        self.assertIn("migración inicial", it["origin_note"])
        self.assertIn("Nadie la aprobó", it["origin_note"])

    def test_la_migrada_NO_dice_que_alguien_la_aprobo(self):
        it = panel_item(_slot(status_by=None, status_changed_at=None))
        texto = json.dumps(it, ensure_ascii=False).lower()
        self.assertNotIn("aprobada por", texto)

    def test_escrita_por_el_sistema_y_nunca_moderada(self):
        """Con APPROVAL=0 lo derivado nace approved sin que nadie decida: tiene
        marca de tiempo pero no de persona."""
        it = panel_item(_slot(status_by=None,
                              status_changed_at="2026-09-20T10:00:00Z"))
        self.assertEqual(it["approval_origin"], "system")
        self.assertIn("nadie la ha revisado", it["origin_note"])

    def test_los_tres_origenes_son_excluyentes(self):
        casos = {
            "human": _slot(status_by="ana", status_changed_at="2026-09-20T10:00:00Z"),
            "migration": _slot(status_by=None, status_changed_at=None),
            "system": _slot(status_by=None, status_changed_at="2026-09-20T10:00:00Z"),
        }
        for esperado, slot in casos.items():
            with self.subTest(origen=esperado):
                self.assertEqual(origen_de_aprobacion(slot), esperado)

    def test_el_origen_no_depende_del_estado(self):
        """Una rechazada tambien tiene origen: quien la rechazo."""
        self.assertEqual(
            origen_de_aprobacion(_slot(status="rejected", status_by="ana")), "human")


# ───────────────────────────── 3. la ruta entrega ambas cosas

class _ApiBase(unittest.TestCase):
    def setUp(self):
        self._prev = {k: os.environ.get(k) for k in
                      ("ANDES_ASSISTANT_MEMORY_ENABLED",
                       "ANDES_ASSISTANT_MEMORY_APPROVAL",
                       "ANDES_ASSISTANT_MEMORY_DB", "ANDES_ORCH_AUDIT_PATH")}
        self._dir = tempfile.TemporaryDirectory()
        ruta = Path(self._dir.name) / "mem.db"
        self.log = Path(self._dir.name) / "audit.jsonl"
        os.environ["ANDES_ASSISTANT_MEMORY_ENABLED"] = "1"
        os.environ["ANDES_ASSISTANT_MEMORY_APPROVAL"] = "1"
        os.environ["ANDES_ASSISTANT_MEMORY_DB"] = str(ruta)
        os.environ["ANDES_ORCH_AUDIT_PATH"] = str(self.log)

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

    def _mem(self, **kw):
        base = dict(actor_user="albertadmin", scope="user",
                    memory_type="preference", key="estilo",
                    value={"answer_style": "brief"}, status="suggested",
                    source="derived", verify_conversation=False)
        base.update(kw)
        return self.s.upsert(**base)

    def _panel(self, **params):
        q = "&".join(f"{k}={v}" for k, v in params.items())
        return self.client.get("/assistant/api/memory/panel" + (f"?{q}" if q else ""))


class LaRutaEntregaLaTransicionYElOrigenTests(_ApiBase):

    def test_tras_aprobar_la_transicion_sobrevive_a_recargar(self):
        """Esto es lo que una flecha guardada solo en el navegador no puede."""
        self._login()
        m = self._mem()
        r = self.client.post(f"/assistant/api/memory/{m['id']}/approve",
                             json={"version": memory_version(m)})
        self.assertEqual(r.status_code, 200)

        # Recarga completa: el panel se pide de cero, sin estado de cliente.
        item = next(i for i in (self._panel().get_json() or {})["items"]
                    if i["id"] == m["id"])
        self.assertEqual(item["last_transition"]["previous_status"], "suggested")
        self.assertEqual(item["last_transition"]["new_status"], "approved")
        self.assertEqual(item["last_transition"]["actor_user"], "albertadmin")
        self.assertEqual(item["approval_origin"], "human")

    def test_tras_rechazar_igual(self):
        self._login()
        m = self._mem()
        self.client.post(f"/assistant/api/memory/{m['id']}/reject",
                         json={"version": memory_version(m)})
        item = next(i for i in (self._panel().get_json() or {})["items"]
                    if i["id"] == m["id"])
        self.assertEqual(item["last_transition"]["new_status"], "rejected")

    def test_un_conflicto_no_deja_transicion(self):
        self._login()
        m = self._mem()
        v = memory_version(m)
        self.client.post(f"/assistant/api/memory/{m['id']}/approve",
                         json={"version": v})
        r = self.client.post(f"/assistant/api/memory/{m['id']}/reject",
                             json={"version": v})
        self.assertEqual(r.status_code, 409)
        item = next(i for i in (self._panel().get_json() or {})["items"]
                    if i["id"] == m["id"])
        # La transicion sigue siendo la que SI ocurrio.
        self.assertEqual(item["last_transition"]["new_status"], "approved")

    def test_una_memoria_migrada_no_tiene_transicion_y_lo_dice(self):
        self._login()
        m = self._mem(status="approved")
        # Firma de la migracion de 10.2.1: ambas marcas en NULL.
        import sqlite3
        con = sqlite3.connect(os.environ["ANDES_ASSISTANT_MEMORY_DB"])
        con.execute("UPDATE assistant_memory_slot SET status_by = NULL, "
                    "status_changed_at = NULL WHERE id = ?", (m["id"],))
        con.commit()
        con.close()

        item = next(i for i in (self._panel().get_json() or {})["items"]
                    if i["id"] == m["id"])
        self.assertIsNone(item["last_transition"])
        self.assertEqual(item["approval_origin"], "migration")
        self.assertIn("migración inicial", item["origin_note"])

    def test_la_transicion_no_arrastra_el_valor_de_la_memoria(self):
        self._login()
        m = self._mem(value={"answer_style": "operational"})
        self.client.post(f"/assistant/api/memory/{m['id']}/approve",
                         json={"version": memory_version(m)})
        item = next(i for i in (self._panel().get_json() or {})["items"]
                    if i["id"] == m["id"])
        self.assertNotIn("operational",
                         json.dumps(item["last_transition"], ensure_ascii=False))

    def test_un_actor_no_ve_la_transicion_de_otro(self):
        self._login("albertadmin")
        m = self._mem()
        self.client.post(f"/assistant/api/memory/{m['id']}/approve",
                         json={"version": memory_version(m)})
        # Mismo id en el log, pero pedido por otro actor.
        self.assertEqual(ultimas_transiciones("otro", self.log), {})


# ───────────────────────────── 4. el modal

class ElModalReemplazaAWindowTests(unittest.TestCase):

    def _js(self) -> str:
        return JS.read_text(encoding="utf-8")

    def test_no_queda_ninguna_llamada_a_window_confirm_ni_prompt(self):
        js = self._js()
        llamadas = re.findall(r"(?<![\w.])window\.(confirm|prompt)\s*\(", js)
        self.assertEqual(llamadas, [], f"quedan llamadas: {llamadas}")

    def test_el_esqueleto_del_modal_existe_en_el_drawer(self):
        """Hijo directo del drawer: dentro del cuerpo quedaria recortado por su
        `overflow:auto` y se desplazaria con la lista."""
        d = HTML_DRAWER.read_text(encoding="utf-8")
        self.assertIn('id="ap-assistant-modal"', d)
        self.assertNotIn('id="ap-assistant-modal"',
                         HTML_PANEL.read_text(encoding="utf-8"))

    def test_el_dialogo_se_declara_como_tal(self):
        d = HTML_DRAWER.read_text(encoding="utf-8")
        self.assertIn('role="dialog"', d)
        self.assertIn('aria-modal="true"', d)
        self.assertIn('aria-labelledby="ap-assistant-modal-title"', d)

    def test_tiene_cancelar_y_una_accion_primaria_distinta(self):
        d = HTML_DRAWER.read_text(encoding="utf-8")
        self.assertIn('id="ap-assistant-modal-cancel"', d)
        self.assertIn('id="ap-assistant-modal-ok"', d)
        self.assertIn("is-primary", d)

    def test_el_motivo_esta_acotado_a_lo_que_acepta_el_servidor(self):
        from app.assistant.orchestrator.memory_approval import MAX_REASON

        d = HTML_DRAWER.read_text(encoding="utf-8")
        self.assertIn(f'maxlength="{MAX_REASON}"', d)

    def test_la_plantilla_del_modal_no_interpola_nada(self):
        d = HTML_DRAWER.read_text(encoding="utf-8")
        modal = d[d.index('id="ap-assistant-modal"'):]
        self.assertNotIn("|safe", modal)
        self.assertNotIn("{{", modal)

    def test_el_contenido_variable_entra_por_textContent(self):
        js = self._js()
        bloque = js[js.index("function abrirModal"):js.index("function errorEnModal")]
        self.assertIn("modal.titulo.textContent", bloque)
        self.assertIn("modal.lead.textContent", bloque)
        self.assertNotIn("innerHTML", bloque)

    def test_escape_cierra_el_modal_y_no_el_drawer(self):
        js = self._js()
        self.assertIn("event.stopPropagation()", js)
        self.assertIn("cerrarModal()", js)
        # En captura, para correr ANTES del Escape de assistant.js.
        self.assertIn("}, true);", js)

    def test_el_foco_se_administra_y_se_devuelve(self):
        js = self._js()
        self.assertIn("state.focoPrevio = document.activeElement", js)
        self.assertIn("previo.focus()", js)
        self.assertIn("function focoUtil", js)

    def test_el_tabulador_no_sale_del_dialogo(self):
        js = self._js()
        bloque = js[js.index("if (event.key !== 'Tab') return;"):]
        self.assertIn("event.preventDefault()", bloque)
        self.assertIn("ultimo.focus()", bloque)
        self.assertIn("primero.focus()", bloque)

    def test_el_error_del_backend_se_ve_sin_cerrar_el_dialogo(self):
        js = self._js()
        self.assertIn("function errorEnModal", js)
        self.assertIn("errorEnModal((res && res.error)", js)

    def test_el_conflicto_usa_el_mensaje_del_backend_y_no_reintenta(self):
        js = self._js()
        bloque = js[js.index("function conflicto"):js.index("function moderar")]
        self.assertIn("mensaje ||", bloque)
        self.assertIn("load()", bloque)
        self.assertNotIn("moderateMemory", bloque)
        self.assertNotIn("moderar(", bloque)

    def test_sigue_sin_librerias_externas(self):
        w = Path("app/templates/assistant/widget.html").read_text(encoding="utf-8")
        self.assertNotIn("http://", w)
        self.assertNotIn("https://", w)
        self.assertNotIn("cdn", w.lower())

    def test_el_modal_es_usable_en_movil(self):
        css = Path("app/static/css/assistant.css").read_text(encoding="utf-8")
        movil = css[css.rindex("@media (max-width: 600px)"):]
        self.assertIn(".ap-assistant-modal", movil)
        self.assertIn("width: 100%", movil)

    def test_el_contrato_http_no_cambio(self):
        """El modal es UI: manda exactamente lo mismo que antes."""
        srv = Path("app/static/js/assistant_service.js").read_text(encoding="utf-8")
        bloque = srv[srv.index("AssistantService.prototype.moderateMemory"):]
        bloque = bloque[:1400]
        self.assertIn("'X-CSRF-Token': csrfToken()", srv)
        for prohibido in ("actor_user", "status_by", "permission_epoch",
                          "sensitivity", "source:"):
            self.assertNotIn(prohibido, bloque, prohibido)


class LaTarjetaDistingueLosTresCasosTests(unittest.TestCase):
    """Que se pinta, y cual de las tres frases gana."""

    def _bloque(self) -> str:
        js = JS.read_text(encoding="utf-8")
        return js[js.index("var trans = item.just_changed"):
                  js.index("(item.warnings || [])")]

    def test_la_transicion_manda_sobre_el_resto(self):
        b = self._bloque()
        self.assertLess(b.index("if (trans)"), b.index("approval_origin === 'human'"))

    def test_la_heredada_no_se_pinta_como_aprobacion_humana(self):
        b = self._bloque()
        self.assertIn("is-inherited", b)
        self.assertIn("item.origin_note", b)
        cola = b[b.index("else if (item.origin_note)"):]
        self.assertNotIn("por ", cola)

    def test_la_transicion_recien_hecha_se_marca_distinto(self):
        self.assertIn("is-fresh", self._bloque())


if __name__ == "__main__":
    unittest.main()
