"""FASE 10.3.3 — `vault_broker`: la frontera de autorizacion y uso del Vault.

EL INVARIANTE QUE ESTA SUITE EXISTE PARA DEFENDER

    EL BROKER NO DEVUELVE TEXTO PLANO. A NADIE.

Todo lo demas —grants de un solo uso, revalidacion completa, saneado— sirve a
eso. El secreto centinela se guarda de verdad, se inyecta de verdad en una
peticion, y despues se busca en el grant, en la aprobacion, en el resultado, en
la auditoria, en los logs, en las excepciones y en cada `repr`. No aparece en
ninguno.

TRES ENTIDADES QUE NO SON LA MISMA

    APPROVAL   la decision humana, durable
    GRANT      su token consumible, efimero, de un solo uso
    SECRET     el material protegido

Colapsarlas convertiria un "si" en acceso indefinido.

CONSUMIR VA ANTES DE EJECUTAR

Es una decision, no un descuido, y se prueba: si el ejecutor falla el grant
queda gastado. Al reves, dos hilos podrian pasar la validacion y ejecutar los
dos antes de pelearse por consumir.

LO QUE NO SE PRUEBA AQUI PORQUE NO EXISTE

Rutas HTTP, interfaz, proveedores reales y acciones de escritura. Los
ejecutores de esta suite son dobles en memoria; ninguno abre un socket.
"""
from __future__ import annotations

import io
import json
import logging
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.assistant.vault.vault_broker import (
    ACTIONS,
    AUTHORIZATION_CODES,
    EXECUTION_CODES,
    GRANT_STATUSES,
    SECRET_SLOTS,
    BrokerError,
    ExecutionRequest,
    ExecutionResult,
    Grant,
    PreparedRequest,
    SecretBroker,
    sanitize_result,
)
from app.assistant.vault.vault_keys import VaultKeyring, initialize_keyring
from app.assistant.vault.vault_store import VaultStore

CENTINELA = b"SUPER_SECRET_TEST_VALUE_7d3f10ab95c2e648"


# ───────────────────────────── dobles de ejecutor

class EjecutorOk:
    """Responde bien y no hace eco del secreto."""

    def __init__(self):
        self.llamadas: list[PreparedRequest] = []

    def execute(self, prepared: PreparedRequest) -> ExecutionResult:
        self.llamadas.append(prepared)
        return ExecutionResult(code="success", status=200, body='{"items": []}')


class EjecutorIndiscreto:
    """Devuelve el secreto en el cuerpo. Un tercero puede hacer esto."""

    def execute(self, prepared: PreparedRequest) -> ExecutionResult:
        return ExecutionResult(
            code="success", status=200,
            body="Authorization used: " + prepared.headers.get("Authorization", ""),
            detail="eco: " + (prepared.body or ""))


class EjecutorQueRevienta:
    def __init__(self, exc=None):
        self.exc = exc or RuntimeError("fallo del proveedor")

    def execute(self, prepared: PreparedRequest) -> ExecutionResult:
        raise self.exc


class EjecutorQueDevuelveBasura:
    def execute(self, prepared: PreparedRequest):
        return {"no": "soy un ExecutionResult"}


class _ConBroker(unittest.TestCase):
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
        # El epoch vive en la base de memoria: se apunta a un temporal para no
        # tocar `data/andes.db`.
        os.environ["ANDES_ASSISTANT_MEMORY_DB"] = str(self.raiz / "mem.db")
        os.environ["ANDES_ASSISTANT_MEMORY_ENABLED"] = "1"
        from app.assistant.orchestrator import memory_epoch

        memory_epoch.reset_permission_epoch_store_for_tests(self.raiz / "mem.db")

        initialize_keyring(self.llavero)
        self.store = VaultStore(self.db, keyring=VaultKeyring(self.llavero))
        self.broker = SecretBroker(self.store)
        self.secreto = self.store.create_secret(
            owner_actor="ana", name="MercadoLibre", provider="mercadolibre",
            purposes=["read_listings", "read_account"], plaintext=CENTINELA)

    def tearDown(self):
        from app.assistant.orchestrator import memory_epoch

        memory_epoch.set_permission_epoch_provider_for_tests(None)
        memory_epoch._DEFAULT_STORE = None  # noqa: SLF001
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._dir.cleanup()

    def _req(self, **kw):
        base = dict(action="read_listings", resource="https://api.ml/items",
                    method="GET",
                    headers={"Authorization": "<<secret:AUTHORIZATION>>"})
        base.update(kw)
        return ExecutionRequest(**base)

    def _grant(self, request=None, **kw):
        base = dict(actor="ana", secret_id=self.secreto.id,
                    purpose="read_listings",
                    request=request if request is not None else self._req(),
                    source="ui")
        base.update(kw)
        return self.broker.approve_and_issue_grant(**base)

    def _eventos(self, evento=None):
        if not self.log.exists():
            return []
        out = []
        for l in self.log.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = json.loads(l)
            except ValueError:
                continue
            if evento is None or r.get("event") == evento:
                out.append(r)
        return out


# ───────────────────────────── el camino feliz

