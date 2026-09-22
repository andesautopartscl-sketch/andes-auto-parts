"""FASE 8.5 — la escalera analítica: dato, cálculo, supuesto, proyección, recomendación.

EL BLOQUEO QUE RESUELVE
-----------------------
Medido, no supuesto. Con la arquitectura anterior:

    claims=[{"kind": "dato", "text": "El stock actual es 7 unidades."},
            {"kind": "dato", "text": "Para cubrir dos meses necesitarías 24 unidades."}]
    -> dropped_claims: 1
    -> publicado: "DATOS:\\n- El stock actual es 7 unidades."

La proyección se descarta, y con razón: 24 no está en la evidencia y el verifier
no puede distinguir un cálculo legítimo de una alucinación. Marcarla como
``inferencia`` tampoco servía, porque una inferencia con cualquier cifra se
rechaza de plano.

El resultado es que **toda la mitad analítica del negocio era inalcanzable**:
cobertura, demanda estimada, riesgo de quiebre, necesidad de reposición,
proyección de compra. No por falta de inteligencia del modelo, sino porque la
arquitectura no tenía dónde poner una cifra derivada.

Faltaban dos piezas, y las dos son estructurales:

1. **No existía multiplicación.** Las operaciones eran min/max/sum/diff/ratio/
   count, así que "12 unidades/mes × 2 meses" no era expresable.
2. **No existía forma de declarar un supuesto.** ``inputs`` sólo aceptaba rutas
   de evidencia, así que "dos meses" no tenía dónde vivir. Un horizonte no está
   en el ERP: lo pone la pregunta.

POR QUÉ UN SUPUESTO NO ES UNA PUERTA TRASERA
--------------------------------------------
Dejar que el modelo declare supuestos podría ser el mayor agujero jamás abierto
en este sistema: bastaría inventar "supongo que la demanda es 500/mes" para que
cualquier cifra quedara derivada de algo. Por eso un supuesto:

- es de un tipo cerrado (``ASSUMPTION_KINDS``), no texto libre;
- es numérico y con unidad;
- está acotado por rango, para que un horizonte de 9000 meses no pase;
- y sobre todo **tiene que justificar su origen**: o el valor aparece en la
  pregunta del usuario, o coincide con un default del sistema declarado aquí.
  Un supuesto que el modelo se inventa no se acepta.

Esa última regla es la que sostiene todo. Un supuesto no puede introducir
información nueva sobre el negocio: sólo puede recoger un parámetro que el
usuario pidió o uno que la casa declaró de antemano.

LO QUE NO CAMBIA
----------------
Un ``dato`` sigue exigiendo grounding literal contra la evidencia. La
procedencia sigue aplicando. Una proyección NUNCA se publica como un hecho: la
escalera se renderiza con sus etiquetas para que el lector vea exactamente qué
es observado, qué es aritmética y qué depende de un supuesto.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

# Tipos de supuesto admitidos. Cerrado a propósito: cada uno es un parámetro de
# planificación, nunca un dato del negocio.
ASSUMPTION_KINDS: dict[str, dict[str, Any]] = {
    # Cuántos meses hacia adelante se proyecta. Lo pone la pregunta.
    "horizon_months": {"min": 0.25, "max": 24.0, "unit": "meses",
                       "label": "horizonte de cobertura"},
    # Días del periodo observado sobre el que se calcula una tasa.
    "window_days": {"min": 1.0, "max": 730.0, "unit": "días",
                    "label": "ventana observada"},
    # Multiplicador de stock de seguridad sobre la demanda proyectada.
    "safety_factor": {"min": 1.0, "max": 3.0, "unit": "x",
                      "label": "factor de seguridad"},
    # Plazo de reposición del proveedor, en días.
    "lead_time_days": {"min": 0.0, "max": 365.0, "unit": "días",
                       "label": "plazo de reposición"},
}

# Defaults declarados por la casa. Un supuesto con basis="default" tiene que
# coincidir con uno de estos; no puede elegir su propio número.
ASSUMPTION_DEFAULTS: dict[str, float] = {
    "horizon_months": 2.0,
    "window_days": 30.0,
    "safety_factor": 1.0,
    "lead_time_days": 0.0,
}

VALID_BASES = frozenset({"user_request", "default"})

# Claims de la escalera. 'dato' e 'inferencia' son los de 8.1 y no cambian.
LADDER_KINDS = ("dato", "calculo", "supuesto", "proyeccion", "recomendacion")

# Orden de presentación. Una proyección detrás de sus supuestos, y la
# recomendación al final, para que nadie lea el número sin su condicional.
LADDER_ORDER = ("dato", "calculo", "supuesto", "proyeccion", "recomendacion",
                "inferencia")
LADDER_LABELS = {
    "dato": "DATOS",
    "calculo": "CÁLCULOS",
    "supuesto": "SUPUESTOS",
    "proyeccion": "PROYECCIÓN",
    "recomendacion": "RECOMENDACIÓN",
    "inferencia": "INFERENCIA",
}

_ASSUMPTION_ID_RE = re.compile(r"^a[1-9][0-9]{0,2}$")
_NUM_IN_TEXT_RE = re.compile(r"(?<![\d.,])\d+(?:[.,]\d+)?(?!\d)")
MAX_ASSUMPTIONS = 6


class AssumptionError(ValueError):
    """Un supuesto que no justifica su origen o sale de rango."""


def _fold(text: str) -> str:
    raw = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(c for c in raw if not unicodedata.combining(c))


_WORD_NUMBERS = {
    "un": 1.0, "una": 1.0, "uno": 1.0, "dos": 2.0, "tres": 3.0, "cuatro": 4.0,
    "cinco": 5.0, "seis": 6.0, "siete": 7.0, "ocho": 8.0, "nueve": 9.0,
    "diez": 10.0, "once": 11.0, "doce": 12.0, "quince": 15.0, "veinte": 20.0,
    "treinta": 30.0, "noventa": 90.0, "medio": 0.5,
}


def numbers_in_question(question: str) -> set[float]:
    """Valores que el usuario escribió, en cifra o en palabra.

    "para dos meses" y "para 2 meses" son la misma petición; si sólo se leyeran
    los dígitos, la mitad de las preguntas reales quedarían sin justificar su
    horizonte y el supuesto se rechazaría por una diferencia de redacción.
    """
    folded = _fold(question)
    out: set[float] = set()
    for token in _NUM_IN_TEXT_RE.findall(folded):
        try:
            out.add(float(token.replace(",", ".")))
        except ValueError:
            continue
    for word, value in _WORD_NUMBERS.items():
        if re.search(rf"\b{word}\b", folded):
            out.add(value)
    # "un trimestre" / "el trimestre" son horizontes idiomáticos frecuentes.
    if re.search(r"\btrimestre\b", folded):
        out.add(3.0)
    if re.search(r"\bsemestre\b", folded):
        out.add(6.0)
    if re.search(r"\b(anual|ano|year)\b", folded):
        out.add(12.0)
    return out


# Intencion analitica: la pregunta pide PROYECTAR, ESTIMAR o RECOMENDAR, no solo
# consultar. Cerrado y determinista, como el resto de detectores del sistema.
#
# Medido en el A/B de 8.5: inyectar la escalera en TODA pregunta costo +17% de
# tokens por turno, tumbo T09 contra el techo de presupuesto y —lo peor— cebo al
# modelo hacia lectura temporal: "2404" se leyo como "24/04" y dos preguntas
# inequivocas terminaron pidiendo aclaracion. La capacidad no estaba de mas; el
# problema era cargarla siempre.
_ANALYTIC_STEMS = (
    "deberia", "deberiamos", "necesitaria", "necesitariamos", "haria falta",
    "faltaria", "falta comprar", "cuanto comprar", "que comprar",
    "cobertura", "alcanza", "alcanzara", "cubrir", "cubro",
    "proyecta", "proyeccion", "proyectar", "estimar", "estimacion", "estimado",
    "recomienda", "recomiendas", "recomendacion", "recomiendame",
    "riesgo de quiebre", "quiebre de stock", "sobrestock", "reponer", "reposicion",
    "demanda estimada", "demanda proyectada", "rotacion",
)


def analytical_intent(message: str) -> bool:
    """True solo si la pregunta pide un derivado, no un dato.

    Deliberadamente conservador: ante la duda NO activa. Una pregunta analitica
    respondida sin escalera sigue siendo correcta —el modelo dira que no puede
    estimar, como hizo A03—; una pregunta normal contaminada por la escalera se
    rompe, que es lo que el A/B demostro.
    """
    folded = _fold(message)
    if not folded:
        return False
    return any(re.search(rf"(?<![a-z0-9]){re.escape(_fold(stem))}", folded)
               for stem in _ANALYTIC_STEMS)


@dataclass(frozen=True)
class Assumption:
    id: str
    kind: str
    value: float
    basis: str

    @property
    def unit(self) -> str:
        return str(ASSUMPTION_KINDS[self.kind]["unit"])

    @property
    def label(self) -> str:
        return str(ASSUMPTION_KINDS[self.kind]["label"])

    def sentence(self) -> str:
        """Texto determinista. No lo redacta el modelo: lo redacta la casa."""
        value = int(self.value) if float(self.value).is_integer() else self.value
        origin = ("indicado en la consulta" if self.basis == "user_request"
                  else "valor por defecto del sistema")
        return f"{self.label}: {value} {self.unit} ({origin})."

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "value": self.value,
                "unit": self.unit, "basis": self.basis, "label": self.label}


def validate_assumptions(
    raw: Any,
    *,
    question: str = "",
) -> list[Assumption]:
    """Acepta sólo supuestos que justifiquen de dónde sale su número.

    Lanza AssumptionError en vez de descartar en silencio: un supuesto mal
    formado invalida la proyección que cuelga de él, y esconderlo publicaría una
    cifra sin su condicional.
    """
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise AssumptionError("assumptions must be a list")
    if len(raw) > MAX_ASSUMPTIONS:
        raise AssumptionError("too many assumptions")

    asked = numbers_in_question(question)
    seen: set[str] = set()
    out: list[Assumption] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise AssumptionError("assumption must be an object")
        aid = str(entry.get("id") or "").strip()
        if not _ASSUMPTION_ID_RE.match(aid) or aid in seen:
            raise AssumptionError(f"invalid assumption id {aid!r}")
        seen.add(aid)
        kind = str(entry.get("kind") or "").strip().lower()
        spec = ASSUMPTION_KINDS.get(kind)
        if spec is None:
            raise AssumptionError(f"unknown assumption kind {kind!r}")
        try:
            value = float(str(entry.get("value")).replace(",", "."))
        except (TypeError, ValueError) as exc:
            raise AssumptionError("assumption value must be numeric") from exc
        if value != value or not (spec["min"] <= value <= spec["max"]):
            raise AssumptionError(f"{kind} out of range: {value}")
        basis = str(entry.get("basis") or "").strip().lower()
        if basis not in VALID_BASES:
            raise AssumptionError(f"invalid assumption basis {basis!r}")
        if basis == "user_request" and value not in asked:
            # El modelo dice que lo pidió el usuario y el usuario no lo escribió.
            raise AssumptionError(f"{kind}={value} not present in the question")
        if basis == "default" and value != ASSUMPTION_DEFAULTS.get(kind):
            # Un "default" que no es el default es un número inventado.
            raise AssumptionError(f"{kind}={value} is not the declared default")
        out.append(Assumption(aid, kind, value, basis))
    return out


def assumption_values(assumptions: list[Assumption]) -> dict[str, float]:
    """Mapa ``a1.value`` -> número, para resolver rutas en las calculations."""
    return {f"{a.id}.value": float(a.value) for a in assumptions}


def is_assumption_path(path: str) -> bool:
    head = str(path or "").split(".", 1)[0]
    return bool(_ASSUMPTION_ID_RE.match(head))


def order_claims(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ordena por peldaño conservando el orden dentro de cada uno.

    Importa que sea estable: el modelo puede tener una secuencia lógica dentro de
    los datos o dentro de los cálculos, y reordenar dentro del peldaño la
    rompería.
    """
    rank = {kind: i for i, kind in enumerate(LADDER_ORDER)}
    return sorted(claims, key=lambda c: rank.get(str(c.get("kind")), len(rank)))


