"""FASE 8.8 — cruce OEM ↔ código interno ↔ alternativo, y aplicaciones.

DÓNDE ESTÁN LOS DATOS (medido antes de escribir nada)
-----------------------------------------------------
Las tablas relacionales que parecían el sitio natural están VACÍAS: ``oems`` 0,
``compatibilidades`` 0, ``vehiculos`` 0, ``motores`` 0, ``modelos_maestros`` 0.
Construir sobre ellas habría repetido el error de 8.6, donde se levantó una
capacidad sobre datos que no existían.

Los datos viven desnormalizados en ``productos`` (28 082 filas) y están llenos:

    CODIGO OEM                      20 293  (72%)
    MODELO / MARCA                  ~28 070 (99%)
    MOTOR                           26 544  (94%)
    HOMOLOGADOS                      5 778  (20%)
    CODIGO ALTERNATIVO O ANTIGUO     3 877  (13%)

Y ninguna tool los expone: search_catalog devuelve 4 campos y get_product 9;
ninguno incluye OEM ni homologados.

POR QUÉ SE COMPARA NORMALIZADO
------------------------------
Los códigos OEM llevan separadores que nadie teclea igual. Medido en la base:
``0 280 751 089`` (Bosch) es UN código con espacios, y ``038-1701225`` lleva
guión. Un usuario escribe ``0280751089``. Con igualdad literal, el 72% de los
datos sería inalcanzable por quien escriba el código de forma natural.

La normalización quita todo lo que no sea alfanumérico y pasa a mayúsculas, en
AMBOS lados de la comparación.

Cuidado medido: ``CODIGO OEM`` puede traer varios códigos separados por " / "
(254 filas), pero NO por espacio: ``0 280 751 089`` es uno solo. Partir por
espacios rompería 973 filas.

HOMOLOGADOS NO SON CÓDIGOS
--------------------------
Es el error fácil de este dominio. ``HOMOLOGADOS`` contiene APLICACIONES de
vehículo —``DFM K07 1.3,GAC GONOW 1.3,CM10``—, no códigos equivalentes.
Mezclarlos haría que el agente ofreciera "equivalencias" que en realidad son
modelos de coche. Viajan en su propio campo y con su propio nombre.
"""
from __future__ import annotations

import re
from typing import Any

from app.internal_agent.m2m import FORBIDDEN_BODY_KEYS, InternalAuthError

SQL_VALUE_RE = re.compile(
    r"\b(SELECT|INSERT|UPDATE|DELETE|DROP|UNION|ALTER|EXEC|MERGE|TRUNCATE)\b",
    re.IGNORECASE,
)
_NON_ALNUM_RE = re.compile(r"[^A-Z0-9]")

MIN_LIMIT = 1
MAX_LIMIT = 20
DEFAULT_LIMIT = 10
# Una clave demasiado corta empareja con medio catálogo y el resultado deja de
# ser una equivalencia para ser ruido.
MIN_KEY_LEN = 4
MAX_TERM_LEN = 64
MAX_APPLICATIONS = 12

# Separadores que de verdad separan códigos en esta base. El espacio NO está:
# forma parte de códigos Bosch como "0 280 751 089".
MULTI_SEPARATORS = ("/", ",", ";")
# Separadores que se eliminan al normalizar para comparar.
_SQL_STRIP_CHARS = (" ", "-", ".", "/", "_", ",")

OEM_COLUMN = "CODIGO OEM"
ALT_COLUMN = "CODIGO ALTERNATIVO O ANTIGUO"
APP_COLUMN = "HOMOLOGADOS"


def normalize_code(value: Any) -> str:
    """Clave de comparación: sólo alfanuméricos, en mayúsculas."""
    return _NON_ALNUM_RE.sub("", str(value or "").upper())


def split_codes(raw: Any) -> list[str]:
    """Separa un campo multivalor SIN partir por espacios."""
    text = str(raw or "").strip()
    if not text:
        return []
    parts = [text]
    for sep in MULTI_SEPARATORS:
        parts = [chunk for part in parts for chunk in part.split(sep)]
    return [p.strip() for p in parts if p.strip()]


