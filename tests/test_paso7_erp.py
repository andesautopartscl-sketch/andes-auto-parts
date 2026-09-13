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
from app.internal_agent.purchase_orders import (
    BLOCKED_OC_FIELDS,
    get_public_purchase_orders,
    validate_purchase_order_args,
)
from app.internal_agent.routes import internal_agent_bp
from app.utils.csrf import CSRF_SESSION_KEY


TOKEN = "erp-test-token"


class OcItem:
    def __init__(self, **kwargs):
        self.codigo_producto = kwargs.get("codigo_producto", "2404")
        self.descripcion = kwargs.get("descripcion", "FILTRO DIESEL")
        self.marca = kwargs.get("marca", "BOSCH")
        self.bodega = kwargs.get("bodega", "Bodega 1")
        self.origen_compra = kwargs.get("origen_compra", "nacional")
        self.cantidad = kwargs.get("cantidad", 2)
        self.precio_unitario = kwargs.get("precio_unitario", 8900.0)
        self.margen_porcentaje = kwargs.get("margen_porcentaje", 35.0)
        self.subtotal = kwargs.get("subtotal", 17800.0)
        for key, value in kwargs.items():
            setattr(self, key, value)


class OcDoc:
    def __init__(self, **kwargs):
        self.id = kwargs.get("id", 1)
        self.numero = kwargs.get("numero", "OC-100")
        self.tipo = kwargs.get("tipo", "orden_compra")
        self.fecha_documento = kwargs.get("fecha_documento", datetime(2026, 7, 31))
        self.status = kwargs.get("status", "pendiente")
        self.cliente_nombre = kwargs.get("cliente_nombre", "REPUESTOS DEL SUR")
        self.cliente_rut = kwargs.get("cliente_rut", "76111111-1")
        self.cliente_email = kwargs.get("cliente_email", "hidden@example.com")
        self.cliente_telefono = kwargs.get("cliente_telefono", "123456")
        self.total = kwargs.get("total", 17800.0)
        self.source_id = kwargs.get("source_id", 99)
        self.root_id = kwargs.get("root_id", 1)
        self.pago_referencia = kwargs.get("pago_referencia", "secret-ref")
        self.items = kwargs.get("items", [OcItem()])
        for key, value in kwargs.items():
            setattr(self, key, value)


class PurchaseOrderSchemaTests(unittest.TestCase):
    def test_campo_extra(self):
        with self.assertRaises(InternalAuthError) as ctx:
            validate_purchase_order_args({"numero": "OC-100", "sql": "select 1"})
        self.assertEqual(ctx.exception.code, "invalid_args")

    def test_rango_invalido(self):
        with self.assertRaises(InternalAuthError) as ctx:
            validate_purchase_order_args(
                {"codigo": "2404", "fecha_desde": "2026-09-12", "fecha_hasta": "2026-09-01"}
            )
        self.assertEqual(ctx.exception.code, "invalid_args")

    def test_limit_mayor_20(self):
        with self.assertRaises(InternalAuthError) as ctx:
            validate_purchase_order_args({"numero": "OC-100", "limit": 21})
        self.assertEqual(ctx.exception.code, "invalid_args")

    def test_limit_y_filtros(self):
        args = validate_purchase_order_args(
            {
                "numero": "oc-100",
                "proveedor": "DEL SUR",
                "estado": "pendiente",
                "codigo": "2404",
                "fecha_desde": "2026-05-01",
                "fecha_hasta": "2026-07-31",
                "limit": 20,
            }
        )
        self.assertEqual(args["numero"], "OC-100")
        self.assertEqual(args["proveedor"], "DEL SUR")
        self.assertEqual(args["estado"], "pendiente")
        self.assertEqual(args["codigo"], "2404")
        self.assertEqual(args["limit"], 20)

    def test_estado_inventado(self):
        with self.assertRaises(InternalAuthError) as ctx:
            validate_purchase_order_args({"estado": "borrador"})
        self.assertEqual(ctx.exception.code, "invalid_args")


