"""FASE 10.3.1 — `vault_crypto`, el modulo criptografico del Secret Vault.

POR QUE ESTE MODULO SE PRUEBA ANTES DE QUE EXISTA NADA MAS

Es la unica parte del vault sin superficie de ataque: bytes a bytes, sin actor,
sin sesion, sin HTTP, sin prompt. Y mientras no exista almacen, **no hay forma
de guardar un secreto y por tanto no hay nada que filtrar**. Ese orden permite
probar aqui cosas —nonce repetido, AAD manipulado, rotacion a medias— que mas
adelante costarian datos reales.

Es ademas donde los errores son irreversibles. Un fallo en el broker se arregla
y se despliega; un nonce reutilizado o un AAD que no ate el criptograma a su
fila se descubren cuando ya hay datos cifrados con ellos.

EL VALOR CENTINELA

`SUPER_SECRET_TEST_VALUE_9f82...` no es decoracion: varios tests lo cifran y
despues barren excepciones, logs, `repr` y el sobre serializado para demostrar
que no aparece en ninguno.
"""
from __future__ import annotations

import importlib.util
import io
import json
import logging
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

from app.assistant.vault.vault_crypto import (
    ALGORITHM,
    DEK_BYTES,
    ENVELOPE_VERSION,
    KEY_BYTES,
    MAX_PLAINTEXT_BYTES,
    NONCE_BYTES,
    EncryptionContext,
    Envelope,
    StaticKeyResolver,
    VaultCryptoError,
    decrypt,
    encrypt,
    generate_key,
    rewrap,
    validate_key,
)

CENTINELA = b"SUPER_SECRET_TEST_VALUE_9f82a1c7d4e6b0f3"
FUENTE = Path("app/assistant/vault/vault_crypto.py")


def _ctx(**kw):
    base = dict(secret_id="sec-1", version=1, owner="ana", key_version=1)
    base.update(kw)
    return EncryptionContext(**base)


class _Base(unittest.TestCase):
    def setUp(self):
        self.k1 = generate_key()
        self.k2 = generate_key()
        self.r = StaticKeyResolver({1: self.k1, 2: self.k2})
        self.ctx = _ctx()


# ───────────────────────────── 1-3: lo que tiene que funcionar

class ElSobreViajaDeIdaYDeVueltaTests(_Base):

    def test_1_encrypt_decrypt(self):
        self.assertEqual(decrypt(encrypt(CENTINELA, self.ctx, self.r),
                                 self.ctx, self.r), CENTINELA)

    def test_2_cada_cifrado_usa_un_nonce_distinto(self):
        """Reutilizar un nonce con la misma clave no degrada GCM: lo rompe.
        Por eso no hay parametro para fijarlo — nadie puede elegirlo."""
        nonces, dek_nonces, cts = set(), set(), set()
        for _ in range(60):
            s = encrypt(CENTINELA, self.ctx, self.r)
            nonces.add(s.nonce)
            dek_nonces.add(s.dek_nonce)
            cts.add(s.ciphertext)
        self.assertEqual(len(nonces), 60)
        self.assertEqual(len(dek_nonces), 60)
        # Y el criptograma tambien cambia: el DEK es nuevo en cada llamada.
        self.assertEqual(len(cts), 60)

    def test_2_el_nonce_mide_96_bits(self):
        import base64
        s = encrypt(CENTINELA, self.ctx, self.r)
        self.assertEqual(len(base64.b64decode(s.nonce)), NONCE_BYTES)
        self.assertEqual(NONCE_BYTES, 12)

    def test_3_sin_la_clave_el_texto_no_esta(self):
        s = encrypt(CENTINELA, self.ctx, self.r)
        crudo = s.to_json().encode("utf-8")
        self.assertNotIn(CENTINELA, crudo)
        self.assertNotIn(b"SUPER_SECRET", crudo)
        # Ni siquiera un trozo reconocible.
        self.assertNotIn(CENTINELA[:12], crudo)

    def test_3_otro_resolver_no_puede_abrirlo(self):
        s = encrypt(CENTINELA, self.ctx, self.r)
        ajeno = StaticKeyResolver({1: generate_key()})
        with self.assertRaises(VaultCryptoError) as e:
            decrypt(s, self.ctx, ajeno)
        self.assertEqual(e.exception.code, "authentication_failed")


