from __future__ import annotations

import json
import os
import unittest
from datetime import date
from unittest.mock import MagicMock, patch
from urllib.error import URLError

from flask import Flask

from app.assistant.routes import assistant_bp
from app.internal_agent.ingresos import (
    BLOCKED_INGRESO_FIELDS,
    get_public_ingresos,
    validate_ingreso_args,
)
from app.internal_agent.m2m import InternalAuthError
from app.internal_agent.routes import internal_agent_bp
from app.utils.csrf import CSRF_SESSION_KEY


TOKEN = "erp-test-token"


class Doc:
    def __init__(self, **kwargs):
        self.id = kwargs.get("id", 1)
        self.numero_documento = kwargs.get("numero_documento", "302")
        self.fecha_documento = kwargs.get("fecha_documento", date(2026, 7, 31))
        self.proveedor_nombre = kwargs.get("proveedor_nombre", "REPUESTOS DEL SUR")
        self.proveedor_rut = kwargs.get("proveedor_rut", "76111111-1")
        self.proveedor_email = kwargs.get("proveedor_email", "hidden@example.com")
        self.anulado = kwargs.get("anulado", False)
        self.usuario = kwargs.get("usuario", "albertadmin")
        self.total_factura = kwargs.get("total_factura", 9999)
        self.iva_factura = kwargs.get("iva_factura", 1900)
        for key, value in kwargs.items():
            setattr(self, key, value)


class Item:
    def __init__(self, **kwargs):
        self.codigo_producto = kwargs.get("codigo_producto", "2404")
        self.descripcion_producto = kwargs.get("descripcion_producto", "FILTRO DIESEL")
        self.marca = kwargs.get("marca", "BOSCH")
        self.bodega = kwargs.get("bodega", "Bodega 1")
        self.origen_compra = kwargs.get("origen_compra", "nacional")
        self.cantidad = kwargs.get("cantidad", 2)
        self.valor_neto = kwargs.get("valor_neto", 4500.0)
        self.precio_venta_neto = kwargs.get("precio_venta_neto", 8900.0)
        self.margen_pct = kwargs.get("margen_pct", 35.0)
        self.codigo_proveedor = kwargs.get("codigo_proveedor", "P-2404")
        for key, value in kwargs.items():
            setattr(self, key, value)


class IngresoSchemaTests(unittest.TestCase):
    def test_campo_extra(self):
        with self.assertRaises(InternalAuthError) as ctx:
            validate_ingreso_args({"codigo": "2404", "sql": "select 1"})
        self.assertEqual(ctx.exception.code, "invalid_args")

    def test_rango_invalido(self):
        with self.assertRaises(InternalAuthError) as ctx:
            validate_ingreso_args(
                {"codigo": "2404", "fecha_desde": "2026-09-12", "fecha_hasta": "2026-09-01"}
            )
        self.assertEqual(ctx.exception.code, "invalid_args")

    def test_limit_mayor_20(self):
        with self.assertRaises(InternalAuthError) as ctx:
            validate_ingreso_args({"codigo": "2404", "limit": 21})
        self.assertEqual(ctx.exception.code, "invalid_args")

    def test_limit_y_filtros(self):
        args = validate_ingreso_args(
            {
                "codigo": "2404",
                "proveedor": "DEL SUR",
                "numero_documento": "302",
                "fecha_desde": "2026-05-01",
                "fecha_hasta": "2026-07-31",
                "limit": 20,
            }
        )
        self.assertEqual(args["codigo"], "2404")
        self.assertEqual(args["proveedor"], "DEL SUR")
        self.assertEqual(args["limit"], 20)


