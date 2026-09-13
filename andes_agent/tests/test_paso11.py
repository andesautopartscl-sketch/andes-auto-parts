from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlparse

from andes_agent.config import Settings
from andes_agent.schemas import SchemaError, validate_tool_arguments
from andes_agent.server import create_app
from andes_agent.tools.registry import list_tools

TOKEN = "test-service-token"


def _kpi_payload(*, finance=True, stock=True):
    data = {
        "ventas_hoy": 100.0 if finance else None,
        "ventas_mes": 500.0 if finance else None,
        "ventas_periodo": 200.0 if finance else None,
        "docs_hoy": 2,
        "docs_mes": 10,
        "docs_periodo": 4,
        "chart_data": [
            {"dia": "2026-09-10", "total": 50.0 if finance else None},
            {"dia": "2026-09-11", "total": 0.0 if finance else None},
        ],
        "top_productos": [
            {"codigo": "2404", "descripcion": "FILTRO", "qty": 3, "venta": 90.0 if finance else None}
        ],
        "top_clientes": [{"nombre": "ALBERT", "docs": 2, "total": 90.0 if finance else None}],
        "stock_critico": [{"codigo": "X1", "marca": "M", "bodega": "B1", "stock": 1}] if stock else None,
    }
    return {
        "ok": True,
        "tool": "get_dashboard_kpis",
        "classification": "CONFIDENTIAL",
        "data": data,
        "meta": {
            "environment": "local",
            "periodo": "snapshot",
            "fecha_desde": "2026-09-01",
            "fecha_hasta": "2026-09-13",
            "finanzas": finance,
            "stock_incluido": stock,
        },
    }


class _FakeERP(BaseHTTPRequestHandler):
    last = {}
    force_permission_denied = False
    force_no_finance = False
    force_no_stock = False

    def _capture(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8") or "{}") if raw else {}
        except json.JSONDecodeError:
            body = {}
        type(self).last = {
            "method": self.command,
            "path": urlparse(self.path).path,
            "cookie": self.headers.get("Cookie"),
            "body": body,
        }

    def _reply(self, status, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._capture()
        path = urlparse(self.path).path
        if path.startswith("/internal/agent/v1/catalog/product/"):
            self._reply(200, {"ok": True, "tool": "get_product", "data": {"codigo": "2404", "descripcion": "X", "marca": "", "modelo": "", "motor": "", "anio": "", "activo": True, "categoria": "", "subcategoria": ""}, "meta": {"environment": "local"}})
            return
        self._reply(404, {"ok": False})

    def do_POST(self):
        self._capture()
        path = urlparse(self.path).path
        stubs = {
            "/internal/agent/v1/catalog/search": {"ok": True, "tool": "search_catalog", "data": {"items": [], "count": 0}, "meta": {"limit": 10, "truncated": False, "environment": "local"}},
            "/internal/agent/v1/inventory/stock": {"ok": True, "tool": "get_inventory", "data": {"codigo": "2404", "descripcion": "X", "items": [], "total_stock": 0}, "meta": {"environment": "local"}},
            "/internal/agent/v1/inventory/check-stock": {"ok": True, "tool": "check_stock", "data": {"available": True, "items": []}, "meta": {"environment": "local"}},
            "/internal/agent/v1/stock/movements": {"ok": True, "tool": "get_stock_movements", "data": {"codigo": "2404", "descripcion": "X", "items": [], "count": 0}, "meta": {"limit": 20, "truncated": False, "environment": "local"}},
            "/internal/agent/v1/bodega/ingresos": {"ok": True, "tool": "get_ingresos", "data": {"codigo": "2404", "descripcion": "X", "items": [], "count": 0}, "meta": {"limit": 20, "truncated": False, "environment": "local"}},
            "/internal/agent/v1/ventas/purchase-orders": {"ok": True, "tool": "get_purchase_orders", "data": {"items": [], "count": 0}, "meta": {"limit": 20, "truncated": False, "environment": "local"}},
            "/internal/agent/v1/ventas/customers": {"ok": True, "tool": "get_customer", "data": {"items": [], "count": 0}, "meta": {"limit": 20, "truncated": False, "environment": "local"}},
            "/internal/agent/v1/ventas/suppliers": {"ok": True, "tool": "get_supplier", "data": {"items": [], "count": 0}, "meta": {"limit": 20, "truncated": False, "environment": "local"}},
        }
        if path in stubs:
            self._reply(200, stubs[path])
            return
        if path == "/internal/agent/v1/dashboard/kpis":
            if getattr(type(self), "force_permission_denied", False):
                self._reply(403, {"ok": False, "error_code": "permission_denied", "message": "Permission mod_dashboard is required"})
                return
            finance = not bool(getattr(type(self), "force_no_finance", False))
            stock = not bool(getattr(type(self), "force_no_stock", False))
            payload = _kpi_payload(finance=finance, stock=stock)
            body = type(self).last.get("body") or {}
            if body.get("periodo"):
                payload["meta"]["periodo"] = body.get("periodo")
            self._reply(200, payload)
            return
        self._reply(404, {"ok": False})

    def log_message(self, *_args):
        return


def _start_erp():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeERP)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


def _request(app, method, path, headers=None, payload=None):
    raw = b""
    if payload is not None:
        raw = json.dumps(payload).encode("utf-8")
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "wsgi.input": BytesIO(raw),
        "CONTENT_LENGTH": str(len(raw)),
        "wsgi.errors": BytesIO(),
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": "http",
        "wsgi.multithread": False,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
    }
    for key, value in (headers or {}).items():
        environ["HTTP_" + key.upper().replace("-", "_")] = value
    captured = {}

    def start_response(status, response_headers):
        captured["status"] = status
        captured["headers"] = dict(response_headers)

    body = b"".join(app(environ, start_response))
    return int(captured["status"].split()[0]), json.loads(body.decode("utf-8"))


