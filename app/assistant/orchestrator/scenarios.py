"""Deterministic FakePlanner scenarios for FASE 2 etapa 1 (no LLM)."""
from __future__ import annotations

from typing import Any

# Scenario keys used by FakePlanner and tests
SCENARIO_ONE_TOOL = "one_tool"
SCENARIO_TWO_TOOLS = "two_tools"
SCENARIO_THREE_TOOLS = "three_tools"
SCENARIO_AMBIGUOUS = "ambiguous"
SCENARIO_EMPTY = "empty_results"
SCENARIO_NO_PERMISSION = "no_permission"
SCENARIO_KPI_NO_FINANCE = "kpi_no_finance"
SCENARIO_WRITE = "write_reject"
SCENARIO_OUT_OF_DOMAIN = "out_of_domain"
SCENARIO_PII = "pii_unavailable"


def scenario_fixtures() -> dict[str, dict[str, Any]]:
    return {
        SCENARIO_ONE_TOOL: {
            "plan_id": "fix-one-tool",
            "scenario": SCENARIO_ONE_TOOL,
            "user_intent": "Buscar filtro de aceite en catálogo",
            "answer_style": "operational",
            "steps": [
                {
                    "step": 1,
                    "tool": "search_catalog",
                    "arguments": {"q": "filtro aceite", "limit": 5},
                    "reason": "búsqueda simple",
                }
            ],
        },
        SCENARIO_TWO_TOOLS: {
            "plan_id": "fix-two-tools",
            "scenario": SCENARIO_TWO_TOOLS,
            "user_intent": "Buscar producto y consultar stock",
            "answer_style": "operational",
            "steps": [
                {
                    "step": 1,
                    "tool": "search_catalog",
                    "arguments": {"q": "2404", "limit": 5},
                    "reason": "resolver código",
                },
                {
                    "step": 2,
                    "tool": "get_inventory",
                    "arguments": {"codigo": "$steps.1.data.items.0.codigo"},
                    "depends_on": [1],
                    "reason": "stock del primer match",
                },
            ],
        },
        SCENARIO_THREE_TOOLS: {
            "plan_id": "fix-three-tools",
            "scenario": SCENARIO_THREE_TOOLS,
            "user_intent": "Buscar, stock y movimientos",
            "answer_style": "operational",
            "steps": [
                {
                    "step": 1,
                    "tool": "search_catalog",
                    "arguments": {"q": "filtro diesel", "limit": 5},
                    "reason": "encontrar código",
                },
                {
                    "step": 2,
                    "tool": "get_inventory",
                    "arguments": {"codigo": "$steps.1.data.items.0.codigo"},
                    "depends_on": [1],
                    "reason": "stock",
                },
                {
                    "step": 3,
                    "tool": "get_stock_movements",
                    "arguments": {"codigo": "$steps.1.data.items.0.codigo", "limit": 5},
                    "depends_on": [1],
                    "reason": "movimientos",
                },
            ],
        },
        SCENARIO_AMBIGUOUS: {
            "plan_id": "fix-ambiguous",
            "scenario": SCENARIO_AMBIGUOUS,
            "user_intent": "Consulta ambigua de cliente",
            "needs_clarification": True,
            "reject_message": "¿Puedes indicar el nombre, RUT o id del cliente?",
        },
        SCENARIO_EMPTY: {
            "plan_id": "fix-empty",
            "scenario": SCENARIO_EMPTY,
            "user_intent": "Búsqueda sin resultados esperados",
            "answer_style": "operational",
            "steps": [
                {
                    "step": 1,
                    "tool": "search_catalog",
                    "arguments": {"q": "xyzzy-no-existe-999", "limit": 5},
                    "reason": "probar vacío",
                }
            ],
        },
        SCENARIO_NO_PERMISSION: {
            "plan_id": "fix-no-permission",
            "scenario": SCENARIO_NO_PERMISSION,
            "user_intent": "Consultar proveedor sin permiso",
            "answer_style": "confidential",
            "steps": [
                {
                    "step": 1,
                    "tool": "get_supplier",
                    "arguments": {"q": "ACME", "limit": 5},
                    "reason": "directorio proveedores",
                }
            ],
        },
        SCENARIO_KPI_NO_FINANCE: {
            "plan_id": "fix-kpi-no-finance",
            "scenario": SCENARIO_KPI_NO_FINANCE,
            "user_intent": "Ventas de la semana sin ver_finanzas",
            "answer_style": "financial",
            "steps": [
                {
                    "step": 1,
                    "tool": "get_dashboard_kpis",
                    "arguments": {"periodo": "7d"},
                    "reason": "KPIs 7d",
                }
            ],
        },
        SCENARIO_WRITE: {
            "plan_id": "fix-write",
            "scenario": SCENARIO_WRITE,
            "reject": True,
            "reject_code": "write_not_allowed",
            "reject_message": "Solo puedo consultar información; no puedo crear, anular ni modificar datos.",
            "user_intent": "Solicitud de escritura",
        },
        SCENARIO_OUT_OF_DOMAIN: {
            "plan_id": "fix-ood",
            "scenario": SCENARIO_OUT_OF_DOMAIN,
            "reject": True,
            "reject_code": "out_of_scope",
            "reject_message": "Esa consulta está fuera del alcance del asistente de Andes Auto Parts.",
            "user_intent": "Fuera de dominio",
        },
        SCENARIO_PII: {
            "plan_id": "fix-pii",
            "scenario": SCENARIO_PII,
            "user_intent": "Pedir email/teléfono de cliente",
            "answer_style": "confidential",
            "steps": [
                {
                    "step": 1,
                    "tool": "get_customer",
                    "arguments": {"q": "Albert", "limit": 5},
                    "reason": "directorio sin PII de contacto",
                }
            ],
        },
    }


def detect_scenario(message: str) -> str:
    text = (message or "").strip().lower()

    # Explicit scenario tags for tests: [scenario:one_tool]
    if "[scenario:" in text:
        start = text.index("[scenario:") + len("[scenario:")
        end = text.find("]", start)
        if end > start:
            return text[start:end].strip()

    # WRITE before other matches
    if any(w in text for w in ("crea una oc", "crear oc", "anula", "descuenta stock", "elimina", "borra factura")):
        return SCENARIO_WRITE
    if "clima" in text or "weather" in text or "fuera de dominio" in text:
        return SCENARIO_OUT_OF_DOMAIN
    if "email" in text or "teléfono" in text or "telefono" in text or "dirección" in text or "direccion" in text:
        return SCENARIO_PII
    if "cómo va eso del cliente" in text or "como va eso del cliente" in text or text.strip() in {"el cliente", "cliente?"}:
        return SCENARIO_AMBIGUOUS
    if "xyzzy-no-existe-999" in text or "sin resultados" in text:
        return SCENARIO_EMPTY
    if "sin permiso" in text or "proveedor acme" in text:
        return SCENARIO_NO_PERMISSION
    if "sin ver_finanzas" in text or ("vendimos" in text and "semana" in text) or "kpis 7d" in text:
        return SCENARIO_KPI_NO_FINANCE
    if "movimientos" in text and ("stock" in text or "busca" in text or "filtro diesel" in text):
        return SCENARIO_THREE_TOOLS
    if ("stock" in text and ("busca" in text or "2404" in text)) or "dos tools" in text:
        return SCENARIO_TWO_TOOLS
    if "busca filtro" in text or "buscar filtro" in text or "filtro de aceite" in text or "filtro aceite" in text:
        return SCENARIO_ONE_TOOL

    # Default: treat as ambiguous clarification rather than free invent
    return SCENARIO_AMBIGUOUS
