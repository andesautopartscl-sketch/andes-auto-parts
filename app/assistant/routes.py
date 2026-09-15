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


@assistant_bp.route("/api/metrics/summary", methods=["GET"])
@login_required
def api_metrics_summary():
    """Local-only aggregate dashboard — no messages, prompts, or secrets."""
    if _environment() not in {"local", "staging"}:
        return jsonify(
            ok=False,
            error_code="not_available",
            message="Resumen de métricas solo disponible en local/staging.",
        ), 404

    username = (session.get("user") or "").strip()
    if not username:
        return jsonify(ok=False, error_code="unauthorized", message="Debe iniciar sesión."), 401

    from app.assistant.orchestrator.metrics import get_default_metrics_store

    summary = get_default_metrics_store().summary()
    return jsonify(summary)


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


@assistant_bp.route("/api/conversations", methods=["GET"])
@login_required
def api_conversations_list():
    """List conversations for the session user (FASE 7A)."""
    from app.assistant.orchestrator.history_config import history_enabled
    from app.assistant.orchestrator.history_store import get_default_history_store

    username = (session.get("user") or "").strip()
    if not username:
        return jsonify(ok=False, error_code="unauthorized", message="Debe iniciar sesión."), 401
    if not history_enabled():
        return jsonify(ok=False, error_code="history_disabled", message="Historial deshabilitado."), 404

    limit = request.args.get("limit", 20)
    try:
        limit_i = int(limit)
    except (TypeError, ValueError):
        limit_i = 20
    cursor = request.args.get("cursor")
    data = get_default_history_store().list_conversations(username, limit=limit_i, cursor=cursor)
    return jsonify(ok=True, **data)


@assistant_bp.route("/api/conversations", methods=["POST"])
@login_required
def api_conversations_create():
    """Ensure/create a server-owned conversation id."""
    from app.assistant.orchestrator.history_config import history_enabled
    from app.assistant.orchestrator.history_store import get_default_history_store

    username = (session.get("user") or "").strip()
    if not username:
        return jsonify(ok=False, error_code="unauthorized", message="Debe iniciar sesión."), 401
    if not history_enabled():
        return jsonify(ok=False, error_code="history_disabled", message="Historial deshabilitado."), 404

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify(ok=False, error_code="invalid_args", message="JSON inválido."), 400
    client_id = str(payload.get("client_conversation_id") or payload.get("conversation_id") or "")[:80]
    conv = get_default_history_store().ensure_conversation(
        username,
        client_conversation_id=client_id or None,
        title=str(payload.get("title") or "")[:120] or None,
    )
    if not conv:
        return jsonify(ok=False, error_code="history_unavailable", message="No se pudo crear la conversación."), 503
    return jsonify(ok=True, conversation=conv)


@assistant_bp.route("/api/conversations/<conversation_id>", methods=["GET"])
@login_required
def api_conversation_get(conversation_id: str):
    from app.assistant.orchestrator.history_config import history_enabled
    from app.assistant.orchestrator.history_store import get_default_history_store

    username = (session.get("user") or "").strip()
    if not username:
        return jsonify(ok=False, error_code="unauthorized", message="Debe iniciar sesión."), 401
    if not history_enabled():
        return jsonify(ok=False, error_code="history_disabled", message="Historial deshabilitado."), 404

    store = get_default_history_store()
    conv = store.get_conversation(username, str(conversation_id or "")[:80])
    if not conv:
        return jsonify(ok=False, error_code="not_found", message="Conversación no encontrada."), 404
    turns = store.list_turns(username, conv["id"], limit=min(int(request.args.get("limit") or 20), 50))
    return jsonify(ok=True, conversation=conv, turns=turns.get("items") or [], next_before_seq=turns.get("next_before_seq"))


@assistant_bp.route("/api/conversations/<conversation_id>/turns", methods=["GET"])
@login_required
def api_conversation_turns(conversation_id: str):
    from app.assistant.orchestrator.history_config import history_enabled
    from app.assistant.orchestrator.history_store import get_default_history_store

    username = (session.get("user") or "").strip()
    if not username:
        return jsonify(ok=False, error_code="unauthorized", message="Debe iniciar sesión."), 401
    if not history_enabled():
        return jsonify(ok=False, error_code="history_disabled", message="Historial deshabilitado."), 404

    before = request.args.get("before_seq")
    after = request.args.get("after_seq")
    try:
        before_i = int(before) if before is not None else None
    except ValueError:
        before_i = None
    try:
        after_i = int(after) if after is not None else None
    except ValueError:
        after_i = None
    try:
        limit_i = int(request.args.get("limit") or 20)
    except ValueError:
        limit_i = 20

    data = get_default_history_store().list_turns(
        username,
        str(conversation_id or "")[:80],
        limit=limit_i,
        before_seq=before_i,
        after_seq=after_i,
    )
    if data.get("error_code") == "not_found":
        return jsonify(ok=False, error_code="not_found", message="Conversación no encontrada."), 404
    return jsonify(ok=True, **{k: v for k, v in data.items() if k != "error_code"})


