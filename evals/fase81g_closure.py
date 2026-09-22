"""FASE 8.1G closure — deterministic proof + optional real-LLM validation + report.

  python -m evals.fase81g_closure                  deterministic only (no LLM key needed)
  python -m evals.fase81g_closure --llm            + P03/T07/M02/M04 x10 with the real LLM
  python -m evals.fase81g_closure --llm --bench    + the full 64-case benchmark

Always restores AGENT=0 / NL=0 / ORCH=fake before exiting. Never prints secrets,
prompts or raw model responses. Writes:

  data/fase81_eval/fase81_final_report.json
  data/fase81_eval/fase81_final_report.txt
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "data" / "fase81_eval"


def _arm_report(out_dir: Path, *, measurement_valid: bool = True) -> dict[str, Any]:
    """Que brazo es este y si el otro sigue disponible para comparar."""
    import os as _os
    from datetime import datetime, timezone

    this = arm_id()
    siblings: dict[str, Any] = {}
    # Se generan desde las dimensiones en vez de escribirse: anadir una tercera
    # y olvidar esta lista haria que un brazo no viera a sus hermanos.
    for other in _all_arm_ids():
        path = out_dir / f"fase81_final_report.{other}.json"
        if other == this or not path.exists():
            continue
        siblings[other] = datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
    return {
        "arm": this,
        "analysis_enabled": "an1" in this.split("-"),
        "provenance_enforced": "pv1" in this.split("-"),
        "period_resolution": "pr1" in this.split("-"),
        "orders_enabled": "or1" in this.split("-"),
        "siblings_available": siblings,
        # Un A/B necesita los DOS brazos. Decirlo aqui evita analizar uno solo
        # creyendo que se tiene la pareja, que es lo que paso en 8.5.
        "measurement_valid": measurement_valid,
        # Un A/B necesita dos brazos Y que los dos hayan medido algo. Comparar
        # contra una corrida con casillas vacias produce una regresion inventada.
        "ab_complete": bool(siblings) and measurement_valid,
        "note": ("no comparable: esta corrida no midio (llm_unavailable)"
                 if not measurement_valid else
                 "comparable: hay otro brazo en disco" if siblings else
                 "brazo unico: ejecuta el otro antes de concluir un A/B"),
    }


def arm_id() -> str:
    """Identificador del BRAZO de configuracion de esta corrida.

    FASE 8.6 — el A/B de 8.5 se perdio a medias: los dos brazos escribian a las
    mismas rutas y el segundo piso al primero, asi que hubo que comparar contra
    una corrida de otra hora. Un A/B que no conserva sus dos brazos no es un A/B.

    El id se deriva de los flags que DEFINEN el brazo, no de un contador ni de un
    timestamp: dos corridas con la misma configuracion deben caer en el mismo
    fichero (es una repeticion), y dos configuraciones distintas NUNCA pueden
    caer en el mismo (serian dos cosas mezcladas).
    """
    import os as _os

    def _on(name: str) -> bool:
        return (_os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}

    parts = [
        "an1" if _on("ANDES_ASSISTANT_ANALYSIS_ENABLED") else "an0",
        "pv1" if _on("ANDES_ASSISTANT_PROVENANCE_ENFORCE") else "pv0",
        # FASE 8.x — la resolucion de periodos cambia los ARGUMENTOS con que se
        # llama a las tools en 7 de 76 preguntas, y por tanto las respuestas. Una
        # dimension que cambia el comportamiento tiene que estar en el id del
        # brazo o dos configuraciones distintas se pisan el fichero, que es
        # exactamente el fallo de 8.5 que este identificador existe para evitar.
        "pr1" if _on("ANDES_ASSISTANT_PERIOD_RESOLUTION") else "pr0",
        # FASE 9.2 — la visibilidad de get_orders cambia el prompt de TODAS las
        # preguntas, no solo de las comerciales. Mismo argumento que `pr`: una
        # dimension que cambia el comportamiento va en el id o dos brazos se
        # pisan el fichero.
        "or1" if _on("ANDES_ASSISTANT_ORDERS_ENABLED") else "or0",
    ]
    return "-".join(parts)


# Dimension -> (prefijo, variable de entorno). Una sola tabla: el id del brazo,
# la enumeracion de hermanos y la restauracion tras la suite salen de aqui.
#
# FASE 9.2 — antes la restauracion enumeraba las banderas a mano, y al anadir la
# cuarta dimension me la deje: la corrida con ORDERS=1 se reetiqueto como `or0` y
# piso el informe del otro brazo. El guard lo detecto (stable=False) porque se
# comprueba a si mismo, pero la lista a mano era el defecto. Derivarla cierra la
# clase entera.
ARM_FLAGS: tuple[tuple[str, str], ...] = (
    ("an", "ANDES_ASSISTANT_ANALYSIS_ENABLED"),
    ("pv", "ANDES_ASSISTANT_PROVENANCE_ENFORCE"),
    ("pr", "ANDES_ASSISTANT_PERIOD_RESOLUTION"),
    ("or", "ANDES_ASSISTANT_ORDERS_ENABLED"),
)
ARM_DIMENSIONS = tuple((f"{p}0", f"{p}1") for p, _ in ARM_FLAGS)


def _all_arm_ids() -> tuple[str, ...]:
    import itertools

    return tuple("-".join(combo) for combo in itertools.product(*ARM_DIMENSIONS))


def arm_path(out_dir: Path, stem: str, suffix: str) -> Path:
    """Ruta con el brazo incrustado. Sin esto los brazos se sobrescriben."""
    return out_dir / f"{stem}.{arm_id()}{suffix}"
DATASET = ROOT / "evals" / "fase81_evidence_dataset.jsonl"

BASELINE = {
    "source": "data/fase81_eval/summary_llm.json (FASE 8.1D, pre-8.1G)",
    "n": 64,
    "pass": 59,
    "pass_rate": 0.9219,
    "failed": ["T07", "K05", "P03", "M02", "M04"],
    "agent_true_failures": ["T07", "K05", "P03"],
    "fallback": 2,
    "verifier_failures": 3,
    "premature_final": 1,
    "blocked_final": 2,
    "invalid_args": 0,
    "avg_latency_ms": 5554,
    "p95_latency_ms": 11008,
}

# N04 joins the set in 8.1G.3: the definitive-failure derivation must hold across
# runs, not just in the deterministic probe.
# El suelo de regresion de 8.1G: estos cinco se repiten siempre, pasen o no.
STABILITY_CORE_IDS = ("P03", "T07", "M02", "M04", "N04")
STABILITY_RUNS = 10

# FASE 8.x — sin repeticiones, "varianza" es una opinion.
#
# El set de estabilidad era una constante congelada en 8.1G, asi que medi­a los
# casos que importaban entonces y ninguno de los que fallan hoy. Resultado: la
# tercera A/B no pudo distinguir si V02 fallo por varianza o por producto, y la
# respuesta se argumento en prosa en vez de medirse. La lista de vigilancia se
# DERIVA de lo que fallo la ultima vez en este mismo brazo, asi que se mantiene
# sola: un caso arreglado deja de vigilarse y uno nuevo entra el mismo dia.
STABILITY_WATCH_RUNS = 5
# Semilla para la primera corrida de un brazo sin informe previo en disco.
# Solo bootstrap: en cuanto exista un informe del brazo, la lista sale de
# `benchmark.failed` y esta constante deja de usarse. Medido el 2026-09-20:
# O01/O04 pasan 4/4 y V02 pasa con pr1, asi que el unico fallo con causa
# demostrada y abierta es A01.
STABILITY_WATCH_SEED = ("A01",)

# Techo estructural. La lista se deriva del informe anterior, asi que un informe
# degradado la dispara: medido el 2026-09-20, el brazo an1-pv0-pr0 quedo con 58
# casos "fallidos" que en realidad eran casillas vacias, y la siguiente corrida
# de ese brazo habria gastado 265 turnos extra vigilandolos — en una cuenta ya
# sin cuota, garantizando otra corrida agotada. El tope protege aunque la
# deteccion de validez fallara algun dia.
MAX_WATCH_IDS = 6

TEST_MODULES = [
    # 8.1A .. 8.1G
    "tests.test_orchestrator_fase81_agent",
    "tests.test_orchestrator_fase81a_goal",
    "tests.test_orchestrator_fase81b_args",
    "tests.test_orchestrator_fase81c_failure_diagnosis",
    "tests.test_orchestrator_fase81d_scorer",
    "tests.test_orchestrator_fase81g_progress",
    "tests.test_orchestrator_fase81g2_harness_parity",
    "tests.test_orchestrator_fase81g3_definitive",
    "tests.test_orchestrator_fase81h_grounding",
    "tests.test_orchestrator_fase81h2_dates",
    "tests.test_orchestrator_fase81i_count",
    "tests.test_orchestrator_fase82_provenance",
    "tests.test_orchestrator_fase82b_budget",
    "tests.test_orchestrator_fase82c_evidence_chain",
    "tests.test_orchestrator_fase82d_budget_honesty",
    "tests.test_orchestrator_fase82e_contract",
    "tests.test_orchestrator_fase83_sufficiency",
    "tests.test_orchestrator_fase84_answer_view",
    "tests.test_orchestrator_fase85_analysis",
    "tests.test_orchestrator_fase85b_capability_gate",
    "tests.test_orchestrator_fase86_sales",
    "tests.test_orchestrator_fase87_integration",
    "tests.test_orchestrator_fase88_equivalences",
    "tests.test_orchestrator_fase89_contract_gaps",
    # 8.x — contrato/schema/normalizador/validador y vacios explicados
    "tests.test_orchestrator_fase8x_contract_schema",
    "tests.test_orchestrator_fase8x_scorer_honesty",
    "tests.test_orchestrator_fase8x_empty_is_a_fact",
    "tests.test_orchestrator_fase8x_period",
    "tests.test_orchestrator_fase8x_a01_finding",
    "tests.test_orchestrator_fase8x_unmeasured",
    "tests.test_orchestrator_fase8x_boundaries",
    "tests.test_orchestrator_fase8x_artifacts",
    # 9.1 — economia de prompt
    "tests.test_orchestrator_fase9_prompt_economy",
    "tests.test_orchestrator_fase92_orders",
    "tests.test_orchestrator_fase9_flag_measurement",
    "tests.test_orchestrator_fase9_preflight",
    # FASE 5 / 7A / 7B
    "tests.test_orchestrator_fase5_context",
    "tests.test_orchestrator_fase7a_history",
    "tests.test_orchestrator_fase7b1_memory",
    "tests.test_orchestrator_fase7b2_memory",
    "tests.test_orchestrator_fase7b3_memory",
    "tests.test_orchestrator_fase7b4_memory",
    "tests.test_orchestrator_fase7b5_memory",
    # Gateway contracts
    "tests.test_orchestrator_gateway_contracts",
    # ERP
    "tests.test_paso2_erp",
    "tests.test_paso3_erp",
    "tests.test_paso4_erp",
    "tests.test_paso5_erp",
    "tests.test_paso6_erp",
    "tests.test_paso7_erp",
    "tests.test_paso8_erp",
    "tests.test_paso9_erp",
    "tests.test_paso10_erp",
    "tests.test_paso11_erp",
]

MODIFIED_FILES = [
    "app/assistant/orchestrator/agent_progress.py  (new)",
    "app/assistant/orchestrator/agent_loop.py",
    "app/assistant/orchestrator/agent_config.py",
    "app/assistant/orchestrator/evidence_store.py",
    "app/assistant/orchestrator/goal_coverage.py",
    "app/assistant/orchestrator/answer_verifier.py",
    "app/assistant/orchestrator/agent_schema.py",
    "app/assistant/orchestrator/llm/plan_schema.py",
    "app/assistant/orchestrator/llm/agent_prompts.py",
    "app/assistant/orchestrator/service.py",
    "evals/fase81_runner.py",
    "evals/fase81g_closure.py  (new)",
    "tests/test_orchestrator_fase81g_progress.py  (new)",
    "tests/test_orchestrator_fase81g2_harness_parity.py  (new)",
    "tests/test_orchestrator_fase81g3_definitive.py  (new)",
    "tests/test_orchestrator_fase81h_grounding.py  (new)",
    "tests/test_orchestrator_fase81h2_dates.py  (new)",
    "tests/test_orchestrator_fase81i_count.py  (new)",
    "evals/fase81h2_dropped_claims.py  (new)",
    "docs/fase81-evidence-aware.md",
]


# --------------------------------------------------------------------------- env


def _restore_safe_env() -> None:
    os.environ["ANDES_ASSISTANT_AGENT_ENABLED"] = "0"
    os.environ["ANDES_ASSISTANT_NL_ENABLED"] = "0"
    os.environ["ANDES_ORCH_PLANNER"] = "fake"


def _base_env() -> None:
    from app.utils.load_env import load_project_dotenv

    load_project_dotenv(force=False)
    os.environ.setdefault("ANDES_AGENT_URL", "http://127.0.0.1:5055")
    os.environ.setdefault("ANDES_ENV", "local")
    os.environ.setdefault("ANDES_ASSISTANT_MEMORY_ENABLED", "0")
    os.environ.setdefault("ANDES_ASSISTANT_HISTORY_ENABLED", "0")


def _scrub(text: str) -> str:
    """Never let a secret reach an artifact, even if it could only get there by a
    bug upstream. Mirrors evals/fase81_runner.py."""
    out = text
    for name in ("ANDES_LLM_API_KEY", "ANDES_AGENT_SERVICE_TOKEN"):
        value = (os.environ.get(name) or "").strip()
        if value:
            out = out.replace(value, f"[REDACTED_{name}]")
    return out


def _gateway_ready() -> bool:
    return bool((os.environ.get("ANDES_AGENT_SERVICE_TOKEN") or "").strip())


def _llm_ready() -> bool:
    return bool((os.environ.get("ANDES_LLM_API_KEY") or "").strip())


# ------------------------------------------------------------------ deterministic


def _call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": "call_tool",
        "tool": tool,
        "arguments": arguments,
        "reason": "closure probe",
        "claims": [],
        "calculations": [],
    }


def _final(text: str, eids: list[str]) -> dict[str, Any]:
    return {
        "action": "final_answer",
        "reason": "enough",
        "draft_reply": text,
        "claims": [{"kind": "dato", "text": text, "evidence_ids": eids}],
        "calculations": [],
    }


def _scripted_loop(message: str, decisions: list[dict[str, Any]]) -> Any:
    """Drive the real AgentLoop against the real Gateway with scripted decisions.

    No LLM involved: this isolates the loop/admission logic from model variance.
    """
    from app.assistant.orchestrator.agent_loop import QueueDecisionClient, continue_agent_loop
    from app.assistant.routes import invoke_gateway

    return continue_agent_loop(
        message=message,
        actor_user="albertadmin",
        conversation_id="fase81g-closure",
        correlation_id="fase81g-closure",
        initial_plan=None,
        initial_evidence=None,
        invoke_fn=invoke_gateway,
        decision_client=QueueDecisionClient(decisions),
    )


def _old_gate_would_block(raw_evidence: list[dict[str, Any]], empty_followups: int) -> bool:
    """The pre-8.1G gate, reproduced for the before/after comparison only."""
    from app.assistant.orchestrator.agent_config import MAX_EMPTY_FOLLOWUPS

    last_empty = bool(raw_evidence and raw_evidence[-1].get("empty"))
    return bool(last_empty and empty_followups >= MAX_EMPTY_FOLLOWUPS)


def probe_t07_shape() -> dict[str, Any]:
    """supplier(empty) -> purchase_orders must execute. Real Gateway, scripted acts."""
    out = _scripted_loop(
        "Proveedor BOSCH y sus órdenes de compra.",
        [
            _call("get_supplier", {"q": "BOSCH"}),
            _call("get_purchase_orders", {"proveedor": "BOSCH"}),
            _final("Sin registros para BOSCH.", ["e1", "e2"]),
        ],
    )
    tools = [i.tool for i in out.state.evidence.items]
    statuses = {r.type: r.status for r in out.state.goal.requirements}
    first_empty = bool(out.raw_evidence and out.raw_evidence[0].get("empty"))
    return {
        "tools_executed": tools,
        "get_purchase_orders_executed": "get_purchase_orders" in tools,
        "first_result_empty": first_empty,
        "requirements": statuses,
        "both_covered": statuses.get("supplier") == "covered"
        and statuses.get("purchase_orders") == "covered",
        "fallback_used": bool(out.fallback_used),
        "fallback_reason": out.fallback_reason,
        "blocked_finals": int(out.state.blocked_finals),
        "progress": out.state.ledger.safe_snapshot(),
        "old_gate_would_block_second_tool": _old_gate_would_block(out.raw_evidence[:1], 1),
        "verdict": (
            "PASS"
            if ("get_purchase_orders" in tools and not out.fallback_used)
            else "FAIL"
        ),
    }


def probe_t07_blocked_finals() -> dict[str, Any]:
    """Blocked finals must not consume the budget that reaches the covering tool."""
    out = _scripted_loop(
        "Proveedor BOSCH y sus órdenes de compra.",
        [
            _call("get_supplier", {"q": "BOSCH"}),
            _final("parcial", ["e1"]),
            _final("parcial otra vez", ["e1"]),
            _call("get_purchase_orders", {"proveedor": "BOSCH"}),
            _final("Sin registros para BOSCH.", ["e1", "e2"]),
        ],
    )
    tools = [i.tool for i in out.state.evidence.items]
    return {
        "tools_executed": tools,
        "blocked_finals": int(out.state.blocked_finals),
        "fallback_used": bool(out.fallback_used),
        "fallback_reason": out.fallback_reason,
        "verdict": "PASS" if "get_purchase_orders" in tools and not out.fallback_used else "FAIL",
    }


def probe_no_progress_guard() -> dict[str, Any]:
    """Same tool, different args, equivalent payload → rejected, turn survives."""
    out = _scripted_loop(
        "KPIs de los últimos 7 días.",
        [
            _call("get_dashboard_kpis", {"periodo": "7d"}),
            _call("get_dashboard_kpis", {"periodo": "7d", "top_limit": 5}),
            _call("get_dashboard_kpis", {"periodo": "7d", "top_limit": 3}),
            _final("Periodo 7d.", ["e1"]),
        ],
    )
    snap = out.state.ledger.safe_snapshot()
    return {
        "invokes": len(out.state.evidence.items),
        "rejections": int(snap.get("rejections") or 0),
        "no_progress_steps": int(snap.get("no_progress_steps") or 0),
        "fallback_used": bool(out.fallback_used),
        "fallback_reason": out.fallback_reason,
        "verdict": "PASS" if len(out.state.evidence.items) <= 2 else "FAIL",
    }


def probe_thrashing_terminates() -> dict[str, Any]:
    """Persistent non-progress still terminates, with its own reason.

    Repeats a tool that returns empty and covers nothing in this goal: the
    arguments differ every time, so MAX_SAME_CALL alone would never fire.
    """
    out = _scripted_loop(
        "KPIs de los últimos 7 días.",
        [
            _call("get_supplier", {"q": "ZZZNOEXISTE1"}),
            _call("get_supplier", {"q": "ZZZNOEXISTE2"}),
            _call("get_supplier", {"q": "ZZZNOEXISTE3"}),
            _call("get_supplier", {"q": "ZZZNOEXISTE4"}),
            _call("get_supplier", {"q": "ZZZNOEXISTE5"}),
        ],
    )
    snap = out.state.ledger.safe_snapshot()
    return {
        "invokes": len(out.state.evidence.items),
        "rejections": int(snap.get("rejections") or 0),
        "fallback_used": bool(out.fallback_used),
        "fallback_reason": out.fallback_reason,
        "budget_spent_vs_max_tool_calls": f"{len(out.state.evidence.items)}/5",
        "verdict": "PASS" if out.fallback_reason == "agent_no_progress" else "FAIL",
    }


def probe_alternation_terminates() -> dict[str, Any]:
    """Alternating between tools without changing coverage is also non-progress."""
    out = _scripted_loop(
        "KPIs de los últimos 7 días.",
        [
            _call("get_supplier", {"q": "ZZZNOEXISTE1"}),
            _call("get_customer", {"q": "ZZZNOEXISTE2"}),
            _call("get_purchase_orders", {"proveedor": "ZZZNOEXISTE3"}),
            _call("get_ingresos", {"codigo": "ZZZNOEXISTE"}),
        ],
    )
    snap = out.state.ledger.safe_snapshot()
    return {
        "invokes": len(out.state.evidence.items),
        "no_progress_steps": int(snap.get("no_progress_steps") or 0),
        "fallback_used": bool(out.fallback_used),
        "fallback_reason": out.fallback_reason,
        "verdict": "PASS" if out.fallback_reason == "agent_no_progress" else "FAIL",
    }


def probe_definitive_not_found() -> dict[str, Any]:
    """FASE 8.1G.3 — a nonexistent entity must close as a grounded negative.

    The scripted acts reproduce what the model actually does after a not_found:
    it keeps proposing final_answer. Before the fix that loop was blocked until the
    token budget died; now the requirement is derived impossible on the first pass.
    """
    out = _scripted_loop(
        "Stock del codigo ZZZZNOEXISTE",
        [_call("get_inventory", {"codigo": "ZZZZNOEXISTE"})]
        + [_final("No hay registro del codigo consultado.", ["e1"])] * 5,
    )
    reqs = {r.type: (r.status, r.reason) for r in out.state.goal.requirements}
    return {
        "invokes": len(out.state.evidence.items),
        "decisions": int(out.state.step_index),
        "blocked_finals": int(out.state.blocked_finals),
        "requirements": {k: list(v) for k, v in reqs.items()},
        "fallback_used": bool(out.fallback_used),
        "fallback_reason": out.fallback_reason,
        "negative_note_in_reply": "No pude resolver" in (out.reply or ""),
        "verdict": (
            "PASS"
            if (
                not out.fallback_used
                and reqs.get("current_inventory", ("", ""))[0] == "impossible"
                and int(out.state.blocked_finals) == 0
            )
            else "FAIL"
        ),
    }


def probe_transient_error_stays_open() -> dict[str, Any]:
    """A transport failure must NEVER become impossible."""
    from app.assistant.orchestrator.evidence_store import EvidenceStore
    from app.assistant.orchestrator.goal_coverage import (
        EXTRACTION_DETECTED,
        GoalCoverage,
        GoalRequirement,
    )

    rows = {}
    ok = True
    for code in ("timeout", "agent_unavailable", "erp_unavailable", "unknown_code"):
        goal = GoalCoverage(
            extraction=EXTRACTION_DETECTED,
            requirements=[GoalRequirement(id="r1", type="current_inventory")],
        )
        store = EvidenceStore()
        store.add_from_tool_result(
            tool="get_inventory",
            arguments={"codigo": "Z"},
            result={"ok": False, "empty": True, "error_code": code, "data": {}, "meta": {}},
        )
        goal.refresh(store)
        st = goal.by_type("current_inventory").status
        rows[code] = st
        ok = ok and st == "uncovered"
    return {"rows": rows, "verdict": "PASS" if ok else "FAIL"}


def probe_blocked_final_release() -> dict[str, Any]:
    """FASE 8.1G.3 — the Nth blocked final releases a partial, not a fallback."""
    out = _scripted_loop(
        "Proveedor BOSCH y sus órdenes de compra.",
        [
            _call("get_supplier", {"q": "BOSCH"}),
            _final("parcial uno", ["e1"]),
            _final("parcial dos", ["e1"]),
            _final("parcial tres", ["e1"]),
        ],
    )
    snap = out.state.ledger.safe_snapshot()
    return {
        "blocked_finals": int(out.state.blocked_finals),
        "released": bool(out.state.blocked_final_released),
        "consecutive_blocked_finals": int(snap.get("consecutive_blocked_finals") or 0),
        "fallback_used": bool(out.fallback_used),
        "fallback_reason": out.fallback_reason,
        "invokes": len(out.state.evidence.items),
        "negative_note_in_reply": "No pude resolver" in (out.reply or ""),
        "verdict": (
            "PASS"
            if (out.state.blocked_final_released and not out.fallback_used)
            else "FAIL"
        ),
    }


def probe_two_blocked_finals_still_continue() -> dict[str, Any]:
    """Measured threshold guard: 2 blocks must NOT release (T07 passes at 2)."""
    out = _scripted_loop(
        "Proveedor BOSCH y sus órdenes de compra.",
        [
            _call("get_supplier", {"q": "BOSCH"}),
            _final("parcial uno", ["e1"]),
            _final("parcial dos", ["e1"]),
            _call("get_purchase_orders", {"proveedor": "BOSCH"}),
            _final("Sin registros para BOSCH.", ["e1", "e2"]),
        ],
    )
    tools = [i.tool for i in out.state.evidence.items]
    return {
        "blocked_finals": int(out.state.blocked_finals),
        "released_too_early": bool(out.state.blocked_final_released),
        "tools_executed": tools,
        "fallback_used": bool(out.fallback_used),
        "verdict": (
            "PASS"
            if (
                not out.state.blocked_final_released
                and "get_purchase_orders" in tools
                and not out.fallback_used
            )
            else "FAIL"
        ),
    }


def probe_numeric_grounding() -> dict[str, Any]:
    """FASE 8.1H — a figure is stated only if evidence or a verified calc attests it.

    Uses the real get_ingresos payload: no field equals 3, but "3" occurs inside
    numero_documento and inside the dates, which is what the old substring check
    accepted for "suman 3 unidades (2 + 1)".
    """
    from app.assistant.orchestrator.answer_verifier import (
        _evidence_blob,
        grounded_numbers,
        verify_agent_answer,
    )
    from app.assistant.orchestrator.evidence_store import EvidenceStore
    from app.assistant.orchestrator.normalizer import normalize_tool_result
    from app.assistant.routes import invoke_gateway

    store = EvidenceStore()
    for tool, args in (("get_ingresos", {"codigo": "2404"}), ("get_inventory", {"codigo": "2404"})):
        status, body = invoke_gateway(
            {
                "agent_id": "andes-assistant",
                "conversation_id": "fase81h",
                "actor_user": "albertadmin",
                "tool": tool,
                "arguments": args,
            }
        )
        store.add_from_tool_result(
            tool=tool, arguments=args, result=normalize_tool_result(status, body)
        )
    claims = [
        {"kind": "dato", "text": "Ingresos recientes del 2404 suman 3 unidades (2 + 1).",
         "evidence_ids": ["e1"]},
        {"kind": "dato", "text": "Stock actual del 2404 en Bodega 1 es de 2 unidades.",
         "evidence_ids": ["e2"]},
    ]
    broken = verify_agent_answer(
        store=store,
        decision={"action": "final_answer", "draft_reply": "", "claims": claims,
                  "calculations": [{"id": "c1", "op": "sum", "inputs": ["e1.data.total"], "result": 3}]},
    )
    verified = verify_agent_answer(
        store=store,
        decision={"action": "final_answer", "draft_reply": "", "claims": claims,
                  "calculations": [{"id": "c1", "op": "sum",
                                    "inputs": ["e1.data.items.0.cantidad", "e1.data.items.1.cantidad"],
                                    "result": 3}]},
    )
    blob_has_three = "3" in _evidence_blob(store, None)
    three_grounded = "3" in grounded_numbers(store)
    return {
        "blob_contains_a_3_somewhere": blob_has_three,
        "3_is_a_grounded_token": three_grounded,
        "unverified_calc": {
            **broken.breakdown(),
            "published_the_derived_figure": "3 unidades" in (broken.reply or ""),
            "kept_the_grounded_claim": "2 unidades" in (broken.reply or ""),
        },
        "verified_calc": {
            **verified.breakdown(),
            "published_the_derived_figure": "3 unidades" in (verified.reply or ""),
        },
        "verdict": (
            "PASS"
            if (
                blob_has_three
                and not three_grounded
                and "3 unidades" not in (broken.reply or "")
                and "2 unidades" in (broken.reply or "")
                and "3 unidades" in (verified.reply or "")
            )
            else "FAIL"
        ),
    }


def probe_cardinality_count() -> dict[str, Any]:
    """FASE 8.1I — a collection of N records grounds N only through count().

    Real KPI payload: stock_critico holds 10 rows and no field equals 10, so the
    figure is unprovable by token grounding and, before count existed, by any
    calculation either.
    """
    from app.assistant.orchestrator.answer_verifier import grounded_numbers, verify_agent_answer
    from app.assistant.orchestrator.evidence_store import EvidenceStore
    from app.assistant.orchestrator.normalizer import normalize_tool_result
    from app.assistant.routes import invoke_gateway

    status, body = invoke_gateway(
        {
            "agent_id": "andes-assistant",
            "conversation_id": "fase81i",
            "actor_user": "albertadmin",
            "tool": "get_dashboard_kpis",
            "arguments": {"periodo": "7d"},
        }
    )
    store = EvidenceStore()
    item = store.add_from_tool_result(
        tool="get_dashboard_kpis", arguments={"periodo": "7d"},
        result=normalize_tool_result(status, body),
    )
    data = item.data_view if isinstance(item.data_view, dict) else {}
    rows = len(data.get("stock_critico") or [])
    claim = "Hay {} productos con stock critico.".format(rows)

    def _run(calcs: list[dict[str, Any]]) -> Any:
        return verify_agent_answer(
            store=store,
            decision={"action": "final_answer", "draft_reply": "",
                      "claims": [{"kind": "dato", "text": claim, "evidence_ids": ["e1"]}],
                      "calculations": calcs},
        )

    def _c(result: Any, path: str = "e1.data.stock_critico") -> list[dict[str, Any]]:
        return [{"id": "c1", "op": "count", "inputs": [path], "result": result}]

    without = _run([])
    correct = _run(_c(rows))
    wrong = _run(_c(rows + 1))
    scalar = _run(_c(rows, "e1.data.docs_periodo"))
    figure = str(rows)
    # Whether the CLAIM survived, not whether the digit appears anywhere: once a
    # claim is dropped Composer writes its own reply, which may legitimately
    # contain the same digit for an unrelated reason.
    phrase = "{} productos con stock critico".format(rows)

    def _kept(result: Any) -> bool:
        return phrase in (result.reply or "")

    return {
        "collection_rows": rows,
        "figure_is_an_evidence_token": figure in grounded_numbers(store),
        "claim_phrase": phrase,
        "without_count": {**without.breakdown(), "claim_published": _kept(without)},
        "with_correct_count": {**correct.breakdown(), "claim_published": _kept(correct)},
        "with_wrong_count": {**wrong.breakdown(), "claim_published": _kept(wrong)},
        "count_on_a_scalar": {**scalar.breakdown(), "claim_published": _kept(scalar)},
        "verdict": (
            "PASS"
            if (
                rows > 0
                and figure not in grounded_numbers(store)
                and not _kept(without)
                and _kept(correct)
                and wrong.calc_mismatch == 1
                and not _kept(wrong)
                and scalar.calc_error == 1
                and not _kept(scalar)
            )
            else "FAIL"
        ),
    }


def probe_covering_tools_are_named() -> dict[str, Any]:
    """FASE 8.1J — the model must be told WHICH tool is missing, not just which
    requirement type. The system always knew; it never said it."""
    from app.assistant.orchestrator.evidence_store import EvidenceStore
    from app.assistant.orchestrator.goal_coverage import (
        EXTRACTION_DETECTED, GoalCoverage, GoalRequirement, covering_tools)

    goal = GoalCoverage(extraction=EXTRACTION_DETECTED, requirements=[
        GoalRequirement(id="r1", type="supplier", status="covered", evidence_ids=["e1"]),
        GoalRequirement(id="r2", type="purchase_orders", status="uncovered")])
    store = EvidenceStore()
    store.add_from_tool_result(tool="get_supplier", arguments={"q": "BOSCH"},
                               result={"ok": True, "empty": True, "data": {}, "meta": {}})
    pack = goal.prompt_pack()
    note = goal.blocked_note(store)
    expected = sorted(covering_tools("purchase_orders"))
    return {
        "expected_tool": expected,
        "named_in_goal_pack": all(t in pack for t in expected),
        "named_in_blocked_note": all(t in note for t in expected),
        "already_used_tool_not_suggested": "get_supplier" not in note.split("Llama UNA")[-1],
        "verdict": (
            "PASS" if all(t in pack for t in expected) and all(t in note for t in expected)
            else "FAIL"
        ),
    }


def probe_calculation_path_grammar() -> dict[str, Any]:
    """FASE 8.1J — the only documented path used to be the whole-list one for
    count, so an indexed scalar path had to be invented: that is the shape that
    produces calc_unresolved."""
    from app.assistant.orchestrator.llm.agent_prompts import build_agent_system_prompt

    prompt = build_agent_system_prompt()
    counting_lines = [l for l in prompt.splitlines() if "count" in l.lower()]
    checks = {
        "roots_documented": "data|meta|arguments" in prompt,
        "array_index_example": "e1.data.items.0.cantidad" in prompt,
        "evidence_id_must_exist": "evidence_id que exista" in prompt,
        "no_figure_without_calculation": "varios" in prompt,
        "counting_lines": len(counting_lines),
    }
    return {**checks, "verdict": (
        "PASS" if (checks["roots_documented"] and checks["array_index_example"]
                   and checks["evidence_id_must_exist"] and checks["counting_lines"] <= 3)
        else "FAIL")}


def probe_claim_provenance() -> dict[str, Any]:
    """FASE 8.2 — evidence_ids se parseaba y se ignoraba: un claim podia citar e1
    mientras su cifra venia de e2. Arranca en observacion (se cuenta, no descarta)."""
    import os as _os

    from app.assistant.orchestrator.answer_verifier import verify_agent_answer
    from app.assistant.orchestrator.evidence_store import EvidenceStore

    store = EvidenceStore()
    store.add_from_tool_result(
        tool="get_ingresos", arguments={"codigo": "2404"},
        result={"ok": True, "empty": False, "meta": {},
                "data": {"items": [{"cantidad": 2}, {"cantidad": 1}]}})
    store.add_from_tool_result(
        tool="get_inventory", arguments={"codigo": "2404"},
        result={"ok": True, "empty": False, "meta": {}, "data": {"total_stock": 7}})

    def _run(eids: list[str]) -> Any:
        return verify_agent_answer(
            store=store,
            decision={"action": "final_answer", "draft_reply": "",
                      "claims": [{"kind": "dato", "text": "El stock total es 7 unidades.",
                                  "evidence_ids": eids}],
                      "calculations": []})

    prev = _os.environ.get("ANDES_ASSISTANT_PROVENANCE_ENFORCE")
    try:
        _os.environ["ANDES_ASSISTANT_PROVENANCE_ENFORCE"] = "0"
        miscited = _run(["e1"])
        correct = _run(["e2"])
        _os.environ["ANDES_ASSISTANT_PROVENANCE_ENFORCE"] = "1"
        enforced = _run(["e1"])
    finally:
        if prev is None:
            _os.environ.pop("ANDES_ASSISTANT_PROVENANCE_ENFORCE", None)
        else:
            _os.environ["ANDES_ASSISTANT_PROVENANCE_ENFORCE"] = prev
    return {
        "default_is_observation": not miscited.provenance_enforced,
        "miscited_flagged": miscited.provenance_violations == 1,
        "miscited_not_dropped_in_observation": miscited.dropped_claims == 0,
        "miscited_published_in_observation": "7" in (miscited.reply or ""),
        "correct_citation_clean": correct.provenance_violations == 0,
        "enforced_drops_the_claim": enforced.dropped_claims == 1,
        "enforced_does_not_publish": "7 unidades" not in (enforced.reply or ""),
        "verdict": (
            "PASS" if (not miscited.provenance_enforced
                       and miscited.provenance_violations == 1
                       and miscited.dropped_claims == 0
                       and correct.provenance_violations == 0
                       and enforced.dropped_claims == 1)
            else "FAIL"),
    }


def probe_real_truncation() -> dict[str, Any]:
    """FASE 8.2D — degradacion con un payload REAL del Gateway, no sintetico.

    probe_evidence_degradation prueba el mecanismo con 80 filas fabricadas. Eso
    demuestra que el codigo funciona, no que la ruta se alcance en produccion.
    Medido contra el Gateway real: get_supplier con una q amplia devuelve 5226
    caracteres, por encima de MAX_TOOL_RESULT_CHARS=4000. Este probe recorre la
    cadena entera —tool -> store -> pack -> verifier— con ese payload real y
    comprueba que la respuesta publicada solo cite cifras que sobrevivieron.
    """
    from app.assistant.orchestrator.answer_verifier import grounded_numbers
    from app.assistant.orchestrator.evidence_store import EvidenceStore
    from app.assistant.routes import invoke_gateway

    _, res = invoke_gateway(
        {"tool": "get_supplier", "arguments": {"q": "a"}, "actor_user": "albertadmin"})
    raw_chars = len(json.dumps(res.get("data"), ensure_ascii=False))
    store = EvidenceStore()
    item = store.add_from_tool_result(
        tool="get_supplier", arguments={"q": "a"}, result=res)
    pack_ok = True
    try:
        parsed = json.loads(store.prompt_pack())
        pack_ok = isinstance(parsed, list) and bool(parsed)
    except json.JSONDecodeError:
        pack_ok = False
    view = item.data_view if isinstance(item.data_view, dict) else {}
    rows = view.get("items") if isinstance(view.get("items"), list) else []
    numbers = grounded_numbers(store)
    # Ni una sola cifra citable puede venir de texto serializado: si el preview
    # crudo volviera, el tokenizador mineria el JSON y estos ids apareceran.
    blob = json.dumps(view, ensure_ascii=False)
    return {
        "raw_chars": raw_chars,
        "exceeds_cap": raw_chars > MAX_TOOL_RESULT_CHARS_LOCAL(),
        "degraded": bool(item.truncated),
        "shape_preserved": isinstance(rows, list),
        "rows_retained": len(rows),
        "omitted_rows": dict(item.omitted_rows),
        "paths_still_resolve": _path_ok(store),
        "pack_is_valid_json": pack_ok,
        "no_preview_key": "preview" not in view,
        "grounded_count": len(numbers),
        "view_within_cap": len(blob) <= MAX_TOOL_RESULT_CHARS_LOCAL(),
        "verdict": (
            "PASS" if (item.truncated and isinstance(rows, list) and rows
                       and _path_ok(store) and pack_ok and "preview" not in view
                       and len(blob) <= MAX_TOOL_RESULT_CHARS_LOCAL())
            else "FAIL"),
    }


def MAX_TOOL_RESULT_CHARS_LOCAL() -> int:
    from app.assistant.orchestrator.agent_config import MAX_TOOL_RESULT_CHARS

    return MAX_TOOL_RESULT_CHARS


def _path_ok(store: Any) -> bool:
    try:
        return isinstance(store.resolve_path("e1.data.items"), list)
    except Exception:  # noqa: BLE001
        return False


def _gateway_call(tool: str, args: dict[str, Any], *,
                  actor: str = "albertadmin") -> tuple[int, dict[str, Any]]:
    """Llamada al Gateway que sabe distinguir "roto" de "limitado".

    El Gateway limita a 60 peticiones por ventana de 60 s (ANDES_AGENT_RATE_LIMIT_*).
    El closure hace decenas de llamadas entre todos sus probes, asi que los
    ultimos chocaban con el limitador y reportaban FAIL. Un 429 NO es un fallo
    del producto: es la proteccion funcionando. Confundirlos hacia que el informe
    acusara a la capacidad de algo que hacia bien el limitador.

    Se reintenta una vez tras esperar; si sigue limitado, el probe lo dira en su
    veredicto en vez de fingir un fallo.
    """
    import time as _time

    from app.assistant.routes import invoke_gateway

    payload = {"tool": tool, "arguments": args, "actor_user": actor}
    status, body = invoke_gateway(payload)
    if status == 429:
        _time.sleep(RATE_LIMIT_BACKOFF_SECONDS)
        status, body = invoke_gateway(payload)
    return status, body


RATE_LIMIT_BACKOFF_SECONDS = 8.0
# Escalonado hasta cubrir la ventana completa de 60 s del limitador.
RATE_LIMIT_BACKOFFS = (5.0, 20.0, 45.0)


def _rate_limited(*responses: dict[str, Any]) -> bool:
    return any(str(r.get("error_code") or "") == "rate_limited" for r in responses)


def probe_contract_honesty() -> dict[str, Any]:
    """FASE 8.9 — lo que el sistema declara tiene que ser lo que exige.

    Los tres defectos que el A/B con LLM real destapo viven en esa frontera:
    un ancla exigida y no declarada (O01/O04 agotaron reintentos sin llamar la
    tool), y una tool sin renderizado de texto (V02 publico nombres de campos).
    """
    from app.assistant.orchestrator.catalog import ALLOWED_TOOLS
    from app.assistant.orchestrator.tool_contracts import (
        TOOL_CONTRACTS, format_contracts_for_prompt, inspect_arg_fields)

    rendered = format_contracts_for_prompt()
    undeclared = []
    silent_retry = []
    for tool, spec in TOOL_CONTRACTS.items():
        any_of = spec.get("any_of") or ()
        if not any_of:
            continue
        if "al menos uno de" not in "".join(
                l for l in rendered.splitlines() if l.startswith(f"{tool}:")):
            undeclared.append(tool)
        if not inspect_arg_fields(tool, {}):
            silent_retry.append(tool)

    src = (Path(__file__).resolve().parents[1]
           / "app" / "assistant" / "orchestrator" / "composer.py").read_text(encoding="utf-8")
    no_renderer = [t for t in ALLOWED_TOOLS if f'tool == "{t}"' not in src]

    # FASE 8.x — declarar el ancla en el prompt no basta si el modelo no puede
    # escribirla. Este probe comprobaba que el contrato DICE lo correcto; ahora
    # comprueba tambien que la cadena entera lo DEJA PASAR.
    from app.assistant.orchestrator.llm.plan_schema import (
        contract_schema_drift, format_drift)

    drift = contract_schema_drift()

    return {
        "tools_with_conditional_anchor": [t for t, s in TOOL_CONTRACTS.items()
                                          if s.get("any_of")],
        "anchors_not_declared_in_prompt": undeclared,
        "anchors_with_silent_retry": silent_retry,
        "tools_without_text_renderer": no_renderer,
        "contract_schema_drift": drift,
        "drift_by_side": {
            side: [f"{f['tool']}.{f['arg']}" for f in drift if f["side"] == side]
            for side in ("generation_schema", "normalizer", "validator", "contract")
        },
        "drift_diagnostic": format_drift(drift),
        "verdict": ("PASS" if not undeclared and not silent_retry
                    and not no_renderer and not drift else "FAIL"),
    }


def _explained_empty_reply(body: dict[str, Any]) -> bool:
    """El hecho tiene que llegar al TEXTO, no solo al payload.

    FASE 8.x — `compose_answer` cortocircuitaba en `empty` antes de llamar al
    formateador, asi que las ramas `not_found`/`no_oem_declared` eran codigo
    muerto en produccion aunque el ERP las emitiera. Un probe que mire solo el
    JSON habria dado PASS con el usuario recibiendo "no devolvio resultados".
    """
    from app.assistant.orchestrator.composer import compose_answer
    from app.assistant.orchestrator.normalizer import normalize_tool_result

    item = normalize_tool_result(200, body)
    item["tool"] = "get_equivalences"
    reply = compose_answer(
        plan={"steps": [{"step": 1, "tool": "get_equivalences"}]},
        evidence=[item])["reply"]
    return "OEM" in reply and "no devolvi" not in reply


def probe_orders_over_http() -> dict[str, Any]:
    """FASE 9.2 — ordenes de cliente por la CADENA REAL.

    Existir en disco no es estar entregado: Gateway y ERP son procesos aparte
    sin reloader, y en 8.6 una tool respondia tool_not_allowed por HTTP mientras
    todos los tests en proceso pasaban. Este probe es el que nota la diferencia.
    """
    from app.assistant.orchestrator.composer import compose_answer
    from app.assistant.orchestrator.normalizer import normalize_tool_result
    from app.assistant.routes import invoke_gateway

    def _call(args: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return _gateway_call("get_orders", args)

    status, base = _call({})
    data = base.get("data") or {}
    _, ventana = _call({"fecha_desde": "2026-07-01", "fecha_hasta": "2026-07-31"})
    _, con_anuladas = _call({"estados": ["pagada", "recibida", "anulada"]})
    mal_estado, _ = _call({"estados": ["inventado"]})
    mal_group, _ = _call({"group_by": "semana"})
    denegado, body_denegado = invoke_gateway(
        {"tool": "get_orders", "arguments": {}, "actor_user": "noexiste_usuario"})

    blob = json.dumps(base, ensure_ascii=False).lower()
    leaked = [k for k in ("vendedor", "direccion", "despacho", "observacion",
                          "usuario", "referencia", "cliente_id", "telefono",
                          "email", "rut", "password", "token") if k in blob]

    item = normalize_tool_result(status, base)
    item["tool"] = "get_orders"
    reply = compose_answer(plan={"steps": [{"step": 1, "tool": "get_orders"}]},
                           evidence=[item])["reply"]

    vent = (ventana.get("data") or {}).get("periodo") or {}
    base_ordenes = int(data.get("ordenes") or 0)
    anul_ordenes = int((con_anuladas.get("data") or {}).get("ordenes") or 0)
    return {
        "http_status": status,
        "reachable": status == 200 and bool(base.get("ok")),
        "classification": base.get("classification"),
        "ordenes": base_ordenes,
        "lineas": data.get("lineas"),
        "leaked_keys": leaked,
        "scope_always_declared": isinstance(data.get("periodo"), dict),
        "windowed_periodo": vent,
        "cancelled_excluded_by_default": bool(data.get("anuladas_excluidas")),
        # Nombrarlas tiene que cambiar la cifra: si no, la exclusion no existe.
        "naming_cancelled_changes_the_figure": anul_ordenes > base_ordenes,
        "state_breakdown_present": bool(data.get("ordenes_por_estado")),
        "bad_state_rejected": mal_estado == 400,
        "bad_group_by_rejected": mal_group == 400,
        "unknown_actor_denied": denegado in (401, 403) and not body_denegado.get("ok"),
        "reply_states_the_exclusion": "anuladas" in reply.lower(),
        "reply_states_its_scope": "sin filtro de fecha" in reply,
        "verdict": (
            "RATE_LIMITED" if _rate_limited(base)
            else "PASS" if (status == 200 and base.get("ok") and not leaked
                            and isinstance(data.get("periodo"), dict)
                            and data.get("anuladas_excluidas")
                            and anul_ordenes > base_ordenes
                            and mal_estado == 400 and mal_group == 400
                            and denegado in (401, 403)
                            and "anuladas" in reply.lower())
            else "FAIL"),
    }


def probe_artifact_integrity(out_dir: Path) -> dict[str, Any]:
    """FASE 8.x — que ningun artefacto en disco pueda mentir sobre su medicion.

    Dos trampas reales, y la primera me atrapo a mi en esta misma sesion: lei
    `fase81_final_report.an0-pv0.json` como si fuera una corrida determinista
    fresca cuando era el informe LLM de la tercera A/B, tres horas mas viejo.

    1. **Esquema de brazo supersedido.** Al anadir la dimension `pr`, los
       ficheros `an0-pv0` / `an1-pv0` dejaron de corresponder a ningun brazo que
       el harness produzca, pero siguen en el mismo directorio con el mismo
       prefijo. Son historia legitima; lo que no puede pasar es que se confundan
       con los vigentes.

    2. **Validez indeterminable.** `measurement_valid` se anadio DESPUES de las
       corridas del 2026-09-20, asi que el unico brazo valido —an0-pv0-pr0, 74/76—
       no lo declara, mientras los dos invalidos si dicen False. Un campo ausente
       leido como "valido" es exactamente el error que el guard existe para
       evitar. Cuando falta, se RECALCULA desde los runs del propio artefacto
       contando `llm_unavailable`: es una medicion, no una suposicion.
    """
    vigentes = set(_all_arm_ids())
    rows: list[dict[str, Any]] = []
    for path in sorted(out_dir.glob("fase81_final_report*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rows.append({"file": path.name, "error": "ilegible"})
            continue
        bench = data.get("benchmark") or {}
        arm = (data.get("arm") or {}).get("arm")
        valid = bench.get("measurement_valid")
        origen = "declarado"
        if data.get("llm_executed") and bench and valid is None:
            runs = out_dir / f"fase81g_runs_llm.{arm}.jsonl"
            try:
                vacias = sum(
                    1 for line in runs.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                    and json.loads(line).get("fallback_reason") == "llm_unavailable")
                valid, origen = vacias == 0, f"recalculado ({vacias} vacias)"
            except (OSError, ValueError, TypeError):
                origen = "indeterminable"
        rows.append({
            "file": path.name,
            "generated_at": data.get("generated_at"),
            "arm": arm,
            "llm_executed": bool(data.get("llm_executed")),
            "measurement_valid": valid,
            "validity_source": origen if data.get("llm_executed") and bench else "n/a",
            "scheme": ("vigente" if arm in vigentes
                       else "determinista" if ".det." in path.name
                       else "SUPERSEDIDO"),
            "pass": bench.get("pass"),
            "n": bench.get("n"),
        })

    supersedidos = [r["file"] for r in rows if r.get("scheme") == "SUPERSEDIDO"]
    # Indeterminable solo importa en el esquema VIGENTE: un artefacto de un
    # dataset anterior cuyos runs ya no existen es historia, y hacer fallar el
    # probe para siempre por el lo convertiria en una alarma que nadie mira —
    # que es justo la clase de metrica que este bloque lleva once defectos
    # eliminando.
    indeterminables = [r["file"] for r in rows
                       if r.get("validity_source") == "indeterminable"
                       and r.get("scheme") == "vigente"]
    # Los supersedidos NO son un fallo: son historia, y listarlos es justo lo que
    # impide confundirlos. Lo que si es un fallo es un informe LLM vigente cuya
    # validez no se pueda establecer ni leyendo sus propios runs.
    vigentes_llm = [r for r in rows
                    if r.get("scheme") == "vigente" and r.get("llm_executed")]
    ciegos = [r["file"] for r in vigentes_llm if r.get("measurement_valid") is None]
    return {
        "artifacts": rows,
        "superseded_arm_scheme": supersedidos,
        "validity_indeterminable": indeterminables,
        "current_llm_reports_without_validity": ciegos,
        "current_valid_arms": sorted(
            {r["arm"] for r in vigentes_llm if r.get("measurement_valid")}),
        "verdict": "PASS" if not ciegos and not indeterminables else "FAIL",
    }


def probe_period_resolution() -> dict[str, Any]:
    """FASE 8.x — la ventana derivada llega al ERP y cambia la respuesta.

    Lo que hay que demostrar por HTTP, no en proceso: que "las ventas de enero a
    marzo de 2026" termina consultando ESA ventana y devolviendo el cero
    verdadero, en vez de los dos documentos de 2026-04-07 que devolvia la
    llamada sin filtro. Y que con el flag apagado nada cambia.
    """
    import importlib
    import os as _os

    from app.assistant.orchestrator import agent_config
    from app.assistant.orchestrator.composer import compose_answer
    from app.assistant.orchestrator.normalizer import normalize_tool_result
    from app.assistant.orchestrator.tool_contracts import normalize_agent_args

    pregunta = "Muestrame las ventas de enero a marzo de 2026."
    previo = _os.environ.get("ANDES_ASSISTANT_PERIOD_RESOLUTION")
    salida: dict[str, Any] = {}
    try:
        for flag in ("0", "1"):
            _os.environ["ANDES_ASSISTANT_PERIOD_RESOLUTION"] = flag
            importlib.reload(agent_config)
            args = normalize_agent_args("get_sales", {}, user_message=pregunta)
            status, body = _gateway_call("get_sales", args)
            item = normalize_tool_result(status, {"ok": body.get("ok"),
                                                  "tool": "get_sales",
                                                  "classification": body.get("classification"),
                                                  "data": body.get("data"),
                                                  "meta": body.get("meta") or {}})
            item["tool"] = "get_sales"
            reply = compose_answer(plan={"steps": [{"step": 1, "tool": "get_sales"}]},
                                   evidence=[item])["reply"]
            salida[f"pr{flag}"] = {
                "arguments": args,
                "documentos": (item.get("data") or {}).get("documentos"),
                "periodo": (item.get("data") or {}).get("periodo"),
                "reply": reply[:160],
            }
    finally:
        if previo is None:
            _os.environ.pop("ANDES_ASSISTANT_PERIOD_RESOLUTION", None)
        else:
            _os.environ["ANDES_ASSISTANT_PERIOD_RESOLUTION"] = previo
        importlib.reload(agent_config)

    off, on = salida.get("pr0") or {}, salida.get("pr1") or {}
    # Apagado: sin fechas, y el ERP devuelve los documentos de abril.
    off_unfiltered = not off.get("arguments")
    # Encendido: la ventana viaja y el periodo consultado NO tiene ventas.
    on_windowed = on.get("arguments") == {"fecha_desde": "2026-01-01",
                                          "fecha_hasta": "2026-03-31"}
    on_truthful = on.get("documentos") in (0, None) and "no devolvi" in (on.get("reply") or "")
    return {
        "question": pregunta,
        "flag_off": off,
        "flag_on": on,
        "off_calls_unfiltered": off_unfiltered,
        "on_sends_the_window": on_windowed,
        "on_answer_is_truthful": on_truthful,
        "april_documents_no_longer_answer_a_q1_question":
            bool(off.get("documentos")) and not on.get("documentos"),
        "verdict": ("RATE_LIMITED" if _rate_limited({"data": off}, {"data": on})
                    else "PASS" if (off_unfiltered and on_windowed and on_truthful)
                    else "FAIL"),
    }


def probe_equivalences_over_http() -> dict[str, Any]:
    """FASE 8.8 — cruce OEM por la cadena real, con el catalogo de produccion.

    Lo que hay que demostrar es que un usuario que teclea el codigo de forma
    natural lo encuentra: "0 280 751 089" y "0280751089" son el mismo Bosch, y
    con igualdad literal el 72% de los datos seria inalcanzable.
    """
    from app.assistant.routes import invoke_gateway

    def _call(args: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return _gateway_call("get_equivalences", args)

    _, dashed = _call({"oem": "038-1701225"})
    _, plain = _call({"oem": "0381701225"})
    _, by_code = _call({"codigo": "2404"})
    no_anchor_status, _ = _call({"marca": "BOSCH"})
    short_status, _ = _call({"oem": "ab"})
    _, missing = _call({"codigo": "ZZZNOEXISTE"})
    # FASE 8.x — la via OEM no declaraba su vacio, asi que "ese OEM no existe"
    # era indistinguible de "no busque nada". Y con marca filtrando NO puede
    # declararlo: el OEM podria existir fuera de esa marca.
    _, missing_oem = _call({"oem": "ZZZNOEXISTE999"})
    _, ambiguous = _call({"oem": "ZZZNOEXISTE999", "marca": "BOSCH"})

    dashed_codes = sorted(i["codigo"] for i in (dashed.get("data") or {}).get("items") or [])
    plain_codes = sorted(i["codigo"] for i in (plain.get("data") or {}).get("items") or [])
    blob = json.dumps(by_code, ensure_ascii=False).lower()
    leaked = [k for k in ("email", "telefono", "direccion", "rut", "password",
                          "token", "precio", "costo", "margen") if k in blob]
    apps = [a for i in (by_code.get("data") or {}).get("items") or []
            for a in (i.get("aplicaciones") or [])]

    return {
        "separator_insensitive": bool(dashed_codes) and dashed_codes == plain_codes,
        "codes_for_dashed_oem": dashed_codes[:4],
        "equivalents_by_code": len((by_code.get("data") or {}).get("items") or []),
        "applications_present": apps[:3],
        "anchor_required": no_anchor_status == 400,
        "short_key_rejected": short_status == 400,
        "missing_code_is_a_fact": bool((missing.get("data") or {}).get("not_found")),
        "missing_oem_is_a_fact": bool((missing_oem.get("data") or {}).get("not_found")),
        "ambiguous_empty_is_not_claimed": not (ambiguous.get("data") or {}).get("not_found"),
        "empty_is_explained_in_the_reply": _explained_empty_reply(missing_oem),
        "leaked_keys": leaked,
        "verdict": (
            "RATE_LIMITED" if _rate_limited(dashed, plain, by_code, missing)
            else "PASS" if (dashed_codes and dashed_codes == plain_codes
                            and no_anchor_status == 400 and short_status == 400
                            and (missing.get("data") or {}).get("not_found")
                            and (missing_oem.get("data") or {}).get("not_found")
                            and not (ambiguous.get("data") or {}).get("not_found")
                            and _explained_empty_reply(missing_oem)
                            and not leaked)
            else "FAIL"),
    }


def probe_limit_coherence() -> dict[str, Any]:
    """FASE 8.8 — un plan valido en una capa no puede morir en la siguiente.

    Medido: el orquestador admitia limit=50 en cuatro tools y el Gateway las
    rechazaba con 400, asi que un plan correcto producia invalid_args en la
    frontera exterior. invalid_args es uno de los ejes que mide el benchmark.
    """
    import sys as _sys
    from pathlib import Path as _Path

    # Al FINAL, nunca al principio: andes_agent/ tiene su propio paquete
    # `tests`, y anteponerlo eclipsa el tests/ del proyecto. Sintoma medido:
    # los 41 modulos de la suite dejaron de importar dentro del closure mientras
    # la suite seguia pasando por separado.
    root = str(_Path(__file__).resolve().parents[1] / "andes_agent")
    if root not in _sys.path:
        _sys.path.append(root)
    from andes_agent.schemas import validate_tool_arguments

    from app.assistant.orchestrator.arg_schema import validate_tool_args
    from app.assistant.orchestrator.catalog import ALLOWED_TOOLS

    args = {
        "search_catalog": {"q": "filtro"}, "get_product": {"codigo": "2404"},
        "get_inventory": {"codigo": "2404"},
        "check_stock": {"items": [{"codigo": "2404", "cantidad": 1}]},
        "get_stock_movements": {"codigo": "2404"},
        "get_ingresos": {"codigo": "2404"}, "get_purchase_orders": {},
        "get_customer": {"q": "a"}, "get_supplier": {"q": "a"},
        "get_sales": {}, "get_equivalences": {"oem": "038-1701225"},
        "get_dashboard_kpis": {},
    }

    def _ceiling(fn: Any, tool: str) -> int:
        highest = 0
        for candidate in range(1, 101):
            try:
                fn(tool, {**args.get(tool, {}), "limit": candidate})
                highest = candidate
            except Exception:  # noqa: BLE001
                pass
        return highest

    offenders = []
    for tool in sorted(ALLOWED_TOOLS):
        inner = _ceiling(validate_tool_args, tool)
        outer = _ceiling(validate_tool_arguments, tool)
        if inner > outer:
            offenders.append({"tool": tool, "orchestrator": inner, "gateway": outer})
    return {
        "tools_checked": len(ALLOWED_TOOLS),
        "offenders": offenders,
        "verdict": "PASS" if not offenders else "FAIL",
    }


def probe_sales_over_http() -> dict[str, Any]:
    """FASE 8.7 — get_sales por la CADENA REAL, no en proceso.

    Existir en disco no es estar entregado: Gateway y ERP son procesos aparte sin
    reloader, y durante 8.6 la tool respondia tool_not_allowed por HTTP mientras
    todos los tests en proceso pasaban. Este probe es el que nota esa diferencia.
    """
    from app.assistant.routes import invoke_gateway

    def _call(args: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return _gateway_call("get_sales", args)

    status, ok_res = _call({"group_by": "mes"})
    data = ok_res.get("data") or {}
    blob = json.dumps(ok_res, ensure_ascii=False).lower()
    leaked = [k for k in ("email", "telefono", "direccion", "rut", "password",
                          "token", "api_key", "pago_referencia")
              if k in blob]

    purchase_status, _ = _call({"tipos": ["orden_compra"]})
    badgroup_status, _ = _call({"group_by": "semana"})
    # FASE 8.x — el alcance tiene que estar SIEMPRE en la evidencia. Medido en
    # V02: sin filtro de fecha el payload callaba su ventana, y la respuesta
    # presento documentos de abril como el trimestre preguntado.
    _, unfiltered = _call({})
    _, windowed = _call({"fecha_desde": "2026-01-01", "fecha_hasta": "2026-03-31"})
    unf_periodo = (unfiltered.get("data") or {}).get("periodo")
    win_periodo = (windowed.get("data") or {}).get("periodo")
    scope_always_declared = (
        isinstance(unf_periodo, dict) and isinstance(win_periodo, dict)
        and unf_periodo.get("desde") is None and unf_periodo.get("hasta") is None
        and win_periodo.get("desde") == "2026-01-01")
    denied_status, denied = invoke_gateway(
        {"tool": "get_sales", "arguments": {}, "actor_user": "noexiste_usuario"})

    return {
        "http_status": status,
        "reachable": status == 200 and bool(ok_res.get("ok")),
        "classification": ok_res.get("classification"),
        "leaked_keys": leaked,
        "nets_credit_notes": bool(data.get("neto_notas_credito")),
        "declares_partial_detail": "detalle_parcial" in data,
        "has_series": isinstance(data.get("series"), list),
        "purchase_type_rejected": purchase_status == 400,
        "bad_group_by_rejected": badgroup_status == 400,
        "unknown_actor_denied": denied_status in (401, 403)
        and not denied.get("ok"),
        "scope_always_declared": scope_always_declared,
        "unfiltered_periodo": unf_periodo,
        "windowed_periodo": win_periodo,
        "verdict": (
            "RATE_LIMITED" if _rate_limited(ok_res)
            else "PASS" if (status == 200 and ok_res.get("ok") and not leaked
                            and data.get("neto_notas_credito") and scope_always_declared
                            and purchase_status == 400 and badgroup_status == 400
                            and denied_status in (401, 403))
            else "FAIL"),
    }


def probe_finance_redaction() -> dict[str, Any]:
    """FASE 8.7 — decir que se oculto el dinero, en vez de ocultarlo callando.

    finance_redacted solo se calculaba para get_dashboard_kpis, asi que en
    ventas, ingresos u OC el sistema omitia montos en silencio: el usuario no
    podia distinguir "no te lo muestro" de "no hubo", que es la confusion
    null/cero que el sistema existe para impedir.
    """
    from app.assistant.orchestrator.plan_validator import validate_plan
    from app.assistant.orchestrator.tool_runner import run_plan_steps
    from app.assistant.routes import invoke_gateway

    out: dict[str, Any] = {}
    for tool, args in (("get_sales", {}), ("get_ingresos", {"codigo": "2404"}),
                       ("get_purchase_orders", {})):
        plan = validate_plan({"steps": [{"step": 1, "tool": tool, "arguments": args}]})
        evidence, _ = run_plan_steps(plan, actor_user="albertadmin",
                                     conversation_id="probe-87",
                                     invoke_fn=invoke_gateway)
        item = evidence[0]
        out[tool] = {"ok": bool(item.get("ok")),
                     "finance_redacted": item.get("finance_redacted")}
    signalled = [t for t, v in out.items() if v["finance_redacted"] is not None]
    return {
        "per_tool": out,
        "tools_signalling": signalled,
        # Lo que importa no es que sea True o False, sino que deje de ser None:
        # un None significa "nadie lo calculo".
        "verdict": "PASS" if len(signalled) == len(out) else "FAIL",
    }


def probe_sales_capability() -> dict[str, Any]:
    """FASE 8.6 — get_sales contra la BASE REAL, no contra un doble.

    Lo que hay que demostrar no es que la tool responda, sino que responda lo
    CORRECTO en los dos sitios donde una tool de ventas ingenua se equivoca:

    - mezclar tipos: ventas_documentos guarda ventas Y compras en la misma tabla;
    - ignorar devoluciones: en la base real el bruto es 5 unidades / 132 400 y las
      notas de credito son 5 / 132 400, asi que el neto es CERO. Una tool que las
      ignorase diria "vendiste 5 unidades" cuando todo fue devuelto.
    """
    from app import create_app
    from app.internal_agent.m2m import InternalAuthError
    from app.internal_agent.sales import (
        PURCHASE_TIPOS, QUOTE_TIPOS, SALE_TIPOS, get_public_sales,
        validate_sales_args)

    app = create_app()
    with app.app_context():
        net, _ = get_public_sales(include_finance=True, group_by="mes")
        gross_units = net["unidades"] + net["notas_credito"]["unidades"]

    purchase_blocked = False
    try:
        validate_sales_args({"tipos": list(PURCHASE_TIPOS)})
    except InternalAuthError:
        purchase_blocked = True

    return {
        "default_tipos": list(SALE_TIPOS),
        "quotes_excluded_by_default": not any(t in QUOTE_TIPOS for t in SALE_TIPOS),
        "purchases_rejected": purchase_blocked,
        "gross_units": gross_units,
        "credit_note_units": net["notas_credito"]["unidades"],
        "net_units": net["unidades"],
        "nets_credit_notes": bool(net.get("neto_notas_credito")),
        "declares_partial_detail": "detalle_parcial" in net,
        "has_series": "series" in net,
        "verdict": (
            "PASS" if (purchase_blocked and net.get("neto_notas_credito")
                       and "cotizacion" not in SALE_TIPOS
                       and net["unidades"] == gross_units - net["notas_credito"]["unidades"])
            else "FAIL"),
    }


def probe_conditional_analysis() -> dict[str, Any]:
    """FASE 8.6 — el analisis solo viaja en un turno que lo pida.

    Cargarlo siempre costo +17% de tokens por turno, tumbo T09 contra el techo y
    cebo la lectura temporal. Aqui se comprueba que un turno normal recibe el
    prompt y el schema intactos aunque el flag este encendido.
    """
    import os as _os

    from app.assistant.orchestrator.analysis import analytical_intent
    from app.assistant.orchestrator.llm.agent_prompts import build_agent_system_prompt
    from app.assistant.orchestrator.llm.plan_schema import (
        AGENT_DECISION_JSON_SCHEMA, agent_decision_response_format)

    prev = _os.environ.get("ANDES_ASSISTANT_ANALYSIS_ENABLED")
    _os.environ["ANDES_ASSISTANT_ANALYSIS_ENABLED"] = "1"
    try:
        plain = "Revisa los movimientos del 2404 y despues dime cuanto stock hay."
        analytic = "Cuanto me faltaria comprar del 2404 para cubrir tres meses?"
        plain_prompt = build_agent_system_prompt(analytical=analytical_intent(plain))
        analytic_prompt = build_agent_system_prompt(analytical=analytical_intent(analytic))
        plain_schema = agent_decision_response_format(
            analytical=analytical_intent(plain))["json_schema"]["schema"]
        analytic_schema = agent_decision_response_format(
            analytical=analytical_intent(analytic))["json_schema"]["schema"]
    finally:
        if prev is None:
            _os.environ.pop("ANDES_ASSISTANT_ANALYSIS_ENABLED", None)
        else:
            _os.environ["ANDES_ASSISTANT_ANALYSIS_ENABLED"] = prev

    # FASE 8.x — "ensanchado" se comprobaba por IDENTIDAD (`is not BASE`), y una
    # copia profunda que no ensanchara NADA habria pasado igual. El defecto que
    # este probe existe para atrapar es justo ese: en 8.5 el validador aceptaba
    # peldanos que el schema de generacion no podia emitir, y la corrida reporto
    # "cero usos de la escalera" como si midiera al modelo. Ahora se comprueba
    # el CONTENIDO, y contra la misma fuente de verdad que usa el validador.
    from app.assistant.orchestrator.analysis import ASSUMPTION_KINDS, VALID_BASES

    aprops = analytic_schema["properties"]
    rungs = set(aprops["claims"]["items"]["properties"]["kind"].get("enum") or [])
    ops = set(aprops["calculations"]["items"]["properties"]["op"].get("enum") or [])
    asm = aprops.get("assumptions") or {}
    aitems = asm.get("items") or {}
    akinds = set((aitems.get("properties") or {}).get("kind", {}).get("enum") or [])
    abases = set((aitems.get("properties") or {}).get("basis", {}).get("enum") or [])
    ladder = {"calculo", "supuesto", "proyeccion", "recomendacion"}
    emittable = {
        "ladder_rungs_missing": sorted(ladder - rungs),
        "mul_emittable": "mul" in ops,
        "assumptions_property": bool(asm),
        "assumption_kinds_missing": sorted(set(ASSUMPTION_KINDS) - akinds),
        "bases_mismatch": sorted(set(VALID_BASES) ^ abases),
        "assumption_ids_required": "assumption_ids" in (
            aprops["claims"]["items"].get("required") or []),
    }
    widened = (not emittable["ladder_rungs_missing"] and emittable["mul_emittable"]
               and emittable["assumptions_property"]
               and not emittable["assumption_kinds_missing"]
               and not emittable["bases_mismatch"]
               and emittable["assumption_ids_required"])
    # Y el turno normal no puede haber heredado nada de eso.
    plain_rungs = set(plain_schema["properties"]["claims"]["items"][
        "properties"]["kind"].get("enum") or [])
    plain_clean = not (plain_rungs & ladder) and "assumptions" not in plain_schema["properties"]

    return {
        "plain_is_analytical": analytical_intent(plain),
        "analytic_is_analytical": analytical_intent(analytic),
        "plain_schema_untouched": plain_schema is AGENT_DECISION_JSON_SCHEMA,
        "plain_schema_has_no_ladder": plain_clean,
        "analytic_schema_widened": analytic_schema is not AGENT_DECISION_JSON_SCHEMA,
        "analytic_schema_really_emits_the_ladder": widened,
        "emittable_detail": emittable,
        "plain_prompt_chars": len(plain_prompt),
        "analytic_prompt_chars": len(analytic_prompt),
        "token_tax_on_plain_turns": len(analytic_prompt) - len(plain_prompt),
        "verdict": (
            "PASS" if (not analytical_intent(plain) and analytical_intent(analytic)
                       and plain_schema is AGENT_DECISION_JSON_SCHEMA and plain_clean
                       and analytic_schema is not AGENT_DECISION_JSON_SCHEMA and widened
                       and len(plain_prompt) < len(analytic_prompt))
            else "FAIL"),
    }


def probe_analysis_ladder() -> dict[str, Any]:
    """FASE 8.5 — proyeccion sobre evidencia REAL, y sus vectores de invencion.

    Con datos del Gateway: se comprueba que la escalera completa se publica con
    sus etiquetas, y que las cuatro formas de colar una cifra inventada por la
    via del supuesto siguen cerradas.
    """
    from app.assistant.orchestrator.agent_schema import (
        AgentDecisionError, validate_agent_decision)
    from app.assistant.orchestrator.analysis import AssumptionError, validate_assumptions
    from app.assistant.orchestrator.answer_verifier import verify_agent_answer
    from app.assistant.orchestrator.evidence_store import EvidenceStore
    from app.assistant.routes import invoke_gateway

    question = "Con estas ventas, cuanto stock deberia tener para dos meses?"
    store = EvidenceStore()
    for tool, args in (("get_inventory", {"codigo": "2404"}),
                       ("get_stock_movements", {"codigo": "2404"})):
        _, res = invoke_gateway(
            {"tool": tool, "arguments": args, "actor_user": "albertadmin"})
        store.add_from_tool_result(tool=tool, arguments=args, result=res)

    stock = float((store.items[0].data_view or {}).get("total_stock") or 0)
    rows = (store.items[1].data_view or {}).get("items") or []
    qty = float(abs((rows[0] or {}).get("cantidad") or 0)) if rows else 0.0
    need = qty * 2.0
    gap = need - stock

    decision = {
        "action": "final_answer", "draft_reply": "",
        "assumptions": [{"id": "a1", "kind": "horizon_months", "value": 2,
                         "basis": "user_request"}],
        "calculations": [
            {"id": "c1", "op": "mul",
             "inputs": ["e2.data.items.0.cantidad", "a1.value"],
             "result": qty * 2.0 if qty else 0.0},
            {"id": "c2", "op": "diff", "inputs": ["c1", "e1.data.total_stock"],
             "result": (qty * 2.0) - stock},
        ],
        "claims": [
            {"kind": "dato", "text": f"El stock actual del 2404 es {int(stock)} unidades.",
             "evidence_ids": ["e1"]},
            {"kind": "supuesto", "text": "", "assumption_ids": ["a1"]},
            {"kind": "proyeccion",
             "text": f"Para dos meses necesitarias {int(need)} unidades.",
             "assumption_ids": ["a1"], "evidence_ids": ["e2"]},
            {"kind": "recomendacion",
             "text": f"Podrias evaluar una reposicion cercana a {int(gap)} unidades.",
             "evidence_ids": ["e1", "e2"]},
        ],
    }
    out = verify_agent_answer(
        store=store,
        decision=validate_agent_decision(decision, user_message=question))

    blocked = 0
    vectors = [
        [{"id": "a1", "kind": "horizon_months", "value": 7, "basis": "user_request"}],
        [{"id": "a1", "kind": "horizon_months", "value": 5, "basis": "default"}],
        [{"id": "a1", "kind": "demanda_mensual", "value": 500, "basis": "default"}],
        [{"id": "a1", "kind": "horizon_months", "value": 9000, "basis": "default"}],
    ]
    for bad in vectors:
        try:
            validate_assumptions(bad, question=question)
        except AssumptionError:
            blocked += 1

    no_assumption = dict(decision)
    no_assumption["claims"] = [
        {**c, "assumption_ids": []} if c["kind"] == "proyeccion" else c
        for c in decision["claims"]]
    projection_text = next(c["text"] for c in decision["claims"]
                           if c["kind"] == "proyeccion")
    try:
        loose = verify_agent_answer(
            store=store,
            decision=validate_agent_decision(no_assumption, user_message=question))
        # Se comprueba la FRASE, no el digito: "4" vive dentro de "2404" y un
        # substring suelto daria un falso negativo sobre el codigo del producto.
        projection_needs_assumption = (
            projection_text not in loose.reply and loose.dropped_claims > 0)
    except AgentDecisionError:
        projection_needs_assumption = True

    labels = [label for label in ("DATOS:", "SUPUESTOS:", "PROYECCI", "RECOMENDACI")
              if label in out.reply]
    return {
        "ladder_failures": int(out.failures),
        "dropped_claims": int(out.dropped_claims),
        "labels_present": labels,
        "invention_vectors_blocked": f"{blocked}/{len(vectors)}",
        "projection_requires_assumption": projection_needs_assumption,
        "verdict": (
            "PASS" if (out.failures == 0 and len(labels) == 4
                       and blocked == len(vectors) and projection_needs_assumption)
            else "FAIL"),
    }


def probe_answer_view() -> dict[str, Any]:
    """FASE 8.4 — la vista estructurada con datos REALES del Gateway.

    Lo que hay que demostrar no es que se pinten tarjetas, sino que la tarjeta no
    puede convertirse en una segunda verdad: cada valor proyectado tiene que
    existir en la evidencia verificada, y ningun campo fuera de la lista blanca
    puede colarse. Se prueba con get_supplier de busqueda amplia porque es el
    unico payload real que ademas DEGRADA, asi que comprueba de paso que una
    lista truncada lo confiesa en vez de aparentar estar completa.
    """
    from app.assistant.orchestrator.answer_view import build_answer_view
    from app.assistant.orchestrator.evidence_store import EvidenceStore
    from app.assistant.routes import invoke_gateway

    store = EvidenceStore()
    for tool, args in (("get_inventory", {"codigo": "2404"}),
                       ("get_supplier", {"q": "a"})):
        _, res = invoke_gateway(
            {"tool": tool, "arguments": args, "actor_user": "albertadmin"})
        store.add_from_tool_result(tool=tool, arguments=args, result=res)

    view = build_answer_view(store).as_dict()
    blob = json.dumps(view, ensure_ascii=False)
    evidence_blob = json.dumps(
        [i.data_view for i in store.items], ensure_ascii=False)

    values: list[str] = []
    for block in view["blocks"]:
        for field in block["summary"]:
            values.append(str(field["value"]))
        for card in block["cards"]:
            values.append(str(card["title"]))
            for field in card["fields"]:
                values.append(str(field["value"]))
    outside = [v for v in values if v and v not in evidence_blob]
    leaked = [k for k in ("token", "password", "api_key", "@", "email", "telefono")
              if k in blob]
    truncated = [b for b in view["blocks"] if b["truncated"]]
    every_card_traced = all(
        c.get("evidence_id") for b in view["blocks"] for c in b["cards"])
    return {
        "blocks": len(view["blocks"]),
        "cards": sum(len(b["cards"]) for b in view["blocks"]),
        "values_outside_evidence": outside[:5],
        "leaked_keys": leaked,
        "every_card_carries_provenance": every_card_traced,
        "truncation_declared": bool(truncated),
        "verdict": (
            "PASS" if (view["blocks"] and not outside and not leaked
                       and every_card_traced and truncated)
            else "FAIL"),
    }


def probe_answer_sufficiency() -> dict[str, Any]:
    """FASE 8.3 — con evidencia REAL del Gateway, distinguir util de vacuo.

    Se usan las dos respuestas literales observadas con LLM real sobre la misma
    pregunta y la misma evidencia: F01 cito cantidades y fechas, C07 describio la
    forma de los datos. Si la metrica no las separa, no mide lo que dice medir.
    Y se comprueba la direccion peligrosa: una cifra inventada NO puede subir la
    suficiencia, porque solo cuentan valores que estan en la evidencia.
    """
    from app.assistant.orchestrator.answer_sufficiency import analyze_sufficiency
    from app.assistant.orchestrator.evidence_store import EvidenceStore
    from app.assistant.orchestrator.goal_coverage import GoalCoverage
    from app.assistant.routes import invoke_gateway

    question = "Movimientos de stock del 2404"
    store = EvidenceStore()
    _, res = invoke_gateway({"tool": "get_stock_movements",
                             "arguments": {"codigo": "2404"},
                             "actor_user": "albertadmin"})
    store.add_from_tool_result(
        tool="get_stock_movements", arguments={"codigo": "2404"}, result=res)
    goal = GoalCoverage.from_message(question)
    goal.refresh(store)

    def _verdict(reply: str) -> str:
        return analyze_sufficiency(store=store, goal=goal, reply=reply,
                                   question=question).verdict

    concrete = _verdict("DATOS:\n- El producto 2404 tuvo un ingreso de 2 unidades "
                        "el 2026-07-31 en Bodega 1.")
    vague = _verdict("DATOS:\n- Movimientos de stock para el producto 2404 incluyen "
                     "ingresos y ajustes en Bodega 1 con marca BOSCH.")
    invented = _verdict("DATOS:\n- Hubo 8888 movimientos el 1999-01-01.")
    empty = _verdict("")
    return {
        "evidence_rows": len((store.items[0].data_view or {}).get("items") or []),
        "concrete_answer": concrete,
        "vague_answer": vague,
        "invented_figures": invented,
        "empty_answer": empty,
        "separates_useful_from_vacuous": concrete == "sufficient" and vague == "insufficient",
        "invention_cannot_buy_sufficiency": invented == "insufficient",
        "verdict": (
            "PASS" if (concrete == "sufficient" and vague == "insufficient"
                       and invented == "insufficient" and empty == "insufficient")
            else "FAIL"),
    }


def probe_prompt_economics() -> dict[str, Any]:
    """FASE 8.2D — de que esta hecho el prompt y que parte se reenvia.

    El AgentLoop reconstruye system+user en CADA decision. Medir la composicion
    dice donde esta el coste antes de intentar optimizarlo: si el grueso fuera la
    evidencia, la palanca seria no reenviarla; si es el system prompt, la palanca
    es otra. Sin este numero, 8.3 seria una apuesta.
    """
    from app.assistant.orchestrator.agent_config import (
        CHARS_PER_TOKEN, MAX_AGENT_STEPS, TOKEN_BUDGET_FALLBACK,
        budget_snapshot, decisions_affordable)
    from app.assistant.orchestrator.evidence_store import EvidenceStore
    from app.assistant.orchestrator.llm.agent_prompts import (
        build_agent_system_prompt, build_agent_user_prompt)
    from app.assistant.routes import invoke_gateway

    system_chars = len(build_agent_system_prompt())
    store = EvidenceStore()

    def _user() -> int:
        return len(build_agent_user_prompt(
            "Movimientos y stock actual del 2404.", evidence_pack=store.prompt_pack(),
            memory_note=None, context_note=None, loop_note=None,
            remaining_calls=4, goal_note=None))

    steps = [{"decision": 1, "system_chars": system_chars, "user_chars": _user()}]
    for tool, args in (("get_stock_movements", {"codigo": "2404"}),
                       ("get_inventory", {"codigo": "2404"})):
        _, res = invoke_gateway(
            {"tool": tool, "arguments": args, "actor_user": "albertadmin"})
        store.add_from_tool_result(tool=tool, arguments=args, result=res)
        steps.append({"decision": len(steps) + 1, "system_chars": system_chars,
                      "user_chars": _user()})
    total_chars = sum(s["system_chars"] + s["user_chars"] for s in steps)
    system_total = sum(s["system_chars"] for s in steps)
    worst, best = decisions_affordable()
    return {
        "steps": steps,
        "system_prompt_chars": system_chars,
        "total_prompt_chars_3_decisions": total_chars,
        "system_share_pct": round(100.0 * system_total / max(1, total_chars), 1),
        "evidence_share_pct": round(
            100.0 * (total_chars - system_total - 3 * steps[0]["user_chars"])
            / max(1, total_chars), 1),
        "modelled_tokens_3_decisions": int(total_chars / CHARS_PER_TOKEN),
        "token_budget": TOKEN_BUDGET_FALLBACK,
        "decisions_affordable_worst": worst,
        "decisions_affordable_best": best,
        "max_agent_steps": MAX_AGENT_STEPS,
        "steps_exceed_budget": bool(budget_snapshot().get("steps_exceed_budget")),
        # No es un veredicto de correccion: es la fotografia que decide 8.3.
        "verdict": "PASS" if system_chars > 0 and total_chars > 0 else "FAIL",
    }


def probe_evidence_degradation() -> dict[str, Any]:
    """FASE 8.2C — degradar evidencia no puede inventar cifras ni romper paths.

    El truncado anterior emitia {"truncated": True, "preview": "<json crudo>"}:
    los paths morian, el tokenizador minaba el texto del preview y los valores
    que no cabian dejaban de ser citables.
    """
    from app.assistant.orchestrator.answer_verifier import (
        CollectionTruncated, grounded_numbers, recompute_calculation)
    from app.assistant.orchestrator.evidence_store import EvidenceStore

    data = {"items": [{"codigo": f"P{i:04d}", "cantidad": 1000 + i, "obs": "Z" * 60}
                      for i in range(80)]}
    store = EvidenceStore()
    item = store.add_from_tool_result(
        tool="get_supplier", arguments={"q": "a"},
        result={"ok": True, "empty": False, "data": data, "meta": {}})
    numbers = grounded_numbers(store)
    kept = {str(r["cantidad"]) for r in item.data_view.get("items", [])}
    dropped = {str(r["cantidad"]) for r in data["items"]} - kept
    try:
        recompute_calculation(store, {"id": "c", "op": "count",
                                      "inputs": ["e1.data.items"], "result": len(kept)})
        count_refused = False
    except CollectionTruncated:
        count_refused = True
    try:
        path_alive = store.resolve_path("e1.data.items.0.cantidad") == 1000
    except Exception:  # noqa: BLE001
        path_alive = False
    return {
        "degraded": bool(item.truncated),
        "shape_preserved": isinstance(item.data_view.get("items"), list),
        "omitted_rows_recorded": dict(item.omitted_rows),
        "paths_still_resolve": path_alive,
        "retained_values_citable": kept <= numbers,
        "no_figure_mined_from_text": not (dropped & numbers),
        "count_refuses_degraded_collection": count_refused,
        "verdict": (
            "PASS" if (item.truncated and isinstance(item.data_view.get("items"), list)
                       and path_alive and kept <= numbers and not (dropped & numbers)
                       and count_refused and item.omitted_rows)
            else "FAIL"),
    }


def probe_extraction() -> dict[str, Any]:
    from app.assistant.orchestrator.goal_coverage import extract_requirement_types

    facet = {
        "KPIs de 7 días con stock crítico.": ["dashboard_kpis"],
        "Movimientos de stock del 2404": ["stock_movements"],
    }
    dual = (
        "Stock y movimientos del 2404.",
        "Consulta movimientos y stock actual del 2404.",
        "KPIs y stock actual del 2404",
    )
    noise = ("en orden alfabético", "ordena los productos por precio", "po", "Ventas del día")
    rows = {}
    ok = True
    for text, expected in facet.items():
        _e, types = extract_requirement_types(text)
        rows[text] = types
        ok = ok and types == expected
    for text in dual:
        _e, types = extract_requirement_types(text)
        rows[text] = types
        ok = ok and "current_inventory" in types
    for text in noise:
        _e, types = extract_requirement_types(text)
        rows[text] = types
        ok = ok and "purchase_orders" not in types
    return {"rows": rows, "verdict": "PASS" if ok else "FAIL"}


def probe_verifier_breakdown() -> dict[str, Any]:
    """The M02/M04 mechanism, isolated from model variance.

    A number supplied by the user is not evidence. Proves that discarding such a
    claim and discarding the whole answer are different outcomes that the old
    single ``failures`` counter could not tell apart.
    """
    from app.assistant.orchestrator.answer_verifier import verify_agent_answer
    from app.assistant.orchestrator.evidence_store import EvidenceStore

    store = EvidenceStore()
    store.add_from_tool_result(
        tool="get_inventory",
        arguments={"codigo": "2404"},
        result={
            "ok": True,
            "empty": False,
            "data": {"codigo": "2404", "total_stock": 2, "items": [{"bodega": "Bodega 1", "stock": 2}]},
            "meta": {},
        },
    )
    m04 = verify_agent_answer(
        store=store,
        decision={
            "action": "final_answer",
            "draft_reply": "",
            "claims": [
                {"kind": "dato", "text": "El stock del 2404 es 2 unidades", "evidence_ids": ["e1"]},
                {"kind": "inferencia", "text": "Bajó desde las 25 unidades anteriores", "evidence_ids": ["e1"]},
            ],
            "calculations": [],
        },
    )
    m02 = verify_agent_answer(
        store=store,
        decision={
            "action": "final_answer",
            "draft_reply": "",
            "claims": [{"kind": "dato", "text": "El stock del 2404 es 8888", "evidence_ids": ["e1"]}],
            "calculations": [],
        },
    )
    return {
        "m04_shape": {
            **m04.breakdown(),
            "published_grounded": "2 unidades" in m04.reply and "25" not in m04.reply,
        },
        "m02_shape": {
            **m02.breakdown(),
            "published_grounded": "8888" not in m02.reply,
        },
        "verdict": "PASS"
        if (
            m04.dropped_claims == 1
            and not m04.answer_replaced
            and m02.answer_replaced
            and "8888" not in m02.reply
        )
        else "FAIL",
    }


class _RateTolerantGateway:
    """Envuelve invoke_gateway durante la corrida determinista.

    El Gateway limita a 60 peticiones por ventana de 60 s. El closure lanza 28
    probes y muchos hacen varias llamadas, asi que los ultimos chocaban con el
    limitador y reportaban FAIL. Medido: answer_view y analysis_ladder daban FAIL
    dentro del closure y PASS al ejecutarlos sueltos — un 429 acusando al
    producto de algo que hacia bien la proteccion.

    Se envuelve AQUI y no en cada probe por dos razones: hay 27 puntos de llamada
    directos, y un probe nuevo heredaria el problema sin que nadie lo notara. El
    parche se revierte al terminar: fuera del closure, invoke_gateway es la
    funcion de siempre.
    """

    def __init__(self) -> None:
        self.retries = 0
        self.waited = 0.0
        self.exhausted = 0
        self._original = None

    def __enter__(self) -> "_RateTolerantGateway":
        import time as _time

        from app.assistant import routes as _routes

        self._original = _routes.invoke_gateway

        def _tolerant(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            status, body = self._original(payload)
            # La ventana del limitador es de 60 s. Un solo reintento corto no la
            # deja pasar: si el segundo intento tambien choca, el probe recibe
            # una respuesta de ERROR y la interpreta como fallo del producto.
            # Medido: answer_view devolvia 1 bloque en vez de 2 y analysis_ladder
            # 5 failures, ambos por evidencia que nunca llego.
            for wait in RATE_LIMIT_BACKOFFS:
                if status != 429:
                    break
                self.retries += 1
                self.waited += wait
                _time.sleep(wait)
                status, body = self._original(payload)
            if status == 429:
                self.exhausted += 1
            return status, body

        _routes.invoke_gateway = _tolerant
        return self

    def __exit__(self, *_exc: Any) -> None:
        from app.assistant import routes as _routes

        if self._original is not None:
            _routes.invoke_gateway = self._original


from app.assistant.orchestrator.agent_config import MAX_SECONDS as MAX_SECONDS_HINT  # noqa: E402


def _reclassify_throttled(section: dict[str, Any], waited: float) -> list[str]:
    """FASE 9.2 — un probe que expiro por culpa del limitador no midio el producto.

    El AgentLoop tiene un plazo de MAX_SECONDS (40 s). Si el limitador del
    Gateway durmio 70 s durante la tanda, ese plazo se agota por espera, no por
    comportamiento: medido en `definitive_not_found`, que dio FAIL dentro del
    closure con `agent_timeout` y PASS en aislamiento treinta segundos despues.

    Es la misma clase que `unmeasured` en el benchmark: no confundir la
    disponibilidad de la infraestructura con un fallo del producto. La regla es
    estrecha a proposito — solo reclasifica un FAIL cuyo propio payload declara
    un timeout, y solo si el limitador llego a dormir.
    """
    if waited <= 0:
        return []
    reclasificados = []
    for name, row in section.items():
        if not isinstance(row, dict) or row.get("verdict") != "FAIL":
            continue
        if "timeout" not in json.dumps(row, ensure_ascii=False, default=str).lower():
            continue
        row["verdict"] = "RATE_LIMITED"
        row["throttled_note"] = (
            f"reclasificado: el limitador durmio {waited:.1f}s y el plazo del "
            f"AgentLoop ({MAX_SECONDS_HINT}s) expiro por espera, no por el producto")
        reclasificados.append(name)
    return reclasificados


def run_deterministic(*, with_gateway: bool) -> dict[str, Any]:
    with _RateTolerantGateway() as guard:
        section = _run_deterministic_probes(with_gateway=with_gateway)
    reclasificados = _reclassify_throttled(section, guard.waited)
    if guard.retries or guard.exhausted or reclasificados:
        section["rate_limit"] = {
            "retries": guard.retries,
            "seconds_waited": round(guard.waited, 1),
            # Si esto NO es cero, algun probe trabajo con una respuesta de error
            # y su veredicto no es fiable. Decirlo vale mas que un FAIL opaco.
            "exhausted_calls": guard.exhausted,
            "throttled_probes": reclasificados,
        }
    return section


def _run_deterministic_probes(*, with_gateway: bool) -> dict[str, Any]:
    section: dict[str, Any] = {
        "extraction": probe_extraction(),
        "verifier_breakdown": probe_verifier_breakdown(),
        "transient_error_stays_open": probe_transient_error_stays_open(),
    }
    if not with_gateway:
        section["gateway_probes"] = {"skipped": "ANDES_AGENT_SERVICE_TOKEN missing"}
        return section
    section["t07_shape"] = probe_t07_shape()
    section["t07_blocked_finals"] = probe_t07_blocked_finals()
    section["no_progress_guard"] = probe_no_progress_guard()
    section["thrashing_terminates"] = probe_thrashing_terminates()
    section["alternation_terminates"] = probe_alternation_terminates()
    section["definitive_not_found"] = probe_definitive_not_found()
    section["blocked_final_release"] = probe_blocked_final_release()
    section["two_blocked_finals_continue"] = probe_two_blocked_finals_still_continue()
    section["numeric_grounding"] = probe_numeric_grounding()
    section["cardinality_count"] = probe_cardinality_count()
    section["covering_tools_are_named"] = probe_covering_tools_are_named()
    section["calculation_path_grammar"] = probe_calculation_path_grammar()
    section["claim_provenance"] = probe_claim_provenance()
    section["evidence_degradation"] = probe_evidence_degradation()
    section["real_truncation"] = probe_real_truncation()
    section["prompt_economics"] = probe_prompt_economics()
    section["answer_sufficiency"] = probe_answer_sufficiency()
    section["answer_view"] = probe_answer_view()
    section["analysis_ladder"] = probe_analysis_ladder()
    section["sales_capability"] = probe_sales_capability()
    section["sales_over_http"] = probe_sales_over_http()
    section["equivalences_over_http"] = probe_equivalences_over_http()
    section["contract_honesty"] = probe_contract_honesty()
    section["period_resolution"] = probe_period_resolution()
    section["orders_over_http"] = probe_orders_over_http()
    section["artifact_integrity"] = probe_artifact_integrity(OUT_DIR)
    section["limit_coherence"] = probe_limit_coherence()
    section["finance_redaction"] = probe_finance_redaction()
    section["conditional_analysis"] = probe_conditional_analysis()
    return section


# ------------------------------------------------------------------------ tests


def run_tests() -> dict[str, Any]:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    missing: list[str] = []
    for name in TEST_MODULES:
        try:
            suite.addTests(loader.loadTestsFromName(name))
        except Exception:  # noqa: BLE001 — a missing module must not kill the report
            missing.append(name)
    runner = unittest.TextTestRunner(verbosity=0, stream=open(os.devnull, "w", encoding="utf-8"))
    result = runner.run(suite)
    return {
        "modules": len(TEST_MODULES) - len(missing),
        "missing_modules": missing,
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "ok": result.wasSuccessful(),
        # FASE 8.2D — sin los nombres, "failures=1" obliga a reproducir la corrida
        # entera a ciegas para saber que se rompio. Solo el id del test, nunca el
        # traceback (puede llevar datos del Gateway).
        "failed_tests": sorted(str(t) for t, _ in list(result.failures) + list(result.errors)),
    }


# -------------------------------------------------------------------------- llm


def _load_cases() -> list[dict[str, Any]]:
    from evals.fase81_runner import load_cases

    return load_cases(DATASET)


def _enable_agent() -> bool:
    os.environ["ANDES_ASSISTANT_AGENT_ENABLED"] = "1"
    os.environ["ANDES_ASSISTANT_NL_ENABLED"] = "1"
    os.environ["ANDES_ORCH_PLANNER"] = "llm"
    os.environ.setdefault("ANDES_LLM_PROVIDER", "openai_compatible")
    from app.assistant.orchestrator.agent_config import agent_loop_allowed

    return bool(agent_loop_allowed())


def _metrics_supported() -> bool:
    import inspect

    from app.assistant.orchestrator.service import run_orchestrator_chat

    return "metrics_store" in inspect.signature(run_orchestrator_chat).parameters


class _UsageProbe:
    """Intercepta el metric del turno para leer los tokens SIN persistir nada.

    El servicio ya emite prompt/completion tokens; sin capturarlos aqui, el coste
    real por turno es inobservable despues de la corrida y solo queda estimarlo.
    Se guardan conteos, nunca contenido.
    """

    def __init__(self) -> None:
        self.last: dict[str, Any] = {}

    def record_turn(self, metric: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
        if isinstance(metric, dict):
            self.last = {k: metric.get(k) for k in
                         ("prompt_tokens", "completion_tokens", "total_tokens",
                          "llm_latency_ms", "total_latency_ms")}
        return metric


def _last_usage(metrics: Any) -> dict[str, Any]:
    return dict(getattr(metrics, "last", {}) or {})


def _run_case(gold: dict[str, Any], suffix: str) -> dict[str, Any]:
    """One benchmark case, mirroring evals/fase81_runner.py exactly.

    FASE 8.1G.2 — the setup turns are part of the case, not scaffolding: F01/F06/
    Q01/Q04 are anaphoric follow-ups whose antecedent only exists if the prior turn
    ran. They must share ONE TurnStore, ONE planner instance and ONE
    conversation_id with the scored turn, run in dataset order, and the timer must
    start before them so latency stays comparable with the reference runner.
    """
    from app.assistant.orchestrator.factory import build_planner
    from app.assistant.orchestrator.service import run_orchestrator_chat
    from app.assistant.orchestrator.turn_store import TurnStore
    from app.assistant.routes import invoke_gateway

    planner = build_planner()
    store = TurnStore()
    # El servicio ya emite prompt/completion tokens al MetricsStore. Capturarlos
    # aqui es lo que permite analizar el presupuesto DESPUES de la corrida en vez
    # de estimarlo: sin esto, "cuantos tokens costo cada turno" es inobservable.
    metrics = _UsageProbe() if _metrics_supported() else None
    cid = f"fase81g-{gold.get('id')}-{suffix}"
    actor = str(gold.get("actor") or "albertadmin")

    t0 = time.perf_counter()
    for setup in gold.get("setup_turns") or []:
        run_orchestrator_chat(
            message=str(setup.get("prompt") or ""),
            actor_user=actor,
            conversation_id=cid,
            invoke_fn=invoke_gateway,
            planner=planner,
            turn_store=store,
        )
    result = run_orchestrator_chat(
        message=str(gold.get("prompt") or ""),
        actor_user=actor,
        conversation_id=cid,
        invoke_fn=invoke_gateway,
        planner=planner,
        turn_store=store,
        **({"metrics_store": metrics} if metrics is not None else {}),
    )
    latency = int((time.perf_counter() - t0) * 1000)
    usage = _last_usage(metrics)
    return {
        "setup_turns": len(gold.get("setup_turns") or []),
        "id": gold.get("id"),
        "bucket": gold.get("bucket"),
        "ok": result.get("ok"),
        "tools_used": list(result.get("tools_used") or []),
        "fallback_used": bool(result.get("fallback_used")),
        "fallback_reason": result.get("fallback_reason"),
        "needs_clarification": bool(result.get("needs_clarification")),
        "scenario": result.get("scenario"),
        "reply": result.get("reply"),
        "agent_trace": result.get("agent_trace") or [],
        "goal_coverage": result.get("goal_coverage"),
        "arg_errors": result.get("arg_errors") or [],
        "verifier_failures": int(result.get("verifier_failures") or 0),
        "verifier_breakdown": result.get("verifier_breakdown") or {},
        "agent_progress": result.get("agent_progress") or {},
        "latency_ms": latency,
        "cost_est": result.get("cost_est"),
        "budget": result.get("budget"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "llm_latency_ms": usage.get("llm_latency_ms"),
        # FASE 8.2D — token_economics viene del servicio y lleva el acumulado real
        # del turno, incluido cached_tokens. usage viene del metric y puede no
        # llevarlo si el proveedor no lo reporta.
        "cached_tokens": (result.get("token_economics") or {}).get("cached_tokens"),
        "token_economics": result.get("token_economics"),
        "evidence_degradation": result.get("evidence_degradation"),
        "sufficiency": result.get("sufficiency"),
    }


def stability_watch_ids(out_dir: Path) -> tuple[str, ...]:
    """Los casos que fallaron la ultima vez en ESTE brazo, leidos del informe.

    No es una lista escrita a mano: es el estado del brazo. Un caso arreglado
    deja de vigilarse solo; uno nuevo entra solo.
    """
    path = out_dir / f"fase81_final_report.{arm_id()}.json"
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        bench = report.get("benchmark") or {}
        # Una corrida que no midio no tiene fallos que vigilar: sus "failed" son
        # casillas vacias. Derivar de ahi convertiria un agotamiento del
        # proveedor en 265 turnos de vigilancia sobre nada.
        if bench.get("measurement_valid") is False:
            ids: tuple[str, ...] = ()
        else:
            ids = tuple(str(x) for x in (bench.get("failed") or []) if x)
    except (OSError, ValueError, TypeError):
        ids = ()
    if not ids:
        ids = STABILITY_WATCH_SEED
    return tuple(i for i in ids if i not in STABILITY_CORE_IDS)[:MAX_WATCH_IDS]


def run_stability(out_dir: Path) -> dict[str, Any]:
    from evals.fase81_scorer import score_case

    cases = {c["id"]: c for c in _load_cases()}
    runs_path = arm_path(out_dir, "fase81g_stability_runs", ".jsonl")
    if runs_path.exists():
        runs_path.unlink()
    summary: dict[str, Any] = {}
    watch = stability_watch_ids(out_dir)
    plan = ([(cid, "core", STABILITY_RUNS) for cid in STABILITY_CORE_IDS]
            + [(cid, "watch", STABILITY_WATCH_RUNS) for cid in watch])
    for cid, role, runs in plan:
        gold = cases.get(cid)
        if gold is None:
            summary[cid] = {"error": "case_not_in_dataset", "role": role}
            continue
        passes = 0
        rows: list[dict[str, Any]] = []
        for i in range(runs):
            run = _run_case(gold, f"s{i}")
            score = score_case(gold, run)
            run["pass"] = bool(score.get("pass"))
            run["reasons"] = score.get("reasons") or []
            run["failure_kind"] = score.get("failure_kind")
            passes += int(run["pass"])
            rows.append(run)
            with runs_path.open("a", encoding="utf-8") as fh:
                fh.write(_scrub(json.dumps(run, ensure_ascii=False)) + "\n")
            print(f"  [{role}] {cid} run {i + 1}/{runs} pass={run['pass']}")
        summary[cid] = _summarize_stability(cid, rows, passes)
        summary[cid]["role"] = role
    summary["_watch_ids"] = list(watch)
    return summary


def _summarize_stability(cid: str, rows: list[dict[str, Any]], passes: int) -> dict[str, Any]:
    tool_sets = sorted({",".join(sorted(set(r["tools_used"]))) for r in rows})
    lat = [int(r["latency_ms"]) for r in rows]
    drops = sum(int((r.get("verifier_breakdown") or {}).get("dropped_claims") or 0) for r in rows)
    replaced = sum(
        1 for r in rows if (r.get("verifier_breakdown") or {}).get("answer_replaced")
    )
    covered_all = sum(
        1
        for r in rows
        if all(
            q.get("status") in {"covered", "impossible"}
            for q in ((r.get("goal_coverage") or {}).get("requirements") or [])
        )
    )
    return {
        "runs": len(rows),
        "pass": passes,
        "rate": round(passes / max(1, len(rows)), 4),
        "stable": passes in (0, len(rows)),
        "verdict": "PASS_STABLE" if passes == len(rows) else ("FAIL_STABLE" if passes == 0 else "FLAKY"),
        # Lo que el scorer NO puede saber de una sola corrida: un fallo que se
        # repite siempre con la misma configuracion es comportamiento
        # determinista del producto; uno que aparece y desaparece es varianza
        # del modelo. Sin esto, la distincion es una opinion.
        "attribution": ("stable_pass" if passes == len(rows)
                        else "deterministic_failure" if passes == 0
                        else "variance"),
        "failure_kinds": sorted({str(r.get("failure_kind")) for r in rows
                                 if r.get("failure_kind")}),
        "tool_sets": tool_sets,
        "all_requirements_resolved_runs": covered_all,
        "fallbacks": sum(1 for r in rows if r["fallback_used"]),
        "fallback_reasons": sorted({str(r["fallback_reason"]) for r in rows if r["fallback_reason"]}),
        "verifier_failures_total": sum(int(r["verifier_failures"]) for r in rows),
        "verifier_dropped_claims_total": drops,
        "verifier_answer_replaced_runs": replaced,
        "reasons": sorted({x for r in rows for x in (r.get("reasons") or [])}),
        "avg_latency_ms": int(statistics.mean(lat)) if lat else 0,
    }


def _safely(fn: Any, arg: Any, *, default: Any) -> Any:
    """Una metrica opcional no puede costar el informe entero.

    Aprendido a las malas: _evidence_summary lanzo AttributeError sobre una clave
    con forma inesperada y una corrida completa de 66 casos con LLM real —siete
    minutos y dinero real— termino sin escribir fase81_final_report. Los jsonl se
    salvaron porque se escriben incrementalmente; el informe no. Cualquier
    agregado accesorio va envuelto, y el fallo se reporta dentro del informe en
    vez de reemplazarlo.
    """
    try:
        return fn(arg)
    except Exception as exc:  # noqa: BLE001 — el informe manda sobre la metrica
        return {"error": f"{type(exc).__name__}: {exc}"} if isinstance(default, dict)             else f"{default} ({type(exc).__name__})"


def _cached_summary(runs: list[dict[str, Any]]) -> str:
    """FASE 8.2D — cuanto del prompt sirvio el proveedor desde cache de prefijo.

    Decide si el techo de tokens esta contando a precio completo un system prompt
    que en realidad se paga una vez. Si sale 0 en todos los turnos, el proveedor
    no cachea y 8.3 tiene que reducir el prompt, no confiar en la cache.
    """
    seen = [r for r in runs if r.get("cached_tokens") is not None]
    if not seen:
        return "no medido (el proveedor no reporto cached_tokens)"
    cached = sum(int(r.get("cached_tokens") or 0) for r in seen)
    prompt = sum(int(r.get("prompt_tokens") or 0) for r in seen)
    if not prompt:
        return "no medido (sin prompt_tokens)"
    pct = round(100.0 * cached / prompt, 1)
    return f"{cached}/{prompt} tokens de prompt servidos desde cache ({pct}%) en {len(seen)} turnos"


def _evidence_summary(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Cuantos turnos degradaron evidencia con datos reales.

    Lee evidence_degradation, NO evidence_summary: esa segunda clave es la lista
    publica de evidencias por herramienta y tiene otra forma. Se filtra por tipo
    ademas de por verdad porque una corrida vieja puede traer cualquiera de las
    dos, y una excepcion aqui tiraba el informe entero de una corrida de siete
    minutos con LLM real.
    """
    evs = [r.get("evidence_degradation") for r in runs]
    evs = [e for e in evs if isinstance(e, dict) and e]
    tools: set[str] = set()
    for e in evs:
        tools |= set(e.get("degraded_tools") or [])
    return {
        "cases_with_evidence": len(evs),
        "cases_degraded": sum(1 for e in evs if int(e.get("truncated_items") or 0) > 0),
        "omitted_rows_total": sum(int(e.get("omitted_rows_total") or 0) for e in evs),
        "degraded_tools": sorted(tools),
    }


