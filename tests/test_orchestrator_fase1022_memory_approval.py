"""FASE 10.2.2 — politica de memoria y puerta de seleccion.

EL CAMBIO DE MODELO

    ANTES   conversacion -> memoria automatica -> LLM
    AHORA   conversacion -> SUGGESTED -> el usuario aprueba -> LLM

Dos piezas, una bandera:

  1. POLITICA — lo que el sistema INFIERE (`source=derived`) nace `suggested`.
     Lo que el usuario pide explicitamente no pasa por ahi.
  2. PUERTA — `memory_selector` deja llegar al modelo SOLO lo `approved`.

LA PUERTA ESTA EN UN SOLO SITIO, y va DESPUES de propiedad, scope, TTL y epoch.
Ese orden es la propiedad de seguridad de esta unidad: el estado solo puede
CERRAR una puerta mas, nunca abrir ninguna de las anteriores. Una memoria de
otro actor se descarta antes de que nadie mire su estado.

MODO SOMBRA

Con la bandera apagada la puerta no filtra: cuenta. Asi se puede medir cuanta
memoria quedaria fuera antes de activarla, sin cambiar ninguna respuesta.

NOTA SOBRE LOS TIPOS USADOS

Los tests que cuentan seleccion usan tipos `benign` (preference, ui_pref) a
proposito. `frequent_entity` y `pinned_entity` son `contextual`, y el epoch
falla cerrado cuando no hay contexto de aplicacion: se caerian ANTES de llegar
a la puerta y el conteo estaria midiendo otra cosa. Eso mismo se verifica
explicitamente mas abajo, como propiedad de seguridad.
"""
from __future__ import annotations

import os
import pathlib
import tempfile
import unittest

# Importar del paquete `app` AQUI, a nivel de modulo, no es cosmetico.
# `app/__init__.py` llama a `create_app()` al importarse, y eso recarga `.env`
# con force=True: cualquier variable que un `setUp` hubiera puesto ANTES del
# primer import quedaria pisada por el `.env` del proyecto, donde MEMORY=0.
# Importando aqui, el `.env` ya se cargo cuando corre el primer `setUp`, y las
# banderas que fija cada test son las que mandan. Medido: sin esta linea, el
# primer test del modulo —y solo ese— veia la memoria apagada.
from app.assistant.orchestrator.memory_selector import (
    hint_contains_prohibited,
    select_memory_hints,
)


def _store(path):
    from app.assistant.orchestrator.memory_store import MemoryStore

    return MemoryStore(path=path)


class _Base(unittest.TestCase):
    """Enciende memoria; la bandera de aprobacion la fija cada clase."""

    APPROVAL = "0"

    def setUp(self):
        self._prev = {
            k: os.environ.get(k)
            for k in ("ANDES_ASSISTANT_MEMORY_ENABLED",
                      "ANDES_ASSISTANT_MEMORY_APPROVAL")
        }
        os.environ["ANDES_ASSISTANT_MEMORY_ENABLED"] = "1"
        os.environ["ANDES_ASSISTANT_MEMORY_APPROVAL"] = self.APPROVAL
        import importlib

        import app.assistant.orchestrator.memory_config as mc
        importlib.reload(mc)
        self._dir = tempfile.TemporaryDirectory()
        self.ruta = pathlib.Path(self._dir.name) / "mem.db"
        self.s = _store(self.ruta)
        self.s.ensure_schema()

    def tearDown(self):
        # Devolver, nunca `pop`: en 8.x un pop() reetiqueto una corrida entera.
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._dir.cleanup()

    def _pref(self, key="answer_style", status=None, actor="ana", scope="user",
              conversation_id=None, **kw):
        """Una memoria `benign`: sobrevive al epoch, asi que mide la puerta."""
        base = dict(actor_user=actor, scope=scope, memory_type="preference",
                    key=key, value={"answer_style": "brief"},
                    conversation_id=conversation_id, status=status,
                    verify_conversation=False)
        base.update(kw)
        return self.s.upsert(**base)

    def _sel(self, actor="ana", conversation_id="c1"):
        return select_memory_hints(actor_user=actor,
                                   conversation_id=conversation_id, store=self.s)


# ───────────────────────────── ON: la puerta filtra

