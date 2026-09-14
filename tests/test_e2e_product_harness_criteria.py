"""Harness criteria for product E2E — fixes false FAIL on cases 9 and 16.

Does not change orchestrator/Gateway/tool runtime behavior.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from e2e_product_harness_criteria import (  # noqa: E402
    controlled_gateway_failure,
    money_null_rendered_as_zero,
)


class HarnessCriteriaTests(unittest.TestCase):
    def test_case9_real_zero_with_finance_is_not_null_to_zero(self):
        entry = {
            "reply": "Ventas del período: 0.0.\nDocumentos en período: 0.",
            "gateway_invokes": [{"ok": True, "money_fields": {"ventas_periodo": 0.0}}],
        }
        self.assertFalse(money_null_rendered_as_zero(entry))

    def test_null_finance_rendered_as_zero_is_flagged(self):
        entry = {
            "reply": "Ventas del período: 0",
            "gateway_invokes": [{"ok": True, "money_fields": {"ventas_periodo": None}}],
        }
        self.assertTrue(money_null_rendered_as_zero(entry))

    def test_case16_controlled_unavailable_message_passes(self):
        entry = {
            "ok": True,
            "error_code": None,
            "reply": "El servicio no está disponible al consultar `search_catalog`.",
            "gateway_invokes": [
                {"ok": False, "http_status": 503, "error_code": "agent_unavailable"}
            ],
        }
        self.assertTrue(controlled_gateway_failure(entry))

    def test_case16_fabricated_catalog_without_gateway_fails(self):
        entry = {
            "ok": True,
            "reply": "Catálogo: 1 resultado(s).\n- 2404: FILTRO DIESEL",
            "gateway_invokes": [{"ok": False}],
        }
        self.assertFalse(controlled_gateway_failure(entry))


if __name__ == "__main__":
    unittest.main()