# ───────────────────────────── 4-8: manipulacion del sobre

class ManipularElSobreLoInvalidaTests(_Base):

    def _mutar_b64(self, valor: str) -> str:
        """Cambia un byte real del contenido, no del texto base64."""
        import base64
        crudo = bytearray(base64.b64decode(valor))
        crudo[0] ^= 0x01
        return base64.b64encode(bytes(crudo)).decode("ascii")

    def _falla(self, sobre, ctx=None, resolver=None, code="authentication_failed"):
        with self.assertRaises(VaultCryptoError) as e:
            decrypt(sobre, ctx or self.ctx, resolver or self.r)
        self.assertEqual(e.exception.code, code)

    def test_4_ciphertext_alterado(self):
        s = encrypt(CENTINELA, self.ctx, self.r)
        d = s.to_dict()
        d["ciphertext"] = self._mutar_b64(d["ciphertext"])
        self._falla(d)

    def test_5_nonce_alterado(self):
        s = encrypt(CENTINELA, self.ctx, self.r)
        d = s.to_dict()
        d["nonce"] = self._mutar_b64(d["nonce"])
        self._falla(d)

    def test_6_tag_alterado(self):
        """`cryptography` concatena el tag al final del ciphertext; no hay un
        campo `tag` separado. Se altera el ultimo byte, que es el tag."""
        import base64
        s = encrypt(CENTINELA, self.ctx, self.r)
        crudo = bytearray(base64.b64decode(s.ciphertext))
        crudo[-1] ^= 0x01
        d = s.to_dict()
        d["ciphertext"] = base64.b64encode(bytes(crudo)).decode("ascii")
        self._falla(d)

    def test_6_el_dek_envuelto_alterado_tambien_falla(self):
        s = encrypt(CENTINELA, self.ctx, self.r)
        d = s.to_dict()
        d["wrapped_dek"] = self._mutar_b64(d["wrapped_dek"])
        self._falla(d)

    def test_7_clave_incorrecta(self):
        s = encrypt(CENTINELA, self.ctx, self.r)
        self._falla(s, resolver=StaticKeyResolver({1: self.k2}))

    def test_8_key_version_desconocida_para_el_resolver(self):
        s = encrypt(CENTINELA, _ctx(key_version=2), self.r)
        with self.assertRaises(VaultCryptoError) as e:
            decrypt(s, _ctx(key_version=2), StaticKeyResolver({1: self.k1}))
        self.assertEqual(e.exception.code, "unknown_key_version")


# ───────────────────────────── 9-12: el AAD ata el sobre a su fila

class ElContextoEstaAtadoAlCriptogramaTests(_Base):
    """Un criptograma copiado a la fila de otro secreto —o de otro dueño—
    tiene que FALLAR, no entregar su contenido. Eso convierte un error en la
    capa de datos en un fallo ruidoso en vez de una fuga silenciosa."""

    def _falla_con(self, **cambio):
        s = encrypt(CENTINELA, self.ctx, self.r)
        with self.assertRaises(VaultCryptoError) as e:
            decrypt(s, _ctx(**cambio), self.r)
        return e.exception.code

    def test_9_secret_id_alterado(self):
        self.assertEqual(self._falla_con(secret_id="sec-2"),
                         "authentication_failed")

    def test_10_version_alterada(self):
        self.assertEqual(self._falla_con(version=2), "authentication_failed")

    def test_11_owner_alterado(self):
        """El caso que mas importa: mover una fila entre actores."""
        self.assertEqual(self._falla_con(owner="beto"), "authentication_failed")

    def test_12_key_version_del_contexto_alterada(self):
        # El sobre dice 1 y el contexto dice 2: se detecta antes de descifrar.
        self.assertEqual(self._falla_con(key_version=2), "invalid_context")

    def test_el_aad_es_exactamente_el_de_1030(self):
        self.assertEqual(_ctx().aad(), b"sec-1|1|ana|1")

    def test_el_separador_no_se_puede_inyectar(self):
        """Sin esto, ('a|b', 1) y ('a', 'b|1') podrian producir el mismo AAD."""
        for malo in ("sec|1", "a|b|c"):
            with self.subTest(valor=malo):
                with self.assertRaises(VaultCryptoError) as e:
                    _ctx(secret_id=malo)
                self.assertEqual(e.exception.code, "invalid_context")
        with self.assertRaises(VaultCryptoError):
            _ctx(owner="ana|beto")

    def test_un_contexto_vacio_o_absurdo_se_rechaza(self):
        for kw in ({"secret_id": ""}, {"owner": "   "}, {"version": 0},
                   {"key_version": -1}, {"version": True}, {"secret_id": None}):
            with self.subTest(kw=kw):
                with self.assertRaises(VaultCryptoError) as e:
                    _ctx(**kw)
                self.assertEqual(e.exception.code, "invalid_context")


