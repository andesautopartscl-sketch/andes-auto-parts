"""
Heurísticas para sugerir categoría/subcategoría a partir del texto del producto.
Las semillas en BD suelen tener palabras_clave vacías; aquí se complementan con
sinónimos y términos típicos de repuestos (Chile / español).
"""
from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models import Producto, Subcategoria

_RE_SPLIT = re.compile(r"[,;\n/|]+|\s+y\s+", re.IGNORECASE)


def _sin(s: str) -> str:
    s = (s or "").lower()
    nk = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nk if unicodedata.category(c) != "Mn")


# Clave: nombre de subcategoría normalizado (_sin, espacios colapsados)
_SUBCATEGORIA_SINONIMOS: dict[str, tuple[str, ...]] = {
    "bujias e incandescentes": (
        "bujia",
        "bujias",
        "candela",
        "candelas",
        "incandescente",
        "incandescentes",
        "spark",
        "precalentamiento",
    ),
    "filtros": (
        "filtro",
        "filtros",
        "aceite",
        "aire",
        "combustible",
        "habitaculo",
        "polen",
        "gasoil",
        "diesel",
        "cartucho",
    ),
    "correas y tensores": (
        "correa",
        "correas",
        "tensor",
        "tensores",
        "distribucion",
        "poly",
        "polyv",
        "alternador",
        "accesorios correa",
        "kit distribucion",
    ),
    "embrague": (
        "embrague",
        "clutch",
        "collarin",
        "volante motor",
        "disco embrague",
    ),
    "encendido": (
        "bobina",
        "bobinas",
        "distribuidor",
        "modulo",
        "delco",
        "encendido",
        "inmovilizador",
    ),
    "bateria": ("bateria", "baterias", "acumulador", "arranque bateria"),
    "iluminacion": (
        "faro",
        "faros",
        "foco",
        "focos",
        "lampara",
        "led",
        "xenon",
        "estacionamiento",
        "direccional",
        "intermitente",
    ),
    "sensores": (
        "sensor",
        "sensores",
        "sonda",
        "lambda",
        "oxigeno",
        "map",
        "maf",
        "abs",
        "ckp",
        "cmp",
    ),
    "pastillas": (
        "pastilla",
        "pastillas",
        "balata",
        "balatas",
        "pastilla freno",
    ),
    "discos": (
        "disco freno",
        "disco de freno",
        "disco ventilado",
        "disco solido",
        "tambor freno",
    ),
    "liquido y accesorios": (
        "liquido freno",
        "dot",
        "cilindro",
        "cilindro freno",
        "bendix",
        "bomba freno",
    ),
    "amortiguadores": (
        "amortiguador",
        "amortiguadores",
        "shock",
        "gabriel",
        "monroe",
        "kyb",
        "sachs",
    ),
    "rotulas": (
        "rotula",
        "rotulas",
        "meseta",
        "rotula suspension",
    ),
    "terminales": (
        "terminal",
        "terminales",
        "barra direccion",
        "cremallera",
        "axial",
        "punta eje",
    ),
    "espejos": ("espejo", "espejos", "retrovisor", "retrovisores"),
    "paragolpes": (
        "paragolpe",
        "paragolpes",
        "parachoques",
        "bumper",
        "defensa",
    ),
    "accesorios": (
        "moldura",
        "molduras",
        "emblema",
        "rejilla",
        "spoiler",
        "estribo",
    ),
}


def _plural_singular_variants(token: str) -> list[str]:
    t = token.strip().lower()
    if len(t) < 3:
        return []
    out = {t}
    if len(t) > 3 and t.endswith("es") and t[-3] not in "aeiou":
        out.add(t[:-2])
    if t.endswith("s") and not t.endswith("ss"):
        out.add(t[:-1])
    if not t.endswith("s") and len(t) >= 4:
        out.add(t + "s")
    return [x for x in out if len(x) >= 3]


def _tokens_from_phrase(phrase: str) -> list[str]:
    """Palabras significativas de un nombre de categoría/subcategoría."""
    raw = _sin(phrase or "")
    parts = re.split(r"[^\w]+", raw)
    tokens: list[str] = []
    for p in parts:
        if len(p) < 3:
            continue
        for v in _plural_singular_variants(p):
            tokens.append(v)
    return tokens


