"""Contract tests: orchestrator ToolRunner ↔ real Gateway 0.11.0 (FakeERP backend)."""
from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "andes_agent"))

from andes_agent.config import Settings  # noqa: E402
from andes_agent.server import create_app  # noqa: E402
from andes_agent.tools.registry import list_tools  # noqa: E402

from app.assistant.orchestrator.catalog import ALLOWED_TOOLS  # noqa: E402
from app.assistant.orchestrator.plan_validator import validate_plan  # noqa: E402
from app.assistant.orchestrator.tool_runner import run_plan_steps  # noqa: E402

TOKEN = "test-service-token"


class _FakeERP(BaseHTTPRequestHandler):
    mode = "ok"  # ok | deny | empty | no_finance | no_stock

    def _capture(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8") or "{}") if raw else {}
        except json.JSONDecodeError:
            body = {}
        type(self).last = {"path": urlparse(self.path).path, "cookie": self.headers.get("Cookie"), "body": body}

    def _reply(self, status, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._capture()
        if type(self).mode == "deny":
            self._reply(403, {"ok": False, "error_code": "permission_denied", "message": "denied"})
            return
        path = urlparse(self.path).path
        if path.startswith("/internal/agent/v1/catalog/product/"):
            codigo = path.rsplit("/", 1)[-1]
            self._reply(
                200,
                {
                    "ok": True,
                    "tool": "get_product",
                    "classification": "INTERNAL",
                    "data": {
                        "codigo": codigo.upper(),
                        "descripcion": "PROD",
                        "marca": "M",
                        "modelo": "",
                        "motor": "",
                        "anio": "",
                        "activo": True,
                        "categoria": "",
                        "subcategoria": "",
                    },
                    "meta": {"environment": "local"},
                },
            )
            return
        self._reply(404, {"ok": False})

    def do_POST(self):
        self._capture()
        if self.headers.get("Cookie"):
            self._reply(401, {"ok": False, "error_code": "cookies_not_allowed", "message": "no cookies"})
            return
        if type(self).mode == "deny":
            self._reply(403, {"ok": False, "error_code": "permission_denied", "message": "denied"})
            return
        path = urlparse(self.path).path
        empty = type(self).mode == "empty"
        finance = type(self).mode != "no_finance"
        stock = type(self).mode != "no_stock"

        catalog_items = [] if empty else [{"codigo": "2404", "descripcion": "FILTRO", "marca": "M", "modelo": "X"}]
        stubs = {
            "/internal/agent/v1/catalog/search": {
                "ok": True,
                "tool": "search_catalog",
                "classification": "INTERNAL",
                "data": {"items": catalog_items, "count": len(catalog_items)},
                "meta": {"limit": 10, "truncated": False, "environment": "local"},
            },
            "/internal/agent/v1/inventory/stock": {
                "ok": True,
                "tool": "get_inventory",
                "classification": "INTERNAL",
                "data": {
                    "codigo": "2404",
                    "descripcion": "FILTRO",
                    "items": [] if empty else [{"marca": "M", "bodega": "B1", "origen_compra": "", "stock": 2}],
                    "total_stock": 0 if empty else 2,
                },
                "meta": {"environment": "local"},
            },
            "/internal/agent/v1/inventory/check-stock": {
                "ok": True,
                "tool": "check_stock",
                "classification": "INTERNAL",
                "data": {
                    "available": not empty,
                    "items": []
                    if empty
                    else [{"codigo": "2404", "cantidad": 1, "disponible": 2, "ok": True}],
                },
                "meta": {"environment": "local", "count": 0 if empty else 1},
            },
            "/internal/agent/v1/stock/movements": {
                "ok": True,
                "tool": "get_stock_movements",
                "classification": "INTERNAL",
                "data": {"codigo": "2404", "descripcion": "FILTRO", "items": [], "count": 0},
                "meta": {"limit": 20, "truncated": False, "environment": "local"},
            },
            "/internal/agent/v1/bodega/ingresos": {
                "ok": True,
                "tool": "get_ingresos",
                "classification": "CONFIDENTIAL",
                "data": {"codigo": "2404", "descripcion": "FILTRO", "items": [], "count": 0},
                "meta": {"limit": 20, "truncated": False, "environment": "local"},
            },
            "/internal/agent/v1/ventas/purchase-orders": {
                "ok": True,
                "tool": "get_purchase_orders",
                "classification": "CONFIDENTIAL",
                "data": {"items": [], "count": 0},
                "meta": {"limit": 20, "truncated": False, "environment": "local"},
            },
            "/internal/agent/v1/catalog/equivalences": {
                "ok": True,
                "tool": "get_equivalences",
                "classification": "INTERNAL",
                "data": {
                    "query": {"oem": "038-1701225"},
                    "matched_on": "oem",
                    "items": []
                    if empty
                    else [{"codigo": "FK1264", "descripcion": "ANILLO",
                           "marca": "FK", "modelo": "X", "motor": "",
                           "oem": ["038-1701225"], "alternativos": [],
                           "aplicaciones": ["RICH 6 2.5"]}],
                    "count": 0 if empty else 1,
                },
                "meta": {"limit": 10, "truncated": False, "environment": "local"},
            },
            "/internal/agent/v1/ventas/sales": {
                "ok": True,
                "tool": "get_sales",
                "classification": "CONFIDENTIAL",
                "data": {
                    # Agregados autoritativos + muestra. El detalle NUNCA es el
                    # total: por eso viajan ambos y 'detalle_parcial' lo declara.
                    "unidades": 0 if empty else 5,
                    "documentos": 0 if empty else 2,
                    "neto_notas_credito": True,
                    "notas_credito": {"documentos": 0, "unidades": 0},
                    "tipos": ["factura", "boleta"],
                    "incluye_cotizaciones": False,
                    "detalle_parcial": False,
                    "items": []
                    if empty
                    else [{"fecha": "2026-04-07", "tipo": "factura", "numero": "FA-0001",
                           "estado": "aprobada", "cliente": "ANDES AUTO PARTS LTDA",
                           "codigo": "2404", "descripcion": "FILTRO", "marca": "BOSCH",
                           "bodega": "Bodega 1", "cantidad": 5,
                           "precio_unitario": 100.0, "subtotal": 500.0}],
                    "count": 0 if empty else 1,
                },
                "meta": {"limit": 10, "truncated": False, "environment": "local"},
            },
            "/internal/agent/v1/ventas/customers": {
                "ok": True,
                "tool": "get_customer",
                "classification": "CONFIDENTIAL",
                "data": {
                    "items": []
                    if empty
                    else [{"id": 1, "nombre": "ALBERT", "rut": "1-9", "giro": "", "comuna": "", "ciudad": "", "region": "", "pais": "", "activo": True}],
                    "count": 0 if empty else 1,
                },
                "meta": {"limit": 20, "truncated": False, "environment": "local"},
            },
            "/internal/agent/v1/ventas/suppliers": {
                "ok": True,
                "tool": "get_supplier",
                "classification": "CONFIDENTIAL",
                "data": {
                    "items": []
                    if empty
                    else [{"id": 1, "nombre": "ACME", "empresa": "ACME", "rut": "2-7", "giro": "", "comuna": "", "ciudad": "", "region": "", "pais": "", "activo": True}],
                    "count": 0 if empty else 1,
                },
                "meta": {"limit": 20, "truncated": False, "environment": "local"},
            },
            "/internal/agent/v1/dashboard/kpis": {
                "ok": True,
                "tool": "get_dashboard_kpis",
                "classification": "CONFIDENTIAL",
                "data": {
                    "ventas_hoy": 10.0 if finance else None,
                    "ventas_mes": 20.0 if finance else None,
                    "ventas_periodo": 15.0 if finance else None,
                    "docs_hoy": 1,
                    "docs_mes": 2,
                    "docs_periodo": 2,
                    "chart_data": [{"dia": "2026-09-13", "total": 15.0 if finance else None}],
                    "top_productos": [],
                    "top_clientes": [],
                    "stock_critico": [{"codigo": "X", "marca": "M", "bodega": "B", "stock": 1}] if stock else None,
                },
                "meta": {
                    "environment": "local",
                    "periodo": "7d",
                    "fecha_desde": "2026-09-07",
                    "fecha_hasta": "2026-09-13",
                    "finanzas": finance,
                    "stock_incluido": stock,
                },
            },
        }
        if path in stubs:
            self._reply(200, stubs[path])
            return
        self._reply(404, {"ok": False, "error_code": "not_found", "message": path})

    def log_message(self, *_args):
        return


def _request(app, method: str, path: str, headers: dict, body: dict | None = None):
    raw = json.dumps(body or {}).encode("utf-8") if body is not None else None
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "SERVER_NAME": "localhost",
        "SERVER_PORT": "80",
        "wsgi.input": BytesIO(raw or b""),
        "CONTENT_LENGTH": str(len(raw or b"")),
        "CONTENT_TYPE": "application/json",
    }
    for key, value in headers.items():
        environ[f"HTTP_{key.upper().replace('-', '_')}"] = value
    status_headers = {}

    def start_response(status, resp_headers):
        status_headers["status"] = int(status.split()[0])
        status_headers["headers"] = dict(resp_headers)

    result = b"".join(app(environ, start_response))
    payload = json.loads(result.decode("utf-8") or "{}") if result else {}
    return status_headers["status"], payload


class GatewayContractOrchestratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.erp = ThreadingHTTPServer(("127.0.0.1", 0), _FakeERP)
        cls.erp_thread = threading.Thread(target=cls.erp.serve_forever, daemon=True)
        cls.erp_thread.start()
        host, port = cls.erp.server_address
        cls.erp_url = f"http://{host}:{port}"

    @classmethod
    def tearDownClass(cls):
        cls.erp.shutdown()

    def setUp(self):
        _FakeERP.mode = "ok"
        self.tmp = TemporaryDirectory()
        self.settings = Settings(
            environment="local",
            service_token=TOKEN,
            host="127.0.0.1",
            port=5055,
            rate_limit_max=1000,
            rate_limit_window_seconds=60,
            audit_path=Path(self.tmp.name) / "audit.jsonl",
            erp_base_url=self.erp_url,
        )
        self.app = create_app(self.settings)

    def tearDown(self):
        self.tmp.cleanup()

    def _invoke_gateway(self, payload: dict):
        return _request(
            self.app,
            "POST",
            "/v1/invoke",
            {
                "Authorization": f"Bearer {TOKEN}",
                "X-Andes-Env": "local",
                "Content-Type": "application/json",
            },
            payload,
        )

    def test_registry_matches_orchestrator_allowlist(self):
        names = {t.name for t in list_tools()}
        self.assertEqual(names, set(ALLOWED_TOOLS))
        for t in list_tools():
            self.assertFalse(t.write)

    def test_every_allowlisted_tool_runs_through_the_runner(self):
        plans = {
            "search_catalog": {"steps": [{"step": 1, "tool": "search_catalog", "arguments": {"q": "filtro", "limit": 5}}]},
            "get_product": {"steps": [{"step": 1, "tool": "get_product", "arguments": {"codigo": "2404"}}]},
            "get_inventory": {"steps": [{"step": 1, "tool": "get_inventory", "arguments": {"codigo": "2404"}}]},
            "check_stock": {
                "steps": [{"step": 1, "tool": "check_stock", "arguments": {"items": [{"codigo": "2404", "cantidad": 1}]}}]
            },
            "get_stock_movements": {
                "steps": [{"step": 1, "tool": "get_stock_movements", "arguments": {"codigo": "2404", "limit": 5}}]
            },
            "get_sales": {
                "steps": [{"step": 1, "tool": "get_sales", "arguments": {"codigo": "2404", "limit": 5}}]
            },
            "get_equivalences": {
                "steps": [{"step": 1, "tool": "get_equivalences",
                           "arguments": {"oem": "038-1701225", "limit": 5}}]
            },
            "get_ingresos": {"steps": [{"step": 1, "tool": "get_ingresos", "arguments": {"codigo": "2404", "limit": 5}}]},
            "get_purchase_orders": {"steps": [{"step": 1, "tool": "get_purchase_orders", "arguments": {"limit": 5}}]},
            "get_customer": {"steps": [{"step": 1, "tool": "get_customer", "arguments": {"q": "albert", "limit": 5}}]},
            "get_supplier": {"steps": [{"step": 1, "tool": "get_supplier", "arguments": {"q": "acme", "limit": 5}}]},
            "get_dashboard_kpis": {"steps": [{"step": 1, "tool": "get_dashboard_kpis", "arguments": {"periodo": "7d"}}]},
        }
        self.assertEqual(set(plans), set(ALLOWED_TOOLS))
        for tool, raw in plans.items():
            plan = validate_plan(raw)
            evidence, _ = run_plan_steps(
                plan,
                actor_user="albertadmin",
                conversation_id="contract",
                invoke_fn=self._invoke_gateway,
            )
            self.assertEqual(len(evidence), 1, tool)
            self.assertTrue(evidence[0]["ok"], f"{tool}: {evidence[0]}")
            self.assertEqual(evidence[0]["tool"], tool)
            self.assertFalse(evidence[0].get("write"))
            self.assertNotIn("email", json.dumps(evidence[0].get("data") or {}))

    def test_permission_denied_contract(self):
        _FakeERP.mode = "deny"
        plan = validate_plan({"steps": [{"step": 1, "tool": "get_supplier", "arguments": {"q": "x", "limit": 5}}]})
        evidence, _ = run_plan_steps(plan, actor_user="noperm", conversation_id="c", invoke_fn=self._invoke_gateway)
        self.assertFalse(evidence[0]["ok"])
        self.assertEqual(evidence[0]["error_code"], "permission_denied")

    def test_kpi_no_finance_nulls(self):
        _FakeERP.mode = "no_finance"
        plan = validate_plan({"steps": [{"step": 1, "tool": "get_dashboard_kpis", "arguments": {"periodo": "7d"}}]})
        evidence, _ = run_plan_steps(plan, actor_user="u", conversation_id="c", invoke_fn=self._invoke_gateway)
        self.assertTrue(evidence[0]["ok"])
        self.assertIsNone(evidence[0]["data"]["ventas_periodo"])
        self.assertTrue(evidence[0]["finance_redacted"])

    def test_kpi_no_stock(self):
        _FakeERP.mode = "no_stock"
        plan = validate_plan({"steps": [{"step": 1, "tool": "get_dashboard_kpis", "arguments": {"periodo": "7d"}}]})
        evidence, _ = run_plan_steps(plan, actor_user="u", conversation_id="c", invoke_fn=self._invoke_gateway)
        self.assertTrue(evidence[0]["ok"])
        self.assertIsNone(evidence[0]["data"]["stock_critico"])
        self.assertTrue(evidence[0]["stock_omitted"])

    def test_gateway_rejects_cookie(self):
        status, body = _request(
            self.app,
            "POST",
            "/v1/invoke",
            {
                "Authorization": f"Bearer {TOKEN}",
                "X-Andes-Env": "local",
                "Content-Type": "application/json",
                "Cookie": "session=abc",
            },
            {
                "agent_id": "andes-assistant",
                "conversation_id": "c",
                "actor_user": "albertadmin",
                "tool": "search_catalog",
                "arguments": {"q": "filtro", "limit": 5},
            },
        )
        self.assertEqual(status, 401)
        self.assertIn(body.get("error_code"), {"cookies_not_allowed", "unauthorized"})


if __name__ == "__main__":
    unittest.main()