def run_benchmark(out_dir: Path) -> dict[str, Any]:
    from evals.fase81_scorer import aggregate, score_case

    cases = _load_cases()
    runs_path = arm_path(out_dir, "fase81g_runs_llm", ".jsonl")
    scores_path = arm_path(out_dir, "fase81g_scores_llm", ".jsonl")
    for p in (runs_path, scores_path):
        if p.exists():
            p.unlink()
    scores: list[dict[str, Any]] = []
    _bench_runs: list[dict[str, Any]] = []
    # FASE 8.1G.2 — counted exactly as evals/fase81_runner.py counts them, so the
    # numbers are comparable with the 59/64 baseline. The per-event totals are kept
    # as a separate field instead of redefining the headline counters.
    blocked_final = 0
    premature = 0
    blocked_final_events = 0
    for gold in cases:
        run = _run_case(gold, "b")
        score = score_case(gold, run)
        scores.append(score)
        _bench_runs.append(run)
        trace = run.get("agent_trace") or []
        if any(t.get("blocked_final") for t in trace):
            blocked_final += 1
        blocked_final_events += sum(1 for t in trace if t.get("blocked_final"))
        expected = list(gold.get("expected_tools") or gold.get("tools_expected") or [])
        got = list(run.get("tools_used") or [])
        if len(expected) >= 2 and len(got) == 1 and not run.get("fallback_used"):
            premature += 1
        with runs_path.open("a", encoding="utf-8") as fh:
            fh.write(_scrub(json.dumps(run, ensure_ascii=False)) + "\n")
        with scores_path.open("a", encoding="utf-8") as fh:
            fh.write(_scrub(json.dumps(score, ensure_ascii=False)) + "\n")
        print(f"  {gold.get('id')} pass={score.get('pass')} tools={score.get('tools_got')}")
    # El estado del flag lo sabe el brazo, no el scorer: antes `state` lo
    # afirmaba sin leerlo y la tercera A/B reporto el flag apagado en los dos.
    agg = aggregate(scores, analysis_enabled=arm_id().startswith("an1"))
    tok = [int(r.get("total_tokens") or 0) for r in _bench_runs if r.get("total_tokens")]
    agg["tokens"] = {
        "cases_with_usage": len(tok),
        "avg_total_tokens": int(sum(tok) / len(tok)) if tok else 0,
        "max_total_tokens": max(tok) if tok else 0,
        "p95_total_tokens": (sorted(tok)[int(0.95 * (len(tok) - 1))] if tok else 0),
        "budget": (_bench_runs[0].get("budget") if _bench_runs else None),
        "headroom_vs_token_budget": (
            None if not tok else
            f"{max(tok)} / {(_bench_runs[0].get('budget') or {}).get('token_budget_fallback')}"),
        "cached_summary": _safely(_cached_summary, _bench_runs, default="no medido"),
    }
    agg["evidence"] = _safely(_evidence_summary, _bench_runs, default={})
    agg["decisions"] = {
        "max": max((len([t for t in (r.get("agent_trace") or [])
                         if t.get("action") != "fallback"]) for r in _bench_runs), default=0),
        "max_invokes": max((len(r.get("tools_used") or []) for r in _bench_runs), default=0),
    }
    agg["premature_final"] = premature
    agg["blocked_final"] = blocked_final
    agg["blocked_final_events"] = blocked_final_events
    agg["setup_turns_cases"] = sum(1 for g in cases if g.get("setup_turns"))
    agg["failed"] = [s["id"] for s in scores if not s.get("pass")]
    agg["agent_true_failures_ids"] = [s["id"] for s in scores if s.get("agent_true_failure")]
    return agg


