"""Shared product-E2E harness evaluation criteria (cases 9 / 16).

Evaluation-only. Does not change orchestrator, Gateway, or tool runtime.
"""
from __future__ import annotations

from typing import Any


def money_null_rendered_as_zero(entry: dict[str, Any]) -> bool:
    """Flag only when evidence had null ventas_periodo AND reply shows sales as 0.

    A real zero (ventas_periodo == 0.0) with finance permission must NOT fail.
    """
    reply_l = str(entry.get("reply") or "").lower()
    looks_zero = (
        "ventas del período: 0" in reply_l
        or "ventas del periodo: 0" in reply_l
    )
    if not looks_zero:
        return False
    for g in entry.get("gateway_invokes") or []:
        mf = g.get("money_fields") or {}
        if "ventas_periodo" in mf and mf.get("ventas_periodo") is None:
            return True
    return False


def controlled_gateway_failure(entry: dict[str, Any]) -> bool:
    """Accept controlled degradation even if orchestrator returns ok=true.

    Fabricating catalog content without a successful gateway invoke must fail.
    """
    reply = str(entry.get("reply") or "")
    reply_l = reply.lower()
    gw = entry.get("gateway_invokes") or []
    if any(g.get("ok") for g in gw):
        return False
    if "FILTRO DIESEL" in reply and not any(g.get("ok") for g in gw):
        return False
    return bool(
        entry.get("error_code")
        or not entry.get("ok")
        or "no disponible" in reply_l
        or "no está disponible" in reply_l
        or "agente" in reply_l
        or "servicio" in reply_l
    )
