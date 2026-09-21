"""FASE 8.1 — deterministic answer verifier. No LLM. Never invents values."""
from __future__ import annotations

import json
import re
from typing import Any

from app.assistant.orchestrator.catalog import ALLOWED_TOOLS
from app.assistant.orchestrator.composer import _scrub_leaked_pii, reply_contains_only_evidence_values
from app.assistant.orchestrator.analysis import (
    LADDER_LABELS,
    is_assumption_path,
    ladder_sections,
    order_claims,
)
from app.assistant.orchestrator.evidence_store import EvidenceStore

_CODE_RE = re.compile(
    r"\b(?=[A-Z0-9._-]{3,64}\b)(?=[A-Z0-9._-]*[A-Z])(?=[A-Z0-9._-]*\d)[A-Z0-9._-]+\b",
    re.IGNORECASE,
)
_NUM_RE = re.compile(r"\b\d+(?:[.,]\d+)?\b")
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_PCT_RE = re.compile(r"\b\d+(?:[.,]\d+)?\s*%")
_EPS = 1e-6

# FASE 8.1H — a figure is grounded only when it appears as a whole numeric TOKEN
# in the evidence, or is the result of a verified calculation. Substring matching
# used to accept any digit that happened to occur inside a larger number or a
# date. The lookbehind stops "5" from matching inside "12345"; dates are masked
# out before scanning, so "2026-09-16" contributes no numbers of its own.
_NUM_TOKEN_RE = re.compile(r"(?<![\d.,])-?\d+(?:[.,]\d+)?(?!\d)")

# FASE 8.1H.2 — dates are grounded SEMANTICALLY, not by substring. 8.1H masked
# only the ISO form, so "31 de julio de 2026" and "31/07/2026" decayed into three
# loose numbers that no longer matched anything and were rejected, even though
# the evidence held that exact date. Every recognized form is normalized to
# YYYY-MM-DD and compared against the dates the evidence actually contains.
#
# The masking still stands on both sides: a date never contributes its day, month
# or year to the set of citable figures, so "2026-07-31" grounds neither 7, 26 nor
# 31 as independent numbers.
_MONTHS_ES = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}
_MONTH_ALT = "|".join(sorted(_MONTHS_ES, key=len, reverse=True))
_ISO_DATE_RE = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
_DMY_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})(?!\d)")
_TEXT_DATE_RE = re.compile(
    rf"(?<!\d)(\d{{1,2}})\s+de\s+({_MONTH_ALT})(?:\s+de[l]?\s+(\d{{4}}))?",
    re.IGNORECASE,
)
# A claim may name a day and month without the year ("el 31 de julio"). That is a
# partial reference: grounded when some evidence date falls on that day/month.
_PARTIAL_DATE_PREFIX = "??"


