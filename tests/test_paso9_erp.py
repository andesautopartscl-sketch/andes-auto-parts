from __future__ import annotations

import json
import os
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import URLError

from flask import Flask

from app.assistant.routes import assistant_bp
from app.internal_agent.m2m import InternalAuthError
from app.internal_agent.routes import internal_agent_bp
from app.internal_agent.supplier import (
    BLOCKED_SUPPLIER_FIELDS,
    get_public_suppliers,
    validate_supplier_args,
)
from app.utils.csrf import CSRF_SESSION_KEY

TOKEN = "erp-test-token"


class Prov:
    def __init__(self, **kwargs):
        self.id = kwargs.get("id", 1)
        self.nombre = kwargs.get("nombre", "ALBERT CASTILLO")
        self.empresa = kwargs.get("empresa", "ANDES AUTO PARTS LTDA")
        self.rut = kwargs.get("rut", "780742887")
        self.giro = kwargs.get("giro", "REPUESTOS")
        self.direccion = kwargs.get("direccion", "Calle Secreta 1")
        self.region = kwargs.get("region", "METROPOLITANA")
        self.comuna = kwargs.get("comuna", "PROVIDENCIA")
        self.ciudad = kwargs.get("ciudad", "SANTIAGO")
        self.pais = kwargs.get("pais", "Chile")
        self.telefono = kwargs.get("telefono", "912345678")
        self.email = kwargs.get("email", "hidden@example.com")
        self.activo = kwargs.get("activo", True)
        for key, value in kwargs.items():
            setattr(self, key, value)


class SupplierSchemaTests(unittest.TestCase):
    def test_campo_extra(self):
        with self.assertRaises(InternalAuthError) as ctx:
            validate_supplier_args({"q": "ALBERT", "sql": "select 1"})
        self.assertEqual(ctx.exception.code, "invalid_args")

    def test_sin_filtro(self):
        with self.assertRaises(InternalAuthError) as ctx:
            validate_supplier_args({})
        self.assertEqual(ctx.exception.code, "invalid_args")

    def test_limit_mayor_20(self):
        with self.assertRaises(InternalAuthError) as ctx:
            validate_supplier_args({"q": "ALBERT", "limit": 21})
        self.assertEqual(ctx.exception.code, "invalid_args")

    def test_ok(self):
        args = validate_supplier_args({"q": "ALBERT", "rut": "78.074.288-7", "id": 1, "limit": 10})
        self.assertEqual(args["q"], "ALBERT")
        self.assertEqual(args["rut"], "780742887")
        self.assertEqual(args["id"], 1)
        self.assertEqual(args["limit"], 10)


class PublicSupplierTests(unittest.TestCase):
    def _query(self, rows):
        q = MagicMock()
        q.filter.return_value = q
        q.order_by.return_value = q
        q.limit.return_value = q
        q.first.return_value = rows[0] if rows else None
        q.all.return_value = rows
        return q

    def test_proveedor_sin_pii(self):
        q = self._query([Prov()])
        with patch("app.ventas.models.Proveedor") as Model:
            Model.query.filter.return_value = q
            data, truncated = get_public_suppliers(supplier_id=1)
        self.assertFalse(truncated)
        self.assertEqual(data["items"][0]["nombre"], "ALBERT CASTILLO")
        self.assertEqual(data["items"][0]["empresa"], "ANDES AUTO PARTS LTDA")
        blob = json.dumps(data)
        for field in BLOCKED_SUPPLIER_FIELDS:
            self.assertNotIn(f'"{field}"', blob)
        self.assertNotIn("cliente", blob.lower())

    def test_inexistente(self):
        q = self._query([])
        with patch("app.ventas.models.Proveedor") as Model:
            Model.query.filter.return_value = q
            self.assertIsNone(get_public_suppliers(supplier_id=999))

    def test_truncated(self):
        rows = [Prov(id=i, nombre=f"P{i}", empresa=f"E{i}") for i in range(3)]
        q = self._query(rows)
        with patch("app.ventas.models.Proveedor") as Model:
            Model.query.filter.return_value = q
            data, truncated = get_public_suppliers(q="P", limit=2)
        self.assertTrue(truncated)
        self.assertEqual(data["count"], 2)
        q.limit.assert_called_with(3)


