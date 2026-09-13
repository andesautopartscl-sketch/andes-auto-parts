from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch
from urllib.error import URLError

from flask import Flask

from app.assistant.routes import assistant_bp
from app.internal_agent.inventory import (
    BLOCKED_INVENTORY_FIELDS,
    get_public_inventory,
    validate_inventory_args,
)
from app.internal_agent.m2m import InternalAuthError
from app.internal_agent.routes import internal_agent_bp
from app.utils.csrf import CSRF_SESSION_KEY


TOKEN = "erp-test-token"


class Variant:
    def __init__(self, marca, bodega, origen_compra, stock, **extra):
        self.marca = marca
        self.bodega = bodega
        self.origen_compra = origen_compra
        self.stock = stock
        for key, value in extra.items():
            setattr(self, key, value)


class InventorySchemaTests(unittest.TestCase):
    def test_codigo_vacio(self):
        with self.assertRaises(InternalAuthError) as ctx:
            validate_inventory_args({"codigo": ""})
        self.assertEqual(ctx.exception.code, "invalid_args")

    def test_campo_extra(self):
        with self.assertRaises(InternalAuthError) as ctx:
            validate_inventory_args({"codigo": "2404", "limit": 10})
        self.assertEqual(ctx.exception.code, "invalid_args")

    def test_sql(self):
        with self.assertRaises(InternalAuthError) as ctx:
            validate_inventory_args({"codigo": "SELECT * FROM productos"})
        self.assertEqual(ctx.exception.code, "invalid_args")


class PublicInventoryTests(unittest.TestCase):
    def test_producto_con_stock_y_total(self):
        rows = [
            Variant("MAXUS", "Bodega Central", "nacional", 8, costo=99, margen=0.2, proveedor="ACME"),
            Variant("BOSCH", "Bodega 2", "importado", 3),
        ]
        with patch("app.internal_agent.product.get_public_product", return_value={"codigo": "2404", "descripcion": "FILTRO DIESEL"}), patch(
            "app.utils.stock_control._variant_stock_query"
        ) as query, patch("app.utils.stock_control.get_available_stock", return_value=11):
            query.return_value.all.return_value = rows
            data = get_public_inventory("2404")
        self.assertEqual(data["codigo"], "2404")
        self.assertEqual(data["total_stock"], 11)
        self.assertEqual(len(data["items"]), 2)
        blob = json.dumps(data)
        for field in BLOCKED_INVENTORY_FIELDS:
            self.assertNotIn(f'"{field}"', blob)
        self.assertEqual(set(data["items"][0].keys()), {"marca", "bodega", "origen_compra", "stock"})

    def test_producto_sin_stock(self):
        with patch("app.internal_agent.product.get_public_product", return_value={"codigo": "2404", "descripcion": "X"}), patch(
            "app.utils.stock_control._variant_stock_query"
        ) as query, patch("app.utils.stock_control.get_available_stock", return_value=0):
            query.return_value.all.return_value = []
            data = get_public_inventory("2404")
        self.assertEqual(data["items"], [])
        self.assertEqual(data["total_stock"], 0)

    def test_producto_inexistente(self):
        with patch("app.internal_agent.product.get_public_product", return_value=None):
            self.assertIsNone(get_public_inventory("NO-EXISTE"))


