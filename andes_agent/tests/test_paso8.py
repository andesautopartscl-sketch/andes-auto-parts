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

CUSTOMERS = [
    {
        "id": 1,
        "nombre": "ALBERT CASTILLO",
        "rut": "78.074.288-7",
        "giro": "REPUESTOS",
        "comuna": "PROVIDENCIA",
        "ciudad": "SANTIAGO",
        "region": "METROPOLITANA",
        "pais": "Chile",
        "activo": True,
        "cliente_mayorista": True,
        "margen_descuento_pct": 5.0,
        "email": "secret@example.com",
        "telefono": "912345678",
        "direccion": "Calle Falsa 123",
    },
    {
        "id": 2,
        "nombre": "CLIENTE DEMO E2E",
        "rut": "76.111.111-1",
        "giro": "COMERCIO",
        "comuna": "MAIPU",
        "ciudad": "SANTIAGO",
        "region": "METROPOLITANA",
        "pais": "Chile",
        "activo": True,
        "cliente_mayorista": False,
        "margen_descuento_pct": 0.0,
    },
]


def _public_customer(row, include_finance=True):
    item = {
        "id": row["id"],
        "nombre": row["nombre"],
        "rut": row["rut"],
        "giro": row.get("giro") or "",
        "comuna": row.get("comuna") or "",
        "ciudad": row.get("ciudad") or "",
        "region": row.get("region") or "",
        "pais": row.get("pais") or "Chile",
        "activo": bool(row.get("activo", True)),
    }
    if include_finance:
        item["cliente_mayorista"] = bool(row.get("cliente_mayorista"))
        item["margen_descuento_pct"] = float(row.get("margen_descuento_pct") or 0)
    return item


