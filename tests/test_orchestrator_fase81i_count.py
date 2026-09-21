"""FASE 8.1I — verifiable cardinality.

A payload may hold a collection of N records without holding N anywhere as a
value, so "hay N productos" could not be proven by either grounding rule and was
discarded even when correct. ``count`` closes that gap WITHOUT loosening anything:
it reads a real collection at an evidence path and the verifier recomputes it.

Scope: the verifier's calculation set and the decision contract. AgentLoop,
GoalCoverage, ProgressLedger, allow_partial and the scorer are untouched.
"""
from __future__ import annotations

import unittest
from typing import Any

from app.assistant.orchestrator.agent_schema import (
    CALC_OPS,
    AgentDecisionError,
    validate_agent_decision,
)
from app.assistant.orchestrator.answer_verifier import (
    CollectionRequired,
    _claim_grounded,
    _evidence_blob,
    grounded_dates,
    grounded_numbers,
    recompute_calculation,
    verify_agent_answer,
)
from app.assistant.orchestrator.evidence_store import EvidenceStore


def _store(data: dict[str, Any], tool: str = "get_dashboard_kpis") -> EvidenceStore:
    store = EvidenceStore()
    store.add_from_tool_result(
        tool=tool, arguments={},
        result={"ok": True, "empty": False, "data": data, "meta": {}},
    )
    return store


def _grounded(store: EvidenceStore, text: str, verified: list[Any] | None = None) -> bool:
    return _claim_grounded(
        text,
        _evidence_blob(store, verified),
        {str(i.tool or "").upper() for i in store.items},
        grounded_numbers(store, verified),
        grounded_dates(store),
    )


def _calc(path: str, result: float, op: str = "count") -> dict[str, Any]:
    return {"id": "c1", "op": op, "inputs": [path], "result": result}


class CountOperationTests(unittest.TestCase):
    """A. B. C. D. E. F. — what count may and may not be pointed at."""

    def test_a_three_element_list_counts_three(self):
        store = _store({"items": [{"a": 1}, {"b": 2}, {"c": 3}]})
        self.assertEqual(recompute_calculation(store, _calc("e1.data.items", 3)), 3.0)

    def test_b_ten_element_list_counts_ten(self):
        store = _store({"stock_critico": [{"codigo": f"C{i}"} for i in range(10)]})
        self.assertEqual(recompute_calculation(store, _calc("e1.data.stock_critico", 10)), 10.0)

    def test_c_empty_list_counts_zero(self):
        store = _store({"top_productos": []})
        self.assertEqual(recompute_calculation(store, _calc("e1.data.top_productos", 0)), 0.0)

    def test_d_count_is_refused_on_a_string(self):
        store = _store({"descripcion": "stock critico"})
        with self.assertRaises(CollectionRequired):
            recompute_calculation(store, _calc("e1.data.descripcion", 13))

    def test_e_count_is_refused_on_a_date(self):
        store = _store({"items": [{"fecha": "2026-07-31"}]})
        with self.assertRaises(CollectionRequired):
            recompute_calculation(store, _calc("e1.data.items.0.fecha", 10))

    def test_f_count_is_refused_on_an_integer(self):
        store = _store({"total": 10})
        with self.assertRaises(CollectionRequired):
            recompute_calculation(store, _calc("e1.data.total", 10))

    def test_count_is_refused_on_a_dict(self):
        """A dict has a length too; counting its keys would be a fabricated figure."""
        store = _store({"resumen": {"a": 1, "b": 2}})
        with self.assertRaises(CollectionRequired):
            recompute_calculation(store, _calc("e1.data.resumen", 2))

    def test_count_is_refused_on_the_whole_evidence_item(self):
        store = _store({"items": [{"a": 1}]})
        with self.assertRaises(CollectionRequired):
            recompute_calculation(store, _calc("e1", 1))

    def test_count_cannot_reach_outside_the_evidence(self):
        store = _store({"items": [{"a": 1}]})
        for path in ("e9.data.items", "raw", "blob", "stock critico", "e1.raw"):
            with self.subTest(path=path):
                with self.assertRaises((KeyError, CollectionRequired)):
                    recompute_calculation(store, _calc(path, 1))

    def test_count_requires_exactly_one_input(self):
        store = _store({"items": [{"a": 1}], "otros": [{"b": 2}]})
        with self.assertRaises(ValueError):
            recompute_calculation(
                store, {"id": "c1", "op": "count", "inputs": ["e1.data.items", "e1.data.otros"], "result": 2}
            )

    def test_nested_collections_are_countable(self):
        store = _store({"items": [{"lineas": [{"x": 1}, {"x": 2}]}]}, tool="get_purchase_orders")
        self.assertEqual(
            recompute_calculation(store, _calc("e1.data.items.0.lineas", 2)), 2.0
        )