# ----------------------------------------------------------------------- report


def _txt_report(report: dict[str, Any]) -> str:
    L: list[str] = []
    det = report["deterministic"]
    bench = report.get("benchmark") or {}
    stab = report.get("stability") or {}
    tests = report["tests"]

    L.append("FASE 8.1 — INFORME DE CIERRE (8.1G)")
    L.append(f"generated_at={report['generated_at']}  llm_sections={report['llm_executed']}")
    L.append("")
    L.append("HARNESS (8.1G.2)")
    L.append("   _run_case ejecuta gold['setup_turns'] con el mismo TurnStore, planner,")
    L.append("   conversation_id y actor, en orden, y cronometra desde antes del primero,")
    L.append("   igual que evals/fase81_runner.py. Paridad A/B cubierta por")
    L.append("   tests/test_orchestrator_fase81g2_harness_parity.py.")
    if bench:
        L.append(f"   casos con setup_turns en esta corrida: {bench.get('setup_turns_cases')}")
    L.append("")
    L.append("1. BASELINE")
    L.append(f"   {BASELINE['pass']}/{BASELINE['n']} ({BASELINE['pass_rate']})  failed={BASELINE['failed']}")
    L.append(f"   source={BASELINE['source']}")
    L.append("")
    L.append("2. RESULTADO FINAL")
    if bench and not bench.get("measurement_valid", True):
        # FASE 8.x — lo primero, antes de cualquier cifra. El 2026-09-20 el
        # informe publico 18/76 y 17/76 de dos corridas en las que 59 casos no
        # recibieron respuesta del modelo, y las presento como si midieran la
        # escalera analitica y la resolucion de periodos. Una cifra invalida
        # arriba del todo es peor que ninguna cifra.
        L.append("   *** CORRIDA NO VALIDA — NO COMPARAR CON OTROS BRAZOS ***")
        L.append(f"   {bench.get('cases_unmeasured')} de {bench.get('n')} casos sin "
                 f"respuesta del modelo (llm_unavailable).")
        L.append(f"   {bench.get('measurement_note')}")
        L.append(f"   casos realmente medidos: {bench.get('cases_measured')}"
                 f"   pass_rate_measured={bench.get('pass_rate_measured')}")
        L.append("   Aviso: en una corrida degradada, un caso con turnos de")
        L.append("   preparacion (F01/F06/Q01/Q04) puede fallar porque su antecedente")
        L.append("   nunca se establecio. Ninguna conclusion por caso es fiable aqui.")
        L.append(f"   sin medir: {bench.get('unmeasured_ids')}")
        L.append("")
    if bench:
        L.append(f"   {bench.get('pass')}/{bench.get('n')} ({bench.get('pass_rate')})  failed={bench.get('failed')}")
        delta = int(bench.get("pass") or 0) - BASELINE["pass"]
        L.append(f"   delta_vs_baseline={delta:+d}")
        L.append(f"   product_failures={bench.get('product_failures')}"
                 f" ids={bench.get('product_failure_ids')}"
                 f" kinds={bench.get('failure_kinds')}")
        L.append(f"   agent_true_failures={bench.get('agent_true_failures')}"
                 f" ids={bench.get('agent_true_failures_ids')} (solo dataset; ver seccion 8)")
    else:
        L.append("   PENDIENTE — requiere ANDES_LLM_API_KEY (--llm --bench)")
    L.append("")

    for num, key, name in (
        (3, "P03", "P03"),
        (4, "T07", "T07"),
        (5, "M02", "M02"),
        (6, "M04", "M04"),
        (6.5, "N04", "N04"),
    ):
        L.append(f"{num}. {name}")
        row = stab.get(key)
        if row and "error" not in row:
            L.append(f"   {row['pass']}/{row['runs']}  verdict={row['verdict']}  tool_sets={row['tool_sets']}")
            L.append(
                f"   requirements_resueltos={row['all_requirements_resolved_runs']}/{row['runs']}"
                f"  fallbacks={row['fallbacks']}{row['fallback_reasons']}"
            )
            L.append(
                f"   verifier: failures={row['verifier_failures_total']}"
                f" dropped_claims={row['verifier_dropped_claims_total']}"
                f" answer_replaced_runs={row['verifier_answer_replaced_runs']}"
            )
            if row["reasons"]:
                L.append(f"   reasons={row['reasons']}")
        else:
            L.append("   PENDIENTE — requiere ANDES_LLM_API_KEY (--llm)")
        if key == "T07":
            t = det.get("t07_shape") or {}
            if t:
                L.append(
                    f"   determinista(Gateway real): tools={t.get('tools_executed')}"
                    f" both_covered={t.get('both_covered')} fallback={t.get('fallback_used')}"
                    f" verdict={t.get('verdict')}"
                )
                L.append(
                    f"   gate 8.1F habría bloqueado la 2a tool: {t.get('old_gate_would_block_second_tool')}"
                )
        if key == "N04":
            n = det.get("definitive_not_found") or {}
            if n:
                L.append(
                    f"   determinista(Gateway real): requirements={n.get('requirements')}"
                    f" blocked_finals={n.get('blocked_finals')} fallback={n.get('fallback_used')}"
                    f" decisiones={n.get('decisions')} verdict={n.get('verdict')}"
                )
        if key in ("M02", "M04"):
            vb = det.get("verifier_breakdown") or {}
            shape = vb.get("m02_shape" if key == "M02" else "m04_shape") or {}
            L.append(
                f"   mecanismo determinista: dropped_claims={shape.get('dropped_claims')}"
                f" answer_replaced={shape.get('answer_replaced')}"
                f" published_grounded={shape.get('published_grounded')}"
            )
        L.append("")

    L.append("7. K05 — BENCHMARK POLICY CANDIDATE (no es fallo de producción)")
    L.append("   check_stock y get_inventory son la MISMA familia current_inventory.")
    L.append("   El gold pide tool_count_policy=exact expected_tools=[check_stock] sin")
    L.append("   acceptable_tool_families, así que get_inventory falla por política, no por")
    L.append("   selección incorrecta. No se cambió ni el AgentLoop ni el scorer por K05.")
    L.append("")

    L.append("8. FALLOS DE PRODUCTO")
    if bench:
        L.append(f"   product_failures  : {bench.get('product_failures')}"
                 f" -> {bench.get('product_failure_ids')}")
        L.append(f"   por tipo          : {bench.get('failure_kinds')}")
        L.append("   'argument_contract' = el modelo eligio bien y no pudo expresar")
        L.append("   los argumentos: defecto de la cadena contrato/schema/normalizador.")
        # FASE 8.x — producto, varianza y evaluador no se distinguen con una sola
        # corrida. La lista de vigilancia repite los fallos y aqui se cruzan.
        stab = report.get("stability") or {}
        attributed = [(cid, (stab.get(cid) or {}).get("attribution"),
                       (stab.get(cid) or {}).get("rate"))
                      for cid in (bench.get("product_failure_ids") or [])
                      if isinstance(stab.get(cid), dict)]
        if attributed:
            L.append("   atribucion medida con repeticiones:")
            for cid, attr, rate in attributed:
                L.append(f"     {cid}: {attr}  (pass_rate={rate})")
            unmeasured = [c for c in (bench.get("product_failure_ids") or [])
                          if c not in {a[0] for a in attributed}]
            if unmeasured:
                L.append(f"     sin repeticiones, atribucion NO medida: {unmeasured}")
        else:
            L.append("   atribucion producto/varianza: NO medida en esta corrida")
            L.append("   (requiere la lista de vigilancia de estabilidad, --llm)")
        L.append(f"   agent_true_failures: {bench.get('agent_true_failures')}"
                 f" -> {bench.get('agent_true_failures_ids')}")
        L.append("   (cuenta SOLO casos con baseline_failure=agent en el dataset; es una")
        L.append("   lista de regresion de 8.1D, no un detector. Un 0 aqui no significa")
        L.append("   cero defectos: en la tercera A/B salio 0 con O01/O04 rotos por schema.)")
        L.append(f"   baseline: {len(BASELINE['agent_true_failures'])} -> {BASELINE['agent_true_failures']}")
    else:
        L.append("   PENDIENTE (--llm --bench)")
    L.append("")
    L.append("9. BENCHMARK POLICY CASES")
    L.append("   K05  check_stock vs get_inventory (misma familia, policy=exact)")
    L.append("")
    L.append("10. VERIFIER FAILURES")
    if bench:
        L.append(f"   total={bench.get('verifier_failures')}  (baseline={BASELINE['verifier_failures']})")
    else:
        L.append("   PENDIENTE (--llm --bench)")
    L.append("   Ningún check del verifier fue debilitado. Se añadió SOLO un desglose")
    L.append("   (dropped_claims / answer_replaced / calc_*); 'failures' conserva su valor.")
    L.append("")
    L.append("11. PREMATURE FINAL")
    L.append(f"   {bench.get('premature_final') if bench else 'PENDIENTE'}  (baseline={BASELINE['premature_final']})")
    L.append("")
    L.append("12. BLOCKED FINAL")
    if bench:
        L.append(
            f"   casos con >=1 bloqueo: {bench.get('blocked_final')}"
            f"  (baseline={BASELINE['blocked_final']}, misma definicion)"
        )
        L.append(f"   eventos totales de bloqueo: {bench.get('blocked_final_events')}")
    else:
        L.append("   PENDIENTE")
    L.append("")
    L.append("13. FALLBACK")
    L.append(f"   {bench.get('fallback') if bench else 'PENDIENTE'}  (baseline={BASELINE['fallback']})")
    L.append("   nueva razón enumerable: agent_no_progress (distinta de agent_limit)")
    L.append("")
    L.append("14. INVALID_ARGS")
    L.append(f"   {bench.get('invalid_args') if bench else 'PENDIENTE'}  (baseline={BASELINE['invalid_args']})")
    L.append("   Contratos por herramienta intactos (q vs codigo no se reabrió).")
    L.append("")
    L.append("15. AVG LATENCY")
    L.append(f"   {bench.get('avg_latency_ms') if bench else 'PENDIENTE'} ms  (baseline={BASELINE['avg_latency_ms']})")
    L.append("")
    L.append("16. P95 LATENCY")
    L.append(f"   {bench.get('p95_latency_ms') if bench else 'PENDIENTE'} ms  (baseline={BASELINE['p95_latency_ms']})")
    L.append("")
    L.append("17. PROCEDENCIA DE CLAIMS (8.2)")
    if bench and bench.get("provenance"):
        pv = bench["provenance"]
        L.append(f"   enforcement_ready : {pv.get('enforcement_ready')}")
        L.append(f"   casos aplicables  : {pv.get('applicable_cases')}/{bench.get('n')}"
                 f"   (no aplicables: {pv.get('not_applicable_cases')})")
        L.append(f"   medidos de esos   : {pv.get('measured_cases')}")
        L.append("   El denominador son los casos que PUEDEN emitir claims. Un turno que")
        L.append("   nunca llega al verifier no puede violar procedencia; contarlo como")
        L.append("   'sin medir' dejaba el gate en unknown para siempre.")
        L.append(f"   casos con violacion: {pv.get('cases_with_violations')}"
                 f"  total: {pv.get('total_violations')}")
        L.append(f"   severidad alta    : {pv.get('high_severity_cases')}"
                 f"   media: {pv.get('medium_severity_cases')}")
        L.append(f"   claims publicados en riesgo si se activa: {pv.get('published_claims_at_risk')}")
        L.append(f"   por tipo          : {pv.get('by_kind')}")
        L.append("   enforcing sigue APAGADO: la senal se reporta, no puntua.")
    else:
        L.append("   PENDIENTE — requiere la corrida con LLM")
    L.append("")
    L.append("18. PRESUPUESTO Y COSTE REAL")
    if bench and bench.get("tokens"):
        tk, dc = bench["tokens"], bench.get("decisions") or {}
        L.append(f"   turnos con medicion de tokens: {tk.get('cases_with_usage')}/{bench.get('n')}")
        L.append(f"   tokens por turno  : avg={tk.get('avg_total_tokens')}"
                 f" p95={tk.get('p95_total_tokens')} max={tk.get('max_total_tokens')}")
        L.append(f"   margen vs techo   : {tk.get('headroom_vs_token_budget')}")
        L.append(f"   decisiones max    : {dc.get('max')}   invokes max: {dc.get('max_invokes')}")
        L.append(f"   tokens cacheados  : {tk.get('cached_summary')}")
        L.append(f"   limites activos   : {tk.get('budget')}")
        pe = det.get("prompt_economics") or {}
        if pe:
            L.append("")
            L.append("   COMPOSICION DEL PROMPT (medida, 3 decisiones):")
            L.append(f"     system prompt     : {pe.get('system_prompt_chars')} chars"
                     f"  -> {pe.get('system_share_pct')}% del prompt del turno")
            L.append(f"     evidencia         : {pe.get('evidence_share_pct')}%")
            L.append(f"     modelo predice    : {pe.get('modelled_tokens_3_decisions')} tokens")
            L.append(f"     decisiones que paga el techo: peor={pe.get('decisions_affordable_worst')}"
                     f" mejor={pe.get('decisions_affordable_best')}"
                     f"  (MAX_AGENT_STEPS={pe.get('max_agent_steps')})")
            if pe.get("steps_exceed_budget"):
                L.append("     AVISO: los pasos declarados superan lo que el techo paga.")
        ev = bench.get("evidence") or {}
        if ev:
            L.append("")
            L.append("   DEGRADACION DE EVIDENCIA EN ESTA CORRIDA:")
            L.append(f"     turnos que degradaron: {ev.get('cases_degraded')}"
                     f"  filas omitidas: {ev.get('omitted_rows_total')}")
            L.append(f"     herramientas         : {ev.get('degraded_tools')}")
    else:
        L.append("   PENDIENTE — requiere la corrida con LLM")
    L.append("")
    L.append("18.5. UTILIDAD DE LA RESPUESTA (8.3)")
    suf = (bench or {}).get("sufficiency") or {}
    if suf.get("measured_cases"):
        L.append(f"   estado            : {suf.get('state')}")
        L.append(f"   casos medidos     : {suf.get('measured_cases')}"
                 f"   juzgables: {suf.get('judged_cases')}")
        L.append(f"   veredictos        : {suf.get('verdicts')}")
        L.append(f"   insuficientes     : {suf.get('insufficient_ids')}")
        L.append(f"   por tipo          : {suf.get('insufficient_by_type')}")
        L.append(f"   uso medio de filas: {suf.get('avg_row_utilization')}")
        L.append("   La suficiencia NO puntua: se reporta. Mide el dual del")
        L.append("   grounding —si algo de la evidencia llego a la respuesta— y solo")
        L.append("   puede subir citando datos reales, nunca inventando.")
    elif bench:
        L.append(f"   {suf.get('state') or 'PENDIENTE'}")
    else:
        L.append("   PENDIENTE — requiere la corrida con LLM")
    sp = det.get("answer_sufficiency") or {}
    if sp:
        L.append(f"   probe determinista: separa util/vacuo="
                 f"{sp.get('separates_useful_from_vacuous')}"
                 f"  inventar no sirve={sp.get('invention_cannot_buy_sufficiency')}")
    L.append("")
    L.append("18.7. ESCALERA ANALITICA (8.5)")
    an = (bench or {}).get("analysis") or {}
    if bench:
        L.append(f"   estado            : {an.get('state')}")
        L.append(f"   casos que la piden: {an.get('cases_expecting_analysis')}"
                 f"   que la usan: {an.get('cases_using_ladder')}")
        L.append(f"   peldanos usados   : {an.get('rung_usage')}")
        if an.get("analysis_enabled") is False:
            L.append("   Cero usos con el flag apagado NO mide al modelo: el schema de")
            L.append("   structured output fija kind a dato|inferencia, asi que emitir")
            L.append("   un peldano nuevo es imposible. Enciende")
            L.append("   ANDES_ASSISTANT_ANALYSIS_ENABLED=1 para medir la capacidad.")
        elif an.get("analysis_enabled") and not an.get("cases_using_ladder"):
            L.append("   Con el flag ENCENDIDO esto si mide al modelo: el bloque entro,")
            L.append("   el schema admitia los peldanos y aun asi no emitio ninguno.")
        elif an.get("analysis_enabled") is None:
            L.append("   El brazo no informo el estado del flag: no se puede decir si")
            L.append("   cero usos mide al modelo o mide una capacidad apagada.")
    else:
        L.append("   PENDIENTE — requiere la corrida con LLM")
    ap = det.get("analysis_ladder") or {}
    if ap:
        L.append(f"   probe determinista: vectores de invencion bloqueados="
                 f"{ap.get('invention_vectors_blocked')}"
                 f"  proyeccion exige supuesto={ap.get('projection_requires_assumption')}")
    L.append("")
    L.append("18.9. BRAZO DE LA CORRIDA (8.6)")
    arm = report.get("arm") or {}
    L.append(f"   brazo             : {arm.get('arm')}"
             f"   analysis={arm.get('analysis_enabled')}"
             f" provenance={arm.get('provenance_enforced')}")
    L.append(f"   otros brazos      : {arm.get('siblings_available') or 'ninguno'}")
    L.append(f"   A/B completo      : {arm.get('ab_complete')} — {arm.get('note')}")
    L.append("")
    L.append("19. TESTS")
    L.append(
        f"   modules={tests['modules']} run={tests['tests_run']} failures={tests['failures']}"
        f" errors={tests['errors']} ok={tests['ok']}"
    )
    L.append("   cubre 8.1A 8.1B 8.1C 8.1D 8.1G + FASE 5 + 7A + 7B + ERP + Gateway")
    if tests["missing_modules"]:
        L.append(f"   AUSENTES={tests['missing_modules']}")
    if tests.get("failed_tests"):
        L.append(f"   FALLAN={tests['failed_tests']}")
    L.append("   probes deterministas contra Gateway real:")
    for name in (
        "t07_shape",
        "t07_blocked_finals",
        "no_progress_guard",
        "thrashing_terminates",
        "alternation_terminates",
        "definitive_not_found",
        "blocked_final_release",
        "two_blocked_finals_continue",
        "real_truncation",
        "prompt_economics",
        "answer_sufficiency",
        "answer_view",
        "analysis_ladder",
        "sales_capability",
        "sales_over_http",
        "equivalences_over_http",
        "contract_honesty",
        "limit_coherence",
        "finance_redaction",
        "conditional_analysis",
        "numeric_grounding",
        "cardinality_count",
        "covering_tools_are_named",
        "calculation_path_grammar",
        "claim_provenance",
        "evidence_degradation",
    ):
        row = det.get(name)
        L.append(f"     {name}: {row.get('verdict') if isinstance(row, dict) else 'SKIPPED'}")
    L.append(f"     extraction: {det['extraction']['verdict']}")
    L.append(f"     transient_error_stays_open: {det['transient_error_stays_open']['verdict']}")
    L.append(f"     verifier_breakdown: {det['verifier_breakdown']['verdict']}")
    L.append("")
    L.append("20. ARCHIVOS MODIFICADOS")
    for f in MODIFIED_FILES:
        L.append(f"   {f}")
    L.append("")
    L.append("21. RIESGOS")
    for r in report["risks"]:
        L.append(f"   - {r}")
    L.append("")
    L.append("22. ROLLBACK")
    for r in report["rollback"]:
        L.append(f"   - {r}")
    L.append("")
    L.append(f"ENV RESTAURADO: AGENT={os.environ.get('ANDES_ASSISTANT_AGENT_ENABLED')} "
             f"NL={os.environ.get('ANDES_ASSISTANT_NL_ENABLED')} "
             f"ORCH={os.environ.get('ANDES_ORCH_PLANNER')}")
    return "\n".join(L)