class ElCaminoCompletoTests(_ConBroker):

    def test_aprobacion_emite_grant(self):
        cod, g = self._grant()
        self.assertEqual(cod, "authorized")
        self.assertEqual(g.status, "issued")
        self.assertEqual(g.secret_version, 1)
        self.assertEqual(g.actor, "ana")

    def test_canje_inyecta_y_ejecuta(self):
        _, g = self._grant()
        req = self._req()
        ex = EjecutorOk()
        r = self.broker.execute_with_secret(grant_id=g.id, actor="ana",
                                            request=req, executor=ex)
        self.assertTrue(r.ok)
        self.assertEqual((r.code, r.execution, r.status),
                         ("authorized", "success", 200))
        # El ejecutor recibio el secreto ya puesto, con el molde del slot.
        self.assertEqual(ex.llamadas[0].headers["Authorization"],
                         "Bearer " + CENTINELA.decode())

    def test_el_grant_queda_consumido(self):
        _, g = self._grant()
        self.broker.execute_with_secret(grant_id=g.id, actor="ana",
                                        request=self._req(), executor=EjecutorOk())
        fresco = self.broker.get_grant(g.id)
        self.assertEqual(fresco.status, "consumed")
        self.assertIsNotNone(fresco.consumed_at)

    def test_la_peticion_que_construye_el_llamador_no_tiene_el_secreto(self):
        """Por eso puede registrarse entera sin riesgo."""
        req = self._req()
        self.assertNotIn("SUPER_SECRET", json.dumps(req.to_public_dict()))
        self.assertNotIn("SUPER_SECRET", repr(req))
        self.assertEqual(req.slots(), ("AUTHORIZATION",))


# ───────────────────────────── un solo uso

class ElGrantSeGastaUnaVezTests(_ConBroker):

    def test_el_segundo_canje_falla(self):
        _, g = self._grant()
        primero = self.broker.execute_with_secret(
            grant_id=g.id, actor="ana", request=self._req(), executor=EjecutorOk())
        segundo = self.broker.execute_with_secret(
            grant_id=g.id, actor="ana", request=self._req(), executor=EjecutorOk())
        self.assertTrue(primero.ok)
        self.assertEqual(segundo.code, "grant_consumed")

    def test_el_ejecutor_no_se_llama_la_segunda_vez(self):
        _, g = self._grant()
        ex = EjecutorOk()
        self.broker.execute_with_secret(grant_id=g.id, actor="ana",
                                        request=self._req(), executor=ex)
        self.broker.execute_with_secret(grant_id=g.id, actor="ana",
                                        request=self._req(), executor=ex)
        self.assertEqual(len(ex.llamadas), 1)

    def test_la_comprobacion_y_el_consumo_son_ATOMICOS(self):
        """Un SELECT seguido de UPDATE dejaria la ventana por la que dos hilos
        gastan el mismo grant. Se fija leyendo el fuente."""
        fuente = Path("app/assistant/vault/vault_broker.py").read_text(encoding="utf-8")
        cuerpo = fuente[fuente.index("def _consumir"):fuente.index("def _ejecutar")]
        self.assertIn("BEGIN IMMEDIATE", cuerpo)
        self.assertIn("WHERE id=? AND status='issued'", cuerpo)
        self.assertIn("cur.rowcount", cuerpo)

    def test_un_grant_inventado(self):
        r = self.broker.execute_with_secret(
            grant_id="no-existe", actor="ana", request=self._req(),
            executor=EjecutorOk())
        self.assertEqual(r.code, "invalid_grant")

    def test_un_grant_revocado(self):
        _, g = self._grant()
        self.assertTrue(self.broker.revoke_grant(g.id))
        r = self.broker.execute_with_secret(grant_id=g.id, actor="ana",
                                            request=self._req(),
                                            executor=EjecutorOk())
        self.assertEqual(r.code, "invalid_grant")

    def test_los_estados_del_grant_son_cuatro(self):
        self.assertEqual(GRANT_STATUSES,
                         ("issued", "consumed", "expired", "revoked"))


# ───────────────────────────── expiracion

class ElGrantCaducaTests(_ConBroker):

    def test_un_grant_vencido_no_se_canjea(self):
        _, g = self._grant(ttl_seconds=1)
        con = sqlite3.connect(str(self.db))
        con.execute("UPDATE vault_grant SET expires_at='2020-01-01T00:00:00Z' "
                    "WHERE id=?", (g.id,))
        con.commit(); con.close()
        r = self.broker.execute_with_secret(grant_id=g.id, actor="ana",
                                            request=self._req(),
                                            executor=EjecutorOk())
        self.assertEqual(r.code, "expired")

    def test_la_caducidad_se_CALCULA_no_se_cree_lo_almacenado(self):
        """Un `status` que dijera `issued` sobre un grant vencido no basta."""
        _, g = self._grant()
        con = sqlite3.connect(str(self.db))
        con.execute("UPDATE vault_grant SET expires_at='2020-01-01T00:00:00Z', "
                    "status='issued' WHERE id=?", (g.id,))
        con.commit(); con.close()
        self.assertEqual(
            self.broker.execute_with_secret(
                grant_id=g.id, actor="ana", request=self._req(),
                executor=EjecutorOk()).code, "expired")
        # Y al detectarlo, lo deja marcado.
        self.assertEqual(self.broker.get_grant(g.id).status, "expired")

    def test_el_TTL_por_defecto_es_de_minutos(self):
        from app.assistant.vault.vault_broker import GRANT_TTL_SECONDS

        self.assertLessEqual(GRANT_TTL_SECONDS, 600)

    def test_se_distingue_grant_caducado_de_secreto_caducado(self):
        _, g1 = self._grant()
        con = sqlite3.connect(str(self.db))
        con.execute("UPDATE vault_grant SET expires_at='2020-01-01T00:00:00Z' "
                    "WHERE id=?", (g1.id,))
        con.commit(); con.close()
        self.assertEqual(self.broker.execute_with_secret(
            grant_id=g1.id, actor="ana", request=self._req(),
            executor=EjecutorOk()).code, "expired")

        _, g2 = self._grant()
        self.store.expire_secret(secret_id=self.secreto.id)
        r2 = self.broker.execute_with_secret(grant_id=g2.id, actor="ana",
                                             request=self._req(),
                                             executor=EjecutorOk())
        # Mismo codigo publico, distinta causa; la auditoria las separa.
        self.assertEqual(r2.code, "expired")
        self.assertTrue(self._eventos("grant_denied"))


# ───────────────────────────── revalidacion A-L

