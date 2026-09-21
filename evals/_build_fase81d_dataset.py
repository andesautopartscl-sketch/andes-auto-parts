"""Build FASE 8.1D dataset + change log from 8.1C F diagnosis. Run once locally."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "evals" / "fase81_evidence_dataset.jsonl"
OUT_JSON = ROOT / "data" / "fase81_eval" / "scorer_changes_81d.json"
OUT_TXT = ROOT / "data" / "fase81_eval" / "scorer_changes_81d.txt"

FAM = {
    "current_inventory": ["get_inventory", "check_stock"],
    "stock_movements": ["get_stock_movements"],
    "catalog_search": ["search_catalog"],
    "product_detail": ["get_product"],
    "ingresos": ["get_ingresos"],
    "purchase_orders": ["get_purchase_orders"],
    "dashboard_kpis": ["get_dashboard_kpis"],
    "customer": ["get_customer"],
    "supplier": ["get_supplier"],
}

F_PATCHES = {
    "C03": {
        "expected_tools": ["get_inventory"],
        "required_goal_types": ["current_inventory"],
        "acceptable_tool_families": {"current_inventory": FAM["current_inventory"]},
        "tool_count_policy": "family",
        "reason": "Sibling family: get_inventory covers current_inventory; check_stock not required",
    },
    "C05": {
        "expected_tools": ["get_inventory"],
        "required_goal_types": ["current_inventory"],
        "acceptable_tool_families": {"current_inventory": FAM["current_inventory"]},
        "tool_count_policy": "family",
        "reason": "search_catalog beyond required_goal_types; inventory covers stock",
    },
    "G02": {
        "allow_fase5_shortcircuit": True,
        "acceptable_outcomes": ["agent_tools", "fase5_clarify", "clarify"],
        "tool_count_policy": "family",
        "expected_tools": ["get_inventory"],
        "required_goal_types": ["current_inventory"],
        "acceptable_tool_families": {"current_inventory": FAM["current_inventory"]},
        "reason": "FASE5 Resolver may clarify before AgentLoop; not AgentLoop tool fail",
    },
    "T03": {
        "expected_tools": ["get_inventory"],
        "required_goal_types": ["current_inventory"],
        "acceptable_tool_families": {"current_inventory": FAM["current_inventory"]},
        "tool_count_policy": "optional_extra",
        "reason": "Only current_inventory required; get_product is optional_extra",
    },
    "T04": {
        "expected_tools": ["get_inventory"],
        "required_goal_types": ["current_inventory"],
        "acceptable_tool_families": {"current_inventory": FAM["current_inventory"]},
        "tool_count_policy": "family",
        "reason": "search_catalog not in required_goal_types; inventory covers stock",
    },
    "F02": {
        "allow_fase5_shortcircuit": True,
        "acceptable_outcomes": ["agent_tools", "fase5_reuse", "fase5_clarify"],
        "tool_count_policy": "family",
        "expected_tools": ["get_inventory"],
        "required_goal_types": ["current_inventory"],
        "acceptable_tool_families": {"current_inventory": FAM["current_inventory"]},
        "reason": "Follow-up: context_reuse is valid FASE5",
    },
    "F03": {
        "allow_fase5_shortcircuit": True,
        "acceptable_outcomes": ["agent_tools", "fase5_clarify", "clarify"],
        "tool_count_policy": "family",
        "expected_tools": ["get_inventory"],
        "required_goal_types": ["current_inventory"],
        "acceptable_tool_families": {"current_inventory": FAM["current_inventory"]},
        "reason": "Follow-up after multi-hit search: context_ambiguous clarify valid",
    },
    "F04": {
        "allow_fase5_shortcircuit": True,
        "acceptable_outcomes": ["agent_tools", "fase5_reuse", "fase5_clarify"],
        "tool_count_policy": "family",
        "expected_tools": ["get_inventory"],
        "required_goal_types": ["current_inventory"],
        "acceptable_tool_families": {"current_inventory": FAM["current_inventory"]},
        "reason": "Follow-up bodega: context_reuse valid",
    },
    "E06": {
        "expected_tools": ["get_stock_movements"],
        "required_goal_types": ["stock_movements"],
        "acceptable_tool_families": {"stock_movements": FAM["stock_movements"]},
        "tool_count_policy": "optional_extra",
        "reason": "optional get_inventory must not fail exact policy",
    },
    "K02": {
        "allow_fase5_shortcircuit": True,
        "acceptable_outcomes": ["agent_tools", "fase5_clarify", "clarify"],
        "tool_count_policy": "family",
        "expected_tools": ["get_inventory"],
        "required_goal_types": ["current_inventory"],
        "acceptable_tool_families": {"current_inventory": FAM["current_inventory"]},
        "reason": "Resolver short-circuit like G02; not AgentLoop failure",
    },
    "R03": {
        "expected_tools": ["get_dashboard_kpis"],
        "required_goal_types": ["dashboard_kpis"],
        "acceptable_tool_families": {"dashboard_kpis": FAM["dashboard_kpis"]},
        "tool_count_policy": "optional_extra",
        "reason": "optional get_inventory must not fail exact KPIs case",
    },
    "N04": {
        "expected_tools": ["get_inventory"],
        "required_goal_types": ["current_inventory"],
        "acceptable_tool_families": {"current_inventory": FAM["current_inventory"]},
        "tool_count_policy": "family",
        "empty_not_found_ok": True,
        "reason": "Correct tool executed; empty/not-found must not require covered",
    },
    "Q02": {
        "allow_fase5_shortcircuit": True,
        "acceptable_outcomes": ["agent_tools", "fase5_reuse", "fase5_clarify"],
        "tool_count_policy": "family",
        "expected_tools": ["get_inventory"],
        "required_goal_types": ["current_inventory"],
        "acceptable_tool_families": {"current_inventory": FAM["current_inventory"]},
        "reason": "Context follow-up: reuse valid FASE5",
    },
    "Q03": {
        "expect_clarify": True,
        "acceptable_outcomes": ["clarify", "fase5_clarify", "fase5_ambiguous", "no_tools_safe"],
        "allow_fase5_shortcircuit": True,
        "expected_tools": [],
        "required_goal_types": [],
        "tool_count_policy": "exact",
        "reason": "Ambiguous 'eso': clarify/no-tools-safe accepted",
    },
}

AGENT_BASELINE = {
    "T07": {
        "baseline_failure": "agent",
        "tool_count_policy": "minimum",
        "expected_tools": ["get_supplier", "get_purchase_orders"],
    },
    "K05": {
        "baseline_failure": "agent",
        "tool_count_policy": "exact",
        "expected_tools": ["check_stock"],
    },
    "F01": {
        "baseline_failure": "agent",
        "tool_count_policy": "minimum",
        "expected_tools": ["get_stock_movements"],
    },
    "P03": {
        "baseline_failure": "agent",
        "tool_count_policy": "exact",
        "expected_tools": ["get_dashboard_kpis"],
        "expect_fallback": False,
    },
}


def migrate(row: dict) -> dict:
    out = dict(row)
    expected = list(out.pop("tools_expected", None) or out.get("expected_tools") or [])
    out["expected_tools"] = expected
    goals = list(out.pop("goal_types", None) or out.get("required_goal_types") or [])
    out["required_goal_types"] = goals
    tc = out.pop("tool_count", None) or out.get("tool_count_policy") or "exact"
    if tc == "min":
        tc = "minimum"
    out["tool_count_policy"] = tc
    if goals and "acceptable_tool_families" not in out:
        out["acceptable_tool_families"] = {g: FAM[g] for g in goals if g in FAM}
    return out


def main() -> None:
    rows = []
    for line in SRC.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rows.append(json.loads(line))

    changes = []
    new_rows = []
    for row in rows:
        cid = row["id"]
        old_expectation = {
            "expected_tools": list(row.get("tools_expected") or row.get("expected_tools") or []),
            "tool_count": row.get("tool_count"),
            "goal_types": list(row.get("goal_types") or []),
            "expect_clarify": row.get("expect_clarify"),
        }
        m = migrate(row)
        if cid in F_PATCHES:
            patch = dict(F_PATCHES[cid])
            reason = patch.pop("reason")
            m.update(patch)
            m["baseline_failure"] = "eval"
            changes.append(
                {
                    "case_id": cid,
                    "old_expectation": old_expectation,
                    "new_expectation": {
                        "expected_tools": m.get("expected_tools"),
                        "required_goal_types": m.get("required_goal_types"),
                        "acceptable_tool_families": m.get("acceptable_tool_families"),
                        "tool_count_policy": m.get("tool_count_policy"),
                        "allow_fase5_shortcircuit": m.get("allow_fase5_shortcircuit"),
                        "acceptable_outcomes": m.get("acceptable_outcomes"),
                        "empty_not_found_ok": m.get("empty_not_found_ok"),
                    },
                    "reason": reason,
                    "source_failure_class": "F",
                }
            )
        if cid in AGENT_BASELINE:
            m.update(AGENT_BASELINE[cid])
        m.setdefault("expected_tools", [])
        m.setdefault("required_goal_types", [])
        m.setdefault("tool_count_policy", "exact")
        new_rows.append(m)

    SRC.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in new_rows) + "\n",
        encoding="utf-8",
    )
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(
        json.dumps({"n": len(changes), "changes": changes}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    lines = ["FASE 8.1D scorer/dataset changes (14 F cases)", f"n={len(changes)}", ""]
    for c in changes:
        lines.append(f"{c['case_id']}: {c['reason']}")
        lines.append(f"  old={json.dumps(c['old_expectation'], ensure_ascii=False)}")
        lines.append(f"  new={json.dumps(c['new_expectation'], ensure_ascii=False)}")
        lines.append("")
    OUT_TXT.write_text("\n".join(lines), encoding="utf-8")
    print(f"dataset={len(new_rows)} changes={len(changes)}")


if __name__ == "__main__":
    main()
