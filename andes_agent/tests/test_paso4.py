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

STOCK_ITEMS = [
    {"marca": "MAXUS", "bodega": "Bodega Central", "origen_compra": "nacional", "stock": 8, "costo": 999},
    {"marca": "BOSCH", "bodega": "Bodega 2", "origen_compra": "importado", "stock": 3, "margen": 0.4},
]


class _FakeERP(BaseHTTPRequestHandler):
    last = {}

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
            if codigo == "NO-EXISTE":
                self._reply(404, {"ok": False, "error_code": "not_found", "message": "Producto no encontrado"})
                return
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
            codigo = str(body.get("codigo") or "").upper()
            if codigo in {"NO-EXISTE", "ZZZNOEXISTE"}:
                self._reply(
                    404,
                    {
                        "ok": False,
                        "error_code": "not_found",
                        "message": "Producto no encontrado",
                        "error": {"code": "not_found", "message": "Producto no encontrado"},
                    },
                )
                return
            items = list(STOCK_ITEMS)
            marca = str(body.get("marca") or "").strip().upper()
            bodega = str(body.get("bodega") or "").strip().upper()
            if codigo == "EMPTY-STOCK":
                items = []
            if marca:
                items = [row for row in items if str(row["marca"]).upper() == marca]
            if bodega:
                items = [row for row in items if str(row["bodega"]).upper() == bodega]
            if getattr(type(self), "force_permission_denied", False):
                self._reply(403, {"ok": False, "error_code": "permission_denied", "message": "Permission ver_stock is required"})
                return
            self._reply(
                200,
                {
                    "ok": True,
                    "tool": "get_inventory",
                    "classification": "INTERNAL",
                    "data": {
                        "codigo": codigo or "2404",
                        "descripcion": "FILTRO DIESEL",
                        "items": items,
                        "total_stock": sum(int(row["stock"]) for row in items),
                    },
                    "meta": {"environment": "local"},
                },
            )
            return
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


