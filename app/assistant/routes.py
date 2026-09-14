"""Human BFF for the Assistant UI. The browser never sees the M2M token."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
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


def _agent_url() -> str:
    return (os.environ.get("ANDES_AGENT_URL") or "http://127.0.0.1:5055").rstrip("/")


def _service_token() -> str:
    return (os.environ.get("ANDES_AGENT_SERVICE_TOKEN") or "").strip()


def _environment() -> str:
    return (os.environ.get("ANDES_ENV") or "local").strip().lower()


def invoke_gateway(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    token = _service_token()
    if not token:
        return 503, {"ok": False, "error_code": "agent_misconfigured", "message": "El agente no está configurado."}

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{_agent_url()}/v1/invoke",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "X-Andes-Env": _environment(),
        },
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
        body = {"ok": False, "error_code": error_code, "message": message}
        if nested:
            body["error"] = {"code": str(nested.get("code") or error_code), "message": str(nested.get("message") or message)}
        elif error_code:
            body["error"] = {"code": str(error_code), "message": str(message)}
        return int(exc.code), body
    except urllib.error.URLError:
        return 503, {"ok": False, "error_code": "agent_unavailable", "message": "El Agent Gateway no está disponible."}
    except TimeoutError:
        return 504, {"ok": False, "error_code": "agent_timeout", "message": "El agente tardó demasiado."}


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

    result = run_orchestrator_chat(
        message=payload.get("message"),
        actor_user=username,
        conversation_id=str(payload.get("conversation_id") or "")[:80],
        invoke_fn=invoke_gateway,
    )
    status = int(result.pop("http_status", 200) or 200)
    return jsonify(result), status
