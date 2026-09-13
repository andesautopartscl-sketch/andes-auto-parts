"""HTTP adapter toward ERP /internal/agent/v1/. No SQLite. No cookies."""
from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from andes_agent.adapters.timeout import TimeoutConfig


class AdapterError(Exception):
    def __init__(self, code: str, message: str, status: int = 503, payload: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.payload = payload or {}


class ERPAdapter:
    """M2M caller for ERP internal read APIs."""

    INTERNAL_PREFIX = "/internal/agent/v1/"

    def __init__(
        self,
        base_url: str = "",
        service_token: str = "",
        environment: str = "local",
        timeout: TimeoutConfig | None = None,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.service_token = (service_token or "").strip()
        self.environment = environment
        self.timeout = timeout or TimeoutConfig()

    def call(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        actor_user: str = "",
        method: str = "POST",
    ) -> dict[str, Any]:
        if not self.base_url:
            raise AdapterError("erp_unavailable", "ERP base URL is not configured", status=503)
        if not self.service_token:
            raise AdapterError("unauthorized", "Gateway service token is not configured", status=401)
        if not (actor_user or "").strip():
            raise AdapterError("principal_required", "Human principal is required", status=400)

        url = f"{self.base_url}{self.INTERNAL_PREFIX}{path.lstrip('/')}"
        headers = {
            "Authorization": f"Bearer {self.service_token}",
            "Accept": "application/json",
            "X-Andes-Env": self.environment,
            "X-Andes-Actor": actor_user.strip(),
        }
        verb = (method or "POST").upper()
        if verb == "GET":
            request = Request(url, headers=headers, method="GET")
        else:
            body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
            request = Request(url, data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=self.timeout.total_seconds) as resp:
                raw = resp.read().decode("utf-8")
                data = json.loads(raw) if raw else {}
                if not isinstance(data, dict):
                    raise AdapterError("erp_error", "ERP returned an invalid payload", status=502)
                return data
        except AdapterError:
            raise
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                data = {}
            if not isinstance(data, dict):
                data = {}
            nested = data.get("error") if isinstance(data.get("error"), dict) else {}
            code = str(data.get("error_code") or nested.get("code") or "erp_error")
            message = str(data.get("message") or nested.get("message") or "ERP request failed")
            raise AdapterError(code, message, status=int(exc.code), payload=data) from exc
        except TimeoutError as exc:
            raise AdapterError("erp_timeout", "ERP request timed out", status=504) from exc
        except URLError as exc:
            raise AdapterError("erp_unavailable", "ERP is not reachable", status=503) from exc
        except json.JSONDecodeError as exc:
            raise AdapterError("erp_error", "ERP returned invalid JSON", status=502) from exc
