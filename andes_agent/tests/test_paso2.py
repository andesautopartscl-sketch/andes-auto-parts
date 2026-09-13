from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from andes_agent.config import Settings
from andes_agent.schemas import validate_tool_arguments
from andes_agent.server import create_app
from andes_agent.tools.registry import list_tools


TOKEN = "test-service-token"


class _FakeERP(BaseHTTPRequestHandler):
    payload = {
        "ok": True,
        "tool": "search_catalog",
        "classification": "INTERNAL",
        "data": {
            "items": [{"codigo": "F-001", "descripcion": "Filtro aceite", "marca": "MANN", "modelo": "W712"}],
            "count": 1,
        },
        "meta": {"limit": 10, "truncated": False, "environment": "local"},
    }
    status = 200
    last = {}

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            body = {}
        type(self).last = {
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "cookie": self.headers.get("Cookie"),
            "env": self.headers.get("X-Andes-Env"),
            "actor": self.headers.get("X-Andes-Actor"),
            "body": body,
        }
        data = json.dumps(type(self).payload).encode("utf-8")
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

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


class SearchCatalogPaso2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.audit_path = Path(self.tmp.name) / "audit.jsonl"
        self.erp, self.erp_url = _start_erp()
        _FakeERP.status = 200
        _FakeERP.payload = {
            "ok": True,
            "tool": "search_catalog",
            "classification": "INTERNAL",
            "data": {
                "items": [
                    {
                        "codigo": "F-001",
                        "descripcion": "Filtro aceite",
                        "marca": "MANN",
                        "modelo": "W712",
                        "precio": 99999,
                    }
                ],
                "count": 1,
            },
            "meta": {"limit": 10, "truncated": False, "environment": "local"},
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

    def _invoke(self, arguments, headers=None, tool="search_catalog", actor="albert"):
        return _request(
            self.app,
            "POST",
            "/v1/invoke",
            headers=headers or self._auth(),
            payload={
                "agent_id": "andes-assistant",
                "conversation_id": "conv-1",
                "actor_user": actor,
                "tool": tool,
                "arguments": arguments,
            },
        )

    def test_busqueda_valida_y_exitosa(self):
        code, body = self._invoke({"q": "filtro aceite", "limit": 10})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["tool"], "search_catalog")
        self.assertFalse(body["write"])
        self.assertEqual(body["data"]["count"], 1)
        self.assertEqual(body["data"]["items"][0]["codigo"], "F-001")
        self.assertNotIn("precio", body["data"]["items"][0])
        self.assertIsNone(_FakeERP.last.get("cookie"))
        self.assertEqual(_FakeERP.last.get("actor"), "albert")
        self.assertEqual(_FakeERP.last.get("path"), "/internal/agent/v1/catalog/search")

    def test_resultado_vacio(self):
        _FakeERP.payload = {
            "ok": True,
            "data": {"items": [], "count": 0},
            "meta": {"limit": 10, "truncated": False, "environment": "local"},
        }
        code, body = self._invoke({"q": "zzzz-no-existe"})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["data"]["items"], [])
        self.assertEqual(body["data"]["count"], 0)
        self.assertFalse(body["meta"]["truncated"])

    def test_truncated_inconsistente_se_corrige(self):
        _FakeERP.payload = {
            "ok": True,
            "data": {"items": [{"codigo": "A", "descripcion": "x", "marca": "", "modelo": ""}], "count": 1},
            "meta": {"limit": 10, "truncated": True, "environment": "local"},
        }
        code, body = self._invoke({"q": "filtro aceite", "limit": 10})
        self.assertEqual(code, 200)
        self.assertEqual(body["data"]["count"], 1)
        self.assertFalse(body["meta"]["truncated"])

    def test_truncated_true_cuando_hay_mas_que_limit(self):
        items = [{"codigo": f"C{i}", "descripcion": "x", "marca": "", "modelo": ""} for i in range(12)]
        _FakeERP.payload = {
            "ok": True,
            "data": {"items": items, "count": 12},
            "meta": {"limit": 10, "truncated": True, "environment": "local"},
        }
        code, body = self._invoke({"q": "filtro aceite", "limit": 10})
        self.assertEqual(code, 200)
        self.assertEqual(body["data"]["count"], 10)
        self.assertTrue(body["meta"]["truncated"])
        self.assertEqual(len(body["data"]["items"]), body["data"]["count"])

    def test_q_demasiado_corto(self):
        code, body = self._invoke({"q": "f"})
        self.assertEqual(code, 400)
        self.assertEqual(body["error_code"], "invalid_args")

    def test_q_demasiado_largo(self):
        code, body = self._invoke({"q": "x" * 81})
        self.assertEqual(code, 400)
        self.assertEqual(body["error_code"], "invalid_args")

    def test_limit_mayor_a_25_se_clampa(self):
        code, body = self._invoke({"q": "filtro aceite", "limit": 100})
        self.assertEqual(code, 200)
        self.assertEqual(_FakeERP.last["body"]["limit"], 25)
        self.assertEqual(body["meta"]["limit"], 25)

    def test_campo_no_permitido(self):
        code, body = self._invoke({"q": "filtro", "order_by": "precio"})
        self.assertEqual(code, 400)
        self.assertEqual(body["error_code"], "invalid_args")

    def test_usuario_sin_permiso(self):
        _FakeERP.status = 403
        _FakeERP.payload = {"ok": False, "error_code": "permission_denied", "message": "Permission mod_productos is required"}
        code, body = self._invoke({"q": "filtro aceite"})
        self.assertEqual(code, 403)
        self.assertEqual(body["error_code"], "permission_denied")

    def test_token_incorrecto(self):
        code, body = self._invoke({"q": "filtro aceite"}, headers={"Authorization": "Bearer wrong", "X-Andes-Env": "local"})
        self.assertEqual(code, 401)
        self.assertEqual(body["error_code"], "unauthorized")

    def test_environment_incorrecto(self):
        code, body = self._invoke({"q": "filtro aceite"}, headers=self._auth({"X-Andes-Env": "staging"}))
        self.assertEqual(code, 400)
        self.assertEqual(body["error_code"], "invalid_environment")
        code, body = self._invoke({"q": "filtro aceite"}, headers=self._auth({"X-Andes-Env": "production"}))
        self.assertEqual(code, 400)
        self.assertEqual(body["error_code"], "invalid_environment")

    def test_erp_apagado(self):
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
                "tool": "search_catalog",
                "arguments": {"q": "filtro aceite"},
            },
        )
        self.assertEqual(code, 503)
        self.assertEqual(body["error_code"], "erp_unavailable")

    def test_erp_encendido(self):
        code, body = self._invoke({"q": "filtro aceite"})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(_FakeERP.last["authorization"], f"Bearer {TOKEN}")

    def test_audit_sin_secretos(self):
        self._invoke({"q": "filtro aceite"})
        raw = self.audit_path.read_text(encoding="utf-8")
        self.assertIn("search_catalog", raw)
        self.assertIn("albert", raw)
        self.assertNotIn(TOKEN, raw)
        self.assertNotIn("Authorization", raw)
        self.assertNotIn("Bearer ", raw)
        self.assertNotIn("password", raw.lower())
        record = json.loads(raw.strip().splitlines()[-1])
        self.assertTrue(record["success"])
        self.assertEqual(record["tool_name"], "search_catalog")
        self.assertIsNone(record["error_code"])

    def test_otras_tools_siguen_sin_write_ni_implementar(self):
        for spec in list_tools():
            self.assertFalse(spec.write)
            if spec.name in {"search_catalog", "get_product", "get_inventory", "check_stock", "get_stock_movements", "get_ingresos", "get_purchase_orders", "get_customer", "get_supplier", "get_dashboard_kpis"}:
                continue
            result = spec.handler({})
            self.assertEqual(result["error_code"], "not_implemented")

    def test_schema_limit_clamp_directo(self):
        out = validate_tool_arguments("search_catalog", {"q": "filtro aceite", "limit": 80})
        self.assertEqual(out["limit"], 25)


if __name__ == "__main__":
    unittest.main()
