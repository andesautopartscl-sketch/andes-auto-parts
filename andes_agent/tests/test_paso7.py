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
    {"marca": "MAXUS", "bodega": "Bodega Central", "origen_compra": "nacional", "stock": 8},
    {"marca": "BOSCH", "bodega": "Bodega 2", "origen_compra": "importado", "stock": 3},
]

OC_DOCS = [
    {
        "numero": "OC-100",
        "tipo": "orden_compra",
        "fecha": "2026-07-31",
        "estado": "pendiente",
        "proveedor": "REPUESTOS DEL SUR",
        "lineas": 1,
        "items": [
            {
                "codigo": "2404",
                "descripcion": "FILTRO DIESEL",
                "cantidad": 2,
                "marca": "BOSCH",
                "bodega": "Bodega 1",
                "origen_compra": "nacional",
                "precio": 8900.0,
                "margen_pct": 35.0,
                "subtotal": 17800.0,
            }
        ],
        "total": 17800.0,
        "cliente_rut": "76.111.111-1",
        "email": "secret@example.com",
    },
    {
        "numero": "OC-101",
        "tipo": "orden_compra",
        "fecha": "2026-05-12",
        "estado": "entregada",
        "proveedor": "REPUESTOS DEL SUR",
        "lineas": 1,
        "items": [
            {
                "codigo": "2404",
                "descripcion": "FILTRO DIESEL",
                "cantidad": 1,
                "marca": "BOSCH",
                "bodega": "Bodega 1",
                "origen_compra": "nacional",
                "precio": 8700.0,
                "margen_pct": 32.0,
                "subtotal": 8700.0,
            }
        ],
        "total": 8700.0,
    },
]


def _public_line(row, include_finance=True):
    item = {
        "codigo": row["codigo"],
        "descripcion": row.get("descripcion") or "",
        "cantidad": row["cantidad"],
        "marca": row.get("marca") or "",
        "bodega": row.get("bodega") or "Bodega 1",
        "origen_compra": row.get("origen_compra") or "nacional",
    }
    if include_finance:
        for key in ("precio", "margen_pct", "subtotal"):
            if key in row:
                item[key] = row[key]
    return item


