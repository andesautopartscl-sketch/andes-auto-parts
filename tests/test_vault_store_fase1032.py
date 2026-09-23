"""FASE 10.3.2 — `vault_store`: persistencia y ciclo de vida del Secret Vault.

LO QUE ESTA FASE TIENE QUE DEMOSTRAR

Que se puede guardar un secreto cifrado y recuperarlo tras un reinicio, y que
ninguna de las rutas por las que algo sale de esta maquina se lo lleva.

Tres rutas, verificadas leyendo el repositorio: `gdrive_backup.py` sube
`data/andes.db` a Google Drive, `scripts/sync_db_to_render.py` lo empaqueta
hacia la nube, y ese archivo ademas esta versionado en git bajo `skip-worktree`.
`vault.db` no entra en ninguna, y la KEK no entra en `vault.db`.

EL SECRETO CENTINELA

`SUPER_SECRET_TEST_VALUE_...` se guarda de verdad y despues se busca en el
archivo de base, en los logs, en las excepciones, en los `repr` y en el
inventario. No aparece en ninguno. Nunca se usa una credencial real: ni
`ANDES_LLM_API_KEY`, ni `ANDES_AGENT_SERVICE_TOKEN`, ni nada de Andes.

LO QUE TODAVIA NO EXISTE

Secret Broker, grants, integracion con el Approval Engine, rutas HTTP, interfaz
y secretos reales. El Store no sabe quien pregunta ni con que permiso: usa el
dueño como parte de la identidad del secreto, no como autorizacion.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from app.assistant.vault.vault_crypto import Envelope, VaultCryptoError
from app.assistant.vault.vault_keys import (
    VaultKeyring,
    VaultKeyringError,
    add_key_version,
    describe_key_protection,
    initialize_keyring,
)
from app.assistant.vault.vault_store import (
    PURPOSES,
    VaultStore,
    VaultStoreError,
    vault_db_path,
)

CENTINELA = b"SUPER_SECRET_TEST_VALUE_c41f9a7e2b60d385"
OTRO = b"SUPER_SECRET_TEST_VALUE_segunda_version"


class _ConVault(unittest.TestCase):
    """Cada test, su propio `vault.db` temporal y su propio llavero."""

    CON_LLAVERO = True

    def setUp(self):
        self._prev = {k: os.environ.get(k) for k in
                      ("ANDES_VAULT_DB", "ANDES_VAULT_KEYRING",
                       "ANDES_VAULT_KEK_CURRENT")}
        for k in list(os.environ):
            if k.startswith("ANDES_VAULT_KEK_V"):
                self._prev[k] = os.environ[k]
        self._dir = tempfile.TemporaryDirectory()
        self.raiz = Path(self._dir.name)
        self.db = self.raiz / "vault.db"
        self.llavero = self.raiz / "keys.json"
        os.environ["ANDES_VAULT_DB"] = str(self.db)
        os.environ["ANDES_VAULT_KEYRING"] = str(self.llavero)
        os.environ.pop("ANDES_VAULT_KEK_CURRENT", None)
        for k in list(os.environ):
            if k.startswith("ANDES_VAULT_KEK_V"):
                del os.environ[k]
        if self.CON_LLAVERO:
            initialize_keyring(self.llavero)
        self.store = VaultStore(self.db, keyring=VaultKeyring(self.llavero))

    def tearDown(self):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._dir.cleanup()

    def _crear(self, plaintext=CENTINELA, **kw):
        base = dict(owner_actor="ana", name="MercadoLibre produccion",
                    provider="mercadolibre", purposes=["read_listings"],
                    plaintext=plaintext)
        base.update(kw)
        return self.store.create_secret(**base)

    def _leer(self, meta, owner="ana", store=None):
        s = store or self.store
        return s.with_current_plaintext(secret_id=meta.id, owner_actor=owner,
                                        consumer=lambda b: b)


# ───────────────────────────── A-D: creacion, metadata, versiones

class CrearYRecuperarTests(_ConVault):

    def test_A_crear_cifra_y_devuelve_metadata(self):
        m = self._crear()
        self.assertEqual(m.status, "active")
        self.assertEqual(m.current_version, 1)
        self.assertEqual(m.owner_actor, "ana")
        self.assertEqual(m.purposes, ("read_listings",))

    def test_A_el_plaintext_nunca_llega_a_SQL(self):
        self._crear()
        crudo = self.db.read_bytes()
        self.assertNotIn(CENTINELA, crudo)
        self.assertNotIn(b"SUPER_SECRET", crudo)
        # Ni un trozo reconocible.
        self.assertNotIn(CENTINELA[:14], crudo)

    def test_A_se_recupera_igual(self):
        m = self._crear()
        self.assertEqual(self._leer(m), CENTINELA)

    def test_A_nombres_duplicados_por_dueño(self):
        self._crear()
        with self.assertRaises(VaultStoreError) as e:
            self._crear()
        self.assertEqual(e.exception.code, "duplicate_name")

    def test_A_el_mismo_nombre_para_otro_dueño_si(self):
        self._crear()
        m = self._crear(owner_actor="beto")
        self.assertEqual(m.owner_actor, "beto")

    def test_A_vocabularios_cerrados(self):
        for kw, code in (({"scope": "company"}, "invalid_scope"),
                         ({"provider": "inventado"}, "invalid_provider"),
                         ({"purposes": ["write_all"]}, "invalid_purpose"),
                         ({"purposes": []}, "invalid_purpose")):
            with self.subTest(kw=kw):
                with self.assertRaises(VaultStoreError) as e:
                    self._crear(name=f"x{code}{kw}", **kw)
                self.assertEqual(e.exception.code, code)

    def test_B_metadata_no_trae_sobre_ni_clave(self):
        m = self._crear()
        d = self.store.get_secret_metadata(secret_id=m.id).to_dict()
        texto = json.dumps(d, ensure_ascii=False)
        for prohibido in ("envelope", "ciphertext", "wrapped_dek", "nonce",
                          "SUPER_SECRET"):
            self.assertNotIn(prohibido, texto, prohibido)

    def test_B_el_inventario_no_consulta_la_tabla_de_sobres(self):
        """No es disciplina: el SQL no nombra `vault_secret_version`.

        Se mira el SQL por AST y no el texto del metodo, porque su docstring
        nombra la tabla precisamente para declarar que no la toca — y un grep
        no distingue una promesa de un incumplimiento.
        """
        import ast

        arbol = ast.parse(
            Path("app/assistant/vault/vault_store.py").read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(arbol)
                  if isinstance(n, ast.FunctionDef) and n.name == "list_metadata")
        # Se salta el nodo del docstring en vez de restar su texto:
        # `ast.get_docstring` lo devuelve limpio y no coincide con el crudo.
        cuerpo = fn.body[1:] if (fn.body and isinstance(fn.body[0], ast.Expr)
                                 and isinstance(fn.body[0].value, ast.Constant)
                                 ) else fn.body
        sql = " ".join(
            n.value for stmt in cuerpo for n in ast.walk(stmt)
            if isinstance(n, ast.Constant) and isinstance(n.value, str))
        self.assertIn("FROM vault_secret WHERE", sql)
        self.assertNotIn("vault_secret_version", sql)
        self.assertNotIn("envelope", sql)

    def test_C_varias_versiones_conviven(self):
        m = self._crear()
        self.store.add_version(secret_id=m.id, owner_actor="ana", plaintext=OTRO)
        versiones = self.store.list_versions(secret_id=m.id)
        self.assertEqual([(v.version, v.status) for v in versiones],
                         [(1, "superseded"), (2, "active")])

    def test_C_la_anterior_no_se_destruye(self):
        """Conservarla es lo que permite rollback y trazabilidad."""
        m = self._crear()
        self.store.add_version(secret_id=m.id, owner_actor="ana", plaintext=OTRO)
        v1 = next(v for v in self.store.list_versions(secret_id=m.id)
                  if v.version == 1)
        self.assertEqual(v1.status, "superseded")
        self.assertIsNotNone(v1.superseded_at)
        self.assertIsNone(v1.revoked_at)

    def test_C_supersedida_no_es_lo_mismo_que_revocada(self):
        """Revocar diria que alguien la retiro, y eso no paso."""
        m = self._crear()
        self.store.add_version(secret_id=m.id, owner_actor="ana", plaintext=OTRO)
        self.assertNotEqual(
            next(v for v in self.store.list_versions(secret_id=m.id)
                 if v.version == 1).status, "revoked")

    def test_D_current_apunta_a_la_ultima(self):
        m = self._crear()
        m2 = self.store.add_version(secret_id=m.id, owner_actor="ana",
                                    plaintext=OTRO)
        self.assertEqual(m2.current_version, 2)
        self.assertEqual(self.store.get_current_version(secret_id=m.id).version, 2)
        self.assertEqual(self._leer(m), OTRO)

    def test_D_la_version_current_es_metadata_no_texto_plano(self):
        m = self._crear()
        v = self.store.get_current_version(secret_id=m.id)
        self.assertNotIn("SUPER_SECRET", repr(v))
        self.assertFalse(hasattr(v, "plaintext"))
        self.assertFalse(hasattr(v, "envelope"))


# ───────────────────────────── E-F: revoke y expire

class RevocarYCaducarTests(_ConVault):

    def test_E_revocar_cierra_el_secreto_y_sus_versiones(self):
        m = self._crear()
        r = self.store.revoke_secret(secret_id=m.id)
        self.assertEqual(r.status, "revoked")
        self.assertTrue(all(v.status == "revoked"
                            for v in self.store.list_versions(secret_id=m.id)))

    def test_E_una_revocada_no_se_puede_usar(self):
        m = self._crear()
        self.store.revoke_secret(secret_id=m.id)
        with self.assertRaises(VaultStoreError) as e:
            self._leer(m)
        self.assertEqual(e.exception.code, "revoked")

    def test_E_una_version_revocada_no_puede_ser_current(self):
        """Si la metadata dijera que si, la metadata esta mintiendo."""
        m = self._crear()
        self.store.revoke_secret(secret_id=m.id)
        con = sqlite3.connect(str(self.db))
        con.execute("UPDATE vault_secret SET status='active' WHERE id=?", (m.id,))
        con.commit(); con.close()
        with self.assertRaises(VaultStoreError) as e:
            self.store.get_current_version(secret_id=m.id)
        self.assertEqual(e.exception.code, "no_active_version")

    def test_E_revocar_no_borra_la_historia(self):
        m = self._crear()
        self.store.add_version(secret_id=m.id, owner_actor="ana", plaintext=OTRO)
        self.store.revoke_secret(secret_id=m.id)
        self.assertEqual(len(self.store.list_versions(secret_id=m.id)), 2)

    def test_F_expirar_es_distinto_de_revocar(self):
        m = self._crear()
        r = self.store.expire_secret(secret_id=m.id)
        self.assertEqual(r.status, "expired")
        with self.assertRaises(VaultStoreError) as e:
            self._leer(m)
        self.assertEqual(e.exception.code, "expired")

    def test_F_el_TTL_manda_sobre_el_estado_almacenado(self):
        """Caducado es caducado aunque la columna diga `active`. Misma leccion
        que el panel de memoria en 10.2.4."""
        m = self._crear(expires_at="2020-01-01T00:00:00Z")
        self.assertEqual(
            self.store.get_secret_metadata(secret_id=m.id).status, "active")
        with self.assertRaises(VaultStoreError) as e:
            self._leer(m)
        self.assertEqual(e.exception.code, "expired")

    def test_F_no_se_puede_añadir_version_a_uno_cerrado(self):
        for cerrar, code in ((self.store.revoke_secret, "revoked"),
                             (self.store.expire_secret, "expired")):
            with self.subTest(code=code):
                m = self._crear(name=f"sec-{code}")
                cerrar(secret_id=m.id)
                with self.assertRaises(VaultStoreError) as e:
                    self.store.add_version(secret_id=m.id, owner_actor="ana",
                                           plaintext=OTRO)
                self.assertEqual(e.exception.code, code)

    def test_borrar_exige_confirmacion_explicita(self):
        m = self._crear()
        with self.assertRaises(VaultStoreError) as e:
            self.store.delete_secret(secret_id=m.id)
        self.assertEqual(e.exception.code, "confirmation_required")
        self.assertTrue(self.store.delete_secret(secret_id=m.id, confirm=True))
        with self.assertRaises(VaultStoreError) as e:
            self.store.get_secret_metadata(secret_id=m.id)
        self.assertEqual(e.exception.code, "not_found")

    def test_borrar_se_lleva_las_versiones(self):
        m = self._crear()
        self.store.add_version(secret_id=m.id, owner_actor="ana", plaintext=OTRO)
        self.store.delete_secret(secret_id=m.id, confirm=True)
        con = sqlite3.connect(str(self.db))
        n = con.execute("SELECT COUNT(*) FROM vault_secret_version "
                        "WHERE secret_id=?", (m.id,)).fetchone()[0]
        con.close()
        self.assertEqual(n, 0)


# ───────────────────────────── G: rotacion de KEK

class RotarLaClaveMaestraTests(_ConVault):

    def test_G_rotar_conserva_el_contenido(self):
        m = self._crear()
        add_key_version(self.llavero, key_version=2, make_current=True)
        st = VaultStore(self.db, keyring=VaultKeyring(self.llavero))
        r = st.rotate_key_version(new_key_version=2)
        self.assertEqual((r["candidatas"], r["migradas"], r["fallidas"]), (1, 1, 0))
        self.assertEqual(self._leer(m, store=st), CENTINELA)

    def test_G_el_payload_no_se_reescribe_a_ciegas(self):
        """Rotar mueve 32 bytes de DEK; el criptograma del payload cambia solo
        porque el AAD cambio, no porque se haya vuelto a cifrar con otra clave."""
        m = self._crear()
        add_key_version(self.llavero, key_version=2)
        st = VaultStore(self.db, keyring=VaultKeyring(self.llavero))
        st.rotate_key_version(new_key_version=2)
        v = self.store.list_versions(secret_id=m.id)[0]
        self.assertEqual(v.key_version, 2)

    def test_G_rotacion_parcial_deja_la_base_mixta_pero_legible(self):
        a = self._crear(name="a")
        b = self._crear(name="b")
        add_key_version(self.llavero, key_version=2)
        st = VaultStore(self.db, keyring=VaultKeyring(self.llavero))
        st.rotate_key_version(new_key_version=2, secret_id=a.id)
        self.assertEqual(st.list_versions(secret_id=a.id)[0].key_version, 2)
        self.assertEqual(st.list_versions(secret_id=b.id)[0].key_version, 1)
        # Las dos siguen abriendose: cada fila dice con que KEK esta envuelta.
        self.assertEqual(self._leer(a, store=st), CENTINELA)
        self.assertEqual(self._leer(b, store=st), CENTINELA)

    def test_G_rotar_a_una_clave_inexistente_no_migra_nada(self):
        """Empezar la rotacion sin comprobar la clave destino daria un informe
        que parece exito con cero filas migradas."""
        m = self._crear()
        with self.assertRaises(VaultCryptoError) as e:
            self.store.rotate_key_version(new_key_version=9)
        self.assertEqual(e.exception.code, "unknown_key_version")
        self.assertEqual(self.store.list_versions(secret_id=m.id)[0].key_version, 1)

    def test_G_rotar_dos_veces_es_idempotente(self):
        self._crear()
        add_key_version(self.llavero, key_version=2)
        st = VaultStore(self.db, keyring=VaultKeyring(self.llavero))
        st.rotate_key_version(new_key_version=2)
        segunda = st.rotate_key_version(new_key_version=2)
        self.assertEqual(segunda["candidatas"], 0)

    def test_G_al_retirar_la_clave_vieja_lo_rotado_sigue_vivo(self):
        m = self._crear()
        add_key_version(self.llavero, key_version=2, make_current=True)
        st = VaultStore(self.db, keyring=VaultKeyring(self.llavero))
        st.rotate_key_version(new_key_version=2)
        datos = json.loads(self.llavero.read_text(encoding="utf-8"))
        del datos["keys"]["1"]
        self.llavero.write_text(json.dumps(datos), encoding="utf-8")
        solo_nueva = VaultStore(self.db, keyring=VaultKeyring(self.llavero))
        self.assertEqual(self._leer(m, store=solo_nueva), CENTINELA)


# ───────────────────────────── H: reinicio

class SobrevivirAUnReinicioTests(_ConVault):

    def test_H_reabrir_el_store_en_el_mismo_proceso(self):
        m = self._crear()
        otro = VaultStore(self.db, keyring=VaultKeyring(self.llavero))
        self.assertEqual(self._leer(m, store=otro), CENTINELA)

    def test_H_reabrir_en_un_PROCESO_NUEVO(self):
        """La prueba que importa: guardar, cerrar, reiniciar, resolver la KEK
        desde disco y recuperar lo mismo."""
        m = self._crear()
        guion = (
            "import os, sys\n"
            f"sys.path.insert(0, r'{Path.cwd()}')\n"
            f"os.environ['ANDES_VAULT_DB'] = r'{self.db}'\n"
            f"os.environ['ANDES_VAULT_KEYRING'] = r'{self.llavero}'\n"
            "from app.assistant.vault.vault_store import VaultStore\n"
            "from app.assistant.vault.vault_keys import VaultKeyring\n"
            f"st = VaultStore(r'{self.db}', keyring=VaultKeyring(r'{self.llavero}'))\n"
            f"v = st.with_current_plaintext(secret_id='{m.id}', "
            "owner_actor='ana', consumer=lambda b: b)\n"
            "print('IGUAL' if v == b'" + CENTINELA.decode() + "' else 'DISTINTO')\n"
        )
        out = subprocess.run([sys.executable, "-c", guion], capture_output=True,
                             text=True, timeout=180)
        self.assertEqual(out.returncode, 0, out.stderr[-700:])
        self.assertIn("IGUAL", out.stdout)
        # Y el proceso hijo no imprimio el secreto.
        self.assertNotIn("SUPER_SECRET", out.stdout + out.stderr)


# ───────────────────────────── I-K: la KEK

class SinLaClaveElVaultNoSeAbreTests(_ConVault):
    CON_LLAVERO = False

    def test_I_sin_llavero_el_vault_esta_cerrado_no_vacio(self):
        """Confundirlos convierte una perdida de clave en una perdida de datos
        silenciosa."""
        st = VaultStore(self.db, keyring=VaultKeyring(self.llavero))
        self.assertEqual(st.status()["keyring"], "uninitialized")
        with self.assertRaises(VaultStoreError) as e:
            st.create_secret(owner_actor="ana", name="x",
                             provider="generico", purposes=["read_account"],
                             plaintext=CENTINELA)
        self.assertEqual(e.exception.code, "vault_locked")

    def test_I_con_datos_y_sin_clave_el_estado_es_locked(self):
        initialize_keyring(self.llavero)
        st = VaultStore(self.db, keyring=VaultKeyring(self.llavero))
        st.create_secret(owner_actor="ana", name="x", provider="generico",
                         purposes=["read_account"], plaintext=CENTINELA)
        self.llavero.unlink()
        cerrado = VaultStore(self.db, keyring=VaultKeyring(self.llavero))
        estado = cerrado.status()
        self.assertEqual(estado["state"], "locked")
        self.assertEqual(estado["secrets"], 1, "sabe que HAY secretos")

    def test_I_leer_nunca_genera_una_clave_nueva(self):
        """Generar una KEK sobre un vault con datos convertiria sus secretos en
        ruido indistinguible de 'aun no hay nada'."""
        VaultKeyring(self.llavero)
        VaultStore(self.db, keyring=VaultKeyring(self.llavero)).status()
        self.assertFalse(self.llavero.exists())

    def test_I_inicializar_es_explicito_y_no_sobrescribe(self):
        initialize_keyring(self.llavero)
        with self.assertRaises(VaultKeyringError) as e:
            initialize_keyring(self.llavero)
        self.assertEqual(e.exception.code, "keyring_exists")

    def test_J_clave_equivocada(self):
        initialize_keyring(self.llavero)
        st = VaultStore(self.db, keyring=VaultKeyring(self.llavero))
        m = st.create_secret(owner_actor="ana", name="x", provider="generico",
                             purposes=["read_account"], plaintext=CENTINELA)
        otro = self.raiz / "otras.json"
        initialize_keyring(otro)
        malo = VaultStore(self.db, keyring=VaultKeyring(otro))
        with self.assertRaises(VaultStoreError) as e:
            malo.with_current_plaintext(secret_id=m.id, owner_actor="ana",
                                        consumer=lambda b: b)
        self.assertEqual(e.exception.code, "authentication_failed")

    def _crear_con(self, st):
        return st.create_secret(owner_actor="ana", name="x", provider="generico",
                                purposes=["read_account"], plaintext=CENTINELA)

    def test_K_key_version_desconocida(self):
        """Fila y sobre coinciden en una version que el llavero no tiene."""
        initialize_keyring(self.llavero)
        st = VaultStore(self.db, keyring=VaultKeyring(self.llavero))
        m = self._crear_con(st)
        con = sqlite3.connect(str(self.db))
        sobre = json.loads(con.execute(
            "SELECT envelope_json FROM vault_secret_version WHERE secret_id=?",
            (m.id,)).fetchone()[0])
        sobre["key_version"] = 7
        con.execute("UPDATE vault_secret_version SET key_version=7, "
                    "envelope_json=? WHERE secret_id=?",
                    (json.dumps(sobre), m.id))
        con.commit(); con.close()
        with self.assertRaises(VaultStoreError) as e:
            st.with_current_plaintext(secret_id=m.id, owner_actor="ana",
                                      consumer=lambda b: b)
        self.assertEqual(e.exception.code, "unknown_key_version")

    def test_K_la_fila_y_el_sobre_en_desacuerdo_se_detecta(self):
        """Si la columna dice 7 y el sobre dice 1, alguien toco la fila. Se
        detecta ANTES de intentar descifrar, que es mas util que un fallo de
        autenticacion generico."""
        initialize_keyring(self.llavero)
        st = VaultStore(self.db, keyring=VaultKeyring(self.llavero))
        m = self._crear_con(st)
        con = sqlite3.connect(str(self.db))
        con.execute("UPDATE vault_secret_version SET key_version=7 "
                    "WHERE secret_id=?", (m.id,))
        con.commit(); con.close()
        with self.assertRaises(VaultStoreError) as e:
            st.with_current_plaintext(secret_id=m.id, owner_actor="ana",
                                      consumer=lambda b: b)
        self.assertEqual(e.exception.code, "corrupt_envelope")

    def test_llavero_ilegible_se_distingue_de_ausente(self):
        self.llavero.write_text("{no es json", encoding="utf-8")
        k = VaultKeyring(self.llavero)
        self.assertEqual(k.estado, "unreadable")
        with self.assertRaises(VaultKeyringError) as e:
            k.exigir_abierto()
        self.assertEqual(e.exception.code, "keyring_unreadable")

    def test_el_entorno_puede_aportar_la_clave(self):
        import base64
        from app.assistant.vault.vault_crypto import generate_key

        os.environ["ANDES_VAULT_KEK_V1"] = base64.b64encode(
            generate_key()).decode("ascii")
        try:
            k = VaultKeyring(self.llavero)
            self.assertTrue(k.esta_abierto())
            self.assertEqual(k.versions(), (1,))
            st = VaultStore(self.db, keyring=k)
            m = st.create_secret(owner_actor="ana", name="x",
                                 provider="generico", purposes=["read_account"],
                                 plaintext=CENTINELA)
            self.assertEqual(
                st.with_current_plaintext(secret_id=m.id, owner_actor="ana",
                                          consumer=lambda b: b), CENTINELA)
            self.assertFalse(self.llavero.exists(), "no toco disco")
        finally:
            os.environ.pop("ANDES_VAULT_KEK_V1", None)


# ───────────────────────────── L-N: corrupcion e integridad

class CorrupcionEIntegridadTests(_ConVault):

    def test_L_base_corrupta_no_se_confunde_con_vacia(self):
        self._crear()
        self.db.write_bytes(b"esto no es una base sqlite" * 50)
        st = VaultStore(self.db, keyring=VaultKeyring(self.llavero))
        self.assertEqual(st.status()["state"], "corrupt")

    def test_M_sobre_corrupto(self):
        m = self._crear()
        con = sqlite3.connect(str(self.db))
        con.execute("UPDATE vault_secret_version SET envelope_json='{roto' "
                    "WHERE secret_id=?", (m.id,))
        con.commit(); con.close()
        with self.assertRaises(VaultStoreError) as e:
            self._leer(m)
        self.assertEqual(e.exception.code, "corrupt_envelope")

    def test_M_ciphertext_alterado(self):
        m = self._crear()
        con = sqlite3.connect(str(self.db))
        fila = con.execute("SELECT envelope_json FROM vault_secret_version "
                           "WHERE secret_id=?", (m.id,)).fetchone()[0]
        d = json.loads(fila)
        d["ciphertext"] = "AAAA" + d["ciphertext"][4:]
        con.execute("UPDATE vault_secret_version SET envelope_json=? "
                    "WHERE secret_id=?", (json.dumps(d), m.id))
        con.commit(); con.close()
        with self.assertRaises(VaultStoreError) as e:
            self._leer(m)
        self.assertEqual(e.exception.code, "authentication_failed")

    def _mover_sobre(self, origen_id, destino_id):
        con = sqlite3.connect(str(self.db))
        sobre = con.execute("SELECT envelope_json FROM vault_secret_version "
                            "WHERE secret_id=?", (origen_id,)).fetchone()[0]
        con.execute("UPDATE vault_secret_version SET envelope_json=? "
                    "WHERE secret_id=?", (sobre, destino_id))
        con.commit(); con.close()

    def test_N_mover_un_sobre_a_otro_secreto_falla(self):
        a = self._crear(name="a")
        b = self._crear(name="b", plaintext=OTRO)
        self._mover_sobre(a.id, b.id)
        with self.assertRaises(VaultStoreError) as e:
            self._leer(b)
        self.assertEqual(e.exception.code, "authentication_failed")

    def test_N_cambiar_el_dueño_en_la_fila_falla(self):
        """El caso que mas importa: mover un secreto entre actores por SQL."""
        m = self._crear()
        con = sqlite3.connect(str(self.db))
        con.execute("UPDATE vault_secret SET owner_actor='beto' WHERE id=?", (m.id,))
        con.commit(); con.close()
        with self.assertRaises(VaultStoreError) as e:
            self._leer(m, owner="beto")
        self.assertEqual(e.exception.code, "authentication_failed")

    def test_N_copiar_un_sobre_a_otra_version(self):
        m = self._crear()
        self.store.add_version(secret_id=m.id, owner_actor="ana", plaintext=OTRO)
        con = sqlite3.connect(str(self.db))
        v1 = con.execute("SELECT envelope_json FROM vault_secret_version "
                         "WHERE secret_id=? AND version=1", (m.id,)).fetchone()[0]
        con.execute("UPDATE vault_secret_version SET envelope_json=? "
                    "WHERE secret_id=? AND version=2", (v1, m.id))
        con.commit(); con.close()
        with self.assertRaises(VaultStoreError) as e:
            self._leer(m)
        self.assertEqual(e.exception.code, "authentication_failed")

    def test_N_cambiar_key_version_de_forma_coherente_tambien_falla(self):
        """Fila Y sobre puestos a una clave que SI existe, pero que no es la
        que cifro. El AAD lleva `key_version`, asi que la mentira es
        criptografica y no de esquema: falla al autenticar."""
        m = self._crear()
        add_key_version(self.llavero, key_version=2)
        st = VaultStore(self.db, keyring=VaultKeyring(self.llavero))
        con = sqlite3.connect(str(self.db))
        sobre = json.loads(con.execute(
            "SELECT envelope_json FROM vault_secret_version WHERE secret_id=?",
            (m.id,)).fetchone()[0])
        sobre["key_version"] = 2
        con.execute("UPDATE vault_secret_version SET key_version=2, "
                    "envelope_json=? WHERE secret_id=?", (json.dumps(sobre), m.id))
        con.commit(); con.close()
        with self.assertRaises(VaultStoreError) as e:
            st.with_current_plaintext(secret_id=m.id, owner_actor="ana",
                                      consumer=lambda b: b)
        self.assertEqual(e.exception.code, "authentication_failed")

    def test_N_el_contexto_lo_construye_el_store_no_quien_llama(self):
        """Si alguien pudiera pasar su propio contexto, el AAD no ataria nada."""
        import inspect
        for fn in (VaultStore.with_current_plaintext, VaultStore.create_secret,
                   VaultStore.add_version):
            with self.subTest(fn=fn.__name__):
                params = set(inspect.signature(fn).parameters)
                self.assertNotIn("context", params)
                self.assertNotIn("aad", params)
                self.assertNotIn("encryption_context", params)


# ───────────────────────────── O: concurrencia

class ConcurrenciaTests(_ConVault):

    def _store(self):
        return VaultStore(self.db, keyring=VaultKeyring(self.llavero))

    def test_O_dos_creaciones_del_mismo_nombre(self):
        resultados: list[str] = []

        def crear():
            try:
                self._store().create_secret(
                    owner_actor="ana", name="misma", provider="generico",
                    purposes=["read_account"], plaintext=CENTINELA)
                resultados.append("ok")
            except VaultStoreError as e:
                resultados.append(e.code)

        hilos = [threading.Thread(target=crear) for _ in range(6)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()
        self.assertEqual(resultados.count("ok"), 1, resultados)
        self.assertEqual(len(self._store().list_metadata()), 1)

    def test_O_varias_versiones_concurrentes_no_se_pisan(self):
        m = self._crear()
        errores: list[str] = []

        def añadir(n):
            try:
                self._store().add_version(secret_id=m.id, owner_actor="ana",
                                          plaintext=b"v%d" % n)
            except VaultStoreError as e:
                errores.append(e.code)

        hilos = [threading.Thread(target=añadir, args=(i,)) for i in range(2, 8)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()
        self.assertEqual(errores, [])
        versiones = self._store().list_versions(secret_id=m.id)
        numeros = [v.version for v in versiones]
        self.assertEqual(numeros, sorted(set(numeros)), "sin huecos ni duplicados")
        activas = [v for v in versiones if v.status == "active"]
        self.assertEqual(len(activas), 1, "NUNCA dos versiones current")
        meta = self._store().get_secret_metadata(secret_id=m.id)
        self.assertEqual(meta.current_version, activas[0].version)
        self.assertEqual(meta.current_version, max(numeros))

    def test_O_current_nunca_apunta_a_una_version_inexistente(self):
        m = self._crear()
        for i in range(5):
            self._store().add_version(secret_id=m.id, owner_actor="ana",
                                      plaintext=b"v%d" % i)
        meta = self._store().get_secret_metadata(secret_id=m.id)
        numeros = {v.version for v in self._store().list_versions(secret_id=m.id)}
        self.assertIn(meta.current_version, numeros)

    def test_O_revoke_y_rotate_concurrentes(self):
        m = self._crear()
        add_key_version(self.llavero, key_version=2)
        errores: list[str] = []

        def revocar():
            try:
                self._store().revoke_secret(secret_id=m.id)
            except VaultStoreError as e:
                errores.append(e.code)

        def rotar():
            try:
                self._store().rotate_key_version(new_key_version=2)
            except (VaultStoreError, VaultCryptoError) as e:
                errores.append(getattr(e, "code", "?"))

        hilos = [threading.Thread(target=revocar), threading.Thread(target=rotar)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()
        self.assertEqual(errores, [])
        meta = self._store().get_secret_metadata(secret_id=m.id)
        self.assertEqual(meta.status, "revoked")
        # Ningun estado ambiguo: revocado sigue revocado, rotado o no.
        with self.assertRaises(VaultStoreError) as e:
            self._leer(m, store=self._store())
        self.assertEqual(e.exception.code, "revoked")

    def test_O_leer_durante_una_rotacion(self):
        """WAL en ESTE archivo: leer metadata no espera al lock de escritura."""
        metas = [self._crear(name=f"s{i}") for i in range(12)]
        add_key_version(self.llavero, key_version=2)
        leidos: list[int] = []
        fallos: list[str] = []

        def rotar():
            try:
                self._store().rotate_key_version(new_key_version=2)
            except Exception as e:  # noqa: BLE001
                fallos.append(type(e).__name__)

        def leer():
            try:
                for _ in range(25):
                    leidos.append(len(self._store().list_metadata()))
            except Exception as e:  # noqa: BLE001
                fallos.append(type(e).__name__)

        h = [threading.Thread(target=rotar), threading.Thread(target=leer)]
        for t in h:
            t.start()
        for t in h:
            t.join()
        self.assertEqual(fallos, [])
        self.assertTrue(all(n == len(metas) for n in leidos), set(leidos))

    def test_O_el_archivo_usa_WAL(self):
        self._crear()
        con = sqlite3.connect(str(self.db))
        modo = con.execute("PRAGMA journal_mode").fetchone()[0]
        con.close()
        self.assertEqual(str(modo).lower(), "wal")


# ───────────────────────────── P-Q-V: aislamiento, git, backup

class ElVaultVivePorSuCuentaTests(_ConVault):

    def test_P_no_toca_andes_db(self):
        self._crear()
        self.assertNotEqual(self.db.resolve(), Path("data/andes.db").resolve())
        con = sqlite3.connect("data/andes.db")
        try:
            tablas = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND "
                "(name LIKE '%vault%' OR name LIKE '%secret%')")]
        finally:
            con.close()
        self.assertEqual(tablas, [], "no hay tablas de vault en andes.db")

    def test_P_el_modulo_no_conoce_el_ORM_del_ERP(self):
        import ast

        fuente = Path("app/assistant/vault/vault_store.py").read_text(encoding="utf-8")
        arbol = ast.parse(fuente)
        modulos = set()
        for n in ast.walk(arbol):
            if isinstance(n, ast.Import):
                modulos |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module:
                modulos.add(n.module)
        for prohibido in ("flask", "sqlalchemy"):
            self.assertNotIn(prohibido, {m.split(".")[0] for m in modulos})
        propios = {m for m in modulos if m.startswith("app.")}
        self.assertTrue(all(m.startswith("app.assistant.vault") for m in propios),
                        propios)

    def test_P_la_ruta_por_defecto_es_configurable(self):
        self.assertEqual(vault_db_path(), Path(str(self.db)))
        self.assertEqual(vault_db_path("/otra/ruta.db"), Path("/otra/ruta.db"))
        prev = os.environ.pop("ANDES_VAULT_DB", None)
        try:
            self.assertEqual(vault_db_path(), Path("data/vault.db"))
        finally:
            if prev is not None:
                os.environ["ANDES_VAULT_DB"] = prev

    def test_P_no_reutiliza_el_URI_fijo_de_create_app(self):
        """Por AST: el docstring de `vault_db_path` nombra `create_app()` justo
        para explicar por que no se cuelga de el."""
        import ast

        arbol = ast.parse(
            Path("app/assistant/vault/vault_store.py").read_text(encoding="utf-8"))
        llamados = {n.func.id for n in ast.walk(arbol)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        atributos = {n.attr for n in ast.walk(arbol) if isinstance(n, ast.Attribute)}
        self.assertNotIn("create_app", llamados)
        self.assertNotIn("config", atributos)
        constantes = {n.value for n in ast.walk(arbol)
                      if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        self.assertNotIn("SQLALCHEMY_DATABASE_URI", constantes)

    def test_Q_vault_db_y_llavero_estan_en_gitignore(self):
        reglas = Path(".gitignore").read_text(encoding="utf-8")
        self.assertIn("data/vault.db", reglas)
        self.assertIn("data/.vault_keys.json", reglas)

    def test_Q_git_los_ignora_de_verdad(self):
        """La regla escrita no basta: se le pregunta a git.

        Solo tiene sentido dentro de un repositorio. Fuera de uno —por ejemplo
        al verificar un checkpoint extraido con `git archive`— `check-ignore`
        devuelve 128 por no haber repo, no por no estar ignorado, y afirmar con
        eso seria afirmar sobre otra cosa.
        """
        dentro = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                                capture_output=True, text=True, timeout=60)
        if dentro.returncode != 0:
            self.skipTest("fuera de un repositorio git")
        for nombre in ("data/vault.db", "data/.vault_keys.json",
                       "data/vault.db-wal", "data/vault.db-shm"):
            with self.subTest(nombre=nombre):
                r = subprocess.run(["git", "check-ignore", "-q", nombre],
                                   capture_output=True, timeout=60)
                self.assertEqual(r.returncode, 0, f"{nombre} NO esta ignorado")

    def test_V_el_backup_a_Drive_solo_empaqueta_andes_db(self):
        """No enumera `data/`: nombra el archivo. `vault.db` no puede colarse."""
        fuente = Path("app/utils/gdrive_backup.py").read_text(encoding="utf-8")
        self.assertIn('root / "data" / "andes.db"', fuente)
        self.assertNotIn("vault", fuente.lower())
        self.assertNotIn("glob(", fuente)
        self.assertNotIn("iterdir(", fuente)

    def test_V_el_sync_a_Render_tampoco(self):
        fuente = Path("scripts/sync_db_to_render.py").read_text(encoding="utf-8")
        self.assertNotIn("vault", fuente.lower())
        self.assertIn('arcname="andes.db"', fuente)

    def test_V_ningun_empaquetador_recorre_data(self):
        for ruta in ("app/utils/gdrive_backup.py", "scripts/sync_db_to_render.py"):
            with self.subTest(ruta=ruta):
                f = Path(ruta).read_text(encoding="utf-8")
                self.assertNotIn('data").glob', f)
                self.assertNotIn("write_dir", f)


# ───────────────────────────── R: permisos del sistema de archivos

class PermisosDelLlaveroTests(_ConVault):

    def test_R_el_informe_dice_la_verdad_de_esta_plataforma(self):
        info = describe_key_protection(self.llavero)
        self.assertTrue(info["existe"])
        if os.name == "nt":
            # Medido: os.chmod deja el modo en 0o666. Decir que 0600 protege
            # aqui seria prometer algo que Windows no cumple.
            self.assertIn("no es un control de acceso",
                          info["advertencia"].lower())
            self.assertIn(info["mecanismo"], ("ACL de NTFS", "ninguno"))
        else:
            self.assertEqual(info["mecanismo"], "modo de archivo")
            self.assertTrue(info["efectivo"])
            self.assertEqual(info["modo"], "0o600")

    @unittest.skipIf(os.name == "nt", "el modo POSIX no significa nada en Windows")
    def test_R_un_llavero_legible_por_otros_cierra_el_vault(self):
        os.chmod(self.llavero, 0o644)
        k = VaultKeyring(self.llavero)
        self.assertEqual(k.estado, "insecure")
        with self.assertRaises(VaultKeyringError) as e:
            k.exigir_abierto()
        self.assertEqual(e.exception.code, "keyring_insecure")

    def test_R_el_llavero_se_crea_en_el_directorio_de_datos_no_en_el_repo(self):
        from app.assistant.vault.vault_keys import DEFAULT_KEYRING

        self.assertEqual(DEFAULT_KEYRING, Path("data/.vault_keys.json"))

    def test_R_el_directorio_del_vault_se_crea_si_falta(self):
        hondo = self.raiz / "sub" / "dir" / "vault.db"
        st = VaultStore(hondo, keyring=VaultKeyring(self.llavero))
        st.ensure_schema()
        self.assertTrue(hondo.exists())


# ───────────────────────────── S-T-U: nada filtra el secreto

class NingunaSuperficieFiltraElSecretoTests(_ConVault):

    def _limpio(self, texto: str, donde: str):
        self.assertNotIn("SUPER_SECRET", texto, f"centinela en {donde}")
        self.assertNotIn(CENTINELA.decode(), texto, f"centinela en {donde}")

    def test_S_no_esta_en_el_archivo_de_base(self):
        self._crear()
        self._limpio(self.db.read_bytes().decode("latin-1"), "vault.db")

    def test_S_ni_en_el_WAL(self):
        self._crear()
        for sufijo in ("-wal", "-shm"):
            p = Path(str(self.db) + sufijo)
            if p.exists():
                self._limpio(p.read_bytes().decode("latin-1"), f"vault.db{sufijo}")

    def test_S_ni_en_la_metadata_ni_en_el_inventario(self):
        m = self._crear()
        self._limpio(json.dumps([x.to_dict() for x in self.store.list_metadata()],
                                ensure_ascii=False), "inventario")
        self._limpio(json.dumps(
            self.store.get_secret_metadata(secret_id=m.id).to_dict(),
            ensure_ascii=False), "metadata")

    def test_S_ni_en_el_repr_de_nada(self):
        m = self._crear()
        self._limpio(repr(m), "repr(SecretMetadata)")
        self._limpio(repr(self.store), "repr(VaultStore)")
        self._limpio(repr(VaultKeyring(self.llavero)), "repr(VaultKeyring)")
        self._limpio(repr(self.store.list_versions(secret_id=m.id)), "repr(versiones)")

    def test_S_el_llavero_no_vuelca_material_de_clave(self):
        k = VaultKeyring(self.llavero)
        crudo = json.loads(self.llavero.read_text(encoding="utf-8"))
        clave_b64 = crudo["keys"]["1"]
        self.assertNotIn(clave_b64[:12], repr(k))
        self.assertNotIn(clave_b64[:12], json.dumps(describe_key_protection(self.llavero)))

    def test_T_no_aparece_en_los_logs(self):
        buf = io.StringIO()
        h = logging.StreamHandler(buf)
        raiz = logging.getLogger()
        raiz.addHandler(h)
        nivel = raiz.level
        raiz.setLevel(logging.DEBUG)
        try:
            m = self._crear()
            self._leer(m)
            self.store.add_version(secret_id=m.id, owner_actor="ana",
                                   plaintext=OTRO)
            try:
                self._leer(m, owner="beto")
            except VaultStoreError as exc:
                logging.getLogger("prueba").exception("fallo: %s", exc)
            logging.getLogger("prueba").info("store=%r meta=%r", self.store, m)
        finally:
            raiz.removeHandler(h)
            raiz.setLevel(nivel)
        self._limpio(buf.getvalue(), "logger")

    def test_U_no_aparece_en_excepciones_ni_trazas(self):
        import traceback

        m = self._crear()
        con = sqlite3.connect(str(self.db))
        con.execute("UPDATE vault_secret SET owner_actor='beto' WHERE id=?", (m.id,))
        con.commit(); con.close()
        try:
            self._leer(m, owner="beto")
        except VaultStoreError as exc:
            self._limpio(str(exc), "str(exc)")
            self._limpio(repr(exc), "repr(exc)")
            tb = traceback.format_exc()
        self._limpio(tb, "traceback")
        # Sin excepcion encadenada de la libreria, que si lleva buffers.
        self.assertNotIn("During handling", tb)
        self.assertNotIn("InvalidTag", tb)

    def test_U_los_mensajes_son_fijos_no_interpolados(self):
        fuente = Path("app/assistant/vault/vault_store.py").read_text(encoding="utf-8")
        bloque = fuente[fuente.index("_MENSAJES = {"):fuente.index("def __init__(self, code")]
        self.assertNotIn("%s", bloque)
        self.assertNotIn('f"', bloque)

    def test_no_hay_ninguna_credencial_real_en_juego(self):
        """Solo secretos sinteticos. Ni la clave del LLM ni el token M2M."""
        fuente = Path("tests/test_vault_store_fase1032.py").read_text(encoding="utf-8")
        for real in ("ANDES_LLM_API_KEY", "ANDES_AGENT_SERVICE_TOKEN",
                     "ANDES_DB_SYNC_TOKEN"):
            self.assertNotIn(f'environ["{real}"]', fuente)
            self.assertNotIn(f"environ.get(\"{real}\")", fuente)
        self.assertTrue(CENTINELA.startswith(b"SUPER_SECRET_TEST_VALUE_"))


# ───────────────────────────── la frontera que 10.3.3 heredara

class ElStoreNoAbreUnaPuertaAlTextoPlanoTests(_ConVault):

    def test_no_existe_get_plaintext(self):
        publicos = [n for n in dir(VaultStore) if not n.startswith("_")]
        for prohibido in ("get_plaintext", "read_secret", "decrypt_secret",
                          "reveal", "plaintext"):
            self.assertNotIn(prohibido, publicos, prohibido)

    def test_el_unico_camino_es_un_consumer(self):
        import inspect

        sig = inspect.signature(VaultStore.with_current_plaintext)
        self.assertIn("consumer", sig.parameters)
        m = self._crear()
        # Lo que vuelve es lo que devuelve el consumer, no el secreto.
        self.assertEqual(
            self.store.with_current_plaintext(
                secret_id=m.id, owner_actor="ana", consumer=len),
            len(CENTINELA))

    def test_el_store_no_decide_autorizacion(self):
        """Quien puede usar que es del Broker. Aqui el dueño es identidad."""
        import ast

        arbol = ast.parse(Path("app/assistant/vault/vault_store.py")
                          .read_text(encoding="utf-8"))
        ids = {n.id for n in ast.walk(arbol) if isinstance(n, ast.Name)}
        ids |= {n.name for n in ast.walk(arbol)
                if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        for prohibido in ("grant", "approve", "authorize", "permission_check",
                          "session", "request"):
            self.assertNotIn(prohibido, ids, prohibido)

    def test_el_store_no_conoce_ni_al_broker_ni_a_la_capa_HTTP(self):
        """La direccion de la dependencia es parte del diseno.

        10.3.3 anadio el Broker y 10.3.4-A la capa HTTP. Los dos usan el Store;
        el Store no sabe que existen. Si algun dia lo supiera, la separacion
        entre "guardar cifrado" y "decidir quien puede usarlo" habria
        desaparecido sin que nadie lo decidiera.
        """
        import ast

        arbol = ast.parse(Path("app/assistant/vault/vault_store.py")
                          .read_text(encoding="utf-8"))
        for n in ast.walk(arbol):
            mod = (n.module if isinstance(n, ast.ImportFrom) else None) or ""
            self.assertNotIn("broker", mod)
            self.assertNotIn("vault_api", mod)
            self.assertNotIn("flask", mod)
        # Y no conoce nada de HTTP: ni sesion, ni peticion, ni respuesta.
        codigo = Path("app/assistant/vault/vault_store.py").read_text(encoding="utf-8")
        for prohibido in ("session", "jsonify", "Blueprint", "login_required"):
            self.assertNotIn(prohibido, codigo, prohibido)

    def test_ningun_modulo_del_orquestador_importa_el_vault(self):
        """La misma frontera que 10.2.3 impuso para el motor de aprobacion."""
        import ast

        culpables = []
        for f in Path("app/assistant/orchestrator").glob("*.py"):
            arbol = ast.parse(f.read_text(encoding="utf-8"))
            for n in ast.walk(arbol):
                mod = (n.module if isinstance(n, ast.ImportFrom) else None) or ""
                nombres = ([a.name for a in n.names]
                           if isinstance(n, ast.Import) else [])
                if "vault" in mod or any("vault" in x for x in nombres):
                    culpables.append(f.name)
        self.assertEqual(culpables, [])


if __name__ == "__main__":
    unittest.main()
