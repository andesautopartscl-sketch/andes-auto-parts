from __future__ import annotations

import json
import os
import unittest
from datetime import date
from unittest.mock import MagicMock, patch
from urllib.error import URLError

from flask import Flask

from app.assistant.routes import assistant_bp
from app.internal_agent.dashboard import (
    get_public_dashboard_kpis,
    resolve_period_window,
    validate_dashboard_args,
)
from app.internal_agent.m2m import InternalAuthError
from app.internal_agent.routes import internal_agent_bp
from app.utils.csrf import CSRF_SESSION_KEY

TOKEN = "erp-test-token"


class DashboardSchemaTests(unittest.TestCase):
    def test_ok_defaults(self):
        args = validate_dashboard_args({})
        self.assertEqual(args["periodo"], "snapshot")

    def test_periodo_invalido(self):
        with self.assertRaises(InternalAuthError):
            validate_dashboard_args({"periodo": "year"})

    def test_rango_invertido(self):
        with self.assertRaises(InternalAuthError):
            resolve_period_window(
                {
                    "periodo": "custom",
                    "fecha_desde": date(2026, 9, 10),
                    "fecha_hasta": date(2026, 9, 1),
                    "top_limit": 5,
                    "stock_threshold": 3,
                    "stock_limit": 10,
                }
            )

    def test_rango_mas_90(self):
        with self.assertRaises(InternalAuthError):
            resolve_period_window(
                {
                    "periodo": "custom",
                    "fecha_desde": date(2026, 1, 1),
                    "fecha_hasta": date(2026, 6, 1),
                    "top_limit": 5,
                    "stock_threshold": 3,
                    "stock_limit": 10,
                }
            )

    def test_sql(self):
        with self.assertRaises(InternalAuthError):
            validate_dashboard_args({"periodo": "SELECT 1"})


class PublicDashboardTests(unittest.TestCase):
    def test_redaccion_null_no_cero(self):
        with patch("app.internal_agent.dashboard._sum_ventas", return_value=10.0), patch(
            "app.internal_agent.dashboard._count_docs", return_value=2
        ), patch(
            "app.internal_agent.dashboard._chart_data",
            return_value=[{"dia": "2026-09-01", "total": 10.0}],
        ), patch(
            "app.internal_agent.dashboard._top_productos",
            return_value=[{"codigo": "A", "descripcion": "X", "qty": 1, "venta": 10.0}],
        ), patch(
            "app.internal_agent.dashboard._top_clientes",
            return_value=[{"nombre": "C", "docs": 1, "total": 10.0}],
        ), patch("app.internal_agent.dashboard._stock_critico", return_value=[]):
            result = get_public_dashboard_kpis(
                {
                    "periodo": "snapshot",
                    "fecha_desde": None,
                    "fecha_hasta": None,
                    "top_limit": 5,
                    "stock_threshold": 3,
                    "stock_limit": 10,
                },
                include_finance=False,
                include_stock=True,
                today=date(2026, 9, 13),
            )
        data = result["data"]
        self.assertIsNone(data["ventas_hoy"])
        self.assertIsNone(data["ventas_periodo"])
        self.assertIsNone(data["chart_data"][0]["total"])
        self.assertEqual(data["docs_hoy"], 2)
        self.assertEqual(data["top_productos"][0]["qty"], 1)
        self.assertIsNone(data["top_productos"][0]["venta"])

    def test_sin_stock_degrada(self):
        with patch("app.internal_agent.dashboard._sum_ventas", return_value=0.0), patch(
            "app.internal_agent.dashboard._count_docs", return_value=0
        ), patch("app.internal_agent.dashboard._chart_data", return_value=[]), patch(
            "app.internal_agent.dashboard._top_productos", return_value=[]
        ), patch("app.internal_agent.dashboard._top_clientes", return_value=[]), patch(
            "app.internal_agent.dashboard._stock_critico"
        ) as stock:
            result = get_public_dashboard_kpis(
                {
                    "periodo": "7d",
                    "fecha_desde": None,
                    "fecha_hasta": None,
                    "top_limit": 5,
                    "stock_threshold": 3,
                    "stock_limit": 10,
                },
                include_finance=True,
                include_stock=False,
                today=date(2026, 9, 13),
            )
        stock.assert_not_called()
        self.assertIsNone(result["data"]["stock_critico"])
        self.assertFalse(result["meta"]["stock_incluido"])

    def test_tops_acotados_al_periodo(self):
        captured = {}

        def fake_tops(start, end, limit):
            captured["start"] = start
            captured["end"] = end
            captured["limit"] = limit
            return []

        with patch("app.internal_agent.dashboard._sum_ventas", return_value=0.0), patch(
            "app.internal_agent.dashboard._count_docs", return_value=0
        ), patch("app.internal_agent.dashboard._chart_data", return_value=[]), patch(
            "app.internal_agent.dashboard._top_productos", side_effect=fake_tops
        ), patch("app.internal_agent.dashboard._top_clientes", return_value=[]), patch(
            "app.internal_agent.dashboard._stock_critico", return_value=[]
        ):
            get_public_dashboard_kpis(
                {
                    "periodo": "7d",
                    "fecha_desde": None,
                    "fecha_hasta": None,
                    "top_limit": 10,
                    "stock_threshold": 3,
                    "stock_limit": 10,
                },
                include_finance=True,
                include_stock=True,
                today=date(2026, 9, 13),
            )
        self.assertEqual(captured["start"], date(2026, 9, 7))
        self.assertEqual(captured["end"], date(2026, 9, 13))
        self.assertEqual(captured["limit"], 10)