class PublicIngresoTests(unittest.TestCase):
    def _query(self, rows):
        q = MagicMock()
        q.join.return_value = q
        q.filter.return_value = q
        q.order_by.return_value = q
        q.limit.return_value = q
        q.all.return_value = rows
        return q

    def test_producto_con_ingresos_y_anulado(self):
        rows = [
            (
                Item(),
                Doc(anulado=False),
            ),
            (
                Item(cantidad=3, valor_neto=10),
                Doc(id=2, numero_documento="999", anulado=True, fecha_documento=date(2026, 7, 1)),
            ),
        ]
        q = self._query(rows)
        with patch("app.internal_agent.product.get_public_product", return_value={"codigo": "2404", "descripcion": "FILTRO DIESEL"}), patch(
            "app.extensions.db"
        ) as db:
            db.session.query.return_value = q
            data, truncated = get_public_ingresos(codigo="2404", include_finance=True)
        self.assertEqual(data["count"], 2)
        self.assertFalse(truncated)
        self.assertTrue(data["items"][1]["anulado"])
        self.assertEqual(data["items"][0]["costo_neto"], 4500.0)
        blob = json.dumps(data)
        for field in BLOCKED_INGRESO_FIELDS:
            self.assertNotIn(f'"{field}"', blob)

    def test_sin_finanzas_omite_costos(self):
        q = self._query([(Item(), Doc())])
        with patch("app.internal_agent.product.get_public_product", return_value={"codigo": "2404", "descripcion": "X"}), patch(
            "app.extensions.db"
        ) as db:
            db.session.query.return_value = q
            data, _truncated = get_public_ingresos(codigo="2404", include_finance=False)
        item = data["items"][0]
        self.assertNotIn("costo_neto", item)
        self.assertNotIn("precio_venta_neto", item)
        self.assertNotIn("margen_pct", item)

    def test_producto_sin_ingresos(self):
        q = self._query([])
        with patch("app.internal_agent.product.get_public_product", return_value={"codigo": "2404", "descripcion": "X"}), patch(
            "app.extensions.db"
        ) as db:
            db.session.query.return_value = q
            data, truncated = get_public_ingresos(codigo="2404")
        self.assertEqual(data["items"], [])
        self.assertFalse(truncated)

    def test_producto_inexistente(self):
        with patch("app.internal_agent.product.get_public_product", return_value=None):
            self.assertIsNone(get_public_ingresos(codigo="NO-EXISTE"))

    def test_truncated(self):
        rows = [(Item(), Doc(id=i, numero_documento=str(i))) for i in range(3)]
        q = self._query(rows)
        with patch("app.internal_agent.product.get_public_product", return_value={"codigo": "2404", "descripcion": "X"}), patch(
            "app.extensions.db"
        ) as db:
            db.session.query.return_value = q
            data, truncated = get_public_ingresos(codigo="2404", limit=2)
        self.assertTrue(truncated)
        self.assertEqual(data["count"], 2)
        q.limit.assert_called_with(3)


