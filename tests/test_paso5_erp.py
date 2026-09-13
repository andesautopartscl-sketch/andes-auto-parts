from __future__ import annotations

import json
import os
import unittest
from datetime import date, datetime
from unittest.mock import MagicMock, patch
from urllib.error import URLError

from flask import Flask

from app.assistant.routes import assistant_bp
from app.internal_agent.m2m import InternalAuthError
from app.internal_agent.movements import (
    BLOCKED_MOVEMENT_FIELDS,
    get_public_movements,
    validate_movement_args,
)
from app.internal_agent.routes import internal_agent_bp
from app.utils.csrf import CSRF_SESSION_KEY


TOKEN = "erp-test-token"


class Movement:
    def __init__(self, **kwargs):
        self.fecha = kwargs.get("fecha")
        self.tipo = kwargs.get("tipo", "ingreso")
        self.cantidad = kwargs.get("cantidad", 0)
        self.marca = kwargs.get("marca", "")
        self.bodega = kwargs.get("bodega", "")
        self.origen_compra = kwargs.get("origen_compra", "nacional")
        self.usuario = kwargs.get("usuario", "")
        self.observacion = kwargs.get("observacion", "")
        for key, value in kwargs.items():
            setattr(self, key, value)


class MovementSchemaTests(unittest.TestCase):
    def test_codigo_vacio(self):
        with self.assertRaises(InternalAuthError) as ctx:
            validate_movement_args({"codigo": ""})
        self.assertEqual(ctx.exception.code, "invalid_args")

    def test_campo_extra(self):
        with self.assertRaises(InternalAuthError) as ctx:
            validate_movement_args({"codigo": "2404", "order_by": "fecha"})
        self.assertEqual(ctx.exception.code, "invalid_args")

    def test_sql(self):
        with self.assertRaises(InternalAuthError) as ctx:
            validate_movement_args({"codigo": "SELECT * FROM movimientos_stock"})
        self.assertEqual(ctx.exception.code, "invalid_args")

    def test_rango_invalido(self):
        with self.assertRaises(InternalAuthError) as ctx:
            validate_movement_args(
                {"codigo": "2404", "fecha_desde": "2026-09-12", "fecha_hasta": "2026-09-01"}
            )
        self.assertEqual(ctx.exception.code, "invalid_args")

    def test_limit_mayor_50(self):
        with self.assertRaises(InternalAuthError) as ctx:
            validate_movement_args({"codigo": "2404", "limit": 51})
        self.assertEqual(ctx.exception.code, "invalid_args")

    def test_limit_valido_y_fechas(self):
        codigo, desde, hasta, limit = validate_movement_args(
            {"codigo": "2404", "fecha_desde": "2026-09-01", "fecha_hasta": "2026-09-12", "limit": 20}
        )
        self.assertEqual(codigo, "2404")
        self.assertEqual(desde, date(2026, 9, 1))
        self.assertEqual(hasta, date(2026, 9, 12))
        self.assertEqual(limit, 20)


class PublicMovementTests(unittest.TestCase):
    def _query(self, rows):
        q = MagicMock()
        q.filter.return_value = q
        q.order_by.return_value = q
        q.limit.return_value = q
        q.all.return_value = rows
        return q

    def test_producto_con_movimientos_sin_invertir_signo(self):
        rows = [
            Movement(
                fecha=datetime(2026, 9, 10, 15, 0, 0),
                tipo="salida",
                cantidad=-1,
                marca="BOSCH",
                bodega="Bodega 1",
                origen_compra="nacional",
                usuario="albertadmin",
                observacion="Venta",
                precio_venta_neto=12000,
                total_neto=12000,
                proveedor="ACME",
                costo=99,
            )
        ]
        q = self._query(rows)
        with patch("app.internal_agent.product.get_public_product", return_value={"codigo": "2404", "descripcion": "FILTRO DIESEL"}), patch(
            "app.bodega.models.MovimientoStock"
        ) as model:
            model.query.filter.return_value = q
            data, truncated = get_public_movements("2404")
        self.assertEqual(data["codigo"], "2404")
        self.assertEqual(data["count"], 1)
        self.assertFalse(truncated)
        item = data["items"][0]
        self.assertEqual(item["tipo"], "salida")
        self.assertEqual(item["cantidad"], -1)
        self.assertEqual(item["fecha"], "2026-09-10")
        blob = json.dumps(data)
        for field in BLOCKED_MOVEMENT_FIELDS:
            self.assertNotIn(f'"{field}"', blob)

    def test_producto_sin_movimientos(self):
        q = self._query([])
        with patch("app.internal_agent.product.get_public_product", return_value={"codigo": "2404", "descripcion": "X"}), patch(
            "app.bodega.models.MovimientoStock"
        ) as model:
            model.query.filter.return_value = q
            data, truncated = get_public_movements("2404")
        self.assertEqual(data["items"], [])
        self.assertEqual(data["count"], 0)
        self.assertFalse(truncated)

    def test_producto_inexistente(self):
        with patch("app.internal_agent.product.get_public_product", return_value=None):
            self.assertIsNone(get_public_movements("NO-EXISTE"))

    def test_truncated_solo_si_hay_mas_que_limit(self):
        rows = [
            Movement(fecha=datetime(2026, 9, 10, 15, 0, 0), tipo="ingreso", cantidad=1, marca="A", bodega="B")
            for _ in range(3)
        ]
        q = self._query(rows)
        with patch("app.internal_agent.product.get_public_product", return_value={"codigo": "2404", "descripcion": "X"}), patch(
            "app.bodega.models.MovimientoStock"
        ) as model:
            model.query.filter.return_value = q
            data, truncated = get_public_movements("2404", limit=2)
        self.assertTrue(truncated)
        self.assertEqual(data["count"], 2)
        q.limit.assert_called_with(3)