class PublicPurchaseOrderTests(unittest.TestCase):
    def _query(self, rows):
        q = MagicMock()
        q.filter.return_value = q
        q.order_by.return_value = q
        q.limit.return_value = q
        q.all.return_value = rows
        return q

    def test_oc_existente_sin_secretos(self):
        q = self._query([OcDoc()])
        with patch("app.ventas.models.DocumentoVenta") as Doc:
            Doc.query.filter.return_value = q
            data, truncated = get_public_purchase_orders(numero="OC-100", include_finance=True)
        self.assertEqual(data["count"], 1)
        self.assertFalse(truncated)
        self.assertEqual(data["items"][0]["numero"], "OC-100")
        self.assertEqual(data["items"][0]["tipo"], "orden_compra")
        self.assertEqual(data["items"][0]["proveedor"], "REPUESTOS DEL SUR")
        self.assertEqual(data["items"][0]["total"], 17800.0)
        self.assertEqual(data["items"][0]["items"][0]["precio"], 8900.0)
        blob = json.dumps(data)
        for field in BLOCKED_OC_FIELDS:
            self.assertNotIn(f'"{field}"', blob)

    def test_sin_finanzas_omite_precios(self):
        q = self._query([OcDoc()])
        with patch("app.ventas.models.DocumentoVenta") as Doc:
            Doc.query.filter.return_value = q
            data, _truncated = get_public_purchase_orders(numero="OC-100", include_finance=False)
        item = data["items"][0]
        self.assertNotIn("total", item)
        self.assertNotIn("precio", item["items"][0])
        self.assertNotIn("margen_pct", item["items"][0])
        self.assertNotIn("subtotal", item["items"][0])

    def test_oc_inexistente(self):
        q = self._query([])
        with patch("app.ventas.models.DocumentoVenta") as Doc:
            Doc.query.filter.return_value = q
            self.assertIsNone(get_public_purchase_orders(numero="NO-EXISTE"))

    def test_producto_inexistente(self):
        with patch("app.internal_agent.product.get_public_product", return_value=None):
            self.assertIsNone(get_public_purchase_orders(codigo="NO-EXISTE"))

    def test_truncated(self):
        rows = [OcDoc(id=i, numero=f"OC-{i}") for i in range(3)]
        q = self._query(rows)
        with patch("app.ventas.models.DocumentoVenta") as Doc:
            Doc.query.filter.return_value = q
            data, truncated = get_public_purchase_orders(limit=2)
        self.assertTrue(truncated)
        self.assertEqual(data["count"], 2)
        q.limit.assert_called_with(3)


