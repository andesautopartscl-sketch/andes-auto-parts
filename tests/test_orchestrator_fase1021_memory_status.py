"""FASE 10.2.1 — ciclo de vida de la memoria: solo el estado.

QUE INTRODUCE

Tres columnas en `assistant_memory_slot`: `status`, `status_changed_at` y
`status_by`. Nada mas. La politica que hara nacer `suggested` a lo derivado, y
la puerta del selector que lo excluira del prompt, son 10.2.2.

LA PROPIEDAD QUE ESTA UNIDAD TIENE QUE DEMOSTRAR

Que no cambia nada. Una memoria que hoy el modelo ve, la sigue viendo; una base
existente migra a `approved` en todas sus filas; y `derived` —que en 10.2.2
nacera `suggested`— aqui sigue naciendo `approved`. Una migracion que convirtiera
en `suggested` algo que hoy funciona seria un cambio de comportamiento
disfrazado de migracion.
"""
from __future__ import annotations

import os
import pathlib
import sqlite3
import tempfile
import unittest

from app.assistant.orchestrator.memory_schema import (
    DEFAULT_STATUS,
    STATUSES,
    MemorySchemaError,
    validate_status,
)

# Esquema EXACTO de antes de 10.2.1. Es la base sobre la que hay que migrar.
ESQUEMA_PRE_1021 = """
CREATE TABLE assistant_memory_slot (
    id TEXT PRIMARY KEY, actor_user TEXT NOT NULL, scope TEXT NOT NULL,
    conversation_id TEXT, memory_type TEXT NOT NULL, key TEXT NOT NULL,
    value_json TEXT NOT NULL, confidence REAL, source TEXT NOT NULL,
    permission_epoch INTEGER, sensitivity TEXT NOT NULL DEFAULT 'benign',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at TEXT,
    deleted_at TEXT, source_turn_id TEXT, meta_json TEXT
);
"""

VALORES = {
    "preference": '{"answer_style":"brief"}',
    "frequent_entity": '{"kind":"codigo","value":"2404","hit_count":3}',
    "ui_pref": '{"compact":true}',
}


def _store(path):
    from app.assistant.orchestrator.memory_store import MemoryStore

    return MemoryStore(path=path)