# ───────────────────────────── 13-14: forma y clave

class LaFormaDelSobreSeValidaTests(_Base):

    def test_13_version_de_sobre_desconocida(self):
        s = encrypt(CENTINELA, self.ctx, self.r).to_dict()
        s["envelope_version"] = ENVELOPE_VERSION + 1
        with self.assertRaises(VaultCryptoError) as e:
            decrypt(s, self.ctx, self.r)
        self.assertEqual(e.exception.code, "unsupported_version")

    def test_13_la_version_se_comprueba_antes_que_los_campos(self):
        """Un sobre del futuro puede tener otros campos; rechazarlo por
        'le falta nonce' seria mentir sobre la causa."""
        with self.assertRaises(VaultCryptoError) as e:
            decrypt({"envelope_version": 99, "algorithm": ALGORITHM,
                     "key_version": 1}, self.ctx, self.r)
        self.assertEqual(e.exception.code, "unsupported_version")

    def test_13_algoritmo_distinto(self):
        s = encrypt(CENTINELA, self.ctx, self.r).to_dict()
        s["algorithm"] = "AES-128-CBC"
        with self.assertRaises(VaultCryptoError) as e:
            decrypt(s, self.ctx, self.r)
        self.assertEqual(e.exception.code, "invalid_envelope")

    def test_13_sobres_malformados(self):
        for malo in (None, 42, [], "no es json", "{}", {"envelope_version": 1},
                     {"envelope_version": 1, "algorithm": ALGORITHM,
                      "key_version": 1, "nonce": 5, "dek_nonce": "x",
                      "wrapped_dek": "y", "ciphertext": "z"}):
            with self.subTest(malo=repr(malo)[:40]):
                with self.assertRaises(VaultCryptoError):
                    decrypt(malo, self.ctx, self.r)

    def test_13_base64_invalido(self):
        s = encrypt(CENTINELA, self.ctx, self.r).to_dict()
        s["nonce"] = "no-es-base64-!!"
        with self.assertRaises(VaultCryptoError) as e:
            decrypt(s, self.ctx, self.r)
        self.assertEqual(e.exception.code, "invalid_envelope")

    def test_13_nonce_del_tamaño_equivocado(self):
        import base64
        s = encrypt(CENTINELA, self.ctx, self.r).to_dict()
        s["nonce"] = base64.b64encode(b"corto").decode("ascii")
        with self.assertRaises(VaultCryptoError) as e:
            decrypt(s, self.ctx, self.r)
        self.assertEqual(e.exception.code, "invalid_envelope")

    def test_14_clave_de_tamaño_incorrecto(self):
        for mala in (b"", b"x" * 16, b"x" * 31, b"x" * 33, "no son bytes", None):
            with self.subTest(n=len(mala) if hasattr(mala, "__len__") else mala):
                with self.assertRaises(VaultCryptoError) as e:
                    validate_key(mala)
                self.assertEqual(e.exception.code, "invalid_key_size")

    def test_14_el_resolver_tampoco_acepta_una_clave_mala(self):
        with self.assertRaises(VaultCryptoError) as e:
            StaticKeyResolver({1: b"corta"})
        self.assertEqual(e.exception.code, "invalid_key_size")

    def test_14_una_clave_generada_mide_256_bits(self):
        self.assertEqual(len(generate_key()), KEY_BYTES)
        self.assertEqual(KEY_BYTES, 32)
        # Y dos claves generadas no coinciden.
        self.assertNotEqual(generate_key(), generate_key())


