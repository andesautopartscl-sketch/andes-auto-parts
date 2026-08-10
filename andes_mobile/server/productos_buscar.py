"""Búsqueda multi-campo de productos para la PWA móvil (solo lectura sobre Producto)."""
from __future__ import annotations

import logging
import re
import unicodedata

from sqlalchemy import func, or_

from app.bodega.models import ProductoVarianteStock
from app.extensions import db
from app.models import Producto, ProductoImagen
from app.utils.format_currency_cl import format_precio_publico_con_iva
from app.utils.product_image_url import product_image_src

logger = logging.getLogger(__name__)

_SEARCH_COLUMNS = (
    Producto.codigo,
    Producto.descripcion,
    Producto.modelo,
    Producto.motor,
    Producto.marca,
    Producto.codigo_oem,
    Producto.codigo_alternativo,
    Producto.homologados,
    Producto.medidas,
    Producto.anio,
    Producto.version,
)

# Columnas de código: se buscan por subcadena, fuera del alcance de FTS5.
_CODE_COLUMNS = (
    Producto.codigo,
    Producto.codigo_oem,
    Producto.codigo_alternativo,
    Producto.homologados,
)

_FIELD_GETTERS: tuple[tuple[str, str], ...] = (
    ("codigo", "codigo"),
    ("descripcion", "descripcion"),
    ("modelo", "modelo"),
    ("motor", "motor"),
    ("marca", "marca"),
    ("codigo_oem", "codigo_oem"),
    ("codigo_alternativo", "codigo_alternativo"),
    ("homologados", "homologados"),
    ("medidas", "medidas"),
    ("anio", "anio"),
    ("version", "version"),
)

_MATCH_LABELS = {
    "codigo": "Código",
    "codigo_oem": "OEM",
    "codigo_alternativo": "Alternativo",
    "homologados": "Homologado",
    "descripcion": "Descripción",
    "medidas": "Medidas",
    "motor": "Motor",
    "modelo": "Modelo",
    "marca": "Marca",
    "anio": "Año",
    "version": "Versión",
}


def normalize_text(value: str | None) -> str:
    s = (value or "").strip().lower()
    if not s:
        return ""
    decomposed = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def split_terms(query: str) -> list[str]:
    norm = normalize_text(query)
    if not norm:
        return []
    return [t for t in re.split(r"\s+", norm) if t]


def product_search_blob(producto: Producto) -> str:
    parts: list[str] = []
    for _, attr in _FIELD_GETTERS:
        val = getattr(producto, attr, None)
        if val is not None and str(val).strip():
            parts.append(str(val))
    return normalize_text(" ".join(parts))


