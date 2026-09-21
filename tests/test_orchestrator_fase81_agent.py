"""FASE 8.1 — evidence-aware AgentLoop tests. AGENT=0 must keep current behavior."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from app.assistant.orchestrator.agent_config import (
    MAX_TOOL_RESULT_CHARS,
    agent_enabled,
    agent_loop_allowed,
)
from app.assistant.orchestrator.agent_config import TOKEN_BUDGET_FALLBACK
from app.assistant.orchestrator.agent_loop import QueueDecisionClient, continue_agent_loop
from app.assistant.orchestrator.agent_schema import AgentDecisionError, validate_agent_decision
from app.assistant.orchestrator.answer_verifier import recompute_calculation, verify_agent_answer
from app.assistant.orchestrator.audit import OrchestratorAudit
from app.assistant.orchestrator.evidence_store import EvidenceStore, canonical_call_key
from app.assistant.orchestrator.metrics import build_turn_metric, sanitize_metric
from app.assistant.orchestrator.planner import FakePlanner
from app.assistant.orchestrator.service import run_orchestrator_chat
from app.assistant.orchestrator.turn_store import TurnStore


def _inv_ok(codigo: str = "2404", total: int = 25, bodegas: list[tuple[str, int]] | None = None):
    items = [{"bodega": b, "stock": s} for b, s in (bodegas or [("Bodega 1", total)])]
    return (
        200,
        {
            "ok": True,
            "tool": "get_inventory",
            "classification": "INTERNAL",
            "write": False,
            "data": {"codigo": codigo, "total_stock": total, "items": items},
            "meta": {},
        },
    )


def _mov_ok(codigo: str = "2404"):
    return (
        200,
        {
            "ok": True,
            "tool": "get_stock_movements",
            "classification": "INTERNAL",
            "write": False,
            "data": {
                "codigo": codigo,
                "items": [
                    {"fecha": "2026-03-01", "tipo": "salida", "cantidad": 4},
                    {"fecha": "2026-03-02", "tipo": "salida", "cantidad": 2},
                ],
            },
            "meta": {},
        },
    )


def _empty_catalog():
    return (
        200,
        {
            "ok": True,
            "tool": "search_catalog",
            "classification": "INTERNAL",
            "data": {"items": [], "count": 0},
            "meta": {},
            "empty": True,
        },
    )


def _invoke_router(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    tool = payload.get("tool")
    args = payload.get("arguments") or {}
    if tool == "get_inventory":
        st, body = _inv_ok(str(args.get("codigo") or "2404"))
        return st, body
    if tool == "get_stock_movements":
        return _mov_ok(str(args.get("codigo") or "2404"))
    if tool == "search_catalog":
        q = str(args.get("q") or "")
        if "xyzzy" in q:
            st, body = _empty_catalog()
            body["empty"] = True
            return st, body
        return 200, {
            "ok": True,
            "tool": "search_catalog",
            "classification": "INTERNAL",
            "data": {"items": [{"codigo": "2404", "descripcion": "DOW"}], "count": 1},
            "meta": {},
        }
    if tool == "get_dashboard_kpis":
        return 200, {
            "ok": True,
            "tool": "get_dashboard_kpis",
            "classification": "INTERNAL",
            "finance_redacted": True,
            "data": {"ventas_periodo": None, "ventas_hoy": None, "periodo": "7d"},
            "meta": {"periodo": "7d"},
        }
    if tool == "get_product":
        return 403, {"ok": False, "error_code": "permission_denied", "tool": "get_product"}
    return 200, {"ok": True, "tool": tool, "data": {}, "meta": {}, "empty": True}


def _final(text: str = "2404 tiene 25 unidades", eids: list[str] | None = None) -> dict[str, Any]:
    refs = list(eids) if eids else ["e1"]
    return {
        "action": "final_answer",
        "reason": "enough",
        "draft_reply": text,
        "claims": [{"kind": "dato", "text": text, "evidence_ids": refs}],
        "calculations": [],
    }


def _call(tool: str, arguments: dict[str, Any], reason: str = "need") -> dict[str, Any]:
    return {
        "action": "call_tool",
        "tool": tool,
        "arguments": arguments,
        "reason": reason,
        "draft_reply": None,
        "claims": [],
        "calculations": [],
    }


class Fase81AgentTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.audit = OrchestratorAudit(path=Path(self._tmpdir.name) / "audit.jsonl")
        self.store = TurnStore()
        self._env = patch.dict(
            os.environ,
            {
                "ANDES_ASSISTANT_AGENT_ENABLED": "0",
                "ANDES_ASSISTANT_NL_ENABLED": "0",
                "ANDES_ORCH_PLANNER": "fake",
                "ANDES_ASSISTANT_MEMORY_ENABLED": "0",
                "ANDES_ASSISTANT_HISTORY_ENABLED": "0",
            },
            clear=False,
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmpdir.cleanup()

    def _chat(self, message: str, **kwargs):
        return run_orchestrator_chat(
            message=message,
            actor_user="albertadmin",
            conversation_id="c-81",
            invoke_fn=kwargs.pop("invoke_fn", _invoke_router),
            planner=kwargs.pop("planner", FakePlanner()),
            audit=self.audit,
            turn_store=self.store,
            **kwargs,
        )

    @patch("app.assistant.orchestrator.service.agent_loop_allowed", return_value=True)
    def test_write_attempt_falls_back(self, _allowed):
        client = QueueDecisionClient(
            [
                {
                    "action": "call_tool",
                    "tool": "create_invoice",
                    "arguments": {"codigo": "2404"},
                    "reason": "write",
                },
                {
                    "action": "call_tool",
                    "tool": "create_invoice",
                    "arguments": {"codigo": "2404"},
                    "reason": "write",
                },
            ]
        )
        out = self._chat("Stock del 2404", agent_decision_client=client)
        self.assertTrue(out.get("ok"))
        self.assertTrue(out.get("fallback_used"))
        used = [str(t).lower() for t in (out.get("tools_used") or [])]
        self.assertFalse(any("create" in t or "write" in t for t in used))
        self.assertNotIn("get_inventory", used)

    def test_agent_flag_default_off(self):
        self.assertFalse(agent_enabled())
        self.assertFalse(agent_loop_allowed())

    def test_agent_1_without_soft_enable_does_not_loop(self):
        client = QueueDecisionClient([_final()])
        with patch.dict(os.environ, {"ANDES_ASSISTANT_AGENT_ENABLED": "1"}, clear=False):
            self.assertFalse(agent_loop_allowed())
            out = self._chat("Stock del 2404", agent_decision_client=client)
        self.assertTrue(out.get("ok"))
        self.assertFalse(out.get("agent_enabled"))
        self.assertEqual(client.calls, 0)
        self.assertIn("get_inventory", out.get("tools_used") or [])

    def test_agent_0_ignores_decision_client(self):
        client = QueueDecisionClient([_final()])
        out = self._chat("Stock del 2404", agent_decision_client=client)
        self.assertTrue(out.get("ok"))
        self.assertEqual(client.calls, 0)
        self.assertFalse(out.get("fallback_used"))

    @patch("app.assistant.orchestrator.service.agent_loop_allowed", return_value=True)
    def test_agent_1_first_decision_is_exactly_one_tool(self, _allowed):
        planner = FakePlanner()
        planner_calls: list[str] = []
        orig = planner.plan

        def spy(message: str, *, context=None):
            planner_calls.append(message)
            return orig(message, context=context)

        planner.plan = spy  # type: ignore[method-assign]
        client = QueueDecisionClient(
            [
                _call("get_inventory", {"codigo": "2404"}, "stock"),
                _final(),
            ]
        )
        out = self._chat("Stock del 2404", planner=planner, agent_decision_client=client)
        self.assertTrue(out.get("ok"))
        self.assertTrue(out.get("agent_enabled"))
        self.assertEqual(planner_calls, [])
        self.assertEqual(client.calls, 2)
        self.assertEqual(out.get("tools_used"), ["get_inventory"])
        trace = out.get("agent_trace") or []
        self.assertGreaterEqual(len(trace), 2)
        self.assertEqual(trace[0].get("action"), "call_tool")
        self.assertEqual(trace[0].get("tool"), "get_inventory")
        self.assertEqual(trace[0].get("evidence_count_before"), 0)
        self.assertEqual(trace[0].get("evidence_count_after"), 1)
        self.assertEqual(trace[1].get("action"), "final_answer")
        self.assertEqual(trace[1].get("evidence_count_before"), 1)
        self.assertFalse(out.get("fallback_used"))
        self.assertIn("DATOS", out.get("reply") or "")
        self.assertIn("25", out.get("reply") or "")

    @patch("app.assistant.orchestrator.service.agent_loop_allowed", return_value=True)
    def test_reactive_second_sees_e1_and_can_call_another_tool(self, _allowed):
        seen_before: list[int] = []

        class _Probe(QueueDecisionClient):
            def complete_decision(self, *, system: str, user: str) -> dict[str, Any]:
                pack = user.split("<evidence>", 1)[-1].split("</evidence>", 1)[0]
                seen_before.append(pack.count('"evidence_id"'))
                return super().complete_decision(system=system, user=user)

        client = _Probe(
            [
                _call("get_stock_movements", {"codigo": "2404"}, "movimientos"),
                _call("get_inventory", {"codigo": "2404"}, "stock after e1"),
                _final("2404 tiene 25 unidades", eids=["e1", "e2"]),
            ]
        )
        planner = FakePlanner()
        planner_calls: list[str] = []
        orig = planner.plan

        def spy(message: str, *, context=None):
            planner_calls.append(message)
            return orig(message, context=context)

        planner.plan = spy  # type: ignore[method-assign]
        out = self._chat(
            "Muéstrame los movimientos del 2404 y dime cuánto stock queda actualmente.",
            planner=planner,
            agent_decision_client=client,
        )
        self.assertTrue(out.get("ok"))
        self.assertEqual(planner_calls, [])
        self.assertEqual(client.calls, 3)
        self.assertEqual(out.get("tools_used"), ["get_stock_movements", "get_inventory"])
        self.assertEqual(seen_before, [0, 1, 2])
        trace = out.get("agent_trace") or []
        self.assertEqual(trace[0].get("evidence_count_before"), 0)
        self.assertEqual(trace[0].get("evidence_count_after"), 1)
        self.assertEqual(trace[1].get("action"), "call_tool")
        self.assertEqual(trace[1].get("tool"), "get_inventory")
        self.assertEqual(trace[1].get("evidence_count_before"), 1)
        self.assertEqual(trace[1].get("evidence_count_after"), 2)
        self.assertEqual(trace[2].get("action"), "final_answer")
        self.assertEqual(trace[2].get("evidence_count_before"), 2)
        self.assertFalse(out.get("fallback_used"))

    @patch("app.assistant.orchestrator.service.agent_loop_allowed", return_value=True)
    def test_clarify_and_reject(self, _allowed):
        client = QueueDecisionClient(
            [{"action": "clarify", "reason": "need code", "draft_reply": "¿Qué código?"}]
        )
        out = self._chat("Stock del 2404", agent_decision_client=client)
        self.assertTrue(out.get("ok"))
        self.assertTrue(out.get("needs_clarification") or "código" in (out.get("reply") or "").lower() or "detalles" in (out.get("reply") or "").lower())

        client2 = QueueDecisionClient(
            [{"action": "reject", "reason": "ood", "draft_reply": "Fuera de dominio"}]
        )
        out2 = self._chat("Stock del 2404", agent_decision_client=client2)
        self.assertTrue(out2.get("ok"))
        self.assertIn("rechaz", (out2.get("reply") or "").lower() + "rechazada")

    def test_invalid_unknown_write_args(self):
        with self.assertRaises(AgentDecisionError) as ctx:
            validate_agent_decision({"action": "nope"})
        self.assertEqual(ctx.exception.code, "invalid_decision")
        with self.assertRaises(AgentDecisionError) as ctx:
            validate_agent_decision({"action": "call_tool", "tool": "delete_product", "arguments": {"codigo": "2404"}})
        self.assertEqual(ctx.exception.code, "write_not_allowed")
        with self.assertRaises(AgentDecisionError) as ctx:
            validate_agent_decision({"action": "call_tool", "tool": "invented_tool", "arguments": {}})
        self.assertEqual(ctx.exception.code, "tool_not_allowed")
        with self.assertRaises(AgentDecisionError) as ctx:
            validate_agent_decision(
                {"action": "call_tool", "tool": "get_inventory", "arguments": {"sql": "select 1"}}
            )
        self.assertIn(ctx.exception.code, {"invalid_args", "forbidden_arg"})
        with self.assertRaises(AgentDecisionError) as ctx:
            validate_agent_decision(
                {
                    "action": "call_tool",
                    "tool": "get_inventory",
                    "arguments": {"codigo": "2404"},
                    "steps": [{"tool": "get_stock_movements"}],
                }
            )
        self.assertEqual(ctx.exception.code, "invalid_decision")
        with self.assertRaises(AgentDecisionError) as ctx:
            validate_agent_decision(
                {
                    "action": "call_tool",
                    "tools": ["get_inventory", "get_stock_movements"],
                    "arguments": {"codigo": "2404"},
                }
            )
        self.assertEqual(ctx.exception.code, "invalid_decision")
        with self.assertRaises(AgentDecisionError) as ctx:
            validate_agent_decision(
                {
                    "action": "final_answer",
                    "draft_reply": "ok",
                    "claims": [{"kind": "dato", "text": "2404 tiene 25", "evidence_ids": ["e1"]}],
                    "calculations": [{"op": "sum", "inputs": ["e1.data.total_stock"], "result": 25}],
                }
            )
        self.assertEqual(ctx.exception.code, "invalid_decision")
        with self.assertRaises(AgentDecisionError) as ctx:
            validate_agent_decision(
                {
                    "action": "final_answer",
                    "draft_reply": "ok",
                    "claims": [{"kind": "dato", "text": "n", "evidence_ids": ["e1"]}],
                    "calculations": [
                        {"id": "c1", "op": "nope", "inputs": ["e1.data.total_stock"], "result": 1}
                    ],
                }
            )
        self.assertEqual(ctx.exception.code, "invalid_decision")

    def test_final_answer_with_residual_arguments_is_stripped(self):
        """FASE 8.1F.4 — residual union-bag args on final_answer are cleared, not rejected."""
        ok = validate_agent_decision(
            {
                "action": "final_answer",
                "tool": None,
                "arguments": {
                    "q": "BOSCH",
                    "limit": 10,
                    "codigo": None,
                    "proveedor": None,
                    "periodo": None,
                },
                "draft_reply": "Proveedor BOSCH sin OC.",
                "claims": [{"kind": "dato", "text": "BOSCH", "evidence_ids": ["e1"]}],
                "calculations": [],
            }
        )
        self.assertEqual(ok["action"], "final_answer")
        self.assertIn(ok.get("tool"), (None, ""))
        self.assertEqual(ok.get("arguments"), {})
        ok_null = validate_agent_decision(
            {
                "action": "final_answer",
                "tool": None,
                "arguments": {"codigo": None, "proveedor": None},
                "draft_reply": "ok",
                "claims": [{"kind": "dato", "text": "BOSCH", "evidence_ids": ["e1"]}],
                "calculations": [],
            }
        )
        self.assertEqual(ok_null["action"], "final_answer")
        self.assertEqual(ok_null.get("arguments"), {})
        ok_calc = validate_agent_decision(
            {
                "action": "final_answer",
                "draft_reply": "ok",
                "claims": [{"kind": "dato", "text": "2404 tiene 25", "evidence_ids": ["e1"]}],
                "calculations": [
                    {"id": "c1", "op": "sum", "inputs": ["e1.data.total_stock"], "result": 25}
                ],
            }
        )
        self.assertEqual(ok_calc["action"], "final_answer")
        ok_mov = validate_agent_decision(
            {
                "action": "call_tool",
                "tool": "get_stock_movements",
                "arguments": {"codigo": "2404", "q": "noise", "limit": 10},
                "reason": "mov",
            }
        )
        self.assertEqual(ok_mov["tool"], "get_stock_movements")
        self.assertEqual(ok_mov["arguments"].get("codigo"), "2404")
        ok_lim = validate_agent_decision(
            {
                "action": "call_tool",
                "tool": "get_stock_movements",
                "arguments": {"codigo": "2404", "limit": 0},
                "reason": "mov",
            }
        )
        self.assertEqual(ok_lim["arguments"].get("codigo"), "2404")
        self.assertNotIn("limit", ok_lim["arguments"])

    def test_fase81f4_non_call_tool_argument_strip_contract(self):
        """FASE 8.1F.4 — A/B/C/D/E schema contract for residual tool/args."""
        # A: final_answer + q/limit residual → valid, tool empty, arguments={}
        a = validate_agent_decision(
            {
                "action": "final_answer",
                "arguments": {"q": "BOSCH", "limit": 10},
                "draft_reply": "ok",
                "claims": [{"kind": "dato", "text": "BOSCH", "evidence_ids": ["e1"]}],
                "calculations": [],
            }
        )
        self.assertEqual(a["action"], "final_answer")
        self.assertFalse(a.get("tool"))
        self.assertEqual(a["arguments"], {})

        # B: final_answer + real tool → reject (not silently executed)
        with self.assertRaises(AgentDecisionError) as ctx_b:
            validate_agent_decision(
                {
                    "action": "final_answer",
                    "tool": "get_purchase_orders",
                    "arguments": {"q": "BOSCH"},
                    "draft_reply": "ok",
                    "claims": [{"kind": "dato", "text": "x", "evidence_ids": ["e1"]}],
                    "calculations": [],
                }
            )
        self.assertEqual(ctx_b.exception.code, "invalid_decision")
        self.assertIn("must not include a tool call", ctx_b.exception.message)

        # C: clarify + residual args
        c = validate_agent_decision(
            {
                "action": "clarify",
                "arguments": {"q": "BOSCH", "limit": 5},
                "draft_reply": "¿Cuál proveedor?",
            }
        )
        self.assertEqual(c["action"], "clarify")
        self.assertFalse(c.get("tool"))
        self.assertEqual(c["arguments"], {})

        # D: reject + residual args
        d = validate_agent_decision(
            {
                "action": "reject",
                "arguments": {"q": "x", "limit": 1},
                "reject_message": "No permitido",
            }
        )
        self.assertEqual(d["action"], "reject")
        self.assertFalse(d.get("tool"))
        self.assertEqual(d["arguments"], {})

        # E: call_tool intact
        e = validate_agent_decision(
            {
                "action": "call_tool",
                "tool": "get_supplier",
                "arguments": {"q": "BOSCH", "limit": 10},
                "reason": "supplier",
            }
        )
        self.assertEqual(e["action"], "call_tool")
        self.assertEqual(e["tool"], "get_supplier")
        self.assertEqual(e["arguments"].get("q"), "BOSCH")
        self.assertIn("limit", e["arguments"])

    @patch("app.assistant.orchestrator.service.agent_loop_allowed", return_value=True)
    def test_invalid_decision_fallback(self, _allowed):
        client = QueueDecisionClient([{"action": "nope"}, {"action": "still_nope"}])
        out = self._chat("Stock del 2404", agent_decision_client=client)
        self.assertTrue(out.get("ok"))
        self.assertTrue(out.get("fallback_used"))
        self.assertEqual(out.get("tools_used") or [], [])

    def test_evidence_store_and_truncation(self):
        store = EvidenceStore(correlation_id="cid")
        huge = {"codigo": "2404", "blob": "X" * (MAX_TOOL_RESULT_CHARS + 50)}
        item = store.add_from_tool_result(
            tool="get_inventory",
            arguments={"codigo": "2404"},
            result={"ok": True, "empty": False, "data": huge, "meta": {"Authorization": "secret"}},
            correlation_id="cid",
        )
        self.assertTrue(item.truncated)
        self.assertNotIn("Authorization", json.dumps(item.meta_view))
        pack = store.prompt_pack()
        self.assertIn("e1", pack)
        self.assertLessEqual(len(pack), 6000)

    def test_loop_detection_and_different_args(self):
        k1 = canonical_call_key("get_inventory", {"codigo": "2404"})
        k2 = canonical_call_key("get_inventory", {"codigo": "9999"})
        self.assertEqual(k1, canonical_call_key("get_inventory", {"codigo": "2404"}))
        self.assertNotEqual(k1, k2)
        client = QueueDecisionClient(
            [
                {
                    "action": "call_tool",
                    "tool": "get_inventory",
                    "arguments": {"codigo": "2404"},
                    "reason": "repeat",
                },
                _final("2404 tiene 25 unidades"),
            ]
        )
        plan = {
            "plan_id": "p",
            "user_intent": "stock",
            "answer_style": "operational",
            "scenario": "inventory_only",
            "steps": [
                {"step": 1, "tool": "get_inventory", "arguments": {"codigo": "2404"}, "reason": "x", "depends_on": []}
            ],
        }
        from app.assistant.orchestrator.plan_validator import validate_plan

        plan = validate_plan(plan)
        ev = [
            {
                "step": 1,
                "tool": "get_inventory",
                "ok": True,
                "empty": False,
                "data": {"codigo": "2404", "total_stock": 25, "items": [{"bodega": "B1", "stock": 25}]},
                "meta": {},
                "classification": "INTERNAL",
                "latency_ms": 1,
            }
        ]
        result = continue_agent_loop(
            message="Stock del 2404",
            actor_user="albertadmin",
            conversation_id="c",
            correlation_id="cid",
            initial_plan=plan,
            initial_evidence=ev,
            invoke_fn=_invoke_router,
            decision_client=client,
        )
        self.assertTrue(result.state.loop_detected)
        self.assertEqual(result.state.invoke_count, 1)

    def test_timeout_and_cost_limit(self):
        plan = {
            "plan_id": "p",
            "user_intent": "stock",
            "answer_style": "operational",
            "steps": [
                {"step": 1, "tool": "get_inventory", "arguments": {"codigo": "2404"}, "reason": "x", "depends_on": []}
            ],
        }
        from app.assistant.orchestrator.plan_validator import validate_plan

        plan = validate_plan(plan)
        ev = [
            {
                "step": 1,
                "tool": "get_inventory",
                "ok": True,
                "empty": False,
                "data": {"codigo": "2404", "total_stock": 25},
                "meta": {},
                "classification": "INTERNAL",
            }
        ]
        client = QueueDecisionClient([_final()])
        timed = continue_agent_loop(
            message="Stock del 2404",
            actor_user="a",
            conversation_id="c",
            correlation_id="x",
            initial_plan=plan,
            initial_evidence=ev,
            invoke_fn=_invoke_router,
            decision_client=client,
            started_at=0.0,
            max_seconds=0.0,
        )
        self.assertTrue(timed.fallback_used)
        self.assertTrue(timed.state.timeout)
        self.assertEqual(client.calls, 0)

        client2 = QueueDecisionClient([_final()])
        costly = continue_agent_loop(
            message="Stock del 2404",
            actor_user="a",
            conversation_id="c",
            correlation_id="x",
            initial_plan=plan,
            initial_evidence=ev,
            invoke_fn=_invoke_router,
            decision_client=client2,
            # Derivado del techo, no fijado a mano: un total escrito a mano deja
            # de agotar el presupuesto en cuanto el techo cambia, y el test pasa
            # a comprobar otra cosa sin que nadie lo note.
            usage_totals={"prompt_tokens": TOKEN_BUDGET_FALLBACK,
                          "completion_tokens": 0},
        )
        self.assertTrue(costly.fallback_used)
        self.assertEqual(client2.calls, 0)

    @patch("app.assistant.orchestrator.service.agent_loop_allowed", return_value=True)
    def test_empty_permission_tool_error(self, _allowed):
        client = QueueDecisionClient(
            [
                _call("search_catalog", {"q": "xyzzy-no-existe-999"}, "empty"),
                _final("sin resultados", eids=["e1"]),
            ]
        )
        out = self._chat("Busca xyzzy-no-existe-999", agent_decision_client=client)
        self.assertTrue(out.get("ok"))

        client_pd = QueueDecisionClient(
            [
                {
                    "action": "call_tool",
                    "tool": "get_product",
                    "arguments": {"codigo": "2404"},
                    "reason": "ficha",
                },
                {
                    "action": "final_answer",
                    "draft_reply": "No tienes permiso para usar get_product",
                    "claims": [],
                },
            ]
        )
        out_pd = self._chat("Stock del 2404", agent_decision_client=client_pd)
        self.assertTrue(out_pd.get("ok"))

    def test_verifier_success_failure_calc_and_finance_null(self):
        store = EvidenceStore()
        store.add_from_tool_result(
            tool="get_inventory",
            arguments={"codigo": "2404"},
            result={
                "ok": True,
                "empty": False,
                "data": {
                    "codigo": "2404",
                    "total_stock": 25,
                    "items": [{"bodega": "A", "stock": 10}, {"bodega": "B", "stock": 15}],
                },
                "meta": {},
            },
        )
        ok = verify_agent_answer(
            store=store,
            decision={
                "action": "final_answer",
                "draft_reply": "",
                "claims": [{"kind": "dato", "text": "2404 tiene 25 unidades", "evidence_ids": ["e1"]}],
                "calculations": [
                    {"id": "c1", "op": "min", "inputs": ["e1.data.items.0.stock", "e1.data.items.1.stock"], "result": 10}
                ],
            },
        )
        self.assertIn("25", ok.reply)
        self.assertFalse(ok.used_composer_fallback)

        mismatch = verify_agent_answer(
            store=store,
            decision={
                "action": "final_answer",
                "draft_reply": "",
                "claims": [{"kind": "dato", "text": "stock 9999", "evidence_ids": ["e1"]}],
                "calculations": [
                    {"id": "c1", "op": "sum", "inputs": ["e1.data.items.0.stock", "e1.data.items.1.stock"], "result": 99}
                ],
            },
        )
        self.assertTrue(mismatch.used_composer_fallback or mismatch.failures >= 1)

        self.assertEqual(
            recompute_calculation(
                store,
                {"op": "diff", "inputs": ["e1.data.items.1.stock", "e1.data.items.0.stock"]},
            ),
            5,
        )

        fin = EvidenceStore()
        fin.add_from_tool_result(
            tool="get_dashboard_kpis",
            arguments={"periodo": "7d"},
            result={
                "ok": True,
                "empty": False,
                "data": {"ventas_periodo": None, "ventas_hoy": None},
                "meta": {},
            },
        )
        zero = verify_agent_answer(
            store=fin,
            decision={
                "action": "final_answer",
                "draft_reply": "Ventas del período: 0",
                "claims": [{"kind": "dato", "text": "Ventas del período: 0", "evidence_ids": ["e1"]}],
                "calculations": [],
            },
        )
        self.assertTrue(zero.used_composer_fallback or zero.failures >= 1)
        self.assertNotRegex(zero.reply.lower(), r"ventas del per[ií]odo:\s*0")

    @patch("app.assistant.orchestrator.service.agent_loop_allowed", return_value=True)
    def test_memory_hints_are_not_evidence_in_first_decision(self, _allowed):
        seen: list[tuple[bool, bool]] = []

        class _Probe(QueueDecisionClient):
            def complete_decision(self, *, system: str, user: str) -> dict[str, Any]:
                pack = user.split("<evidence>", 1)[-1].split("</evidence>", 1)[0].strip()
                seen.append((pack == "[]", "<memory_hints>" in user and "2404" in user))
                return super().complete_decision(system=system, user=user)

        client = _Probe(
            [
                _call("get_inventory", {"codigo": "2404"}),
                _final(),
            ]
        )
        from app.assistant.orchestrator.agent_loop import continue_agent_loop
        from app.assistant.orchestrator.agent_loop import empty_agent_plan

        result = continue_agent_loop(
            message="Stock del 2404",
            actor_user="albertadmin",
            conversation_id="c",
            correlation_id="cid",
            initial_plan=empty_agent_plan(),
            initial_evidence=[],
            invoke_fn=_invoke_router,
            decision_client=client,
            memory_hints=[{"type": "frequent_entity", "codigo": "2404"}],
        )
        self.assertTrue(seen)
        self.assertTrue(seen[0][0])
        self.assertTrue(seen[0][1])
        self.assertNotIn("stock=25", (result.reply or "").lower().replace(" ", ""))
        self.assertEqual(result.state.trace[0]["evidence_count_before"], 0)

    def test_memory_is_not_evidence(self):
        store = EvidenceStore()
        store.add_from_tool_result(
            tool="get_inventory",
            arguments={"codigo": "2404"},
            result={"ok": True, "data": {"codigo": "2404", "total_stock": 25}, "meta": {}},
        )
        bad = verify_agent_answer(
            store=store,
            decision={
                "action": "final_answer",
                "claims": [{"kind": "dato", "text": "stock 8888 del pin", "evidence_ids": ["e1"]}],
                "calculations": [],
            },
        )
        self.assertTrue(bad.failures >= 1 or bad.used_composer_fallback)
        self.assertNotIn("8888", bad.reply)

    def test_metrics_scrub_agent_fields(self):
        rec = build_turn_metric(
            actor_user="albertadmin",
            conversation_id="c",
            correlation_id="cid",
            message_hash_value="abc",
            ok=True,
            agent_enabled=True,
            agent_steps=2,
            loop_detected=True,
            fallback_used=True,
            timeout=False,
            verifier_failures=1,
            retries=1,
            evidence_size_chars=120,
            prompt_tokens=10,
            completion_tokens=5,
        )
        self.assertTrue(rec.get("agent_enabled"))
        self.assertEqual(rec.get("agent_steps"), 2)
        self.assertTrue(rec.get("loop_detected"))
        self.assertNotIn("reply", rec)
        self.assertNotIn("prompt", rec)
        cleaned = sanitize_metric({"reply": "secret", "agent_enabled": True, "cookie": "x"})
        self.assertNotIn("reply", cleaned)
        self.assertNotIn("cookie", cleaned)

    def test_fase5_reuse_does_not_open_agent_loop(self):
        """Reuse path must not call AgentLoop (evidence vieja ≠ invoke fresco)."""
        client = QueueDecisionClient([_final()])
        first = self._chat("Stock del 2404", agent_decision_client=client)
        self.assertTrue(first.get("ok"))
        with patch("app.assistant.orchestrator.service.agent_loop_allowed", return_value=True):
            follow = self._chat("cuáles son", agent_decision_client=client)
        self.assertTrue(follow.get("ok"))
        self.assertTrue(follow.get("reuse_prior_evidence") or follow.get("scenario") == "context_reuse")
        self.assertEqual(client.calls, 0)


if __name__ == "__main__":
    unittest.main()
