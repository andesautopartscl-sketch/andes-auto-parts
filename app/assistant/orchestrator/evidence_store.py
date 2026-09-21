"""FASE 8.1 — turn-scoped evidence store (RAM). Not memory. Not history."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.assistant.orchestrator.agent_config import (
    MAX_EVIDENCE_PROMPT_CHARS,
    MAX_TOOL_RESULT_CHARS,
)
from app.assistant.orchestrator.normalizer import _strip_pii

_FORBIDDEN_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "cookies",
        "set-cookie",
        "token",
        "password",
        "api_key",
        "apikey",
        "secret",
        "access_token",
        "bearer",
        "headers",
        "header",
        "raw",
        "http",
        "m2m",
    }
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def canonical_call_key(tool: str, arguments: dict[str, Any] | None) -> str:
    payload = json.dumps(
        {"tool": str(tool or ""), "arguments": arguments or {}},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def canonical_content_key(
    tool: str,
    *,
    ok: bool,
    empty: bool,
    data_view: Any,
    meta_view: Any,
    raw_size: int | None = None,
) -> str:
    """Fingerprint of WHAT a tool returned. Two calls with the same fingerprint
    carry equivalent evidence, whatever arguments produced them.

    Used only by the turn-scoped progress ledger to tell "new evidence" from
    "the same evidence again". Never persisted, never a cache key.
    """
    payload = json.dumps(
        {
            "tool": str(tool or ""),
            "ok": bool(ok),
            "empty": bool(empty),
            "data": data_view,
            "meta": meta_view,
            # El fingerprint se calcula sobre la vista YA truncada, asi que dos
            # payloads distintos con el mismo prefijo colisionaban y la segunda
            # llamada se contaba como "evidencia equivalente". El tamano real
            # separa esos casos sin volver a mirar el contenido cortado.
            "raw_size": int(raw_size) if raw_size is not None else None,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _strip_forbidden(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, inner in value.items():
            lk = str(key).strip().lower()
            if lk in _FORBIDDEN_KEYS or any(
                p in lk for p in ("authorization", "cookie", "token", "password", "secret", "api_key")
            ):
                continue
            out[key] = _strip_forbidden(inner)
        return out
    if isinstance(value, list):
        return [_strip_forbidden(v) for v in value]
    return value


def _raw_size(value: Any) -> int:
    """Tamano del payload ANTES de truncar. Solo se usa para el fingerprint."""
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return 0


def _list_paths(node: Any, prefix: str = "") -> list[tuple[str, list[Any]]]:
    """Every list reachable in the structure, deepest-last, with its dotted path."""
    out: list[tuple[str, list[Any]]] = []
    if isinstance(node, dict):
        for key, inner in node.items():
            out.extend(_list_paths(inner, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(node, list):
        out.append((prefix, node))
        for index, inner in enumerate(node):
            out.extend(_list_paths(inner, f"{prefix}.{index}"))
    return out


def _truncate_view(value: Any) -> tuple[Any, bool, dict[str, int]]:
    """Degrade a payload WITHOUT destroying its shape.

    The previous version replaced an oversized payload with
    ``{"truncated": True, "preview": "<raw json text>"}``. That broke three
    things at once:

    - every path died, so any calculation over that evidence was unresolvable;
    - the preview is a STRING, so the number tokenizer mined it and turned
      serialized text — including fragments cut mid-number — into citable
      figures that were never values;
    - the values that did not fit stopped being citable at all.

    Shrinking the collections instead keeps objects addressable, keeps real
    values as real values, and records how many rows were dropped so the caller
    can refuse to state a cardinality it can no longer prove.
    """
    cleaned = _strip_pii(_strip_forbidden(value if isinstance(value, (dict, list)) else {}))

    def _size(node: Any) -> int:
        return len(json.dumps(node, ensure_ascii=False, default=str))

    if _size(cleaned) <= MAX_TOOL_RESULT_CHARS:
        return cleaned, False, {}

    omitted: dict[str, int] = {}
    # Shrink the biggest collection first and repeat: one huge list should not
    # cost every other field its rows.
    for _ in range(64):
        if _size(cleaned) <= MAX_TOOL_RESULT_CHARS:
            break
        lists = [(path, node) for path, node in _list_paths(cleaned) if len(node) > 1]
        if not lists:
            break
        path, node = max(lists, key=lambda pair: _size(pair[1]))
        keep = max(1, len(node) // 2)
        omitted[path] = omitted.get(path, 0) + (len(node) - keep)
        del node[keep:]
    if _size(cleaned) <= MAX_TOOL_RESULT_CHARS:
        return cleaned, bool(omitted), omitted
    # Shape preserved but still oversized (huge scalars, not rows): keep the keys
    # and drop the values rather than emitting text that would be mined.
    if isinstance(cleaned, dict):
        return ({"__oversized__": True, "fields": sorted(str(k) for k in cleaned)},
                True, omitted)
    return {"__oversized__": True}, True, omitted


@dataclass
class EvidenceItem:
    evidence_id: str
    tool: str
    arguments: dict[str, Any]
    ok: bool
    empty: bool
    error_code: str | None
    classification: str
    finance_redacted: bool
    stock_omitted: bool
    data_view: Any
    meta_view: Any
    ts: str
    correlation_id: str
    latency_ms: int
    truncated: bool
    call_key: str
    content_key: str = ""
    # Filas descartadas por path al degradar. Una coleccion aqui ya no puede
    # sostener una afirmacion de cardinalidad: su longitud no es la real.
    omitted_rows: dict[str, int] = field(default_factory=dict)


@dataclass
class EvidenceStore:
    """Temporary per-turn store. Never upserted to assistant_memory_slot."""

    correlation_id: str = ""
    items: list[EvidenceItem] = field(default_factory=list)
    _seq: int = 0

    def add_from_tool_result(
        self,
        *,
        tool: str,
        arguments: dict[str, Any] | None,
        result: dict[str, Any],
        correlation_id: str = "",
    ) -> EvidenceItem:
        self._seq += 1
        eid = f"e{self._seq}"
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
        data_view, trunc_d, omitted_d = _truncate_view(data)
        meta_view, trunc_m, omitted_m = _truncate_view(meta)
        omitted = {f"data.{k}" if k else "data": v for k, v in omitted_d.items()}
        omitted.update({f"meta.{k}" if k else "meta": v for k, v in omitted_m.items()})
        args = dict(arguments or {})
        item = EvidenceItem(
            evidence_id=eid,
            tool=str(tool or result.get("tool") or ""),
            arguments=args,
            ok=bool(result.get("ok")),
            empty=bool(result.get("empty")),
            error_code=(str(result.get("error_code")) if result.get("error_code") else None),
            classification=str(result.get("classification") or "INTERNAL"),
            finance_redacted=bool(result.get("finance_redacted")),
            stock_omitted=bool(result.get("stock_omitted")),
            data_view=data_view,
            meta_view=meta_view,
            ts=_utc_now().isoformat(),
            correlation_id=(correlation_id or self.correlation_id or "")[:80],
            latency_ms=int(result.get("latency_ms") or 0),
            truncated=bool(trunc_d or trunc_m),
            omitted_rows=omitted,
            call_key=canonical_call_key(str(tool or ""), args),
            content_key=canonical_content_key(
                str(tool or result.get("tool") or ""),
                ok=bool(result.get("ok")),
                empty=bool(result.get("empty")),
                data_view=data_view,
                meta_view=meta_view,
                raw_size=_raw_size(data) + _raw_size(meta),
            ),
        )
        self.items.append(item)
        return item

    def by_id(self, evidence_id: str) -> EvidenceItem | None:
        for item in self.items:
            if item.evidence_id == evidence_id:
                return item
        return None

    def size_chars(self) -> int:
        return len(self.prompt_pack())

    def _entry(self, item: EvidenceItem) -> dict[str, Any]:
        return {
            "evidence_id": item.evidence_id,
            "tool": item.tool,
            "arguments": item.arguments,
            "ok": item.ok,
            "empty": item.empty,
            "error_code": item.error_code,
            "classification": item.classification,
            "finance_redacted": item.finance_redacted,
            "stock_omitted": item.stock_omitted,
            "truncated": item.truncated,
            "omitted_rows": item.omitted_rows,
            "data": item.data_view,
            "meta": item.meta_view,
        }

    @staticmethod
    def _stub(item: EvidenceItem) -> dict[str, Any]:
        """Identidad sin payload: el evidence_id sigue siendo citable y resoluble
        por resolve_path aunque su contenido no quepa en el prompt."""
        return {
            "evidence_id": item.evidence_id,
            "tool": item.tool,
            "ok": item.ok,
            "empty": item.empty,
            "error_code": item.error_code,
            "omitted_from_prompt": True,
        }

    def safe_summary(self) -> dict[str, Any]:
        """FASE 8.2D — cuanto se degrado en este turno. Conteos, nunca contenido.

        Sin esto, la degradacion estructural de 8.2C es invisible en produccion:
        se sabia que el mecanismo funciona porque hay tests, pero no si llegaba a
        activarse con datos reales ni en que herramientas. Ningun valor, nombre de
        cliente, codigo ni fragmento de payload sale por aqui: solo el tool, si se
        trunco, y cuantas filas se omitieron por path.
        """
        return {
            "items": len(self.items),
            "truncated_items": sum(1 for i in self.items if i.truncated),
            "omitted_rows_total": sum(
                sum(int(v) for v in (i.omitted_rows or {}).values()) for i in self.items
            ),
            "degraded_tools": sorted({i.tool for i in self.items if i.truncated}),
        }

    def prompt_pack(self) -> str:
        """Pack por ITEMS COMPLETOS, siempre JSON valido.

        Antes se serializaba todo y se cortaba a MAX_EVIDENCE_PROMPT_CHARS, lo que
        partia la estructura a mitad: el modelo recibia JSON no parseable y ninguna
        senal de que faltaba nada. Con MAX_TOOL_RESULT_CHARS * MAX_TOOL_CALLS muy
        por encima del cap, eso ocurria justo en los turnos multi-tool.

        Ahora se conservan los items mas RECIENTES enteros y los que no caben se
        degradan a un stub con su identidad, para que sus evidence_id sigan siendo
        citables y el modelo vea explicitamente que se omitio contenido.
        """
        entries: list[dict[str, Any]] = []
        used = 2  # los corchetes del array
        kept = 0
        for item in reversed(self.items):
            blob = json.dumps(self._entry(item), ensure_ascii=False, default=str)
            if kept and used + len(blob) + 1 > MAX_EVIDENCE_PROMPT_CHARS:
                break
            entries.append(self._entry(item))
            used += len(blob) + 1
            kept += 1
        for item in list(reversed(self.items))[kept:]:
            entries.append(self._stub(item))
        entries.reverse()
        raw = json.dumps(entries, ensure_ascii=False, default=str)
        if len(raw) <= MAX_EVIDENCE_PROMPT_CHARS:
            return raw
        # Un solo item no cabe: degradarlo tambien, nunca devolver JSON roto.
        entries = [self._stub(item) for item in self.items]
        return json.dumps(entries, ensure_ascii=False, default=str)

    def resolve_path(self, path: str) -> Any:
        """Resolve 'e1.data.total_stock' or 'e1.data.items.0.stock'."""
        parts = [p for p in str(path or "").split(".") if p]
        if not parts:
            raise KeyError("empty path")
        item = self.by_id(parts[0])
        if item is None:
            raise KeyError(f"unknown evidence_id {parts[0]}")
        cursor: Any
        if len(parts) == 1:
            return item.data_view
        root = parts[1]
        if root == "data":
            cursor = item.data_view
        elif root == "meta":
            cursor = item.meta_view
        elif root == "arguments":
            cursor = item.arguments
        else:
            raise KeyError(f"unsupported root {root}")
        for part in parts[2:]:
            if isinstance(cursor, list):
                try:
                    cursor = cursor[int(part)]
                except (ValueError, IndexError) as exc:
                    raise KeyError(path) from exc
            elif isinstance(cursor, dict):
                if part not in cursor:
                    raise KeyError(path)
                cursor = cursor[part]
            else:
                raise KeyError(path)
        return cursor
