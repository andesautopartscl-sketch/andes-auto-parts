"""FASE 8.2B — pack de evidencia, presupuesto y coherencia entre límites.

Dos clases de problema que comparten raíz: límites declarados que no coinciden
con el comportamiento efectivo, y un truncado que rompía la estructura en vez de
degradarla.
"""
from __future__ import annotations

import json
import unittest
from typing import Any

from app.assistant.orchestrator.agent_config import (
    MAX_AGENT_STEPS,
    MAX_COST_PER_TURN,
    MAX_EVIDENCE_PROMPT_CHARS,
    MAX_OUTPUT_TOKENS,
    MAX_SECONDS,
    MAX_TOOL_CALLS,
    MAX_TOOL_RESULT_CHARS,
    TOKEN_BUDGET_FALLBACK,
    budget_exceeded,
    decisions_affordable,
)
from app.assistant.orchestrator.evidence_store import EvidenceStore


def _bulky(store: EvidenceStore, n: int, rows: int = 40) -> EvidenceStore:
    for i in range(n):
        store.add_from_tool_result(
            tool="get_stock_movements", arguments={"codigo": f"C{i}"},
            result={"ok": True, "empty": False, "meta": {},
                    "data": {"items": [{"fecha": "2026-07-31", "cantidad": k, "obs": "X" * 70}
                                       for k in range(rows)]}},
        )
    return store


class EvidencePackTests(unittest.TestCase):
    """El pack se degrada por items, nunca parte la estructura.

    Antes se serializaba todo y se cortaba a MAX_EVIDENCE_PROMPT_CHARS, con lo que
    el modelo recibía JSON no parseable y ninguna señal de que faltara contenido.
    """

    def test_the_pack_is_always_valid_json(self):
        for n in (0, 1, 2, 5, 12):
            with self.subTest(items=n):
                pack = _bulky(EvidenceStore(), n).prompt_pack()
                parsed = json.loads(pack)  # no debe lanzar
                self.assertIsInstance(parsed, list)

    def test_the_pack_respects_the_cap(self):
        pack = _bulky(EvidenceStore(), 8).prompt_pack()
        self.assertLessEqual(len(pack), MAX_EVIDENCE_PROMPT_CHARS)

    def test_every_evidence_id_stays_visible_even_when_omitted(self):
        """Si un id desaparece del pack el modelo no puede citarlo, y la
        procedencia de 8.2 se apoya justamente en esos ids."""
        store = _bulky(EvidenceStore(), 6)
        parsed = json.loads(store.prompt_pack())
        self.assertEqual(
            {e["evidence_id"] for e in parsed},
            {i.evidence_id for i in store.items},
        )

    def test_omitted_items_are_marked_explicitly(self):
        store = _bulky(EvidenceStore(), 6)
        parsed = json.loads(store.prompt_pack())
        omitted = [e for e in parsed if e.get("omitted_from_prompt")]
        self.assertTrue(omitted, "debe señalar que se omitió contenido")
        for entry in omitted:
            self.assertNotIn("data", entry)

    def test_the_most_recent_evidence_is_the_one_kept_whole(self):
        store = _bulky(EvidenceStore(), 6)
        parsed = json.loads(store.prompt_pack())
        whole = [e for e in parsed if not e.get("omitted_from_prompt")]
        self.assertTrue(whole)
        self.assertEqual(whole[-1]["evidence_id"], store.items[-1].evidence_id)

    def test_a_small_turn_is_untouched(self):
        store = EvidenceStore()
        store.add_from_tool_result(tool="get_inventory", arguments={"codigo": "2404"},
                                   result={"ok": True, "empty": False,
                                           "data": {"total_stock": 2}, "meta": {}})
        parsed = json.loads(store.prompt_pack())
        self.assertEqual(len(parsed), 1)
        self.assertNotIn("omitted_from_prompt", parsed[0])
        self.assertEqual(parsed[0]["data"], {"total_stock": 2})

    def test_a_single_oversized_item_still_yields_valid_json(self):
        store = _bulky(EvidenceStore(), 1, rows=400)
        parsed = json.loads(store.prompt_pack())
        self.assertIsInstance(parsed, list)
        self.assertLessEqual(len(store.prompt_pack()), MAX_EVIDENCE_PROMPT_CHARS)


