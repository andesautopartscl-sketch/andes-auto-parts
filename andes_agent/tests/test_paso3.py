from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import URLError

from andes_agent.config import Settings
from andes_agent.schemas import SchemaError, validate_tool_arguments
from andes_agent.server import create_app
from andes_agent.tools.registry import list_tools


TOKEN = "test-service-token"


class _FakeERP(BaseHTTPRequestHandler):
    payload = {}
    status = 200
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
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "cookie": self.headers.get("Cookie"),
            "env": self.headers.get("X-Andes-Env"),
            "actor": self.headers.get("X-Andes-Actor"),
            "body": body,
            "content_type": self.headers.get("Content-Type"),
        }

    def _reply(self):
        data = json.dumps(type(self).payload).encode("utf-8")
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._capture()
        self._reply()

    def do_POST(self):
        self._capture()
        self._reply()

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


PRODUCT = {
    "codigo": "ABC123",
    "descripcion": "Filtro de aceite",
    "marca": "MANN",
    "modelo": "W712",
    "motor": "1.6",
    "anio": "2015",
    "activo": True,
    "categoria": "Filtros",
    "subcategoria": "Aceite",
    "precio": 99999,
    "costo": 5000,
    "margen": 0.5,
    "p_publico": 12000,
}


class GetProductPaso3Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.audit_path = Path(self.tmp.name) / "audit.jsonl"
        self.erp, self.erp_url = _start_erp()
        _FakeERP.status = 200
        _FakeERP.payload = {
            "ok": True,
            "tool": "get_product",
            "classification": "INTERNAL",
            "data": dict(PRODUCT),
            "meta": {"environment": "local"},
        }
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

    def _invoke(self, arguments, headers=None, actor="albert"):
        return _request(
            self.app,
            "POST",
            "/v1/invoke",
            headers=headers or self._auth(),
            payload={
                "agent_id": "andes-assistant",
                "conversation_id": "conv-product",
                "actor_user": actor,
                "tool": "get_product",
                "arguments": arguments,
            },
        )

    def test_01_producto_existente(self):
        code, body = self._invoke({"codigo": "ABC123"})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["tool"], "get_product")
        self.assertFalse(body["write"])
        self.assertEqual(body["classification"], "INTERNAL")
        self.assertEqual(body["data"]["codigo"], "ABC123")
        self.assertEqual(body["data"]["descripcion"], "Filtro de aceite")
        self.assertEqual(body["meta"]["environment"], "local")
        self.assertEqual(_FakeERP.last.get("method"), "GET")
        self.assertEqual(_FakeERP.last.get("path"), "/internal/agent/v1/catalog/product/ABC123")
        self.assertEqual(_FakeERP.last.get("actor"), "albert")
        self.assertIsNone(_FakeERP.last.get("cookie"))
        self.assertFalse(_FakeERP.last.get("content_type"))

    def test_02_producto_inexistente(self):
        _FakeERP.status = 404
        _FakeERP.payload = {
            "ok": False,
            "error_code": "not_found",
            "message": "Producto no encontrado",
            "error": {"code": "not_found", "message": "Producto no encontrado"},
        }
        code, body = self._invoke({"codigo": "NO-EXISTE"})
        self.assertEqual(code, 404)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error_code"], "not_found")
        self.assertEqual(body["error"]["code"], "not_found")
        self.assertEqual(body["error"]["message"], "Producto no encontrado")
        self.assertNotEqual(code, 500)

    def test_03_codigo_vacio(self):
        code, body = self._invoke({"codigo": ""})
        self.assertEqual(code, 400)
        self.assertEqual(body["error_code"], "invalid_args")

    def test_04_codigo_demasiado_largo(self):
        code, body = self._invoke({"codigo": "A" * 65})
        self.assertEqual(code, 400)
        self.assertEqual(body["error_code"], "invalid_args")

    def test_05_campo_adicional(self):
        code, body = self._invoke({"codigo": "ABC123", "limit": 1})
        self.assertEqual(code, 400)
        self.assertEqual(body["error_code"], "invalid_args")

    def test_06_sql_en_argumentos(self):
        code, body = self._invoke({"codigo": "SELECT * FROM productos"})
        self.assertEqual(code, 400)
        self.assertEqual(body["error_code"], "invalid_args")
        code, body = self._invoke({"codigo": "ABC123", "sql": "DROP TABLE"})
        self.assertEqual(code, 400)
        self.assertEqual(body["error_code"], "invalid_args")

    def test_07_token_incorrecto(self):
        code, body = self._invoke({"codigo": "ABC123"}, headers={"Authorization": "Bearer wrong", "X-Andes-Env": "local"})
        self.assertEqual(code, 401)
        self.assertEqual(body["error_code"], "unauthorized")

    def test_08_cookie(self):
        code, body = self._invoke({"codigo": "ABC123"}, headers=self._auth({"Cookie": "session=abc123"}))
        self.assertEqual(code, 401)
        self.assertEqual(body["error_code"], "cookies_not_allowed")

    def test_09_environment_incorrecto(self):
        code, body = self._invoke({"codigo": "ABC123"}, headers=self._auth({"X-Andes-Env": "staging"}))
        self.assertEqual(code, 400)
        self.assertEqual(body["error_code"], "invalid_environment")
        code, body = self._invoke({"codigo": "ABC123"}, headers=self._auth({"X-Andes-Env": "production"}))
        self.assertEqual(code, 400)
        self.assertEqual(body["error_code"], "invalid_environment")

    def test_10_permiso_inexistente(self):
        _FakeERP.status = 403
        _FakeERP.payload = {
            "ok": False,
            "error_code": "permission_denied",
            "message": "Permission mod_productos is required",
        }
        code, body = self._invoke({"codigo": "ABC123"})
        self.assertEqual(code, 403)
        self.assertEqual(body["error_code"], "permission_denied")

    def test_11_respuesta_sin_datos_financieros(self):
        code, body = self._invoke({"codigo": "abc123"})
        self.assertEqual(code, 200)
        blob = json.dumps(body)
        self.assertEqual(
            set(body["data"].keys()),
            {
                "codigo",
                "descripcion",
                "marca",
                "modelo",
                "motor",
                "anio",
                "activo",
                "categoria",
                "subcategoria",
            },
        )
        for field in ("precio", "costo", "margen", "p_publico", "prec_mayor", "stock"):
            self.assertNotIn(f'"{field}"', blob)
        self.assertNotIn("99999", blob)
        self.assertNotIn("5000", blob)

    def test_12_audit_sin_secretos(self):
        self._invoke({"codigo": "ABC123"})
        raw = self.audit_path.read_text(encoding="utf-8")
        self.assertIn("get_product", raw)
        self.assertIn("albert", raw)
        self.assertIn("andes-assistant", raw)
        self.assertIn("conv-product", raw)
        self.assertNotIn(TOKEN, raw)
        self.assertNotIn("Authorization", raw)
        self.assertNotIn("Bearer ", raw)
        self.assertNotIn("password", raw.lower())
        record = json.loads(raw.strip().splitlines()[-1])
        self.assertTrue(record["success"])
        self.assertEqual(record["tool_name"], "get_product")
        self.assertEqual(record["actor_user"], "albert")
        self.assertIsNone(record["error_code"])
        self.assertIn("duration_ms", record)
        self.assertIn("result_summary", record)
        self.assertIn("environment", record)
        self.assertEqual(record["arguments_redacted"]["codigo"], "ABC123")

    def test_13_gateway_schema_sigue_deny_by_default(self):
        with self.assertRaises(SchemaError):
            validate_tool_arguments("get_product", {"codigo": "ABC123", "query_raw": "x"})

    def test_14_erp_apagado(self):
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
                "tool": "get_product",
                "arguments": {"codigo": "ABC123"},
            },
        )
        self.assertEqual(code, 503)
        self.assertEqual(body["error_code"], "erp_unavailable")
        self.assertNotEqual(code, 500)

    def test_15_otras_tools_siguen_stub(self):
        for spec in list_tools():
            self.assertFalse(spec.write)
            if spec.name in {"search_catalog", "get_product", "get_inventory", "check_stock", "get_stock_movements", "get_ingresos", "get_purchase_orders", "get_customer", "get_supplier", "get_dashboard_kpis"}:
                continue
            result = spec.handler({})
            self.assertEqual(result["error_code"], "not_implemented")


class AdapterGetProductTests(unittest.TestCase):
    def test_urlerror_es_erp_unavailable(self):
        from andes_agent.adapters.erp_http import AdapterError, ERPAdapter

        adapter = ERPAdapter(base_url="http://127.0.0.1:9", service_token=TOKEN, environment="local")
        with self.assertRaises(AdapterError) as ctx:
            adapter.call("catalog/product/ABC123", None, actor_user="albert", method="GET")
        self.assertEqual(ctx.exception.code, "erp_unavailable")
        self.assertIsInstance(ctx.exception.__cause__, URLError)


if __name__ == "__main__":
    unittest.main()
