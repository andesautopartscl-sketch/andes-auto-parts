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
STOCK = {"2404": 10, "ABC123": 1}


class _FakeERP(BaseHTTPRequestHandler):
    last = {}
    force_permission_denied = False
    writes = 0

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
            "authorization": self.headers.get("Authorization"),
            "cookie": self.headers.get("Cookie"),
            "env": self.headers.get("X-Andes-Env"),
            "actor": self.headers.get("X-Andes-Actor"),
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
            codigo = path.rsplit("/", 1)[-1]
            self._reply(
                200,
                {
                    "ok": True,
                    "tool": "get_product",
                    "data": {
                        "codigo": codigo,
                        "descripcion": "Filtro",
                        "marca": "MAXUS",
                        "modelo": "",
                        "motor": "",
                        "anio": "",
                        "activo": True,
                        "categoria": "Motor",
                        "subcategoria": "Filtros",
                    },
                    "meta": {"environment": "local"},
                },
            )
            return
        self._reply(404, {"ok": False, "error_code": "not_found"})

    def do_POST(self):
        self._capture()
        path = urlparse(self.path).path
        body = type(self).last.get("body") or {}
        if path == "/internal/agent/v1/catalog/search":
            self._reply(
                200,
                {
                    "ok": True,
                    "tool": "search_catalog",
                    "data": {
                        "items": [{"codigo": "2404", "descripcion": "FILTRO DIESEL", "marca": "MAXUS", "modelo": "T60"}],
                        "count": 1,
                    },
                    "meta": {"limit": 10, "truncated": False, "environment": "local"},
                },
            )
            return
        if path == "/internal/agent/v1/inventory/stock":
            self._reply(
                200,
                {
                    "ok": True,
                    "tool": "get_inventory",
                    "data": {"codigo": "2404", "descripcion": "FILTRO DIESEL", "items": [], "total_stock": 10},
                    "meta": {"environment": "local"},
                },
            )
            return
        if path == "/internal/agent/v1/inventory/check-stock":
            if getattr(type(self), "force_permission_denied", False):
                self._reply(
                    403,
                    {"ok": False, "error_code": "permission_denied", "message": "Permission ver_stock is required"},
                )
                return
            # Fake ERP never mutates STOCK
            items_out = []
            available = True
            for row in body.get("items") or []:
                codigo = str(row.get("codigo") or "").upper()
                cantidad = int(row.get("cantidad") or 0)
                disponible = int(STOCK.get(codigo, 0))
                ok = disponible >= cantidad
                if not ok:
                    available = False
                item = {"codigo": codigo, "cantidad": cantidad, "disponible": disponible, "ok": ok}
                if row.get("marca"):
                    item["marca"] = str(row.get("marca"))
                if row.get("bodega"):
                    item["bodega"] = str(row.get("bodega"))
                items_out.append(item)
            self._reply(
                200,
                {
                    "ok": True,
                    "tool": "check_stock",
                    "classification": "INTERNAL",
                    "data": {"available": available, "items": items_out},
                    "meta": {"environment": "local", "count": len(items_out)},
                },
            )
            return
        if path == "/internal/agent/v1/stock/movements":
            self._reply(
                200,
                {
                    "ok": True,
                    "tool": "get_stock_movements",
                    "data": {"codigo": "2404", "descripcion": "FILTRO DIESEL", "items": [], "count": 0},
                    "meta": {"limit": 20, "truncated": False, "environment": "local"},
                },
            )
            return
        if path == "/internal/agent/v1/bodega/ingresos":
            self._reply(
                200,
                {
                    "ok": True,
                    "tool": "get_ingresos",
                    "classification": "CONFIDENTIAL",
                    "data": {"codigo": "2404", "descripcion": "FILTRO DIESEL", "items": [], "count": 0},
                    "meta": {"limit": 20, "truncated": False, "environment": "local"},
                },
            )
            return
        if path == "/internal/agent/v1/ventas/purchase-orders":
            self._reply(
                200,
                {
                    "ok": True,
                    "tool": "get_purchase_orders",
                    "classification": "CONFIDENTIAL",
                    "data": {"items": [], "count": 0},
                    "meta": {"limit": 20, "truncated": False, "environment": "local"},
                },
            )
            return
        if path == "/internal/agent/v1/ventas/customers":
            self._reply(
                200,
                {
                    "ok": True,
                    "tool": "get_customer",
                    "classification": "CONFIDENTIAL",
                    "data": {"items": [], "count": 0},
                    "meta": {"limit": 20, "truncated": False, "environment": "local"},
                },
            )
            return
        if path == "/internal/agent/v1/ventas/suppliers":
            self._reply(
                200,
                {
                    "ok": True,
                    "tool": "get_supplier",
                    "classification": "CONFIDENTIAL",
                    "data": {"items": [], "count": 0},
                    "meta": {"limit": 20, "truncated": False, "environment": "local"},
                },
            )
            return
        # Any unexpected write-like path would be a failure signal
        type(self).writes += 1
        self._reply(404, {"ok": False, "error_code": "not_found"})

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


