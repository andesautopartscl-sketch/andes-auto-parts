"""FASE 10.1 — que herramientas ve el modelo en ESTE turno.

EL PROBLEMA, MEDIDO

El system prompt se reenvia ENTERO en cada decision. T09 toma 5 decisiones, asi
que su prompt son ~10 825 tokens de los cuales ~6 015 son el mismo system cinco
veces. Los contratos de herramientas son el 27 % de ese system (1 297 ch), y en
Fase 9 se midio que ORDERS costaba +37 tokens en TODOS los turnos para una
capacidad que 0 de 76 casos eligieron.

Con 12 herramientas el catalogo ya pesa. Con 30 se come el presupuesto.

LA REGLA

    CORE      siempre visible. Cubre la pregunta corriente.
    OPTIONAL  entra cuando un detector deterministico la reclama.
    resto     no se muestra.

Y la salvaguarda que hace esto seguro: **sin senal no se oculta nada**. Si
ningun detector dispara, el turno ve el catalogo completo. Un router que
adivina mal en silencio es peor que no tener router.

POR QUE NO SE REUSA `extract_requirement_types` A SECAS

Se reusa —es el detector que ya existe y ya esta probado— pero NO basta. Seis de
los doce tipos declarados en REQUIREMENT_TYPES no tienen senal en `_AUTO_SIGNALS`
(`ingresos`, `customer`, `catalog_search`, `product_detail`, `sales`,
`customer_orders`), y medido contra las 76 corridas reales eso producia UN
capability_miss: C04 ("Compara ingresos y stock actual del 2404") se quedaba sin
`get_ingresos`.

La tabla de abajo cubre ese hueco **solo para visibilidad**, y esa asimetria es
deliberada: hacer visible una herramienta de mas cuesta tokens; exigir cobertura
de una que el turno no toca rompe el turno. Por eso este router es MAS permisivo
que `goal_coverage` y no toca `_AUTO_SIGNALS`, que gobierna la cobertura.

MEDIDO ANTES DE ESCRIBIR ESTO (76 casos, corridas reales de Fase 9)

    capability_miss      0
    visibles de media    6,0 de 12
    max_total_tokens     11 764 -> 10 964 proyectado
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from app.assistant.orchestrator.goal_coverage import (
    covering_tools,
    extract_requirement_types,
)

# Las que cubren la pregunta corriente. Se midieron tres tamanos contra los 76
# casos: con 2 herramientas aparecian 3 capability_miss, con 3 uno, con estas 4
# ninguno. No es una eleccion de gusto.
CORE_TOOLS: frozenset[str] = frozenset({
    "get_inventory",
    "get_product",
    "search_catalog",
    "get_stock_movements",
})

# Senales SOLO de visibilidad para los tipos que `_AUTO_SIGNALS` no detecta.
# Cerradas y cortas, como el resto de detectores del sistema.
_VISIBILITY_SIGNALS: dict[str, tuple[str, ...]] = {
    "ingresos": ("ingreso", "ingresos", "recepcion", "recepciones",
                 "recibido", "recibidos"),
    "customer": ("cliente", "clientes"),
    "sales": ("venta", "ventas", "vendido", "vendidas", "vendio",
              "facturacion", "facturado"),
}

REASON_NO_SIGNAL = "sin_senal"
REASON_CORE = "core"


def _fold(text: str) -> str:
    norm = unicodedata.normalize("NFD", str(text or "").lower())
    return "".join(c for c in norm if unicodedata.category(c) != "Mn")


def _visibility_types(folded: str) -> set[str]:
    return {
        rtype for rtype, frases in _VISIBILITY_SIGNALS.items()
        if any(re.search(rf"\b{re.escape(p)}\b", folded) for p in frases)
    }


@dataclass(frozen=True)
class CapabilityDecision:
    """Que se mostro, que se oculto, y por que. Nombres, nunca contenido."""

    selected: frozenset[str]
    hidden: frozenset[str]
    reasons: dict[str, str]
    requirement_types: tuple[str, ...]
    fell_back: bool

    def observation(self) -> dict[str, Any]:
        return {
            "capabilities_selected": sorted(self.selected),
            "capabilities_hidden": sorted(self.hidden),
            "capability_reasons": dict(self.reasons),
            "capability_requirements": list(self.requirement_types),
            "capability_fallback": bool(self.fell_back),
        }


def select_capabilities(
    message: str,
    *,
    available: set[str] | frozenset[str],
) -> CapabilityDecision:
    """Las herramientas que este turno puede ver.

    ``available`` es lo que las banderas y permisos ya autorizaron —tipicamente
    ``model_facing_tools()``—. Este router SOLO PUEDE QUITAR de esa lista: nunca
    anade una herramienta que la capa de permisos no haya autorizado, y por eso
    no puede reabrir `get_orders` mientras su bandera este apagada.
    """
    disponibles = frozenset(available or ())
    folded = _fold(message)

    extraction, detected = extract_requirement_types(message)
    tipos = set(detected) | _visibility_types(folded)

    if not tipos:
        # Sin senal no se oculta nada: el turno ve lo que veria hoy.
        return CapabilityDecision(
            selected=disponibles,
            hidden=frozenset(),
            reasons={t: REASON_NO_SIGNAL for t in sorted(disponibles)},
            requirement_types=(),
            fell_back=True,
        )

    razones: dict[str, str] = {}
    seleccion: set[str] = set()
    for tool in CORE_TOOLS & disponibles:
        seleccion.add(tool)
        razones[tool] = REASON_CORE
    for rtype in sorted(tipos):
        for tool in covering_tools(rtype) & disponibles:
            seleccion.add(tool)
            # El requisito explica mejor que "core" por que esta ahi.
            razones[tool] = rtype

    return CapabilityDecision(
        selected=frozenset(seleccion),
        hidden=disponibles - seleccion,
        reasons=razones,
        requirement_types=tuple(sorted(tipos)),
        fell_back=False,
    )