class DecisionContractTests(unittest.TestCase):
    def test_count_is_an_allowed_op(self):
        self.assertIn("count", CALC_OPS)
        # El conjunto se fija a propósito: ampliarlo amplía lo que una cifra
        # puede "demostrar", así que cada op nueva es una decisión explícita.
        # 8.5 añadió 'mul' porque sin multiplicación "12 unidades/mes × 2 meses"
        # no era expresable y ninguna proyección podía recomputarse.
        self.assertEqual(CALC_OPS,
                         {"min", "max", "sum", "diff", "ratio", "count", "mul"})

    def test_decision_with_a_count_validates(self):
        out = validate_agent_decision(
            {
                "action": "final_answer",
                "draft_reply": "",
                "claims": [{"kind": "dato", "text": "hay 10 productos", "evidence_ids": ["e1"]}],
                "calculations": [_calc("e1.data.stock_critico", 10)],
            }
        )
        self.assertEqual(out["calculations"][0]["op"], "count")

    def test_count_arity_is_enforced_at_decision_time(self):
        with self.assertRaises(AgentDecisionError):
            validate_agent_decision(
                {
                    "action": "final_answer",
                    "draft_reply": "",
                    "claims": [{"kind": "dato", "text": "hay 10", "evidence_ids": ["e1"]}],
                    "calculations": [
                        {"id": "c1", "op": "count", "inputs": ["e1.data.a", "e1.data.b"], "result": 2}
                    ],
                }
            )

    def test_the_model_is_told_the_op_exists(self):
        """A grounding route the prompt never mentions is unreachable in practice."""
        from app.assistant.orchestrator.llm.agent_prompts import build_agent_system_prompt
        from app.assistant.orchestrator.llm.plan_schema import agent_decision_response_format

        self.assertIn("count", build_agent_system_prompt())
        blob = str(agent_decision_response_format())
        self.assertIn("count", blob)

    def test_a_cardinality_requires_a_declared_count(self):
        """FASE 8.1I.2 / 8.1J — the invariant, not the wording.

        8.1I.2 stated this with a shouted OBLIGATORIO block that made counting
        the dominant calculation guidance in the whole prompt. 8.1J keeps the
        rule and generalizes it to every derived figure, so the assertions here
        are about what must hold, not about the phrasing.
        """
        from app.assistant.orchestrator.llm.agent_prompts import build_agent_system_prompt

        prompt = build_agent_system_prompt()
        self.assertIn("CANTIDAD de elementos no está en <evidence>", prompt)
        self.assertIn("requiere count", prompt)
        # the non-numeric way out, for any derived figure
        self.assertIn("varios", prompt)
        # count is restricted to collections
        self.assertIn("nunca a objetos, strings ni escalares", prompt)

    def test_the_input_path_grammar_is_documented(self):
        """FASE 8.1J — the only path example used to be the whole-list one for
        count, so an indexed scalar path had to be invented. That is the shape
        that produces calc_unresolved."""
        from app.assistant.orchestrator.llm.agent_prompts import build_agent_system_prompt

        prompt = build_agent_system_prompt()
        self.assertIn("data|meta|arguments", prompt)
        self.assertIn("e1.data.items.0.cantidad", prompt)
        self.assertIn("evidence_id que exista", prompt)

    def test_cardinality_guidance_does_not_dominate_the_prompt(self):
        """A rule that crowds out the rest of the contract is a regression risk:
        the same prompt has to keep multi-tool selection working."""
        from app.assistant.orchestrator.llm.agent_prompts import build_agent_system_prompt

        lines = build_agent_system_prompt().splitlines()
        counting = [l for l in lines if "count" in l.lower()]
        self.assertLessEqual(len(counting), 3, [l[:60] for l in counting])

    def test_the_prompt_never_names_a_benchmark_case(self):
        """The example is a data path, not case knowledge."""
        from app.assistant.orchestrator.llm.agent_prompts import build_agent_system_prompt

        prompt = build_agent_system_prompt()
        for case_id in ("G03", "P04", "R03", "C04", "M02", "M04", "K05"):
            self.assertNotIn(case_id, prompt, case_id)


