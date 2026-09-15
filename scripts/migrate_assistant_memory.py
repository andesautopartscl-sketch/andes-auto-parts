#!/usr/bin/env python3
"""Idempotent migration: assistant_memory_slot (FASE 7B.1).

Usage:
  .\\.venv\\Scripts\\python.exe scripts/migrate_assistant_memory.py
  .\\.venv\\Scripts\\python.exe scripts/migrate_assistant_memory.py --db data/andes.db

Does not enable memory (ANDES_ASSISTANT_MEMORY_ENABLED stays default 0).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.assistant.orchestrator.memory_store import SCHEMA_SQL, MemoryStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate assistant memory tables")
    parser.add_argument(
        "--db",
        default=os.environ.get("ANDES_ASSISTANT_MEMORY_DB")
        or os.environ.get("ANDES_ASSISTANT_HISTORY_DB")
        or str(ROOT / "data" / "andes.db"),
        help="SQLite database path",
    )
    args = parser.parse_args()
    path = Path(args.db)
    store = MemoryStore(path=path)
    store.ensure_schema()
    print(f"OK assistant memory schema at {path}")
    assert "assistant_memory_slot" in SCHEMA_SQL
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
