from __future__ import annotations

import json
import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from andes_agent.adapters.erp_http import AdapterError, ERPAdapter
from andes_agent.audit import AuditLogger, AuditRecord, now_iso
from andes_agent.auth import AuthError, authenticate
from andes_agent.config import ConfigError, Settings, load_settings, parse_environment
from andes_agent.server import create_app
from andes_agent.tools.registry import ALLOWED_TOOLS, list_tools


TOKEN = "test-service-token"


def _settings(audit_path: Path) -> Settings:
    return Settings(
        environment="local",
        service_token=TOKEN,
        host="127.0.0.1",
        port=5055,
        rate_limit_max=1000,
        rate_limit_window_seconds=60,
        audit_path=audit_path,
        erp_base_url="",
    )


def _request(app, method: str, path: str, headers: dict[str, str] | None = None, payload: dict | None = None):
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
    status_headers = {}

    def start_response(status, response_headers):
        status_headers["status"] = status
        status_headers["headers"] = dict(response_headers)

    body = b"".join(app(environ, start_response))
    code = int(status_headers["status"].split()[0])
    return code, json.loads(body.decode("utf-8"))


class GatewayPaso1Tests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.audit_path = Path(self._tmp.name) / "audit.jsonl"
        self.app = create_app(_settings(self.audit_path))

    def tearDown(self):
        self._tmp.cleanup()

    def _auth(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        if extra:
            headers.update(extra)
        return headers

    def _invoke(self, tool: str, arguments: dict, headers: dict[str, str] | None = None):
        return _request(
            self.app,
            "POST",
            "/v1/invoke",
            headers=headers or self._auth(),
            payload={
                "agent_id": "andes-assistant",
                "conversation_id": "conv-1",
                "actor_user": "tester",
                "tool": tool,
                "arguments": arguments,
            },
        )

    def test_01_healthcheck(self):
        code, body = _request(self.app, "GET", "/health")
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["service"], "andes-agent")
        self.assertEqual(body["environment"], "local")
        self.assertIn("version", body)
        self.assertNotIn("token", json.dumps(body).lower())
        self.assertNotIn("ANDES_AGENT_SERVICE_TOKEN", json.dumps(body))

    def test_02_token_ausente(self):
        code, body = _request(
            self.app,
            "POST",
            "/v1/invoke",
            payload={"tool": "search_catalog", "arguments": {"q": "filtro"}},
        )
        self.assertEqual(code, 401)
        self.assertEqual(body["error_code"], "unauthorized")

    def test_03_token_incorrecto(self):
        code, body = self._invoke(
            "search_catalog",
            {"q": "filtro"},
            headers={"Authorization": "Bearer wrong-token"},
        )
        self.assertEqual(code, 401)
        self.assertEqual(body["error_code"], "unauthorized")

    def test_04_cookie_presente(self):
        code, body = self._invoke(
            "search_catalog",
            {"q": "filtro"},
            headers=self._auth({"Cookie": "session=abc123"}),
        )
        self.assertEqual(code, 401)
        self.assertEqual(body["error_code"], "cookies_not_allowed")
        with self.assertRaises(AuthError) as ctx:
            authenticate({"Authorization": f"Bearer {TOKEN}", "Cookie": "session=1"}, TOKEN)
        self.assertEqual(ctx.exception.code, "cookies_not_allowed")

    def test_05_environment_invalido(self):
        with self.assertRaises(ConfigError) as ctx:
            parse_environment("staging")
        self.assertEqual(ctx.exception.code, "invalid_environment")
        code, body = self._invoke(
            "search_catalog",
            {"q": "filtro"},
            headers=self._auth({"X-Andes-Env": "staging"}),
        )
        self.assertEqual(code, 400)
        self.assertEqual(body["error_code"], "invalid_environment")

    def test_06_tool_desconocida(self):
        code, body = self._invoke("drop_database", {"q": "filtro"})
        self.assertEqual(code, 403)
        self.assertEqual(body["error_code"], "tool_not_allowed")

    def test_07_args_invalidos(self):
        code, body = self._invoke("search_catalog", {"q": "x"})
        self.assertEqual(code, 400)
        self.assertEqual(body["error_code"], "invalid_args")
        code, body = self._invoke("get_product", {})
        self.assertEqual(code, 400)
        self.assertEqual(body["error_code"], "invalid_args")

    def test_08_campo_extra(self):
        code, body = self._invoke("search_catalog", {"q": "filtro", "unexpected": 1})
        self.assertEqual(code, 400)
        self.assertEqual(body["error_code"], "invalid_args")

    def test_09_sql_en_argumentos(self):
        code, body = self._invoke("search_catalog", {"q": "SELECT * FROM productos"})
        self.assertEqual(code, 400)
        self.assertEqual(body["error_code"], "invalid_args")
        code, body = self._invoke("search_catalog", {"q": "filtro", "sql": "DROP TABLE"})
        self.assertEqual(code, 400)
        self.assertEqual(body["error_code"], "invalid_args")

    def test_10_audit_sin_secretos(self):
        logger = AuditLogger(self.audit_path)
        logger.write(
            AuditRecord(
                agent_id="andes-assistant",
                conversation_id="conv-secret",
                actor_user="tester",
                tool_name="search_catalog",
                arguments_redacted={
                    "q": "filtro",
                    "password": "super-secret",
                    "token": "must-not-persist",
                    "Authorization": "Bearer leaked",
                    "cookie": "session=abc",
                },
                timestamp=now_iso(),
                duration_ms=1,
                success=False,
                error_code="not_implemented",
                environment="local",
                result_summary="ok",
            )
        )
        raw = self.audit_path.read_text(encoding="utf-8")
        self.assertIn("[redacted]", raw)
        self.assertNotIn("super-secret", raw)
        self.assertNotIn("must-not-persist", raw)
        self.assertNotIn("Bearer leaked", raw)
        self.assertNotIn("session=abc", raw)
        self.assertNotIn(TOKEN, raw)

    def test_11_tool_read_registrada(self):
        expected = {
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
        }
        self.assertEqual(set(ALLOWED_TOOLS), expected)
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
        # All registered tools are implemented as of PASO 11.
        self.assertTrue(all(spec.write is False for spec in list_tools()))

    def test_12_ninguna_tool_puede_escribir(self):
        self.assertTrue(list_tools())
        self.assertTrue(all(spec.write is False for spec in list_tools()))

    def test_adapter_no_llama_erp(self):
        adapter = ERPAdapter(base_url="")
        with self.assertRaises(AdapterError) as ctx:
            adapter.call("catalog/search", {"q": "filtro"}, actor_user="albert")
        self.assertEqual(ctx.exception.code, "erp_unavailable")