RISKS = [
    "MAX_NO_PROGRESS_STEPS=2 permite como máximo 2 invokes improductivos consecutivos "
    "frente a 1 del gate anterior; el techo real sigue siendo MAX_TOOL_CALLS=5, "
    "MAX_AGENT_STEPS=5, MAX_SECONDS=40 y MAX_COST_PER_TURN.",
    "Una decisión rechazada por admisión ya no termina el turno: consume un paso de "
    "MAX_AGENT_STEPS y una llamada al modelo. Acotado por MAX_REJECTED_DECISIONS=3.",
    "content_key compara payloads normalizados ya truncados a MAX_TOOL_RESULT_CHARS; "
    "dos respuestas que difieran sólo más allá del truncado se consideran equivalentes.",
    "La regla de faceta quita current_inventory en 'movimientos de stock'. Si un usuario "
    "quisiera ambos, debe escribir un segundo indicador de inventario ('stock actual', "
    "'cuánto queda'); el detector lo conserva en ese caso.",
    "Los stems sueltos 'orden'/'ordenes' ahora exigen un calificador de compra/proveedor. "
    "Un phrasing de OC sin ninguno de esos términos cae a extraction=unknown, que NO "
    "bloquea el final: el agente responde igual, sólo pierde el bloqueo de cobertura.",
    "8.2D: MAX_AGENT_STEPS=5 satisface a la vez las dos restricciones que lo acotan "
    "(MAX_BLOCKED_FINALS_NO_PROGRESS+2=5 por abajo, decisions_affordable_best+1=5 por "
    "arriba), pero el techo de tokens sólo paga 4 decisiones: el camino de liberación "
    "tras 3 finales bloqueados necesita la 5a y caería a agent_token_budget con LLM "
    "real. steps_exceed_budget lo expone en cada turno. Lo resuelve 8.3, no un "
    "retoque del límite: la causa es que el system prompt se reenvía íntegro en cada "
    "decisión y se lleva ~77% del prompt del turno.",
    "8.2D: cached_tokens se OBSERVA pero no entra en budget_exceeded. Hasta medirlo "
    "con LLM real, el techo sigue contando el prefijo reenviado a precio completo.",
    "8.2D: goal_coverage no detecta 'ingresos' ni 'product_detail' como tipos de "
    "requisito aunque el scorer sí tiene esas familias. Añadirlos cambia qué finales "
    "se bloquean, así que no se toca sin medición: queda como deuda nombrada.",
]