class BudgetCoherenceTests(unittest.TestCase):
    """Los límites declarados deben guardar relación con los efectivos.

    Medido sobre 114 turnos con LLM real: máximo 4 decisiones, máximo 2 invokes,
    latencia máxima 10.7s y cost_est None en 114/114 — es decir el techo de tokens
    es SIEMPRE el guard activo y MAX_COST_PER_TURN nunca se evalúa.
    """

    def test_the_step_budget_stays_within_what_tokens_can_fund(self):
        """8 era ficcion: el techo de tokens financia ~5 decisiones en el mejor
        caso. Si alguien sube MAX_AGENT_STEPS sin subir el presupuesto, o encoge
        el presupuesto sin bajar los pasos, este test lo dice."""
        worst, best = decisions_affordable()
        self.assertLessEqual(MAX_AGENT_STEPS, best + 1,
                             f"MAX_AGENT_STEPS={MAX_AGENT_STEPS} vs mejor caso {best}")
        self.assertGreaterEqual(worst, 1)

    def test_the_longest_designed_path_still_fits(self):
        """3 finales bloqueados + la tool que los resuelve + el final = 5."""
        from app.assistant.orchestrator.agent_config import MAX_BLOCKED_FINALS_NO_PROGRESS

        self.assertGreaterEqual(MAX_AGENT_STEPS, MAX_BLOCKED_FINALS_NO_PROGRESS + 2)

    def test_the_declared_budgets_cover_the_observed_envelope(self):
        """Envolvente observada con LLM real: 4 decisiones, 2 invokes, 10.7s."""
        self.assertGreaterEqual(MAX_AGENT_STEPS, 4)
        self.assertGreaterEqual(MAX_TOOL_CALLS, 2)
        self.assertGreaterEqual(MAX_SECONDS, 11.0)

    def test_the_token_ceiling_is_the_active_guard_when_cost_is_unpriced(self):
        self.assertTrue(budget_exceeded(prompt_tokens=TOKEN_BUDGET_FALLBACK,
                                        completion_tokens=0, cost_est=None))
        self.assertFalse(budget_exceeded(prompt_tokens=TOKEN_BUDGET_FALLBACK - 1,
                                         completion_tokens=0, cost_est=None))

    def test_the_cost_ceiling_still_applies_when_cost_is_priced(self):
        self.assertTrue(budget_exceeded(prompt_tokens=0, completion_tokens=0,
                                        cost_est=MAX_COST_PER_TURN))
        self.assertFalse(budget_exceeded(prompt_tokens=10 ** 9, completion_tokens=0,
                                         cost_est=MAX_COST_PER_TURN / 2))

    def test_the_evidence_cap_bounds_the_prompt_regardless_of_tool_output(self):
        """MAX_TOOL_RESULT_CHARS * MAX_TOOL_CALLS supera el cap del pack; el que
        manda sobre el tamaño del prompt es el cap del pack."""
        self.assertGreater(MAX_TOOL_RESULT_CHARS * MAX_TOOL_CALLS, MAX_EVIDENCE_PROMPT_CHARS)
        pack = _bulky(EvidenceStore(), MAX_TOOL_CALLS).prompt_pack()
        self.assertLessEqual(len(pack), MAX_EVIDENCE_PROMPT_CHARS)

    def test_output_tokens_are_bounded(self):
        self.assertGreater(MAX_OUTPUT_TOKENS, 0)
        self.assertLess(MAX_OUTPUT_TOKENS, TOKEN_BUDGET_FALLBACK)


class BudgetSnapshotTests(unittest.TestCase):
    def test_the_snapshot_exposes_the_active_guard(self):
        from app.assistant.orchestrator.agent_config import budget_snapshot

        snap = budget_snapshot()
        for key in ("max_agent_steps", "max_tool_calls", "token_budget_fallback",
                    "cost_priced", "active_guard", "decisions_affordable_worst",
                    "decisions_affordable_best"):
            self.assertIn(key, snap, key)
        self.assertIn(snap["active_guard"], {"token_budget", "cost_per_turn"})


if __name__ == "__main__":
    unittest.main()
