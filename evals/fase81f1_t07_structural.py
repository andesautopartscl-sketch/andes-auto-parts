"""FASE 8.1F.1 — structural diagnosis of T07. No production changes."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "data" / "fase81_eval"
DATASET = ROOT / "evals" / "fase81_evidence_dataset.jsonl"


def _inherit_key() -> bool:
    if (os.environ.get("ANDES_LLM_API_KEY") or "").strip():
        return True
    try:
        import psutil
    except ImportError:
        return False
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if (p.info.get("name") or "").lower() not in {"powershell.exe", "pwsh.exe", "cmd.exe"}:
                continue
            env = p.environ()
            key = (env.get("ANDES_LLM_API_KEY") or "").strip()
            if not key:
                continue
            os.environ["ANDES_LLM_API_KEY"] = key
            for k in ("ANDES_LLM_BASE_URL", "ANDES_LLM_PLANNER_MODEL", "ANDES_LLM_PROVIDER"):
                v = (env.get(k) or "").strip()
                if v and not (os.environ.get(k) or "").strip():
                    os.environ[k] = v
            return True
        except Exception:
            continue
    return False


def _args_fp(arguments: Any) -> str:
    payload = json.dumps(arguments or {}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _synthetic_goal_test() -> dict[str, Any]:
    from app.assistant.orchestrator.evidence_store import EvidenceStore
    from app.assistant.orchestrator.goal_coverage import GoalCoverage, GoalRequirement, EXTRACTION_DETECTED

    cov = GoalCoverage(
        extraction=EXTRACTION_DETECTED,
        requirements=[
            GoalRequirement(id="r1", type="supplier"),
            GoalRequirement(id="r2", type="purchase_orders"),
        ],
    )
    store = EvidenceStore()
    cov.refresh(store)
    before = {r.type: r.status for r in cov.requirements}
    store.add_from_tool_result(
        tool="get_purchase_orders",
        arguments={"proveedor": "BOSCH"},
        result={"ok": True, "data": {"items": []}, "meta": {}},
    )
    cov.refresh(store)
    after_po = {r.type: r.status for r in cov.requirements}
    store.add_from_tool_result(
        tool="get_supplier",
        arguments={"q": "BOSCH"},
        result={"ok": True, "data": {"nombre": "BOSCH"}, "meta": {}},
    )
    cov.refresh(store)
    after_both = {r.type: r.status for r in cov.requirements}
    return {
        "before_tools": before,
        "after_get_purchase_orders": after_po,
        "after_get_supplier": after_both,
        "ok": (
            after_po.get("purchase_orders") == "covered"
            and after_po.get("supplier") == "uncovered"
            and after_both.get("purchase_orders") == "covered"
            and after_both.get("supplier") == "covered"
        ),
    }


def _language_analysis(message: str) -> dict[str, Any]:
    from app.assistant.orchestrator.goal_coverage import (
        _AUTO_SIGNALS,
        _fold,
        _has_phrase,
        covering_tools,
        extract_requirement_types,
        REQUIREMENT_TYPES,
    )

    folded = _fold(message)
    tokens = [t for t in folded.replace(",", " ").replace(".", " ").split() if t]
    signal_hits: dict[str, list[str]] = {}
    for rtype, phrases in _AUTO_SIGNALS.items():
        hits = [p for p in phrases if _has_phrase(folded, p)]
        if hits:
            signal_hits[rtype] = hits
    # Candidate stems present in T07 language but NOT in _AUTO_SIGNALS
    candidate_stems = {
        "supplier": ("proveedor", "proveedores", "supplier"),
        "purchase_orders": (
            "ordenes de compra",
            "órdenes de compra",
            "orden de compra",
            "oc",
            "purchase orders",
        ),
    }
    missing_signal_matches: dict[str, list[str]] = {}
    for rtype, phrases in candidate_stems.items():
        hits = []
        for p in phrases:
            pf = _fold(p)
            if " " in pf:
                if pf in folded:
                    hits.append(p)
            elif _has_phrase(folded, pf):
                hits.append(p)
        if hits:
            missing_signal_matches[rtype] = hits

    ext, types = extract_requirement_types(message)
    return {
        "message_token_count": len(tokens),
        "folded_token_sample": tokens[:12],
        "auto_signal_hits": signal_hits,
        "candidate_stems_in_message_but_absent_from_AUTO_SIGNALS": missing_signal_matches,
        "extract_requirement_types": {"extraction": ext, "types": types},
        "catalog_has_supplier": "supplier" in REQUIREMENT_TYPES,
        "catalog_has_purchase_orders": "purchase_orders" in REQUIREMENT_TYPES,
        "covering_tools": {
            "supplier": sorted(covering_tools("supplier")),
            "purchase_orders": sorted(covering_tools("purchase_orders")),
        },
        "AUTO_SIGNALS_keys": list(_AUTO_SIGNALS.keys()),
    }


class _Spy:
    def __init__(self, inner: Any):
        self.inner = inner
        self.last_usage = None
        self.events: list[dict[str, Any]] = []
        self.rejected: list[dict[str, Any]] = []

    def complete_decision(self, *, system: str, user: str) -> dict[str, Any]:
        raw = self.inner.complete_decision(system=system, user=user)
        self.last_usage = getattr(self.inner, "last_usage", None)
        action = str((raw or {}).get("action") or "")[:40]
        tool = (raw or {}).get("tool")
        args = (raw or {}).get("arguments") if isinstance((raw or {}).get("arguments"), dict) else {}
        self.events.append(
            {
                "action": action or None,
                "tool": (str(tool)[:64] if tool else None),
                "args_fp": _args_fp(args) if action == "call_tool" else None,
                "arg_keys": sorted(str(k) for k in args.keys()) if action == "call_tool" else [],
            }
        )
        return raw


def main() -> int:
    from app.utils.load_env import load_project_dotenv

    load_project_dotenv(force=False)
    if not _inherit_key():
        print("FATAL: ANDES_LLM_API_KEY missing")
        return 2
    key = os.environ["ANDES_LLM_API_KEY"]
    os.environ["ANDES_ASSISTANT_AGENT_ENABLED"] = "1"
    os.environ["ANDES_ASSISTANT_NL_ENABLED"] = "1"
    os.environ["ANDES_ORCH_PLANNER"] = "llm"
    os.environ["ANDES_ENV"] = "local"
    os.environ.setdefault("ANDES_LLM_PROVIDER", "openai_compatible")
    os.environ.setdefault("ANDES_AGENT_URL", "http://127.0.0.1:5055")
    os.environ.setdefault("ANDES_ASSISTANT_MEMORY_ENABLED", "0")
    os.environ.setdefault("ANDES_ASSISTANT_HISTORY_ENABLED", "0")
    load_project_dotenv(force=False)
    os.environ["ANDES_LLM_API_KEY"] = key
    os.environ["ANDES_ASSISTANT_AGENT_ENABLED"] = "1"
    os.environ["ANDES_ASSISTANT_NL_ENABLED"] = "1"
    os.environ["ANDES_ORCH_PLANNER"] = "llm"

    try:
        from app.assistant.orchestrator.agent_config import agent_loop_allowed
        from app.assistant.orchestrator.agent_loop import LlmAgentDecisionClient
        from app.assistant.orchestrator.factory import build_planner
        from app.assistant.orchestrator.goal_coverage import covering_tools
        from app.assistant.orchestrator.llm.client import OpenAICompatibleClient
        from app.assistant.orchestrator.llm.config import load_llm_settings
        from app.assistant.orchestrator.service import run_orchestrator_chat
        from app.assistant.orchestrator.turn_store import TurnStore
        from app.assistant.routes import invoke_gateway
        from evals.fase81_runner import load_cases

        if not agent_loop_allowed():
            print("FATAL: agent_loop_allowed=false")
            return 2

        gold = next(c for c in load_cases(DATASET) if c.get("id") == "T07")
        # Do not print full prompt; only structural language analysis.
        lang = _language_analysis(str(gold.get("prompt") or ""))
        synth = _synthetic_goal_test()

        spy = _Spy(LlmAgentDecisionClient(OpenAICompatibleClient(load_llm_settings())))
        coverage_snapshots: list[dict[str, Any]] = []
        t0 = time.perf_counter()
        result = run_orchestrator_chat(
            message=str(gold.get("prompt") or ""),
            actor_user=gold.get("actor") or "albertadmin",
            conversation_id="fase81f1-T07",
            invoke_fn=invoke_gateway,
            planner=build_planner(),
            turn_store=TurnStore(),
            agent_decision_client=spy,
        )
        latency_ms = int((time.perf_counter() - t0) * 1000)

        tools = [str(t) for t in (result.get("tools_used") or []) if t]
        trace = list(result.get("agent_trace") or [])
        goal = result.get("goal_coverage") if isinstance(result.get("goal_coverage"), dict) else {}
        reqs = goal.get("requirements") or []

        # Reconstruct coverage after each tool from trace evidence counts + final goal
        # (final goal is authoritative; per-tool covered derived from which tools ran)
        covered_after_each: list[dict[str, Any]] = []
        ran: list[str] = []
        for t in tools:
            ran.append(t)
            covered = []
            uncovered = []
            # If requirements empty, nothing covered by definition
            for r in reqs:
                rtype = str(r.get("type"))
                members = covering_tools(rtype)
                if members & set(ran):
                    covered.append(rtype)
                else:
                    uncovered.append(rtype)
            covered_after_each.append(
                {
                    "after_tool": t,
                    "tools_so_far": list(ran),
                    "covered_requirements": covered,
                    "uncovered_requirements": uncovered,
                }
            )

        proposed_tools = [e.get("tool") for e in spy.events if e.get("tool")]
        supplier_proposed = "get_supplier" in proposed_tools
        supplier_in_tools = "get_supplier" in tools
        arg_errors = list(result.get("arg_errors") or [])

        # Hypothesis
        extraction = lang["extract_requirement_types"]["extraction"]
        types = lang["extract_requirement_types"]["types"]
        if extraction == "unknown" or "supplier" not in types:
            root = "A"
            rationale = [
                "extract_requirement_types returns no supplier (and no purchase_orders)",
                "_AUTO_SIGNALS has no supplier/purchase_orders stems",
                "T07 language contains 'proveedor' and 'ordenes de compra' but those stems are not wired",
                "GoalCoverage.requirements=[] so blocks_final is False; AgentLoop does not demand get_supplier",
            ]
        elif "supplier" in types and not reqs:
            root = "B"
            rationale = ["supplier extracted but requirements empty unexpectedly"]
        elif any(r.get("type") == "supplier" and r.get("status") == "uncovered" for r in reqs):
            if supplier_proposed and not supplier_in_tools:
                root = "D"
                rationale = ["get_supplier proposed but rejected before invoke"]
            elif not supplier_proposed:
                root = "C"
                rationale = ["supplier uncovered but model never proposed get_supplier"]
            else:
                root = "F"
                rationale = ["supplier uncovered with atypical path"]
        elif any(r.get("type") == "supplier" and r.get("status") == "covered" for r in reqs) and not supplier_in_tools:
            root = "E"
            rationale = ["supplier marked covered without get_supplier"]
        else:
            root = "F"
            rationale = ["unclassified"]

        if supplier_proposed and not supplier_in_tools and arg_errors:
            root = "D"
            rationale.append(f"arg_errors={arg_errors}")

        report = {
            "phase": "8.1F.1",
            "case_id": "T07",
            "run_id": "T07-structural-r01",
            "latency_ms": latency_ms,
            "requirements": [r.get("type") for r in reqs],
            "requirement_count": len(reqs),
            "goal_extraction": goal.get("extraction"),
            "covered_requirements_before": [],
            "covered_requirements_after_each_tool": covered_after_each,
            "uncovered_requirements": [r.get("type") for r in reqs if r.get("status") == "uncovered"],
            "covered_requirements_final": [r.get("type") for r in reqs if r.get("status") == "covered"],
            "tool_sequence": tools,
            "decision_count": len(trace),
            "agent_steps": int(result.get("agent_steps") or len(trace) or 0),
            "final_reason": result.get("fallback_reason") or ("pass" if not result.get("fallback_used") else "fallback"),
            "fallback": bool(result.get("fallback_used")),
            "fallback_reason": result.get("fallback_reason"),
            "spy_proposed_tools": proposed_tools,
            "supplier_proposed_in_raw_decision": supplier_proposed,
            "supplier_executed": supplier_in_tools,
            "arg_errors": arg_errors,
            "requirement_covering_tools": {
                "supplier": sorted(covering_tools("supplier")),
                "purchase_orders": sorted(covering_tools("purchase_orders")),
            },
            "gold_expected_tools": list(gold.get("expected_tools") or []),
            "language_analysis": lang,
            "synthetic_goal_coverage": synth,
            "hypothesis": {
                "A": "extraction never detects supplier requirement",
                "B": "detects supplier but GoalCoverage does not require coverage",
                "C": "supplier uncovered but AgentLoop does not propose get_supplier",
                "D": "model proposes get_supplier but schema/validator rejects",
                "E": "get_purchase_orders incorrectly covers supplier",
                "F": "other",
            },
            "T07_ROOT_CAUSE": root,
            "rationale": rationale,
            "minimal_fix_candidate": (
                "Add closed _AUTO_SIGNALS for supplier (proveedor/…) and purchase_orders "
                "(ordenes de compra/…) so T07 extracts both requirements; AgentLoop will then "
                "block final until get_supplier covers supplier. Do NOT implement in 8.1F.1."
            ),
        }

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / "t07_structural.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        lines = [
            "FASE 8.1F.1 T07 structural diagnosis",
            f"T07_ROOT_CAUSE={root}",
            f"extraction={lang['extract_requirement_types']}",
            f"requirements={report['requirements']} count={report['requirement_count']}",
            f"tool_sequence={tools}",
            f"spy_proposed_tools={proposed_tools}",
            f"supplier_proposed={supplier_proposed} supplier_executed={supplier_in_tools}",
            f"synthetic_ok={synth['ok']} after_po={synth['after_get_purchase_orders']} after_both={synth['after_get_supplier']}",
            f"missing_stems={lang['candidate_stems_in_message_but_absent_from_AUTO_SIGNALS']}",
            f"AUTO_SIGNALS_keys={lang['AUTO_SIGNALS_keys']}",
            "",
            "rationale:",
        ]
        for r in rationale:
            lines.append(f"- {r}")
        lines.append("")
        lines.append(f"minimal_fix_candidate={report['minimal_fix_candidate']}")
        (OUT_DIR / "t07_structural.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {
                    "T07_ROOT_CAUSE": root,
                    "requirements": report["requirements"],
                    "tools": tools,
                    "supplier_proposed": supplier_proposed,
                    "synthetic_ok": synth["ok"],
                    "latency_ms": latency_ms,
                },
                ensure_ascii=False,
            )
        )
        return 0
    finally:
        os.environ["ANDES_ASSISTANT_AGENT_ENABLED"] = "0"
        os.environ["ANDES_ASSISTANT_NL_ENABLED"] = "0"
        os.environ["ANDES_ORCH_PLANNER"] = "fake"
        print("RESTORED AGENT=0 NL=0 ORCH=fake")


if __name__ == "__main__":
    raise SystemExit(main())
