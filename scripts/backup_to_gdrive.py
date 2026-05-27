"""
Sube respaldo de andes.db a Google Drive.
Uso: python scripts/backup_to_gdrive.py
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["ANDES_SKIP_AUTO_CREATE_APP"] = "1"

from app.utils.gdrive_backup import run_gdrive_backup
from app.utils.load_env import load_project_dotenv

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backup_to_gdrive")


def main() -> int:
    load_project_dotenv()
    result = run_gdrive_backup(logger_instance=logger)
    if result.success:
        logger.info(
            "Éxito | fecha=%s | archivo=%s | tamaño=%s bytes",
            result.ran_at,
            result.filename,
            result.size_bytes,
        )
        return 0
    logger.error("Error | fecha=%s | mensaje=%s", result.ran_at, result.message)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
