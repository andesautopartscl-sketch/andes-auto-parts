from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from flask import Flask

from app.assistant.routes import assistant_bp
from app.internal_agent.catalog import (
    BLOCKED_ITEM_FIELDS,
    search_catalog_items,
    search_catalog_page,
    validate_search_args,
)
from app.internal_agent.m2m import InternalAuthError
from app.internal_agent.routes import internal_agent_bp
from app.utils.csrf import CSRF_SESSION_KEY


TOKEN = "erp-test-token"


class CatalogSchemaTests(unittest.TestCase):
    def test_q_corto(self):
        with self.assertRaises(InternalAuthError) as ctx:
            validate_search_args({"q": "f"})
        self.assertEqual(ctx.exception.code, "invalid_args")

    def test_q_largo(self):
        with self.assertRaises(InternalAuthError) as ctx:
            validate_search_args({"q": "x" * 81})
        self.assertEqual(ctx.exception.code, "invalid_args")

    def test_limit_clampa(self):
        q, limit = validate_search_args({"q": "filtro aceite", "limit": 100})
        self.assertEqual(q, "filtro aceite")
        self.assertEqual(limit, 25)

    def test_campo_no_permitido(self):
        with self.assertRaises(InternalAuthError) as ctx:
            validate_search_args({"q": "filtro", "sql": "select 1"})
        self.assertEqual(ctx.exception.code, "invalid_args")
        with self.assertRaises(InternalAuthError):
            validate_search_args({"q": "filtro", "order_by": "precio"})


class InternalSearchApiTests(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.get("ANDES_ENV")
        self._token = os.environ.get("ANDES_AGENT_SERVICE_TOKEN")
        os.environ["ANDES_ENV"] = "local"
        os.environ["ANDES_AGENT_SERVICE_TOKEN"] = TOKEN
        app = Flask(__name__)
        app.config["TESTING"] = True
        app.register_blueprint(internal_agent_bp)
        self.client = app.test_client()

    def tearDown(self):
        if self._env is None:
            os.environ.pop("ANDES_ENV", None)
        else:
            os.environ["ANDES_ENV"] = self._env
        if self._token is None:
            os.environ.pop("ANDES_AGENT_SERVICE_TOKEN", None)
        else:
            os.environ["ANDES_AGENT_SERVICE_TOKEN"] = self._token

    def _headers(self, extra=None):
        headers = {
            "Authorization": f"Bearer {TOKEN}",
            "X-Andes-Env": "local",
            "X-Andes-Actor": "albert",
            "Content-Type": "application/json",
        }
        if extra:
            headers.update(extra)
        return headers

    def test_busqueda_exitosa_sin_datos_financieros(self):
        items = [{"codigo": "F-001", "descripcion": "Filtro aceite", "marca": "MANN", "modelo": "W712"}]
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_productos"
        ), patch("app.internal_agent.routes.search_catalog_page", return_value=(items, False)):
            resp = self.client.post(
                "/internal/agent/v1/catalog/search",
                headers=self._headers(),
                data=json.dumps({"q": "filtro aceite", "limit": 10}),
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["data"]["items"][0]["codigo"], "F-001")
        self.assertNotIn("precio", json.dumps(body))
        self.assertNotIn(TOKEN, json.dumps(body))

    def test_resultado_vacio(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_productos"
        ), patch("app.internal_agent.routes.search_catalog_page", return_value=([], False)):
            resp = self.client.post(
                "/internal/agent/v1/catalog/search",
                headers=self._headers(),
                data=json.dumps({"q": "zzzzzz"}),
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["data"]["count"], 0)

    def test_usuario_sin_permiso(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("bob", "vendedor")), patch(
            "app.internal_agent.routes.require_mod_productos",
            side_effect=InternalAuthError("permission_denied", "Permission mod_productos is required", 403),
        ):
            resp = self.client.post(
                "/internal/agent/v1/catalog/search",
                headers=self._headers({"X-Andes-Actor": "bob"}),
                data=json.dumps({"q": "filtro aceite"}),
            )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json()["error_code"], "permission_denied")

    def test_sin_bearer(self):
        resp = self.client.post(
            "/internal/agent/v1/catalog/search",
            headers={"X-Andes-Env": "local", "X-Andes-Actor": "albert", "Content-Type": "application/json"},
            data=json.dumps({"q": "filtro aceite"}),
        )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.get_json()["error_code"], "unauthorized")

    def test_bearer_correcto_environment_incorrecto(self):
        resp = self.client.post(
            "/internal/agent/v1/catalog/search",
            headers=self._headers({"X-Andes-Env": "production"}),
            data=json.dumps({"q": "filtro aceite"}),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error_code"], "invalid_environment")

    def test_bearer_correcto_con_permiso_200(self):
        items = [{"codigo": "F-001", "descripcion": "Filtro", "marca": "", "modelo": ""}]
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_productos"
        ), patch("app.internal_agent.routes.search_catalog_page", return_value=(items, False)):
            resp = self.client.post(
                "/internal/agent/v1/catalog/search",
                headers=self._headers(),
                data=json.dumps({"q": "filtro aceite", "limit": 10}),
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertFalse(body["meta"]["truncated"])
        self.assertEqual(body["data"]["count"], 1)

    def test_token_incorrecto(self):
        resp = self.client.post(
            "/internal/agent/v1/catalog/search",
            headers=self._headers({"Authorization": "Bearer wrong"}),
            data=json.dumps({"q": "filtro aceite"}),
        )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.get_json()["error_code"], "unauthorized")

    def test_environment_incorrecto(self):
        resp = self.client.post(
            "/internal/agent/v1/catalog/search",
            headers=self._headers({"X-Andes-Env": "staging"}),
            data=json.dumps({"q": "filtro aceite"}),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error_code"], "invalid_environment")

    def test_cookie_rechazada(self):
        self.client.set_cookie("session", "abc")
        resp = self.client.post(
            "/internal/agent/v1/catalog/search",
            headers=self._headers(),
            data=json.dumps({"q": "filtro aceite"}),
        )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.get_json()["error_code"], "cookies_not_allowed")

    def test_q_corto_api(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_productos"
        ):
            resp = self.client.post(
                "/internal/agent/v1/catalog/search",
                headers=self._headers(),
                data=json.dumps({"q": "x"}),
            )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error_code"], "invalid_args")