@assistant_bp.route("/api/conversations/<conversation_id>", methods=["DELETE"])
@login_required
def api_conversation_delete(conversation_id: str):
    from app.assistant.orchestrator.history_config import history_enabled
    from app.assistant.orchestrator.history_store import get_default_history_store
    from app.assistant.orchestrator.turn_store import get_default_turn_store

    username = (session.get("user") or "").strip()
    if not username:
        return jsonify(ok=False, error_code="unauthorized", message="Debe iniciar sesión."), 401
    if not history_enabled():
        return jsonify(ok=False, error_code="history_disabled", message="Historial deshabilitado."), 404

    cid = str(conversation_id or "")[:80]
    ok = get_default_history_store().soft_delete_conversation(username, cid)
    if not ok:
        return jsonify(ok=False, error_code="not_found", message="Conversación no encontrada."), 404
    get_default_turn_store().clear(username, cid)
    return jsonify(ok=True, deleted=True, conversation_id=cid)


# ---------------------------------------------------------------------------
# FASE 7B.1 / 7B.3 — memory administration + explicit write
# ---------------------------------------------------------------------------


@assistant_bp.route("/api/memory", methods=["GET"])
@login_required
def api_memory_list():
    """List memory slots for the session user. MEMORY=0 → 404 memory_disabled."""
    from app.assistant.orchestrator.memory_config import memory_enabled
    from app.assistant.orchestrator.memory_store import get_default_memory_store

    username = (session.get("user") or "").strip()
    if not username:
        return jsonify(ok=False, error_code="unauthorized", message="Debe iniciar sesión."), 401
    if not memory_enabled():
        return jsonify(ok=False, error_code="memory_disabled", message="Memoria deshabilitada."), 404

    scope = request.args.get("scope")
    conversation_id = request.args.get("conversation_id")
    try:
        limit_i = int(request.args.get("limit") or 100)
    except (TypeError, ValueError):
        limit_i = 100
    items = get_default_memory_store().list_slots(
        username,
        scope=(scope.strip().lower() if scope else None),
        conversation_id=(str(conversation_id)[:80] if conversation_id else None),
        limit=limit_i,
    )
    return jsonify(ok=True, items=items, count=len(items))


@assistant_bp.route("/api/memory", methods=["POST"])
@login_required
def api_memory_create():
    """FASE 7B.3 — structured explicit memory create/upsert (closed schema)."""
    from app.assistant.orchestrator.memory_config import memory_enabled
    from app.assistant.orchestrator.memory_explicit import write_explicit_memory

    username = (session.get("user") or "").strip()
    if not username:
        return jsonify(ok=False, error_code="unauthorized", message="Debe iniciar sesión."), 401
    if not memory_enabled():
        return jsonify(ok=False, error_code="memory_disabled", message="Memoria deshabilitada."), 404

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify(ok=False, error_code="invalid_json", message="JSON inválido."), 400
    # Never accept client actor_user / epoch / sensitivity as authority
    for forbidden in ("actor_user", "Authorization", "token", "permission_epoch", "sensitivity"):
        if forbidden in payload:
            return jsonify(ok=False, error_code="forbidden_field", message="Campo no permitido."), 400

    result = write_explicit_memory(
        actor_user=username,
        memory_type=str(payload.get("memory_type") or ""),
        key=str(payload.get("key") or ""),
        value=payload.get("value") if isinstance(payload.get("value"), dict) else {},
        scope=str(payload.get("scope") or "user"),
        conversation_id=(
            str(payload.get("conversation_id"))[:80]
            if payload.get("conversation_id")
            else None
        ),
        require_explicit_flag=False,
    )
    if not result.ok:
        status = 404 if result.error_code == "memory_disabled" else 400
        if result.error_code == "not_found":
            status = 404
        return (
            jsonify(
                ok=False,
                error_code=result.error_code,
                message=result.message,
                memory_type=result.memory_type,
                scope=result.scope,
            ),
            status,
        )
    slot = result.slot or {}
    public = {
        "id": slot.get("id"),
        "type": slot.get("memory_type"),
        "key": slot.get("key"),
        "value": slot.get("value"),
        "scope": slot.get("scope"),
        "conversation_id": slot.get("conversation_id"),
    }
    return jsonify(ok=True, item=public, confirmation=result.confirmation)


