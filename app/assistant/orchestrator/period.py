"""FASE 8.x — traduccion DETERMINISTA de un periodo en lenguaje natural a fechas.

Por que existe, medido contra el ERP real:

    get_sales(fecha_desde=2026-01-01, fecha_hasta=2026-03-31)  ->  0 ventas
    get_sales()                       [sin filtro]             ->  2 documentos de 2026-04-07

V02 pregunta "las ventas de enero a marzo de 2026". El sistema no podia expresar
esa ventana: ``normalize_agent_args`` descarta toda fecha que no aparezca como
YYYY-MM-DD en la pregunta — regla anti-alucinacion correcta — asi que la llamada
salia SIN filtro y devolvia documentos de ABRIL. Las cifras estaban grounded y la
respuesta era falsa igualmente. El brazo ON llego a publicar "en el periodo
consultado" y el scorer lo aprobo, porque una afirmacion sin cifra ni fecha no
tiene con que contrastarse.

Reparto de la culpa medido en el dataset: 7 de 76 casos (9,2%) expresan el
periodo en lenguaje natural y solo UNO usa formato ISO. La forma natural es la
norma, no la excepcion.

TRES REGLAS QUE ESTE MODULO NO ROMPE

1. **Lee el mensaje del USUARIO, nunca la salida del modelo.** Resolver
   "enero a marzo de 2026" a dos fechas es una derivacion de las palabras del
   propio usuario, de la misma clase que ``normalize_code``. No es que el modelo
   proponga una fecha y el sistema se la crea.

2. **Gramatica cerrada. Lo que no reconoce, lo rechaza.** Sin adivinar. Un
   ``None`` devuelve el sistema al comportamiento de hoy: sin filtro y con el
   alcance declarado. Honesto aunque incompleto es mejor que util y falso.

3. **Sin ano no hay ventana.** "ventas de marzo" podria ser cualquier marzo;
   elegir el mas reciente seria una suposicion, y las suposiciones de este
   sistema viven en ``analysis.ASSUMPTION_KINDS``, declaradas y acotadas, no
   escondidas en un parser. Lo relativo ("ultimos 7 dias", "mes pasado") SI se
   resuelve porque su ancla es la fecha de servidor, que es un dato.
"""
from __future__ import annotations

import re
import unicodedata
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta

# Ventana maxima que el ERP admite en get_sales. Un periodo mas largo se rechaza
# aqui en vez de morir en la frontera exterior con un 400.
MAX_WINDOW_DAYS = 400

# Cota superior de "ultimos N": mas alla, N deja de ser una expresion coloquial y
# empieza a ser una cifra que conviene escribir como fecha.
MAX_RELATIVE_UNITS = 120

MONTHS: dict[str, int] = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}

_MONTH_ALT = "|".join(sorted(MONTHS, key=len, reverse=True))
_QUARTERS = {"primer": 1, "1er": 1, "segundo": 2, "2do": 2,
             "tercer": 3, "3er": 3, "cuarto": 4, "4to": 4}
_QUARTER_ALT = "|".join(sorted(_QUARTERS, key=len, reverse=True))

# Un ano plausible para este ERP. Fuera de rango no es un ano, es otra cifra.
_MIN_YEAR, _MAX_YEAR = 2000, 2100

# Una regla que RECONOCE su forma y la rechaza por ambigua tiene que abortar la
# resolucion entera, no dejar paso a una regla mas general.
#
# Medido al construir esto: "de noviembre a marzo de 2026" cruza el ano, asi que
# `_rule_month_range` lo rechazaba — y `_rule_single_month` recogia "marzo de
# 2026" y devolvia marzo a secas. El sistema habria contestado en silencio una
# pregunta distinta de la que se hizo, que es exactamente el defecto de V02 que
# este modulo existe para cerrar.
AMBIGUOUS = object()