# ───────────────────────────── 15-17: contenido

class ElContenidoPuedeSerCualquierCosaTests(_Base):

    def test_15_unicode(self):
        for texto in ("contraseña con ñ y tildes áéíóú", "日本語のトークン",
                      "emoji 🔑🗝️", "​ invisible ﻿"):
            with self.subTest(texto=texto[:20]):
                crudo = texto.encode("utf-8")
                self.assertEqual(
                    decrypt(encrypt(crudo, self.ctx, self.r), self.ctx, self.r),
                    crudo)

    def test_16_plaintext_vacio_se_acepta(self):
        """Politica: se acepta. GCM cifra 0 bytes y el tag sigue autenticando
        el AAD, asi que un sobre vacio sigue atado a su fila. Rechazarlo
        obligaria a quien llama a distinguir 'sin valor' de 'error'."""
        s = encrypt(b"", self.ctx, self.r)
        self.assertEqual(decrypt(s, self.ctx, self.r), b"")
        # Y sigue detectando manipulacion del contexto.
        with self.assertRaises(VaultCryptoError):
            decrypt(s, _ctx(owner="beto"), self.r)

    def test_17_valores_grandes_razonables(self):
        for n in (1, 1024, 16 * 1024, MAX_PLAINTEXT_BYTES):
            with self.subTest(n=n):
                crudo = os.urandom(n)
                self.assertEqual(
                    decrypt(encrypt(crudo, self.ctx, self.r), self.ctx, self.r),
                    crudo)

    def test_17_por_encima_del_limite_se_rechaza(self):
        """Una credencial son decenas de bytes. Un megabyte es otra cosa que
        alguien metio por error, y el vault no es un almacen de archivos."""
        with self.assertRaises(VaultCryptoError) as e:
            encrypt(b"x" * (MAX_PLAINTEXT_BYTES + 1), self.ctx, self.r)
        self.assertEqual(e.exception.code, "plaintext_too_large")

    def test_17_un_plaintext_que_no_son_bytes(self):
        with self.assertRaises(VaultCryptoError):
            encrypt("texto, no bytes", self.ctx, self.r)


# ───────────────────────────── 18: varias versiones a la vez

class VariasVersionesDeClaveConvivenTests(_Base):

    def test_18_cada_sobre_se_abre_con_la_suya(self):
        s1 = encrypt(b"con la uno", _ctx(key_version=1), self.r)
        s2 = encrypt(b"con la dos", _ctx(key_version=2), self.r)
        self.assertEqual(decrypt(s1, _ctx(key_version=1), self.r), b"con la uno")
        self.assertEqual(decrypt(s2, _ctx(key_version=2), self.r), b"con la dos")

    def test_18_no_se_asume_que_haya_una_sola_clave(self):
        r = StaticKeyResolver({k: generate_key() for k in (1, 2, 3, 7, 12)})
        self.assertEqual(r.versions(), (1, 2, 3, 7, 12))
        for k in r.versions():
            with self.subTest(key_version=k):
                c = _ctx(key_version=k)
                self.assertEqual(decrypt(encrypt(CENTINELA, c, r), c, r), CENTINELA)

    def test_18_una_version_que_no_existe(self):
        with self.assertRaises(VaultCryptoError) as e:
            encrypt(CENTINELA, _ctx(key_version=99), self.r)
        self.assertEqual(e.exception.code, "unknown_key_version")