class GetInventoryPaso4Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.audit_path = Path(self.tmp.name) / "audit.jsonl"
        self.erp, self.erp_url = _start_erp()
        _FakeERP.force_permission_denied = False
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
                "conversation_id": "conv-stock",
                "actor_user": actor,
                "tool": tool,
                "arguments": arguments,
            },
        )

    def test_01_producto_con_stock(self):
        code, body = self._invoke("get_inventory", {"codigo": "2404"})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["tool"], "get_inventory")
        self.assertFalse(body["write"])
        self.assertEqual(body["data"]["codigo"], "2404")
        self.assertEqual(body["data"]["total_stock"], 11)
        self.assertEqual(len(body["data"]["items"]), 2)
        self.assertEqual(_FakeERP.last.get("path"), "/internal/agent/v1/inventory/stock")
        self.assertEqual(_FakeERP.last.get("method"), "POST")
        self.assertIsNone(_FakeERP.last.get("cookie"))

    def test_02_producto_sin_stock(self):
        code, body = self._invoke("get_inventory", {"codigo": "EMPTY-STOCK"})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["data"]["items"], [])
        self.assertEqual(body["data"]["total_stock"], 0)

    def test_03_producto_inexistente(self):
        code, body = self._invoke("get_inventory", {"codigo": "NO-EXISTE"})
        self.assertEqual(code, 404)
        self.assertEqual(body["error_code"], "not_found")
        self.assertEqual(body["error"]["message"], "Producto no encontrado")
        self.assertNotEqual(code, 500)

    def test_04_filtro_por_marca(self):
        code, body = self._invoke("get_inventory", {"codigo": "2404", "marca": "MAXUS"})
        self.assertEqual(code, 200)
        self.assertEqual(len(body["data"]["items"]), 1)
        self.assertEqual(body["data"]["items"][0]["marca"], "MAXUS")
        self.assertEqual(body["data"]["total_stock"], 8)
        self.assertEqual(_FakeERP.last["body"].get("marca"), "MAXUS")

    def test_05_filtro_por_bodega(self):
        code, body = self._invoke("get_inventory", {"codigo": "2404", "bodega": "Bodega 2"})
        self.assertEqual(code, 200)
        self.assertEqual(len(body["data"]["items"]), 1)
        self.assertEqual(body["data"]["items"][0]["bodega"], "Bodega 2")
        self.assertEqual(body["data"]["total_stock"], 3)

    def test_06_codigo_vacio(self):
        code, body = self._invoke("get_inventory", {"codigo": ""})
        self.assertEqual(code, 400)
        self.assertEqual(body["error_code"], "invalid_args")

    def test_07_argumentos_adicionales(self):
        code, body = self._invoke("get_inventory", {"codigo": "2404", "limit": 10})
        self.assertEqual(code, 400)
        self.assertEqual(body["error_code"], "invalid_args")

    def test_08_sql_en_argumentos(self):
        code, body = self._invoke("get_inventory", {"codigo": "SELECT * FROM productos"})
        self.assertEqual(code, 400)
        self.assertEqual(body["error_code"], "invalid_args")
        code, body = self._invoke("get_inventory", {"codigo": "2404", "sql": "DROP TABLE"})
        self.assertEqual(code, 400)
        self.assertEqual(body["error_code"], "invalid_args")

    def test_09_token_incorrecto(self):
        code, body = self._invoke(
            "get_inventory",
            {"codigo": "2404"},
            headers={"Authorization": "Bearer wrong", "X-Andes-Env": "local"},
        )
        self.assertEqual(code, 401)
        self.assertEqual(body["error_code"], "unauthorized")

    def test_10_cookie(self):
        code, body = self._invoke("get_inventory", {"codigo": "2404"}, headers=self._auth({"Cookie": "session=abc"}))
        self.assertEqual(code, 401)
        self.assertEqual(body["error_code"], "cookies_not_allowed")

    def test_11_environment_incorrecto(self):
        code, body = self._invoke("get_inventory", {"codigo": "2404"}, headers=self._auth({"X-Andes-Env": "production"}))
        self.assertEqual(code, 400)
        self.assertEqual(body["error_code"], "invalid_environment")

    def test_12_usuario_sin_ver_stock(self):
        _FakeERP.force_permission_denied = True
        code, body = self._invoke("get_inventory", {"codigo": "2404"})
        self.assertEqual(code, 403)
        self.assertEqual(body["error_code"], "permission_denied")

    def test_13_respuesta_sin_informacion_sensible(self):
        code, body = self._invoke("get_inventory", {"codigo": "2404"})
        self.assertEqual(code, 200)
        blob = json.dumps(body)
        for field in ("costo", "margen", "precio", "proveedor", "rut", "token"):
            self.assertNotIn(f'"{field}"', blob)
        self.assertEqual(set(body["data"]["items"][0].keys()), {"marca", "bodega", "origen_compra", "stock"})

    def test_14_audit_sin_secretos(self):
        self._invoke("get_inventory", {"codigo": "2404"})
        raw = self.audit_path.read_text(encoding="utf-8")
        self.assertIn("get_inventory", raw)
        self.assertIn("albert", raw)
        self.assertNotIn(TOKEN, raw)
        self.assertNotIn("Authorization", raw)
        self.assertNotIn("Bearer ", raw)
        record = json.loads(raw.strip().splitlines()[-1])
        self.assertTrue(record["success"])
        self.assertEqual(record["tool_name"], "get_inventory")
        self.assertIsNone(record["error_code"])
        self.assertEqual(record["arguments_redacted"]["codigo"], "2404")

    def test_15_erp_apagado(self):
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
            payload={"actor_user": "albert", "tool": "get_inventory", "arguments": {"codigo": "2404"}},
        )
        self.assertEqual(code, 503)
        self.assertEqual(body["error_code"], "erp_unavailable")

    def test_17_search_catalog_sigue_funcionando(self):
        code, body = self._invoke("search_catalog", {"q": "filtro aceite"})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["tool"], "search_catalog")
        self.assertEqual(body["data"]["items"][0]["codigo"], "2404")

    def test_18_get_product_sigue_funcionando(self):
        code, body = self._invoke("get_product", {"codigo": "2404"})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["tool"], "get_product")
        self.assertEqual(body["data"]["codigo"], "2404")

    def test_schema_rechaza_limit(self):
        with self.assertRaises(SchemaError):
            validate_tool_arguments("get_inventory", {"codigo": "2404", "limit": 10})

    def test_otras_tools_siguen_stub(self):
        for spec in list_tools():
            self.assertFalse(spec.write)
            if spec.name in {"search_catalog", "get_product", "get_inventory", "check_stock", "get_stock_movements", "get_ingresos", "get_purchase_orders", "get_customer", "get_supplier", "get_dashboard_kpis"}:
                continue
            result = spec.handler({})
            self.assertEqual(result["error_code"], "not_implemented")


if __name__ == "__main__":
    unittest.main()