def _as_number(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        raise TypeError("not numeric")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", ".")
    return float(text)


class CollectionRequired(TypeError):
    """``count`` was pointed at something that is not a collection."""


class CollectionTruncated(ValueError):
    """``count`` was pointed at a collection whose rows were degraded.

    Its length is no longer the real one, so counting it would publish a wrong
    cardinality — exactly the kind of confident-but-false figure the verifier
    exists to stop. Refusing is the only safe answer.
    """


def _resolve_collection(store: EvidenceStore, path: str) -> list[Any]:
    """The list an evidence path points at, or refuse.

    ``count`` is the only op that reads structure instead of a number, so it is
    also the only one that can be pointed at the wrong kind of thing. A string
    and a dict both have a length; neither is a collection of records, and
    counting their characters or keys would be a fabricated figure. Only a real
    JSON array qualifies — and ``resolve_path`` already refuses anything that is
    not inside this turn's evidence, so the raw blob is unreachable by
    construction.
    """
    raw = store.resolve_path(str(path))
    if not isinstance(raw, list):
        raise CollectionRequired(
            f"count requires a collection, got {type(raw).__name__}"
        )
    head = str(path or "").split(".")[0]
    suffix = str(path or "")[len(head) + 1:]
    item = store.by_id(head)
    if item is not None and suffix in (item.omitted_rows or {}):
        raise CollectionTruncated(
            f"count over a degraded collection ({item.omitted_rows[suffix]} rows dropped)"
        )
    return raw


def recompute_calculation(
    store: EvidenceStore,
    calc: dict[str, Any],
    assumptions: dict[str, float] | None = None,
) -> float:
    """Recomputa el resultado declarado. ``assumptions`` resuelve rutas ``aN.value``.

    FASE 8.5: un parametro de planificacion —un horizonte de dos meses— no esta
    en el ERP, lo pone la pregunta. Sin poder referenciarlo aqui, ninguna
    proyeccion era recomputable y toda cifra proyectada quedaba indistinguible
    de una alucinacion. Los supuestos llegan YA validados por analysis.py: su
    valor o lo escribio el usuario o es un default declarado por la casa.
    """
    op = str(calc.get("op") or "")
    inputs = list(calc.get("inputs") or [])
    assume = dict(assumptions or {})
    if op == "count":
        # FASE 8.1I — cardinality. A payload may hold a collection of N records
        # without holding N anywhere as a value, so "hay N productos" had no way
        # of being proven and was discarded even when correct. Counting the rows
        # the evidence actually contains is deterministic and reconstructible, so
        # it grounds N the same way an arithmetic result does.
        if len(inputs) != 1:
            raise ValueError("count requires exactly 1 input")
        return float(len(_resolve_collection(store, inputs[0])))

    values: list[float] = []
    for path in inputs:
        key = str(path)
        # ``assume`` trae los supuestos declarados (a1.value) y los resultados de
        # los calculos ya verificados (c1), asi que un paso puede apoyarse en el
        # anterior sin reabrir el store.
        if key in assume:
            values.append(float(assume[key]))
            continue
        if is_assumption_path(key):
            raise KeyError(f"unknown assumption path {key}")
        values.append(_as_number(store.resolve_path(key)))
    if op == "min":
        return float(min(values))
    if op == "max":
        return float(max(values))
    if op == "sum":
        return float(sum(values))
    if op == "diff":
        return float(values[0] - values[1])
    if op == "ratio":
        if abs(values[1]) < _EPS:
            raise ZeroDivisionError("ratio denominator is 0")
        return float(values[0] / values[1])
    if op == "mul":
        if len(values) < 2:
            raise ValueError("mul requires at least 2 inputs")
        product = 1.0
        for value in values:
            product *= value
        return float(product)
    raise ValueError(f"unsupported op {op}")


def _canonical_date(day: Any, month: Any, year: Any | None) -> str | None:
    """YYYY-MM-DD, or ??-MM-DD when the claim omitted the year. None if invalid."""
    try:
        d = int(day)
        m = int(month)
    except (TypeError, ValueError):
        return None
    if not (1 <= m <= 12 and 1 <= d <= 31):
        return None
    if year in (None, ""):
        return f"{_PARTIAL_DATE_PREFIX}-{m:02d}-{d:02d}"
    try:
        y = int(year)
    except (TypeError, ValueError):
        return None
    if y < 100:  # two-digit year, e.g. 31/07/26
        y += 2000
    if not (1900 <= y <= 2999):
        return None
    return f"{y:04d}-{m:02d}-{d:02d}"


def mask_dates(text: str) -> tuple[str, list[str]]:
    """Blank out every recognized date and return it in canonical form.

    Masking is what keeps a date from being re-read as loose digits, and it runs
    on the evidence side too so a date never grounds its own components.
    """
    raw = text or ""
    found: list[str] = []
    chars = list(raw)

    def _blank(start: int, end: int) -> None:
        for i in range(start, end):
            chars[i] = " "

    for match in _ISO_DATE_RE.finditer(raw):
        canon = _canonical_date(match.group(3), match.group(2), match.group(1))
        if canon:
            found.append(canon)
            _blank(*match.span())
    partial = "".join(chars)
    for match in _DMY_DATE_RE.finditer(partial):
        canon = _canonical_date(match.group(1), match.group(2), match.group(3))
        if canon:
            found.append(canon)
            _blank(*match.span())
    partial = "".join(chars)
    for match in _TEXT_DATE_RE.finditer(partial):
        month = _MONTHS_ES.get(match.group(2).lower())
        canon = _canonical_date(match.group(1), month, match.group(3))
        if canon:
            found.append(canon)
            _blank(*match.span())
    return "".join(chars), found


def _date_grounded(canonical: str, dates: set[str]) -> bool:
    if canonical in dates:
        return True
    if canonical.startswith(_PARTIAL_DATE_PREFIX):
        suffix = canonical[len(_PARTIAL_DATE_PREFIX):]
        return any(known.endswith(suffix) for known in dates)
    return False


def _collect_evidence_dates(value: Any, out: set[str]) -> None:
    if isinstance(value, str):
        out.update(mask_dates(value)[1])
        return
    if isinstance(value, dict):
        for inner in value.values():
            _collect_evidence_dates(inner, out)
        return
    if isinstance(value, (list, tuple)):
        for inner in value:
            _collect_evidence_dates(inner, out)


def _in_scope(item: Any, scope: set[str] | None) -> bool:
    return scope is None or str(getattr(item, "evidence_id", "")) in scope


def grounded_dates(store: EvidenceStore, scope: set[str] | None = None) -> set[str]:
    """Canonical dates the evidence contains. ``scope`` restricts to cited items."""
    out: set[str] = set()
    for item in store.items:
        if not _in_scope(item, scope):
            continue
        _collect_evidence_dates(item.data_view, out)
        _collect_evidence_dates(item.meta_view, out)
        _collect_evidence_dates(item.arguments, out)
    return {d for d in out if not d.startswith(_PARTIAL_DATE_PREFIX)}


def evidence_ids_in_paths(inputs: list[Any] | None) -> set[str]:
    """Which evidence items a calculation actually reads (first path segment)."""
    out: set[str] = set()
    for raw in inputs or []:
        head = str(raw or "").split(".")[0].strip()
        if head:
            out.add(head)
    return out


def _norm_number(raw: Any) -> str | None:
    """Canonical form so 2, "2", 2.0 and "2,0" compare equal. None if not numeric."""
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        value = float(str(raw).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        return None
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{round(value, 6):g}"


def _add_number(out: set[str], raw: Any) -> None:
    norm = _norm_number(raw)
    if norm is None:
        return
    out.add(norm)
    if norm.startswith("-"):
        # The magnitude is attested by the same evidence: a movement of -5 grounds
        # "bajó 5 unidades". Sign semantics are the composer's job, not grounding.
        out.add(norm[1:])


def _collect_evidence_numbers(value: Any, out: set[str]) -> None:
    """Numbers that literally occur as values in the evidence.

    Strings are tokenized (a description may legitimately carry a figure) but
    dates are removed first: "2026-09-16" must not ground 2026, 9 or 16.
    """
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        _add_number(out, value)
        return
    if isinstance(value, str):
        for token in _NUM_TOKEN_RE.findall(mask_dates(value)[0]):
            _add_number(out, token)
        return
    if isinstance(value, dict):
        for inner in value.values():
            _collect_evidence_numbers(inner, out)
        return
    if isinstance(value, (list, tuple)):
        for inner in value:
            _collect_evidence_numbers(inner, out)


def grounded_numbers(
    store: EvidenceStore,
    verified_results: list[Any] | None = None,
    scope: set[str] | None = None,
) -> set[str]:
    """Every figure the answer is allowed to state.

    Rule 1: it appears as a numeric token in THIS turn's evidence.
    Rule 2: it is the result of a calculation the verifier recomputed and matched,
            whose inputs are evidence paths by construction.

    Nothing else. A derived figure whose calculation did not verify is not here,
    which is exactly the C04 case: "suman 3 unidades (2 + 1)" with an unresolved
    calculation used to pass because "3" occurred somewhere inside the blob.
    """
    out: set[str] = set()
    for item in store.items:
        if not _in_scope(item, scope):
            continue
        _collect_evidence_numbers(item.data_view, out)
        _collect_evidence_numbers(item.meta_view, out)
        _collect_evidence_numbers(item.arguments, out)
    for result in verified_results or []:
        _add_number(out, result)
    return out


def _claim_number_tokens(text: str) -> list[str]:
    """Figures a claim asserts, from text whose dates were already masked.

    Product codes are masked here: they are validated by their own rule and must
    not be split into meaningless digits.
    """
    return _NUM_TOKEN_RE.findall(_CODE_RE.sub(" ", text or ""))


def _evidence_blob(
    store: EvidenceStore,
    extra: list[Any] | None = None,
    scope: set[str] | None = None,
) -> str:
    payload = []
    for item in store.items:
        if not _in_scope(item, scope):
            continue
        payload.append(
            {
                "id": item.evidence_id,
                "tool": item.tool,
                "arguments": item.arguments,
                "data": item.data_view,
                "meta": item.meta_view,
            }
        )
    if extra:
        payload.append({"calc": extra})
    return json.dumps(payload, ensure_ascii=False, default=str)


def _finance_null_zero(reply: str, store: EvidenceStore) -> bool:
    low = (reply or "").lower()
    if "ventas" not in low or not re.search(r"\b0(?:[.,]0+)?\b", low):
        return False
    for item in store.items:
        data = item.data_view if isinstance(item.data_view, dict) else {}
        for key in ("ventas_hoy", "ventas_mes", "ventas_periodo"):
            if key in data and data.get(key) is None:
                return True
    return False


def _claim_grounded(
    text: str,
    blob: str,
    tool_names: set[str],
    numbers: set[str] | None = None,
    dates: set[str] | None = None,
) -> bool:
    upper_blob = blob.upper()
    for token in _CODE_RE.findall((text or "").upper()):
        if token in tool_names:
            continue
        if token not in upper_blob:
            return False
    blob_l = blob.lower()
    text_u = text or ""
    allowed = numbers if numbers is not None else set()
    for pct in _PCT_RE.findall(text_u):
        if pct.lower() in blob_l or pct.replace(" ", "").lower() in blob_l.replace(" ", ""):
            continue
        # Not written verbatim in the evidence: its numeric part must then be a
        # grounded figure (evidence token or verified calculation).
        if _norm_number(re.sub(r"[^\d.,-]", "", pct)) not in allowed:
            return False
    # Dates first: each must name a date the evidence holds, in any supported
    # form. Their spans are then gone from the text, so "31 de julio de 2026"
    # is never re-read as the loose numbers 31 and 2026.
    known_dates = dates if dates is not None else set()
    text_wo_dates, claim_dates = mask_dates(text_u)
    for canonical in claim_dates:
        if not _date_grounded(canonical, known_dates):
            return False
    for num in _claim_number_tokens(text_wo_dates):
        if _norm_number(num) not in allowed:
            return False
    for name in ALLOWED_TOOLS:
        if name in text_u and name not in blob and name.upper() not in tool_names:
            # mentioning a tool not in this turn's evidence
            if name not in blob:
                return False
    invented = set()
    for word in re.findall(r"\b[a-z_]+\b", text_u.lower()):
        if word.startswith(("create_", "write_", "delete_", "update_")):
            invented.add(word)
    if invented:
        return False
    return True


def _composer_evidence(store: EvidenceStore) -> list[dict[str, Any]]:
    out = []
    for item in store.items:
        out.append(
            {
                "tool": item.tool,
                "ok": item.ok,
                "empty": item.empty,
                "error_code": item.error_code,
                "classification": item.classification,
                "data": item.data_view if isinstance(item.data_view, dict) else {},
                "meta": item.meta_view if isinstance(item.meta_view, dict) else {},
                "finance_redacted": item.finance_redacted,
                "stock_omitted": item.stock_omitted,
            }
        )
    return out


class VerifyResult:
    """Outcome of verification.

    ``failures`` keeps its original meaning and value — it is the guardrail
    activation count and no check was loosened to change it.

    FASE 8.1G adds a *breakdown* on top (observability only, never a decision
    input), because one number cannot distinguish two very different outcomes:

    - the model proposed an ungrounded claim, the verifier discarded it and still
      published a fully grounded answer (``dropped_claims``, guardrail working);
    - the verifier could not produce any grounded answer and Composer replaced it
      (``answer_replaced``, degraded turn).
    """

    def __init__(
        self,
        *,
        ok: bool,
        reply: str,
        failures: int,
        used_composer_fallback: bool,
        classification: str = "INTERNAL",
        dropped_claims: int = 0,
        dropped_draft: int = 0,
        calc_mismatch: int = 0,
        calc_unresolved: int = 0,
        calc_error: int = 0,
        answer_replaced: bool = False,
        replaced_reason: str | None = None,
        provenance_violations: int = 0,
        provenance_kinds: list[str] | None = None,
        provenance_by_kind: dict[str, int] | None = None,
        provenance_severity: str = "none",
        provenance_enforced: bool = False,
    ):
        self.ok = ok
        self.reply = reply
        self.failures = failures
        self.used_composer_fallback = used_composer_fallback
        self.classification = classification
        self.dropped_claims = int(dropped_claims)
        self.dropped_draft = int(dropped_draft)
        self.calc_mismatch = int(calc_mismatch)
        self.calc_unresolved = int(calc_unresolved)
        self.calc_error = int(calc_error)
        self.answer_replaced = bool(answer_replaced)
        self.replaced_reason = replaced_reason
        # FASE 8.2 — observation mode: counted and reported, not acted on unless
        # ANDES_ASSISTANT_PROVENANCE_ENFORCE=1.
        self.provenance_violations = int(provenance_violations)
        self.provenance_kinds = list(provenance_kinds or [])
        self.provenance_by_kind = dict(provenance_by_kind or {})
        self.provenance_severity = str(provenance_severity)
        self.provenance_enforced = bool(provenance_enforced)

    def breakdown(self) -> dict[str, Any]:
        """Enumerable counters. No claim text, no evidence values."""
        return {
            "failures": int(self.failures),
            "dropped_claims": int(self.dropped_claims),
            "dropped_draft": int(self.dropped_draft),
            "calc_mismatch": int(self.calc_mismatch),
            "calc_unresolved": int(self.calc_unresolved),
            "calc_error": int(self.calc_error),
            "answer_replaced": bool(self.answer_replaced),
            "replaced_reason": (
                str(self.replaced_reason)[:40] if self.replaced_reason else None
            ),
            "provenance_violations": int(self.provenance_violations),
            "provenance_kinds": list(self.provenance_kinds),
            "provenance_by_kind": dict(self.provenance_by_kind),
            "provenance_severity": str(self.provenance_severity),
            "provenance_enforced": bool(self.provenance_enforced),
        }


def verify_agent_answer(
    *,
    store: EvidenceStore,
    decision: dict[str, Any],
    raw_evidence: list[dict[str, Any]] | None = None,
    plan: dict[str, Any] | None = None,
) -> VerifyResult:
    from app.assistant.orchestrator.composer import compose_answer

    from app.assistant.orchestrator.agent_config import provenance_enforced

    enforce_provenance = provenance_enforced()
    provenance_violations: list[str] = []
    failures = 0
    # FASE 8.1G breakdown — counters only; every check below is unchanged.
    dropped_claims = 0
    dropped_draft = 0
    calc_mismatch = 0
    calc_unresolved = 0
    calc_error = 0
    calc_ok: list[dict[str, Any]] = []
    # FASE 8.5 — supuestos ya validados por analysis.validate_assumptions en el
    # parseo de la decision: aqui solo se resuelven como valores.
    declared = [a for a in (decision.get("assumptions") or []) if isinstance(a, dict)]
    assumption_map = {
        f"{a.get('id')}.value": float(a.get("value"))
        for a in declared if a.get("id") is not None and a.get("value") is not None
    }
    assumption_by_id = {str(a.get("id")): a for a in declared}
    for calc in decision.get("calculations") or []:
        try:
            # FASE 8.5 — una derivación real encadena pasos: demanda mensual ×
            # horizonte = necesidad, y necesidad − stock = brecha. Sin poder
            # referenciar el resultado anterior, el segundo paso era irresoluble
            # y la recomendación que colgaba de él se caía. Sólo se resuelven
            # cálculos YA verificados, así que no hay ciclos ni referencias hacia
            # adelante: un paso sólo puede apoyarse en lo ya demostrado.
            recomputed = recompute_calculation(
                store, calc, {**assumption_map,
                              **{str(c.get("id")): float(c.get("result"))
                                 for c in calc_ok if c.get("id") is not None}})
        except KeyError:
            # inputs pointed at an evidence id / path that does not exist
            failures += 1
            calc_unresolved += 1
            continue
        except Exception:
            failures += 1
            calc_error += 1
            continue
        claimed = float(calc.get("result") or 0)
        if abs(recomputed - claimed) > max(_EPS, abs(recomputed) * 1e-6):
            failures += 1
            calc_mismatch += 1
            continue
        calc_ok.append({**calc, "result": recomputed})

    blob = _evidence_blob(store, [c.get("result") for c in calc_ok])
    tool_names = {str(i.tool or "").upper() for i in store.items}
    # FASE 8.1H — the closed set of figures this answer may state: evidence
    # tokens plus the results of calculations that actually recomputed.
    numbers = grounded_numbers(store, [c.get("result") for c in calc_ok])
    dates = grounded_dates(store)
    known_ids = {str(i.evidence_id) for i in store.items}

    def _provenance(claim: dict[str, Any], text: str) -> str | None:
        """FASE 8.2 — does the claim's figures come from the evidence it cites?

        ``evidence_ids`` was parsed and then ignored, so a claim could cite e1
        while every figure in it came from e2, or cite an id that does not exist.
        Returns a violation code, or None when the citation holds.
        """
        cited = {str(x) for x in (claim.get("evidence_ids") or []) if x}
        if not cited:
            return None
        unknown = cited - known_ids
        if unknown:
            return "unknown_evidence_id"
        scoped_calcs = [
            c.get("result") for c in calc_ok
            if evidence_ids_in_paths(c.get("inputs")) <= cited
        ]
        scoped_numbers = grounded_numbers(store, scoped_calcs, scope=cited)
        scoped_dates = grounded_dates(store, scope=cited)
        scoped_blob = _evidence_blob(store, scoped_calcs, scope=cited)
        if _claim_grounded(text, scoped_blob, tool_names, scoped_numbers, scoped_dates):
            return None
        # Severidad por clase de cifra: una cardinalidad o un derivado atribuidos
        # a la evidencia equivocada son peores que una fecha mal citada, porque el
        # numero no existe en ninguna parte salvo por la calculation.
        if any(evidence_ids_in_paths(c.get("inputs")) - cited for c in calc_ok):
            return "derived_figure_outside_cited_evidence"
        text_wo_dates, claim_dates = mask_dates(text)
        if claim_dates and not _claim_number_tokens(text_wo_dates):
            return "date_outside_cited_evidence"
        return "figure_outside_cited_evidence"

    datos: list[str] = []
    inferencias: list[str] = []
    # FASE 8.5 — peldaños de la escalera, en el orden en que se publicarán.
    rungs: list[tuple[str, str]] = []
    unsupported_projection = 0
    for claim in order_claims([c for c in (decision.get("claims") or [])
                               if isinstance(c, dict)]):
        text = str(claim.get("text") or "").strip()
        kind = claim.get("kind")

        if kind == "supuesto":
            # El texto de un supuesto NO lo redacta el modelo: se reconstruye
            # desde el supuesto validado. Así no puede editorializar ("supongo,
            # razonablemente, una demanda alta") ni colar una cifra de paso.
            for aid in (claim.get("assumption_ids") or []):
                declared_a = assumption_by_id.get(str(aid))
                if declared_a is None:
                    continue
                value = float(declared_a.get("value"))
                shown = int(value) if value.is_integer() else value
                origin = ("indicado en la consulta"
                          if declared_a.get("basis") == "user_request"
                          else "valor por defecto del sistema")
                rungs.append((
                    "supuesto",
                    f"{declared_a.get('label')}: {shown} "
                    f"{declared_a.get('unit')} ({origin})."))
            continue

        if not text:
            continue

        if kind == "proyeccion":
            # Una proyección sin supuesto no es una proyección: es una cifra sin
            # condicional. Y su número tiene que salir de una calculation que
            # recomputó, igual que cualquier otro derivado.
            cited_a = [str(x) for x in (claim.get("assumption_ids") or [])
                       if str(x) in assumption_by_id]
            if not cited_a:
                failures += 1
                dropped_claims += 1
                unsupported_projection += 1
                continue
            if not _claim_grounded(text, blob, tool_names, numbers, dates):
                failures += 1
                dropped_claims += 1
                continue
            violation = _provenance(claim, text)
            if violation:
                provenance_violations.append(violation)
                if enforce_provenance:
                    failures += 1
                    dropped_claims += 1
                    continue
            rungs.append(("proyeccion", text))
            continue

        if kind in ("calculo", "recomendacion"):
            if not _claim_grounded(text, blob, tool_names, numbers, dates):
                failures += 1
                dropped_claims += 1
                continue
            violation = _provenance(claim, text)
            if violation:
                provenance_violations.append(violation)
                if enforce_provenance:
                    failures += 1
                    dropped_claims += 1
                    continue
            rungs.append((str(kind), text))
            continue

        if kind == "inferencia" and _NUM_RE.search(text):
            # inferences may not introduce ungrounded figures
            if not _claim_grounded(text, blob, tool_names, numbers, dates):
                failures += 1
                dropped_claims += 1
                continue
            violation = _provenance(claim, text)
            if violation:
                provenance_violations.append(violation)
                if enforce_provenance:
                    failures += 1
                    dropped_claims += 1
                    continue
            inferencias.append(text)
            continue
        if not _claim_grounded(text, blob, tool_names, numbers, dates):
            failures += 1
            dropped_claims += 1
            continue
        violation = _provenance(claim, text)
        if violation:
            provenance_violations.append(violation)
            if enforce_provenance:
                failures += 1
                dropped_claims += 1
                continue
        if kind == "dato":
            datos.append(text)
        else:
            inferencias.append(text)

    draft = str(decision.get("draft_reply") or "").strip()
    if draft and not (datos or inferencias):
        if _claim_grounded(draft, blob, tool_names, numbers, dates):
            datos.append(draft)
        else:
            failures += 1
            dropped_draft += 1

    # La escalera se publica con sus etiquetas. No son decoración: sin ellas
    # "necesitarías 24 unidades" y "hay 7 unidades" parecen la misma clase de
    # afirmación, y una es observada mientras la otra depende de un supuesto.
    rendered: list[tuple[str, str]] = [("dato", line) for line in datos]
    rendered += list(rungs)
    rendered += [("inferencia", line) for line in inferencias]
    reply = "\n".join(ladder_sections(rendered)).strip()

    breakdown = {
        "provenance_violations": len(provenance_violations),
        "provenance_kinds": sorted(set(provenance_violations)),
        "provenance_by_kind": {
            kind: provenance_violations.count(kind)
            for kind in sorted(set(provenance_violations))
        },
        "provenance_severity": (
            "high" if any(k in {"unknown_evidence_id",
                                "derived_figure_outside_cited_evidence"}
                          for k in provenance_violations)
            else ("medium" if provenance_violations else "none")
        ),
        "provenance_enforced": enforce_provenance,
        "dropped_claims": dropped_claims,
        "dropped_draft": dropped_draft,
        "calc_mismatch": calc_mismatch,
        "calc_unresolved": calc_unresolved,
        "calc_error": calc_error,
    }
    composer_evidence = raw_evidence if raw_evidence else _composer_evidence(store)
    if not reply:
        composed = compose_answer(plan=plan or {"answer_style": "operational"}, evidence=composer_evidence)
        return VerifyResult(
            ok=False,
            reply=str(composed.get("reply") or ""),
            failures=max(failures, 1),
            used_composer_fallback=True,
            classification=str(composed.get("classification") or "INTERNAL"),
            answer_replaced=True,
            replaced_reason="no_grounded_claim",
            **breakdown,
        )

    if _finance_null_zero(reply, store):
        failures += 1
        composed = compose_answer(plan=plan or {"answer_style": "operational"}, evidence=composer_evidence)
        return VerifyResult(
            ok=False,
            reply=str(composed.get("reply") or ""),
            failures=failures,
            used_composer_fallback=True,
            classification=str(composed.get("classification") or "INTERNAL"),
            answer_replaced=True,
            replaced_reason="finance_null_as_zero",
            **breakdown,
        )

    reply = _scrub_leaked_pii(reply, composer_evidence)
    if not reply_contains_only_evidence_values(reply, composer_evidence):
        # Allow verified calculation numbers that composer regex might miss
        calc_blob = blob
        if not _claim_grounded(reply, calc_blob, tool_names, numbers, dates):
            failures += 1
            composed = compose_answer(plan=plan or {"answer_style": "operational"}, evidence=composer_evidence)
            return VerifyResult(
                ok=False,
                reply=str(composed.get("reply") or ""),
                failures=failures,
                used_composer_fallback=True,
                classification=str(composed.get("classification") or "INTERNAL"),
                answer_replaced=True,
                replaced_reason="reply_not_only_evidence",
                **breakdown,
            )

    classification = "CONFIDENTIAL" if any(
        i.classification == "CONFIDENTIAL" for i in store.items
    ) else "INTERNAL"
    return VerifyResult(
        ok=failures == 0,
        reply=reply,
        failures=failures,
        used_composer_fallback=False,
        classification=classification,
        answer_replaced=False,
        **breakdown,
    )