class SeRevalidaTodoEnElCanjeTests(_ConBroker):
    """No basta con lo que era cierto al emitir el grant."""

    def test_A_actor_distinto(self):
        _, g = self._grant()
        r = self.broker.execute_with_secret(grant_id=g.id, actor="beto",
                                            request=self._req(),
                                            executor=EjecutorOk())
        self.assertEqual(r.code, "actor_mismatch")

    def test_D_secreto_revocado_despues_de_aprobar(self):
        _, g = self._grant()
        self.store.revoke_secret(secret_id=self.secreto.id)
        r = self.broker.execute_with_secret(grant_id=g.id, actor="ana",
                                            request=self._req(),
                                            executor=EjecutorOk())
        self.assertEqual(r.code, "revoked")

    def test_E_secreto_caducado_despues_de_aprobar(self):
        _, g = self._grant()
        self.store.expire_secret(secret_id=self.secreto.id)
        self.assertEqual(self.broker.execute_with_secret(
            grant_id=g.id, actor="ana", request=self._req(),
            executor=EjecutorOk()).code, "expired")

    def test_C_secreto_rotado_despues_de_aprobar(self):
        """La aprobacion era para la credencial de entonces."""
        _, g = self._grant()
        self.store.add_version(secret_id=self.secreto.id, owner_actor="ana",
                               plaintext=b"SUPER_SECRET_TEST_VALUE_v2")
        r = self.broker.execute_with_secret(grant_id=g.id, actor="ana",
                                            request=self._req(),
                                            executor=EjecutorOk())
        self.assertEqual(r.code, "version_conflict")

    def test_G_epoch_cambiado(self):
        from app.assistant.orchestrator.memory_epoch import bump_actor_permission_epoch

        _, g = self._grant()
        bump_actor_permission_epoch("ana")
        r = self.broker.execute_with_secret(grant_id=g.id, actor="ana",
                                            request=self._req(),
                                            executor=EjecutorOk())
        self.assertEqual(r.code, "permission_epoch_conflict")

    def test_G_sin_fuente_de_epoch_no_se_emite_grant(self):
        """Fallar cerrado. Sin epoch nadie se enteraria de que a `ana` le
        quitaron el permiso entre el "si" y el uso, que es lo unico que el
        campo existe para detectar."""
        from app.assistant.orchestrator import memory_epoch

        memory_epoch.set_permission_epoch_provider_for_tests(None)  # Neutral
        cod, g = self._grant()
        self.assertEqual(cod, "permission_epoch_conflict")
        self.assertIsNone(g)
        self.assertEqual(self._eventos("grant_denied")[-1]["result"],
                         "permission_epoch_unavailable")

    def test_G_si_la_fuente_desaparece_el_grant_vivo_deja_de_valer(self):
        from app.assistant.orchestrator import memory_epoch

        _, g = self._grant()
        memory_epoch.set_permission_epoch_provider_for_tests(None)
        ex = EjecutorOk()
        r = self.broker.execute_with_secret(grant_id=g.id, actor="ana",
                                            request=self._req(), executor=ex)
        self.assertEqual(r.code, "permission_epoch_conflict")
        self.assertEqual(ex.llamadas, [])

    def test_G_el_epoch_se_resuelve_AHORA(self):
        """Si se creyera el valor guardado en el grant, el bump no cambiaria
        nada — que es justo lo que el epoch existe para evitar."""
        fuente = Path("app/assistant/vault/vault_broker.py").read_text(encoding="utf-8")
        cuerpo = fuente[fuente.index("def _consumir"):fuente.index("def _ejecutar")]
        self.assertIn("actual = self._epoch(actor)", cuerpo)
        self.assertIn('!= g["permission_epoch"]', cuerpo)

    def test_I_accion_distinta_de_la_aprobada(self):
        _, g = self._grant()
        otra = self._req(action="read_account")
        r = self.broker.execute_with_secret(grant_id=g.id, actor="ana",
                                            request=otra, executor=EjecutorOk())
        self.assertEqual(r.code, "purpose_mismatch")

    def test_J_recurso_distinto_del_aprobado(self):
        _, g = self._grant()
        otro = self._req(resource="https://api.ml/OTRA-COSA")
        r = self.broker.execute_with_secret(grant_id=g.id, actor="ana",
                                            request=otro, executor=EjecutorOk())
        self.assertEqual(r.code, "purpose_mismatch")

    def test_la_FORMA_de_la_peticion_esta_en_la_huella(self):
        """Aprobar X y ejecutar X' es la amenaza D de 10.3.0. Cambiar una
        cabecera, el metodo o los huecos cambia la huella."""
        _, g = self._grant()
        for cambio in ({"method": "POST"},
                       {"headers": {"Authorization": "<<secret:AUTHORIZATION>>",
                                    "X-Extra": "1"}},
                       {"headers": {"X-Api-Key": "<<secret:API_KEY>>"}},
                       {"body": "<<secret:TOKEN>>"}):
            with self.subTest(cambio=list(cambio)):
                r = self.broker.execute_with_secret(
                    grant_id=g.id, actor="ana", request=self._req(**cambio),
                    executor=EjecutorOk())
                self.assertEqual(r.code, "purpose_mismatch")

    def test_K_aprobacion_invalidada(self):
        _, g = self._grant()
        con = sqlite3.connect(str(self.db))
        con.execute("UPDATE vault_approval SET status='revoked' WHERE id=?",
                    (g.approval_id,))
        con.commit(); con.close()
        r = self.broker.execute_with_secret(grant_id=g.id, actor="ana",
                                            request=self._req(),
                                            executor=EjecutorOk())
        self.assertEqual(r.code, "approval_required")

    def test_H_purpose_retirado_del_secreto(self):
        _, g = self._grant()
        con = sqlite3.connect(str(self.db))
        con.execute("UPDATE vault_secret SET purposes_json=? WHERE id=?",
                    (json.dumps(["read_account"]), self.secreto.id))
        con.commit(); con.close()
        r = self.broker.execute_with_secret(grant_id=g.id, actor="ana",
                                            request=self._req(),
                                            executor=EjecutorOk())
        self.assertEqual(r.code, "purpose_mismatch")

    def test_nada_se_ejecuto_en_ninguno_de_esos_casos(self):
        _, g = self._grant()
        self.store.revoke_secret(secret_id=self.secreto.id)
        ex = EjecutorOk()
        self.broker.execute_with_secret(grant_id=g.id, actor="ana",
                                        request=self._req(), executor=ex)
        self.assertEqual(ex.llamadas, [])


