"""FASE 8.3 — ¿la respuesta usa los datos que el agente fue a buscar?

EL PROBLEMA, Y POR QUÉ ERA PREDECIBLE
-------------------------------------
8.1 y 8.2 construyeron una garantía fuerte: toda cifra publicada tiene que estar
en la evidencia, y todo claim tiene que citar la evidencia de la que sale. Eso
hace imposible mentir. Pero crea una asimetría: publicar una cifra equivocada
cuesta el claim entero, y no publicar ninguna no cuesta nada.

Bajo ese incentivo la estrategia más segura para el modelo es **describir la
forma de los datos en vez de citarlos**. Y eso es exactamente lo que se observó
con LLM real, en la misma pregunta y sobre la misma evidencia:

    F01  "...tuvo un ingreso de 2 unidades el 2026-07-31..."          útil
    K03  "...ingresos y ajustes en fechas 2026-07-31 y 2026-05-12"    útil
    C07  "...incluyen ingresos y ajustes en Bodega 1 con marca BOSCH" inútil
    E06  "...incluyen ingresos y ajustes en Bodega 1 con marca BOSCH" inútil
    F06  "...incluyen ingresos y ajustes en la bodega 1"              inútil
    K06  "...incluyen ingresos y un ajuste negativo reciente"         inútil
    G07  "Se encontraron proveedores con la letra 'a' en su nombre"   inútil

Las siete están *grounded*. Ninguna miente. Cuatro no sirven para nada. El
sistema no podía notar la diferencia porque medía veracidad, no suficiencia.

QUÉ SE MIDE
-----------
El dual exacto del grounding. El grounding pregunta "¿toda cifra de la respuesta
está en la evidencia?". La suficiencia pregunta "¿algo de la evidencia está en la
respuesta?".

Formulado así es objetivo y NO puede empujar al modelo a inventar: sólo cuenta
valores que YA están en la evidencia verificada. Un dato inventado no suma aquí
—no está en el store— y además lo tumba el grounding. Utilidad y veracidad no
compiten: esta métrica sólo puede subir citando datos reales.

CÓMO, Y POR QUÉ ASÍ
-------------------
Dos preguntas independientes, porque un payload responde de dos maneras:

- **filas**: de las N filas recuperadas, ¿cuántas se identifican en la respuesta?
  Una fila está identificada si aparece alguno de sus valores distintivos: cifra,
  fecha o NOMBRE. Un primer diseño sólo numérico era estructuralmente ciego al
  caso del directorio de proveedores, donde la información útil son nombres.
- **escalares**: las medidas de resumen. Un KPI se responde con ellas, no
  recitando su serie diaria de 30 fechas, que es el eje y no la respuesta.

Sólo cuentan los campos que **varían entre filas**: `fecha` y `cantidad` cambian
fila a fila, `marca` y `bodega` no. Decir "hay movimientos en Bodega 1 con marca
BOSCH" no transmite nada que el usuario no pudiera suponer. Un campo constante
describe el conjunto; uno variable lo distingue.

Se descuenta lo que ya venía en la pregunta: devolver "2404" cuando el usuario
escribió "2404" no es informar.

Y la regla es CONDICIONAL: sólo se exige enseñar algo si la evidencia ofrecía
algo. Si `get_stock_movements` volvió vacío, "no hay movimientos" es la respuesta
suficiente. Sin esa condición la métrica presionaría por rellenar, que es
exactamente lo que no se quiere.

LO QUE NO HACE
--------------
No decide cuánto es "bastante". Marcar insuficiente exige que la respuesta no
haya tomado NADA por ninguna de las dos vías: el extremo inequívoco. Una
respuesta que enseña 1 de 5 filas se reporta con su proporción y no se juzga:
elegir ese umbral con catorce ejemplos etiquetados a mano sería ajustar a la
muestra, y es el error que ya se cometió una vez en 8.1I.2.

EXACTITUD MEDIDA Y LÍMITE CONOCIDO
----------------------------------
Contra catorce respuestas reales etiquetadas a mano: **12/14, con CERO falsos
positivos**. Ese perfil es deliberado —sub-detecta, nunca acusa de más— porque
el daño de marcar como pobre una respuesta correcta es muy superior al de dejar
pasar una pobre.

Los dos fallos son el mismo: la métrica no sabe a QUÉ requisito pertenece cada
cifra de la respuesta. Cuando un turno responde dos temas ("movimientos y stock
actual"), el `2` de la frase del stock acredita también a las filas de
movimientos, y un resumen vago de los movimientos queda tapado por la mitad
buena. El arreglo existe y está identificado: el verifier ya recibe claims con
`evidence_ids`, así que la suficiencia puede calcularse por claim y no sobre el
texto entero. No se hace aquí porque exige medir antes el efecto con LLM real.

MODO OBSERVACIÓN
----------------
Igual que la procedencia en 8.2: se mide y se reporta, no altera la respuesta.
Cambiar lo que se publica exige medirlo antes con LLM real.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.assistant.orchestrator.answer_verifier import (
    _claim_number_tokens,
    _norm_number,
    mask_dates,
)
from app.assistant.orchestrator.evidence_store import EvidenceStore

# Un identificador (documento, RUT, id interno) no es una medida: repetirlo no
# responde "cuánto" ni "cuándo", y además colisiona con cantidades pequeñas.
_ID_LIKE_KEYS = ("id", "codigo", "rut", "numero", "documento", "doc", "folio")

# Demasiado comunes para que aparecer en la respuesta pruebe que se miró el dato.
_STOPWORDS = frozenset({
    "de", "del", "la", "el", "los", "las", "un", "una", "y", "o", "en", "con",
    "por", "para", "sin", "no", "si", "es", "son", "hay", "total", "datos",
    "stock", "producto", "productos", "bodega", "unidad", "unidades", "marca",
    "fecha", "tipo", "nombre", "cantidad", "ingreso", "ingresos", "ajuste",
    "ajustes", "actual", "null", "none", "true", "false", "inferencia",
})
_WORD_RE = re.compile(
    r"[0-9a-zA-ZáéíóúüñÁÉÍÓÚÜÑ][0-9a-zA-ZáéíóúüñÁÉÍÓÚÜÑ.\-]{2,}")
_MAX_ROWS = 500

# Una respuesta puede transmitir el cero sin escribirlo: "no hubo ventas" dice
# exactamente lo mismo que "ventas: 0" y es mejor castellano. Sin reconocerlo, la
# métrica marcaba como pobres las respuestas negativas CORRECTAS, que en un ERP
# son una clase grande y legítima —y confundirlas con respuestas vacuas sería el
# peor error que esta métrica puede cometer.
_NEGATION_RE = re.compile(
    r"\bno\s+(?:se\s+)?(?:hubo|hay|existe|existen|tiene|tienen|registr\w*|"
    r"encontr\w*|dispone|consta\w*)\b|\bsin\s+(?:registros|movimientos|ventas|"
    r"resultados|datos)\b|\bningun\w*\b",
    re.IGNORECASE)


def _is_id_like(key: str) -> bool:
    low = str(key or "").strip().lower()
    return any(low == k or low.endswith("_" + k) or low.startswith(k + "_")
               for k in _ID_LIKE_KEYS)


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _WORD_RE.findall(text or "")} - _STOPWORDS


def _numbers(text: str) -> set[str]:
    masked, _dates = mask_dates(text or "")
    return {n for n in (_norm_number(t) for t in _claim_number_tokens(masked)) if n}


def _dates(text: str) -> set[str]:
    _masked, found = mask_dates(text or "")
    return {d for d in found if not d.startswith("??")}


def _rows_of(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list) and value and all(isinstance(r, dict) for r in value):
        return value[:_MAX_ROWS]
    return []


def _hashable(value: Any) -> Any:
    return repr(value) if isinstance(value, (dict, list)) else value


def _varying_keys(rows: list[dict[str, Any]]) -> set[str]:
    """Claves cuyo valor NO es el mismo en todas las filas.

    Con una sola fila no hay constantes que descartar: todo su contenido es la
    información disponible, así que se admiten todas sus claves.
    """
    if len(rows) <= 1:
        return {k for r in rows for k in r}
    out: set[str] = set()
    for key in {k for r in rows for k in r}:
        if len({_hashable(r.get(key)) for r in rows}) > 1:
            out.add(key)
    return out


def _row_fingerprint(row: dict[str, Any], keys: set[str]) -> tuple[set[str], bool]:
    """Lo que distingue a ESTA fila, y si la fila contiene algo que informar.

    Devuelve (huella, informativa). Una fila cuyas medidas son todas cero no es
    dato que enseñar: la serie diaria de un KPI sin ventas son 40 filas de ceros,
    y exigir que la respuesta las cite marcaría como pobre justamente la
    respuesta correcta —"no hubo ventas en los últimos 7 días"—. Esa clase de
    respuesta negativa es grande y legítima en un ERP; confundirla con una
    respuesta vacua sería el peor error posible de esta métrica.
    """
    out: set[str] = set()
    informative = False
    for key in keys:
        if _is_id_like(key):
            continue
        value = row.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            norm = _norm_number(value)
            if norm is not None:
                out.add(norm)
                if float(value) != 0.0:
                    informative = True
        elif isinstance(value, str):
            dates = _dates(value)
            words = _tokens(mask_dates(value)[0])
            out |= dates
            out |= words
            # Una fecha dice CUÁNDO, no QUÉ. Un bucket diario con ventas 0 y
            # documentos 0 no deja de estar vacío por llevar fecha, así que la
            # fecha sola no vuelve informativa a la fila; un nombre sí.
            if words:
                informative = True
    return out, informative


def _label_numbers(rows: list[dict[str, Any]], varying: set[str]) -> set[str]:
    """Números que además viven en un campo constante o de identificador.

    "Bodega 1" pone un 1 en el texto indistinguible de una cantidad 1. Medido
    contra el Gateway real: la respuesta vaga de C07 aprobaba únicamente por esa
    colisión, así que sin esto la métrica no separa lo útil de lo vacuo. Excluir
    el valor ambiguo puede hacer que se pase por alto una cita legítima de "1",
    y esa es la dirección correcta del error: sub-detectar, nunca acusar de más.
    """
    out: set[str] = set()
    for row in rows:
        for key, value in row.items():
            if key in varying and not _is_id_like(key):
                continue
            if isinstance(value, bool) or value is None:
                continue
            if isinstance(value, (int, float)):
                norm = _norm_number(value)
                if norm is not None:
                    out.add(norm)
            elif isinstance(value, str):
                out |= _numbers(value)
    return out


def _collections(value: Any, acc: list[list[dict[str, Any]]], depth: int = 0) -> None:
    if depth > 6:
        return
    rows = _rows_of(value)
    if rows:
        acc.append(rows)
        return
    if isinstance(value, dict):
        for sub in value.values():
            _collections(sub, acc, depth + 1)
    elif isinstance(value, list):
        for sub in value:
            _collections(sub, acc, depth + 1)


def _scalar_measures(value: Any, out: set[str], depth: int = 0) -> None:
    """Medidas de resumen del payload: el titular, no el detalle.

    Un payload de KPIs responde en sus escalares (ventas, documentos) y arrastra
    además una serie diaria que es el eje. Exigir que se recite la serie marcaría
    como pobre una respuesta correcta, así que resumen y detalle se cuentan por
    separado y cualquiera de los dos basta.
    """
    if depth > 3 or not isinstance(value, dict):
        return
    for key, sub in value.items():
        if _is_id_like(key) or isinstance(sub, bool) or sub is None:
            continue
        if isinstance(sub, (int, float)):
            norm = _norm_number(sub)
            if norm is not None:
                out.add(norm)
        elif isinstance(sub, dict):
            _scalar_measures(sub, out, depth + 1)


@dataclass(frozen=True)
class RequirementSufficiency:
    requirement_id: str
    requirement_type: str
    status: str
    rows_available: int
    rows_surfaced: int
    scalars_available: int
    scalars_cited: int
    sufficient: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.requirement_id, "type": self.requirement_type,
            "status": self.status, "rows_available": self.rows_available,
            "rows_surfaced": self.rows_surfaced,
            "scalars_available": self.scalars_available,
            "scalars_cited": self.scalars_cited,
            "sufficient": self.sufficient, "reason": self.reason,
        }


@dataclass(frozen=True)
class SufficiencyResult:
    requirements: tuple[RequirementSufficiency, ...]
    verdict: str
    row_utilization: float
    insufficient_types: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "row_utilization": self.row_utilization,
            "insufficient_types": list(self.insufficient_types),
            "requirements": [r.as_dict() for r in self.requirements],
        }

    def safe_snapshot(self) -> dict[str, Any]:
        """Conteos y tipos. Ni texto de respuesta, ni de evidencia, ni PII."""
        return {
            "verdict": self.verdict,
            "row_utilization": self.row_utilization,
            "insufficient_types": list(self.insufficient_types),
            "requirements": [
                {k: v for k, v in r.as_dict().items() if k != "id"}
                for r in self.requirements
            ],
        }


def analyze_sufficiency(
    *,
    store: EvidenceStore,
    goal: Any,
    reply: str,
    question: str = "",
) -> SufficiencyResult:
    """Por requisito: ¿la respuesta deja ver los datos que lo cubren?

    Se evalúa requisito a requisito y no sobre la respuesta entera porque una
    respuesta puede resolver bien una mitad y despachar la otra con una vaguedad
    —K06 citó el stock exacto y resumió los movimientos como "un ajuste negativo
    reciente"—. Medida en bloque, K06 parecería suficiente.
    """
    echo = _tokens(question) | _numbers(question) | _dates(question)
    cited = _tokens(reply) | _numbers(reply) | _dates(reply)

    out: list[RequirementSufficiency] = []
    total_rows = 0
    total_surfaced = 0

    for req in getattr(goal, "requirements", None) or []:
        status = str(getattr(req, "status", "") or "")
        eids = set(getattr(req, "evidence_ids", None) or ())
        rtype = str(getattr(req, "type", "") or "")
        rid = str(getattr(req, "id", "") or "")
        if status != "covered" or not eids:
            # Un requisito imposible o sin cubrir ya lo gobiernan GoalCoverage y
            # el verifier. Pedirle datos aquí sería pedir lo que no existe.
            out.append(RequirementSufficiency(
                rid, rtype, status, 0, 0, 0, 0, True, "not_applicable"))
            continue

        rows_available = rows_surfaced = 0
        scalars: set[str] = set()
        for item in store.items:
            if item.evidence_id not in eids:
                continue
            _scalar_measures(item.data_view, scalars)
            groups: list[list[dict[str, Any]]] = []
            _collections(item.data_view, groups)
            for rows in groups:
                keys = _varying_keys(rows)
                ambiguous = _label_numbers(rows, keys)
                for row in rows:
                    fingerprint, informative = _row_fingerprint(row, keys)
                    fingerprint -= echo | ambiguous
                    if not fingerprint or not informative:
                        continue
                    rows_available += 1
                    if fingerprint & cited:
                        rows_surfaced += 1

        scalars -= echo
        scalars_cited = len(scalars & cited)
        total_rows += rows_available
        total_surfaced += rows_surfaced

        if not rows_available and not scalars:
            out.append(RequirementSufficiency(
                rid, rtype, status, 0, 0, 0, 0, True, "no_data_to_cite"))
            continue
        # Todas las medidas de resumen en cero y la respuesta afirmando ausencia:
        # el cero se transmitió, sólo que en palabras.
        all_zero = bool(scalars) and all(s in ("0", "-0") for s in scalars)
        negated = all_zero and bool(_NEGATION_RE.search(reply or ""))
        sufficient = bool(rows_surfaced or scalars_cited or negated)
        reason = "ok" if (rows_surfaced or scalars_cited) else (
            "stated_absence" if negated else "showed_no_retrieved_data")
        out.append(RequirementSufficiency(
            rid, rtype, status, rows_available, rows_surfaced,
            len(scalars), scalars_cited, sufficient, reason))

    insufficient = tuple(r.requirement_type for r in out if not r.sufficient)
    if not out or all(r.reason in ("not_applicable", "no_data_to_cite") for r in out):
        verdict = "not_applicable"
    else:
        verdict = "insufficient" if insufficient else "sufficient"
    return SufficiencyResult(
        tuple(out), verdict,
        round(total_surfaced / total_rows, 4) if total_rows else 0.0,
        insufficient)