class InternalInventoryApiTests(unittest.TestCase):
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

    def test_01_producto_con_stock(self):
        data = {
            "codigo": "2404",
            "descripcion": "FILTRO DIESEL",
            "items": [{"marca": "MAXUS", "bodega": "Bodega Central", "origen_compra": "nacional", "stock": 8}],
            "total_stock": 8,
        }
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_ver_stock"
        ), patch("app.internal_agent.routes.get_public_inventory", return_value=data):
            resp = self.client.post(
                "/internal/agent/v1/inventory/stock",
                headers=self._headers(),
                data=json.dumps({"codigo": "2404"}),
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["tool"], "get_inventory")
        self.assertEqual(body["data"]["total_stock"], 8)

    def test_02_producto_sin_stock(self):
        data = {"codigo": "2404", "descripcion": "X", "items": [], "total_stock": 0}
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_ver_stock"
        ), patch("app.internal_agent.routes.get_public_inventory", return_value=data):
            resp = self.client.post(
                "/internal/agent/v1/inventory/stock",
                headers=self._headers(),
                data=json.dumps({"codigo": "2404"}),
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["data"]["items"], [])
        self.assertEqual(resp.get_json()["data"]["total_stock"], 0)

    def test_03_producto_inexistente(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_ver_stock"
        ), patch("app.internal_agent.routes.get_public_inventory", return_value=None):
            resp = self.client.post(
                "/internal/agent/v1/inventory/stock",
                headers=self._headers(),
                data=json.dumps({"codigo": "NO-EXISTE"}),
            )
        self.assertEqual(resp.status_code, 404)
        body = resp.get_json()
        self.assertEqual(body["error_code"], "not_found")
        self.assertEqual(body["error"]["message"], "Producto no encontrado")

    def test_04_filtro_marca(self):
        captured = {}

        def fake(codigo, marca=None, bodega=None):
            captured["args"] = (codigo, marca, bodega)
            return {
                "codigo": codigo,
                "descripcion": "X",
                "items": [{"marca": marca, "bodega": "Bodega Central", "origen_compra": "nacional", "stock": 8}],
                "total_stock": 8,
            }

        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_ver_stock"
        ), patch("app.internal_agent.routes.get_public_inventory", side_effect=fake):
            resp = self.client.post(
                "/internal/agent/v1/inventory/stock",
                headers=self._headers(),
                data=json.dumps({"codigo": "2404", "marca": "MAXUS"}),
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured["args"], ("2404", "MAXUS", None))

    def test_05_filtro_bodega(self):
        captured = {}

        def fake(codigo, marca=None, bodega=None):
            captured["args"] = (codigo, marca, bodega)
            return {
                "codigo": codigo,
                "descripcion": "X",
                "items": [{"marca": "MAXUS", "bodega": bodega, "origen_compra": "nacional", "stock": 8}],
                "total_stock": 8,
            }

        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_ver_stock"
        ), patch("app.internal_agent.routes.get_public_inventory", side_effect=fake):
            resp = self.client.post(
                "/internal/agent/v1/inventory/stock",
                headers=self._headers(),
                data=json.dumps({"codigo": "2404", "bodega": "Bodega Central"}),
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured["args"][2], "Bodega Central")

    def test_06_codigo_vacio(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_ver_stock"
        ):
            resp = self.client.post(
                "/internal/agent/v1/inventory/stock",
                headers=self._headers(),
                data=json.dumps({"codigo": ""}),
            )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error_code"], "invalid_args")

    def test_07_argumentos_adicionales(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_ver_stock"
        ):
            resp = self.client.post(
                "/internal/agent/v1/inventory/stock",
                headers=self._headers(),
                data=json.dumps({"codigo": "2404", "order_by": "stock"}),
            )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error_code"], "invalid_args")

    def test_08_sql(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_ver_stock"
        ):
            resp = self.client.post(
                "/internal/agent/v1/inventory/stock",
                headers=self._headers(),
                data=json.dumps({"codigo": "2404", "sql": "select 1"}),
            )
        self.assertEqual(resp.status_code, 400)

    def test_09_token_incorrecto(self):
        resp = self.client.post(
            "/internal/agent/v1/inventory/stock",
            headers=self._headers({"Authorization": "Bearer wrong"}),
            data=json.dumps({"codigo": "2404"}),
        )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.get_json()["error_code"], "unauthorized")

    def test_10_cookie(self):
        self.client.set_cookie("session", "abc")
        resp = self.client.post(
            "/internal/agent/v1/inventory/stock",
            headers=self._headers(),
            data=json.dumps({"codigo": "2404"}),
        )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.get_json()["error_code"], "cookies_not_allowed")

    def test_11_environment_incorrecto(self):
        resp = self.client.post(
            "/internal/agent/v1/inventory/stock",
            headers=self._headers({"X-Andes-Env": "production"}),
            data=json.dumps({"codigo": "2404"}),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error_code"], "invalid_environment")

    def test_12_usuario_sin_ver_stock(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("bob", "vendedor")), patch(
            "app.internal_agent.routes.require_ver_stock",
            side_effect=InternalAuthError("permission_denied", "Permission ver_stock is required", 403),
        ):
            resp = self.client.post(
                "/internal/agent/v1/inventory/stock",
                headers=self._headers({"X-Andes-Actor": "bob"}),
                data=json.dumps({"codigo": "2404"}),
            )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json()["error_code"], "permission_denied")


class AssistantBffInventoryTests(unittest.TestCase):
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

    def test_bff_invoca_get_inventory(self):
        fake = {
            "ok": True,
            "tool": "get_inventory",
            "write": False,
            "data": {"codigo": "2404", "descripcion": "FILTRO DIESEL", "items": [], "total_stock": 0},
            "meta": {"environment": "local"},
        }
        self._login()
        with patch("app.assistant.routes.invoke_gateway", return_value=(200, fake)) as mocked:
            resp = self.client.post(
                "/assistant/api/invoke",
                json={"tool": "get_inventory", "arguments": {"codigo": "2404"}, "conversation_id": "c1"},
                headers={"X-CSRF-Token": "csrf-test"},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["tool"], "get_inventory")
        self.assertEqual(mocked.call_args[0][0]["tool"], "get_inventory")
        self.assertNotIn("Bearer", json.dumps(resp.get_json()))

    def test_15_gateway_apagado(self):
        self._login()
        with patch.dict(os.environ, {"ANDES_AGENT_SERVICE_TOKEN": TOKEN, "ANDES_ENV": "local"}), patch(
            "app.assistant.routes._agent_url", return_value="http://127.0.0.1:9"
        ), patch("urllib.request.urlopen", side_effect=URLError("down")):
            resp = self.client.post(
                "/assistant/api/invoke",
                json={"tool": "get_inventory", "arguments": {"codigo": "2404"}},
                headers={"X-CSRF-Token": "csrf-test"},
            )
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json()["error_code"], "agent_unavailable")

    def test_16_erp_apagado_via_gateway(self):
        self._login()
        with patch(
            "app.assistant.routes.invoke_gateway",
            return_value=(503, {"ok": False, "error_code": "erp_unavailable", "message": "ERP is not reachable"}),
        ):
            resp = self.client.post(
                "/assistant/api/invoke",
                json={"tool": "get_inventory", "arguments": {"codigo": "2404"}},
                headers={"X-CSRF-Token": "csrf-test"},
            )
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json()["error_code"], "erp_unavailable")

    def test_search_catalog_y_get_product_siguen_en_allowlist(self):
        self._login()
        with patch("app.assistant.routes.invoke_gateway", return_value=(200, {"ok": True, "tool": "search_catalog"})):
            resp = self.client.post(
                "/assistant/api/invoke",
                json={"tool": "search_catalog", "arguments": {"q": "filtro"}},
                headers={"X-CSRF-Token": "csrf-test"},
            )
        self.assertEqual(resp.status_code, 200)
        with patch("app.assistant.routes.invoke_gateway", return_value=(200, {"ok": True, "tool": "get_product"})):
            resp = self.client.post(
                "/assistant/api/invoke",
                json={"tool": "get_product", "arguments": {"codigo": "2404"}},
                headers={"X-CSRF-Token": "csrf-test"},
            )
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