def ladder_sections(
    rendered: list[tuple[str, str]], *, has_evidence: bool = True
) -> list[str]:
    """Agrupa (kind, texto) bajo sus etiquetas, en orden de escalera.

    La etiqueta no es decoración: es lo que impide leer una proyección como un
    hecho. Si un día se quitan las cabeceras, "necesitarías 24 unidades" y "hay 7
    unidades" pasan a parecer la misma clase de afirmación.

    FASE 9.7 — medido en la primera prueba real con lenguaje natural:

        usuario   : "Hola"
        asistente : "DATOS:\\n- Hola, ¿en qué puedo ayudarte?"

    La cabecera afirmaba que un saludo era un dato observado del ERP.

    FASE 9.8 — la primera corrección fue DEMASIADO ancha y se midió: quitar la
    etiqueta a toda respuesta de una sola clase tumbó el benchmark de 72/76 a
    31/76. La causa está en `evals/fase81_scorer.py:243`: un caso con
    ``expected_data_vs_inference`` exige que la respuesta diga "datos", porque
    esa palabra ES la señal de que el turno separó lo observado de lo inferido.
    La queja original era "DATOS: aparece incluso en SALUDOS", no en respuestas
    de datos.

    La regla correcta, por tanto, mira si el turno consultó algo:

        sin evidencia y una sola clase  -> es charla, no lleva etiqueta
        con evidencia                   -> lleva etiqueta, siempre
        dos o más clases                -> lleva etiqueta, siempre

    ``has_evidence`` por defecto True: cualquier llamador que no se pronuncie
    conserva el comportamiento de 8.1.
    """
    clases = {k for k, _ in rendered if k}
    etiquetar = len(clases) >= 2 or bool(has_evidence)
    out: list[str] = []
    for kind in LADDER_ORDER:
        lines = [text for k, text in rendered if k == kind]
        if not lines:
            continue
        if etiquetar:
            out.append(f"{LADDER_LABELS.get(kind, kind.upper())}:")
            out.extend(f"- {line}" for line in lines)
        else:
            # Sin etiqueta tampoco hacen falta viñetas para una sola idea: una
            # frase suelta se lee como una frase, no como un informe de un ítem.
            out.extend([lines[0]] if len(lines) == 1 else [f"- {l}" for l in lines])
    return out