class InternalSupplierApiTests(unittest.TestCase):
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

    def _ok_data(self):
        return {
            "items": [
                {
                    "id": 1,
                    "nombre": "ALBERT CASTILLO",
                    "empresa": "ANDES AUTO PARTS LTDA",
                    "rut": "78.074.288-7",
                    "giro": "REPUESTOS",
                    "comuna": "PROVIDENCIA",
                    "ciudad": "SANTIAGO",
                    "region": "METROPOLITANA",
                    "pais": "Chile",
                    "activo": True,
                }
            ],
            "count": 1,
        }

    def test_01_por_q(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_ventas"
        ), patch("app.internal_agent.routes.get_public_suppliers", return_value=(self._ok_data(), False)):
            resp = self.client.post(
                "/internal/agent/v1/ventas/suppliers",
                headers=self._headers(),
                data=json.dumps({"q": "ALBERT"}),
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["tool"], "get_supplier")
        self.assertEqual(body["classification"], "CONFIDENTIAL")

    def test_02_not_found(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_ventas"
        ), patch("app.internal_agent.routes.get_public_suppliers", return_value=None):
            resp = self.client.post(
                "/internal/agent/v1/ventas/suppliers",
                headers=self._headers(),
                data=json.dumps({"id": 999}),
            )
        self.assertEqual(resp.status_code, 404)

    def test_03_token_incorrecto(self):
        resp = self.client.post(
            "/internal/agent/v1/ventas/suppliers",
            headers=self._headers({"Authorization": "Bearer wrong"}),
            data=json.dumps({"q": "ALBERT"}),
        )
        self.assertEqual(resp.status_code, 401)

    def test_04_cookie(self):
        self.client.set_cookie("session", "abc")
        resp = self.client.post(
            "/internal/agent/v1/ventas/suppliers",
            headers=self._headers(),
            data=json.dumps({"q": "ALBERT"}),
        )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.get_json()["error_code"], "cookies_not_allowed")

    def test_05_environment(self):
        resp = self.client.post(
            "/internal/agent/v1/ventas/suppliers",
            headers=self._headers({"X-Andes-Env": "production"}),
            data=json.dumps({"q": "ALBERT"}),
        )
        self.assertEqual(resp.status_code, 400)

    def test_06_sin_permiso(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("bob", "vendedor")), patch(
            "app.internal_agent.routes.require_mod_ventas",
            side_effect=InternalAuthError("permission_denied", "Permission mod_ventas is required", 403),
        ):
            resp = self.client.post(
                "/internal/agent/v1/ventas/suppliers",
                headers=self._headers({"X-Andes-Actor": "bob"}),
                data=json.dumps({"q": "ALBERT"}),
            )
        self.assertEqual(resp.status_code, 403)

    def test_07_campo_extra(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_ventas"
        ):
            resp = self.client.post(
                "/internal/agent/v1/ventas/suppliers",
                headers=self._headers(),
                data=json.dumps({"q": "ALBERT", "table": "ventas_proveedores"}),
            )
        self.assertEqual(resp.status_code, 400)

    def test_08_limit_alto(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_ventas"
        ):
            resp = self.client.post(
                "/internal/agent/v1/ventas/suppliers",
                headers=self._headers(),
                data=json.dumps({"q": "ALBERT", "limit": 21}),
            )
        self.assertEqual(resp.status_code, 400)

    def test_09_sin_mezclar_clientes(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_ventas"
        ), patch("app.internal_agent.routes.get_public_suppliers", return_value=(self._ok_data(), False)):
            resp = self.client.post(
                "/internal/agent/v1/ventas/suppliers",
                headers=self._headers(),
                data=json.dumps({"q": "ALBERT"}),
            )
        blob = json.dumps(resp.get_json())
        self.assertNotIn("ventas_clientes", blob)
        self.assertNotIn("cliente_mayorista", blob)
        self.assertNotIn("oc_clientes", blob)


class AssistantBffSupplierTests(unittest.TestCase):
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

    def test_bff_invoca_get_supplier(self):
        fake = {
            "ok": True,
            "tool": "get_supplier",
            "write": False,
            "classification": "CONFIDENTIAL",
            "data": {"items": [], "count": 0},
            "meta": {"limit": 20, "truncated": False, "environment": "local"},
        }
        self._login()
        with patch("app.assistant.routes.invoke_gateway", return_value=(200, fake)) as mocked:
            resp = self.client.post(
                "/assistant/api/invoke",
                json={"tool": "get_supplier", "arguments": {"q": "ALBERT"}, "conversation_id": "c1"},
                headers={"X-CSRF-Token": "csrf-test"},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["tool"], "get_supplier")
        self.assertEqual(mocked.call_args[0][0]["tool"], "get_supplier")
        self.assertNotIn("Bearer", json.dumps(resp.get_json()))

    def test_gateway_apagado(self):
        self._login()
        with patch.dict(os.environ, {"ANDES_AGENT_SERVICE_TOKEN": TOKEN, "ANDES_ENV": "local"}), patch(
            "app.assistant.routes._agent_url", return_value="http://127.0.0.1:9"
        ), patch("urllib.request.urlopen", side_effect=URLError("down")):
            resp = self.client.post(
                "/assistant/api/invoke",
                json={"tool": "get_supplier", "arguments": {"q": "ALBERT"}},
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
        ):
            with patch("app.assistant.routes.invoke_gateway", return_value=(200, {"ok": True, "tool": tool})):
                resp = self.client.post(
                    "/assistant/api/invoke",
                    json={"tool": tool, "arguments": arguments},
                    headers={"X-CSRF-Token": "csrf-test"},
                )
            self.assertEqual(resp.status_code, 200, tool)

    def test_stub_tools_bloqueadas(self):
        self._login()
        resp = self.client.post(
            "/assistant/api/invoke",
            json={"tool": "unknown_tool_xyz", "arguments": {"q": "x"}},
            headers={"X-CSRF-Token": "csrf-test"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error_code"], "tool_not_available")


if __name__ == "__main__":
    unittest.main()
