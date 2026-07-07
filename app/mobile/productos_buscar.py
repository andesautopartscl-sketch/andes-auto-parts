"""Búsqueda multi-campo de productos para la PWA móvil (solo lectura sobre Producto)."""
from __future__ import annotations

import re
import unicodedata

from sqlalchemy import func, or_

from app.bodega.models import ProductoVarianteStock
from app.extensions import db
from app.models import Producto
from app.productos.routes import _collect_imagenes_producto
from app.utils.format_currency_cl import format_precio_publico_con_iva
from app.utils.product_image_url import product_image_src

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


def _thumb_url(producto: Producto) -> str | None:
    ref = (producto.imagen_url or "").strip()
    if ref:
        url = product_image_src(ref)
        return url or None
    try:
        imgs = _collect_imagenes_producto(producto)
        if imgs:
            url = product_image_src(imgs[0])
            return url or None
    except Exception:
        pass
    return None


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
) -> dict:
    codigo = (producto.codigo or "").strip().upper()
    precio = float(producto.p_publico or 0)
    stock = _stock_total(codigo, stock_map)
    thumb = _thumb_url(producto)
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


def buscar(query: str, *, puede_ver_precio: bool = True, limit: int = 50) -> list[dict]:
    raw = (query or "").strip()
    if len(raw) < 2:
        return []
    terms = split_terms(raw)
    if not terms:
        return []

    limit = max(1, min(int(limit or 50), 50))
    candidates = _sql_candidates(terms, fetch_limit=limit * 40)

    scored: list[tuple[int, str, Producto, str]] = []
    for producto in candidates:
        match = _score_match(producto, terms, raw)
        if match is None:
            continue
        rank, label = match
        scored.append((rank, (producto.codigo or "").upper(), producto, label))

    scored.sort(key=lambda x: (x[0], x[1]))
    top = scored[:limit]

    codigos = [(p.codigo or "").strip().upper() for _, _, p, _ in top if p.codigo]
    stock_map: dict[str, int] = {}
    if codigos:
        for row in ProductoVarianteStock.query.filter(
            func.upper(ProductoVarianteStock.codigo_producto).in_(codigos)
        ).all():
            c = (row.codigo_producto or "").strip().upper()
            stock_map[c] = stock_map.get(c, 0) + int(row.stock or 0)

    out: list[dict] = []
    for rank, _, producto, label in top:
        row = _serialize_row(
            producto,
            rank=rank,
            match_en=label,
            puede_ver_precio=puede_ver_precio,
            stock_map=stock_map,
        )
        row.pop("_rank", None)
        out.append(row)
    return out


def catalogo_item(producto: Producto, stock_map: dict[str, list[dict]], puede_ver_precio: bool = True) -> dict | None:
    codigo = (producto.codigo or "").strip().upper()
    if not codigo:
        return None
    bodegas = stock_map.get(codigo, [])
    stock_total = sum(int(b.get("stock") or 0) for b in bodegas)
    precio = float(producto.p_publico or 0)
    thumb = _thumb_url(producto)
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