class ConLaPoliticaEncendidaTests(_Base):
    APPROVAL = "1"

    def test_4_approved_entra(self):
        self._pref(status="approved")
        self.assertEqual(self._sel().selected_count, 1)

    def test_3_suggested_NO_entra(self):
        self._pref(status="suggested")
        sel = self._sel()
        self.assertEqual(sel.selected_count, 0)
        self.assertEqual(sel.hints, [])

    def test_5_rejected_NO_entra(self):
        self._pref(status="rejected")
        self.assertEqual(self._sel().selected_count, 0)

    def test_6_expired_NO_entra(self):
        self._pref(status="expired")
        self.assertEqual(self._sel().selected_count, 0)

    def test_solo_lo_approved_sobrevive_a_una_mezcla(self):
        self._pref(key="k_ok", status="approved")
        self._pref(key="k_sug", status="suggested")
        self._pref(key="k_rej", status="rejected")
        sel = self._sel()
        self.assertEqual(sel.selected_count, 1)
        self.assertEqual(sel.approval_excluded, 2)
        self.assertEqual(sel.approval_excluded_by_status,
                         {"suggested": 1, "rejected": 1})

    def test_la_puerta_se_declara_aplicada(self):
        self._pref(status="approved")
        self.assertTrue(self._sel().approval_enforced)

    def test_1_lo_derivado_nace_suggested(self):
        slot = self._pref(key="pref_derivada", source="derived",
                          status="suggested")
        self.assertEqual(slot["status"], "suggested")

    def test_lo_explicito_sigue_naciendo_approved(self):
        self.assertEqual(self._pref(source="explicit")["status"], "approved")

    def test_lo_de_la_UI_sigue_naciendo_approved(self):
        self.assertEqual(self._pref(source="ui")["status"], "approved")


class LaPoliticaDeDerivadaSeAplicaEnElEscritorTests(_Base):
    """1 y 2 — la politica vive en `memory_derived`, no en el store."""
    APPROVAL = "1"

    def test_el_escritor_derivado_pide_suggested_con_la_bandera_ON(self):
        src = pathlib.Path(
            "app/assistant/orchestrator/memory_derived.py").read_text(encoding="utf-8")
        self.assertIn('status="suggested" if memory_approval_enabled() else None',
                      src)

    def test_2_una_derivada_existente_NO_se_reetiqueta(self):
        """El upsert no toca `status` salvo que se le pase. Una derivada que ya
        estaba aprobada sigue aprobada aunque se reescriba su valor."""
        self._pref(key="codigo:2404", source="derived", status="approved")
        vuelto = self.s.upsert(
            actor_user="ana", scope="user", memory_type="preference",
            key="codigo:2404", source="derived",
            value={"answer_style": "detailed"})
        self.assertEqual(vuelto["status"], "approved")
        self.assertEqual(vuelto["value"]["answer_style"], "detailed")


# ───────────────────────────── OFF: nada cambia, pero se mide

class ConLaPoliticaApagadaTests(_Base):
    APPROVAL = "0"

    def test_7_suggested_TODAVIA_entra(self):
        """Apagada, el comportamiento es exactamente el de 10.2.1."""
        self._pref(status="suggested")
        self.assertEqual(self._sel().selected_count, 1)

    def test_7_rejected_y_expired_tambien_entran(self):
        self._pref(key="a", status="rejected")
        self._pref(key="b", memory_type="ui_pref", status="expired",
                   value={"compact": True})
        self.assertEqual(self._sel().selected_count, 2)

    def test_la_puerta_se_declara_NO_aplicada(self):
        self._pref(status="suggested")
        self.assertFalse(self._sel().approval_enforced)

    def test_no_excluye_nada(self):
        self._pref(status="suggested")
        self.assertEqual(self._sel().approval_excluded, 0)

    def test_lo_derivado_sigue_naciendo_approved(self):
        slot = self._pref(key="codigo:2404", source="derived")
        self.assertEqual(slot["status"], "approved")


class ElModoSombraMideSinCambiarNadaTests(_Base):
    APPROVAL = "0"

    def test_cuenta_lo_que_habria_quedado_fuera(self):
        self._pref(key="a", status="suggested")
        self._pref(key="b", memory_type="ui_pref", status="rejected",
                   value={"compact": True})
        sel = self._sel()
        self.assertEqual(sel.selected_count, 2, "la respuesta no cambia")
        self.assertEqual(sel.approval_excluded_by_status,
                         {"suggested": 1, "rejected": 1})
        self.assertEqual(len(sel.approval_shadow), 2)

    def test_la_telemetria_tiene_EXACTAMENTE_los_campos_seguros(self):
        self._pref(status="suggested")
        entrada = self._sel().approval_shadow[0]
        self.assertEqual(
            set(entrada),
            {"memory_id", "status", "reason", "scope", "actor",
             "estimated_tokens"})

    def test_la_telemetria_NO_lleva_el_valor_de_la_memoria(self):
        self._pref(key="estilo_operativo", status="suggested",
                   value={"answer_style": "detailed"})
        texto = str(self._sel().approval_shadow)
        self.assertNotIn("detailed", texto)
        self.assertNotIn("answer_style", texto)

    def test_estimated_tokens_mide_el_hint_no_el_slot(self):
        """Lo que importa medir es lo que habria costado en el prompt."""
        self._pref(status="suggested")
        self.assertGreater(self._sel().approval_shadow[0]["estimated_tokens"], 0)

    def test_la_razon_es_un_vocabulario_cerrado(self):
        self._pref(key="a", status="suggested")
        self._pref(key="b", status="rejected")
        self._pref(key="c", status="expired")
        razones = {e["status"]: e["reason"] for e in self._sel().approval_shadow}
        self.assertEqual(razones, {"suggested": "pendiente_de_aprobacion",
                                   "rejected": "rechazada_por_el_usuario",
                                   "expired": "caducada"})