class CheckStockPaso10Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.audit_path = Path(self.tmp.name) / "audit.jsonl"
        self.erp, self.erp_url = _start_erp()
        _FakeERP.force_permission_denied = False
        _FakeERP.writes = 0
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

    def _invoke(self, tool, arguments, headers=None, actor="albert"):
        return _request(
            self.app,
            "POST",
            "/v1/invoke",
            headers=headers or self._auth(),
            payload={
                "agent_id": "andes-assistant",
                "conversation_id": "conv-disp",
                "actor_user": actor,
                "tool": tool,
                "arguments": arguments,
            },
        )

    def test_01_suficiente(self):
        code, body = self._invoke("check_stock", {"items": [{"codigo": "2404", "cantidad": 2}]})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["tool"], "check_stock")
        self.assertFalse(body["write"])
        self.assertEqual(body["classification"], "INTERNAL")
        self.assertTrue(body["data"]["available"])
        self.assertTrue(body["data"]["items"][0]["ok"])
        self.assertEqual(body["data"]["items"][0]["disponible"], 10)
        self.assertEqual(_FakeERP.last.get("path"), "/internal/agent/v1/inventory/check-stock")
        self.assertIsNone(_FakeERP.last.get("cookie"))
        self.assertEqual(STOCK["2404"], 10)
        self.assertEqual(_FakeERP.writes, 0)

    def test_02_insuficiente(self):
        code, body = self._invoke("check_stock", {"items": [{"codigo": "2404", "cantidad": 999}]})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertFalse(body["write"])
        self.assertFalse(body["data"]["available"])
        self.assertFalse(body["data"]["items"][0]["ok"])
        self.assertEqual(STOCK["2404"], 10)
        self.assertEqual(_FakeERP.writes, 0)

    def test_03_multiples_lineas(self):
        code, body = self._invoke(
            "check_stock",
            {"items": [{"codigo": "2404", "cantidad": 1}, {"codigo": "ABC123", "cantidad": 1}]},
        )
        self.assertEqual(code, 200)
        self.assertTrue(body["data"]["available"])
        self.assertEqual(len(body["data"]["items"]), 2)

    def test_04_multi_insuficiente(self):
        code, body = self._invoke(
            "check_stock",
            {"items": [{"codigo": "2404", "cantidad": 1}, {"codigo": "ABC123", "cantidad": 5}]},
        )
        self.assertEqual(code, 200)
        self.assertFalse(body["data"]["available"])
        self.assertEqual(STOCK["ABC123"], 1)

    def test_05_sin_items(self):
        code, body = self._invoke("check_stock", {"items": []})
        self.assertEqual(code, 400)
        self.assertEqual(body["error_code"], "invalid_args")

    def test_06_sin_filtro(self):
        code, body = self._invoke("check_stock", {})
        self.assertEqual(code, 400)

    def test_07_mas_de_20(self):
        items = [{"codigo": "2404", "cantidad": 1} for _ in range(21)]
        code, body = self._invoke("check_stock", {"items": items})
        self.assertEqual(code, 400)

    def test_08_cantidad_cero(self):
        code, body = self._invoke("check_stock", {"items": [{"codigo": "2404", "cantidad": 0}]})
        self.assertEqual(code, 400)

    def test_09_campo_extra(self):
        code, body = self._invoke(
            "check_stock",
            {"items": [{"codigo": "2404", "cantidad": 1}], "sql": "DROP"},
        )
        self.assertEqual(code, 400)

    def test_10_sql_en_codigo(self):
        code, body = self._invoke("check_stock", {"items": [{"codigo": "SELECT", "cantidad": 1}]})
        self.assertEqual(code, 400)

    def test_11_token_incorrecto(self):
        code, body = self._invoke(
            "check_stock",
            {"items": [{"codigo": "2404", "cantidad": 1}]},
            headers={"Authorization": "Bearer wrong", "X-Andes-Env": "local"},
        )
        self.assertEqual(code, 401)

    def test_12_cookie(self):
        code, body = self._invoke(
            "check_stock",
            {"items": [{"codigo": "2404", "cantidad": 1}]},
            headers=self._auth({"Cookie": "session=abc"}),
        )
        self.assertEqual(code, 401)
        self.assertEqual(body["error_code"], "cookies_not_allowed")

    def test_13_environment(self):
        code, body = self._invoke(
            "check_stock",
            {"items": [{"codigo": "2404", "cantidad": 1}]},
            headers=self._auth({"X-Andes-Env": "production"}),
        )
        self.assertEqual(code, 400)

    def test_14_sin_permiso(self):
        _FakeERP.force_permission_denied = True
        code, body = self._invoke("check_stock", {"items": [{"codigo": "2404", "cantidad": 1}]})
        self.assertEqual(code, 403)

    def test_15_sin_precios_ni_pii(self):
        code, body = self._invoke("check_stock", {"items": [{"codigo": "2404", "cantidad": 1}]})
        blob = json.dumps(body)
        for field in ("precio", "costo", "margen", "email", "telefono", "proveedor", "cliente", "token"):
            self.assertNotIn(f'"{field}"', blob)

    def test_16_audit(self):
        self._invoke("check_stock", {"items": [{"codigo": "2404", "cantidad": 1}]})
        raw = self.audit_path.read_text(encoding="utf-8")
        self.assertIn("check_stock", raw)
        self.assertNotIn(TOKEN, raw)

    def test_17_erp_apagado(self):
        down = create_app(
            Settings(
                environment="local",
                service_token=TOKEN,
                host="127.0.0.1",
                port=5055,
                rate_limit_max=1000,
                rate_limit_window_seconds=60,
                audit_path=self.audit_path,
                erp_base_url="http://127.0.0.1:9",
            )
        )
        code, body = _request(
            down,
            "POST",
            "/v1/invoke",
            headers=self._auth(),
            payload={
                "actor_user": "albert",
                "tool": "check_stock",
                "arguments": {"items": [{"codigo": "2404", "cantidad": 1}]},
            },
        )
        self.assertEqual(code, 503)

    def test_18_search_catalog_sigue(self):
        code, body = self._invoke("search_catalog", {"q": "filtro"})
        self.assertEqual(code, 200)

    def test_19_get_product_sigue(self):
        self.assertEqual(self._invoke("get_product", {"codigo": "2404"})[0], 200)

    def test_20_get_inventory_sigue(self):
        self.assertEqual(self._invoke("get_inventory", {"codigo": "2404"})[0], 200)

    def test_21_get_stock_movements_sigue(self):
        self.assertEqual(self._invoke("get_stock_movements", {"codigo": "2404"})[0], 200)

    def test_22_get_ingresos_sigue(self):
        self.assertEqual(self._invoke("get_ingresos", {"codigo": "2404"})[0], 200)

    def test_23_get_purchase_orders_sigue(self):
        self.assertEqual(self._invoke("get_purchase_orders", {"numero": "OC-1"})[0], 200)

    def test_24_get_customer_sigue(self):
        self.assertEqual(self._invoke("get_customer", {"q": "ALBERT"})[0], 200)

    def test_25_get_supplier_sigue(self):
        self.assertEqual(self._invoke("get_supplier", {"q": "ALBERT"})[0], 200)

    def test_schema(self):
        with self.assertRaises(SchemaError):
            validate_tool_arguments("check_stock", {"items": []})
        with self.assertRaises(SchemaError):
            validate_tool_arguments("check_stock", {"items": [{"codigo": "2404", "cantidad": 0}]})

    def test_otras_tools_siguen_stub(self):
        for spec in list_tools():
            self.assertFalse(spec.write)
            if spec.name in {
                "search_catalog",
                "get_product",
                "get_inventory",
                "check_stock",
                "get_stock_movements",
                "get_ingresos",
                "get_purchase_orders",
                "get_customer",
                "get_supplier",
                "get_dashboard_kpis",
            }:
                continue
            result = spec.handler({})
            self.assertEqual(result["error_code"], "not_implemented")


if __name__ == "__main__":
    unittest.main()