ROLLBACK = [
    "Revertir por archivo: agent_progress.py (borrar), y git checkout de agent_loop.py, "
    "agent_config.py, evidence_store.py, goal_coverage.py, answer_verifier.py, service.py.",
    "Rollback parcial del anti-loop: en agent_loop.py sustituir el bloque de admisión por "
    "el gate anterior (last_empty + MAX_EMPTY_FOLLOWUPS) — reabre T07.",
    "Rollback parcial de extracción: restaurar _AUTO_SIGNALS['purchase_orders'] con "
    "'ordenes','orden','po' y limitar _INVENTORY_FACET_PHRASES a 'stock critico'.",
    "El desglose del verifier es aditivo: eliminar los kwargs nuevos de VerifyResult no "
    "cambia ninguna decisión ni el valor de 'failures'.",
    "AGENT=0 no ejecuta nada de esto: apagar ANDES_ASSISTANT_AGENT_ENABLED desactiva el "
    "AgentLoop completo sin tocar código.",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FASE 8.1G closure report")
    parser.add_argument("--llm", action="store_true", help="run P03/T07/M02/M04 x10 with the real LLM")
    parser.add_argument("--bench", action="store_true", help="run the full 64-case benchmark (implies --llm)")
    parser.add_argument("--out-dir", default=str(OUT_DIR))
    args = parser.parse_args(argv)
    want_llm = bool(args.llm or args.bench)

    _base_env()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "phase": "8.1G",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "baseline": BASELINE,
        "risks": RISKS,
        "rollback": ROLLBACK,
        "modified_files": MODIFIED_FILES,
        "llm_executed": False,
        "notes": [],
    }

    gateway = _gateway_ready()
    if not gateway:
        report["notes"].append("ANDES_AGENT_SERVICE_TOKEN missing: Gateway probes skipped")
    print("fase81g: deterministic probes")
    report["deterministic"] = run_deterministic(with_gateway=gateway)

    print("fase81g: test suites")
    # FASE 8.x — la suite corre EN PROCESO y algunos modulos manipulan las
    # banderas de brazo para probar ambos estados. Uno que no las devuelva
    # reetiqueta la corrida entera: medido, un test que hacia `pop()` en su
    # `finally` hizo que una corrida con ANALYSIS=1 se escribiera en el fichero
    # de `an0-pv0-pr0`. El brazo se fija ANTES y se comprueba despues.
    arm_before = arm_id()
    report["tests"] = run_tests()
    arm_after = arm_id()
    if arm_after != arm_before:
        partes = set(arm_before.split("-"))
        for prefijo, flag in ARM_FLAGS:
            os.environ[flag] = "1" if f"{prefijo}1" in partes else "0"
        report["notes"].append(
            f"la suite altero las banderas de brazo ({arm_before} -> {arm_after}); "
            f"restauradas a {arm_before}")
        print(f"fase81g: AVISO brazo alterado por la suite, restaurado a {arm_before}")
    report["arm_integrity"] = {"before_tests": arm_before, "after_tests": arm_after,
                               "stable": arm_after == arm_before}

    if want_llm:
        # FASE 9 — preflight ANTES de gastar turnos. Una comprobacion de
        # presencia acepta un placeholder: el 2026-09-21 una sesion exporto la
        # cadena "TU_KEY_YA_EXISTENTE", `_llm_ready()` dijo que si, y el arnes
        # ejecuto 354 turnos marcandolos "sin medir" sin que nadie dijera que la
        # credencial era el problema. Una llamada barata lo zanja.
        from evals.fase9_llm_preflight import require_usable_provider

        bloqueo = require_usable_provider(context="fase81g --llm")
        if bloqueo is not None:
            report["notes"].append(
                f"preflight LLM: {bloqueo['verdict']} — {bloqueo.get('advice')}")
            report["llm_preflight"] = bloqueo
            print("fase81g: SKIP llm (preflight)")
        elif not _llm_ready():
            report["notes"].append("ANDES_LLM_API_KEY missing: LLM sections skipped")
            print("fase81g: SKIP llm (no ANDES_LLM_API_KEY)")
        elif not _enable_agent():
            report["notes"].append("agent_loop_allowed=false: LLM sections skipped")
            print("fase81g: SKIP llm (agent_loop_allowed=false)")
        else:
            try:
                print(f"fase81g: stability core={STABILITY_CORE_IDS} x{STABILITY_RUNS}"
                      f" watch={stability_watch_ids(out_dir)} x{STABILITY_WATCH_RUNS}")
                report["stability"] = run_stability(out_dir)
                if args.bench:
                    print("fase81g: benchmark 64")
                    report["benchmark"] = run_benchmark(out_dir)
                report["llm_executed"] = True
            finally:
                _restore_safe_env()
    _restore_safe_env()

    # El informe declara SU brazo y el estado del hermano. Sin esto, un fichero
    # suelto no dice bajo que configuracion se produjo, y comparar dos corridas
    # se vuelve un acto de fe.
    report["arm"] = _arm_report(
        out_dir,
        measurement_valid=bool((report.get("benchmark") or {})
                               .get("measurement_valid", True)))
    report["env_restored"] = {
        "ANDES_ASSISTANT_AGENT_ENABLED": os.environ.get("ANDES_ASSISTANT_AGENT_ENABLED"),
        "ANDES_ASSISTANT_NL_ENABLED": os.environ.get("ANDES_ASSISTANT_NL_ENABLED"),
        "ANDES_ORCH_PLANNER": os.environ.get("ANDES_ORCH_PLANNER"),
    }

    # Una corrida determinista NO puede pisar el informe de una con LLM del mismo
    # brazo. Paso de verdad: dos pre-vuelos deterministas sobrescribieron los
    # informes de la segunda corrida A/B, que habia costado dos ejecuciones
    # completas con modelo real. Los .jsonl sobrevivieron porque se escriben
    # incrementalmente; los informes no.
    #
    # El brazo distingue CONFIGURACION (analysis, provenance) pero no si hubo
    # LLM, y son dos dimensiones distintas. Una corrida con LLM conserva el
    # nombre documentado; una determinista se aparta a '.det'.
    stem = "fase81_final_report" if report["llm_executed"] else "fase81_final_report.det"
    json_path = arm_path(out_dir, stem, ".json")
    txt_path = arm_path(out_dir, stem, ".txt")
    # El JSON va PRIMERO y por separado: es la forma cruda y la que permite
    # reconstruir el informe a mano si el renderizador de texto falla. Perder el
    # informe de una corrida con LLM real cuesta tiempo y dinero que no se
    # recuperan, asi que ninguna de las dos escrituras puede llevarse a la otra.
    json_path.write_text(_scrub(json.dumps(report, ensure_ascii=False, indent=2)), encoding="utf-8")
    print(f"fase81g: wrote {json_path}")
    try:
        txt_path.write_text(_scrub(_txt_report(report)), encoding="utf-8")
        print(f"fase81g: wrote {txt_path}")
    except Exception as exc:  # noqa: BLE001
        txt_path.write_text(
            f"El informe de texto fallo: {type(exc).__name__}: {exc}\n"
            f"Los datos completos estan en {json_path.name}.\n", encoding="utf-8")
        print(f"fase81g: TXT fallo ({type(exc).__name__}); JSON intacto en {json_path}")
    return 0 if report["tests"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