class InternalPurchaseOrdersApiTests(unittest.TestCase):
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
        line = {
            "codigo": "2404",
            "descripcion": "FILTRO DIESEL",
            "cantidad": 2,
            "marca": "BOSCH",
            "bodega": "Bodega 1",
            "origen_compra": "nacional",
        }
        if finance:
            line["precio"] = 8900.0
            line["margen_pct"] = 35.0
            line["subtotal"] = 17800.0
        doc = {
            "numero": "OC-100",
            "tipo": "orden_compra",
            "fecha": "2026-07-31",
            "estado": "pendiente",
            "proveedor": "REPUESTOS DEL SUR",
            "lineas": 1,
            "items": [line],
        }
        if finance:
            doc["total"] = 17800.0
        return {"items": [doc], "count": 1}

    def test_01_oc_existente(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_ventas"
        ), patch("app.internal_agent.routes.actor_can_view_oc_finance", return_value=True), patch(
            "app.internal_agent.routes.get_public_purchase_orders", return_value=(self._ok_data(), False)
        ):
            resp = self.client.post(
                "/internal/agent/v1/ventas/purchase-orders",
                headers=self._headers(),
                data=json.dumps({"numero": "OC-100"}),
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["tool"], "get_purchase_orders")
        self.assertEqual(body["classification"], "CONFIDENTIAL")
        self.assertEqual(body["data"]["items"][0]["numero"], "OC-100")

    def test_02_oc_inexistente(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_ventas"
        ), patch("app.internal_agent.routes.actor_can_view_oc_finance", return_value=False), patch(
            "app.internal_agent.routes.get_public_purchase_orders", return_value=None
        ):
            resp = self.client.post(
                "/internal/agent/v1/ventas/purchase-orders",
                headers=self._headers(),
                data=json.dumps({"numero": "NO-EXISTE"}),
            )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()["error_code"], "not_found")

    def test_03_filtro_estado(self):
        captured = {}

        def fake(**kwargs):
            captured.update(kwargs)
            return (self._ok_data(), False)

        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_ventas"
        ), patch("app.internal_agent.routes.actor_can_view_oc_finance", return_value=True), patch(
            "app.internal_agent.routes.get_public_purchase_orders", side_effect=fake
        ):
            resp = self.client.post(
                "/internal/agent/v1/ventas/purchase-orders",
                headers=self._headers(),
                data=json.dumps({"estado": "pendiente"}),
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured["estado"], "pendiente")

    def test_04_filtro_proveedor(self):
        captured = {}

        def fake(**kwargs):
            captured.update(kwargs)
            return (self._ok_data(), False)

        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_ventas"
        ), patch("app.internal_agent.routes.actor_can_view_oc_finance", return_value=True), patch(
            "app.internal_agent.routes.get_public_purchase_orders", side_effect=fake
        ):
            resp = self.client.post(
                "/internal/agent/v1/ventas/purchase-orders",
                headers=self._headers(),
                data=json.dumps({"proveedor": "DEL SUR"}),
            )
        self.assertEqual(captured["proveedor"], "DEL SUR")

    def test_05_filtro_codigo(self):
        captured = {}

        def fake(**kwargs):
            captured.update(kwargs)
            return (self._ok_data(), False)

        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_ventas"
        ), patch("app.internal_agent.routes.actor_can_view_oc_finance", return_value=True), patch(
            "app.internal_agent.routes.get_public_purchase_orders", side_effect=fake
        ):
            resp = self.client.post(
                "/internal/agent/v1/ventas/purchase-orders",
                headers=self._headers(),
                data=json.dumps({"codigo": "2404"}),
            )
        self.assertEqual(captured["codigo"], "2404")

    def test_06_fecha_desde(self):
        captured = {}

        def fake(**kwargs):
            captured.update(kwargs)
            return (self._ok_data(), False)

        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_ventas"
        ), patch("app.internal_agent.routes.actor_can_view_oc_finance", return_value=True), patch(
            "app.internal_agent.routes.get_public_purchase_orders", side_effect=fake
        ):
            resp = self.client.post(
                "/internal/agent/v1/ventas/purchase-orders",
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
            "app.internal_agent.routes.require_mod_ventas"
        ), patch("app.internal_agent.routes.actor_can_view_oc_finance", return_value=True), patch(
            "app.internal_agent.routes.get_public_purchase_orders", side_effect=fake
        ):
            self.client.post(
                "/internal/agent/v1/ventas/purchase-orders",
                headers=self._headers(),
                data=json.dumps({"codigo": "2404", "fecha_hasta": "2026-05-12"}),
            )
        self.assertEqual(captured["fecha_hasta"], date(2026, 5, 12))

    def test_08_rango_invalido(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_ventas"
        ):
            resp = self.client.post(
                "/internal/agent/v1/ventas/purchase-orders",
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
            "app.internal_agent.routes.require_mod_ventas"
        ), patch("app.internal_agent.routes.actor_can_view_oc_finance", return_value=True), patch(
            "app.internal_agent.routes.get_public_purchase_orders", side_effect=fake
        ):
            resp = self.client.post(
                "/internal/agent/v1/ventas/purchase-orders",
                headers=self._headers(),
                data=json.dumps({"codigo": "2404", "limit": 20}),
            )
        self.assertEqual(captured["limit"], 20)
        self.assertEqual(resp.get_json()["meta"]["limit"], 20)

    def test_10_limit_mayor_20(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_ventas"
        ):
            resp = self.client.post(
                "/internal/agent/v1/ventas/purchase-orders",
                headers=self._headers(),
                data=json.dumps({"numero": "OC-100", "limit": 21}),
            )
        self.assertEqual(resp.status_code, 400)

    def test_11_campo_adicional(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_ventas"
        ):
            resp = self.client.post(
                "/internal/agent/v1/ventas/purchase-orders",
                headers=self._headers(),
                data=json.dumps({"numero": "OC-100", "table": "ventas_documentos"}),
            )
        self.assertEqual(resp.status_code, 400)

    def test_12_sql(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_ventas"
        ):
            resp = self.client.post(
                "/internal/agent/v1/ventas/purchase-orders",
                headers=self._headers(),
                data=json.dumps({"numero": "OC-100", "sql": "select 1"}),
            )
        self.assertEqual(resp.status_code, 400)

    def test_13_token_incorrecto(self):
        resp = self.client.post(
            "/internal/agent/v1/ventas/purchase-orders",
            headers=self._headers({"Authorization": "Bearer wrong"}),
            data=json.dumps({"numero": "OC-100"}),
        )
        self.assertEqual(resp.status_code, 401)

    def test_14_cookie(self):
        self.client.set_cookie("session", "abc")
        resp = self.client.post(
            "/internal/agent/v1/ventas/purchase-orders",
            headers=self._headers(),
            data=json.dumps({"numero": "OC-100"}),
        )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.get_json()["error_code"], "cookies_not_allowed")

    def test_15_environment_incorrecto(self):
        resp = self.client.post(
            "/internal/agent/v1/ventas/purchase-orders",
            headers=self._headers({"X-Andes-Env": "production"}),
            data=json.dumps({"numero": "OC-100"}),
        )
        self.assertEqual(resp.status_code, 400)

    def test_16_usuario_sin_permiso(self):
        with patch("app.internal_agent.routes.resolve_actor", return_value=("bob", "vendedor")), patch(
            "app.internal_agent.routes.require_mod_ventas",
            side_effect=InternalAuthError("permission_denied", "Permission mod_ventas is required", 403),
        ):
            resp = self.client.post(
                "/internal/agent/v1/ventas/purchase-orders",
                headers=self._headers({"X-Andes-Actor": "bob"}),
                data=json.dumps({"numero": "OC-100"}),
            )
        self.assertEqual(resp.status_code, 403)

    def test_17_redaccion_sin_permiso_financiero(self):
        captured = {}

        def fake(**kwargs):
            captured.update(kwargs)
            return (self._ok_data(finance=False), False)

        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "vendedor")), patch(
            "app.internal_agent.routes.require_mod_ventas"
        ), patch("app.internal_agent.routes.actor_can_view_oc_finance", return_value=False), patch(
            "app.internal_agent.routes.get_public_purchase_orders", side_effect=fake
        ):
            resp = self.client.post(
                "/internal/agent/v1/ventas/purchase-orders",
                headers=self._headers(),
                data=json.dumps({"numero": "OC-100"}),
            )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(captured["include_finance"])
        blob = json.dumps(resp.get_json())
        for field in ("precio", "margen_pct", "subtotal", "total"):
            self.assertNotIn(f'"{field}"', blob)

    def test_20_truncated(self):
        data = self._ok_data(finance=False)
        with patch("app.internal_agent.routes.resolve_actor", return_value=("albert", "admin")), patch(
            "app.internal_agent.routes.require_mod_ventas"
        ), patch("app.internal_agent.routes.actor_can_view_oc_finance", return_value=False), patch(
            "app.internal_agent.routes.get_public_purchase_orders", return_value=(data, True)
        ):
            resp = self.client.post(
                "/internal/agent/v1/ventas/purchase-orders",
                headers=self._headers(),
                data=json.dumps({"codigo": "2404", "limit": 20}),
            )
        self.assertTrue(resp.get_json()["meta"]["truncated"])