class SanitizeCatalogTests(unittest.TestCase):
    def test_no_devuelve_campos_bloqueados(self):
        rows = [
            {
                "codigo": "F-001",
                "descripcion": "Filtro aceite",
                "marca": "MANN",
                "modelo": "W712",
                "precio": 12345,
                "costo": 8000,
                "margen": 0.4,
                "rut": "11111111-1",
                "email": "a@b.cl",
                "telefono": "911111111",
                "token": "secret-token",
                "password": "x",
                "stock": 9,
            }
        ]
        with patch("app.internal_agent.catalog._fetch_product_rows", return_value=rows):
            items = search_catalog_items("filtro aceite", 10)
        blob = json.dumps(items)
        self.assertEqual(set(items[0].keys()), {"codigo", "descripcion", "marca", "modelo"})
        for field in BLOCKED_ITEM_FIELDS:
            self.assertNotIn(f'"{field}"', blob)
        self.assertNotIn("12345", blob)
        self.assertNotIn("secret-token", blob)
        self.assertNotIn("11111111-1", blob)
        self.assertNotIn("a@b.cl", blob)

    def test_truncated_false_cuando_hay_menos_que_limit(self):
        rows = [{"codigo": "A", "descripcion": "uno", "marca": "", "modelo": ""}]
        with patch("app.internal_agent.catalog._fetch_product_rows", return_value=rows):
            items, truncated = search_catalog_page("uno", 10)
        self.assertEqual(len(items), 1)
        self.assertFalse(truncated)

    def test_truncated_true_cuando_se_recorta_por_limit(self):
        rows = [{"codigo": f"C{i}", "descripcion": "x", "marca": "", "modelo": ""} for i in range(12)]
        with patch("app.internal_agent.catalog._fetch_product_rows", return_value=rows):
            items, truncated = search_catalog_page("x", 10)
        self.assertTrue(truncated)
        self.assertEqual(len(items), 10)
        self.assertEqual(items[-1]["codigo"], "C9")


class AssistantBffTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.secret_key = "test-secret"
        app.config["TESTING"] = True
        app.register_blueprint(assistant_bp)
        self.client = app.test_client()

    def test_bff_no_expone_token_y_allowlist(self):
        fake = {
            "ok": True,
            "tool": "search_catalog",
            "classification": "INTERNAL",
            "data": {"items": [{"codigo": "F-001", "descripcion": "Filtro", "marca": "", "modelo": ""}], "count": 1},
            "meta": {"limit": 10, "truncated": False, "environment": "local"},
        }
        with self.client.session_transaction() as sess:
            sess["user"] = "albert"
            sess[CSRF_SESSION_KEY] = "csrf-test"
        with patch("app.assistant.routes.invoke_gateway", return_value=(200, fake)) as mocked:
            resp = self.client.post(
                "/assistant/api/invoke",
                json={"tool": "search_catalog", "arguments": {"q": "filtro aceite"}, "conversation_id": "c1"},
                headers={"X-CSRF-Token": "csrf-test"},
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertNotIn("ANDES_AGENT_SERVICE_TOKEN", json.dumps(body))
        self.assertNotIn("Bearer", json.dumps(body))
        sent = mocked.call_args[0][0]
        self.assertEqual(sent["actor_user"], "albert")
        self.assertEqual(sent["tool"], "search_catalog")
        self.assertNotIn("token", sent)

        resp = self.client.post(
            "/assistant/api/invoke",
            json={"tool": "unknown_tool_xyz", "arguments": {}},
            headers={"X-CSRF-Token": "csrf-test"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error_code"], "tool_not_available")


if __name__ == "__main__":
    unittest.main()