class InternalDashboardApiTests(unittest.TestCase):
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
        fake = {
            "data": {
                "ventas_hoy": 1.0,
                "ventas_mes": 2.0,
                "ventas_periodo": 2.0,
                "docs_hoy": 1,
                "docs_mes": 2,
                "docs_periodo": 2,
                "chart_data": [],
                "top_productos": [],
                "top_clientes": [],
                "stock_critico": [],
            },
            "meta": {"periodo": "snapshot", "finanzas": True, "stock_incluido": True},
        }
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_dashboard"
        ), patch("app.internal_agent.routes.actor_can_view_finanzas", return_value=True), patch(
            "app.internal_agent.routes.actor_can_view_stock", return_value=True
        ), patch("app.internal_agent.routes.get_public_dashboard_kpis", return_value=fake):
            resp = self.client.post(
                "/internal/agent/v1/dashboard/kpis",
                headers=self._headers(),
                data=json.dumps({"periodo": "snapshot"}),
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["tool"], "get_dashboard_kpis")

    def test_02_cookie(self):
        self.client.set_cookie("session", "abc")
        resp = self.client.post(
            "/internal/agent/v1/dashboard/kpis",
            headers=self._headers(),
            data=json.dumps({}),
        )
        self.assertEqual(resp.status_code, 401)

    def test_03_sin_permiso(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("bob", "x")), patch(
            "app.internal_agent.routes.require_mod_dashboard",
            side_effect=InternalAuthError("permission_denied", "Permission mod_dashboard is required", 403),
        ):
            resp = self.client.post(
                "/internal/agent/v1/dashboard/kpis",
                headers=self._headers({"X-Andes-Actor": "bob"}),
                data=json.dumps({}),
            )
        self.assertEqual(resp.status_code, 403)

    def test_04_campo_extra(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_dashboard"
        ):
            resp = self.client.post(
                "/internal/agent/v1/dashboard/kpis",
                headers=self._headers(),
                data=json.dumps({"sql": "x"}),
            )
        self.assertEqual(resp.status_code, 400)


class AssistantBffDashboardTests(unittest.TestCase):
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

    def test_bff_invoca_kpis(self):
        fake = {
            "ok": True,
            "tool": "get_dashboard_kpis",
            "write": False,
            "classification": "CONFIDENTIAL",
            "data": {"ventas_hoy": None, "docs_hoy": 1, "chart_data": [], "top_productos": [], "top_clientes": [], "stock_critico": None},
            "meta": {"environment": "local", "finanzas": False, "stock_incluido": False},
        }
        self._login()
        with patch("app.assistant.routes.invoke_gateway", return_value=(200, fake)) as mocked:
            resp = self.client.post(
                "/assistant/api/invoke",
                json={"tool": "get_dashboard_kpis", "arguments": {"periodo": "7d"}},
                headers={"X-CSRF-Token": "csrf-test"},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mocked.call_args[0][0]["tool"], "get_dashboard_kpis")
        self.assertNotIn("Bearer", json.dumps(resp.get_json()))

    def test_tools_previas(self):
        self._login()
        for tool, arguments in (
            ("search_catalog", {"q": "x"}),
            ("get_product", {"codigo": "2404"}),
            ("get_inventory", {"codigo": "2404"}),
            ("check_stock", {"items": [{"codigo": "2404", "cantidad": 1}]}),
            ("get_stock_movements", {"codigo": "2404"}),
            ("get_ingresos", {"codigo": "2404"}),
            ("get_purchase_orders", {"numero": "OC-1"}),
            ("get_customer", {"q": "A"}),
            ("get_supplier", {"q": "A"}),
        ):
            with patch("app.assistant.routes.invoke_gateway", return_value=(200, {"ok": True, "tool": tool})):
                resp = self.client.post(
                    "/assistant/api/invoke",
                    json={"tool": tool, "arguments": arguments},
                    headers={"X-CSRF-Token": "csrf-test"},
                )
            self.assertEqual(resp.status_code, 200, tool)


if __name__ == "__main__":
    unittest.main()