# ───────────────────────────── seguridad: el estado no abre ninguna puerta

class ElEstadoNoPuedeSaltarseNingunaPuertaTests(_Base):
    APPROVAL = "1"

    def test_8_una_approved_de_otro_actor_NO_cruza(self):
        self._pref(actor="beto", status="approved")
        self.assertEqual(self._sel(actor="ana").selected_count, 0)

    def test_8_ni_siquiera_aparece_en_la_telemetria_de_sombra(self):
        """Se descarta por propiedad ANTES de que nadie mire su estado."""
        self._pref(actor="beto", status="suggested")
        self.assertEqual(self._sel(actor="ana").approval_shadow, [])

    def test_9_una_approved_de_otra_conversacion_NO_cruza(self):
        self._pref(scope="conversation", conversation_id="c-otra",
                   status="approved")
        self.assertEqual(self._sel(conversation_id="c-mia").selected_count, 0)

    def test_9_la_de_la_conversacion_actual_si(self):
        self._pref(scope="conversation", conversation_id="c-mia",
                   status="approved")
        self.assertEqual(self._sel(conversation_id="c-mia").selected_count, 1)

    def test_10_el_TTL_sigue_mandando_sobre_una_approved(self):
        self._pref(status="approved", expires_at="2020-01-01T00:00:00Z")
        self.assertEqual(self._sel().selected_count, 0)

    def test_11_el_epoch_sigue_mandando_sobre_una_approved(self):
        """Una contextual aprobada NO entra si el epoch no esta disponible.
        El estado cierra puertas; no abre la que el epoch tiene cerrada."""
        self.s.upsert(actor_user="ana", scope="user",
                      memory_type="frequent_entity", key="codigo:2404",
                      value={"kind": "codigo", "value": "2404", "hit_count": 3},
                      status="approved")
        sel = self._sel()
        self.assertEqual(sel.selected_count, 0)
        self.assertEqual(sel.memory_contextual_invalidated, 1)

    def test_12_la_sensitivity_no_la_decide_el_estado(self):
        slot = self.s.upsert(
            actor_user="ana", scope="user", memory_type="frequent_entity",
            key="codigo:2404", status="approved",
            value={"kind": "codigo", "value": "2404", "hit_count": 1})
        self.assertEqual(slot["sensitivity"], "contextual")

    def test_el_orden_de_la_puerta_esta_fijado_en_el_fuente(self):
        """La propiedad de seguridad: el estado se mira DESPUES de propiedad,
        scope y epoch. Invertirlo dejaria que un estado abriera una puerta."""
        src = pathlib.Path(
            "app/assistant/orchestrator/memory_selector.py").read_text(encoding="utf-8")
        i_scope = src.index("if not _scope_ok(slot")
        i_epoch = src.index("if not memory_passes_permission_epoch(slot")
        i_estado = src.index('estado = str(slot.get("status")')
        self.assertLess(i_scope, i_estado)
        self.assertLess(i_epoch, i_estado)

    def test_la_puerta_esta_en_UN_solo_sitio(self):
        src = pathlib.Path(
            "app/assistant/orchestrator/memory_selector.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("if aplicar_aprobacion:"), 1)


class ElContratoDeEstadoSigueSiendoCerradoTests(_Base):
    APPROVAL = "1"

    def test_un_estado_invalido_no_escribe_nada(self):
        self.assertIsNone(self._pref(status="inventado"))
        self.assertIn("invalid_status", self.s.last_error or "")
        self.assertEqual(self.s.list_slots(actor_user="ana"), [])

    def test_16_actualizar_el_valor_NO_aprueba_una_sugerida(self):
        self._pref(status="suggested")
        vuelto = self.s.upsert(actor_user="ana", scope="user",
                               memory_type="preference", key="answer_style",
                               value={"answer_style": "detailed"})
        self.assertEqual(vuelto["status"], "suggested")
        self.assertEqual(self._sel().selected_count, 0)

    def test_15_la_duplicada_sigue_siendo_una_sola_fila(self):
        a = self._pref(status="suggested")
        b = self._pref(status="suggested")
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(self.s.list_slots(actor_user="ana")), 1)

    def test_13_un_secreto_en_el_valor_sigue_rechazandose(self):
        """La defensa de contenido no la toca esta unidad."""
        self.assertIsNone(self.s.upsert(
            actor_user="ana", scope="user", memory_type="preference",
            key="k", value={"answer_style": "brief", "api_key": "x"},
            status="approved"))
        self.assertEqual(self.s.list_slots(actor_user="ana"), [])

    def test_14_una_aprobada_no_arrastra_contenido_prohibido(self):
        """Aunque el estado sea `approved`, la defensa de contenido del selector
        sigue corriendo sobre el hint."""
        self.assertTrue(hint_contains_prohibited(
            {"type": "preference", "key": "k",
             "value": {"answer_style": "Bearer abc"}}))


if __name__ == "__main__":
    unittest.main()