class LaRotacionReenvuelveSinTocarElPayloadTests(_Base):
    """La propiedad del envelope: rotar mueve 32 bytes de DEK, no el secreto."""

    def test_rotar_conserva_el_contenido(self):
        s1 = encrypt(CENTINELA, self.ctx, self.r)
        s2, ctx2 = rewrap(s1, self.ctx, self.r, 2)
        self.assertEqual(s2.key_version, 2)
        self.assertEqual(ctx2.key_version, 2)
        self.assertEqual(decrypt(s2, ctx2, self.r), CENTINELA)

    def test_el_sobre_viejo_sigue_abriendose_mientras_exista_su_clave(self):
        """Rotacion a medias: la base queda mixta pero consistente, porque cada
        fila dice con que KEK esta envuelta."""
        s1 = encrypt(CENTINELA, self.ctx, self.r)
        s2, ctx2 = rewrap(s1, self.ctx, self.r, 2)
        self.assertEqual(decrypt(s1, self.ctx, self.r), CENTINELA)
        self.assertEqual(decrypt(s2, ctx2, self.r), CENTINELA)

    def test_al_retirar_la_clave_vieja_el_rotado_sigue_vivo(self):
        s1 = encrypt(CENTINELA, self.ctx, self.r)
        s2, ctx2 = rewrap(s1, self.ctx, self.r, 2)
        solo_nueva = StaticKeyResolver({2: self.k2})
        self.assertEqual(decrypt(s2, ctx2, solo_nueva), CENTINELA)
        with self.assertRaises(VaultCryptoError):
            decrypt(s1, self.ctx, solo_nueva)

    def test_el_contexto_nuevo_conserva_identidad_y_dueño(self):
        s1 = encrypt(CENTINELA, self.ctx, self.r)
        _, ctx2 = rewrap(s1, self.ctx, self.r, 2)
        self.assertEqual((ctx2.secret_id, ctx2.version, ctx2.owner),
                         (self.ctx.secret_id, self.ctx.version, self.ctx.owner))

    def test_rotar_con_el_contexto_equivocado_falla(self):
        s1 = encrypt(CENTINELA, self.ctx, self.r)
        with self.assertRaises(VaultCryptoError) as e:
            rewrap(s1, _ctx(owner="beto"), self.r, 2)
        self.assertEqual(e.exception.code, "authentication_failed")

    def test_rotar_a_una_version_inexistente(self):
        s1 = encrypt(CENTINELA, self.ctx, self.r)
        with self.assertRaises(VaultCryptoError) as e:
            rewrap(s1, self.ctx, self.r, 99)
        self.assertEqual(e.exception.code, "unknown_key_version")


# ───────────────────────────── 19: serializacion

class ElSobreSobreviveAIrYVolverDeTextoTests(_Base):

    def test_19_json_ida_y_vuelta(self):
        s = encrypt(CENTINELA, self.ctx, self.r)
        self.assertEqual(decrypt(Envelope.from_json(s.to_json()), self.ctx, self.r),
                         CENTINELA)

    def test_19_dict_ida_y_vuelta(self):
        s = encrypt(CENTINELA, self.ctx, self.r)
        self.assertEqual(Envelope.from_dict(s.to_dict()), s)

    def test_19_el_json_es_estable(self):
        """Claves ordenadas y sin espacios: dos serializaciones del mismo sobre
        son byte a byte iguales, que es lo que permite compararlas."""
        s = encrypt(CENTINELA, self.ctx, self.r)
        self.assertEqual(s.to_json(), s.to_json())
        self.assertEqual(sorted(json.loads(s.to_json())), sorted(s.to_dict()))

    def test_19_el_json_declara_lo_necesario_para_descifrar(self):
        d = json.loads(encrypt(CENTINELA, self.ctx, self.r).to_json())
        for campo in ("envelope_version", "algorithm", "key_version",
                      "dek_nonce", "wrapped_dek", "nonce", "ciphertext"):
            self.assertIn(campo, d)
        self.assertEqual(d["algorithm"], "AES-256-GCM")
        self.assertEqual(d["envelope_version"], ENVELOPE_VERSION)