@dataclass(frozen=True)
class ResolvedPeriod:
    """Una ventana derivada del mensaje, con la frase exacta que la produjo.

    `expression` es lo que hace auditable la resolucion: permite mostrar al
    usuario de que palabras suyas salio la ventana, y permite a una prueba
    comprobar que no salio de ninguna otra parte.
    """

    desde: date
    hasta: date
    expression: str
    rule: str

    def as_args(self) -> dict[str, str]:
        return {"fecha_desde": self.desde.isoformat(),
                "fecha_hasta": self.hasta.isoformat()}

    def boundaries(self) -> frozenset[str]:
        return frozenset({self.desde.isoformat(), self.hasta.isoformat()})


def fold(text: str) -> str:
    """Minusculas sin acentos. 'Últimos' y 'ultimos' son la misma palabra."""
    norm = unicodedata.normalize("NFD", str(text or "").lower())
    return "".join(c for c in norm if unicodedata.category(c) != "Mn")


def _month_end(year: int, month: int) -> date:
    return date(year, month, monthrange(year, month)[1])


def _add_months(anchor: date, months: int) -> date:
    total = (anchor.year * 12 + anchor.month - 1) + months
    return date(total // 12, total % 12 + 1, 1)


def _valid_year(raw: str) -> int | None:
    try:
        year = int(raw)
    except (TypeError, ValueError):
        return None
    return year if _MIN_YEAR <= year <= _MAX_YEAR else None


# --- reglas. Cada una devuelve (desde, hasta) o None; ninguna adivina. --------

def _rule_month_range(text: str, today: date):
    """'de enero a marzo de 2026', 'enero a marzo 2026'."""
    m = re.search(rf"\b({_MONTH_ALT})\s+(?:a|hasta)\s+({_MONTH_ALT})\s+"
                  rf"(?:de\s+|del\s+)?(\d{{4}})\b", text)
    if not m:
        return None
    year = _valid_year(m.group(3))
    if year is None:
        return None
    start, end = MONTHS[m.group(1)], MONTHS[m.group(2)]
    if start > end:
        # "de noviembre a marzo de 2026" cruza el ano y no dice de que ano es
        # cada mes. Se aborta: caer a una regla mas general daria marzo a secas.
        return AMBIGUOUS
    return date(year, start, 1), _month_end(year, end), m.group(0)


def _rule_single_month(text: str, today: date):
    """'marzo de 2026', 'en marzo del 2026'."""
    m = re.search(rf"\b({_MONTH_ALT})\s+(?:de\s+|del\s+)(\d{{4}})\b", text)
    if not m:
        return None
    year = _valid_year(m.group(2))
    if year is None:
        return None
    month = MONTHS[m.group(1)]
    return date(year, month, 1), _month_end(year, month), m.group(0)


def _rule_quarter(text: str, today: date):
    """'primer trimestre de 2026', 'Q1 2026'."""
    m = re.search(rf"\b({_QUARTER_ALT})\s+trimestre\s+(?:de\s+|del\s+)?(\d{{4}})\b", text)
    quarter = None
    if m:
        quarter, raw_year = _QUARTERS[m.group(1)], m.group(2)
    else:
        m = re.search(r"\bq([1-4])\s+(?:de\s+|del\s+)?(\d{4})\b", text)
        if m:
            quarter, raw_year = int(m.group(1)), m.group(2)
    if quarter is None:
        return None
    year = _valid_year(raw_year)
    if year is None:
        return None
    first = 3 * (quarter - 1) + 1
    return date(year, first, 1), _month_end(year, first + 2), m.group(0)


def _rule_relative_units(text: str, today: date):
    """'ultimos 7 dias', 'ultimas 2 semanas', 'ultimos 3 meses'.

    Ancladas a la fecha de servidor, que es un dato, no una suposicion. La
    ventana incluye hoy: "los ultimos 7 dias" son hoy y los seis anteriores.
    """
    m = re.search(r"\bultim[oa]s?\s+(\d{1,3})\s+(dias?|semanas?|meses|mes)\b", text)
    if not m:
        return None
    n = int(m.group(1))
    if n < 1 or n > MAX_RELATIVE_UNITS:
        return None
    unit = m.group(2)
    if unit.startswith("dia"):
        return today - timedelta(days=n - 1), today, m.group(0)
    if unit.startswith("semana"):
        return today - timedelta(days=7 * n - 1), today, m.group(0)
    start = _add_months(date(today.year, today.month, 1), -(n - 1))
    return start, today, m.group(0)


def _rule_named_relative(text: str, today: date):
    """'este mes', 'mes pasado', 'este ano', 'ano pasado', 'hoy', 'ayer'."""
    if re.search(r"\bmes\s+pasado\b", text) or re.search(r"\bel\s+mes\s+anterior\b", text):
        first = _add_months(date(today.year, today.month, 1), -1)
        return first, _month_end(first.year, first.month), "mes pasado"
    if re.search(r"\b(este|el\s+presente)\s+mes\b", text) or re.search(r"\bmes\s+actual\b", text):
        return date(today.year, today.month, 1), today, "este mes"
    if re.search(r"\bano\s+pasado\b", text):
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31), "ano pasado"
    if re.search(r"\b(este|el\s+presente)\s+ano\b", text):
        return date(today.year, 1, 1), today, "este ano"
    if re.search(r"\bayer\b", text):
        return today - timedelta(days=1), today - timedelta(days=1), "ayer"
    if re.search(r"\bhoy\b", text):
        return today, today, "hoy"
    return None


