#!/usr/bin/env python3
"""Purge expired / soft-deleted assistant history (FASE 7A)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.assistant.orchestrator.history_store import HistoryStore


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="", help="SQLite path override")
    args = parser.parse_args()
    store = HistoryStore(path=Path(args.db)) if args.db else HistoryStore()
    store.ensure_schema()
    result = store.purge_expired()
    print(f"purged conversations={result['conversations']} turns={result['turns']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