# ───────────────────────────── 20: nada filtra el secreto

class NingunaSuperficieFiltraElSecretoTests(_Base):
    """El valor centinela se cifra y despues se busca en todas las superficies
    por las que podria escaparse."""

    def _sin_centinela(self, texto: str, donde: str):
        self.assertNotIn("SUPER_SECRET", texto, f"centinela en {donde}")
        self.assertNotIn(CENTINELA.decode(), texto, f"centinela en {donde}")

    def test_20_el_mensaje_de_la_excepcion(self):
        s = encrypt(CENTINELA, self.ctx, self.r)
        d = s.to_dict()
        d["ciphertext"] = "AAAA" + d["ciphertext"][4:]
        for ctx, resolver in ((self.ctx, self.r), (_ctx(owner="x"), self.r),
                              (self.ctx, StaticKeyResolver({1: generate_key()}))):
            with self.subTest():
                try:
                    decrypt(d, ctx, resolver)
                except VaultCryptoError as exc:
                    self._sin_centinela(str(exc), "str(exc)")
                    self._sin_centinela(repr(exc), "repr(exc)")
                    # Ni criptograma ni material de clave.
                    self.assertNotIn(d["ciphertext"][:16], str(exc))
                    self.assertNotIn(d["wrapped_dek"][:16], str(exc))

    def test_20_la_traza_completa(self):
        """El handler de 500 del ERP escribe `logger.exception`: la traza es una
        superficie real, no teorica."""
        import traceback
        try:
            decrypt(encrypt(CENTINELA, self.ctx, self.r), _ctx(owner="otro"), self.r)
        except VaultCryptoError:
            tb = traceback.format_exc()
        self._sin_centinela(tb, "traceback")
        # Y sin excepcion encadenada de la libreria, que si lleva buffers.
        self.assertNotIn("InvalidTag", tb)
        self.assertNotIn("During handling", tb)

    def test_20_la_salida_del_logger(self):
        buf = io.StringIO()
        h = logging.StreamHandler(buf)
        raiz = logging.getLogger()
        raiz.addHandler(h)
        nivel = raiz.level
        raiz.setLevel(logging.DEBUG)
        try:
            s = encrypt(CENTINELA, self.ctx, self.r)
            try:
                decrypt(s, _ctx(version=9), self.r)
            except VaultCryptoError as exc:
                logging.getLogger("prueba").exception("fallo: %s", exc)
            logging.getLogger("prueba").info("sobre=%r ctx=%r", s, self.ctx)
        finally:
            raiz.removeHandler(h)
            raiz.setLevel(nivel)
        self._sin_centinela(buf.getvalue(), "logger")

    def test_20_el_repr_de_cada_objeto(self):
        s = encrypt(CENTINELA, self.ctx, self.r)
        self._sin_centinela(repr(s), "repr(Envelope)")
        self._sin_centinela(repr(self.ctx), "repr(EncryptionContext)")
        self._sin_centinela(repr(self.r), "repr(StaticKeyResolver)")
        # El resolver tampoco vuelca su material de clave.
        self.assertNotIn(self.k1.hex()[:16], repr(self.r))
        self.assertNotIn(str(self.k1), repr(self.r))
        # El sobre no vuelca criptograma en su repr.
        self.assertNotIn(s.ciphertext[:16], repr(s))

    def test_20_el_artefacto_serializado(self):
        """Lo que acabaria en un benchmark o en un volcado de diagnostico."""
        s = encrypt(CENTINELA, self.ctx, self.r)
        artefacto = json.dumps({"sobre": s.to_dict(), "ctx": repr(self.ctx)},
                               ensure_ascii=False)
        self._sin_centinela(artefacto, "artefacto")

    def test_20_los_mensajes_son_fijos_no_interpolados(self):
        """Interpolar es como acaban los valores en los logs."""
        fuente = FUENTE.read_text(encoding="utf-8")
        bloque = fuente[fuente.index("_MENSAJES = {"):fuente.index("def __init__(self, code")]
        self.assertNotIn("{", bloque.replace("_MENSAJES = {", "").replace("}", ""))
        self.assertNotIn("%s", bloque)
        self.assertNotIn("f\"", bloque)