def _public_doc(row, include_finance=True):
    lines = [_public_line(item, include_finance=include_finance) for item in row.get("items") or []]
    doc = {
        "numero": row["numero"],
        "tipo": row.get("tipo") or "orden_compra",
        "fecha": row["fecha"],
        "estado": row["estado"],
        "proveedor": row["proveedor"],
        "lineas": row.get("lineas", len(lines)),
        "items": lines,
    }
    if include_finance and "total" in row:
        doc["total"] = row["total"]
    return doc


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
            self._reply(
                200,
                {
                    "ok": True,
                    "tool": "get_inventory",
                    "data": {
                        "codigo": codigo or "2404",
                        "descripcion": "FILTRO DIESEL",
                        "items": list(STOCK_ITEMS),
                        "total_stock": 11,
                    },
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
                    "data": {
                        "codigo": str(body.get("codigo") or "2404").upper(),
                        "descripcion": "FILTRO DIESEL",
                        "items": [
                            {
                                "fecha": "2026-07-31",
                                "tipo": "ingreso",
                                "cantidad": 2,
                                "marca": "BOSCH",
                                "bodega": "Bodega 1",
                                "origen_compra": "nacional",
                                "usuario": "albertadmin",
                                "observacion": "Doc 302",
                            }
                        ],
                        "count": 1,
                    },
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
                    "data": {
                        "codigo": str(body.get("codigo") or "2404").upper(),
                        "descripcion": "FILTRO DIESEL",
                        "items": [
                            {
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
                        ],
                        "count": 1,
                    },
                    "meta": {"limit": 20, "truncated": False, "environment": "local"},
                },
            )
            return
        if path == "/internal/agent/v1/ventas/purchase-orders":
            numero = str(body.get("numero") or "").strip().upper()
            codigo = str(body.get("codigo") or "").strip().upper()
            if numero in {"NO-EXISTE", "ZZZNOEXISTE"} or codigo in {"NO-EXISTE", "ZZZNOEXISTE"}:
                self._reply(
                    404,
                    {
                        "ok": False,
                        "error_code": "not_found",
                        "message": "Orden de compra no encontrada",
                        "error": {"code": "not_found", "message": "Orden de compra no encontrada"},
                    },
                )
                return
            if getattr(type(self), "force_permission_denied", False):
                self._reply(
                    403,
                    {"ok": False, "error_code": "permission_denied", "message": "Permission mod_ventas is required"},
                )
                return
            include_finance = not bool(getattr(type(self), "force_hide_finance", False))
            if codigo == "EMPTY-OC":
                rows = []
            elif codigo == "MANY-OC":
                rows = [
                    {
                        "numero": f"OC-{idx}",
                        "tipo": "orden_compra",
                        "fecha": "2026-07-31",
                        "estado": "pendiente",
                        "proveedor": "PROV",
                        "lineas": 1,
                        "items": [
                            {
                                "codigo": "MANY-OC",
                                "descripcion": "X",
                                "cantidad": 1,
                                "marca": "BOSCH",
                                "bodega": "Bodega 1",
                                "origen_compra": "nacional",
                            }
                        ],
                    }
                    for idx in range(40)
                ]
            elif codigo == "EXACT-20":
                rows = [
                    {
                        "numero": f"OC-{idx}",
                        "tipo": "orden_compra",
                        "fecha": "2026-07-31",
                        "estado": "pendiente",
                        "proveedor": "PROV",
                        "lineas": 1,
                        "items": [
                            {
                                "codigo": "EXACT-20",
                                "descripcion": "X",
                                "cantidad": 1,
                                "marca": "BOSCH",
                                "bodega": "Bodega 1",
                                "origen_compra": "nacional",
                            }
                        ],
                    }
                    for idx in range(20)
                ]
            else:
                rows = list(OC_DOCS)
            proveedor = str(body.get("proveedor") or "").strip().upper()
            estado = str(body.get("estado") or "").strip().lower()
            fecha_desde = str(body.get("fecha_desde") or "").strip()
            fecha_hasta = str(body.get("fecha_hasta") or "").strip()
            if numero:
                rows = [row for row in rows if str(row["numero"]).upper() == numero]
            if proveedor:
                rows = [row for row in rows if proveedor in str(row["proveedor"]).upper()]
            if estado:
                rows = [row for row in rows if str(row["estado"]).lower() == estado]
            if codigo and codigo not in {"EMPTY-OC", "MANY-OC", "EXACT-20"}:
                rows = [
                    row
                    for row in rows
                    if any(str(item.get("codigo") or "").upper() == codigo for item in (row.get("items") or []))
                ]
            if fecha_desde:
                rows = [row for row in rows if row["fecha"] >= fecha_desde]
            if fecha_hasta:
                rows = [row for row in rows if row["fecha"] <= fecha_hasta]
            try:
                limit = int(body.get("limit") or 20)
            except (TypeError, ValueError):
                limit = 20
            truncated = len(rows) > limit
            items = [_public_doc(row, include_finance=include_finance) for row in rows[:limit]]
            self._reply(
                200,
                {
                    "ok": True,
                    "tool": "get_purchase_orders",
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


class GetPurchaseOrdersPaso7Tests(unittest.TestCase):
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
                "conversation_id": "conv-oc",
                "actor_user": actor,
                "tool": tool,
                "arguments": arguments,
            },
        )

    def test_01_oc_existente(self):
        code, body = self._invoke("get_purchase_orders", {"numero": "OC-100"})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["tool"], "get_purchase_orders")
        self.assertEqual(body["classification"], "CONFIDENTIAL")
        self.assertFalse(body["write"])
        self.assertEqual(body["data"]["items"][0]["numero"], "OC-100")
        self.assertEqual(body["data"]["items"][0]["tipo"], "orden_compra")
        self.assertEqual(body["data"]["items"][0]["estado"], "pendiente")
        self.assertEqual(_FakeERP.last.get("path"), "/internal/agent/v1/ventas/purchase-orders")
        self.assertIsNone(_FakeERP.last.get("cookie"))

    def test_02_oc_inexistente(self):
        code, body = self._invoke("get_purchase_orders", {"numero": "NO-EXISTE"})
        self.assertEqual(code, 404)
        self.assertEqual(body["error_code"], "not_found")

    def test_03_filtro_estado(self):
        code, body = self._invoke("get_purchase_orders", {"estado": "entregada"})
        self.assertEqual(code, 200)
        self.assertEqual(body["data"]["items"][0]["estado"], "entregada")
        self.assertEqual(_FakeERP.last["body"].get("estado"), "entregada")

    def test_04_filtro_proveedor(self):
        code, body = self._invoke("get_purchase_orders", {"proveedor": "DEL SUR"})
        self.assertEqual(code, 200)
        self.assertTrue(body["data"]["items"])
        self.assertEqual(_FakeERP.last["body"].get("proveedor"), "DEL SUR")

    def test_05_filtro_codigo(self):
        code, body = self._invoke("get_purchase_orders", {"codigo": "2404"})
        self.assertEqual(code, 200)
        self.assertEqual(body["data"]["count"], 2)
        self.assertEqual(body["data"]["items"][0]["items"][0]["codigo"], "2404")

    def test_06_fecha_desde(self):
        code, body = self._invoke("get_purchase_orders", {"codigo": "2404", "fecha_desde": "2026-07-01"})
        self.assertEqual(code, 200)
        self.assertEqual(len(body["data"]["items"]), 1)
        self.assertEqual(body["data"]["items"][0]["fecha"], "2026-07-31")

    def test_07_fecha_hasta(self):
        code, body = self._invoke("get_purchase_orders", {"codigo": "2404", "fecha_hasta": "2026-05-12"})
        self.assertEqual(code, 200)
        self.assertEqual(len(body["data"]["items"]), 1)
        self.assertEqual(body["data"]["items"][0]["fecha"], "2026-05-12")

    def test_08_rango_invalido(self):
        code, body = self._invoke(
            "get_purchase_orders",
            {"codigo": "2404", "fecha_desde": "2026-09-12", "fecha_hasta": "2026-09-01"},
        )
        self.assertEqual(code, 400)
        self.assertEqual(body["error_code"], "invalid_args")

    def test_09_limit(self):
        code, body = self._invoke("get_purchase_orders", {"codigo": "2404", "limit": 20})
        self.assertEqual(code, 200)
        self.assertEqual(body["meta"]["limit"], 20)
        self.assertFalse(body["meta"]["truncated"])

    def test_10_limit_mayor_20(self):
        code, body = self._invoke("get_purchase_orders", {"codigo": "2404", "limit": 21})
        self.assertEqual(code, 400)
        self.assertEqual(body["error_code"], "invalid_args")

    def test_11_campo_adicional(self):
        code, body = self._invoke("get_purchase_orders", {"numero": "OC-100", "order_by": "fecha"})
        self.assertEqual(code, 400)
        self.assertEqual(body["error_code"], "invalid_args")

    def test_12_sql_en_argumentos(self):
        code, body = self._invoke("get_purchase_orders", {"numero": "SELECT * FROM ventas"})
        self.assertEqual(code, 400)
        self.assertEqual(body["error_code"], "invalid_args")
        code, body = self._invoke("get_purchase_orders", {"numero": "OC-100", "sql": "DROP TABLE"})
        self.assertEqual(code, 400)
        self.assertEqual(body["error_code"], "invalid_args")

    def test_13_token_incorrecto(self):
        code, body = self._invoke(
            "get_purchase_orders",
            {"numero": "OC-100"},
            headers={"Authorization": "Bearer wrong", "X-Andes-Env": "local"},
        )
        self.assertEqual(code, 401)
        self.assertEqual(body["error_code"], "unauthorized")

    def test_14_cookie(self):
        code, body = self._invoke(
            "get_purchase_orders",
            {"numero": "OC-100"},
            headers=self._auth({"Cookie": "session=abc"}),
        )
        self.assertEqual(code, 401)
        self.assertEqual(body["error_code"], "cookies_not_allowed")

    def test_15_environment_incorrecto(self):
        code, body = self._invoke(
            "get_purchase_orders",
            {"numero": "OC-100"},
            headers=self._auth({"X-Andes-Env": "production"}),
        )
        self.assertEqual(code, 400)
        self.assertEqual(body["error_code"], "invalid_environment")

    def test_16_usuario_sin_permiso(self):
        _FakeERP.force_permission_denied = True
        code, body = self._invoke("get_purchase_orders", {"numero": "OC-100"})
        self.assertEqual(code, 403)
        self.assertEqual(body["error_code"], "permission_denied")

    def test_17_redaccion_sin_permiso_financiero(self):
        _FakeERP.force_hide_finance = True
        code, body = self._invoke("get_purchase_orders", {"numero": "OC-100"})
        self.assertEqual(code, 200)
        blob = json.dumps(body)
        for field in ("precio", "margen_pct", "subtotal", "total", "cliente_rut", "email", "token"):
            self.assertNotIn(f'"{field}"', blob)

    def test_18_audit_sin_secretos(self):
        self._invoke("get_purchase_orders", {"numero": "OC-100"})
        raw = self.audit_path.read_text(encoding="utf-8")
        self.assertIn("get_purchase_orders", raw)
        self.assertNotIn(TOKEN, raw)
        self.assertNotIn("Authorization", raw)
        self.assertNotIn("Bearer ", raw)
        record = json.loads(raw.strip().splitlines()[-1])
        self.assertTrue(record["success"])
        self.assertEqual(record["tool_name"], "get_purchase_orders")
        self.assertEqual(record["arguments_redacted"]["numero"], "OC-100")

    def test_19_erp_apagado(self):
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
            payload={"actor_user": "albert", "tool": "get_purchase_orders", "arguments": {"numero": "OC-100"}},
        )
        self.assertEqual(code, 503)
        self.assertEqual(body["error_code"], "erp_unavailable")

    def test_20_truncated(self):
        code, body = self._invoke("get_purchase_orders", {"codigo": "MANY-OC", "limit": 20})
        self.assertEqual(code, 200)
        self.assertEqual(body["data"]["count"], 20)
        self.assertTrue(body["meta"]["truncated"])
        code, body = self._invoke("get_purchase_orders", {"codigo": "EXACT-20", "limit": 20})
        self.assertEqual(code, 200)
        self.assertEqual(body["data"]["count"], 20)
        self.assertFalse(body["meta"]["truncated"])

    def test_21_search_catalog_sigue_funcionando(self):
        code, body = self._invoke("search_catalog", {"q": "filtro aceite"})
        self.assertEqual(code, 200)
        self.assertEqual(body["tool"], "search_catalog")

    def test_22_get_product_sigue_funcionando(self):
        code, body = self._invoke("get_product", {"codigo": "2404"})
        self.assertEqual(code, 200)
        self.assertEqual(body["tool"], "get_product")

    def test_23_get_inventory_sigue_funcionando(self):
        code, body = self._invoke("get_inventory", {"codigo": "2404"})
        self.assertEqual(code, 200)
        self.assertEqual(body["tool"], "get_inventory")

    def test_24_get_stock_movements_sigue_funcionando(self):
        code, body = self._invoke("get_stock_movements", {"codigo": "2404"})
        self.assertEqual(code, 200)
        self.assertEqual(body["tool"], "get_stock_movements")

    def test_25_get_ingresos_sigue_funcionando(self):
        code, body = self._invoke("get_ingresos", {"codigo": "2404"})
        self.assertEqual(code, 200)
        self.assertEqual(body["tool"], "get_ingresos")

    def test_schema_rechaza_limit_alto(self):
        with self.assertRaises(SchemaError):
            validate_tool_arguments("get_purchase_orders", {"numero": "OC-100", "limit": 21})

    def test_schema_rechaza_estado_inventado(self):
        with self.assertRaises(SchemaError):
            validate_tool_arguments("get_purchase_orders", {"estado": "borrador"})

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