class InternalMovementsApiTests(unittest.TestCase):
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

    def test_01_producto_con_movimientos(self):
        data = {
            "codigo": "2404",
            "descripcion": "FILTRO DIESEL",
            "items": [
                {
                    "fecha": "2026-09-10",
                    "tipo": "salida",
                    "cantidad": -1,
                    "marca": "BOSCH",
                    "bodega": "Bodega 1",
                    "origen_compra": "nacional",
                    "usuario": "albertadmin",
                    "observacion": "Venta",
                }
            ],
            "count": 1,
        }
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_bodega"
        ), patch("app.internal_agent.routes.get_public_movements", return_value=(data, False)):
            resp = self.client.post(
                "/internal/agent/v1/stock/movements",
                headers=self._headers(),
                data=json.dumps({"codigo": "2404"}),
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["tool"], "get_stock_movements")
        self.assertEqual(body["data"]["count"], 1)
        self.assertFalse(body["meta"]["truncated"])

    def test_02_producto_sin_movimientos(self):
        data = {"codigo": "2404", "descripcion": "X", "items": [], "count": 0}
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_bodega"
        ), patch("app.internal_agent.routes.get_public_movements", return_value=(data, False)):
            resp = self.client.post(
                "/internal/agent/v1/stock/movements",
                headers=self._headers(),
                data=json.dumps({"codigo": "2404"}),
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["data"]["items"], [])
        self.assertEqual(resp.get_json()["data"]["count"], 0)

    def test_03_producto_inexistente(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_bodega"
        ), patch("app.internal_agent.routes.get_public_movements", return_value=None):
            resp = self.client.post(
                "/internal/agent/v1/stock/movements",
                headers=self._headers(),
                data=json.dumps({"codigo": "NO-EXISTE"}),
            )
        self.assertEqual(resp.status_code, 404)
        body = resp.get_json()
        self.assertEqual(body["error_code"], "not_found")
        self.assertEqual(body["error"]["message"], "Producto no encontrado")

    def test_04_fecha_desde(self):
        captured = {}

        def fake(codigo, fecha_desde=None, fecha_hasta=None, limit=20):
            captured["args"] = (codigo, fecha_desde, fecha_hasta, limit)
            return (
                {
                    "codigo": codigo,
                    "descripcion": "X",
                    "items": [
                        {
                            "fecha": "2026-09-10",
                            "tipo": "salida",
                            "cantidad": 1,
                            "marca": "BOSCH",
                            "bodega": "Bodega 1",
                            "origen_compra": "nacional",
                            "usuario": "bodega",
                            "observacion": "",
                        }
                    ],
                    "count": 1,
                },
                False,
            )

        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_bodega"
        ), patch("app.internal_agent.routes.get_public_movements", side_effect=fake):
            resp = self.client.post(
                "/internal/agent/v1/stock/movements",
                headers=self._headers(),
                data=json.dumps({"codigo": "2404", "fecha_desde": "2026-09-10"}),
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured["args"][0], "2404")
        self.assertEqual(captured["args"][1], date(2026, 9, 10))
        self.assertIsNone(captured["args"][2])

    def test_05_fecha_hasta(self):
        captured = {}

        def fake(codigo, fecha_desde=None, fecha_hasta=None, limit=20):
            captured["hasta"] = fecha_hasta
            return ({"codigo": codigo, "descripcion": "X", "items": [], "count": 0}, False)

        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_bodega"
        ), patch("app.internal_agent.routes.get_public_movements", side_effect=fake):
            resp = self.client.post(
                "/internal/agent/v1/stock/movements",
                headers=self._headers(),
                data=json.dumps({"codigo": "2404", "fecha_hasta": "2026-09-02"}),
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured["hasta"], date(2026, 9, 2))

    def test_06_rango_invalido(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_bodega"
        ):
            resp = self.client.post(
                "/internal/agent/v1/stock/movements",
                headers=self._headers(),
                data=json.dumps({"codigo": "2404", "fecha_desde": "2026-09-12", "fecha_hasta": "2026-09-01"}),
            )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error_code"], "invalid_args")

    def test_07_limit_valido(self):
        captured = {}

        def fake(codigo, fecha_desde=None, fecha_hasta=None, limit=20):
            captured["limit"] = limit
            return ({"codigo": codigo, "descripcion": "X", "items": [], "count": 0}, False)

        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_bodega"
        ), patch("app.internal_agent.routes.get_public_movements", side_effect=fake):
            resp = self.client.post(
                "/internal/agent/v1/stock/movements",
                headers=self._headers(),
                data=json.dumps({"codigo": "2404", "limit": 20}),
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured["limit"], 20)
        self.assertEqual(resp.get_json()["meta"]["limit"], 20)

    def test_08_limit_mayor_50(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_bodega"
        ):
            resp = self.client.post(
                "/internal/agent/v1/stock/movements",
                headers=self._headers(),
                data=json.dumps({"codigo": "2404", "limit": 51}),
            )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error_code"], "invalid_args")

    def test_09_campo_adicional(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_bodega"
        ):
            resp = self.client.post(
                "/internal/agent/v1/stock/movements",
                headers=self._headers(),
                data=json.dumps({"codigo": "2404", "table": "movimientos_stock"}),
            )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error_code"], "invalid_args")

    def test_10_sql(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_bodega"
        ):
            resp = self.client.post(
                "/internal/agent/v1/stock/movements",
                headers=self._headers(),
                data=json.dumps({"codigo": "2404", "sql": "select 1"}),
            )
        self.assertEqual(resp.status_code, 400)

    def test_11_token_incorrecto(self):
        resp = self.client.post(
            "/internal/agent/v1/stock/movements",
            headers=self._headers({"Authorization": "Bearer wrong"}),
            data=json.dumps({"codigo": "2404"}),
        )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.get_json()["error_code"], "unauthorized")

    def test_12_cookie(self):
        self.client.set_cookie("session", "abc")
        resp = self.client.post(
            "/internal/agent/v1/stock/movements",
            headers=self._headers(),
            data=json.dumps({"codigo": "2404"}),
        )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.get_json()["error_code"], "cookies_not_allowed")

    def test_13_environment_incorrecto(self):
        resp = self.client.post(
            "/internal/agent/v1/stock/movements",
            headers=self._headers({"X-Andes-Env": "production"}),
            data=json.dumps({"codigo": "2404"}),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error_code"], "invalid_environment")

    def test_14_usuario_sin_mod_bodega(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("bob", "vendedor")), patch(
            "app.internal_agent.routes.require_mod_bodega",
            side_effect=InternalAuthError("permission_denied", "Permission mod_bodega is required", 403),
        ):
            resp = self.client.post(
                "/internal/agent/v1/stock/movements",
                headers=self._headers({"X-Andes-Actor": "bob"}),
                data=json.dumps({"codigo": "2404"}),
            )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json()["error_code"], "permission_denied")


