#!/usr/bin/env python3
"""Idempotent migration: assistant_conversation + assistant_turn (FASE 7A).

Usage:
  .\\.venv\\Scripts\\python.exe scripts/migrate_assistant_history.py
  .\\.venv\\Scripts\\python.exe scripts/migrate_assistant_history.py --db data/andes.db

Does not enable history (ANDES_ASSISTANT_HISTORY_ENABLED stays default 0).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.assistant.orchestrator.history_store import HistoryStore, SCHEMA_SQL  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate assistant history tables")
    parser.add_argument(
        "--db",
        default=os.environ.get("ANDES_ASSISTANT_HISTORY_DB")
        or str(ROOT / "data" / "andes.db"),
        help="SQLite database path",
    )
    args = parser.parse_args()
    path = Path(args.db)
    store = HistoryStore(path=path)
    store.ensure_schema()
    print(f"OK assistant history schema at {path}")
    # Touch SCHEMA_SQL so unused-import linters keep the constant referenced
    assert "assistant_conversation" in SCHEMA_SQL
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