class CardinalityGroundingTests(unittest.TestCase):
    """G. H. I. — the claim only survives when the count actually proves it."""

    def _kpi(self, n: int = 10) -> EvidenceStore:
        return _store(
            {
                "ventas_periodo": 0.0,
                "docs_periodo": 0,
                "stock_critico": [{"codigo": f"VG{4000 + i}", "stock": 0} for i in range(n)],
                "top_productos": [],
            }
        )

    def _answer(self, store: EvidenceStore, text: str, calcs: list[dict[str, Any]]):
        return verify_agent_answer(
            store=store,
            decision={
                "action": "final_answer", "draft_reply": "",
                "claims": [{"kind": "dato", "text": text, "evidence_ids": ["e1"]}],
                "calculations": calcs,
            },
        )

    def test_g_ten_products_with_a_real_collection_is_grounded(self):
        store = self._kpi(10)
        self.assertFalse(_grounded(store, "Hay 10 productos con stock critico."))
        out = self._answer(
            store, "Hay 10 productos con stock critico.", [_calc("e1.data.stock_critico", 10)]
        )
        self.assertEqual(out.failures, 0)
        self.assertEqual(out.dropped_claims, 0)
        self.assertFalse(out.answer_replaced)
        self.assertIn("10", out.reply)

    def test_h_eleven_against_a_ten_element_collection_is_not_grounded(self):
        store = self._kpi(10)
        out = self._answer(
            store, "Hay 11 productos con stock critico.", [_calc("e1.data.stock_critico", 11)]
        )
        self.assertEqual(out.calc_mismatch, 1)
        self.assertNotIn("11", out.reply)

    def test_i_without_a_real_collection_the_count_grounds_nothing(self):
        store = _store({"ventas_periodo": 0.0})
        out = self._answer(
            store, "Hay 10 productos con stock critico.", [_calc("e1.data.stock_critico", 10)]
        )
        self.assertEqual(out.calc_unresolved, 1)
        self.assertNotIn("10", out.reply)

    def test_a_bare_number_is_still_not_grounded_without_the_count(self):
        store = self._kpi(10)
        self.assertNotIn("10", grounded_numbers(store))
        self.assertTrue(_grounded(store, "Hay 10 productos.", verified=[10.0]))

    def test_counting_an_empty_collection_grounds_zero(self):
        store = self._kpi(10)
        out = self._answer(
            store, "No hay productos en el top: 0.", [_calc("e1.data.top_productos", 0)]
        )
        self.assertEqual(out.failures, 0)
        self.assertIn("0", out.reply)

    def test_count_pointed_at_a_scalar_drops_the_claim(self):
        store = self._kpi(10)
        out = self._answer(
            store, "Hay 10 productos con stock critico.", [_calc("e1.data.docs_periodo", 10)]
        )
        self.assertEqual(out.calc_error, 1)
        self.assertEqual(out.calc_unresolved, 0)
        self.assertNotIn("10 productos", out.reply)