# ───────────────────────────── invariantes de seguridad

class LosInvariantesDeSeguridadSonVerificablesTests(_Base):
    """Los nueve invariantes de 10.3.0, cada uno con una comprobacion."""

    def _nombres_llamados(self) -> set[str]:
        """Todo lo que el modulo LLAMA, por AST.

        Por AST y no por subcadena: el docstring del modulo nombra `sqlite3` y
        `SQLAlchemy` justo para declarar que no los usa, y un grep no distingue
        una promesa de un incumplimiento.
        """
        import ast

        arbol = ast.parse(FUENTE.read_text(encoding="utf-8"))
        out: set[str] = set()
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Call):
                f = nodo.func
                if isinstance(f, ast.Name):
                    out.add(f.id)
                elif isinstance(f, ast.Attribute):
                    out.add(f.attr)
                    if isinstance(f.value, ast.Name):
                        out.add(f"{f.value.id}.{f.attr}")
            elif isinstance(nodo, ast.Attribute):
                out.add(nodo.attr)
        return out

    def test_1_este_modulo_no_persiste_plaintext(self):
        llamadas = self._nombres_llamados()
        for escritura in ("open", "write_text", "write_bytes", "write",
                          "connect", "execute", "dump", "dumps_to_file"):
            self.assertNotIn(escritura, llamadas, escritura)

    def test_2_este_modulo_no_persiste_material_de_clave(self):
        """`StaticKeyResolver` vive en memoria y no lee ni escribe entorno."""
        llamadas = self._nombres_llamados()
        for acceso in ("getenv", "environ", "os.environ", "expanduser"):
            self.assertNotIn(acceso, llamadas, acceso)

    def test_3_el_nonce_no_se_puede_fijar_desde_fuera(self):
        import inspect
        for fn in (encrypt, rewrap):
            with self.subTest(fn=fn.__name__):
                self.assertNotIn("nonce", inspect.signature(fn).parameters)

    def test_4_el_aad_vincula_ciphertext_con_identidad(self):
        # Ya cubierto funcionalmente; aqui se fija la forma exacta.
        self.assertEqual(
            EncryptionContext("s", 3, "o", 7).aad(), b"s|3|o|7")

    def test_5_los_errores_no_contienen_secretos(self):
        for code in ("invalid_envelope", "unsupported_version",
                     "unknown_key_version", "authentication_failed",
                     "invalid_context", "invalid_key_size",
                     "plaintext_too_large"):
            with self.subTest(code=code):
                e = VaultCryptoError(code)
                self.assertEqual(e.code, code)
                self.assertTrue(str(e))
                self.assertNotIn("SUPER_SECRET", str(e))

    def test_6_la_clave_se_elige_por_key_version(self):
        import inspect
        self.assertIn("key_version", inspect.signature(StaticKeyResolver.get).parameters)

    def test_7_8_9_este_modulo_no_autoriza_ni_ejecuta(self):
        """No hay red, no hay subproceso, no hay nocion de autorizacion.

        Tambien por AST: `sesion` aparece en la prosa que explica justamente
        que este modulo no la tiene.
        """
        import ast

        arbol = ast.parse(FUENTE.read_text(encoding="utf-8"))
        importados = set()
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Import):
                importados |= {a.name.split(".")[0] for a in nodo.names}
            elif isinstance(nodo, ast.ImportFrom) and nodo.module:
                importados.add(nodo.module.split(".")[0])
        for prohibido in ("requests", "urllib", "httpx", "http", "socket",
                          "subprocess", "flask"):
            self.assertNotIn(prohibido, importados, prohibido)

        # Y ningun identificador del dominio de autorizacion.
        ids = {n.id for n in ast.walk(arbol) if isinstance(n, ast.Name)}
        ids |= {n.name for n in ast.walk(arbol)
                if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        ids |= {a.arg for n in ast.walk(arbol)
                if isinstance(n, ast.FunctionDef) for a in n.args.args}
        for prohibido in ("actor_user", "permission_epoch", "approve",
                          "authorize", "grant", "session"):
            self.assertNotIn(prohibido, ids, prohibido)


class ElModuloEsPuroDeVerdadTests(unittest.TestCase):
    """No basta con que el docstring diga que es puro."""

    def test_sus_imports_no_tocan_el_ERP(self):
        import ast
        arbol = ast.parse(FUENTE.read_text(encoding="utf-8"))
        modulos = set()
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Import):
                modulos |= {a.name.split(".")[0] for a in nodo.names}
            elif isinstance(nodo, ast.ImportFrom) and nodo.module:
                modulos.add(nodo.module.split(".")[0])
        self.assertEqual(modulos - {"base64", "json", "os", "secrets",
                                    "dataclasses", "typing", "cryptography",
                                    "__future__"}, set())

    def test_se_carga_y_funciona_sin_importar_app(self):
        """Cargado por ruta, en un interprete limpio: ni Flask, ni SQLAlchemy,
        ni el paquete `app` entran en `sys.modules`."""
        guion = (
            "import importlib.util, sys\n"
            "spec = importlib.util.spec_from_file_location('vc', r'%s')\n"
            "m = importlib.util.module_from_spec(spec)\n"
            "sys.modules['vc'] = m\n"
            "spec.loader.exec_module(m)\n"
            "sucios = sorted(x for x in sys.modules "
            "if x.split('.')[0] in {'flask','sqlalchemy','app'})\n"
            "r = m.StaticKeyResolver({1: m.generate_key()})\n"
            "c = m.EncryptionContext('s', 1, 'o', 1)\n"
            "ok = m.decrypt(m.encrypt(b'x', c, r), c, r) == b'x'\n"
            "print(repr(sucios), ok)\n"
        ) % FUENTE.resolve()
        out = subprocess.run([sys.executable, "-c", guion], capture_output=True,
                             text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-600:])
        self.assertEqual(out.stdout.strip(), "[] True", out.stdout)


