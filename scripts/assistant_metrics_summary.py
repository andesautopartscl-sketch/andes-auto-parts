#!/usr/bin/env python3
"""Print a local assistant metrics summary (no PII / no prompts).

Usage:
  .\\.venv\\Scripts\\python.exe scripts/assistant_metrics_summary.py
  .\\.venv\\Scripts\\python.exe scripts/assistant_metrics_summary.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.assistant.orchestrator.metrics import MetricsStore, get_default_metrics_store


def _print_human(summary: dict) -> None:
    print("=== Andes Assistant metrics (local) ===")
    print(f"source: {summary.get('source')}")
    file_stats = summary.get("file_stats") or {}
    print(
        f"file turns: {file_stats.get('turns', 0)} | "
        f"avg latency ms: {file_stats.get('avg_latency_ms', 0)} | "
        f"avg llm ms: {file_stats.get('avg_llm_latency_ms', 0)}"
    )
    hotspots = summary.get("hotspots") or {}
    print("hotspots:")
    for key in (
        "too_many_tools",
        "clarifications",
        "high_latency",
        "errors",
        "permission_denied",
        "agent_unavailable",
    ):
        print(f"  {key}: {hotspots.get(key, 0)}")
    errors = file_stats.get("error_codes") or {}
    if errors:
        print("error_codes:")
        for code, count in sorted(errors.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {code}: {count}")
    tools = file_stats.get("tools_histogram") or {}
    if tools:
        print("tools:")
        for tool, count in sorted(tools.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {tool}: {count}")
    convos = summary.get("in_memory_conversations") or []
    print(f"in-memory conversations: {len(convos)}")
    for c in convos[:10]:
        print(
            f"  {c.get('conversation_id')} turns={c.get('turns')} "
            f"invokes={c.get('invokes')} clarify={c.get('clarifications')} "
            f"replans={c.get('replans')} errors={c.get('errors')} "
            f"alerts={','.join(c.get('alerts') or []) or '-'}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Assistant metrics summary (safe aggregates)")
    parser.add_argument("--json", action="store_true", help="Print raw JSON summary")
    parser.add_argument(
        "--path",
        default="",
        help="Override metrics JSONL path (default: ANDES_ASSISTANT_METRICS_PATH)",
    )
    args = parser.parse_args()

    store = MetricsStore(path=Path(args.path)) if args.path else get_default_metrics_store()
    summary = store.summary()
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        _print_human(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
