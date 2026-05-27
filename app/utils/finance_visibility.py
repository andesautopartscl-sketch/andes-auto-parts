"""Control de visibilidad de datos financieros (costos, márgenes, utilidad)."""
from __future__ import annotations

from app.utils.permissions import has_permission


def user_can_view_finanzas(username: str | None, role_name: str | None) -> bool:
    return has_permission(username, role_name, "ver_finanzas")


def redact_utilidad_margen_row(row: dict) -> dict:
    out = dict(row)
    out["costo_unit_ref"] = None
    out["utilidad_unit"] = None
    out["utilidad_total"] = None
    out["margen_pct"] = None
    return out


def redact_compra_historial_row(row: dict) -> dict:
    out = dict(row)
    out["costo_unitario"] = None
    out["total_neto"] = None
    out["precio_venta_neto"] = None
    return out


def redact_dashboard_sales(data: dict) -> dict:
    """Anula montos de ventas en payload del dashboard (conserva conteos no monetarios)."""
    out = dict(data)
    out["ventas_hoy"] = None
    out["ventas_mes"] = None
    out["chart_data"] = [
        {**point, "total": None} for point in (out.get("chart_data") or [])
    ]
    out["top_clientes"] = [
        {**row, "total": None} for row in (out.get("top_clientes") or [])
    ]
    out["top_productos"] = [
        {**row, "venta": None} for row in (out.get("top_productos") or [])
    ]
    return out
