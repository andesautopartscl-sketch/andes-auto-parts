"""Closed tool catalog for the orchestrator. Must match Gateway 0.11.0 allowlist."""
from __future__ import annotations

ALLOWED_TOOLS = frozenset(
    {
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
    }
)

# All current tools are READ-ONLY; any write intent is rejected before invoke.
WRITE_TOOLS = frozenset()

MAX_STEPS = 3
MAX_INVOKES = 3
MAX_REPLANS = 1

CONFIDENTIAL_TOOLS = frozenset(
    {
        "get_ingresos",
        "get_purchase_orders",
        "get_customer",
        "get_supplier",
        "get_dashboard_kpis",
    }
)

# Binding paths that may be resolved from prior step evidence.
ALLOWED_BINDING_PATHS = frozenset(
    {
        "data.items.0.codigo",
        "data.items[0].codigo",
        "data.codigo",
        "data.top_productos.0.codigo",
        "data.top_productos[0].codigo",
    }
)
