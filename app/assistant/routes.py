"""Human BFF for the Assistant UI. The browser never sees the M2M token."""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
import urllib.error
import urllib.request
from collections import defaultdict, deque
from typing import Any

from flask import Blueprint, jsonify, request, session

from app.utils.decorators import login_required

assistant_bp = Blueprint("assistant", __name__, url_prefix="/assistant")

ALLOWED_BROWSER_TOOLS = frozenset(
    {
        "search_catalog",
        "get_product",
        "get_inventory",
        "check_stock",
        "get_stock_movements",
        "get_ingresos",
        "get_purchase_orders",
        "get_customer",
        "get_supplier",
        "get_dashboard_kpis",
    }
)
GATEWAY_TIMEOUT_SECONDS = 8.0

_CHAT_HITS: dict[str, deque[float]] = defaultdict(deque)
_CHAT_LOCK = threading.Lock()
_CHAT_WINDOW_SECONDS = 60.0


def _agent_url() -> str:
    return (os.environ.get("ANDES_AGENT_URL") or "http://127.0.0.1:5055").rstrip("/")


def _service_token() -> str:
    return (os.environ.get("ANDES_AGENT_SERVICE_TOKEN") or "").strip()


def _environment() -> str:
    return (os.environ.get("ANDES_ENV") or "local").strip().lower()


def _chat_rate_limit_max() -> int:
    raw = (os.environ.get("ANDES_ASSISTANT_CHAT_RATE_LIMIT") or "10").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 10


def _chat_rate_limited(username: str) -> bool:
    """Return True when the user exceeded the per-minute chat budget.

    Limit 0 disables the check (tests / ops). In-memory per process only.
    """
    limit = _chat_rate_limit_max()
    if limit <= 0:
        return False
    key = (username or "anon").strip().lower() or "anon"
    now = time.monotonic()
    with _CHAT_LOCK:
        hits = _CHAT_HITS[key]
        while hits and (now - hits[0]) > _CHAT_WINDOW_SECONDS:
            hits.popleft()
        if len(hits) >= limit:
            return True
        hits.append(now)
        return False


def invoke_gateway(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    token = _service_token()
    if not token:
        return 503, {"ok": False, "error_code": "agent_misconfigured", "message": "El agente no está configurado."}

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
        "X-Andes-Env": _environment(),
    }
    correlation_id = str(payload.get("correlation_id") or "").strip()
    if correlation_id:
        headers["X-Andes-Correlation-Id"] = correlation_id[:80]

    req = urllib.request.Request(
        f"{_agent_url()}/v1/invoke",
        data=body,
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=GATEWAY_TIMEOUT_SECONDS) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw) if raw else {}
            if not isinstance(data, dict):
                data = {"ok": False, "error_code": "agent_error", "message": "Respuesta inválida del agente."}
            return int(getattr(resp, "status", 200) or 200), data
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        nested = data.get("error") if isinstance(data.get("error"), dict) else {}
        error_code = data.get("error_code") or nested.get("code") or "agent_error"
        message = data.get("message") or nested.get("message") or "El agente rechazó la solicitud."
        body_out = {"ok": False, "error_code": error_code, "message": message}
        if nested:
            body_out["error"] = {
                "code": str(nested.get("code") or error_code),
                "message": str(nested.get("message") or message),
            }
        elif error_code:
            body_out["error"] = {"code": str(error_code), "message": str(message)}
        return int(exc.code), body_out
    except urllib.error.URLError:
        return 503, {"ok": False, "error_code": "agent_unavailable", "message": "El Agent Gateway no está disponible."}
    except TimeoutError:
        return 504, {"ok": False, "error_code": "agent_timeout", "message": "El agente tardó demasiado."}


@assistant_bp.route("/api/capabilities", methods=["GET"])
@login_required
def api_capabilities():
    """Safe capability snapshot for the widget — never returns secrets."""
    from app.assistant.orchestrator.llm.config import load_llm_settings

    username = (session.get("user") or "").strip()
    if not username:
        return jsonify(ok=False, error_code="unauthorized", message="Debe iniciar sesión."), 401

    caps = load_llm_settings().public_capabilities()
    return jsonify(ok=True, **caps)


@assistant_bp.route("/api/invoke", methods=["POST"])
@login_required
def api_invoke():
    username = (session.get("user") or "").strip()
    if not username:
        return jsonify(ok=False, error_code="unauthorized", message="Debe iniciar sesión."), 401

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(ok=False, error_code="invalid_args", message="JSON inválido."), 400

    tool = str(payload.get("tool") or "").strip()
    if tool not in ALLOWED_BROWSER_TOOLS:
        return jsonify(ok=False, error_code="tool_not_available", message="Esta función todavía no está disponible."), 400

    arguments = payload.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return jsonify(ok=False, error_code="invalid_args", message="arguments debe ser un objeto."), 400

    status, body = invoke_gateway(
        {
            "agent_id": "andes-assistant",
            "conversation_id": str(payload.get("conversation_id") or "")[:80],
            "actor_user": username,
            "tool": tool,
            "arguments": arguments,
        }
    )
    return jsonify(body), status


@assistant_bp.route("/api/chat", methods=["POST"])
@login_required
def api_chat():
    """Natural-language orchestrator entry (FakePlanner by default; LLM only if soft-enabled).

    The browser must NOT send a tool name. Actor always comes from the session.
    """
    from app.assistant.orchestrator import run_orchestrator_chat

    username = (session.get("user") or "").strip()
    if not username:
        return jsonify(ok=False, error_code="unauthorized", message="Debe iniciar sesión."), 401

    if _chat_rate_limited(username):
        return jsonify(
            ok=False,
            error_code="rate_limited",
            message="Demasiadas consultas al asistente. Espera un momento e inténtalo de nuevo.",
        ), 429

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(ok=False, error_code="invalid_args", message="JSON inválido."), 400

    # Reject any client-supplied tool / arguments / actor override
    for forbidden in ("tool", "tools", "arguments", "actor_user", "Authorization", "token"):
        if forbidden in payload:
            return jsonify(
                ok=False,
                error_code="invalid_args",
                message=f"Campo '{forbidden}' no está permitido en /assistant/api/chat.",
            ), 400

    correlation_id = str(payload.get("correlation_id") or "").strip() or str(uuid.uuid4())

    def _invoke_with_correlation(gw_payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        body = dict(gw_payload)
        body.setdefault("correlation_id", correlation_id)
        return invoke_gateway(body)

    result = run_orchestrator_chat(
        message=payload.get("message"),
        actor_user=username,
        conversation_id=str(payload.get("conversation_id") or "")[:80],
        invoke_fn=_invoke_with_correlation,
        correlation_id=correlation_id,
    )
    status = int(result.pop("http_status", 200) or 200)
    result.setdefault("correlation_id", correlation_id)
    return jsonify(result), status