# Orden deliberado: de lo mas especifico a lo mas general.
#
# NO hay regla para un ano suelto ("ventas de 2026"), y la ausencia es el
# resultado de una medicion, no un olvido. En el catalogo real hay 1.054 codigos
# de producto de cuatro digitos y 55 de ellos caen entre 2000 y 2100: 2001, 2020,
# 2024, 2025... "Stock del 2024" es un codigo de producto en este negocio, no un
# ano. Una regla de ano suelto convertiria 55 consultas de producto en consultas
# de periodo — el mismo falso positivo que descarto usar CODE_LIKE_RE como
# extractor de codigos, donde detectaba el 8888 de los casos anti-alucinacion.
#
# Las demas reglas son inmunes porque exigen un ancla lexica ("marzo de 2024",
# "primer trimestre de 2024"): con el nombre del mes delante, el 2024 es un ano
# sin ambiguedad. Quien quiera un ano completo puede decir "de enero a diciembre
# de 2026" o escribir las fechas.
_RULES = (
    ("month_range", _rule_month_range),
    ("quarter", _rule_quarter),
    ("single_month", _rule_single_month),
    ("relative_units", _rule_relative_units),
    ("named_relative", _rule_named_relative),
)

_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def resolve_period(message: str, *, today: date | None = None) -> ResolvedPeriod | None:
    """La ventana que el mensaje nombra, o None si no nombra ninguna reconocible.

    Devolver None NO es un fallo: es la respuesta correcta cuando el texto no
    contiene una expresion de la gramatica cerrada. El sistema sigue entonces
    como hasta ahora — sin filtro y declarando su alcance.
    """
    text = fold(message)
    if not text.strip():
        return None
    # Si el usuario ya escribio fechas ISO, el camino de siempre las deja pasar
    # y no hay nada que derivar. Derivar encima podria contradecirle.
    if _ISO_RE.search(text):
        return None
    anchor = today or date.today()
    for rule_name, rule in _RULES:
        found = rule(text, anchor)
        if found is AMBIGUOUS:
            return None
        if not found:
            continue
        desde, hasta, expression = found
        if desde > hasta:
            return None
        if (hasta - desde).days + 1 > MAX_WINDOW_DAYS:
            # Mas larga de lo que el ERP acepta. Se aborta en vez de seguir
            # probando: el usuario nombro ESA ventana, y devolver otra mas
            # estrecha porque otra regla la reconozca seria contestar otra cosa.
            return None
        return ResolvedPeriod(desde=desde, hasta=hasta,
                              expression=expression.strip(), rule=rule_name)
    return None
