"""HTTP server for the Agent Gateway. No ERP, SQLite or LLM imports."""
from __future__ import annotations

import json
import time
from http import HTTPStatus
from typing import Any, Callable
from wsgiref.simple_server import make_server

from andes_agent import SERVICE_NAME, __version__
from andes_agent.adapters.erp_http import ERPAdapter
from andes_agent.audit import AuditLogger, AuditRecord, now_iso, summarize
from andes_agent.auth import AuthError, authenticate
from andes_agent.config import ConfigError, Settings, load_settings, parse_environment
from andes_agent.policy import PolicyError, authorize_tool
from andes_agent.rate_limit import InMemoryRateLimiter, RateLimitError
from andes_agent.redaction import redact
from andes_agent.schemas import SchemaError, validate_tool_arguments
from andes_agent.tools.registry import list_tools


JsonDict = dict[str, Any]

TOOL_ERROR_STATUS = {
    "not_implemented": HTTPStatus.NOT_IMPLEMENTED,
    "erp_unavailable": HTTPStatus.SERVICE_UNAVAILABLE,
    "erp_timeout": HTTPStatus.GATEWAY_TIMEOUT,
    "erp_error": HTTPStatus.BAD_GATEWAY,
    "permission_denied": HTTPStatus.FORBIDDEN,
    "principal_invalid": HTTPStatus.FORBIDDEN,
    "principal_required": HTTPStatus.BAD_REQUEST,
    "unauthorized": HTTPStatus.UNAUTHORIZED,
    "invalid_args": HTTPStatus.BAD_REQUEST,
    "invalid_environment": HTTPStatus.BAD_REQUEST,
    "tool_not_allowed": HTTPStatus.FORBIDDEN,
    "not_found": HTTPStatus.NOT_FOUND,
}


