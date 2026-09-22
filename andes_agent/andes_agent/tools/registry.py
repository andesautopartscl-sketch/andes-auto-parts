"""Central allowlist of READ-only tools. Unknown names never execute."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from andes_agent.tools import (
    check_stock,
    get_customer,
    get_dashboard_kpis,
    get_equivalences,
    get_orders,
    get_ingresos,
    get_inventory,
    get_product,
    get_purchase_orders,
    get_sales,
    get_stock_movements,
    get_supplier,
    search_catalog,
)

Handler = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    write: bool
    handler: Handler
    description: str


def _spec(module, description: str) -> ToolSpec:
    if getattr(module, "WRITE", True):
        raise RuntimeError(f"{module.NAME} must be read-only")
    return ToolSpec(name=module.NAME, write=False, handler=module.handle, description=description)


ALLOWED_TOOLS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        _spec(search_catalog, "Search products in the catalog"),
        _spec(get_product, "Get one product by code"),
        _spec(get_inventory, "Get inventory rows for a product"),
        _spec(check_stock, "Check availability for cart items (READ-ONLY)"),
        _spec(get_stock_movements, "Read stock movements"),
        _spec(get_ingresos, "Read warehouse receipts"),
        _spec(get_purchase_orders, "Read supplier purchase orders"),
        _spec(get_sales, "Read aggregated sales (net of credit notes)"),
        _spec(get_orders, "Read customer orders (lines, states, volume)"),
        _spec(get_equivalences, "Cross-reference OEM / internal / alternative codes"),
        _spec(get_customer, "Read customer directory (ventas_clientes)"),
        _spec(get_supplier, "Read supplier directory (ventas_proveedores)"),
        _spec(get_dashboard_kpis, "Read dashboard KPI snapshot"),
    )
}


def get_tool(name: str) -> ToolSpec | None:
    return ALLOWED_TOOLS.get(name)


def list_tools() -> list[ToolSpec]:
    return list(ALLOWED_TOOLS.values())