class ConfigTests(unittest.TestCase):
    def test_production_requires_token(self):
        old = os.environ.get("ANDES_ENV")
        token = os.environ.get("ANDES_AGENT_SERVICE_TOKEN")
        os.environ["ANDES_ENV"] = "production"
        # Empty string (not pop): prevents repo .env from re-injecting via _load_dotenv.
        os.environ["ANDES_AGENT_SERVICE_TOKEN"] = ""
        try:
            with self.assertRaises(ConfigError) as ctx:
                load_settings()
            self.assertEqual(ctx.exception.code, "missing_service_token")
        finally:
            if old is None:
                os.environ.pop("ANDES_ENV", None)
            else:
                os.environ["ANDES_ENV"] = old
            if token is None:
                os.environ.pop("ANDES_AGENT_SERVICE_TOKEN", None)
            else:
                os.environ["ANDES_AGENT_SERVICE_TOKEN"] = token


class IsolationTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1] / "andes_agent"

    def test_no_sqlite_llm_or_erp_imports(self):
        forbidden = (
            "sqlite3",
            "andes.db",
            "openai",
            "anthropic",
            "langchain",
            "app.models",
            "app.ventas",
            "app.bodega",
            "app.compras",
            "app.clientes",
        )
        for path in self.ROOT.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for needle in forbidden:
                self.assertNotIn(needle, text, f"{path.name} must not mention {needle}")


if __name__ == "__main__":
    unittest.main()