# ───────────────────────────── aprobacion

class LaAprobacionEsUnActoHumanoTests(_ConBroker):

    def test_el_LLM_no_puede_aprobar(self):
        for origen in ("llm", "agent", "tool", "system", ""):
            with self.subTest(origen=origen):
                with self.assertRaises(BrokerError) as e:
                    self._grant(source=origen)
                self.assertEqual(e.exception.code, "invalid_source")

    def test_reutiliza_la_lista_de_10_2_3_no_una_copia(self):
        """Duplicarla seria el segundo sistema de aprobacion que esta fase
        prohibe."""
        import ast

        arbol = ast.parse(Path("app/assistant/vault/vault_broker.py")
                          .read_text(encoding="utf-8"))
        importa = any(
            isinstance(n, ast.ImportFrom)
            and (n.module or "").endswith("memory_approval")
            and any(a.name == "SOURCES_HUMANAS" for a in n.names)
            for n in ast.walk(arbol))
        self.assertTrue(importa)

    def test_no_se_aprueba_sobre_un_secreto_revocado(self):
        self.store.revoke_secret(secret_id=self.secreto.id)
        cod, g = self._grant()
        self.assertEqual(cod, "revoked")
        self.assertIsNone(g)

    def test_no_se_aprueba_un_proposito_que_el_secreto_no_declara(self):
        cod, g = self._grant(purpose="read_documents")
        self.assertEqual(cod, "purpose_mismatch")
        self.assertIsNone(g)

    def test_no_se_aprueba_un_secreto_ajeno(self):
        cod, g = self._grant(actor="beto")
        self.assertEqual(cod, "not_found")
        self.assertIsNone(g)

    def test_aprobar_no_da_acceso_indefinido(self):
        """APPROVAL y GRANT son cosas distintas: la primera queda, el segundo
        caduca y se gasta."""
        _, g = self._grant()
        self.broker.execute_with_secret(grant_id=g.id, actor="ana",
                                        request=self._req(), executor=EjecutorOk())
        con = sqlite3.connect(str(self.db))
        aprobacion = con.execute("SELECT status FROM vault_approval WHERE id=?",
                                 (g.approval_id,)).fetchone()[0]
        con.close()
        self.assertEqual(aprobacion, "active", "la aprobacion sigue registrada")
        self.assertEqual(self.broker.get_grant(g.id).status, "consumed")
        # Y no se puede volver a usar sin una aprobacion nueva.
        self.assertEqual(self.broker.execute_with_secret(
            grant_id=g.id, actor="ana", request=self._req(),
            executor=EjecutorOk()).code, "grant_consumed")


# ───────────────────────────── placeholders

class LosHuecosSonEstrictosTests(_ConBroker):

    def test_solo_los_nombres_declarados(self):
        self.assertEqual(set(SECRET_SLOTS), {"AUTHORIZATION", "TOKEN", "API_KEY"})
        for malo in ("<<secret:CUALQUIER_COSA>>", "<<secret:authorization>>",
                     "<<secret:>>", "<<secret:X Y>>"):
            with self.subTest(malo=malo):
                with self.assertRaises(BrokerError) as e:
                    self.broker.validate_request(
                        self._req(headers={"Authorization": malo}))
                self.assertEqual(e.exception.code, "invalid_request")

    def test_el_molde_lo_decide_el_broker(self):
        """`AUTHORIZATION` siempre produce un Bearer: nadie puede pedir el valor
        pelado donde no toca."""
        _, g = self._grant()
        ex = EjecutorOk()
        self.broker.execute_with_secret(grant_id=g.id, actor="ana",
                                        request=self._req(), executor=ex)
        self.assertTrue(ex.llamadas[0].headers["Authorization"].startswith("Bearer "))

    def test_nunca_en_la_URL(self):
        """Un secreto en una query string acaba en el log del proxy y en el
        `Referer`, y eso ya no se retira."""
        with self.assertRaises(BrokerError) as e:
            self.broker.validate_request(
                self._req(resource="https://api.ml/x?t=<<secret:TOKEN>>"))
        self.assertEqual(e.exception.code, "invalid_request")

    def test_en_el_cuerpo_si(self):
        req = self._req(method="POST", headers={},
                        body='{"token": "<<secret:TOKEN>>"}')
        self.assertEqual(self.broker.validate_request(req), ("TOKEN",))

    def test_una_peticion_sin_huecos_no_necesita_grant(self):
        with self.assertRaises(BrokerError) as e:
            self.broker.validate_request(self._req(headers={"X": "sin huecos"}))
        self.assertEqual(e.exception.code, "invalid_request")

    def test_una_accion_no_declarada(self):
        with self.assertRaises(BrokerError) as e:
            self.broker.validate_request(self._req(action="borrar_todo"))
        self.assertEqual(e.exception.code, "invalid_action")

    def test_todas_las_acciones_son_de_lectura(self):
        self.assertTrue(all(a.startswith("read_") for a in ACTIONS))


# ───────────────────────────── saneado del resultado

