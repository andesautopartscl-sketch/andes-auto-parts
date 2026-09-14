"""FASE 2 etapa 1 — orchestrator unit, contract and chat E2E tests."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from app.assistant.orchestrator.arg_schema import ArgSchemaError, validate_tool_args
from app.assistant.orchestrator.audit import OrchestratorAudit, redact
from app.assistant.orchestrator.bindings import BindingError, resolve_bindings
from app.assistant.orchestrator.catalog import MAX_STEPS
from app.assistant.orchestrator.composer import compose_answer
from app.assistant.orchestrator.input_guard import InputGuardError, detect_write_intent, guard_message
from app.assistant.orchestrator.plan_validator import PlanValidationError, validate_plan
from app.assistant.orchestrator.planner import FakePlanner
from app.assistant.orchestrator.scenarios import (
    SCENARIO_AMBIGUOUS,
    SCENARIO_EMPTY,
    SCENARIO_KPI_NO_FINANCE,
    SCENARIO_NO_PERMISSION,
    SCENARIO_ONE_TOOL,
    SCENARIO_OUT_OF_DOMAIN,
    SCENARIO_PII,
    SCENARIO_THREE_TOOLS,
    SCENARIO_TWO_TOOLS,
    SCENARIO_WRITE,
    detect_scenario,
    scenario_fixtures,
)
from app.assistant.orchestrator.service import run_orchestrator_chat
from app.assistant.orchestrator.tool_runner import run_plan_steps
from app.assistant.routes import assistant_bp
from app.utils.csrf import CSRF_SESSION_KEY


class InputGuardTests(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(guard_message("  busca filtro  "), "busca filtro")

    def test_empty(self):
        with self.assertRaises(InputGuardError) as ctx:
            guard_message("   ")
        self.assertEqual(ctx.exception.code, "invalid_args")

    def test_too_long(self):
        with self.assertRaises(InputGuardError):
            guard_message("x" * 501)

    def test_sql(self):
        with self.assertRaises(InputGuardError):
            guard_message("SELECT * FROM productos")

    def test_endpoint(self):
        with self.assertRaises(InputGuardError):
            guard_message("llama a /internal/agent/v1/catalog/search")

    def test_write_detect(self):
        self.assertTrue(detect_write_intent("crea una OC para el proveedor"))
        self.assertFalse(detect_write_intent("busca filtro aceite"))


class PlanValidatorTests(unittest.TestCase):
    def test_allowlist_reject_unknown_tool(self):
        with self.assertRaises(PlanValidationError) as ctx:
            validate_plan(
                {
                    "steps": [{"step": 1, "tool": "hack_remote_shell", "arguments": {}}],
                }
            )
        self.assertEqual(ctx.exception.code, "tool_not_allowed")

    def test_write_prefix_rejected(self):
        with self.assertRaises(PlanValidationError) as ctx:
            validate_plan({"steps": [{"step": 1, "tool": "create_order", "arguments": {}}]})
        self.assertEqual(ctx.exception.code, "write_not_allowed")

    def test_delete_prefix_rejected(self):
        with self.assertRaises(PlanValidationError) as ctx:
            validate_plan({"steps": [{"step": 1, "tool": "delete_user", "arguments": {}}]})
        self.assertEqual(ctx.exception.code, "write_not_allowed")

    def test_max_steps(self):
        steps = [
            {"step": i, "tool": "search_catalog", "arguments": {"q": "ab", "limit": 5}}
            for i in range(1, MAX_STEPS + 2)
        ]
        with self.assertRaises(PlanValidationError):
            validate_plan({"steps": steps})

    def test_cycle_depends(self):
        with self.assertRaises(PlanValidationError):
            validate_plan(
                {
                    "steps": [
                        {"step": 1, "tool": "search_catalog", "arguments": {"q": "ab", "limit": 5}},
                        {
                            "step": 2,
                            "tool": "get_inventory",
                            "arguments": {"codigo": "2404"},
                            "depends_on": [2],
                        },
                    ]
                }
            )

    def test_binding_requires_depends_on(self):
        with self.assertRaises(PlanValidationError):
            validate_plan(
                {
                    "steps": [
                        {"step": 1, "tool": "search_catalog", "arguments": {"q": "ab", "limit": 5}},
                        {
                            "step": 2,
                            "tool": "get_inventory",
                            "arguments": {"codigo": "$steps.1.data.items.0.codigo"},
                            "depends_on": [],
                        },
                    ]
                }
            )

    def test_sql_in_args(self):
        with self.assertRaises(PlanValidationError):
            validate_plan(
                {
                    "steps": [
                        {
                            "step": 1,
                            "tool": "search_catalog",
                            "arguments": {"q": "SELECT 1", "limit": 5},
                        }
                    ]
                }
            )

    def test_valid_fixture_plans(self):
        for key, plan in scenario_fixtures().items():
            if plan.get("reject") or plan.get("needs_clarification"):
                cleaned = validate_plan(plan)
                self.assertTrue(cleaned.get("reject") or cleaned.get("needs_clarification"), key)
            else:
                cleaned = validate_plan(plan)
                self.assertGreaterEqual(len(cleaned["steps"]), 1, key)


class BindingTests(unittest.TestCase):
    def test_resolve_ok(self):
        payloads = {1: {"data": {"items": [{"codigo": "2404"}]}}}
        out = resolve_bindings({"codigo": "$steps.1.data.items.0.codigo"}, payloads)
        self.assertEqual(out["codigo"], "2404")

    def test_path_not_allowlisted(self):
        payloads = {1: {"data": {"secret": "x"}}}
        with self.assertRaises(BindingError):
            resolve_bindings({"codigo": "$steps.1.data.secret"}, payloads)


class FakePlannerScenarioTests(unittest.TestCase):
    def test_ten_scenarios(self):
        planner = FakePlanner()
        cases = [
            ("busca filtro aceite mann", SCENARIO_ONE_TOOL),
            ("busca 2404 y dime el stock", SCENARIO_TWO_TOOLS),
            ("busca filtro diesel, stock y movimientos", SCENARIO_THREE_TOOLS),
            ("cómo va eso del cliente", SCENARIO_AMBIGUOUS),
            ("busca xyzzy-no-existe-999", SCENARIO_EMPTY),
            ("datos del proveedor ACME sin permiso", SCENARIO_NO_PERMISSION),
            ("cuánto vendimos esta semana sin ver_finanzas", SCENARIO_KPI_NO_FINANCE),
            ("crea una OC para el proveedor", SCENARIO_WRITE),
            ("cuál es el clima en Santiago", SCENARIO_OUT_OF_DOMAIN),
            ("dame el email y teléfono del cliente Albert", SCENARIO_PII),
        ]
        for message, expected in cases:
            self.assertEqual(detect_scenario(message), expected, message)
            plan = planner.plan(message)
            self.assertEqual(plan.get("scenario"), expected, message)


class ComposerTests(unittest.TestCase):
    def test_no_evidence_no_invention(self):
        out = compose_answer(plan={"steps": [{"tool": "search_catalog"}]}, evidence=[])
        self.assertIn("No tengo evidencia", out["reply"])
        self.assertTrue(out["grounded"])

    def test_permission_denied(self):
        out = compose_answer(
            plan={"steps": []},
            evidence=[
                {
                    "tool": "get_supplier",
                    "ok": False,
                    "error_code": "permission_denied",
                    "data": {},
                }
            ],
        )
        self.assertIn("permiso", out["reply"].lower())


class AuditTests(unittest.TestCase):
    def test_redact_secrets(self):
        raw = {"token": "secret-value", "nested": {"password": "x", "ok": True}}
        safe = redact(raw)
        self.assertEqual(safe["token"], "[redacted]")
        self.assertEqual(safe["nested"]["password"], "[redacted]")
        self.assertTrue(safe["nested"]["ok"])

    def test_write_file_no_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            OrchestratorAudit(path).write(
                {"actor_user": "albert", "token": "dev-token-local", "Authorization": "Bearer x"}
            )
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("dev-token-local", text)
            self.assertNotIn("Bearer x", text)
            self.assertIn("[redacted]", text)


class ToolRunnerTests(unittest.TestCase):
    def test_invokes_gateway_payload(self):
        calls = []

        def invoke(payload):
            calls.append(payload)
            return 200, {
                "ok": True,
                "tool": "search_catalog",
                "classification": "INTERNAL",
                "write": False,
                "data": {"items": [{"codigo": "2404", "descripcion": "X", "marca": "", "modelo": ""}], "count": 1},
                "meta": {},
            }

        plan = validate_plan(scenario_fixtures()[SCENARIO_ONE_TOOL])
        evidence, _ = run_plan_steps(plan, actor_user="albertadmin", conversation_id="t", invoke_fn=invoke)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["actor_user"], "albertadmin")
        self.assertEqual(calls[0]["tool"], "search_catalog")
        self.assertNotIn("token", calls[0])
        self.assertTrue(evidence[0]["ok"])

    def test_http_403(self):
        def invoke(_payload):
            return 403, {"ok": False, "error_code": "permission_denied", "message": "denied"}

        plan = validate_plan(scenario_fixtures()[SCENARIO_NO_PERMISSION])
        evidence, _ = run_plan_steps(plan, actor_user="noperm", conversation_id="t", invoke_fn=invoke)
        self.assertFalse(evidence[0]["ok"])
        self.assertEqual(evidence[0]["error_code"], "permission_denied")

    def test_http_503(self):
        def invoke(_payload):
            return 503, {"ok": False, "error_code": "agent_unavailable", "message": "down"}

        plan = validate_plan(scenario_fixtures()[SCENARIO_ONE_TOOL])
        evidence, _ = run_plan_steps(plan, actor_user="albertadmin", conversation_id="t", invoke_fn=invoke)
        self.assertEqual(evidence[0]["error_code"], "agent_unavailable")

    def test_http_400(self):
        def invoke(_payload):
            return 400, {"ok": False, "error_code": "invalid_args", "message": "bad"}

        plan = validate_plan(scenario_fixtures()[SCENARIO_ONE_TOOL])
        evidence, _ = run_plan_steps(plan, actor_user="albertadmin", conversation_id="t", invoke_fn=invoke)
        self.assertEqual(evidence[0]["error_code"], "invalid_args")

    def test_two_step_binding(self):
        def invoke(payload):
            if payload["tool"] == "search_catalog":
                return 200, {
                    "ok": True,
                    "tool": "search_catalog",
                    "write": False,
                    "classification": "INTERNAL",
                    "data": {"items": [{"codigo": "2404", "descripcion": "F", "marca": "M", "modelo": ""}], "count": 1},
                    "meta": {},
                }
            self.assertEqual(payload["arguments"]["codigo"], "2404")
            return 200, {
                "ok": True,
                "tool": "get_inventory",
                "write": False,
                "classification": "INTERNAL",
                "data": {"codigo": "2404", "descripcion": "F", "items": [{"marca": "M", "bodega": "B1", "origen_compra": "", "stock": 2}], "total_stock": 2},
                "meta": {},
            }

        plan = validate_plan(scenario_fixtures()[SCENARIO_TWO_TOOLS])
        evidence, _ = run_plan_steps(plan, actor_user="albertadmin", conversation_id="t", invoke_fn=invoke)
        self.assertEqual(len(evidence), 2)
        self.assertTrue(all(e["ok"] for e in evidence))


class OrchestratorServiceTests(unittest.TestCase):
    def test_write_rejected_before_invoke(self):
        calls = []

        def invoke(payload):
            calls.append(payload)
            return 200, {"ok": True}

        result = run_orchestrator_chat(
            message="crea una OC para el proveedor",
            actor_user="albertadmin",
            invoke_fn=invoke,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(calls, [])
        self.assertIn("consultar", result["reply"].lower())

    def test_out_of_domain(self):
        result = run_orchestrator_chat(
            message="cuál es el clima en Santiago",
            actor_user="albertadmin",
            invoke_fn=lambda p: (500, {}),
        )
        self.assertTrue(result["ok"])
        self.assertIn("fuera", result["reply"].lower())

    def test_ambiguous(self):
        result = run_orchestrator_chat(
            message="cómo va eso del cliente",
            actor_user="albertadmin",
            invoke_fn=lambda p: (500, {}),
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["tools_used"] == [])


class AssistantChatApiTests(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.get("ANDES_ENV")
        self._token = os.environ.get("ANDES_AGENT_SERVICE_TOKEN")
        os.environ["ANDES_ENV"] = "local"
        os.environ["ANDES_AGENT_SERVICE_TOKEN"] = "erp-test-token"
        app = Flask(__name__)
        app.config["TESTING"] = True
        app.config["SECRET_KEY"] = "test"
        app.register_blueprint(assistant_bp)
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

    def _login(self):
        with self.client.session_transaction() as sess:
            sess["user"] = "albertadmin"
            sess[CSRF_SESSION_KEY] = "csrf-test"

    def test_chat_rejects_client_tool(self):
        self._login()
        resp = self.client.post(
            "/assistant/api/chat",
            data=json.dumps({"message": "hola", "tool": "search_catalog"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error_code"], "invalid_args")

    def test_chat_one_tool_e2e_mocked_gateway(self):
        self._login()

        def fake_invoke(payload):
            self.assertEqual(payload["actor_user"], "albertadmin")
            self.assertEqual(payload["tool"], "search_catalog")
            return 200, {
                "ok": True,
                "tool": "search_catalog",
                "write": False,
                "classification": "INTERNAL",
                "data": {
                    "items": [{"codigo": "F-1", "descripcion": "Filtro", "marca": "MANN", "modelo": ""}],
                    "count": 1,
                },
                "meta": {},
            }

        with patch("app.assistant.routes.invoke_gateway", side_effect=fake_invoke):
            resp = self.client.post(
                "/assistant/api/chat",
                data=json.dumps({"message": "busca filtro aceite", "conversation_id": "c1"}),
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertIn("search_catalog", body["tools_used"])
        self.assertNotIn("erp-test-token", json.dumps(body))
        self.assertIn("Filtro", body["reply"])

    def test_chat_requires_login(self):
        # Mini Flask app has no auth.login endpoint; login_required may raise BuildError on redirect.
        try:
            resp = self.client.post(
                "/assistant/api/chat",
                data=json.dumps({"message": "busca filtro"}),
                content_type="application/json",
            )
        except Exception as exc:
            self.assertIn("auth.login", str(exc))
            return
        self.assertIn(resp.status_code, (302, 401, 403))

    def test_slash_invoke_still_works(self):
        self._login()

        def fake_invoke(payload):
            return 200, {
                "ok": True,
                "tool": "search_catalog",
                "write": False,
                "classification": "INTERNAL",
                "data": {"items": [], "count": 0},
                "meta": {},
            }

        with patch("app.assistant.routes.invoke_gateway", side_effect=fake_invoke):
            resp = self.client.post(
                "/assistant/api/invoke",
                data=json.dumps({"tool": "search_catalog", "arguments": {"q": "filtro", "limit": 5}}),
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])


class ArgSchemaContractTests(unittest.TestCase):
    def test_search_ok(self):
        out = validate_tool_args("search_catalog", {"q": "filtro", "limit": 5})
        self.assertEqual(out["q"], "filtro")

    def test_unknown_tool(self):
        with self.assertRaises(ArgSchemaError):
            validate_tool_args("not_a_tool", {})


if __name__ == "__main__":
    unittest.main()
