"""FASE 6 — assistant observability metrics (no secrets / no PII)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.assistant.orchestrator.audit import OrchestratorAudit, redact
from app.assistant.orchestrator.llm.client import OpenAICompatibleClient, _extract_usage
from app.assistant.orchestrator.llm.config import LlmSettings
from app.assistant.orchestrator.metrics import (
    MetricsStore,
    build_turn_metric,
    estimate_cost_usd,
    sanitize_metric,
)
from app.assistant.orchestrator.planner import FakePlanner
from app.assistant.orchestrator.service import run_orchestrator_chat
from app.assistant.orchestrator.turn_store import TurnStore


def _ok_tool(tool: str, *, codigo: str = "2404"):
    data = {"codigo": codigo, "total_stock": 2, "items": [{"bodega": "B1", "stock": 2}]}
    if tool == "get_stock_movements":
        data = {
            "codigo": codigo,
            "count": 1,
            "items": [{"fecha": "2026-09-10", "tipo": "INGRESO", "cantidad": 1}],
        }
    if tool == "search_catalog":
        data = {"count": 1, "items": [{"codigo": codigo, "descripcion": "FILTRO"}]}
    if tool == "get_product":
        data = {"codigo": codigo, "descripcion": "FILTRO", "marca": "BOSCH"}
    if tool == "check_stock":
        data = {"codigo": codigo, "available": True, "total_stock": 2}
    if tool == "get_ingresos":
        data = {"count": 0, "items": []}
    return (
        200,
        {
            "ok": True,
            "tool": tool,
            "classification": "INTERNAL",
            "write": False,
            "data": data,
            "meta": {},
        },
    )


class Fase6MetricsUnitTests(unittest.TestCase):
    def test_sanitize_drops_secrets_and_pii(self):
        dirty = {
            "Authorization": "Bearer sk-secret",
            "api_key": "sk-live",
            "cookie": "session=abc",
            "message": "mi RUT 12.345.678-9 email a@b.com telefono +56911111111",
            "reply": "respuesta completa del asistente",
            "prompt": "system prompt completo",
            "rut": "12.345.678-9",
            "email": "a@b.com",
            "telefono": "+56911111111",
            "direccion": "Calle Falsa 123",
            "ventas_periodo": 999999,
            "monto": 5000,
            "ok": True,
            "tools_used": ["get_inventory"],
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "cost_estimated_usd": 0.01,
            "nested": {"password": "x", "tool": "get_inventory"},
        }
        clean = sanitize_metric(dirty)
        blob = json.dumps(clean)
        self.assertNotIn("sk-secret", blob)
        self.assertNotIn("sk-live", blob)
        self.assertNotIn("session=abc", blob)
        self.assertNotIn("12.345.678-9", blob)
        self.assertNotIn("a@b.com", blob)
        self.assertNotIn("+56911111111", blob)
        self.assertNotIn("Calle Falsa", blob)
        self.assertNotIn("respuesta completa", blob)
        self.assertNotIn("system prompt", blob)
        self.assertNotIn("mi RUT", blob)
        self.assertEqual(clean.get("prompt_tokens"), 100)
        self.assertEqual(clean.get("completion_tokens"), 20)
        self.assertEqual(clean.get("total_tokens"), 120)
        self.assertEqual(clean.get("cost_estimated_usd"), 0.01)
        self.assertEqual(clean["nested"]["tool"], "get_inventory")
        self.assertNotIn("password", clean["nested"])

    def test_audit_redact_no_secrets(self):
        raw = redact(
            {
                "authorization": "Bearer secret-token",
                "cookie": "sid=1",
                "api_key": "k",
                "note": "Bearer abc.def.ghi",
            }
        )
        self.assertEqual(raw["authorization"], "[redacted]")
        self.assertEqual(raw["cookie"], "[redacted]")
        self.assertEqual(raw["api_key"], "[redacted]")
        self.assertIn("[redacted]", raw["note"])

    def test_cost_estimate_without_pii(self):
        with patch.dict(
            os.environ,
            {
                "ANDES_LLM_COST_INPUT_PER_1K": "0.001",
                "ANDES_LLM_COST_OUTPUT_PER_1K": "0.002",
            },
            clear=False,
        ):
            cost = estimate_cost_usd(1000, 500)
            self.assertEqual(cost, 0.002)
            metric = build_turn_metric(
                actor_user="u1",
                conversation_id="c1",
                correlation_id="x",
                message_hash_value="abcd",
                ok=True,
                prompt_tokens=1000,
                completion_tokens=500,
                tools_used=["get_inventory"],
            )
            self.assertEqual(metric["cost_estimated_usd"], 0.002)
            self.assertNotIn("message", metric)
            self.assertNotIn("reply", metric)

    def test_extract_usage_from_provider(self):
        usage = _extract_usage(
            SimpleNamespace(usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18))
        )
        self.assertEqual(usage, {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18})

    def test_client_captures_usage_not_content(self):
        settings = LlmSettings(
            nl_enabled=True,
            planner_mode="llm",
            provider="openai_compatible",
            api_key="test-key",
            base_url="https://example.test/v1",
            planner_model="gpt-test",
            timeout_seconds=5,
            max_retries=0,
            max_output_tokens=100,
            temperature=0,
            environment="local",
        )
        http = MagicMock()
        http.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"plan_id":"x","steps":[]}'))],
            usage=SimpleNamespace(prompt_tokens=40, completion_tokens=10, total_tokens=50),
        )
        client = OpenAICompatibleClient(settings, http_client=http)
        # Plan schema validation in client requires a JSON object — use minimal valid-looking parse
        with patch("app.assistant.orchestrator.llm.client.plan_response_format", return_value={"type": "json_object"}):
            raw = client.complete_plan_json(system="sys", user="usr")
        self.assertIn("plan_id", raw)
        self.assertEqual(client.last_usage["prompt_tokens"], 40)
        self.assertEqual(client.last_usage["completion_tokens"], 10)
        capture = json.dumps(client.last_request_capture)
        self.assertNotIn("test-key", capture)
        self.assertIn("[redacted", capture)


class Fase6MetricsIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.metrics_path = Path(self._tmpdir.name) / "metrics.jsonl"
        self.audit = OrchestratorAudit(path=Path(self._tmpdir.name) / "audit.jsonl")
        self.store = TurnStore()
        self.metrics = MetricsStore(path=self.metrics_path)
        self.planner = FakePlanner()
        self.invokes: list[dict] = []

    def tearDown(self):
        self._tmpdir.cleanup()

    def _chat(self, message: str, *, conversation_id: str = "conv-m6", invoke_fn=None, force_scenario=None):
        def default_invoke(payload: dict):
            self.invokes.append(payload)
            tool = str(payload.get("tool") or "")
            return _ok_tool(tool, codigo=str((payload.get("arguments") or {}).get("codigo") or "2404"))

        return run_orchestrator_chat(
            message=message,
            actor_user="albertadmin",
            conversation_id=conversation_id,
            invoke_fn=invoke_fn or default_invoke,
            planner=self.planner,
            audit=self.audit,
            turn_store=self.store,
            metrics_store=self.metrics,
            force_scenario=force_scenario,
        )

    def _last_metric(self) -> dict:
        lines = [ln for ln in self.metrics_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.assertTrue(lines, "expected at least one metrics line")
        return json.loads(lines[-1])

    def test_one_tool_metric(self):
        r = self._chat("¿Cuánto stock del 2404?", force_scenario="inventory_only")
        self.assertTrue(r["ok"])
        self.assertEqual(r["tools_used"], ["get_inventory"])
        m = self._last_metric()
        self.assertEqual(m["tools_used"], ["get_inventory"])
        self.assertEqual(m["tool_selected"], "get_inventory")
        self.assertEqual(m["invoke_count"], 1)
        self.assertTrue(m["success"])
        self.assertFalse(m["needs_clarification"])
        self.assertIn("message_hash", m)
        self.assertNotIn("message", m)
        self.assertNotIn("reply", m)

    def test_multiple_tools_metric(self):
        r = self._chat("stock y movimientos del 2404", force_scenario="inventory_and_movements")
        self.assertTrue(r["ok"])
        self.assertEqual(set(r["tools_used"]), {"get_inventory", "get_stock_movements"})
        m = self._last_metric()
        self.assertEqual(set(m["tools_used"]), {"get_inventory", "get_stock_movements"})
        self.assertEqual(m["tools_count"], 2)
        self.assertEqual(m["invoke_count"], 2)
        self.assertIsNone(m.get("tool_selected"))

    def test_clarification_metric(self):
        r = self._chat("hola", force_scenario="ambiguous")
        self.assertTrue(r["ok"])
        self.assertTrue(r.get("needs_clarification"))
        m = self._last_metric()
        self.assertTrue(m["needs_clarification"])
        self.assertIn("clarification", m["alerts"])
        self.assertEqual(m["invoke_count"], 0)

    def test_error_metric(self):
        def boom(_payload):
            return (503, {"ok": False, "error_code": "agent_unavailable", "message": "down"})

        # Tool runner normalizes errors into evidence rather than raising for gateway errors —
        # use input_guard overflow for a hard ok=False path, plus agent_unavailable in tool path.
        long_msg = "x" * 501
        r = self._chat(long_msg)
        self.assertFalse(r["ok"])
        m = self._last_metric()
        self.assertFalse(m["ok"])
        self.assertIn("error", m["alerts"])
        self.assertIsNotNone(m.get("error_code"))
        blob = json.dumps(m)
        self.assertNotIn("xxxxx", blob)

    def test_permission_denied_metric(self):
        def deny(payload):
            self.invokes.append(payload)
            return (
                403,
                {"ok": False, "error_code": "permission_denied", "message": "no access"},
            )

        r = self._chat("stock 2404", invoke_fn=deny, force_scenario="inventory_only")
        self.assertTrue(r["ok"])  # grounded denial reply
        m = self._last_metric()
        self.assertTrue(m["permission_denied"])
        self.assertIn("permission_denied", m["alerts"])
        self.assertEqual(m["tools_used"], ["get_inventory"])

    def test_agent_unavailable_metric(self):
        def down(payload):
            self.invokes.append(payload)
            return (
                503,
                {"ok": False, "error_code": "agent_unavailable", "message": "gateway down"},
            )

        r = self._chat("stock 2404", invoke_fn=down, force_scenario="inventory_only")
        self.assertTrue(r["ok"])
        m = self._last_metric()
        self.assertTrue(m["agent_unavailable"])
        self.assertIn("agent_unavailable", m["alerts"])

    def test_replan_metric(self):
        class FlakyPlanner:
            def __init__(self):
                self.calls = 0
                self.last_usage = None

            def plan(self, message, *, context=None):
                self.calls += 1
                if self.calls == 1:
                    return {"plan_id": "bad", "steps": "not-a-list"}  # triggers replan
                return {
                    "plan_id": "ok",
                    "user_intent": "stock",
                    "answer_style": "operational",
                    "scenario": "inventory_only",
                    "steps": [
                        {
                            "step": 1,
                            "tool": "get_inventory",
                            "arguments": {"codigo": "2404"},
                            "reason": "stock",
                        }
                    ],
                }

        planner = FlakyPlanner()
        r = run_orchestrator_chat(
            message="stock 2404",
            actor_user="albertadmin",
            conversation_id="conv-replan",
            invoke_fn=lambda p: _ok_tool("get_inventory"),
            planner=planner,
            audit=self.audit,
            turn_store=self.store,
            metrics_store=self.metrics,
        )
        self.assertTrue(r["ok"])
        self.assertEqual(planner.calls, 2)
        m = self._last_metric()
        self.assertEqual(m["replan_count"], 1)

    def test_evidence_reuse_metric(self):
        r1 = self._chat("¿Cuánto stock del 2404?", force_scenario="inventory_only")
        self.assertTrue(r1["ok"])
        r2 = self._chat("¿Y cuánto queda?")
        self.assertTrue(r2["ok"])
        self.assertTrue(r2.get("reuse_prior_evidence"))
        m = self._last_metric()
        self.assertTrue(m["reuse_prior_evidence"])
        self.assertEqual(m["invoke_count"], 0)
        conv = self.metrics.get_conversation("albertadmin", "conv-m6")
        self.assertIsNotNone(conv)
        self.assertGreaterEqual(conv["reuse_count"], 1)
        self.assertGreaterEqual(conv["turns"], 2)

    def test_latency_metric(self):
        r = self._chat("stock 2404", force_scenario="inventory_only")
        self.assertTrue(r["ok"])
        m = self._last_metric()
        self.assertIsInstance(m["total_latency_ms"], int)
        self.assertGreaterEqual(m["total_latency_ms"], 0)
        self.assertIsInstance(m["llm_latency_ms"], int)
        self.assertGreaterEqual(m["llm_latency_ms"], 0)

    def test_cost_tokens_without_pii_in_jsonl(self):
        with patch.dict(
            os.environ,
            {
                "ANDES_LLM_COST_INPUT_PER_1K": "0.01",
                "ANDES_LLM_COST_OUTPUT_PER_1K": "0.02",
            },
            clear=False,
        ):

            class TokenPlanner:
                last_usage = {"prompt_tokens": 200, "completion_tokens": 50, "total_tokens": 250}

                def plan(self, message, *, context=None):
                    return FakePlanner().plan(message, context=context)

            r = run_orchestrator_chat(
                message="stock 2404",
                actor_user="albertadmin",
                conversation_id="conv-cost",
                invoke_fn=lambda p: _ok_tool("get_inventory"),
                planner=TokenPlanner(),
                audit=self.audit,
                turn_store=TurnStore(),
                metrics_store=self.metrics,
                force_scenario="inventory_only",
            )
            self.assertTrue(r["ok"])
            m = self._last_metric()
            self.assertEqual(m["prompt_tokens"], 200)
            self.assertEqual(m["completion_tokens"], 50)
            self.assertEqual(m["total_tokens"], 250)
            self.assertAlmostEqual(m["cost_estimated_usd"], 0.003)
            raw = self.metrics_path.read_text(encoding="utf-8")
            self.assertNotIn("stock 2404", raw)
            self.assertNotIn("Bearer sk", raw)
            self.assertNotIn("Authorization", raw)

    def test_summary_hotspots(self):
        # Force high latency alert via direct record
        self.metrics.record_turn(
            build_turn_metric(
                actor_user="albertadmin",
                conversation_id="hot",
                correlation_id="c",
                message_hash_value="hhhh",
                ok=False,
                error_code="agent_error",
                tools_used=["get_inventory", "get_product", "get_supplier"],
                invoke_count=3,
                needs_clarification=True,
                total_latency_ms=9000,
            )
        )
        summary = self.metrics.summary()
        self.assertTrue(summary["ok"])
        self.assertGreaterEqual(summary["hotspots"]["too_many_tools"], 1)
        self.assertGreaterEqual(summary["hotspots"]["clarifications"], 1)
        self.assertGreaterEqual(summary["hotspots"]["high_latency"], 1)
        self.assertGreaterEqual(summary["hotspots"]["errors"], 1)
        blob = json.dumps(summary)
        self.assertNotIn("Bearer", blob)
        self.assertNotIn("@", blob)


if __name__ == "__main__":
    unittest.main()