class ElResultadoSaleSaneadoTests(_ConBroker):

    def test_un_proveedor_indiscreto_no_filtra_el_secreto(self):
        _, g = self._grant(request=self._req(method="POST",
                                             body="<<secret:TOKEN>>"))
        r = self.broker.execute_with_secret(
            grant_id=g.id, actor="ana",
            request=self._req(method="POST", body="<<secret:TOKEN>>"),
            executor=EjecutorIndiscreto())
        self.assertEqual(r.execution, "success")
        self.assertNotIn("SUPER_SECRET", r.body or "")
        self.assertNotIn("SUPER_SECRET", r.detail or "")
        self.assertIn("[redacted]", r.body or "")

    def test_sanitize_result_es_una_funcion_explicita(self):
        bruto = ExecutionResult(code="success", status=200,
                                body="valor=" + CENTINELA.decode(),
                                detail="tambien " + CENTINELA.decode())
        limpio = sanitize_result(bruto, CENTINELA)
        self.assertNotIn("SUPER_SECRET", limpio.body)
        self.assertNotIn("SUPER_SECRET", limpio.detail)
        self.assertEqual(limpio.code, "success")

    def test_tambien_atrapa_el_secreto_recodificado(self):
        import base64

        b64 = base64.b64encode(CENTINELA).decode("ascii")
        limpio = sanitize_result(
            ExecutionResult(code="success", body="eco " + b64), CENTINELA)
        self.assertNotIn(b64, limpio.body)

    def test_un_valor_troceado_se_le_escapa_y_se_dice(self):
        """Defensa en profundidad, no la defensa principal. Documentado en el
        docstring de `sanitize_result` en vez de fingir que cubre todo."""
        mitad = CENTINELA.decode()[:10]
        limpio = sanitize_result(ExecutionResult(code="success", body=mitad),
                                 CENTINELA)
        self.assertEqual(limpio.body, mitad)
        doc = sanitize_result.__doc__ or ""
        self.assertIn("troceado", doc)

    def test_un_ejecutor_que_revienta_no_filtra_nada(self):
        _, g = self._grant()
        r = self.broker.execute_with_secret(
            grant_id=g.id, actor="ana", request=self._req(),
            executor=EjecutorQueRevienta(
                RuntimeError("fallo con " + CENTINELA.decode())))
        self.assertEqual(r.execution, "external_error")
        # El detalle es el TIPO, no el mensaje: un mensaje de una libreria
        # externa puede llevar la peticion entera dentro.
        self.assertEqual(r.detail, "RuntimeError")
        self.assertNotIn("SUPER_SECRET", json.dumps(r.to_dict()))

    def test_un_timeout_se_clasifica(self):
        _, g = self._grant()
        r = self.broker.execute_with_secret(
            grant_id=g.id, actor="ana", request=self._req(),
            executor=EjecutorQueRevienta(TimeoutError()))
        self.assertEqual(r.execution, "timeout")

    def test_un_ejecutor_que_devuelve_basura(self):
        _, g = self._grant()
        r = self.broker.execute_with_secret(
            grant_id=g.id, actor="ana", request=self._req(),
            executor=EjecutorQueDevuelveBasura())
        self.assertEqual(r.execution, "invalid_request")

    def test_los_codigos_de_ejecucion_son_los_de_1030(self):
        self.assertEqual(set(EXECUTION_CODES),
                         {"success", "external_error", "timeout", "rejected",
                          "unauthorized", "invalid_request"})


# ───────────────────────────── auditoria

class LaAuditoriaEsDeListaBlancaTests(_ConBroker):

    def test_los_cinco_eventos_existen(self):
        _, g = self._grant()
        self.broker.execute_with_secret(grant_id=g.id, actor="ana",
                                        request=self._req(), executor=EjecutorOk())
        eventos = {e["event"] for e in self._eventos()}
        self.assertIn("grant_issued", eventos)
        self.assertIn("grant_consumed", eventos)
        self.assertIn("secret_use_success", eventos)

        _, g2 = self._grant()
        self.store.revoke_secret(secret_id=self.secreto.id)
        self.broker.execute_with_secret(grant_id=g2.id, actor="ana",
                                        request=self._req(), executor=EjecutorOk())
        self.assertIn("grant_denied", {e["event"] for e in self._eventos()})

    def test_secret_use_failed_cuando_el_proveedor_falla(self):
        _, g = self._grant()
        self.broker.execute_with_secret(grant_id=g.id, actor="ana",
                                        request=self._req(),
                                        executor=EjecutorQueRevienta())
        e = self._eventos("secret_use_failed")
        self.assertTrue(e)
        self.assertEqual(e[-1]["execution"], "external_error")

    def test_el_evento_lleva_exactamente_los_campos_declarados(self):
        _, g = self._grant()
        self.broker.execute_with_secret(grant_id=g.id, actor="ana",
                                        request=self._req(), executor=EjecutorOk())
        e = self._eventos("grant_consumed")[-1]
        esperados = {"event", "grant_id", "approval_id", "actor_user",
                     "secret_id", "secret_version", "action", "purpose",
                     "resource", "correlation_id", "permission_epoch", "result",
                     "execution", "ts"}
        self.assertEqual(set(e), esperados)

    def test_nunca_registra_material_sensible(self):
        _, g = self._grant(request=self._req(method="POST",
                                             body="<<secret:TOKEN>>"))
        self.broker.execute_with_secret(
            grant_id=g.id, actor="ana",
            request=self._req(method="POST", body="<<secret:TOKEN>>"),
            executor=EjecutorIndiscreto())
        crudo = self.log.read_text(encoding="utf-8")
        for prohibido in ("SUPER_SECRET", "envelope", "ciphertext",
                          "wrapped_dek", "Bearer ", "headers", "cookie"):
            self.assertNotIn(prohibido, crudo, prohibido)

    def test_es_lista_blanca_no_lista_negra(self):
        """Construir el evento con `**kwargs` de quien llama es como acaba un
        header entero en un log del que ya no se saca."""
        fuente = Path("app/assistant/vault/vault_broker.py").read_text(encoding="utf-8")
        cuerpo = fuente[fuente.index("def _auditar"):fuente.index("def validate_request")]
        self.assertIn("registro = {", cuerpo)
        self.assertNotIn("registro.update", cuerpo)
        self.assertNotIn("**campos", cuerpo.split("def _auditar")[1].split(")")[1])