class InternalIngresosApiTests(unittest.TestCase):
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

    def _ok_data(self, finance=True):
        item = {
            "fecha": "2026-07-31",
            "numero_documento": "302",
            "proveedor": "REPUESTOS DEL SUR",
            "anulado": False,
            "codigo": "2404",
            "descripcion": "FILTRO DIESEL",
            "marca": "BOSCH",
            "bodega": "Bodega 1",
            "origen_compra": "nacional",
            "cantidad": 2,
        }
        if finance:
            item["costo_neto"] = 4500.0
            item["precio_venta_neto"] = 8900.0
            item["margen_pct"] = 35.0
        return {"codigo": "2404", "descripcion": "FILTRO DIESEL", "items": [item], "count": 1}

    def test_01_ingreso_existente(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_bodega"
        ), patch("app.internal_agent.routes.actor_can_view_finanzas", return_value=True), patch(
            "app.internal_agent.routes.get_public_ingresos", return_value=(self._ok_data(), False)
        ):
            resp = self.client.post(
                "/internal/agent/v1/bodega/ingresos",
                headers=self._headers(),
                data=json.dumps({"numero_documento": "302"}),
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["tool"], "get_ingresos")
        self.assertEqual(body["classification"], "CONFIDENTIAL")
        self.assertEqual(body["data"]["items"][0]["numero_documento"], "302")

    def test_02_producto_con_ingresos(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_bodega"
        ), patch("app.internal_agent.routes.actor_can_view_finanzas", return_value=True), patch(
            "app.internal_agent.routes.get_public_ingresos", return_value=(self._ok_data(), False)
        ):
            resp = self.client.post(
                "/internal/agent/v1/bodega/ingresos",
                headers=self._headers(),
                data=json.dumps({"codigo": "2404"}),
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["data"]["count"], 1)

    def test_03_producto_sin_ingresos(self):
        data = {"codigo": "2404", "descripcion": "X", "items": [], "count": 0}
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_bodega"
        ), patch("app.internal_agent.routes.actor_can_view_finanzas", return_value=False), patch(
            "app.internal_agent.routes.get_public_ingresos", return_value=(data, False)
        ):
            resp = self.client.post(
                "/internal/agent/v1/bodega/ingresos",
                headers=self._headers(),
                data=json.dumps({"codigo": "2404"}),
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["data"]["items"], [])

    def test_04_producto_inexistente(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_bodega"
        ), patch("app.internal_agent.routes.actor_can_view_finanzas", return_value=False), patch(
            "app.internal_agent.routes.get_public_ingresos", return_value=None
        ):
            resp = self.client.post(
                "/internal/agent/v1/bodega/ingresos",
                headers=self._headers(),
                data=json.dumps({"codigo": "NO-EXISTE"}),
            )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()["error_code"], "not_found")

    def test_05_proveedor(self):
        captured = {}

        def fake(**kwargs):
            captured.update(kwargs)
            return (self._ok_data(), False)

        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_bodega"
        ), patch("app.internal_agent.routes.actor_can_view_finanzas", return_value=True), patch(
            "app.internal_agent.routes.get_public_ingresos", side_effect=fake
        ):
            resp = self.client.post(
                "/internal/agent/v1/bodega/ingresos",
                headers=self._headers(),
                data=json.dumps({"proveedor": "DEL SUR"}),
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured["proveedor"], "DEL SUR")

    def test_06_fecha_desde(self):
        captured = {}

        def fake(**kwargs):
            captured.update(kwargs)
            return (self._ok_data(), False)

        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_bodega"
        ), patch("app.internal_agent.routes.actor_can_view_finanzas", return_value=True), patch(
            "app.internal_agent.routes.get_public_ingresos", side_effect=fake
        ):
            resp = self.client.post(
                "/internal/agent/v1/bodega/ingresos",
                headers=self._headers(),
                data=json.dumps({"codigo": "2404", "fecha_desde": "2026-07-01"}),
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured["fecha_desde"], date(2026, 7, 1))

    def test_07_fecha_hasta(self):
        captured = {}

        def fake(**kwargs):
            captured.update(kwargs)
            return (self._ok_data(), False)

        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_bodega"
        ), patch("app.internal_agent.routes.actor_can_view_finanzas", return_value=True), patch(
            "app.internal_agent.routes.get_public_ingresos", side_effect=fake
        ):
            resp = self.client.post(
                "/internal/agent/v1/bodega/ingresos",
                headers=self._headers(),
                data=json.dumps({"codigo": "2404", "fecha_hasta": "2026-05-12"}),
            )
        self.assertEqual(captured["fecha_hasta"], date(2026, 5, 12))

    def test_08_rango_invalido(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_bodega"
        ):
            resp = self.client.post(
                "/internal/agent/v1/bodega/ingresos",
                headers=self._headers(),
                data=json.dumps({"codigo": "2404", "fecha_desde": "2026-09-12", "fecha_hasta": "2026-09-01"}),
            )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error_code"], "invalid_args")

    def test_09_limit(self):
        captured = {}

        def fake(**kwargs):
            captured.update(kwargs)
            return (self._ok_data(), False)

        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_bodega"
        ), patch("app.internal_agent.routes.actor_can_view_finanzas", return_value=True), patch(
            "app.internal_agent.routes.get_public_ingresos", side_effect=fake
        ):
            resp = self.client.post(
                "/internal/agent/v1/bodega/ingresos",
                headers=self._headers(),
                data=json.dumps({"codigo": "2404", "limit": 20}),
            )
        self.assertEqual(captured["limit"], 20)
        self.assertEqual(resp.get_json()["meta"]["limit"], 20)

    def test_10_limit_mayor_20(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_bodega"
        ):
            resp = self.client.post(
                "/internal/agent/v1/bodega/ingresos",
                headers=self._headers(),
                data=json.dumps({"codigo": "2404", "limit": 21}),
            )
        self.assertEqual(resp.status_code, 400)

    def test_11_campo_adicional(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_bodega"
        ):
            resp = self.client.post(
                "/internal/agent/v1/bodega/ingresos",
                headers=self._headers(),
                data=json.dumps({"codigo": "2404", "table": "ingresos_documentos"}),
            )
        self.assertEqual(resp.status_code, 400)

    def test_12_sql(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_bodega"
        ):
            resp = self.client.post(
                "/internal/agent/v1/bodega/ingresos",
                headers=self._headers(),
                data=json.dumps({"codigo": "2404", "sql": "select 1"}),
            )
        self.assertEqual(resp.status_code, 400)

    def test_13_token_incorrecto(self):
        resp = self.client.post(
            "/internal/agent/v1/bodega/ingresos",
            headers=self._headers({"Authorization": "Bearer wrong"}),
            data=json.dumps({"codigo": "2404"}),
        )
        self.assertEqual(resp.status_code, 401)

    def test_14_cookie(self):
        self.client.set_cookie("session", "abc")
        resp = self.client.post(
            "/internal/agent/v1/bodega/ingresos",
            headers=self._headers(),
            data=json.dumps({"codigo": "2404"}),
        )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.get_json()["error_code"], "cookies_not_allowed")

    def test_15_environment_incorrecto(self):
        resp = self.client.post(
            "/internal/agent/v1/bodega/ingresos",
            headers=self._headers({"X-Andes-Env": "production"}),
            data=json.dumps({"codigo": "2404"}),
        )
        self.assertEqual(resp.status_code, 400)

    def test_16_usuario_sin_permiso(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("bob", "vendedor")), patch(
            "app.internal_agent.routes.require_mod_bodega",
            side_effect=InternalAuthError("permission_denied", "Permission mod_bodega is required", 403),
        ):
            resp = self.client.post(
                "/internal/agent/v1/bodega/ingresos",
                headers=self._headers({"X-Andes-Actor": "bob"}),
                data=json.dumps({"codigo": "2404"}),
            )
        self.assertEqual(resp.status_code, 403)

    def test_17_redaccion_sin_permiso_financiero(self):
        captured = {}

        def fake(**kwargs):
            captured.update(kwargs)
            return (self._ok_data(finance=False), False)

        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "vendedor")), patch(
            "app.internal_agent.routes.require_mod_bodega"
        ), patch("app.internal_agent.routes.actor_can_view_finanzas", return_value=False), patch(
            "app.internal_agent.routes.get_public_ingresos", side_effect=fake
        ):
            resp = self.client.post(
                "/internal/agent/v1/bodega/ingresos",
                headers=self._headers(),
                data=json.dumps({"codigo": "2404"}),
            )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(captured["include_finance"])
        blob = json.dumps(resp.get_json())
        for field in ("costo_neto", "precio_venta_neto", "margen_pct"):
            self.assertNotIn(f'"{field}"', blob)

    def test_19_ingreso_anulado(self):
        data = self._ok_data(finance=False)
        data["items"][0]["anulado"] = True
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_bodega"
        ), patch("app.internal_agent.routes.actor_can_view_finanzas", return_value=False), patch(
            "app.internal_agent.routes.get_public_ingresos", return_value=(data, False)
        ):
            resp = self.client.post(
                "/internal/agent/v1/bodega/ingresos",
                headers=self._headers(),
                data=json.dumps({"codigo": "2404"}),
            )
        self.assertTrue(resp.get_json()["data"]["items"][0]["anulado"])

    def test_20_truncated(self):
        data = self._ok_data(finance=False)
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_bodega"
        ), patch("app.internal_agent.routes.actor_can_view_finanzas", return_value=False), patch(
            "app.internal_agent.routes.get_public_ingresos", return_value=(data, True)
        ):
            resp = self.client.post(
                "/internal/agent/v1/bodega/ingresos",
                headers=self._headers(),
                data=json.dumps({"codigo": "2404", "limit": 20}),
            )
        self.assertTrue(resp.get_json()["meta"]["truncated"])