@assistant_bp.route("/api/memory/<slot_id>", methods=["PUT"])
@login_required
def api_memory_update(slot_id: str):
    """FASE 7B.3 — update existing owned memory value (closed schema)."""
    from app.assistant.orchestrator.memory_config import memory_enabled
    from app.assistant.orchestrator.memory_explicit import write_explicit_memory
    from app.assistant.orchestrator.memory_store import get_default_memory_store

    username = (session.get("user") or "").strip()
    if not username:
        return jsonify(ok=False, error_code="unauthorized", message="Debe iniciar sesión."), 401
    if not memory_enabled():
        return jsonify(ok=False, error_code="memory_disabled", message="Memoria deshabilitada."), 404

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify(ok=False, error_code="invalid_json", message="JSON inválido."), 400
    for forbidden in ("actor_user", "Authorization", "token", "permission_epoch", "sensitivity"):
        if forbidden in payload:
            return jsonify(ok=False, error_code="forbidden_field", message="Campo no permitido."), 400

    existing = get_default_memory_store().get_slot(username, str(slot_id or "")[:80])
    if not existing:
        return jsonify(ok=False, error_code="not_found", message="Memoria no encontrada."), 404

    result = write_explicit_memory(
        actor_user=username,
        memory_type=str(payload.get("memory_type") or existing.get("memory_type") or ""),
        key=str(payload.get("key") or existing.get("key") or ""),
        value=(
            payload.get("value")
            if isinstance(payload.get("value"), dict)
            else (existing.get("value") if isinstance(existing.get("value"), dict) else {})
        ),
        scope=str(existing.get("scope") or "user"),
        conversation_id=existing.get("conversation_id"),
        slot_id=str(slot_id or "")[:80],
        require_explicit_flag=False,
    )
    if not result.ok:
        status = 404 if result.error_code in {"memory_disabled", "not_found"} else 400
        return (
            jsonify(ok=False, error_code=result.error_code, message=result.message),
            status,
        )
    slot = result.slot or {}
    public = {
        "id": slot.get("id"),
        "type": slot.get("memory_type"),
        "key": slot.get("key"),
        "value": slot.get("value"),
        "scope": slot.get("scope"),
        "conversation_id": slot.get("conversation_id"),
    }
    return jsonify(ok=True, item=public, confirmation=result.confirmation)


@assistant_bp.route("/api/memory/conversation/<conversation_id>", methods=["DELETE"])
@login_required
def api_memory_delete_conversation(conversation_id: str):
    """Soft-delete conversation-scoped slots for one conversation (ownership by actor)."""
    from app.assistant.orchestrator.memory_config import memory_enabled
    from app.assistant.orchestrator.memory_store import get_default_memory_store

    username = (session.get("user") or "").strip()
    if not username:
        return jsonify(ok=False, error_code="unauthorized", message="Debe iniciar sesión."), 401
    if not memory_enabled():
        return jsonify(ok=False, error_code="memory_disabled", message="Memoria deshabilitada."), 404

    cid = str(conversation_id or "")[:80]
    n = get_default_memory_store().soft_delete_conversation(username, cid)
    return jsonify(ok=True, deleted_count=n, conversation_id=cid)


@assistant_bp.route("/api/memory/<slot_id>", methods=["DELETE"])
@login_required
def api_memory_delete_one(slot_id: str):
    from app.assistant.orchestrator.memory_config import memory_enabled
    from app.assistant.orchestrator.memory_store import get_default_memory_store

    username = (session.get("user") or "").strip()
    if not username:
        return jsonify(ok=False, error_code="unauthorized", message="Debe iniciar sesión."), 401
    if not memory_enabled():
        return jsonify(ok=False, error_code="memory_disabled", message="Memoria deshabilitada."), 404

    ok = get_default_memory_store().soft_delete(username, str(slot_id or "")[:80])
    if not ok:
        return jsonify(ok=False, error_code="not_found", message="Memoria no encontrada."), 404
    return jsonify(ok=True, deleted=True, id=str(slot_id or "")[:80])


@assistant_bp.route("/api/memory", methods=["DELETE"])
@login_required
def api_memory_delete_all():
    """Soft-delete all memory slots for the session user."""
    from app.assistant.orchestrator.memory_config import memory_enabled
    from app.assistant.orchestrator.memory_store import get_default_memory_store

    username = (session.get("user") or "").strip()
    if not username:
        return jsonify(ok=False, error_code="unauthorized", message="Debe iniciar sesión."), 401
    if not memory_enabled():
        return jsonify(ok=False, error_code="memory_disabled", message="Memoria deshabilitada."), 404

    n = get_default_memory_store().soft_delete_all(username)
    return jsonify(ok=True, deleted_count=n)