# ───────────────────────────── concurrencia

class ConcurrenciaTests(_ConBroker):

    def _canjear_en_paralelo(self, g, n):
        resultados: list[str] = []
        ejecutados: list[int] = []
        cerrojo = threading.Lock()

        class Ex:
            def execute(self, prepared):
                with cerrojo:
                    ejecutados.append(1)
                return ExecutionResult(code="success", status=200, body="ok")

        def canjear():
            r = SecretBroker(VaultStore(self.db, keyring=VaultKeyring(self.llavero))
                             ).execute_with_secret(
                grant_id=g.id, actor="ana", request=self._req(), executor=Ex())
            with cerrojo:
                resultados.append(r.code if r.execution is None
                                  else f"{r.code}/{r.execution}")

        hilos = [threading.Thread(target=canjear) for _ in range(n)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()
        return resultados, len(ejecutados)

    def test_doble_canje(self):
        _, g = self._grant()
        resultados, ejecuciones = self._canjear_en_paralelo(g, 2)
        self.assertEqual(resultados.count("authorized/success"), 1, resultados)
        self.assertEqual(ejecuciones, 1)

    def test_triple_canje(self):
        _, g = self._grant()
        resultados, ejecuciones = self._canjear_en_paralelo(g, 3)
        self.assertEqual(resultados.count("authorized/success"), 1, resultados)
        self.assertEqual(ejecuciones, 1, "exactamente UNA ejecucion")
        self.assertEqual(resultados.count("grant_consumed"), 2, resultados)

    def test_ocho_canjes(self):
        _, g = self._grant()
        resultados, ejecuciones = self._canjear_en_paralelo(g, 8)
        self.assertEqual(ejecuciones, 1)
        self.assertEqual(resultados.count("authorized/success"), 1)

    def test_una_sola_auditoria_de_exito(self):
        _, g = self._grant()
        self._canjear_en_paralelo(g, 6)
        self.assertEqual(len(self._eventos("secret_use_success")), 1)
        self.assertEqual(len(self._eventos("grant_consumed")), 1)

    def test_canje_y_revoke_del_secreto(self):
        _, g = self._grant()
        codigos: list[str] = []

        def canjear():
            codigos.append(SecretBroker(
                VaultStore(self.db, keyring=VaultKeyring(self.llavero))
            ).execute_with_secret(grant_id=g.id, actor="ana",
                                  request=self._req(),
                                  executor=EjecutorOk()).code)

        def revocar():
            VaultStore(self.db, keyring=VaultKeyring(self.llavero)
                       ).revoke_secret(secret_id=self.secreto.id)

        hilos = [threading.Thread(target=canjear), threading.Thread(target=revocar)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()
        # Gane quien gane, no hay estado incoherente: o autorizo antes de la
        # revocacion, o fallo cerrado.
        self.assertIn(codigos[0], ("authorized", "revoked"))
        self.assertEqual(
            VaultStore(self.db, keyring=VaultKeyring(self.llavero)
                       ).get_secret_metadata(secret_id=self.secreto.id).status,
            "revoked")

    def test_canje_y_bump_de_epoch(self):
        from app.assistant.orchestrator.memory_epoch import bump_actor_permission_epoch

        _, g = self._grant()
        codigos: list[str] = []

        def canjear():
            codigos.append(SecretBroker(
                VaultStore(self.db, keyring=VaultKeyring(self.llavero))
            ).execute_with_secret(grant_id=g.id, actor="ana",
                                  request=self._req(),
                                  executor=EjecutorOk()).code)

        hilos = [threading.Thread(target=canjear),
                 threading.Thread(target=lambda: bump_actor_permission_epoch("ana"))]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()
        self.assertIn(codigos[0], ("authorized", "permission_epoch_conflict"))

    def test_nunca_un_grant_consumido_sin_registro(self):
        _, g = self._grant()
        self._canjear_en_paralelo(g, 5)
        if self.broker.get_grant(g.id).status == "consumed":
            self.assertEqual(len(self._eventos("grant_consumed")), 1)


# ───────────────────────────── semantica de fallo

class QueOcurreCuandoAlgoFallaTests(_ConBroker):

    def test_A_si_el_ejecutor_falla_el_grant_QUEDA_GASTADO(self):
        """Decision explicita: una llamada fallida puede haber tenido efecto,
        y regalar un reintento gratis es regalar una segunda ejecucion."""
        _, g = self._grant()
        r = self.broker.execute_with_secret(grant_id=g.id, actor="ana",
                                            request=self._req(),
                                            executor=EjecutorQueRevienta())
        self.assertEqual(r.execution, "external_error")
        self.assertEqual(self.broker.get_grant(g.id).status, "consumed")
        self.assertEqual(self.broker.execute_with_secret(
            grant_id=g.id, actor="ana", request=self._req(),
            executor=EjecutorOk()).code, "grant_consumed")

    def test_B_consumir_va_antes_de_ejecutar(self):
        """Si el proceso muere entremedio, el grant queda gastado y no hubo
        ejecucion. La auditoria lo dice: `grant_consumed` sin `secret_use_*`."""
        fuente = Path("app/assistant/vault/vault_broker.py").read_text(encoding="utf-8")
        cuerpo = fuente[fuente.index("def execute_with_secret"):
                        fuente.index("def _consumir")]
        self.assertLess(cuerpo.index("self._consumir("), cuerpo.index("self._ejecutar("))
        self.assertIn("grant_consumed", cuerpo)

    def test_D_revocado_entre_el_consumo_y_el_descifrado(self):
        """El grant ya esta gastado, pero NO se ejecuta: el store revalida al
        descifrar y aqui se falla cerrado."""
        _, g = self._grant()
        ex = EjecutorOk()
        original = self.store.with_version_plaintext

        def revocar_y_seguir(**kw):
            self.store.revoke_secret(secret_id=self.secreto.id)
            return original(**kw)

        with patch.object(self.store, "with_version_plaintext",
                          side_effect=revocar_y_seguir):
            r = self.broker.execute_with_secret(grant_id=g.id, actor="ana",
                                                request=self._req(), executor=ex)
        self.assertEqual(r.code, "revoked")
        self.assertEqual(ex.llamadas, [])
        self.assertTrue(self._eventos("secret_use_failed"))

    def test_E_aprobacion_valida_pero_grant_vencido(self):
        _, g = self._grant()
        con = sqlite3.connect(str(self.db))
        con.execute("UPDATE vault_grant SET expires_at='2020-01-01T00:00:00Z' "
                    "WHERE id=?", (g.id,))
        con.commit(); con.close()
        self.assertEqual(self.broker.execute_with_secret(
            grant_id=g.id, actor="ana", request=self._req(),
            executor=EjecutorOk()).code, "expired")
        # Y hay que volver a aprobar: la aprobacion vieja no emite otro grant.
        cod, nuevo = self._grant()
        self.assertEqual(cod, "authorized")
        self.assertNotEqual(nuevo.id, g.id)

    def test_los_codigos_publicos_son_los_de_1030(self):
        self.assertEqual(set(AUTHORIZATION_CODES), {
            "authorized", "approval_required", "not_found", "revoked",
            "expired", "actor_mismatch", "permission_epoch_conflict",
            "purpose_mismatch", "version_conflict", "grant_consumed",
            "invalid_grant"})


# ───────────────────────────── la frontera

class ElBrokerNoDevuelvePlaintextTests(_ConBroker):

    def test_no_existe_ninguna_puerta_al_valor(self):
        publicos = [n for n in dir(SecretBroker) if not n.startswith("_")]
        for prohibido in ("get_plaintext", "resolve_secret", "read_secret",
                          "reveal", "plaintext", "decrypt"):
            self.assertNotIn(prohibido, publicos, prohibido)

    def test_el_resultado_no_tiene_donde_llevarlo(self):
        from dataclasses import fields

        nombres = {f.name for f in fields(__import__(
            "app.assistant.vault.vault_broker", fromlist=["BrokerResult"]
        ).BrokerResult)}
        self.assertNotIn("plaintext", nombres)
        self.assertNotIn("secret", nombres)

    def test_el_plaintext_solo_vive_dentro_de_una_funcion(self):
        fuente = Path("app/assistant/vault/vault_broker.py").read_text(encoding="utf-8")
        cuerpo = fuente[fuente.index("def _ejecutar"):fuente.index("def _sustituir")]
        self.assertIn("def _con_el_secreto(claro: bytes) -> None:", cuerpo)
        # Lo que sale de la funcion interna es el resultado, no el valor.
        self.assertIn("resultado[\"r\"] = limpio", cuerpo)

    def test_ningun_modulo_del_orquestador_importa_el_vault(self):
        """La misma frontera que 10.2.3 impuso, ahora con el broker dentro.
        Arquitectonica, no convencional."""
        import ast

        culpables = []
        for f in Path("app/assistant/orchestrator").rglob("*.py"):
            arbol = ast.parse(f.read_text(encoding="utf-8-sig"))
            for n in ast.walk(arbol):
                mod = (n.module if isinstance(n, ast.ImportFrom) else None) or ""
                nombres = ([a.name for a in n.names]
                           if isinstance(n, ast.Import) else [])
                if "vault" in mod or any("vault" in x for x in nombres):
                    culpables.append(f"{f.name}: {mod or nombres}")
        self.assertEqual(culpables, [])

    def test_nadie_fuera_del_paquete_importa_las_tripas(self):
        import ast

        culpables = []
        for f in Path("app").rglob("*.py"):
            if "assistant/vault" in f.as_posix() or "assistant\\vault" in str(f):
                continue
            arbol = ast.parse(f.read_text(encoding="utf-8-sig"))
            for n in ast.walk(arbol):
                mod = (n.module if isinstance(n, ast.ImportFrom) else None) or ""
                if "vault_keys" in mod or "vault_store" in mod or \
                        "vault_crypto" in mod or "vault_broker" in mod:
                    culpables.append(f"{f.as_posix()}: {mod}")
        self.assertEqual(culpables, [])

    def test_el_broker_no_se_expone_directamente_por_HTTP(self):
        """Actualizado en 10.3.4-A, que SI anadio rutas e interfaz.

        Este test decia "todavia no hay HTTP ni UI" y seguia pasando despues de
        que las hubiera, porque comprobaba nombres de archivo con `*vault*` y
        los de 10.3.4-A se llaman `_secrets.html` y `assistant_secrets.js`. Un
        test que pasa por la razon equivocada es peor que uno que falla: se
        reescribe para afirmar lo que sigue siendo cierto.

        Lo que sigue siendo cierto: el Broker no tiene ninguna ruta propia. La
        capa HTTP (`vault_api`) administra y aprueba; canjear un grant y
        ejecutar con el secreto no esta expuesto a la red.
        """
        import ast

        # El blueprint de conversacion/memoria sigue sin tocar el vault.
        arbol = ast.parse(Path("app/assistant/routes.py").read_text(encoding="utf-8"))
        for n in ast.walk(arbol):
            mod = (n.module if isinstance(n, ast.ImportFrom) else None) or ""
            self.assertNotIn("vault", mod)

        # Y la capa HTTP del vault no puede ejecutar: no importa el canje.
        api = Path("app/assistant/vault/vault_api.py")
        if api.exists():
            fuente = api.read_text(encoding="utf-8")
            for prohibido in ("execute_with_secret", "with_current_plaintext",
                              "with_version_plaintext", "Executor"):
                self.assertNotIn(prohibido, fuente, prohibido)


# ───────────────────────────── tests hostiles

class NingunaSuperficieFiltraElSecretoTests(_ConBroker):

    def _limpio(self, texto: str, donde: str):
        self.assertNotIn("SUPER_SECRET", texto, f"centinela en {donde}")
        self.assertNotIn(CENTINELA.decode(), texto, f"centinela en {donde}")

    def test_el_grant_serializado(self):
        _, g = self._grant()
        self._limpio(json.dumps(g.to_dict(), ensure_ascii=False), "grant")
        self._limpio(repr(g), "repr(grant)")

    def test_la_aprobacion_en_la_base(self):
        self._grant()
        con = sqlite3.connect(str(self.db))
        filas = con.execute("SELECT * FROM vault_approval").fetchall()
        con.close()
        self._limpio(repr([tuple(f) for f in filas]), "vault_approval")

    def test_la_peticion_serializada(self):
        req = self._req()
        self._limpio(json.dumps(req.to_public_dict()), "ExecutionRequest")

    def test_el_resultado_serializado(self):
        _, g = self._grant(request=self._req(method="POST",
                                             body="<<secret:TOKEN>>"))
        r = self.broker.execute_with_secret(
            grant_id=g.id, actor="ana",
            request=self._req(method="POST", body="<<secret:TOKEN>>"),
            executor=EjecutorIndiscreto())
        self._limpio(json.dumps(r.to_dict(), ensure_ascii=False), "BrokerResult")

    def test_la_auditoria_completa(self):
        _, g = self._grant()
        self.broker.execute_with_secret(grant_id=g.id, actor="ana",
                                        request=self._req(),
                                        executor=EjecutorIndiscreto())
        self._limpio(self.log.read_text(encoding="utf-8"), "auditoria")

    def test_los_logs(self):
        buf = io.StringIO()
        h = logging.StreamHandler(buf)
        raiz = logging.getLogger()
        raiz.addHandler(h)
        nivel = raiz.level
        raiz.setLevel(logging.DEBUG)
        try:
            _, g = self._grant()
            self.broker.execute_with_secret(grant_id=g.id, actor="ana",
                                            request=self._req(),
                                            executor=EjecutorIndiscreto())
            logging.getLogger("prueba").info("broker=%r grant=%r req=%r",
                                             self.broker, g, self._req())
        finally:
            raiz.removeHandler(h)
            raiz.setLevel(nivel)
        self._limpio(buf.getvalue(), "logger")

    def test_stdout_y_stderr(self):
        """Un `print` de depuracion olvidado en la ruta caliente acaba en la
        consola del servidor, que es un sitio del que nadie retira nada."""
        import contextlib

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            _, g = self._grant(request=self._req(method="POST",
                                                 body="<<secret:TOKEN>>"))
            r = self.broker.execute_with_secret(
                grant_id=g.id, actor="ana",
                request=self._req(method="POST", body="<<secret:TOKEN>>"),
                executor=EjecutorIndiscreto())
            print(g)
            print(r)
            print(self.broker.get_grant(g.id))
        self._limpio(out.getvalue(), "stdout")
        self._limpio(err.getvalue(), "stderr")

    def test_ni_los_scripts_de_evaluacion_pueden_llegar_al_vault(self):
        """El banco de pruebas corre con credenciales reales del ERP; que
        pudiera importar el Vault seria abrirle la puerta a volcarlo."""
        import ast

        culpables = []
        for base in ("scripts", "tools", "bench"):
            raiz = Path(base)
            if not raiz.exists():
                continue
            for f in raiz.rglob("*.py"):
                crudo = f.read_bytes()
                for cod in ("utf-8-sig", "utf-16", "latin-1"):
                    try:
                        texto = crudo.decode(cod)
                        break
                    except (UnicodeDecodeError, UnicodeError):
                        continue
                else:  # pragma: no cover
                    self.fail(f"no se pudo leer {f}")
                try:
                    arbol = ast.parse(texto)
                except SyntaxError:
                    # Si no se puede analizar, no se le concede el beneficio
                    # de la duda: se mira el texto.
                    if "vault" in texto:
                        culpables.append(f"{f.as_posix()}: (sin parsear)")
                    continue
                for n in ast.walk(arbol):
                    mod = (n.module if isinstance(n, ast.ImportFrom) else None) or ""
                    if "vault" in mod:
                        culpables.append(f"{f.as_posix()}: {mod}")
        self.assertEqual(culpables, [])

    def test_las_excepciones_y_su_traza(self):
        import traceback

        try:
            self.broker.validate_request(
                self._req(headers={"A": "<<secret:NO_EXISTE>>"}))
        except BrokerError as exc:
            self._limpio(str(exc), "str(exc)")
            self._limpio(repr(exc), "repr(exc)")
            self._limpio(traceback.format_exc(), "traceback")

    def test_el_repr_de_la_peticion_preparada(self):
        """Es el unico objeto que SI lleva el secreto. Su repr no lo vuelca."""
        preparada = SecretBroker._sustituir(self._req(), CENTINELA)  # noqa: SLF001
        self.assertIn(CENTINELA.decode(), preparada.headers["Authorization"])
        self._limpio(repr(preparada), "repr(PreparedRequest)")

    def test_el_correlation_id_no_lo_lleva(self):
        _, g = self._grant(correlation_id="corr-" + CENTINELA.decode()[:8])
        # Se acota y se limpia, pero lo que importa: no se inventa un sitio
        # donde meter el secreto.
        self._limpio(json.dumps(g.to_dict()), "correlation_id")

    def test_ninguna_credencial_real_en_juego(self):
        fuente = Path("tests/test_vault_broker_fase1033.py").read_text(encoding="utf-8")
        for real in ("ANDES_LLM_API_KEY", "ANDES_AGENT_SERVICE_TOKEN",
                     "ANDES_DB_SYNC_TOKEN"):
            self.assertNotIn(f'environ["{real}"]', fuente)
        self.assertTrue(CENTINELA.startswith(b"SUPER_SECRET_TEST_VALUE_"))


if __name__ == "__main__":
    unittest.main()