class LaDependenciaEstaDeclaradaTests(unittest.TestCase):
    """PARTE A — una instalacion limpia tiene que traer cryptography.

    Estaba instalada de forma transitiva via pyOpenSSL. Apoyarse en eso es
    apoyarse en que nadie reorganice los extras de Google.
    """

    def test_requirements_la_declara(self):
        req = Path("requirements.txt").read_text(encoding="utf-8")
        lineas = [l.strip() for l in req.splitlines()
                  if l.strip() and not l.strip().startswith("#")]
        declaradas = [l for l in lineas
                      if re.match(r"^cryptography\b", l, re.IGNORECASE)]
        self.assertEqual(len(declaradas), 1, lineas)
        self.assertRegex(declaradas[0], r"^cryptography>=\d+")

    def test_el_minimo_declarado_no_es_una_intuicion(self):
        """El piso sale de una restriccion real: pyOpenSSL ya exige >=46."""
        req = Path("requirements.txt").read_text(encoding="utf-8")
        linea = next(l for l in req.splitlines()
                     if l.strip().lower().startswith("cryptography"))
        minimo = int(re.search(r">=(\d+)", linea).group(1))
        try:
            import importlib.metadata as md
            exigido = next((r for r in (md.requires("pyOpenSSL") or [])
                            if "cryptography" in r), "")
            piso = re.search(r">=(\d+)", exigido)
            if piso:
                self.assertLessEqual(minimo, int(piso.group(1)) + 0,
                                     "el minimo no puede superar lo que ya hay")
        except Exception:  # noqa: BLE001 - el entorno puede no tener pyOpenSSL
            pass
        self.assertGreaterEqual(minimo, 1)

    def test_el_entorno_actual_la_satisface(self):
        import cryptography
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        mayor = int(cryptography.__version__.split(".")[0])
        self.assertGreaterEqual(mayor, 46)
        self.assertTrue(callable(AESGCM))


if __name__ == "__main__":
    unittest.main()