class PriorGuardsUnchangedTests(unittest.TestCase):
    """J. K. L. — nothing that used to be rejected becomes acceptable."""

    def _ingresos(self) -> EvidenceStore:
        return _store(
            {
                "items": [
                    {"fecha": "2026-07-31", "numero_documento": "340273", "cantidad": 2},
                    {"fecha": "2026-05-11", "numero_documento": "333210", "cantidad": 1},
                ],
                "count": 2,
            },
            tool="get_ingresos",
        )

    def test_j_c04_unresolved_calculation_still_rejected(self):
        store = self._ingresos()
        self.assertNotIn("3", grounded_numbers(store))
        out = verify_agent_answer(
            store=store,
            decision={
                "action": "final_answer", "draft_reply": "",
                "claims": [{"kind": "dato", "text": "los ingresos suman 3 unidades",
                            "evidence_ids": ["e1"]}],
                "calculations": [{"id": "c1", "op": "sum", "inputs": ["e1.data.total"], "result": 3}],
            },
        )
        self.assertEqual(out.calc_unresolved, 1)
        self.assertNotIn("suman 3", out.reply)

    def test_j2_count_cannot_launder_an_arithmetic_result(self):
        """count over a 3-element list must not be usable to state a sum of 3."""
        store = self._ingresos()
        out = verify_agent_answer(
            store=store,
            decision={
                "action": "final_answer", "draft_reply": "",
                "claims": [{"kind": "dato", "text": "los ingresos suman 3 unidades",
                            "evidence_ids": ["e1"]}],
                "calculations": [_calc("e1.data.items", 3)],
            },
        )
        # The collection has 2 rows, so the count does not verify and 3 stays unproven.
        self.assertEqual(out.calc_mismatch, 1)
        self.assertNotIn("suman 3", out.reply)

    def test_k_m02_shape_still_replaces_the_answer(self):
        store = _store({"codigo": "2404", "total_stock": 2,
                        "items": [{"bodega": "Bodega 1", "stock": 2}]}, tool="get_inventory")
        out = verify_agent_answer(
            store=store,
            decision={
                "action": "final_answer", "draft_reply": "El stock del 2404 es 8888",
                "claims": [{"kind": "dato", "text": "El stock del 2404 es 8888",
                            "evidence_ids": ["e1"]}],
                "calculations": [],
            },
        )
        self.assertTrue(out.answer_replaced)
        self.assertEqual(out.replaced_reason, "no_grounded_claim")
        self.assertNotIn("8888", out.reply)

    def test_k2_count_cannot_ground_an_injected_number(self):
        store = _store({"items": [{"stock": 2}]}, tool="get_inventory")
        out = verify_agent_answer(
            store=store,
            decision={
                "action": "final_answer", "draft_reply": "",
                "claims": [{"kind": "dato", "text": "El stock del 2404 es 8888",
                            "evidence_ids": ["e1"]}],
                "calculations": [_calc("e1.data.items", 8888)],
            },
        )
        self.assertEqual(out.calc_mismatch, 1)
        self.assertNotIn("8888", out.reply)

    def test_l_m04_shape_still_drops_the_remembered_number(self):
        store = _store({"codigo": "2404", "total_stock": 2,
                        "items": [{"bodega": "Bodega 1", "stock": 2}]}, tool="get_inventory")
        out = verify_agent_answer(
            store=store,
            decision={
                "action": "final_answer", "draft_reply": "",
                "claims": [
                    {"kind": "dato", "text": "El stock del 2404 es 2 unidades", "evidence_ids": ["e1"]},
                    {"kind": "inferencia", "text": "Bajo desde las 25 unidades", "evidence_ids": ["e1"]},
                ],
                "calculations": [],
            },
        )
        self.assertEqual(out.dropped_claims, 1)
        self.assertFalse(out.answer_replaced)
        self.assertIn("2 unidades", out.reply)
        self.assertNotIn("25", out.reply)

    def test_token_and_date_grounding_are_untouched(self):
        store = _store({"items": [{"fecha": "2026-07-31", "cantidad": 2}]},
                       tool="get_stock_movements")
        self.assertTrue(_grounded(store, "El 31 de julio de 2026 ingresaron 2 unidades."))
        self.assertFalse(_grounded(store, "hay 31 movimientos"))
        self.assertFalse(_grounded(store, "hay 12345 unidades"))

    def test_null_is_still_not_zero_when_zero_is_grounded_elsewhere(self):
        """docs_periodo=0 grounds the figure, so the finance guard is what must fire."""
        store = _store({"ventas_periodo": None, "docs_periodo": 0, "items": []},
                       tool="get_dashboard_kpis")
        out = verify_agent_answer(
            store=store,
            decision={
                "action": "final_answer", "draft_reply": "",
                "claims": [{"kind": "dato", "text": "Las ventas del periodo son 0",
                            "evidence_ids": ["e1"]}],
                "calculations": [],
            },
        )
        self.assertTrue(out.answer_replaced)
        self.assertEqual(out.replaced_reason, "finance_null_as_zero")

    def test_null_finance_is_not_even_groundable_on_its_own(self):
        """With nothing else grounding 0, the claim never survives to be published."""
        store = _store({"ventas_periodo": None, "items": []}, tool="get_dashboard_kpis")
        self.assertNotIn("0", grounded_numbers(store))
        out = verify_agent_answer(
            store=store,
            decision={
                "action": "final_answer", "draft_reply": "",
                "claims": [{"kind": "dato", "text": "Las ventas del periodo son 0",
                            "evidence_ids": ["e1"]}],
                "calculations": [],
            },
        )
        self.assertTrue(out.answer_replaced)
        self.assertNotIn("son 0", out.reply)

    def test_count_of_an_empty_collection_does_not_ground_a_null_finance_figure(self):
        """count(items)=0 must not become a licence to report null sales as 0."""
        store = _store({"ventas_periodo": None, "items": []}, tool="get_dashboard_kpis")
        out = verify_agent_answer(
            store=store,
            decision={
                "action": "final_answer", "draft_reply": "",
                "claims": [{"kind": "dato", "text": "Las ventas del periodo son 0",
                            "evidence_ids": ["e1"]}],
                "calculations": [_calc("e1.data.items", 0)],
            },
        )
        self.assertTrue(out.answer_replaced)
        self.assertEqual(out.replaced_reason, "finance_null_as_zero")


if __name__ == "__main__":
    unittest.main()