class _FakeERP(BaseHTTPRequestHandler):
    last = {}
    force_permission_denied = False
    force_hide_finance = False

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
                    "data": {"codigo": "2404", "descripcion": "FILTRO DIESEL", "items": [], "total_stock": 0},
                    "meta": {"environment": "local"},
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
            if getattr(type(self), "force_permission_denied", False):
                self._reply(
                    403,
                    {"ok": False, "error_code": "permission_denied", "message": "Permission mod_ventas is required"},
                )
                return
            include_finance = not bool(getattr(type(self), "force_hide_finance", False))
            q = str(body.get("q") or "").strip().upper()
            rut = str(body.get("rut") or "").strip().replace(".", "").replace("-", "").upper()
            customer_id = body.get("id")
            try:
                limit = int(body.get("limit") or 20)
            except (TypeError, ValueError):
                limit = 20
            rows = list(CUSTOMERS)
            if customer_id is not None:
                rows = [row for row in rows if row["id"] == int(customer_id)]
                if not rows:
                    self._reply(
                        404,
                        {
                            "ok": False,
                            "error_code": "not_found",
                            "message": "Cliente no encontrado",
                            "error": {"code": "not_found", "message": "Cliente no encontrado"},
                        },
                    )
                    return
            elif rut:
                rows = [
                    row
                    for row in rows
                    if str(row["rut"]).replace(".", "").replace("-", "").upper() == rut
                ]
                if not rows:
                    self._reply(
                        404,
                        {
                            "ok": False,
                            "error_code": "not_found",
                            "message": "Cliente no encontrado",
                            "error": {"code": "not_found", "message": "Cliente no encontrado"},
                        },
                    )
                    return
            elif q == "EMPTY-CLI":
                rows = []
            elif q == "MANY-CLI":
                rows = [
                    {
                        "id": idx + 10,
                        "nombre": f"CLIENTE {idx}",
                        "rut": f"1-{idx}",
                        "giro": "X",
                        "comuna": "A",
                        "ciudad": "B",
                        "region": "C",
                        "pais": "Chile",
                        "activo": True,
                    }
                    for idx in range(40)
                ]
            elif q == "EXACT-20":
                rows = [
                    {
                        "id": idx + 100,
                        "nombre": f"EXACT {idx}",
                        "rut": f"2-{idx}",
                        "giro": "X",
                        "comuna": "A",
                        "ciudad": "B",
                        "region": "C",
                        "pais": "Chile",
                        "activo": True,
                    }
                    for idx in range(20)
                ]
            elif q:
                rows = [row for row in rows if q in str(row["nombre"]).upper() or q in str(row["giro"]).upper()]
            truncated = len(rows) > limit
            items = [_public_customer(row, include_finance=include_finance) for row in rows[:limit]]
            self._reply(
                200,
                {
                    "ok": True,
                    "tool": "get_customer",
                    "classification": "CONFIDENTIAL",
                    "data": {"items": items, "count": len(items)},
                    "meta": {"limit": limit, "truncated": truncated, "environment": "local"},
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


class GetCustomerPaso8Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.audit_path = Path(self.tmp.name) / "audit.jsonl"
        self.erp, self.erp_url = _start_erp()
        _FakeERP.force_permission_denied = False
        _FakeERP.force_hide_finance = False
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
                "conversation_id": "conv-cli",
                "actor_user": actor,
                "tool": tool,
                "arguments": arguments,
            },
        )

    def test_01_por_nombre(self):
        code, body = self._invoke("get_customer", {"q": "ALBERT"})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["tool"], "get_customer")
        self.assertEqual(body["classification"], "CONFIDENTIAL")
        self.assertFalse(body["write"])
        self.assertEqual(body["data"]["items"][0]["nombre"], "ALBERT CASTILLO")
        self.assertEqual(_FakeERP.last.get("path"), "/internal/agent/v1/ventas/customers")
        self.assertIsNone(_FakeERP.last.get("cookie"))

    def test_02_por_rut(self):
        code, body = self._invoke("get_customer", {"rut": "78.074.288-7"})
        self.assertEqual(code, 200)
        self.assertEqual(body["data"]["items"][0]["id"], 1)
        self.assertEqual(_FakeERP.last["body"].get("rut"), "78.074.288-7")

    def test_03_por_id(self):
        code, body = self._invoke("get_customer", {"id": 2})
        self.assertEqual(code, 200)
        self.assertEqual(body["data"]["items"][0]["nombre"], "CLIENTE DEMO E2E")

    def test_04_inexistente_id(self):
        code, body = self._invoke("get_customer", {"id": 99999})
        self.assertEqual(code, 404)
        self.assertEqual(body["error_code"], "not_found")

    def test_05_inexistente_rut(self):
        code, body = self._invoke("get_customer", {"rut": "11.111.111-1"})
        self.assertEqual(code, 404)
        self.assertEqual(body["error_code"], "not_found")

    def test_06_q_vacio_resultados(self):
        code, body = self._invoke("get_customer", {"q": "EMPTY-CLI"})
        self.assertEqual(code, 200)
        self.assertEqual(body["data"]["items"], [])
        self.assertEqual(body["data"]["count"], 0)

    def test_07_sin_filtro(self):
        code, body = self._invoke("get_customer", {})
        self.assertEqual(code, 400)
        self.assertEqual(body["error_code"], "invalid_args")

    def test_08_limit(self):
        code, body = self._invoke("get_customer", {"q": "ALBERT", "limit": 20})
        self.assertEqual(code, 200)
        self.assertEqual(body["meta"]["limit"], 20)
        self.assertFalse(body["meta"]["truncated"])

    def test_09_limit_mayor_20(self):
        code, body = self._invoke("get_customer", {"q": "ALBERT", "limit": 21})
        self.assertEqual(code, 400)
        self.assertEqual(body["error_code"], "invalid_args")

    def test_10_campo_adicional(self):
        code, body = self._invoke("get_customer", {"q": "ALBERT", "order_by": "nombre"})
        self.assertEqual(code, 400)
        self.assertEqual(body["error_code"], "invalid_args")

    def test_11_sql(self):
        code, body = self._invoke("get_customer", {"q": "SELECT * FROM clientes"})
        self.assertEqual(code, 400)
        self.assertEqual(body["error_code"], "invalid_args")
        code, body = self._invoke("get_customer", {"q": "ALBERT", "sql": "DROP TABLE"})
        self.assertEqual(code, 400)

    def test_12_token_incorrecto(self):
        code, body = self._invoke(
            "get_customer",
            {"q": "ALBERT"},
            headers={"Authorization": "Bearer wrong", "X-Andes-Env": "local"},
        )
        self.assertEqual(code, 401)
        self.assertEqual(body["error_code"], "unauthorized")

    def test_13_cookie(self):
        code, body = self._invoke("get_customer", {"q": "ALBERT"}, headers=self._auth({"Cookie": "session=abc"}))
        self.assertEqual(code, 401)
        self.assertEqual(body["error_code"], "cookies_not_allowed")

    def test_14_environment_incorrecto(self):
        code, body = self._invoke(
            "get_customer",
            {"q": "ALBERT"},
            headers=self._auth({"X-Andes-Env": "production"}),
        )
        self.assertEqual(code, 400)
        self.assertEqual(body["error_code"], "invalid_environment")

    def test_15_sin_permiso(self):
        _FakeERP.force_permission_denied = True
        code, body = self._invoke("get_customer", {"q": "ALBERT"})
        self.assertEqual(code, 403)
        self.assertEqual(body["error_code"], "permission_denied")

    def test_16_redaccion_pii_y_finanzas(self):
        _FakeERP.force_hide_finance = True
        code, body = self._invoke("get_customer", {"q": "ALBERT"})
        self.assertEqual(code, 200)
        blob = json.dumps(body)
        for field in ("email", "telefono", "direccion", "cliente_mayorista", "margen_descuento_pct", "token"):
            self.assertNotIn(f'"{field}"', blob)

    def test_17_audit_sin_secretos(self):
        self._invoke("get_customer", {"q": "ALBERT"})
        raw = self.audit_path.read_text(encoding="utf-8")
        self.assertIn("get_customer", raw)
        self.assertNotIn(TOKEN, raw)
        self.assertNotIn("Authorization", raw)
        self.assertNotIn("Bearer ", raw)
        record = json.loads(raw.strip().splitlines()[-1])
        self.assertTrue(record["success"])
        self.assertEqual(record["tool_name"], "get_customer")

    def test_18_erp_apagado(self):
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
            payload={"actor_user": "albert", "tool": "get_customer", "arguments": {"q": "ALBERT"}},
        )
        self.assertEqual(code, 503)
        self.assertEqual(body["error_code"], "erp_unavailable")

    def test_19_truncated(self):
        code, body = self._invoke("get_customer", {"q": "MANY-CLI", "limit": 20})
        self.assertEqual(code, 200)
        self.assertEqual(body["data"]["count"], 20)
        self.assertTrue(body["meta"]["truncated"])
        code, body = self._invoke("get_customer", {"q": "EXACT-20", "limit": 20})
        self.assertEqual(code, 200)
        self.assertEqual(body["data"]["count"], 20)
        self.assertFalse(body["meta"]["truncated"])

    def test_20_search_catalog_sigue(self):
        code, body = self._invoke("search_catalog", {"q": "filtro aceite"})
        self.assertEqual(code, 200)
        self.assertEqual(body["tool"], "search_catalog")

    def test_21_get_product_sigue(self):
        code, body = self._invoke("get_product", {"codigo": "2404"})
        self.assertEqual(code, 200)

    def test_22_get_inventory_sigue(self):
        code, body = self._invoke("get_inventory", {"codigo": "2404"})
        self.assertEqual(code, 200)

    def test_23_get_stock_movements_sigue(self):
        code, body = self._invoke("get_stock_movements", {"codigo": "2404"})
        self.assertEqual(code, 200)

    def test_24_get_ingresos_sigue(self):
        code, body = self._invoke("get_ingresos", {"codigo": "2404"})
        self.assertEqual(code, 200)

    def test_25_get_purchase_orders_sigue(self):
        code, body = self._invoke("get_purchase_orders", {"numero": "OC-1"})
        self.assertEqual(code, 200)

    def test_schema_limit(self):
        with self.assertRaises(SchemaError):
            validate_tool_arguments("get_customer", {"q": "x", "limit": 21})

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
