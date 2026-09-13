from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch
from urllib.error import URLError

from flask import Flask

from app.assistant.routes import assistant_bp
from app.internal_agent.check_stock import check_public_stock, validate_check_stock_args
from app.internal_agent.m2m import InternalAuthError
from app.internal_agent.routes import internal_agent_bp
from app.utils.csrf import CSRF_SESSION_KEY

TOKEN = "erp-test-token"


class CheckStockSchemaTests(unittest.TestCase):
    def test_ok(self):
        items = validate_check_stock_args({"items": [{"codigo": "2404", "cantidad": 2}]})
        self.assertEqual(items[0]["codigo"], "2404")
        self.assertEqual(items[0]["cantidad"], 2)

    def test_sin_items(self):
        with self.assertRaises(InternalAuthError) as ctx:
            validate_check_stock_args({})
        self.assertEqual(ctx.exception.code, "invalid_args")

    def test_mas_de_20(self):
        with self.assertRaises(InternalAuthError):
            validate_check_stock_args({"items": [{"codigo": "2404", "cantidad": 1} for _ in range(21)]})

    def test_cantidad_invalida(self):
        with self.assertRaises(InternalAuthError):
            validate_check_stock_args({"items": [{"codigo": "2404", "cantidad": 0}]})

    def test_campo_extra(self):
        with self.assertRaises(InternalAuthError):
            validate_check_stock_args({"items": [{"codigo": "2404", "cantidad": 1}], "sql": "x"})


class PublicCheckStockTests(unittest.TestCase):
    def test_suficiente_e_insuficiente(self):
        with patch("app.utils.stock_control.get_available_stock", side_effect=lambda *a, **k: 10):
            data = check_public_stock([{"codigo": "2404", "cantidad": 2}])
        self.assertTrue(data["available"])
        self.assertTrue(data["items"][0]["ok"])
        self.assertEqual(data["items"][0]["disponible"], 10)

        with patch("app.utils.stock_control.get_available_stock", side_effect=lambda *a, **k: 1):
            data = check_public_stock([{"codigo": "2404", "cantidad": 5}])
        self.assertFalse(data["available"])
        self.assertFalse(data["items"][0]["ok"])

    def test_solo_get_available_stock(self):
        with patch("app.utils.stock_control.get_available_stock", return_value=3) as mocked:
            with patch("app.utils.stock_control.deduct_stock_for_sale") as deduct:
                check_public_stock([{"codigo": "2404", "cantidad": 1}])
                mocked.assert_called_once()
                deduct.assert_not_called()


class InternalCheckStockApiTests(unittest.TestCase):
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

    def test_01_ok(self):
        fake = {"available": True, "items": [{"codigo": "2404", "cantidad": 1, "disponible": 5, "ok": True}]}
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_ver_stock"
        ), patch("app.internal_agent.routes.check_public_stock", return_value=fake):
            resp = self.client.post(
                "/internal/agent/v1/inventory/check-stock",
                headers=self._headers(),
                data=json.dumps({"items": [{"codigo": "2404", "cantidad": 1}]}),
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["tool"], "check_stock")
        self.assertTrue(body["data"]["available"])

    def test_02_token(self):
        resp = self.client.post(
            "/internal/agent/v1/inventory/check-stock",
            headers=self._headers({"Authorization": "Bearer wrong"}),
            data=json.dumps({"items": [{"codigo": "2404", "cantidad": 1}]}),
        )
        self.assertEqual(resp.status_code, 401)

    def test_03_cookie(self):
        self.client.set_cookie("session", "abc")
        resp = self.client.post(
            "/internal/agent/v1/inventory/check-stock",
            headers=self._headers(),
            data=json.dumps({"items": [{"codigo": "2404", "cantidad": 1}]}),
        )
        self.assertEqual(resp.status_code, 401)

    def test_04_sin_permiso(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("bob", "x")), patch(
            "app.internal_agent.routes.require_ver_stock",
            side_effect=InternalAuthError("permission_denied", "Permission ver_stock is required", 403),
        ):
            resp = self.client.post(
                "/internal/agent/v1/inventory/check-stock",
                headers=self._headers({"X-Andes-Actor": "bob"}),
                data=json.dumps({"items": [{"codigo": "2404", "cantidad": 1}]}),
            )
        self.assertEqual(resp.status_code, 403)

    def test_05_limit_21(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_ver_stock"
        ):
            resp = self.client.post(
                "/internal/agent/v1/inventory/check-stock",
                headers=self._headers(),
                data=json.dumps({"items": [{"codigo": "2404", "cantidad": 1} for _ in range(21)]}),
            )
        self.assertEqual(resp.status_code, 400)


class AssistantBffCheckStockTests(unittest.TestCase):
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

    def test_bff_invoca_check_stock(self):
        fake = {
            "ok": True,
            "tool": "check_stock",
            "write": False,
            "classification": "INTERNAL",
            "data": {"available": True, "items": []},
            "meta": {"environment": "local"},
        }
        self._login()
        with patch("app.assistant.routes.invoke_gateway", return_value=(200, fake)) as mocked:
            resp = self.client.post(
                "/assistant/api/invoke",
                json={"tool": "check_stock", "arguments": {"items": [{"codigo": "2404", "cantidad": 1}]}},
                headers={"X-CSRF-Token": "csrf-test"},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mocked.call_args[0][0]["tool"], "check_stock")
        self.assertNotIn("Bearer", json.dumps(resp.get_json()))

    def test_gateway_apagado(self):
        self._login()
        with patch.dict(os.environ, {"ANDES_AGENT_SERVICE_TOKEN": TOKEN, "ANDES_ENV": "local"}), patch(
            "app.assistant.routes._agent_url", return_value="http://127.0.0.1:9"
        ), patch("urllib.request.urlopen", side_effect=URLError("down")):
            resp = self.client.post(
                "/assistant/api/invoke",
                json={"tool": "check_stock", "arguments": {"items": [{"codigo": "2404", "cantidad": 1}]}},
                headers={"X-CSRF-Token": "csrf-test"},
            )
        self.assertEqual(resp.status_code, 503)

    def test_tools_previas_allowlist(self):
        self._login()
        for tool, arguments in (
            ("search_catalog", {"q": "filtro"}),
            ("get_product", {"codigo": "2404"}),
            ("get_inventory", {"codigo": "2404"}),
            ("get_stock_movements", {"codigo": "2404"}),
            ("get_ingresos", {"codigo": "2404"}),
            ("get_purchase_orders", {"numero": "OC-1"}),
            ("get_customer", {"q": "ALBERT"}),
            ("get_supplier", {"q": "ALBERT"}),
        ):
            with patch("app.assistant.routes.invoke_gateway", return_value=(200, {"ok": True, "tool": tool})):
                resp = self.client.post(
                    "/assistant/api/invoke",
                    json={"tool": tool, "arguments": arguments},
                    headers={"X-CSRF-Token": "csrf-test"},
                )
            self.assertEqual(resp.status_code, 200, tool)

    def test_dashboard_sigue_bloqueado(self):
        self._login()
        resp = self.client.post(
            "/assistant/api/invoke",
            json={"tool": "unknown_tool_xyz", "arguments": {}},
            headers={"X-CSRF-Token": "csrf-test"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error_code"], "tool_not_available")


if __name__ == "__main__":
    unittest.main()