class GatewayApp:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.audit = AuditLogger(settings.audit_path)
        self.limiter: InMemoryRateLimiter = InMemoryRateLimiter(
            settings.rate_limit_max,
            settings.rate_limit_window_seconds,
        )
        self.adapter = ERPAdapter(
            base_url=settings.erp_base_url,
            service_token=settings.service_token,
            environment=settings.environment,
        )

    def __call__(self, environ: dict[str, Any], start_response: Callable) -> list[bytes]:
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO") or "/"
        try:
            status, body = self.dispatch(method, path, environ)
        except Exception:
            status, body = HTTPStatus.INTERNAL_SERVER_ERROR, {
                "ok": False,
                "error_code": "internal_error",
                "message": "Unexpected gateway error",
            }
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(raw))),
            ("Cache-Control", "no-store"),
        ]
        start_response(f"{status.value} {status.phrase}", headers)
        return [raw]

    def dispatch(self, method: str, path: str, environ: dict[str, Any]) -> tuple[HTTPStatus, JsonDict]:
        if path == "/health" and method == "GET":
            return HTTPStatus.OK, {
                "ok": True,
                "service": SERVICE_NAME,
                "version": __version__,
                "environment": self.settings.environment,
            }
        if path == "/v1/invoke" and method == "POST":
            return self._invoke(environ)
        return HTTPStatus.NOT_FOUND, {"ok": False, "error_code": "not_found", "message": "Not found"}

    def _headers(self, environ: dict[str, Any]) -> dict[str, str]:
        headers: dict[str, str] = {}
        for key, value in environ.items():
            if key.startswith("HTTP_"):
                name = key[5:].replace("_", "-")
                headers[name] = str(value)
        auth = environ.get("HTTP_AUTHORIZATION")
        if auth:
            headers["Authorization"] = str(auth)
        return headers

    def _read_json(self, environ: dict[str, Any]) -> JsonDict:
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        raw = environ["wsgi.input"].read(length) if length else b""
        if not raw:
            raise SchemaError("invalid_args", "JSON body is required")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SchemaError("invalid_args", "Body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise SchemaError("invalid_args", "Body must be a JSON object")
        return payload

    def _invoke(self, environ: dict[str, Any]) -> tuple[HTTPStatus, JsonDict]:
        started = time.perf_counter()
        headers = self._headers(environ)
        body: JsonDict = {}
        try:
            authenticate(headers, self.settings.service_token)
            env_header = headers.get("X-ANDES-ENV") or headers.get("X-Andes-Env") or ""
            if env_header:
                parsed = parse_environment(env_header)
                if parsed != self.settings.environment:
                    raise ConfigError("invalid_environment", "Environment mismatch")
            if not self.limiter.allow("invoke"):
                raise RateLimitError()
            body = self._read_json(environ)
            tool_name = str(body.get("tool") or "").strip()
            spec = authorize_tool(tool_name)
            arguments = validate_tool_arguments(spec.name, body.get("arguments"))
            context = {
                "adapter": self.adapter,
                "actor_user": str(body.get("actor_user") or ""),
                "environment": self.settings.environment,
                "agent_id": str(body.get("agent_id") or ""),
                "conversation_id": str(body.get("conversation_id") or ""),
            }
            result = spec.handler(arguments, context)
            duration_ms = int((time.perf_counter() - started) * 1000)
            success = bool(result.get("ok"))
            error_code = None if success else str(result.get("error_code") or "error")
            self.audit.write(
                AuditRecord(
                    agent_id=str(body.get("agent_id") or ""),
                    conversation_id=str(body.get("conversation_id") or ""),
                    actor_user=str(body.get("actor_user") or ""),
                    tool_name=spec.name,
                    arguments_redacted=redact(arguments),
                    timestamp=now_iso(),
                    duration_ms=duration_ms,
                    success=success,
                    error_code=error_code,
                    environment=self.settings.environment,
                    result_summary=summarize(result),
                )
            )
            if success:
                return HTTPStatus.OK, result
            status = TOOL_ERROR_STATUS.get(error_code or "", HTTPStatus.BAD_REQUEST)
            failure = {
                "ok": False,
                "error_code": error_code,
                "message": result.get("message") or error_code,
                "tool": spec.name,
                "write": spec.write,
            }
            nested = result.get("error")
            if isinstance(nested, dict):
                failure["error"] = {
                    "code": str(nested.get("code") or error_code),
                    "message": str(nested.get("message") or result.get("message") or error_code),
                }
            return status, failure
        except (AuthError, PolicyError, SchemaError, RateLimitError, ConfigError) as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            code = getattr(exc, "code", "error")
            status_code = getattr(exc, "status", 400)
            arguments = body.get("arguments") if isinstance(body.get("arguments"), dict) else {}
            self.audit.write(
                AuditRecord(
                    agent_id=str(body.get("agent_id") or ""),
                    conversation_id=str(body.get("conversation_id") or ""),
                    actor_user=str(body.get("actor_user") or ""),
                    tool_name=str(body.get("tool") or ""),
                    arguments_redacted=redact(arguments),
                    timestamp=now_iso(),
                    duration_ms=duration_ms,
                    success=False,
                    error_code=code,
                    environment=self.settings.environment,
                    result_summary=summarize(getattr(exc, "message", str(exc))),
                )
            )
            status = HTTPStatus(status_code)
            return status, {"ok": False, "error_code": code, "message": getattr(exc, "message", str(exc))}


def create_app(settings: Settings | None = None) -> GatewayApp:
    return GatewayApp(settings or load_settings())


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"andes-agent failed to start: {exc.message}", flush=True)
        return 1
    app = create_app(settings)
    tools = ", ".join(spec.name for spec in list_tools())
    print(
        f"{SERVICE_NAME} {__version__} env={settings.environment} "
        f"listening on http://{settings.host}:{settings.port} tools=[{tools}]",
        flush=True,
    )
    with make_server(settings.host, settings.port, app) as httpd:
        httpd.serve_forever()
    return 0
