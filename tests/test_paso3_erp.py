from __future__ import annotations

import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import URLError

from flask import Flask

from app.assistant.routes import assistant_bp
from app.internal_agent.m2m import InternalAuthError
from app.internal_agent.product import (
    BLOCKED_PRODUCT_FIELDS,
    project_product,
    validate_product_codigo,
)
from app.internal_agent.routes import internal_agent_bp
from app.utils.csrf import CSRF_SESSION_KEY


TOKEN = "erp-test-token"


class Producto:
    def __init__(self):
        self.codigo = "ABC123"
        self.descripcion = "Filtro de aceite"
        self.marca = "MANN"
        self.modelo = "W712"
        self.motor = "1.6"
        self.anio = "2015"
        self.activo = True
        self.p_publico = 99999
        self.prec_mayor = 8000
        self.stock_10jul = 12
        self.factura_proveedor = "F-99"
        self.categoria_rel = SimpleNamespace(nombre="Filtros")
        self.subcategoria_rel = SimpleNamespace(nombre="Aceite")


class ProductSchemaTests(unittest.TestCase):
    def test_codigo_vacio(self):
        with self.assertRaises(InternalAuthError) as ctx:
            validate_product_codigo("")
        self.assertEqual(ctx.exception.code, "invalid_args")

    def test_codigo_demasiado_largo(self):
        with self.assertRaises(InternalAuthError) as ctx:
            validate_product_codigo("A" * 65)
        self.assertEqual(ctx.exception.code, "invalid_args")

    def test_sql(self):
        with self.assertRaises(InternalAuthError) as ctx:
            validate_product_codigo("SELECT * FROM productos")
        self.assertEqual(ctx.exception.code, "invalid_args")

    def test_normaliza_mayusculas(self):
        self.assertEqual(validate_product_codigo(" abc123 "), "ABC123")


class ProjectProductTests(unittest.TestCase):
    def test_respuesta_sin_datos_financieros(self):
        data = project_product(Producto())
        blob = json.dumps(data)
        self.assertEqual(
            set(data.keys()),
            {
                "codigo",
                "descripcion",
                "marca",
                "modelo",
                "motor",
                "anio",
                "activo",
                "categoria",
                "subcategoria",
            },
        )
        self.assertEqual(data["categoria"], "Filtros")
        self.assertEqual(data["subcategoria"], "Aceite")
        for field in BLOCKED_PRODUCT_FIELDS:
            self.assertNotIn(f'"{field}"', blob)
        self.assertNotIn("99999", blob)
        self.assertNotIn("8000", blob)
        self.assertNotIn("F-99", blob)


class InternalProductApiTests(unittest.TestCase):
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
        }
        if extra:
            headers.update(extra)
        return headers

    def test_01_producto_existente(self):
        public = project_product(Producto())
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_productos"
        ), patch("app.internal_agent.routes.get_public_product", return_value=public):
            resp = self.client.get("/internal/agent/v1/catalog/product/ABC123", headers=self._headers())
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["tool"], "get_product")
        self.assertEqual(body["data"]["codigo"], "ABC123")
        self.assertEqual(body["meta"]["environment"], "local")
        self.assertNotIn("precio", json.dumps(body))

    def test_02_producto_inexistente(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_productos"
        ), patch("app.internal_agent.routes.get_public_product", return_value=None):
            resp = self.client.get("/internal/agent/v1/catalog/product/NOEXISTE", headers=self._headers())
        self.assertEqual(resp.status_code, 404)
        body = resp.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error_code"], "not_found")
        self.assertEqual(body["error"]["code"], "not_found")
        self.assertEqual(body["error"]["message"], "Producto no encontrado")
        self.assertNotEqual(resp.status_code, 500)

    def test_03_codigo_vacio_no_matchea_ruta(self):
        resp = self.client.get("/internal/agent/v1/catalog/product/", headers=self._headers())
        self.assertIn(resp.status_code, {404, 308, 301})

    def test_04_codigo_demasiado_largo(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_productos"
        ):
            resp = self.client.get("/internal/agent/v1/catalog/product/" + ("A" * 65), headers=self._headers())
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error_code"], "invalid_args")

    def test_05_campo_adicional_query(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_productos"
        ):
            resp = self.client.get(
                "/internal/agent/v1/catalog/product/ABC123?limit=1",
                headers=self._headers(),
            )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error_code"], "invalid_args")

    def test_06_sql_en_argumentos(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_productos"
        ):
            resp = self.client.get(
                "/internal/agent/v1/catalog/product/ABC123?sql=select%201",
                headers=self._headers(),
            )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error_code"], "invalid_args")

    def test_07_token_incorrecto(self):
        resp = self.client.get(
            "/internal/agent/v1/catalog/product/ABC123",
            headers=self._headers({"Authorization": "Bearer wrong"}),
        )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.get_json()["error_code"], "unauthorized")

    def test_08_cookie(self):
        self.client.set_cookie("session", "abc")
        resp = self.client.get("/internal/agent/v1/catalog/product/ABC123", headers=self._headers())
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.get_json()["error_code"], "cookies_not_allowed")

    def test_09_environment_incorrecto(self):
        resp = self.client.get(
            "/internal/agent/v1/catalog/product/ABC123",
            headers=self._headers({"X-Andes-Env": "production"}),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error_code"], "invalid_environment")

    def test_10_permiso_inexistente(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("bob", "vendedor")), patch(
            "app.internal_agent.routes.require_mod_productos",
            side_effect=InternalAuthError("permission_denied", "Permission mod_productos is required", 403),
        ):
            resp = self.client.get(
                "/internal/agent/v1/catalog/product/ABC123",
                headers=self._headers({"X-Andes-Actor": "bob"}),
            )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json()["error_code"], "permission_denied")

    def test_11_respuesta_sin_datos_financieros(self):
        public = project_product(Producto())
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_productos"
        ), patch("app.internal_agent.routes.get_public_product", return_value=public):
            resp = self.client.get("/internal/agent/v1/catalog/product/ABC123", headers=self._headers())
        blob = json.dumps(resp.get_json())
        for field in BLOCKED_PRODUCT_FIELDS:
            self.assertNotIn(f'"{field}"', blob)


class AssistantBffGetProductTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.secret_key = "test-secret"
        app.config["TESTING"] = True
        app.register_blueprint(assistant_bp)
        self.client = app.test_client()

    def _login(self):
        with self.client.session_transaction() as sess:
            sess["user"] = "albert"
            sess[CSRF_SESSION_KEY] = "csrf-test"

    def test_bff_invoca_get_product_sin_exponer_token(self):
        fake = {
            "ok": True,
            "tool": "get_product",
            "classification": "INTERNAL",
            "write": False,
            "data": project_product(Producto()),
            "meta": {"environment": "local"},
        }
        self._login()
        with patch("app.assistant.routes.invoke_gateway", return_value=(200, fake)) as mocked:
            resp = self.client.post(
                "/assistant/api/invoke",
                json={"tool": "get_product", "arguments": {"codigo": "ABC123"}, "conversation_id": "c1"},
                headers={"X-CSRF-Token": "csrf-test"},
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["tool"], "get_product")
        blob = json.dumps(body)
        self.assertNotIn("ANDES_AGENT_SERVICE_TOKEN", blob)
        self.assertNotIn("Bearer", blob)
        sent = mocked.call_args[0][0]
        self.assertEqual(sent["actor_user"], "albert")
        self.assertEqual(sent["tool"], "get_product")
        self.assertEqual(sent["arguments"]["codigo"], "ABC123")
        self.assertNotIn("token", sent)

    def test_13_gateway_apagado(self):
        self._login()
        with patch.dict(os.environ, {"ANDES_AGENT_SERVICE_TOKEN": TOKEN, "ANDES_ENV": "local"}), patch(
            "app.assistant.routes._agent_url", return_value="http://127.0.0.1:9"
        ), patch("urllib.request.urlopen", side_effect=URLError("down")):
            resp = self.client.post(
                "/assistant/api/invoke",
                json={"tool": "get_product", "arguments": {"codigo": "ABC123"}},
                headers={"X-CSRF-Token": "csrf-test"},
            )
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json()["error_code"], "agent_unavailable")
        self.assertNotEqual(resp.status_code, 500)

    def test_14_erp_apagado_via_gateway(self):
        self._login()
        with patch(
            "app.assistant.routes.invoke_gateway",
            return_value=(503, {"ok": False, "error_code": "erp_unavailable", "message": "ERP is not reachable"}),
        ):
            resp = self.client.post(
                "/assistant/api/invoke",
                json={"tool": "get_product", "arguments": {"codigo": "ABC123"}},
                headers={"X-CSRF-Token": "csrf-test"},
            )
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json()["error_code"], "erp_unavailable")


if __name__ == "__main__":
    unittest.main()