class DashboardKpisPaso11Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.audit_path = Path(self.tmp.name) / "audit.jsonl"
        self.erp, self.erp_url = _start_erp()
        _FakeERP.force_permission_denied = False
        _FakeERP.force_no_finance = False
        _FakeERP.force_no_stock = False
        self.app = create_app(
            Settings(
                environment="local",
                service_token=TOKEN,
                host="127.0.0.1",
                port=5055,
                rate_limit_max=1000,
                rate_limit_window_seconds=60,
                audit_path=self.audit_path,
                erp_base_url=self.erp_url,
            )
        )

    def tearDown(self):
        self.erp.shutdown()
        self.erp.server_close()
        self.tmp.cleanup()

    def _auth(self, extra=None):
        headers = {"Authorization": f"Bearer {TOKEN}", "X-Andes-Env": "local"}
        if extra:
            headers.update(extra)
        return headers

    def _invoke(self, tool, arguments, headers=None):
        return _request(
            self.app,
            "POST",
            "/v1/invoke",
            headers=headers or self._auth(),
            payload={
                "agent_id": "andes-assistant",
                "conversation_id": "conv-kpi",
                "actor_user": "albert",
                "tool": tool,
                "arguments": arguments,
            },
        )

    def test_01_snapshot(self):
        code, body = self._invoke("get_dashboard_kpis", {})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertFalse(body["write"])
        self.assertEqual(body["classification"], "CONFIDENTIAL")
        self.assertEqual(body["data"]["ventas_hoy"], 100.0)
        self.assertEqual(_FakeERP.last.get("path"), "/internal/agent/v1/dashboard/kpis")

    def test_02_7d(self):
        code, body = self._invoke("get_dashboard_kpis", {"periodo": "7d"})
        self.assertEqual(code, 200)
        self.assertEqual(_FakeERP.last["body"].get("periodo"), "7d")

    def test_03_sin_finanzas_null_no_cero(self):
        _FakeERP.force_no_finance = True
        code, body = self._invoke("get_dashboard_kpis", {})
        self.assertEqual(code, 200)
        self.assertIsNone(body["data"]["ventas_hoy"])
        self.assertIsNone(body["data"]["ventas_mes"])
        self.assertIsNone(body["data"]["ventas_periodo"])
        self.assertIsNone(body["data"]["chart_data"][0]["total"])
        self.assertIsNone(body["data"]["top_productos"][0]["venta"])
        self.assertIsNone(body["data"]["top_clientes"][0]["total"])
        self.assertEqual(body["data"]["docs_hoy"], 2)
        self.assertEqual(body["data"]["top_productos"][0]["qty"], 3)

    def test_04_sin_stock(self):
        _FakeERP.force_no_stock = True
        code, body = self._invoke("get_dashboard_kpis", {})
        self.assertEqual(code, 200)
        self.assertIsNone(body["data"]["stock_critico"])

    def test_05_rango_custom(self):
        code, body = self._invoke(
            "get_dashboard_kpis",
            {"fecha_desde": "2026-09-01", "fecha_hasta": "2026-09-10"},
        )
        self.assertEqual(code, 200)

    def test_06_rango_mas_90(self):
        code, body = self._invoke(
            "get_dashboard_kpis",
            {"fecha_desde": "2026-01-01", "fecha_hasta": "2026-06-01"},
        )
        self.assertEqual(code, 400)

    def test_07_fechas_invertidas(self):
        code, body = self._invoke(
            "get_dashboard_kpis",
            {"fecha_desde": "2026-09-10", "fecha_hasta": "2026-09-01"},
        )
        self.assertEqual(code, 400)

    def test_08_periodo_invalido(self):
        code, body = self._invoke("get_dashboard_kpis", {"periodo": "year"})
        self.assertEqual(code, 400)

    def test_09_sql_extra(self):
        code, body = self._invoke("get_dashboard_kpis", {"sql": "SELECT 1"})
        self.assertEqual(code, 400)

    def test_10_token(self):
        code, body = self._invoke(
            "get_dashboard_kpis",
            {},
            headers={"Authorization": "Bearer wrong", "X-Andes-Env": "local"},
        )
        self.assertEqual(code, 401)

    def test_11_cookie(self):
        code, body = self._invoke("get_dashboard_kpis", {}, headers=self._auth({"Cookie": "session=x"}))
        self.assertEqual(code, 401)

    def test_12_env(self):
        code, body = self._invoke("get_dashboard_kpis", {}, headers=self._auth({"X-Andes-Env": "production"}))
        self.assertEqual(code, 400)

    def test_13_permiso(self):
        _FakeERP.force_permission_denied = True
        code, body = self._invoke("get_dashboard_kpis", {})
        self.assertEqual(code, 403)

    def test_14_sin_pii(self):
        code, body = self._invoke("get_dashboard_kpis", {})
        blob = json.dumps(body)
        for field in ("email", "telefono", "direccion", "rut", "password", "token", "costo", "margen"):
            self.assertNotIn(f'"{field}"', blob)

    def test_15_regresion_9_tools(self):
        checks = [
            ("search_catalog", {"q": "filtro"}),
            ("get_product", {"codigo": "2404"}),
            ("get_inventory", {"codigo": "2404"}),
            ("check_stock", {"items": [{"codigo": "2404", "cantidad": 1}]}),
            ("get_stock_movements", {"codigo": "2404"}),
            ("get_ingresos", {"codigo": "2404"}),
            ("get_purchase_orders", {"numero": "OC-1"}),
            ("get_customer", {"q": "ALBERT"}),
            ("get_supplier", {"q": "ALBERT"}),
        ]
        for tool, args in checks:
            code, body = self._invoke(tool, args)
            self.assertEqual(code, 200, tool)
            self.assertTrue(body.get("ok"), tool)

    def test_schema(self):
        with self.assertRaises(SchemaError):
            validate_tool_arguments("get_dashboard_kpis", {"periodo": "nope"})
        with self.assertRaises(SchemaError):
            validate_tool_arguments(
                "get_dashboard_kpis",
                {"fecha_desde": "2026-01-01", "fecha_hasta": "2026-12-31"},
            )

    def test_todas_tools_read(self):
        self.assertTrue(list_tools())
        self.assertTrue(all(spec.write is False for spec in list_tools()))
        self.assertEqual(len(list_tools()), 10)


if __name__ == "__main__":
    unittest.main()