def _term_sql_pattern(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _sql_candidates(terms: list[str], fetch_limit: int) -> list[Producto]:
    """Ruta de respaldo: LIKE multi-columna. Recorre la tabla entera, se usa solo si FTS5 falla."""
    if not terms:
        return []
    q = db.session.query(Producto).filter(Producto.activo.is_(True))
    for term in terms:
        pattern = _term_sql_pattern(term)
        q = q.filter(
            or_(
                *[
                    func.coalesce(col, "").ilike(pattern, escape="\\")
                    for col in _SEARCH_COLUMNS
                ]
            )
        )
    return q.order_by(Producto.codigo.asc()).limit(fetch_limit).all()


def _code_candidates(terms: list[str], fetch_limit: int) -> list[Producto]:
    """
    Coincidencias dentro de códigos: interno, OEM, alternativo y homologados.

    FTS5 no las cubre porque indexa palabras completas con prefijo, y estos códigos
    se buscan por subcadena ("180" dentro de "MB891180"). Son las coincidencias de
    máxima prioridad del ranking, así que se resuelven aparte y siempre.
    """
    if not terms:
        return []
    try:
        from app.utils.fts_productos import fts_codes_search

        codigos = fts_codes_search(db.session, terms, limit=fetch_limit)
    except Exception:
        logger.exception("Búsqueda móvil: índice trigram no disponible, se usa LIKE")
        codigos = None

    if codigos is not None:
        if not codigos:
            return []
        productos = (
            db.session.query(Producto)
            .filter(Producto.activo.is_(True))
            .filter(Producto.codigo.in_(codigos))
            .order_by(Producto.codigo.asc())
            .all()
        )
        return productos

    q = db.session.query(Producto).filter(Producto.activo.is_(True))
    for term in terms:
        pattern = _term_sql_pattern(term)
        q = q.filter(
            or_(
                *[
                    func.coalesce(col, "").ilike(pattern, escape="\\")
                    for col in _CODE_COLUMNS
                ]
            )
        )
    return q.order_by(Producto.codigo.asc()).limit(fetch_limit).all()


def _fts_candidates(terms: list[str], fetch_limit: int) -> list[Producto] | None:
    """
    Candidatos vía índice FTS5, ordenados por relevancia.

    Devuelve None (no lista vacía) si el índice no está disponible, para poder
    distinguir "sin resultados" de "FTS no utilizable" y solo entonces degradar a LIKE.
    """
    if not terms:
        return []
    try:
        from app.utils.fts_productos import fts_match_query, fts_search_codes

        match = fts_match_query(terms)
        if not match.strip():
            return None
        codigos = fts_search_codes(db.session, match, limit=fetch_limit)
    except Exception:
        logger.exception("Búsqueda móvil: FTS5 no disponible, se usa LIKE (más lento)")
        return None

    if not codigos:
        return []
    productos = (
        db.session.query(Producto)
        .filter(Producto.activo.is_(True))
        .filter(Producto.codigo.in_(codigos))
        .all()
    )
    # Preservar el orden de relevancia que devolvió FTS5.
    posicion = {c: i for i, c in enumerate(codigos)}
    productos.sort(key=lambda p: posicion.get(p.codigo, len(posicion)))
    return productos


def _field_values(producto: Producto) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, attr in _FIELD_GETTERS:
        out[key] = normalize_text(str(getattr(producto, attr, None) or ""))
    return out


def _term_in_field(term: str, field_key: str, fields: dict[str, str]) -> bool:
    val = fields.get(field_key) or ""
    if not term or not val:
        return False
    if term in val:
        return True
    if field_key == "homologados":
        tokens = re.split(r"[^a-z0-9]+", val)
        return term in {t for t in tokens if t}
    return False


def _score_match(producto: Producto, terms: list[str], raw_query: str) -> tuple[int, str] | None:
    fields = _field_values(producto)
    blob = " ".join(v for v in fields.values() if v)
    if not all(term in blob for term in terms):
        return None

    codigo = fields.get("codigo") or ""
    raw_norm = normalize_text(raw_query).replace(" ", "")
    query_compact = raw_norm

    if codigo and (codigo == query_compact or codigo == normalize_text(raw_query)):
        return 0, _MATCH_LABELS["codigo"]
    for term in terms:
        if codigo == term:
            return 0, _MATCH_LABELS["codigo"]

    for term in terms:
        if _term_in_field(term, "codigo_oem", fields):
            return 1, _MATCH_LABELS["codigo_oem"]
        if _term_in_field(term, "codigo_alternativo", fields):
            return 1, _MATCH_LABELS["codigo_alternativo"]
        if _term_in_field(term, "homologados", fields):
            return 1, _MATCH_LABELS["homologados"]

    desc = fields.get("descripcion") or ""
    first_term = terms[0] if terms else ""
    if desc and first_term and desc.startswith(first_term):
        return 2, _MATCH_LABELS["descripcion"]

    priority_fields = (
        "medidas",
        "motor",
        "modelo",
        "marca",
        "anio",
        "version",
        "descripcion",
    )
    for term in terms:
        for fk in priority_fields:
            if _term_in_field(term, fk, fields):
                return 3, _MATCH_LABELS[fk]

    return 3, _MATCH_LABELS["descripcion"]


_THUMB_IN_CHUNK = 400
_THUMB_SCAN_THRESHOLD = 2000


def _imagen_sort_key(orden, es_principal, img_id) -> tuple[int, int]:
    """Mismo criterio de portada que la galería del ERP: orden asc, id como desempate."""
    try:
        pos = int(orden) if orden is not None else None
    except (TypeError, ValueError):
        pos = None
    if pos is None:
        pos = 0 if es_principal else 999
    return (pos, int(img_id or 0))


def build_thumb_map(productos: list[Producto]) -> dict[str, str]:
    """
    Resuelve la miniatura de muchos productos con una sola consulta agrupada.

    No consulta Cloudinary ni el disco a propósito: en un listado eso significaba
    una llamada de red por fila (50 resultados = ~90 peticiones HTTPS).
    Devuelve {CODIGO: ruta} solo para los productos sin `imagen_url` propia.
    """
    pendientes = [p for p in productos if not (p.imagen_url or "").strip()]
    if not pendientes:
        return {}

    codigos = sorted({(p.codigo or "").strip().upper() for p in pendientes if p.codigo})
    oems = sorted(
        {(p.codigo_oem or "").strip().upper() for p in pendientes if (p.codigo_oem or "").strip()}
    )
    if not codigos and not oems:
        return {}

    col_codigo = func.upper(func.trim(ProductoImagen.producto_codigo))
    col_oem = func.upper(func.trim(func.coalesce(Producto.codigo_oem, "")))
    base_q = (
        db.session.query(
            col_codigo,
            col_oem,
            ProductoImagen.ruta,
            ProductoImagen.orden,
            ProductoImagen.es_principal,
            ProductoImagen.id,
        )
        .join(Producto, ProductoImagen.producto_codigo == Producto.codigo)
        .filter(Producto.activo.is_(True))
        .filter(ProductoImagen.ruta.isnot(None))
        .filter(ProductoImagen.ruta != "")
    )

    # Con muchos productos (sync de catálogo completo) una sola pasada por la tabla
    # de imágenes sale más barata que cientos de consultas con IN.
    if len(codigos) > _THUMB_SCAN_THRESHOLD:
        filas = base_q.all()
    else:
        filas = []
        for i in range(0, max(len(codigos), len(oems)), _THUMB_IN_CHUNK):
            lote_cod = codigos[i : i + _THUMB_IN_CHUNK]
            lote_oem = oems[i : i + _THUMB_IN_CHUNK]
            condiciones = []
            if lote_cod:
                condiciones.append(col_codigo.in_(lote_cod))
            if lote_oem:
                condiciones.append(col_oem.in_(lote_oem))
            if condiciones:
                filas.extend(base_q.filter(or_(*condiciones)).all())

    mejor_por_codigo: dict[str, tuple[tuple[int, int], str]] = {}
    mejor_por_oem: dict[str, tuple[tuple[int, int], str]] = {}
    for codigo, oem, ruta, orden, es_principal, img_id in filas:
        ref = (ruta or "").strip()
        if not ref:
            continue
        clave = _imagen_sort_key(orden, es_principal, img_id)
        if codigo:
            actual = mejor_por_codigo.get(codigo)
            if actual is None or clave < actual[0]:
                mejor_por_codigo[codigo] = (clave, ref)
        if oem:
            actual = mejor_por_oem.get(oem)
            if actual is None or clave < actual[0]:
                mejor_por_oem[oem] = (clave, ref)

    resuelto: dict[str, str] = {}
    for producto in pendientes:
        codigo = (producto.codigo or "").strip().upper()
        if not codigo:
            continue
        # El OEM manda: agrupa las imágenes compartidas entre productos homologados.
        oem = (producto.codigo_oem or "").strip().upper()
        elegido = mejor_por_oem.get(oem) if oem else None
        elegido = elegido or mejor_por_codigo.get(codigo)
        if elegido:
            resuelto[codigo] = elegido[1]
    return resuelto


def _thumb_url(producto: Producto, thumb_map: dict[str, str] | None = None) -> str | None:
    ref = (producto.imagen_url or "").strip()
    if not ref and thumb_map is not None:
        ref = thumb_map.get((producto.codigo or "").strip().upper(), "")
    if not ref:
        return None
    return product_image_src(ref) or None


def _meta_line(producto: Producto) -> str:
    parts: list[str] = []
    for val in (producto.marca, producto.modelo, producto.anio):
        v = (val or "").strip()
        if v and v not in parts:
            parts.append(v)
    return " · ".join(parts)


def _stock_total(codigo: str, stock_map: dict[str, int] | None = None) -> int:
    if stock_map is not None:
        return int(stock_map.get(codigo, 0))
    rows = ProductoVarianteStock.query.filter(
        func.upper(ProductoVarianteStock.codigo_producto) == codigo
    ).all()
    return sum(int(r.stock or 0) for r in rows)


def _serialize_row(
    producto: Producto,
    *,
    rank: int,
    match_en: str,
    puede_ver_precio: bool,
    stock_map: dict[str, int] | None = None,
    thumb_map: dict[str, str] | None = None,
) -> dict:
    codigo = (producto.codigo or "").strip().upper()
    precio = float(producto.p_publico or 0)
    stock = _stock_total(codigo, stock_map)
    thumb = _thumb_url(producto, thumb_map)
    return {
        "codigo": codigo,
        "descripcion": (producto.descripcion or "").strip(),
        "marca": (producto.marca or "").strip(),
        "modelo": (producto.modelo or "").strip(),
        "anio": (producto.anio or "").strip(),
        "meta_linea": _meta_line(producto),
        "precio": precio if puede_ver_precio else None,
        "precio_fmt": format_precio_publico_con_iva(precio) if puede_ver_precio and precio > 0 else "—",
        "stock": stock,
        "imagen": thumb,
        "match_en": match_en,
        "_rank": rank,
    }


def _score_candidates(
    candidates: list[Producto], terms: list[str], raw: str
) -> list[tuple[int, str, Producto, str]]:
    """Filtra y puntúa candidatos: (rank, codigo, producto, etiqueta del campo que coincidió)."""
    scored: list[tuple[int, str, Producto, str]] = []
    for producto in candidates:
        match = _score_match(producto, terms, raw)
        if match is None:
            continue
        rank, label = match
        scored.append((rank, (producto.codigo or "").upper(), producto, label))
    return scored


def buscar(query: str, *, puede_ver_precio: bool = True, limit: int = 50) -> list[dict]:
    raw = (query or "").strip()
    if len(raw) < 2:
        return []
    terms = split_terms(raw)
    if not terms:
        return []

    limit = max(1, min(int(limit or 50), 50))
    # Dos caminos complementarios: FTS5 resuelve el texto libre (rápido y ordenado
    # por relevancia) y el LIKE sobre códigos recupera las subcadenas que FTS5 no
    # puede ver. Juntos cubren lo mismo que el escaneo completo anterior.
    fts = _fts_candidates(terms, fetch_limit=limit * 6)
    if fts is None:
        candidates = _sql_candidates(terms, fetch_limit=limit * 40)
    else:
        candidates = _code_candidates(terms, fetch_limit=limit * 40)
        vistos = {p.codigo for p in candidates}
        candidates.extend(p for p in fts if p.codigo not in vistos)

    scored = _score_candidates(candidates, terms, raw)
    scored.sort(key=lambda x: (x[0], x[1]))
    top = scored[:limit]

    codigos = [(p.codigo or "").strip().upper() for _, _, p, _ in top if p.codigo]
    from app.utils.stock_control import get_stock_totals_map

    stock_map = get_stock_totals_map(codigos)
    thumb_map = build_thumb_map([p for _, _, p, _ in top])

    out: list[dict] = []
    for rank, _, producto, label in top:
        row = _serialize_row(
            producto,
            rank=rank,
            match_en=label,
            puede_ver_precio=puede_ver_precio,
            stock_map=stock_map,
            thumb_map=thumb_map,
        )
        row.pop("_rank", None)
        out.append(row)
    return out


def catalogo_item(
    producto: Producto,
    stock_map: dict[str, list[dict]],
    puede_ver_precio: bool = True,
    thumb_map: dict[str, str] | None = None,
) -> dict | None:
    codigo = (producto.codigo or "").strip().upper()
    if not codigo:
        return None
    bodegas = stock_map.get(codigo, [])
    stock_total = sum(int(b.get("stock") or 0) for b in bodegas)
    precio = float(producto.p_publico or 0)
    thumb = _thumb_url(producto, thumb_map)
    return {
        "codigo": codigo,
        "descripcion": (producto.descripcion or "").strip(),
        "marca": (producto.marca or "").strip(),
        "modelo": (producto.modelo or "").strip(),
        "motor": (producto.motor or "").strip(),
        "anio": (producto.anio or "").strip(),
        "version": (producto.version or "").strip(),
        "codigo_oem": (producto.codigo_oem or "").strip(),
        "codigo_alternativo": (producto.codigo_alternativo or "").strip(),
        "homologados": (producto.homologados or "").strip(),
        "medidas": (producto.medidas or "").strip(),
        "search_text": product_search_blob(producto),
        "precio": precio,
        "precio_fmt": format_precio_publico_con_iva(precio) if precio > 0 else "—",
        "stock": stock_total,
        "bodegas": bodegas,
        "imagen": thumb,
        "meta_linea": _meta_line(producto),
    }