def split_applications(raw: Any) -> list[str]:
    """HOMOLOGADOS es una lista de aplicaciones separada por coma."""
    text = str(raw or "").strip()
    if not text:
        return []
    return [p.strip() for p in text.split(",") if p.strip()][:MAX_APPLICATIONS]


def _bad(field: str, message: str) -> InternalAuthError:
    return InternalAuthError("invalid_args", f"{field}: {message}", status=400)


def _opt_term(payload: dict[str, Any], key: str) -> str | None:
    raw = payload.get(key)
    if raw in (None, ""):
        return None
    text = str(raw).strip()
    if not text:
        return None
    if len(text) > MAX_TERM_LEN:
        raise _bad(key, f"maximo {MAX_TERM_LEN} caracteres")
    if SQL_VALUE_RE.search(text):
        raise _bad(key, "valor no permitido")
    return text


def validate_equivalence_args(payload: Any) -> dict[str, Any]:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise _bad("body", "se esperaba un objeto JSON")
    for forbidden in FORBIDDEN_BODY_KEYS:
        if forbidden in payload:
            raise _bad(forbidden, "campo no permitido")

    oem = _opt_term(payload, "oem")
    codigo = _opt_term(payload, "codigo")
    marca = _opt_term(payload, "marca")
    modelo = _opt_term(payload, "modelo")

    if not oem and not codigo:
        # Sin ancla, la consulta devolveria un recorte arbitrario del catalogo y
        # el agente lo presentaria como "equivalencias", que seria falso.
        raise _bad("oem", "se requiere 'oem' o 'codigo'")
    for name, value in (("oem", oem), ("codigo", codigo)):
        if value and len(normalize_code(value)) < MIN_KEY_LEN:
            raise _bad(name, f"minimo {MIN_KEY_LEN} caracteres alfanumericos")

    try:
        limit = int(payload.get("limit") or DEFAULT_LIMIT)
    except (TypeError, ValueError) as exc:
        raise _bad("limit", "entero requerido") from exc

    return {
        "oem": oem,
        "codigo": codigo.upper() if codigo else None,
        "marca": marca,
        "modelo": modelo,
        "limit": max(MIN_LIMIT, min(MAX_LIMIT, limit)),
    }


def _sql_normalized(column: str) -> str:
    """Expresión SQL que normaliza la columna igual que normalize_code()."""
    expr = f'UPPER("{column}")'
    for ch in _SQL_STRIP_CHARS:
        expr = f"REPLACE({expr}, '{ch}', '')"
    return expr


def _row(record: Any) -> dict[str, Any]:
    oem_codes = split_codes(record.oem)
    alt_codes = split_codes(record.alternativo)
    item: dict[str, Any] = {
        "codigo": str(record.codigo or "").strip().upper(),
        "descripcion": str(record.descripcion or "").strip(),
        "marca": str(record.marca or "").strip(),
        "modelo": str(record.modelo or "").strip(),
        "motor": str(record.motor or "").strip(),
        "oem": oem_codes,
        "alternativos": alt_codes,
        # Nombre propio a proposito: NO son codigos equivalentes, son modelos de
        # vehiculo. Llamarlos "equivalencias" haria que el agente ofreciera un
        # coche donde el usuario espera una pieza.
        "aplicaciones": split_applications(record.aplicaciones),
    }
    return item