class _ConMemoriaActiva(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("ANDES_ASSISTANT_MEMORY_ENABLED")
        os.environ["ANDES_ASSISTANT_MEMORY_ENABLED"] = "1"
        import importlib

        import app.assistant.orchestrator.memory_config as mc
        importlib.reload(mc)
        self._dir = tempfile.TemporaryDirectory()
        self.ruta = pathlib.Path(self._dir.name) / "mem.db"

    def tearDown(self):
        # Devolver, nunca `pop`: en 8.x un test que hizo pop() reetiqueto una
        # corrida entera de otro brazo.
        if self._prev is None:
            os.environ.pop("ANDES_ASSISTANT_MEMORY_ENABLED", None)
        else:
            os.environ["ANDES_ASSISTANT_MEMORY_ENABLED"] = self._prev
        self._dir.cleanup()


class UnaBaseExistenteMigraSinCambiarNadaTests(_ConMemoriaActiva):
    """1 y 2 — migracion con filas existentes, y default approved."""

    def _base_vieja(self):
        conn = sqlite3.connect(str(self.ruta))
        conn.executescript(ESQUEMA_PRE_1021)
        for i, (src, tipo) in enumerate(
            [("explicit", "preference"), ("derived", "frequent_entity"),
             ("ui", "ui_pref")]
        ):
            conn.execute(
                "INSERT INTO assistant_memory_slot (id,actor_user,scope,memory_type,"
                "key,value_json,confidence,source,permission_epoch,sensitivity,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"viejo-{i}", "ana", "user", tipo, f"k{i}", VALORES[tipo], 1.0,
                 src, 0, "benign", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
        conn.commit()
        conn.close()

    def test_las_tres_columnas_se_anaden(self):
        self._base_vieja()
        _store(self.ruta).ensure_schema()
        conn = sqlite3.connect(str(self.ruta))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(assistant_memory_slot)")}
        conn.close()
        self.assertTrue({"status", "status_changed_at", "status_by"} <= cols)

    def test_TODA_fila_existente_queda_approved(self):
        self._base_vieja()
        _store(self.ruta).ensure_schema()
        conn = sqlite3.connect(str(self.ruta))
        estados = [r[0] for r in conn.execute("SELECT status FROM assistant_memory_slot")]
        conn.close()
        self.assertEqual(set(estados), {"approved"})

    def test_ninguna_fila_derivada_queda_suggested_por_accidente(self):
        """El riesgo real de esta migracion: convertir en sugerida una memoria
        que hoy el modelo ya usa."""
        self._base_vieja()
        _store(self.ruta).ensure_schema()
        conn = sqlite3.connect(str(self.ruta))
        n = conn.execute(
            "SELECT COUNT(*) FROM assistant_memory_slot "
            "WHERE source='derived' AND status != 'approved'").fetchone()[0]
        conn.close()
        self.assertEqual(n, 0)

    def test_no_quedan_nulos(self):
        self._base_vieja()
        _store(self.ruta).ensure_schema()
        conn = sqlite3.connect(str(self.ruta))
        n = conn.execute(
            "SELECT COUNT(*) FROM assistant_memory_slot "
            "WHERE status IS NULL OR TRIM(status)=''").fetchone()[0]
        conn.close()
        self.assertEqual(n, 0)

    def test_la_migracion_es_idempotente(self):
        self._base_vieja()
        s = _store(self.ruta)
        for _ in range(3):
            s.ensure_schema()
        conn = sqlite3.connect(str(self.ruta))
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM assistant_memory_slot").fetchone()[0], 3)
        conn.close()

    def test_no_se_borra_ningun_dato(self):
        self._base_vieja()
        _store(self.ruta).ensure_schema()
        conn = sqlite3.connect(str(self.ruta))
        conn.row_factory = sqlite3.Row
        filas = conn.execute(
            "SELECT id, source, value_json FROM assistant_memory_slot ORDER BY id").fetchall()
        conn.close()
        self.assertEqual([f["id"] for f in filas], ["viejo-0", "viejo-1", "viejo-2"])
        self.assertEqual([f["source"] for f in filas], ["explicit", "derived", "ui"])

    def test_una_base_NUEVA_tambien_tiene_las_columnas(self):
        _store(self.ruta).ensure_schema()
        conn = sqlite3.connect(str(self.ruta))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(assistant_memory_slot)")}
        conn.close()
        self.assertTrue({"status", "status_changed_at", "status_by"} <= cols)

    def test_el_indice_de_estado_se_crea_DESPUES_de_la_columna(self):
        """Medido: tenerlo en SCHEMA_SQL rompia `ensure_schema` sobre una base
        anterior con "no such column: status", porque el script corre antes de
        la migracion."""
        self._base_vieja()
        _store(self.ruta).ensure_schema()          # no debe lanzar
        conn = sqlite3.connect(str(self.ruta))
        idx = {r[1] for r in conn.execute("PRAGMA index_list(assistant_memory_slot)")}
        conn.close()
        self.assertIn("ix_asst_mem_actor_status", idx)


class LosEstadosSonUnVocabularioCerradoTests(unittest.TestCase):
    """3 y 4 — estados validos aceptados, invalidos rechazados."""

    def test_los_cuatro_estados(self):
        self.assertEqual(STATUSES,
                         {"approved", "suggested", "rejected", "expired"})

    def test_cada_estado_valido_se_acepta(self):
        for st in sorted(STATUSES):
            with self.subTest(st=st):
                self.assertEqual(validate_status(st), st)

    def test_un_estado_inventado_se_rechaza(self):
        for malo in ("activo", "pending", "APPROVED_", "borrado", "si"):
            with self.subTest(malo=malo):
                with self.assertRaises(MemorySchemaError) as ctx:
                    validate_status(malo)
                self.assertEqual(ctx.exception.code, "invalid_status")

    def test_vacio_significa_approved(self):
        for vacio in (None, "", "   "):
            with self.subTest(vacio=vacio):
                self.assertEqual(validate_status(vacio), DEFAULT_STATUS)

    def test_el_default_es_approved(self):
        self.assertEqual(DEFAULT_STATUS, "approved")

    def test_el_sanitizador_lo_valida(self):
        from app.assistant.orchestrator.memory_sanitize import sanitize_memory_record

        r = sanitize_memory_record(
            actor_user="ana", scope="user", conversation_id=None,
            memory_type="preference", key="answer_style",
            value={"answer_style": "brief"})
        self.assertEqual(r["status"], "approved")
        with self.assertRaises(MemorySchemaError):
            sanitize_memory_record(
                actor_user="ana", scope="user", conversation_id=None,
                memory_type="preference", key="answer_style",
                value={"answer_style": "brief"}, status="inventado")


class LaEscrituraRegistraQuienYCuandoTests(_ConMemoriaActiva):
    """5 y 6 — status_changed_at y status_by."""

    def _crear(self, **kw):
        s = _store(self.ruta)
        s.ensure_schema()
        base = dict(actor_user="ana", scope="user", memory_type="preference",
                    key="answer_style", value={"answer_style": "brief"})
        base.update(kw)
        return s, s.upsert(**base)

    def test_una_memoria_nueva_nace_approved(self):
        _, slot = self._crear()
        self.assertEqual(slot["status"], "approved")

    def test_derived_TAMBIEN_nace_approved_en_esta_unidad(self):
        """La politica que lo hara nacer `suggested` es 10.2.2. Aqui no."""
        _, slot = self._crear(source="derived", memory_type="frequent_entity",
                              key="codigo:2404",
                              value={"kind": "codigo", "value": "2404", "hit_count": 2})
        self.assertEqual(slot["status"], "approved")

    def test_se_puede_crear_suggested_explicitamente(self):
        """El contrato queda preparado aunque nadie lo use todavia."""
        _, slot = self._crear(status="suggested")
        self.assertEqual(slot["status"], "suggested")

    def test_status_changed_at_se_registra(self):
        _, slot = self._crear(status="suggested")
        self.assertTrue(slot["status_changed_at"])
        self.assertTrue(str(slot["status_changed_at"]).endswith("Z"))

    def test_status_by_se_registra(self):
        _, slot = self._crear(status="suggested", status_by="ana")
        self.assertEqual(slot["status_by"], "ana")

    def test_status_by_vacio_queda_nulo_no_cadena_vacia(self):
        _, slot = self._crear(status="suggested", status_by="   ")
        self.assertIsNone(slot["status_by"])

    def test_reescribir_el_valor_NO_reaprueba_en_silencio(self):
        """El riesgo obvio del upsert: que actualizar el valor de una memoria
        sugerida la deje aprobada sin que nadie lo decida."""
        s, slot = self._crear(status="suggested", status_by="ana")
        vuelto = s.upsert(actor_user="ana", scope="user", memory_type="preference",
                          key="answer_style", value={"answer_style": "detailed"})
        self.assertEqual(vuelto["status"], "suggested")
        self.assertEqual(vuelto["status_by"], "ana")
        self.assertEqual(vuelto["value"]["answer_style"], "detailed")

    def test_un_cambio_explicito_de_estado_SI_se_aplica(self):
        s, _ = self._crear(status="suggested")
        vuelto = s.upsert(actor_user="ana", scope="user", memory_type="preference",
                          key="answer_style", value={"answer_style": "brief"},
                          status="approved", status_by="ana")
        self.assertEqual(vuelto["status"], "approved")
        self.assertEqual(vuelto["status_by"], "ana")


class ElAislamientoPorActorNoCambiaTests(_ConMemoriaActiva):
    """7 — el estado no abre ninguna puerta entre actores."""

    def test_no_se_ve_la_memoria_de_otro_actor(self):
        s = _store(self.ruta)
        s.ensure_schema()
        s.upsert(actor_user="ana", scope="user", memory_type="preference",
                 key="answer_style", value={"answer_style": "brief"})
        s.upsert(actor_user="beto", scope="user", memory_type="preference",
                 key="answer_style", value={"answer_style": "detailed"},
                 status="suggested")
        de_ana = s.list_slots(actor_user="ana")
        self.assertEqual(len(de_ana), 1)
        self.assertEqual(de_ana[0]["actor_user"], "ana")

    def test_no_se_puede_leer_por_id_el_slot_de_otro(self):
        s = _store(self.ruta)
        s.ensure_schema()
        ajeno = s.upsert(actor_user="beto", scope="user", memory_type="preference",
                         key="answer_style", value={"answer_style": "brief"})
        self.assertIsNone(s.get_slot(actor_user="ana", slot_id=ajeno["id"]))


class NoCambiaNadaDeLoQueYaFuncionabaTests(_ConMemoriaActiva):
    """8 y 9 — el selector y el historial siguen igual.

    La politica de exclusion es 10.2.2: aqui una memoria `suggested` TODAVIA se
    comporta como hoy. Fijarlo es lo que hace honesta la separacion entre las
    dos unidades.
    """

    def test_el_selector_sigue_seleccionando_lo_de_siempre(self):
        from app.assistant.orchestrator.memory_selector import select_memory_hints

        s = _store(self.ruta)
        s.ensure_schema()
        s.upsert(actor_user="ana", scope="user", memory_type="preference",
                 key="answer_style", value={"answer_style": "brief"})
        sel = select_memory_hints(actor_user="ana", conversation_id="c1", store=s)
        self.assertGreaterEqual(sel.selected_count, 1)

    def test_una_suggested_AUN_se_selecciona_porque_la_puerta_es_de_10_2_2(self):
        from app.assistant.orchestrator.memory_selector import select_memory_hints

        s = _store(self.ruta)
        s.ensure_schema()
        s.upsert(actor_user="ana", scope="user", memory_type="preference",
                 key="answer_style", value={"answer_style": "brief"},
                 status="suggested")
        sel = select_memory_hints(actor_user="ana", conversation_id="c1", store=s)
        self.assertGreaterEqual(sel.selected_count, 1,
                                "10.2.1 no debe excluir nada todavia")

    def test_el_dict_publico_expone_el_estado(self):
        s = _store(self.ruta)
        s.ensure_schema()
        slot = s.upsert(actor_user="ana", scope="user", memory_type="preference",
                        key="answer_style", value={"answer_style": "brief"})
        for campo in ("status", "status_changed_at", "status_by"):
            self.assertIn(campo, slot)

    def test_el_modelo_sqlalchemy_declara_las_tres_columnas(self):
        """El store escribe por SQL directo y SQLAlchemy crea la tabla en bases
        nuevas: si los dos lados no coinciden, el contrato se rompe en silencio."""
        from app.assistant.memory_models import AssistantMemorySlot

        cols = {c.name for c in AssistantMemorySlot.__table__.columns}
        self.assertTrue({"status", "status_changed_at", "status_by"} <= cols)
        estado = AssistantMemorySlot.__table__.columns["status"]
        self.assertFalse(estado.nullable)
        self.assertIsNotNone(estado.server_default,
                             "sin server_default, una fila del store quedaria NULL")


if __name__ == "__main__":
    unittest.main()