def _keyword_parts(palabras_clave: str | None) -> list[str]:
    out: list[str] = []
    for part in _RE_SPLIT.split(palabras_clave or ""):
        p = _sin(part.strip())
        if len(p) >= 2:
            out.append(p)
    return out


def synonyms_for_subcategoria(sub: "Subcategoria") -> list[str]:
    """Lista de cadenas normalizadas (_sin) para comparar contra el texto del producto."""
    seen: set[str] = set()
    ordered: list[str] = []

    def add(s: str) -> None:
        s = _sin(s).strip()
        if len(s) < 2 or s in seen:
            return
        seen.add(s)
        ordered.append(s)

    nombre_key = _sin((sub.nombre or "").strip())
    nombre_key = " ".join(nombre_key.split())
    for syn in _SUBCATEGORIA_SINONIMOS.get(nombre_key, ()):
        add(syn)
    for t in _tokens_from_phrase(sub.nombre or ""):
        add(t)
    for p in _keyword_parts(getattr(sub, "palabras_clave", None)):
        add(p)
    return ordered


def score_subcategoria_against_hay(sub: "Subcategoria", hay: str) -> int:
    """
    hay: texto ya normalizado con _sin (minúsculas, sin tildes).
    Mayor puntaje = mejor coincidencia.
    """
    if not hay or len(hay.strip()) < 2:
        return 0
    score = 0
    for syn in synonyms_for_subcategoria(sub):
        if len(syn) < 3:
            continue
        if syn in hay:
            score += len(syn) + 2
    return score


def min_score_to_assign() -> int:
    """Umbral mínimo para aceptar una subcategoría (evita falsos positivos débiles)."""
    return 6


def _hay_desde_producto(producto: "Producto") -> str:
    """Texto normalizado para cruzar con sinónimos (incluye OEM y homologados)."""
    return _sin(
        " ".join(
            [
                producto.descripcion or "",
                producto.marca or "",
                producto.modelo or "",
                producto.motor or "",
                producto.medidas or "",
                producto.codigo or "",
                producto.codigo_oem or "",
                producto.codigo_alternativo or "",
                producto.homologados or "",
            ]
        )
    )


def auto_asignar_categoria_si_vacio(sess, producto: "Producto", *, do_commit: bool = True) -> bool:
    """
    Si categoría y subcategoría están vacías, asigna la mejor subcategoría por heurística.
    Retorna True si hubo asignación.
    """
    from sqlalchemy.orm import joinedload

    from app.models import Subcategoria

    if producto is None:
        return False
    if producto.categoria_id is not None or producto.subcategoria_id is not None:
        return False
    hay = _hay_desde_producto(producto)
    if len(hay.strip()) < 3:
        return False
    subs = (
        sess.query(Subcategoria)
        .options(joinedload(Subcategoria.categoria))
        .all()
    )
    if not subs:
        return False
    best = None
    best_score = 0
    thresh = min_score_to_assign()
    for sub in subs:
        score = score_subcategoria_against_hay(sub, hay)
        nombre = _sin((sub.nombre or "").strip())
        if len(nombre) >= 6 and nombre in hay:
            score += len(nombre) + 2
        if score > best_score:
            best_score = score
            best = sub
    if best is None or best_score < thresh:
        return False
    producto.categoria_id = best.categoria_id
    producto.subcategoria_id = best.id
    if do_commit:
        sess.commit()
    else:
        sess.flush()
    return True


def bulk_auto_asignar_categorias_faltantes(sess, batch_flush: int = 500) -> int:
    """Asigna categoría a productos sin categoría (commits en lotes)."""
    from app.models import Producto

    n = 0
    q = (
        sess.query(Producto)
        .filter(Producto.categoria_id.is_(None))
        .filter(Producto.subcategoria_id.is_(None))
    )
    buf = 0
    for producto in q.all():
        if auto_asignar_categoria_si_vacio(sess, producto, do_commit=False):
            n += 1
            buf += 1
            if buf >= batch_flush:
                sess.commit()
                buf = 0
    if buf:
        sess.commit()
    return n