def get_public_equivalences(
    *,
    oem: str | None = None,
    codigo: str | None = None,
    marca: str | None = None,
    modelo: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[dict[str, Any], bool]:
    """Códigos internos que corresponden a un OEM, o equivalentes de un código.

    Dos modos y una regla común: se ancla en un identificador, nunca se devuelve
    un recorte del catálogo.

    - ``oem``: busca ese código en CODIGO OEM y en CODIGO ALTERNATIVO.
    - ``codigo``: toma los OEM de ese producto y busca QUIÉN MÁS los declara, que
      es la pregunta "¿qué alternativas tengo?".
    """
    from sqlalchemy import text

    from app.extensions import db

    limit = max(MIN_LIMIT, min(MAX_LIMIT, int(limit or DEFAULT_LIMIT)))
    oem_expr = _sql_normalized(OEM_COLUMN)
    alt_expr = _sql_normalized(ALT_COLUMN)
    select = (
        f'SELECT CODIGO AS codigo, DESCRIPCION AS descripcion, MARCA AS marca, '
        f'MODELO AS modelo, MOTOR AS motor, "{OEM_COLUMN}" AS oem, '
        f'"{ALT_COLUMN}" AS alternativo, "{APP_COLUMN}" AS aplicaciones '
        f"FROM productos"
    )
    params: dict[str, Any] = {}
    where: list[str] = []
    matched_on = None

    if oem:
        key = normalize_code(oem)
        params["key"] = key
        params["like"] = f"%{key}%"
        where.append(f"({oem_expr} = :key OR {oem_expr} LIKE :like "
                     f"OR {alt_expr} = :key OR {alt_expr} LIKE :like)")
        matched_on = "oem"
    elif codigo:
        seed = db.session.execute(
            text(f'SELECT "{OEM_COLUMN}" AS oem, "{ALT_COLUMN}" AS alternativo '
                 f"FROM productos WHERE UPPER(TRIM(CODIGO)) = :c LIMIT 1"),
            {"c": codigo},
        ).fetchone()
        if seed is None:
            return {"query": {"codigo": codigo}, "items": [], "count": 0,
                    "matched_on": "codigo", "not_found": True}, False
        keys = {normalize_code(c) for c in
                split_codes(seed.oem) + split_codes(seed.alternativo)}
        keys = {k for k in keys if len(k) >= MIN_KEY_LEN}
        if not keys:
            # El producto existe pero no declara OEM: no hay equivalencia que
            # ofrecer, y eso es un hecho, no un fallo.
            return {"query": {"codigo": codigo}, "items": [], "count": 0,
                    "matched_on": "codigo", "no_oem_declared": True}, False
        clauses = []
        for i, key in enumerate(sorted(keys)[:8]):
            params[f"k{i}"] = key
            clauses.append(f"({oem_expr} = :k{i} OR {alt_expr} = :k{i})")
        where.append("(" + " OR ".join(clauses) + ")")
        matched_on = "codigo"

    if marca:
        params["marca"] = f"%{marca}%"
        where.append("UPPER(MARCA) LIKE UPPER(:marca)")
    if modelo:
        params["modelo"] = f"%{modelo}%"
        where.append("UPPER(MODELO) LIKE UPPER(:modelo)")

    sql = f"{select} WHERE {' AND '.join(where)} ORDER BY CODIGO LIMIT :lim"
    params["lim"] = limit + 1
    rows = db.session.execute(text(sql), params).fetchall()
    truncated = len(rows) > limit
    items = [_row(r) for r in rows[:limit]]

    data: dict[str, Any] = {
        "query": {k: v for k, v in (("oem", oem), ("codigo", codigo),
                                    ("marca", marca), ("modelo", modelo)) if v},
        "matched_on": matched_on,
        "items": items,
        "count": len(items),
    }
    # FASE 8.x — la rama `codigo` declaraba `not_found` y la rama `oem` no, asi
    # que un OEM inexistente volvia como una lista vacia indistinguible de "no
    # busque nada". Medido contra el ERP real con ZZZNOEXISTE999: el payload era
    # {items:[], count:0} y el composer caia a su rama generica. "Ese OEM no
    # existe en el catalogo" es un HECHO que el sistema conoce, y un hecho que
    # no se declara obliga al modelo a inferirlo o a callarse.
    #
    # Solo cuando el vacio es atribuible al ancla: con marca o modelo tambien
    # filtrando, el OEM podria existir y no estar en esa marca, y afirmar que no
    # existe seria falso.
    if matched_on == "oem" and not items and not marca and not modelo:
        data["not_found"] = True
    return data, truncated