class AssistantBffMovementsTests(unittest.TestCase):
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

    def test_bff_invoca_get_stock_movements(self):
        fake = {
            "ok": True,
            "tool": "get_stock_movements",
            "write": False,
            "data": {"codigo": "2404", "descripcion": "FILTRO DIESEL", "items": [], "count": 0},
            "meta": {"limit": 20, "truncated": False, "environment": "local"},
        }
        self._login()
        with patch("app.assistant.routes.invoke_gateway", return_value=(200, fake)) as mocked:
            resp = self.client.post(
                "/assistant/api/invoke",
                json={"tool": "get_stock_movements", "arguments": {"codigo": "2404"}, "conversation_id": "c1"},
                headers={"X-CSRF-Token": "csrf-test"},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["tool"], "get_stock_movements")
        self.assertEqual(mocked.call_args[0][0]["tool"], "get_stock_movements")
        self.assertNotIn("Bearer", json.dumps(resp.get_json()))

    def test_18_gateway_apagado(self):
        self._login()
        with patch.dict(os.environ, {"ANDES_AGENT_SERVICE_TOKEN": TOKEN, "ANDES_ENV": "local"}), patch(
            "app.assistant.routes._agent_url", return_value="http://127.0.0.1:9"
        ), patch("urllib.request.urlopen", side_effect=URLError("down")):
            resp = self.client.post(
                "/assistant/api/invoke",
                json={"tool": "get_stock_movements", "arguments": {"codigo": "2404"}},
                headers={"X-CSRF-Token": "csrf-test"},
            )
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json()["error_code"], "agent_unavailable")

    def test_17_erp_apagado_via_gateway(self):
        self._login()
        with patch(
            "app.assistant.routes.invoke_gateway",
            return_value=(503, {"ok": False, "error_code": "erp_unavailable", "message": "ERP is not reachable"}),
        ):
            resp = self.client.post(
                "/assistant/api/invoke",
                json={"tool": "get_stock_movements", "arguments": {"codigo": "2404"}},
                headers={"X-CSRF-Token": "csrf-test"},
            )
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json()["error_code"], "erp_unavailable")

    def test_19_20_21_tools_previas_siguen_en_allowlist(self):
        self._login()
        for tool, arguments in (
            ("search_catalog", {"q": "filtro"}),
            ("get_product", {"codigo": "2404"}),
            ("get_inventory", {"codigo": "2404"}),
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
