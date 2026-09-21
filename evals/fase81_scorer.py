"""FASE 8.1D scorer — explicit tool families + separated score axes.

Not a release gate. No LLM. No secrets.
"""
from __future__ import annotations

import re
from typing import Any

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
        "get_sales",
        "get_equivalences",
        "get_dashboard_kpis",
    }
)

# Explicit families only — never inferred from free text / stemming.
TOOL_FAMILIES: dict[str, frozenset[str]] = {
    "current_inventory": frozenset({"get_inventory", "check_stock"}),
    "stock_movements": frozenset({"get_stock_movements"}),
    "catalog_search": frozenset({"search_catalog"}),
    "product_detail": frozenset({"get_product"}),
    "ingresos": frozenset({"get_ingresos"}),
    "purchase_orders": frozenset({"get_purchase_orders"}),
    "dashboard_kpis": frozenset({"get_dashboard_kpis"}),
    "customer": frozenset({"get_customer"}),
    "supplier": frozenset({"get_supplier"}),
    "sales": frozenset({"get_sales"}),
    "equivalences": frozenset({"get_equivalences"}),
}

WRITE_PREFIXES = ("create_", "write_", "delete_", "update_", "insert_", "remove_")
FASE5_SCENARIOS = frozenset(
    {
        "context_reuse",
        "context_ambiguous",
        "context_expired",
        "context_empty",
        "ambiguous",
    }
)
_NUM = re.compile(r"\b\d+(?:[.,]\d+)?\b")


def _tools(run: dict[str, Any]) -> list[str]:
    return [str(t) for t in (run.get("tools_used") or []) if t]


def _policy(gold: dict[str, Any]) -> str:
    raw = gold.get("tool_count_policy") or gold.get("tool_count") or "exact"
    raw = str(raw).strip().lower()
    if raw in {"min", "minimum"}:
        return "minimum"
    if raw in {"family"}:
        return "family"
    if raw in {"optional_extra"}:
        return "optional_extra"
    return "exact"


def _expected_tools(gold: dict[str, Any]) -> list[str]:
    return [str(t) for t in (gold.get("expected_tools") or gold.get("tools_expected") or []) if t]


def _required_goals(gold: dict[str, Any]) -> list[str]:
    return [
        str(t)
        for t in (gold.get("required_goal_types") or gold.get("goal_types") or [])
        if t
    ]


def _families(gold: dict[str, Any]) -> dict[str, frozenset[str]]:
    raw = gold.get("acceptable_tool_families")
    if isinstance(raw, dict) and raw:
        out: dict[str, frozenset[str]] = {}
        for key, tools in raw.items():
            if isinstance(tools, (list, tuple, set, frozenset)):
                out[str(key)] = frozenset(str(t) for t in tools if t)
        return out
    # Default: map required goals to closed TOOL_FAMILIES.
    return {g: TOOL_FAMILIES[g] for g in _required_goals(gold) if g in TOOL_FAMILIES}


def _fase5_ok(gold: dict[str, Any], run: dict[str, Any]) -> bool:
    if not gold.get("allow_fase5_shortcircuit"):
        return False
    scenario = str(run.get("scenario") or "")
    if scenario in FASE5_SCENARIOS or scenario.startswith("context_"):
        return True
    if run.get("needs_clarification"):
        return True
    return False


def _family_satisfied(gold: dict[str, Any], tools: list[str]) -> bool:
    families = _families(gold)
    if not families:
        return False
    used = set(tools)
    for _name, members in families.items():
        if not (used & members):
            return False
    # Reject tools outside declared families + expected extras.
    allowed = set()
    for members in families.values():
        allowed |= set(members)
    allowed |= set(_expected_tools(gold))
    extras = used - allowed
    return not extras