class AssistantBffIngresosTests(unittest.TestCase):
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

    def test_bff_invoca_get_ingresos(self):
        fake = {
            "ok": True,
            "tool": "get_ingresos",
            "write": False,
            "classification": "CONFIDENTIAL",
            "data": {"codigo": "2404", "items": [], "count": 0},
            "meta": {"limit": 20, "truncated": False, "environment": "local"},
        }
        self._login()
        with patch("app.assistant.routes.invoke_gateway", return_value=(200, fake)) as mocked:
            resp = self.client.post(
                "/assistant/api/invoke",
                json={"tool": "get_ingresos", "arguments": {"codigo": "2404"}, "conversation_id": "c1"},
                headers={"X-CSRF-Token": "csrf-test"},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["tool"], "get_ingresos")
        self.assertEqual(mocked.call_args[0][0]["tool"], "get_ingresos")
        self.assertNotIn("Bearer", json.dumps(resp.get_json()))

    def test_22_gateway_apagado(self):
        self._login()
        with patch.dict(os.environ, {"ANDES_AGENT_SERVICE_TOKEN": TOKEN, "ANDES_ENV": "local"}), patch(
            "app.assistant.routes._agent_url", return_value="http://127.0.0.1:9"
        ), patch("urllib.request.urlopen", side_effect=URLError("down")):
            resp = self.client.post(
                "/assistant/api/invoke",
                json={"tool": "get_ingresos", "arguments": {"codigo": "2404"}},
                headers={"X-CSRF-Token": "csrf-test"},
            )
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json()["error_code"], "agent_unavailable")

    def test_21_erp_apagado_via_gateway(self):
        self._login()
        with patch(
            "app.assistant.routes.invoke_gateway",
            return_value=(503, {"ok": False, "error_code": "erp_unavailable", "message": "ERP is not reachable"}),
        ):
            resp = self.client.post(
                "/assistant/api/invoke",
                json={"tool": "get_ingresos", "arguments": {"codigo": "2404"}},
                headers={"X-CSRF-Token": "csrf-test"},
            )
        self.assertEqual(resp.status_code, 503)

    def test_23_26_tools_previas_siguen_en_allowlist(self):
        self._login()
        for tool, arguments in (
            ("search_catalog", {"q": "filtro"}),
            ("get_product", {"codigo": "2404"}),
            ("get_inventory", {"codigo": "2404"}),
            ("get_stock_movements", {"codigo": "2404"}),
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
