"""FASE 2 etapa 2 — hardened orchestrator edge cases and FakePlanner scenarios."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.assistant.orchestrator.audit import OrchestratorAudit, redact
from app.assistant.orchestrator.bindings import BindingError, resolve_bindings
from app.assistant.orchestrator.composer import (
    assert_reply_grounded,
    compose_answer,
    reply_contains_only_evidence_values,
)
from app.assistant.orchestrator.normalizer import normalize_tool_result
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


class Etapa2PlanValidatorHardening(unittest.TestCase):
    def test_tool_inexistente(self):
        with self.assertRaises(PlanValidationError) as ctx:
            validate_plan({"steps": [{"step": 1, "tool": "hack_remote_shell", "arguments": {}}]})
        self.assertEqual(ctx.exception.code, "tool_not_allowed")

    def test_args_invalidos(self):
        with self.assertRaises(PlanValidationError) as ctx:
            validate_plan({"steps": [{"step": 1, "tool": "search_catalog", "arguments": {"q": "x"}}]})
        self.assertEqual(ctx.exception.code, "invalid_args")

    def test_sql_prohibido(self):
        with self.assertRaises(PlanValidationError):
            validate_plan(
                {"steps": [{"step": 1, "tool": "search_catalog", "arguments": {"q": "SELECT 1", "limit": 5}}]}
            )

    def test_key_prohibida(self):
        with self.assertRaises(PlanValidationError):
            validate_plan(
                {
                    "steps": [
                        {
                            "step": 1,
                            "tool": "search_catalog",
                            "arguments": {"q": "filtro", "limit": 5, "password": "x"},
                        }
                    ]
                }
            )

    def test_binding_path_no_allowlisted(self):
        with self.assertRaises(PlanValidationError) as ctx:
            validate_plan(
                {
                    "steps": [
                        {"step": 1, "tool": "search_catalog", "arguments": {"q": "ab", "limit": 5}},
                        {
                            "step": 2,
                            "tool": "get_inventory",
                            "arguments": {"codigo": "$steps.1.data.secret_field"},
                            "depends_on": [1],
                        },
                    ]
                }
            )
        self.assertIn("allowlisted", ctx.exception.message.lower())

    def test_binding_inexistente_en_runtime(self):
        plan = validate_plan(
            {
                "steps": [
                    {"step": 1, "tool": "search_catalog", "arguments": {"q": "ab", "limit": 5}},
                    {
                        "step": 2,
                        "tool": "get_inventory",
                        "arguments": {"codigo": "$steps.1.data.items.0.codigo"},
                        "depends_on": [1],
                    },
                ]
            }
        )

        def invoke(payload):
            if payload["tool"] == "search_catalog":
                return 200, {
                    "ok": True,
                    "tool": "search_catalog",
                    "write": False,
                    "data": {"items": [], "count": 0},
                    "meta": {},
                }
            self.fail("should not invoke second tool")

        evidence, _ = run_plan_steps(plan, actor_user="u", conversation_id="c", invoke_fn=invoke)
        self.assertTrue(any(e.get("error_code") == "dependency_empty" or e.get("empty") for e in evidence))

    def test_max_3_steps_ok(self):
        plan = validate_plan(scenario_fixtures()[SCENARIO_THREE_TOOLS])
        self.assertEqual(len(plan["steps"]), 3)

    def test_max_3_steps_reject(self):
        steps = [
            {"step": i, "tool": "search_catalog", "arguments": {"q": "ab", "limit": 5}} for i in range(1, 5)
        ]
        with self.assertRaises(PlanValidationError):
            validate_plan({"steps": steps})

    def test_ciclo(self):
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

    def test_write(self):
        with self.assertRaises(PlanValidationError) as ctx:
            validate_plan({"steps": [{"step": 1, "tool": "create_order", "arguments": {}}]})
        self.assertEqual(ctx.exception.code, "write_not_allowed")


class Etapa2Bindings(unittest.TestCase):
    def test_binding_inexistente(self):
        with self.assertRaises(BindingError):
            resolve_bindings(
                {"codigo": "$steps.1.data.items.0.codigo"},
                {1: {"data": {"items": []}}},
            )

    def test_binding_campo_no_allowlisted(self):
        with self.assertRaises(BindingError):
            resolve_bindings({"x": "$steps.1.data.email"}, {1: {"data": {"email": "a@b.c"}}})


class Etapa2Normalizer(unittest.TestCase):
    def test_permission_denied(self):
        n = normalize_tool_result(403, {"ok": False, "error_code": "permission_denied", "message": "no"})
        self.assertFalse(n["ok"])
        self.assertEqual(n["error_code"], "permission_denied")

    def test_agent_unavailable(self):
        n = normalize_tool_result(503, {"ok": False, "message": "down"})
        self.assertEqual(n["error_code"], "agent_unavailable")

    def test_erp_5xx(self):
        n = normalize_tool_result(502, {"ok": False, "error_code": "erp_error", "message": "boom"})
        self.assertEqual(n["error_code"], "erp_error")

    def test_malformed_payload(self):
        n = normalize_tool_result(200, ["not", "a", "dict"])
        self.assertEqual(n["error_code"], "malformed_payload")
        self.assertFalse(n["ok"])

    def test_write_true_rejected(self):
        n = normalize_tool_result(200, {"ok": True, "write": True, "tool": "x", "data": {}})
        self.assertEqual(n["error_code"], "write_not_allowed")

    def test_empty_results(self):
        n = normalize_tool_result(
            200,
            {"ok": True, "write": False, "tool": "search_catalog", "data": {"items": [], "count": 0}, "meta": {}},
        )
        self.assertTrue(n["empty"])

    def test_finance_null_not_zero(self):
        n = normalize_tool_result(
            200,
            {
                "ok": True,
                "write": False,
                "tool": "get_dashboard_kpis",
                "data": {
                    "ventas_hoy": None,
                    "ventas_mes": None,
                    "ventas_periodo": None,
                    "docs_periodo": 3,
                    "stock_critico": [{"codigo": "A", "marca": "", "bodega": "B", "stock": 1}],
                },
                "meta": {"finanzas": False, "stock_incluido": True},
            },
        )
        self.assertTrue(n["finance_redacted"])
        self.assertIsNone(n["data"]["ventas_periodo"])
        self.assertNotEqual(n["data"]["ventas_periodo"], 0)

    def test_stock_critico_ausente(self):
        n = normalize_tool_result(
            200,
            {
                "ok": True,
                "write": False,
                "tool": "get_dashboard_kpis",
                "data": {"ventas_periodo": 10.0, "docs_periodo": 1, "stock_critico": None},
                "meta": {"finanzas": True, "stock_incluido": False},
            },
        )
        self.assertTrue(n["stock_omitted"])
        self.assertIsNone(n["data"]["stock_critico"])

    def test_strips_pii(self):
        n = normalize_tool_result(
            200,
            {
                "ok": True,
                "write": False,
                "tool": "get_customer",
                "data": {"items": [{"nombre": "A", "email": "a@b.c", "telefono": "123"}], "count": 1},
                "meta": {},
            },
        )
        self.assertNotIn("email", json.dumps(n["data"]))
        self.assertNotIn("telefono", json.dumps(n["data"]))


class Etapa2ToolRunnerErrors(unittest.TestCase):
    def _one_tool_plan(self):
        return validate_plan(scenario_fixtures()[SCENARIO_ONE_TOOL])

    def test_permission_denied(self):
        evidence, _ = run_plan_steps(
            self._one_tool_plan(),
            actor_user="u",
            conversation_id="c",
            invoke_fn=lambda p: (403, {"ok": False, "error_code": "permission_denied", "message": "no"}),
        )
        self.assertEqual(evidence[0]["error_code"], "permission_denied")

    def test_agent_unavailable(self):
        evidence, _ = run_plan_steps(
            self._one_tool_plan(),
            actor_user="u",
            conversation_id="c",
            invoke_fn=lambda p: (503, {"ok": False, "message": "down"}),
        )
        self.assertEqual(evidence[0]["error_code"], "agent_unavailable")

    def test_malformed(self):
        evidence, _ = run_plan_steps(
            self._one_tool_plan(),
            actor_user="u",
            conversation_id="c",
            invoke_fn=lambda p: (200, "nope"),
        )
        self.assertEqual(evidence[0]["error_code"], "malformed_payload")

    def test_three_invokes(self):
        calls = []

        def invoke(payload):
            calls.append(payload["tool"])
            codigo = "2404"
            if payload["tool"] == "search_catalog":
                return 200, {
                    "ok": True,
                    "tool": "search_catalog",
                    "write": False,
                    "data": {"items": [{"codigo": codigo, "descripcion": "F", "marca": "", "modelo": ""}], "count": 1},
                    "meta": {},
                }
            if payload["tool"] == "get_inventory":
                return 200, {
                    "ok": True,
                    "tool": "get_inventory",
                    "write": False,
                    "data": {"codigo": codigo, "descripcion": "F", "items": [{"marca": "M", "bodega": "B", "origen_compra": "", "stock": 1}], "total_stock": 1},
                    "meta": {},
                }
            return 200, {
                "ok": True,
                "tool": "get_stock_movements",
                "write": False,
                "data": {"codigo": codigo, "descripcion": "F", "items": [], "count": 0},
                "meta": {},
            }

        plan = validate_plan(scenario_fixtures()[SCENARIO_THREE_TOOLS])
        evidence, _ = run_plan_steps(plan, actor_user="u", conversation_id="c", invoke_fn=invoke)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(evidence), 3)


class Etapa2ComposerGrounding(unittest.TestCase):
    def test_sin_evidencia_no_inventa(self):
        out = compose_answer(plan={"steps": []}, evidence=[])
        self.assertIn("No tengo evidencia", out["reply"])
        self.assertNotRegex(out["reply"], r"\$\d+")

    def test_no_fabrica_codigo(self):
        evidence = [
            {
                "ok": True,
                "tool": "search_catalog",
                "data": {"items": [{"codigo": "2404", "descripcion": "Filtro", "marca": "M", "modelo": ""}], "count": 1},
                "meta": {},
                "empty": False,
                "classification": "INTERNAL",
            }
        ]
        out = compose_answer(plan={"steps": []}, evidence=evidence)
        self.assertIn("2404", out["reply"])
        self.assertTrue(reply_contains_only_evidence_values(out["reply"], evidence))
        with self.assertRaises(AssertionError):
            assert_reply_grounded(out["reply"] + "\n- ZZZ999: inventado", evidence)

    def test_finance_null_message(self):
        evidence = [
            {
                "ok": True,
                "tool": "get_dashboard_kpis",
                "data": {"ventas_periodo": None, "docs_periodo": 2, "stock_critico": None},
                "meta": {"periodo": "7d", "fecha_desde": "a", "fecha_hasta": "b", "finanzas": False},
                "empty": False,
                "finance_redacted": True,
                "stock_omitted": True,
                "classification": "CONFIDENTIAL",
            }
        ]
        out = compose_answer(plan={"answer_style": "financial", "scenario": "kpi_no_finance"}, evidence=evidence)
        lower = out["reply"].lower()
        self.assertTrue("permiso" in lower or "no disponible" in lower or "null" in lower)
        self.assertNotIn("Ventas del período: 0", out["reply"])
        self.assertIn("Stock crítico no incluido", out["reply"])

    def test_no_email_telefono(self):
        evidence = [
            {
                "ok": True,
                "tool": "get_customer",
                "data": {"items": [{"nombre": "Albert", "rut": "1-9"}], "count": 1},
                "meta": {},
                "empty": False,
                "classification": "CONFIDENTIAL",
            }
        ]
        out = compose_answer(plan={"scenario": "pii_unavailable"}, evidence=evidence)
        lower = out["reply"].lower()
        self.assertNotIn("@", out["reply"])
        self.assertIn("no se exponen", lower)


class Etapa2AuditSecrets(unittest.TestCase):
    def test_redact_authorization_cookie_token(self):
        safe = redact(
            {
                "Authorization": "Bearer secret-token",
                "cookie": "session=abc",
                "token": "dev-token-local",
                "ok": True,
            }
        )
        blob = json.dumps(safe)
        self.assertNotIn("secret-token", blob)
        self.assertNotIn("session=abc", blob)
        self.assertNotIn("dev-token-local", blob)
        self.assertIn("[redacted]", blob)

    def test_write_audit_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.jsonl"
            OrchestratorAudit(path).write(
                {
                    "actor_user": "u",
                    "Authorization": "Bearer abc",
                    "token": "dev-token-local",
                    "cookie": "x",
                    "message": "busca filtro",
                }
            )
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("Bearer abc", text)
            self.assertNotIn("dev-token-local", text)
            self.assertNotIn("busca filtro", text)


class Etapa2FakePlannerTenScenarios(unittest.TestCase):
    def test_all_ten(self):
        planner = FakePlanner()
        mapping = [
            ("busca filtro aceite", SCENARIO_ONE_TOOL, False),
            ("busca 2404 y dime el stock", SCENARIO_TWO_TOOLS, False),
            ("busca filtro diesel, stock y movimientos", SCENARIO_THREE_TOOLS, False),
            ("cómo va eso del cliente", SCENARIO_AMBIGUOUS, True),
            ("busca xyzzy-no-existe-999", SCENARIO_EMPTY, False),
            ("proveedor ACME sin permiso", SCENARIO_NO_PERMISSION, False),
            ("cuánto vendimos esta semana sin ver_finanzas", SCENARIO_KPI_NO_FINANCE, False),
            ("crea una OC para el proveedor", SCENARIO_WRITE, True),
            ("cuál es el clima en Santiago", SCENARIO_OUT_OF_DOMAIN, True),
            ("dame el email y teléfono del cliente Albert", SCENARIO_PII, False),
        ]
        for message, scenario, no_invoke in mapping:
            self.assertEqual(detect_scenario(message), scenario, message)
            plan = planner.plan(message)
            self.assertEqual(plan.get("scenario"), scenario, message)
            if plan.get("reject") or plan.get("needs_clarification"):
                cleaned = validate_plan(plan)
                self.assertTrue(cleaned.get("reject") or cleaned.get("needs_clarification"))
            else:
                cleaned = validate_plan(plan)
                self.assertGreaterEqual(len(cleaned["steps"]), 1)

            if no_invoke:
                result = run_orchestrator_chat(
                    message=message,
                    actor_user="albertadmin",
                    invoke_fn=lambda p: (_ for _ in ()).throw(AssertionError("no invoke")),
                )
                self.assertTrue(result["ok"])
                self.assertEqual(result.get("tools_used"), [])


if __name__ == "__main__":
    unittest.main()
