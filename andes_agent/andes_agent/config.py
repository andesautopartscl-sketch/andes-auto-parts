"""Runtime configuration. Secrets come only from the environment."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ALLOWED_ENVIRONMENTS = frozenset({"local", "production"})
PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PROJECT_ROOT.parent
DEFAULT_AUDIT_PATH = PROJECT_ROOT / "data" / "audit.jsonl"
_DOTENV_LOADED = False


def _load_dotenv() -> None:
    """Load ANDES_* keys from .env without overriding a live environment. Never log values."""
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True
    for candidate in (PROJECT_ROOT / ".env", REPO_ROOT / ".env"):
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if not key.startswith("ANDES_"):
                continue
            if key in os.environ:
                continue
            os.environ[key] = value.strip().strip('"').strip("'")
        break


class ConfigError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class Settings:
    environment: str
    service_token: str
    host: str
    port: int
    rate_limit_max: int
    rate_limit_window_seconds: int
    audit_path: Path
    erp_base_url: str


def _read_env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def parse_environment(raw: str | None = None) -> str:
    value = (raw if raw is not None else _read_env("ANDES_ENV", "local")).strip().lower()
    if value not in ALLOWED_ENVIRONMENTS:
        raise ConfigError("invalid_environment", "ANDES_ENV must be 'local' or 'production'")
    return value


def load_settings() -> Settings:
    _load_dotenv()
    environment = parse_environment()
    token = _read_env("ANDES_AGENT_SERVICE_TOKEN")
    if environment == "production" and not token:
        raise ConfigError("missing_service_token", "ANDES_AGENT_SERVICE_TOKEN is required in production")
    host = _read_env("ANDES_AGENT_HOST", "127.0.0.1") or "127.0.0.1"
    try:
        port = int(_read_env("ANDES_AGENT_PORT", "5055") or "5055")
    except ValueError as orig:
        raise ConfigError("invalid_port", "ANDES_AGENT_PORT must be an integer") from orig
    try:
        rate_max = int(_read_env("ANDES_AGENT_RATE_LIMIT_MAX", "60") or "60")
        rate_window = int(_read_env("ANDES_AGENT_RATE_LIMIT_WINDOW", "60") or "60")
    except ValueError as orig:
        raise ConfigError("invalid_rate_limit", "Rate limit settings must be integers") from orig
    erp_base_url = _read_env("ANDES_ERP_BASE_URL", "http://127.0.0.1:5000") or "http://127.0.0.1:5000"
    return Settings(
        environment=environment,
        service_token=token,
        host=host,
        port=port,
        rate_limit_max=max(1, rate_max),
        rate_limit_window_seconds=max(1, rate_window),
        audit_path=DEFAULT_AUDIT_PATH,
        erp_base_url=erp_base_url,
    )