def _tool_selection_ok(gold: dict[str, Any], run: dict[str, Any], tools: list[str]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    expected = _expected_tools(gold)
    policy = _policy(gold)
    clarify = bool(run.get("needs_clarification"))
    reject = bool(gold.get("expect_reject"))
    fallback = bool(run.get("fallback_used"))

    if gold.get("expect_clarify") or gold.get("acceptable_outcomes"):
        outcomes = set(gold.get("acceptable_outcomes") or [])
        if gold.get("expect_clarify"):
            outcomes |= {"clarify", "fase5_clarify", "fase5_reuse", "no_tools_safe"}
        reuse = str(run.get("scenario") or "") == "context_reuse"
        amb = str(run.get("scenario") or "") in FASE5_SCENARIOS
        if "clarify" in outcomes and clarify:
            return True, reasons
        if "fase5_reuse" in outcomes and reuse:
            return True, reasons
        if "fase5_clarify" in outcomes and (clarify or amb):
            return True, reasons
        if "no_tools_safe" in outcomes and not tools and not fallback:
            return True, reasons
        if "agent_tools" in outcomes and tools:
            # fall through to policy check
            pass
        elif outcomes and not tools and (clarify or reuse or amb):
            return True, reasons

    if _fase5_ok(gold, run) and not tools:
        return True, reasons

    if reject:
        ok = not tools
        if not ok:
            reasons.append("expected_reject_zero_tools")
        return ok, reasons

    if policy == "family":
        ok = _family_satisfied(gold, tools)
        if not ok and expected:
            # Also accept exact/minimum expected as compatible path.
            ok = set(expected).issubset(set(tools)) or (
                len(expected) == 1 and bool(set(tools) & set(_families(gold).get(_required_goals(gold)[0], frozenset()) if _required_goals(gold) else frozenset()))
            )
        if not ok:
            reasons.append(f"family_unsatisfied tools={tools}")
        return ok, reasons

    if policy == "minimum":
        ok = set(expected).issubset(set(tools))
        if not ok:
            # Sibling family: every required goal has a covering tool used.
            if _required_goals(gold) and _family_satisfied(gold, tools):
                return True, reasons
            reasons.append(f"tools_got={tools} min={expected}")
        return ok, reasons

    if policy == "optional_extra":
        ok = set(expected).issubset(set(tools))
        if not ok and _required_goals(gold) and _family_satisfied(gold, tools):
            return True, reasons
        if not ok:
            reasons.append(f"tools_got={tools} required={expected}")
        return ok, reasons

    # exact
    ok = set(tools) == set(expected)
    if gold.get("expect_fallback") and fallback and not tools:
        ok = True
    if not ok:
        reasons.append(f"tools_got={tools} expected={expected}")
    return ok, reasons


def _goal_coverage_ok(gold: dict[str, Any], run: dict[str, Any], tools: list[str]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    goals = _required_goals(gold)
    if not goals:
        return True, reasons
    if gold.get("empty_not_found_ok") and tools:
        # Correct tool ran; uncovered empty is acceptable.
        families = _families(gold)
        used = set(tools)
        for g in goals:
            members = families.get(g) or TOOL_FAMILIES.get(g) or frozenset()
            if members & used:
                continue
            reasons.append(f"goal_{g}_tool_missing")
            return False, reasons
        return True, reasons

    if _fase5_ok(gold, run) and not tools:
        return True, reasons

    snap = run.get("goal_coverage") if isinstance(run.get("goal_coverage"), dict) else {}
    status = {r.get("type"): r.get("status") for r in (snap.get("requirements") or [])}
    for rtype in goals:
        st = status.get(rtype)
        if st in {"covered", "impossible"}:
            continue
        # Family coverage via tools even if snapshot missing (Resolver/no loop).
        members = (_families(gold).get(rtype) or TOOL_FAMILIES.get(rtype) or frozenset())
        if members & set(tools):
            continue
        if gold.get("expect_fallback") and run.get("fallback_used"):
            continue
        reasons.append(f"goal_{rtype}={st}")
        return False, reasons
    return True, reasons


def _response_ok(gold: dict[str, Any], run: dict[str, Any], tools: list[str]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    reply = str(run.get("reply") or "")
    fallback = bool(run.get("fallback_used"))
    if gold.get("expected_data_vs_inference") and tools and not fallback:
        low = reply.lower()
        if "dato" not in low and "datos" not in low:
            reasons.append("missing_datos")
            return False, reasons
    # 8.3 retiro de aqui el check `expect_figures`. Era un parche por caso —habia
    # que marcar a mano cada gold— y por tanto no podia detectar el defecto en
    # ningun caso que nadie hubiera marcado ya. La senal general vive ahora en
    # answer_sufficiency y se reporta en su propio eje, medida contra la
    # evidencia que el agente recupero de verdad.
    return True, reasons


def _guardrail_events(run: dict[str, Any]) -> dict[str, int]:
    """Times the verifier had to intervene. Reported, never scored.

    An intervention is the guardrail WORKING: the model proposed something it
    could not support and the verifier removed it. Counting that as a case
    failure penalizes exactly the behaviour the system is built to have.
    """
    vb = run.get("verifier_breakdown") or {}
    return {
        "dropped_claims": int(vb.get("dropped_claims") or 0),
        "dropped_draft": int(vb.get("dropped_draft") or 0),
        "calc_unresolved": int(vb.get("calc_unresolved") or 0),
        "calc_mismatch": int(vb.get("calc_mismatch") or 0),
        "calc_error": int(vb.get("calc_error") or 0),
        "verifier_failures": int(run.get("verifier_failures") or 0),
    }


def _analysis(gold: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    """FASE 8.5 — uso real de la escalera analitica. Reportado, NO puntuado.

    La corrida posterior a 8.5 dio CERO usos, y no medía al modelo: el structured
    output schema fijaba kind a dato|inferencia, asi que emitir un peldano nuevo
    era imposible. Esta senal existe para que la proxima corrida distinga las dos
    cosas — "no sabe usarla" y "no puede usarla"— en vez de confundirlas.
    """
    reply = str(run.get("reply") or "")
    present = [rung for rung, label in (
        ("calculo", "CÁLCULOS:"), ("supuesto", "SUPUESTOS:"),
        ("proyeccion", "PROYECCIÓN:"), ("recomendacion", "RECOMENDACIÓN:"))
        if label in reply]
    vb = run.get("verifier_breakdown") or {}
    return {
        "expected": bool(gold.get("analysis_expected")),
        "rungs_used": present,
        "used_ladder": bool(present),
        # Una proyeccion descartada por falta de supuesto es el guardrail
        # funcionando, no un fallo del modelo: hay que poder distinguirlo.
        "dropped_claims": int(vb.get("dropped_claims") or 0),
    }


def _analysis_summary(scores: list[dict[str, Any]],
                      *, analysis_enabled: bool | None = None) -> dict[str, Any]:
    """FASE 8.x — `state` decia el estado del flag sin leerlo nunca.

    Era un literal fijo emitido siempre que `used` estuviera vacio, asi que la
    tercera A/B reporto "ANDES_ASSISTANT_ANALYSIS_ENABLED=0" en los DOS brazos,
    incluido el que corrio con el flag encendido. Cero usos de la escalera con
    la capacidad encendida es justo el hallazgo interesante — el modelo no la
    uso ni una vez en 76 casos — y la etiqueta lo tapaba diciendo que la
    capacidad estaba apagada. Ahora el estado lo aporta quien lo sabe: el brazo.
    """
    rows = [s.get("analysis") or {} for s in scores]
    expected = [s for s in scores if (s.get("analysis") or {}).get("expected")]
    used = [s["id"] for s in scores if (s.get("analysis") or {}).get("used_ladder")]
    # FASE 8.x — "no la uso" y "no se midio" no son lo mismo. En la corrida
    # an1-pv0-pr0 del 2026-09-20 los TRES casos analiticos (A01, A02, A03)
    # volvieron con llm_unavailable, y el resumen seguia afirmando que el modelo
    # no habia emitido ningun peldano. No emitio nada porque no se le pregunto.
    sin_medir = sorted(str(s.get("id")) for s in expected if s.get("unmeasured"))
    rungs: dict[str, int] = {}
    for row in rows:
        for rung in row.get("rungs_used") or []:
            rungs[rung] = rungs.get(rung, 0) + 1
    if sin_medir and len(sin_medir) == len(expected):
        state = (f"SIN MEDIR: los {len(expected)} casos analiticos volvieron sin "
                 f"respuesta del modelo ({', '.join(sin_medir)}). Cero usos aqui "
                 f"no dice nada del modelo.")
    elif analysis_enabled is None:
        state = ("estado del flag no informado por el brazo; "
                 f"escalera usada en {len(used)} caso(s)")
    elif not analysis_enabled:
        state = "no habilitada (el brazo corrio con analysis_enabled=False)"
    elif used:
        state = "habilitada y usada — reportado, no puntua"
    else:
        state = ("habilitada y NO usada: el modelo no emitio ningun peldano "
                 f"en {len(expected) - len(sin_medir)} caso(s) analitico(s) medido(s)")
        if sin_medir:
            state += f"; ademas {len(sin_medir)} sin medir ({', '.join(sin_medir)})"
    return {
        "cases_expecting_analysis": len(expected),
        "cases_using_ladder": len(used),
        "ladder_ids": sorted(used),
        "rung_usage": rungs,
        "analysis_enabled": analysis_enabled,
        "analytic_cases_unmeasured": sin_medir,
        "state": state,
    }


def _sufficiency(run: dict[str, Any]) -> dict[str, Any]:
    """FASE 8.3 — utilidad de la respuesta. Reportada, NO puntuada todavia.

    Mismo camino que la procedencia en 8.2: observar, medir sobre una corrida
    real, y solo entonces decidir si puntua. La metrica tiene falsos negativos
    conocidos —no separa a que requisito pertenece cada cifra cuando la respuesta
    mezcla dos temas— y cero falsos positivos sobre las respuestas etiquetadas a
    mano. Con ese perfil no hace dano puntuar, pero catorce ejemplos no son una
    validacion: puntuar ahora seria ajustar a la muestra.
    """
    suf = run.get("sufficiency")
    if not isinstance(suf, dict) or not suf:
        return {"measured": False, "verdict": "unknown", "row_utilization": None,
                "insufficient_types": []}
    return {
        "measured": True,
        "verdict": str(suf.get("verdict") or "unknown"),
        "row_utilization": suf.get("row_utilization"),
        "insufficient_types": list(suf.get("insufficient_types") or []),
    }


def _sufficiency_summary(scores: list[dict[str, Any]]) -> dict[str, Any]:
    measured = [s for s in scores if (s.get("sufficiency") or {}).get("measured")]
    if not measured:
        return {"measured_cases": 0, "state": "no medido (corrida anterior a 8.3)"}
    verdicts: dict[str, int] = {}
    for s in measured:
        key = str((s.get("sufficiency") or {}).get("verdict"))
        verdicts[key] = verdicts.get(key, 0) + 1
    ratios = [r for r in ((s.get("sufficiency") or {}).get("row_utilization")
                          for s in measured) if isinstance(r, (int, float))]
    types: dict[str, int] = {}
    for s in measured:
        for t in (s.get("sufficiency") or {}).get("insufficient_types") or []:
            types[t] = types.get(t, 0) + 1
    judged = [s for s in measured
              if (s.get("sufficiency") or {}).get("verdict") in ("sufficient", "insufficient")]
    insufficient = [s["id"] for s in measured
                    if (s.get("sufficiency") or {}).get("verdict") == "insufficient"]
    return {
        "measured_cases": len(measured),
        "judged_cases": len(judged),
        "verdicts": verdicts,
        "insufficient_ids": sorted(insufficient),
        "insufficient_by_type": types,
        "avg_row_utilization": round(sum(ratios) / len(ratios), 4) if ratios else None,
        "state": "reportado, no puntua",
    }


def _enforcement_ready(scores: list[dict[str, Any]]) -> str:
    """Tres estados, no un booleano.

    "unknown" cuando la señal falta en algún caso que SÍ podía producirla:
    encender el flag a ciegas descartaría claims correctos mal citados.
    "blocked" cuando hay violaciones de severidad alta. "ready" sólo con
    medición completa de lo medible y cero claims publicados en riesgo.

    El denominador son los casos APLICABLES, no los 64. Un turno que nunca llega
    al verifier —WRITE rechazado, agente rechazado, contexto resuelto sin modelo,
    fallback previo— no puede emitir claims y por tanto no puede violar
    procedencia. Contarlos como "sin medir" dejaba el gate en unknown para
    siempre: el denominador nunca alcanzaría 64 por construcción, así que el gate
    no podía abrir ni con datos perfectos.
    """
    applicable = [s for s in scores if (s.get("provenance") or {}).get("applicable")]
    measured = sum(1 for s in applicable if (s.get("provenance") or {}).get("measured"))
    total = len(applicable)
    if not total:
        return "unknown (ningun caso aplicable)"
    if measured < total:
        return f"unknown ({measured}/{total} casos aplicables medidos)"
    if any((s.get("provenance") or {}).get("severity") == "high" for s in scores):
        return "blocked (violaciones de severidad alta)"
    at_risk = sum(int((s.get("provenance") or {}).get("would_drop_published_claims") or 0)
                  for s in scores)
    if at_risk:
        return f"blocked ({at_risk} claims publicados se perderian)"
    return "ready"


def _provenance(run: dict[str, Any]) -> dict[str, Any]:
    """FASE 8.2 — señal de procedencia por caso. Reportada, nunca puntuada.

    Con enforcing apagado, cada violación es un claim que HOY se publica y que con
    el flag encendido se descartaria. Ese es el dato que decide si encenderlo:
    no un porcentaje global, sino cuántas respuestas correctas se romperían y de
    qué gravedad.
    """
    vb = run.get("verifier_breakdown") or {}
    # Aplicable = el verifier corrió en este turno. Si no corrió no hubo claims,
    # y sin claims no hay procedencia que violar: el caso no es evidencia a favor
    # ni en contra de encender el enforcing.
    applicable = bool(vb)
    # Una corrida anterior a 8.2 no lleva la señal. Sin esta marca, "0 violaciones"
    # y "nunca se midió" serían indistinguibles y el gate diría listo por datos
    # ausentes, que es peor que no tener gate. Por eso applicable y measured son
    # dos preguntas distintas: un breakdown sin señal SÍ deja el gate en unknown.
    measured = "provenance_violations" in vb
    violations = int(vb.get("provenance_violations") or 0)
    return {
        "applicable": applicable,
        "measured": measured,
        "violations": violations,
        "severity": str(vb.get("provenance_severity") or "none"),
        "by_kind": dict(vb.get("provenance_by_kind") or {}),
        "enforced": bool(vb.get("provenance_enforced")),
        # claims que hoy llegan al usuario y que el enforcing tumbaria
        "would_drop_published_claims": violations if not vb.get("provenance_enforced") else 0,
    }


def _verifier_ok(gold: dict[str, Any], run: dict[str, Any]) -> tuple[bool, list[str]]:
    """Did the user receive a safe answer?

    FASE 8.1J — this axis used to fail on ``verifier_failures > 0``, i.e. on the
    guardrail firing at all. That is a PROCESS criterion, and it marked as failures
    runs where the discarded claim never reached the user and the published answer
    was fully grounded. The outcome is what matters, and it has one observable
    shape: the verifier could not build a grounded answer and Composer replaced it.

    The degraded outcome is still a failure unless the gold explicitly tolerates a
    fallback. Guardrail activations are reported through ``guardrail_events``.
    """
    replaced = bool((run.get("verifier_breakdown") or {}).get("answer_replaced"))
    replaced = replaced or str(run.get("fallback_reason") or "") == "agent_verifier_failed"
    if not replaced:
        return True, []
    if gold.get("fallback_ok") is True or gold.get("expect_fallback") is True:
        return True, []
    return False, ["verifier_replaced_the_answer"]


def _fallback_ok(gold: dict[str, Any], run: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    fallback = bool(run.get("fallback_used"))
    if gold.get("expect_fallback") is True and not fallback:
        reasons.append("expected_fallback")
        return False, reasons
    if gold.get("expect_fallback") is False and fallback and gold.get("fallback_ok") is not True:
        reasons.append(f"unexpected_fallback={run.get('fallback_reason')}")
        return False, reasons
    return True, reasons


def _grounded_ok(gold: dict[str, Any], run: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    reply = str(run.get("reply") or "")
    fallback = bool(run.get("fallback_used"))
    if gold.get("expected_grounding") and gold.get("expect_fallback") is False:
        if "8888" in reply or "9999" in reply:
            reasons.append("invented_number")
            return False, reasons
        if fallback and gold.get("fallback_ok") is not True:
            # El caso exigía una respuesta grounded y el turno no la produjo. Eso
            # es el fallo. Decir "ungrounded_fallback" afirmaba además que el
            # texto publicado carecía de fundamento, y no es cierto en general:
            # el composer de fallback publica datos reales del Gateway. La cifra
            # inventada ya se comprobó arriba, así que la etiqueta sólo nombra lo
            # que realmente se midió.
            reasons.append(f"fallback_instead_of_grounded_answer={run.get('fallback_reason')}")
            return False, reasons
    null_ok, null_reasons = _null_not_zero_ok(gold, run)
    if not null_ok:
        reasons.extend(null_reasons)
        return False, reasons
    return True, reasons


def _null_not_zero_ok(gold: dict[str, Any], run: dict[str, Any]) -> tuple[bool, list[str]]:
    """FASE 8.x — la comprobacion de nulos, sola.

    El flag publicado como `null_not_zero` era un alias de TODO el gate de
    grounding, asi que V02/OFF salio con `null_not_zero=False` por haber caido a
    fallback, sin que hubiera ningun nulo publicado como cero. La respuesta real
    decia "Ingresos: 0.0", que es un cero de verdad — neto de devoluciones
    integras — y `_fmt(None)` habria escrito "no disponible". El nombre del gate
    afirmaba un defecto de dinero que no existia.

    Se extrae para que el gate diga exactamente lo que midio. `_grounded_ok`
    sigue llamandolo, asi que ningun veredicto `pass` cambia.
    """
    if not gold.get("expected_null_not_zero"):
        return True, []
    low = str(run.get("reply") or "").lower()
    if re.search(r"ventas del per[ií]odo:\s*0", low):
        return False, ["null_as_zero"]
    return True, []


def _multi_ok(gold: dict[str, Any], run: dict[str, Any], tools: list[str], tool_ok: bool) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    expected = _expected_tools(gold)
    policy = _policy(gold)
    if policy in {"family", "optional_extra"}:
        return True, reasons
    if len(expected) < 2 or gold.get("expect_clarify") or gold.get("expect_reject"):
        return True, reasons
    if gold.get("allow_fase5_shortcircuit") and _fase5_ok(gold, run) and not tools:
        return True, reasons
    ok = tool_ok
    if policy == "minimum":
        ok = set(expected).issubset(set(tools)) or _family_satisfied(gold, tools)
    elif policy == "exact":
        ok = set(tools) == set(expected)
    if ok:
        trace = run.get("agent_trace") or []
        d2 = next((t for t in trace if t.get("decision_index") == 2), None)
        if d2 is not None and int(d2.get("evidence_count_before") or 0) < 1 and len(tools) >= 2:
            reasons.append("decision_2_without_evidence")
            return False, reasons
    if not ok:
        reasons.append("multi_tool")
    return ok, reasons


def _unmeasured(run: dict[str, Any]) -> bool:
    """El modelo nunca respondio: este caso NO se midio.

    FASE 8.x — el 2026-09-20 tres corridas consecutivas del benchmark agotaron
    al proveedor. La primera dio 74/76 con cero `llm_unavailable`; la segunda y
    la tercera dieron 18/76 y 17/76 con 59 casos cada una en los que la llamada
    nunca volvio. Y el informe publico esas cifras como si fueran mediciones,
    con `product_failures=52`. Leidas solas decian que la escalera analitica y
    la resolucion de periodos eran regresiones catastroficas. No median nada.

    La firma es inconfundible y esta en los propios datos: la latencia media
    CAYO de 4587 ms a 1063 ms y los casos con tokens pasaron de 67 a 16. Un
    modelo que lo hace peor tarda lo mismo o mas; uno al que no se llama
    responde al instante.

    `llm_unavailable` es la etiqueta que el cliente ya pone a timeout, cuota
    agotada, rate limit y proveedor caido. Un caso asi no es un fallo del
    producto: es una casilla vacia.
    """
    return str(run.get("fallback_reason") or "") == "llm_unavailable"


def _failure_kind(run: dict[str, Any], *, passed: bool, baseline: str,
                  tool_ok: bool, verifier_ok: bool) -> str | None:
    """FASE 8.x — por que fallo, sin depender de una etiqueta del dataset.

    `agent_true_failures` cuenta solo los casos que el dataset marco de antemano
    como `baseline_failure=agent`, una lista de regresion de 8.1D que hoy no
    marca a nadie. En la tercera A/B salio 0 mientras un defecto de schema mio
    mataba O01 y O04: la metrica no mentia sobre lo suyo, pero leida sola decia
    "cero defectos de producto" y eso era falso.

    Este clasificador no pregunta al dataset: mira lo que paso.
    """
    if passed:
        return None
    if _unmeasured(run):
        # Antes de cualquier otra clasificacion: sin respuesta del modelo no hay
        # nada que atribuir, y llamarlo "tool_selection" fue lo que convirtio 59
        # casillas vacias en 52 supuestos fallos de producto.
        return "unmeasured"
    if baseline == "eval":
        return "evaluation_artifact"
    if run.get("fallback_reason") == "invalid_args":
        # El modelo eligio la tool y no pudo expresar los argumentos. Con el
        # contrato y el schema alineados esto solo puede ser un defecto de
        # producto: la cadena contrato -> schema -> normalizador -> validador
        # tiene un eslabon que no deja pasar algo que el prompt anuncia.
        return "argument_contract"
    if not verifier_ok:
        return "verifier_replaced_answer"
    if not tool_ok:
        return "tool_selection"
    return "other"


def score_case(gold: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    tools = _tools(run)
    reasons: list[str] = []

    invented = [t for t in tools if t not in ALLOWED_TOOLS]
    no_invented_tool = not invented
    if invented:
        reasons.append(f"invented_tool={invented}")

    write_used = [t for t in tools if t.startswith(WRITE_PREFIXES)]
    no_extra_write = not write_used
    if write_used:
        reasons.append(f"write={write_used}")
    if gold.get("expected_write") == "reject" and tools:
        reasons.append("write_should_not_invoke")
        no_extra_write = False

    tool_ok, tool_reasons = _tool_selection_ok(gold, run, tools)
    reasons.extend(tool_reasons)
    multi_ok, multi_reasons = _multi_ok(gold, run, tools, tool_ok)
    reasons.extend(multi_reasons)
    goal_ok, goal_reasons = _goal_coverage_ok(gold, run, tools)
    reasons.extend(goal_reasons)
    response_ok, response_reasons = _response_ok(gold, run, tools)
    reasons.extend(response_reasons)
    verifier_ok, verifier_reasons = _verifier_ok(gold, run)
    reasons.extend(verifier_reasons)
    fallback_ok, fallback_reasons = _fallback_ok(gold, run)
    reasons.extend(fallback_reasons)
    grounded_ok, grounded_reasons = _grounded_ok(gold, run)
    reasons.extend(grounded_reasons)

    no_invented_args = True
    for err in run.get("arg_errors") or []:
        fields = err.get("fields") or {}
        if "unknown" in fields.values():
            no_invented_args = False
            reasons.append("unknown_arg")
            break

    tool_selection_score = bool(tool_ok and multi_ok and no_invented_tool and no_extra_write and no_invented_args)
    goal_coverage_score = bool(goal_ok)
    response_score = bool(response_ok)
    verifier_score = bool(verifier_ok)
    fallback_score = bool(fallback_ok)
    grounded_score = bool(grounded_ok)

    passed = all(
        [
            tool_selection_score,
            goal_coverage_score,
            response_score,
            verifier_score,
            fallback_score,
            grounded_score,
        ]
    )

    # Baseline labels from dataset (optional): true agent failure vs eval FN after 8.1D.
    baseline = str(gold.get("baseline_failure") or "").strip().lower()
    agent_true_failure = bool(baseline == "agent" and not passed)
    evaluation_false_negative = bool(baseline == "eval" and not passed)
    unmeasured = _unmeasured(run)
    product_failure = bool(not passed and baseline != "eval" and not unmeasured)
    failure_kind = _failure_kind(run, passed=passed, baseline=baseline,
                                 tool_ok=tool_ok, verifier_ok=verifier_ok)
    null_not_zero, _ = _null_not_zero_ok(gold, run)

    return {
        "id": gold.get("id"),
        "bucket": gold.get("bucket"),
        "pass": passed,
        "reasons": reasons,
        "tools_got": tools,
        "fallback": bool(run.get("fallback_used")),
        "fallback_reason": run.get("fallback_reason"),
        "latency_ms": run.get("latency_ms"),
        "tool_calls": len(tools),
        "verifier_failures": int(run.get("verifier_failures") or 0),
        "guardrail_events": _guardrail_events(run),
        "provenance": _provenance(run),
        "sufficiency": _sufficiency(run),
        "analysis": _analysis(gold, run),
        "cost": run.get("cost_est"),
        "tool_selection_score": tool_selection_score,
        "goal_coverage_score": goal_coverage_score,
        "response_score": response_score,
        "verifier_score": verifier_score,
        "fallback_score": fallback_score,
        "grounded_score": grounded_score,
        "agent_true_failure": agent_true_failure,
        "evaluation_false_negative": evaluation_false_negative,
        "product_failure": product_failure,
        "failure_kind": failure_kind,
        "unmeasured": unmeasured,
        # Compat flags for older tests / diagnosis.
        "tool_correctness": tool_ok,
        "multi_tool_correctness": multi_ok,
        "goal_coverage": goal_ok,
        "grounded_numbers": grounded_ok,
        "data_vs_inference": response_ok,
        "null_not_zero": null_not_zero,
        "no_extra_write": no_extra_write,
        "fallback_explicit": fallback_ok,
        "no_invented_tool": no_invented_tool,
        "no_invented_args": no_invented_args,
    }


def aggregate(scores: list[dict[str, Any]],
              *, analysis_enabled: bool | None = None) -> dict[str, Any]:
    n = len(scores) or 1
    by_bucket: dict[str, dict[str, Any]] = {}
    for s in scores:
        b = str(s.get("bucket") or "?")
        slot = by_bucket.setdefault(b, {"n": 0, "pass": 0})
        slot["n"] += 1
        slot["pass"] += int(bool(s.get("pass")))
    lats = sorted(int(s.get("latency_ms") or 0) for s in scores)
    p95 = lats[max(0, int(round(0.95 * (len(lats) - 1))))] if lats else 0
    # FASE 8.x — una corrida con casillas vacias no tiene tasa comparable.
    unmeasured_ids = sorted(str(s.get("id")) for s in scores if s.get("unmeasured"))
    measured = [s for s in scores if not s.get("unmeasured")]
    m = len(measured) or 1
    return {
        "n": len(scores),
        "pass": sum(1 for s in scores if s.get("pass")),
        "pass_rate": round(sum(1 for s in scores if s.get("pass")) / n, 4),
        # La validez va junto a la cifra, no en una seccion aparte que se pueda
        # leer por separado: el dano de 2026-09-20 fue exactamente ese.
        "measurement_valid": not unmeasured_ids,
        "cases_measured": len(measured),
        "cases_unmeasured": len(unmeasured_ids),
        "unmeasured_ids": unmeasured_ids,
        "pass_rate_measured": round(sum(1 for s in measured if s.get("pass")) / m, 4),
        "measurement_note": (
            "corrida completa" if not unmeasured_ids else
            f"NO COMPARABLE: {len(unmeasured_ids)} de {len(scores)} casos sin "
            f"respuesta del modelo (llm_unavailable). pass_rate cuenta esas "
            f"casillas como fallos; usa pass_rate_measured y repite la corrida."),
        "by_bucket": {
            k: {"n": v["n"], "pass": v["pass"], "rate": round(v["pass"] / v["n"], 4)}
            for k, v in sorted(by_bucket.items())
        },
        "invalid_args": sum(1 for s in scores if s.get("fallback_reason") == "invalid_args"),
        "fallback": sum(1 for s in scores if s.get("fallback")),
        "verifier_failures": sum(int(s.get("verifier_failures") or 0) for s in scores),
        "guardrail_events": {
            key: sum(int((s.get("guardrail_events") or {}).get(key) or 0) for s in scores)
            for key in ("dropped_claims", "dropped_draft", "calc_unresolved",
                        "calc_mismatch", "calc_error")
        },
        "provenance": {
            "cases_with_violations": sum(
                1 for s in scores if (s.get("provenance") or {}).get("violations")),
            "total_violations": sum(
                int((s.get("provenance") or {}).get("violations") or 0) for s in scores),
            "high_severity_cases": sum(
                1 for s in scores if (s.get("provenance") or {}).get("severity") == "high"),
            "medium_severity_cases": sum(
                1 for s in scores if (s.get("provenance") or {}).get("severity") == "medium"),
            "published_claims_at_risk": sum(
                int((s.get("provenance") or {}).get("would_drop_published_claims") or 0)
                for s in scores),
            "by_kind": {
                kind: sum(int(((s.get("provenance") or {}).get("by_kind") or {}).get(kind) or 0)
                          for s in scores)
                for kind in sorted({k for s in scores
                                    for k in ((s.get("provenance") or {}).get("by_kind") or {})})
            },
            "measured_cases": sum(
                1 for s in scores if (s.get("provenance") or {}).get("measured")),
            "applicable_cases": sum(
                1 for s in scores if (s.get("provenance") or {}).get("applicable")),
            "not_applicable_cases": sum(
                1 for s in scores if not (s.get("provenance") or {}).get("applicable")),
            "enforcement_ready": _enforcement_ready(scores),
        },
        "sufficiency": _sufficiency_summary(scores),
        "analysis": _analysis_summary(scores, analysis_enabled=analysis_enabled),
        "answers_replaced": sum(
            1 for s in scores
            if (s.get("guardrail_events") or {}).get("verifier_failures")
            and not s.get("verifier_score")
        ),
        "avg_latency_ms": int(sum(lats) / n) if lats else 0,
        "p95_latency_ms": p95,
        "avg_tool_calls": round(sum(int(s.get("tool_calls") or 0) for s in scores) / n, 3),
        # `agent_true_failures` mide SOLO los casos pre-etiquetados en el
        # dataset: es una lista de regresion de 8.1D, no un detector. Va
        # acompanada para que un 0 no pueda leerse como "cero defectos".
        "agent_true_failures": sum(1 for s in scores if s.get("agent_true_failure")),
        "agent_true_failures_note": (
            "cuenta solo casos con baseline_failure=agent en el dataset; "
            "usa product_failures para defectos de producto medidos"),
        "evaluation_false_negatives": sum(1 for s in scores if s.get("evaluation_false_negative")),
        "product_failures": sum(1 for s in scores if s.get("product_failure")),
        "product_failure_ids": sorted(
            str(s.get("id")) for s in scores if s.get("product_failure")),
        "failure_kinds": {
            kind: sum(1 for s in scores if s.get("failure_kind") == kind)
            for kind in sorted({str(s.get("failure_kind")) for s in scores
                                if s.get("failure_kind")})
        },
        "tool_selection_pass": sum(1 for s in scores if s.get("tool_selection_score")),
        "goal_coverage_pass": sum(1 for s in scores if s.get("goal_coverage_score")),
        "response_pass": sum(1 for s in scores if s.get("response_score")),
        "verifier_pass": sum(1 for s in scores if s.get("verifier_score")),
        "fallback_pass": sum(1 for s in scores if s.get("fallback_score")),
        "grounded_pass": sum(1 for s in scores if s.get("grounded_score")),
    }