class AssistantBffPurchaseOrdersTests(unittest.TestCase):
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

    def test_bff_invoca_get_purchase_orders(self):
        fake = {
            "ok": True,
            "tool": "get_purchase_orders",
            "write": False,
            "classification": "CONFIDENTIAL",
            "data": {"items": [], "count": 0},
            "meta": {"limit": 20, "truncated": False, "environment": "local"},
        }
        self._login()
        with patch("app.assistant.routes.invoke_gateway", return_value=(200, fake)) as mocked:
            resp = self.client.post(
                "/assistant/api/invoke",
                json={"tool": "get_purchase_orders", "arguments": {"numero": "OC-100"}, "conversation_id": "c1"},
                headers={"X-CSRF-Token": "csrf-test"},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["tool"], "get_purchase_orders")
        self.assertEqual(mocked.call_args[0][0]["tool"], "get_purchase_orders")
        self.assertNotIn("Bearer", json.dumps(resp.get_json()))

    def test_22_gateway_apagado(self):
        self._login()
        with patch.dict(os.environ, {"ANDES_AGENT_SERVICE_TOKEN": TOKEN, "ANDES_ENV": "local"}), patch(
            "app.assistant.routes._agent_url", return_value="http://127.0.0.1:9"
        ), patch("urllib.request.urlopen", side_effect=URLError("down")):
            resp = self.client.post(
                "/assistant/api/invoke",
                json={"tool": "get_purchase_orders", "arguments": {"numero": "OC-100"}},
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
                json={"tool": "get_purchase_orders", "arguments": {"numero": "OC-100"}},
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
            ("get_ingresos", {"codigo": "2404"}),
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
