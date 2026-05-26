"""Carga variables desde .env en la raíz del proyecto (con o sin python-dotenv)."""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


def project_env_path() -> Path:
    return _ENV_PATH


def load_project_dotenv(*, force: bool = True) -> bool:
    """
    Carga .env en os.environ.

    force=True (default): valores del archivo reemplazan los ya definidos en el
    proceso (evita quedar con SII_* vacíos de un arranque anterior).
    """
    env_path = _ENV_PATH
    if not env_path.is_file():
        logger.warning("No se encontró .env en %s", env_path)
        return False
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=force)
        return True
    except ImportError:
        pass
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        if force or key not in os.environ:
            os.environ[key] = value
    return True


def _mask_env(value: str | None, visible: int = 4) -> str:
    v = (value or "").strip()
    if not v:
        return "(vacío)"
    if len(v) <= visible:
        return f"{v}…"
    return f"{v[:visible]}…"


def log_sii_env_startup(app_logger=None) -> None:
    """Registra en log el estado de variables SII (solo prefijos, sin secretos completos)."""
    load_project_dotenv(force=True)
    log = app_logger or logger
    provider = (os.environ.get("SII_API_PROVIDER") or "").strip()
    api_key = (os.environ.get("SII_API_KEY") or "").strip()
    rut_emp = (os.environ.get("SII_RUT_EMPRESA") or "").strip()
    rut_auth = (os.environ.get("SII_RUT") or rut_emp).strip()
    password = (os.environ.get("SII_PASSWORD") or "").strip()
    ambiente = (os.environ.get("SII_AMBIENTE") or "").strip()
    log.info("SII .env path: %s (exists=%s)", _ENV_PATH, _ENV_PATH.is_file())
    log.info(
        "SII env → provider=%s | api_key=%s | rut_empresa=%s | rut=%s | password=%s | ambiente=%s",
        provider or "(vacío)",
        _mask_env(api_key),
        _mask_env(rut_emp),
        _mask_env(rut_auth),
        _mask_env(password),
        ambiente or "(vacío)",
    )
    try:
        from app.sii_sync.sii_service import SIIService

        svc = SIIService()
        log.info(
            "SIIService.configured()=%s (faltan: %s)",
            svc.configured(),
            svc.missing_config_labels() or "ninguno",
        )
    except Exception as exc:
        log.warning("No se pudo evaluar SIIService al arranque: %s", exc)
