from flask import Blueprint, render_template, request, session, send_file, jsonify, make_response, url_for
from datetime import datetime, timedelta
import re
import unicodedata
import json
from pathlib import Path

from markupsafe import Markup, escape
from sqlalchemy import and_, case, func, literal, or_, text
from sqlalchemy.orm import joinedload
from werkzeug.utils import secure_filename
from ..models import (
    SessionDB,
    Producto,
    Categoria,
    Subcategoria,
    ProductoImagen,
    ProductoAuditEvent,
    ProductoAuditDiff,
    ProductoDraft,
    OemDespiece,
)
import pandas as pd
import io
import os
import tempfile
from ..extensions import db as security_db
from app.seguridad.models import Usuario
from app.bodega.models import ProductoVarianteStock
from app.bodega.models import MovimientoStock
from ..utils.decorators import login_required, admin_required
from app.utils.permissions import DEFAULT_PERMISSIONS, get_user_permissions
from ..import_excel import import_products_from_excel
from ..utils.product_audit import build_diffs, register_product_audit
from ..utils.categoria_autodetect import (
    auto_asignar_categoria_si_vacio as _auto_asignar_categoria_si_vacio,
    bulk_auto_asignar_categorias_faltantes,
)
from ..utils.product_image_postprocess import process_uploaded_image


productos_bp = Blueprint("productos", __name__)


def _wants_modal_fragment() -> bool:
    """True when UI requests a partial HTML fragment (e.g. app modal), not a full page."""
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _actor_usuario() -> str:
    return (session.get("user") or "sistema").strip() or "sistema"


def _norm_oem_despiece(s: str | None) -> str:
    return (s or "").strip().upper() or ""


def _synthetic_oem_norm_for_product_codigo(cod: str) -> str:
    """Clave OEM artificial para filas ligadas solo al código interno (única, sin chocar con OEM reales)."""
    c = (cod or "").strip().upper()
    if not c:
        return "_INT_UNKNOWN"
    return ("_INT_" + c)[:64]


def _find_oem_despiece_for_producto(db, producto: Producto) -> OemDespiece | None:
    """Prioridad: fila por producto_codigo; si no, catálogo compartido por OEM normalizado."""
    cod = (getattr(producto, "codigo", None) or "").strip().upper()
    oem = _norm_oem_despiece(getattr(producto, "codigo_oem", None))
    row = None
    if cod:
        try:
            row = db.query(OemDespiece).filter(OemDespiece.producto_codigo == cod).first()
        except Exception:
            row = None
    if row:
        return row
    if oem:
        try:
            row = db.query(OemDespiece).filter(OemDespiece.oem_norm == oem).first()
        except Exception:
            row = None
    return row


def _find_epc_despiece_archivo_en_static(producto: Producto) -> str | None:
    """
    Si hay un archivo en static/epc_despiece/ cuyo nombre coincide con OEM, código interno
    o algún alternativo (misma heurística que productos_img), devuelve solo el basename.
    Así una imagen 3770100-E06.png en disco se muestra aunque imagen_static en BD esté vacío.
    """
    static_epc = Path(__file__).resolve().parent.parent / "static" / "epc_despiece"
    try:
        nombres = os.listdir(static_epc)
    except OSError:
        return None
    archivos = sorted(
        f
        for f in nombres
        if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))
    )
    if not archivos:
        return None

    needles: list[str] = []
    o = _norm_oem_despiece(getattr(producto, "codigo_oem", None))
    if o:
        needles.append(o)
    c = (getattr(producto, "codigo", None) or "").strip()
    if c:
        needles.append(c)
    for tok in _split_codigos_alternativos(getattr(producto, "codigo_alternativo", None)):
        t = tok.strip()
        if t and t.upper() not in {x.upper() for x in needles}:
            needles.append(t)

    for needle in needles:
        for f in archivos:
            if _nombre_archivo_coincide_imagen(f.lower(), needle):
                return f
    return None


def _split_codigos_alternativos(raw: str | None) -> list[str]:
    """Tokens desde CODIGO ALTERNATIVO (separadores / , ; | salto). Orden preservado."""
    if not raw:
        return []
    out: list[str] = []
    for part in re.split(r"[/;,|\n]+", str(raw)):
        t = part.strip()
        if t:
            out.append(t)
    return out


def _nombre_archivo_coincide_imagen(nombre_archivo_lower: str, needle: str) -> bool:
    """
    Coincidencia flexible: subcadena en minúsculas (incluye 2417_1.png para needle 2417);
    si el código trae espacios, también compara versión compacta contra el nombre sin espacios.
    """
    n = (needle or "").strip().lower()
    if not n:
        return False
    if n in nombre_archivo_lower:
        return True
    compact_needle = "".join(n.split())
    if len(compact_needle) < 3:
        return False
    compact_name = nombre_archivo_lower.replace(" ", "").replace("_", "")
    return compact_needle in compact_name


def _collect_imagenes_producto_carpeta(ruta_imagenes: str, producto: Producto) -> list[str]:
    """
    Reúne imágenes que coincidan con OEM, código interno o cualquier alternativo (unión, sin duplicados).
    Orden: primero archivos ligados al OEM, luego al código interno, luego a cada alternativo
    (así no se pierden 2417.jpg / 2417_1.png cuando también hay match por OEM).
    """
    try:
        nombres = os.listdir(ruta_imagenes)
    except OSError:
        return []
    archivos = sorted(
        f
        for f in nombres
        if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))
    )

    seen: set[str] = set()
    out: list[str] = []

    def add_needle(needle: str | None) -> None:
        if not needle or not str(needle).strip():
            return
        n = str(needle).strip()
        for f in archivos:
            if f in seen:
                continue
            if _nombre_archivo_coincide_imagen(f.lower(), n):
                seen.add(f)
                out.append(f)

    add_needle((producto.codigo_oem or "").strip())
    add_needle((producto.codigo or "").strip())
    for tok in _split_codigos_alternativos(getattr(producto, "codigo_alternativo", None)):
        add_needle(tok)

    return out


def _producto_snapshot(p: Producto) -> dict:
    return {
        "codigo": p.codigo or "",
        "descripcion": p.descripcion or "",
        "marca": p.marca or "",
        "modelo": p.modelo or "",
        "motor": p.motor or "",
        "anio": p.anio or "",
        "version": p.version or "",
        "medidas": p.medidas or "",
        "p_publico": p.p_publico,
        "prec_mayor": p.prec_mayor,
        "codigo_oem": p.codigo_oem or "",
        "codigo_alternativo": p.codigo_alternativo or "",
        "homologados": p.homologados or "",
        "categoria_id": p.categoria_id,
        "subcategoria_id": p.subcategoria_id,
        "activo": bool(p.activo),
        "stock_total": (
            (p.stock_10jul or 0)
            + (p.stock_brasil or 0)
            + (p.stock_g_avenida or 0)
            + (p.stock_orientales or 0)
            + (p.stock_b20_outlet or 0)
            + (p.stock_transito or 0)
        ),
    }


def _resolve_taxonomia_create(sess, categoria_nombre: str, subcategoria_nombre: str) -> tuple[int | None, int | None]:
    categoria_nombre = (categoria_nombre or "").strip()
    subcategoria_nombre = (subcategoria_nombre or "").strip()
    categoria_id = None
    subcategoria_id = None

    if categoria_nombre:
        cat = (
            sess.query(Categoria)
            .filter(func.lower(func.trim(Categoria.nombre)) == categoria_nombre.lower())
            .first()
        )
        if not cat:
            cat = Categoria(nombre=categoria_nombre)
            sess.add(cat)
            sess.flush()
        categoria_id = cat.id

    if categoria_id and subcategoria_nombre:
        sub = (
            sess.query(Subcategoria)
            .filter(Subcategoria.categoria_id == categoria_id)
            .filter(func.lower(func.trim(Subcategoria.nombre)) == subcategoria_nombre.lower())
            .first()
        )
        if not sub:
            sub = Subcategoria(nombre=subcategoria_nombre, categoria_id=categoria_id, palabras_clave="")
            sess.add(sub)
            sess.flush()
        subcategoria_id = sub.id

    return categoria_id, subcategoria_id


def _save_uploaded_images(codigo: str, files: list) -> list[str]:
    if not codigo or not files:
        return []
    static_dir = Path("app/static/productos_img")
    static_dir.mkdir(parents=True, exist_ok=True)
    rutas: list[str] = []
    for idx, f in enumerate(files):
        if not f or not getattr(f, "filename", None):
            continue
        filename = secure_filename(f.filename)
        ext = (filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg")
        if ext not in {"jpg", "jpeg", "png", "webp"}:
            continue
        target_name = f"{codigo}.jpg" if idx == 0 else f"{codigo}_{idx + 1}.{ext}"
        target = static_dir / target_name
        f.save(str(target))
        out = process_uploaded_image(target)
        if out is not None:
            target_name = out.name
        rutas.append(f"productos_img/{target_name}")
    return rutas


def _find_producto_by_codigo(sess, codigo_raw):
    """Localiza producto por CODIGO tolerando espacios y mayúsculas."""
    normalized = (codigo_raw or "").strip().upper()
    if not normalized:
        return None
    p = sess.query(Producto).filter(Producto.codigo == normalized).first()
    if p:
        return p
    return (
        sess.query(Producto)
        .filter(func.upper(func.trim(Producto.codigo)) == normalized)
        .first()
    )


def _homologado_tokens(raw: str | None) -> list[str]:
    """
    Corta listas tipo OEM / homologados. Debe incluir '/' y '|' como separadores;
    si no, cadenas como "A / B / 2" generan tokens ['A','/','B','/','2'] y el
    '/' o un '2' suelto pueden resolver al producto con código "2" por error.
    """
    s = (raw or "").replace("\xa0", " ").replace("\u2007", " ").strip()
    if not s:
        return []
    parts = re.split(r"[\s,;/|]+", s)
    out: list[str] = []
    for t in parts:
        u = t.strip().upper()
        if not u or u in ("/", "-", "|", "."):
            continue
        out.append(u)
    return out


def _token_homologado_valido_para_buscar(token: str) -> bool:
    """Evita enlazar productos por códigos espurios (p. ej. dígito suelto entre barras)."""
    t = (token or "").strip().upper()
    if not t:
        return False
    if len(t) == 1 and t.isdigit():
        return False
    return True


def _token_parece_referencia_oem(token: str) -> bool:
    """
    Filtra tokens del campo alternativo antes de buscar en OEM / alternativo.
    Evita palabras cortas que matchean demasiadas filas (p. ej. ME, GE).
    """
    t = (token or "").strip().upper()
    if len(t) < 4:
        return False
    if any(ch.isdigit() for ch in t):
        return True
    if "-" in t:
        return True
    if len(t) >= 8:
        return True
    return False


def _forward_productos_homologados(sess, producto: Producto, exclude_codigo_upper: str) -> list:
    """
    Relaciones hacia otros productos:
    - HOMOLOGADOS: solo coincidencia exacta con CODIGO de otro ítem (listas de modelos no matchean).
    - CODIGO ALTERNATIVO / OEM: busca en catálogo por código, OEM o texto alternativo (referencias fabricante).
    """
    ex = (exclude_codigo_upper or "").strip().upper()
    by_code: dict[str, Producto] = {}

    def _add(p: Producto | None) -> None:
        if not p:
            return
        c = (p.codigo or "").strip().upper()
        if not c or c == ex:
            return
        by_code[c] = p

    if producto.homologados:
        for token in _homologado_tokens(producto.homologados):
            if not _token_homologado_valido_para_buscar(token):
                continue
            p = (
                sess.query(Producto)
                .filter(Producto.activo.is_(True))
                .filter(Producto.codigo == token)
                .first()
            )
            if p:
                _add(p)

    raw_alt = (producto.codigo_alternativo or "").strip()
    if raw_alt:
        seen_tok: set[str] = set()
        for token in _homologado_tokens(producto.codigo_alternativo):
            if not _token_homologado_valido_para_buscar(token):
                continue
            if not _token_parece_referencia_oem(token):
                continue
            if token in seen_tok:
                continue
            seen_tok.add(token)
            q = (
                sess.query(Producto)
                .filter(Producto.activo.is_(True))
                .filter(
                    or_(
                        Producto.codigo == token,
                        Producto.codigo_oem.ilike(f"%{token}%"),
                        Producto.codigo_alternativo.ilike(f"%{token}%"),
                    )
                )
            )
            for p in q.all():
                _add(p)

    return list(by_code.values())


def _merge_by_codigo(productos: list) -> list:
    by_code: dict[str, object] = {}
    for p in productos:
        if not p:
            continue
        c = (getattr(p, "codigo", None) or "").strip().upper()
        if c:
            by_code[c] = p
    return sorted(by_code.values(), key=lambda x: (getattr(x, "codigo", "") or "").upper())


def _sin_tildes(s: str) -> str:
    """Minúsculas y sin acentos para comparar descripción con palabras clave."""
    s = (s or "").lower()
    nk = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nk if unicodedata.category(c) != "Mn")


def _norm_busqueda_token(s: str) -> str:
    """Token de búsqueda normalizado (equivale a tildes/ü/ñ indistintos en el texto)."""
    return _sin_tildes((s or "").strip())


def _sql_fold_busqueda(col):
    """
    Expresión SQL (SQLite) para comparar texto sin depender de tildes ni ü/ñ.
    Debe alinearse con _norm_busqueda_token sobre el mismo string.
    """
    x = func.lower(func.coalesce(col, ""))
    for old, new in (
        ("ü", "u"),
        ("ö", "o"),
        ("ä", "a"),
        ("á", "a"),
        ("é", "e"),
        ("í", "i"),
        ("ó", "o"),
        ("ú", "u"),
        ("à", "a"),
        ("è", "e"),
        ("ì", "i"),
        ("ò", "o"),
        ("ù", "u"),
        ("ñ", "n"),
        ("ç", "c"),
    ):
        x = func.replace(x, old, new)
    return x


def _fold_like_contains(col, token_norm: str):
    """True si el campo plegado contiene token_norm (LIKE con escape para % y _)."""
    if not token_norm:
        return literal(True)
    esc = (
        token_norm.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return _sql_fold_busqueda(col).like(f"%{esc}%", escape="\\")


def _producto_busqueda_blob_expr():
    """
    Un solo texto con todos los campos buscables (espacios entre medias).
    Así SQLite hace lower+replace+LIKE una vez por fila y token, no 9 veces por columna.
    """
    c = Producto
    blob = func.coalesce(c.codigo, "")
    blob = blob + " " + func.coalesce(c.codigo_oem, "")
    blob = blob + " " + func.coalesce(c.codigo_alternativo, "")
    blob = blob + " " + func.coalesce(c.descripcion, "")
    blob = blob + " " + func.coalesce(c.modelo, "")
    blob = blob + " " + func.coalesce(c.motor, "")
    blob = blob + " " + func.coalesce(c.marca, "")
    blob = blob + " " + func.coalesce(c.medidas, "")
    blob = blob + " " + func.coalesce(c.homologados, "")
    return blob


def _reverse_products_listing_homologado(sess, code_upper: str) -> list:
    """Otros productos activos que en HOMOLOGADOS listan este código (relación inversa)."""
    code_upper = (code_upper or "").strip().upper()
    if not code_upper:
        return []
    out = []
    q = (
        sess.query(Producto)
        .filter(Producto.activo.is_(True))
        .filter(Producto.homologados.isnot(None))
        .filter(Producto.homologados != "")
    )
    for p in q.all():
        if not p or not p.codigo:
            continue
        if (p.codigo or "").strip().upper() == code_upper:
            continue
        if code_upper in _homologado_tokens(p.homologados):
            out.append(p)
    return out


def _online_users() -> list[Usuario]:
    try:
        threshold = datetime.utcnow() - timedelta(minutes=2)
        return (
            security_db.session.query(Usuario)
            .filter(Usuario.last_seen >= threshold)
            .order_by(Usuario.usuario.asc())
            .all()
        )
    except Exception:
        security_db.session.rollback()
        return []


# =========================================
# BUSCAR PRODUCTOS
# =========================================
@productos_bp.route("/buscar")
@login_required
def buscar():

    q = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int) or 1
    per_page = request.args.get("per_page", 100, type=int) or 100
    per_page = max(25, min(int(per_page), 200))

    sess = None
    online_users: list = []
    user_perms = dict(DEFAULT_PERMISSIONS)

    variant_map = {}

    def stock_total(p):
        if p is None:
            return 0
        codigo = (getattr(p, "codigo", "") or "").strip().upper()
        if codigo and codigo in variant_map and variant_map[codigo]["total"] >= 0:
            return variant_map[codigo]["total"]
        return (
            (p.stock_10jul or 0) +
            (p.stock_brasil or 0) +
            (p.stock_g_avenida or 0) +
            (p.stock_orientales or 0) +
            (p.stock_b20_outlet or 0) +
            (p.stock_transito or 0)
        )

    def highlight_match(value, term):
        text_value = "" if value is None else str(value)
        search_term = (term or "").strip()
        if not search_term:
            return escape(text_value)

        # Mismos separadores que la búsqueda (comas, ;)
        tokens = [re.escape(t) for t in re.split(r"[\s,;]+", search_term.lower()) if t]
        if not tokens:
            return escape(text_value)

        pattern = re.compile("(" + "|".join(tokens) + ")", re.IGNORECASE)
        escaped_value = escape(text_value)
        # Un solo \1: doble backslash producía el texto literal "\1" en pantalla.
        highlighted = pattern.sub(r"<mark>\1</mark>", str(escaped_value))
        return Markup(highlighted)

    try:
        sess = SessionDB()
        online_users = _online_users()
        user_perms = get_user_permissions(session.get("user"), session.get("rol"))

        query = sess.query(Producto).filter(Producto.activo.is_(True))
        productos = []

        if q:
            termino = q.lower().strip()
            # Separar por espacios, comas o ; (p. ej. "sen, pos, cig vigu 2.4")
            palabras = [p for p in re.split(r"[\s,;]+", termino) if p]

            # Cada palabra debe aparecer en al menos un campo (AND entre palabras, OR entre columnas).
            # Comparación "plegada" (sin tildes / ü / ñ): "cigueñal" encuentra "CIGÜEÑAL"; "sen" encuentra "SENSOR".
            def _token_match_any_column(palabra: str):
                p_norm = _norm_busqueda_token(palabra)
                if not p_norm:
                    return literal(True)
                return _fold_like_contains(_producto_busqueda_blob_expr(), p_norm)

            if len(palabras) == 1:
                query = query.filter(_token_match_any_column(palabras[0]))
            else:
                query = query.filter(
                    and_(*(_token_match_any_column(p) for p in palabras))
                )

            compact_term = termino.replace(" ", "")
            is_numeric = compact_term.isdigit()

            if len(palabras) > 1:
                # Orden estable: priorizar coincidencias en descripción con el primer término (texto plegado)
                p0 = _norm_busqueda_token(palabras[0])
                priority = case(
                    (_fold_like_contains(Producto.descripcion, p0), 0),
                    else_=1,
                )
                query = query.order_by(priority.asc(), Producto.codigo.asc())
            elif is_numeric:
                priority = case(
                    (Producto.codigo.ilike(f"{compact_term}%"), 0),
                    (Producto.codigo_oem.ilike(f"{compact_term}%"), 1),
                    (_fold_like_contains(Producto.descripcion, _norm_busqueda_token(termino)), 2),
                    else_=3,
                )
                query = query.order_by(priority.asc(), Producto.codigo.asc())
            else:
                nt = _norm_busqueda_token(termino)
                priority = case(
                    (_fold_like_contains(Producto.descripcion, nt), 0),
                    (_fold_like_contains(Producto.codigo_oem, nt), 1),
                    (_fold_like_contains(Producto.codigo, nt), 2),
                    else_=3,
                )
                query = query.order_by(priority.asc(), Producto.codigo.asc())
        else:
            query = query.order_by(Producto.codigo.asc())

        total_count = query.count()
        total_pages = max(1, (total_count + per_page - 1) // per_page) if total_count else 1
        page = max(1, min(page, total_pages))

        productos = (
            query.offset((page - 1) * per_page).limit(per_page).all()
        )

        productos = [p for p in productos if p is not None]
        codigos = sorted({(p.codigo or "").strip().upper() for p in productos if (p.codigo or "").strip()})
        if codigos:
            variantes = (
                security_db.session.query(ProductoVarianteStock)
                .filter(ProductoVarianteStock.codigo_producto.in_(codigos))
                .order_by(
                    ProductoVarianteStock.codigo_producto.asc(),
                    ProductoVarianteStock.marca.asc(),
                    ProductoVarianteStock.bodega.asc(),
                )
                .all()
            )
            for v in variantes:
                key = (v.codigo_producto or "").strip().upper()
                if not key:
                    continue
                bucket = variant_map.setdefault(key, {"total": 0, "items": []})
                stock_val = int(v.stock or 0)
                bucket["items"].append(
                    {
                        "marca": (v.marca or "").strip(),
                        "bodega": (v.bodega or "").strip(),
                        "stock": stock_val,
                        "proveedor": (v.proveedor or "").strip(),
                        "available": stock_val > 0,
                    }
                )
                bucket["total"] += stock_val

        for p in productos:
            if not (p.codigo or "").strip():
                print("Producto sin código detectado")

        # Auditoría de búsqueda (solo si hay término para reducir ruido).
        if q:
            try:
                register_product_audit(
                    sess,
                    actor=_actor_usuario(),
                    action="search",
                    modulo="productos",
                    req=request,
                    metadata={
                        "q": q,
                        "page": page,
                        "per_page": per_page,
                        "total_count": total_count,
                    },
                )
                sess.commit()
            except Exception:
                sess.rollback()

        return render_template(
            "buscar.html",
            productos=productos,
            q=q,
            page=page,
            per_page=per_page,
            total_count=total_count,
            total_pages=total_pages,
            session=session,
            stock_total=stock_total,
            variant_map=variant_map,
            highlight_match=highlight_match,
            online_users=online_users,
            active_page="productos_buscar",
            can_view_precio_mayor=bool(user_perms.get("ver_precio_mayor", True)),
        )
    except Exception as exc:
        print("ERROR EN BUSCAR:", exc)
        return render_template(
            "buscar.html",
            productos=[],
            q=q,
            page=1,
            per_page=100,
            total_count=0,
            total_pages=1,
            session=session,
            stock_total=stock_total,
            variant_map=variant_map,
            highlight_match=highlight_match,
            online_users=online_users,
            active_page="productos_buscar",
            can_view_precio_mayor=bool(user_perms.get("ver_precio_mayor", True)),
        )
    finally:
        if sess is not None:
            sess.close()


# =========================================
# EDITAR PRODUCTOS (ERP)
# =========================================
@productos_bp.route("/productos/editar")
@admin_required
def editar_productos_view():
    return render_template(
        "productos/editar.html",
        online_users=_online_users(),
        active_page="productos_editar",
    )


@productos_bp.route("/productos/api/taxonomia")
@login_required
def api_taxonomia_productos():
    """Listas para asignar categoría y subcategoría en edición de producto."""
    sess = SessionDB()
    try:
        categorias = (
            sess.query(Categoria).order_by(Categoria.nombre.asc()).all()
        )
        data = []
        for cat in categorias:
            subs = (
                sess.query(Subcategoria)
                .filter(Subcategoria.categoria_id == cat.id)
                .order_by(Subcategoria.nombre.asc())
                .all()
            )
            data.append(
                {
                    "id": cat.id,
                    "nombre": cat.nombre or "",
                    "subcategorias": [
                        {"id": s.id, "nombre": s.nombre or ""} for s in subs
                    ],
                }
            )
        return jsonify({"success": True, "categorias": data})
    finally:
        sess.close()


@productos_bp.route("/productos/buscar_para_editar", methods=["POST"])
@admin_required
def buscar_para_editar():
    codigo = (request.form.get("codigo") or "").strip().upper()
    if not codigo:
        return jsonify({"success": False, "error": "Codigo invalido"}), 400

    sess = SessionDB()
    try:
        producto = (
            sess.query(Producto)
            .options(
                joinedload(Producto.categoria_rel),
                joinedload(Producto.subcategoria_rel),
            )
            .filter_by(codigo=codigo)
            .filter(Producto.activo.is_(True))
            .first()
        )
        if producto is None:
            return jsonify({"success": False, "error": "No encontrado"}), 404

        stock = (
            (producto.stock_10jul or 0) +
            (producto.stock_brasil or 0) +
            (producto.stock_g_avenida or 0) +
            (producto.stock_orientales or 0) +
            (producto.stock_b20_outlet or 0) +
            (producto.stock_transito or 0)
        )
        categoria_nombre = ""
        subcategoria_nombre = ""
        try:
            if producto.categoria_rel:
                categoria_nombre = producto.categoria_rel.nombre or ""
            if producto.subcategoria_rel:
                subcategoria_nombre = producto.subcategoria_rel.nombre or ""
        except Exception:
            pass
        return jsonify({
            "success": True,
            "codigo": producto.codigo or "",
            "nombre": producto.descripcion or "",
            "marca": producto.marca or "",
            "modelo": producto.modelo or "",
            "motor": producto.motor or "",
            "anio": producto.anio or "",
            "version": producto.version or "",
            "medidas": producto.medidas or "",
            "categoria": categoria_nombre,
            "subcategoria": subcategoria_nombre,
            "categoria_id": producto.categoria_id,
            "subcategoria_id": producto.subcategoria_id,
            "precio": producto.p_publico or 0,
            "precio_mayor": producto.prec_mayor or 0,
            "stock": stock,
            "oem": producto.codigo_oem or "",
            "alternativo": producto.codigo_alternativo or "",
            "homologados": producto.homologados or "",
            "activo": bool(producto.activo),
            "estado": "ACTIVO" if producto.activo else "INACTIVO",
        })
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500
    finally:
        sess.close()


@productos_bp.route("/productos/guardar_edicion", methods=["POST"])
@admin_required
def guardar_edicion():
    codigo = (request.form.get("codigo") or "").strip().upper()
    if not codigo:
        return jsonify({"success": False, "error": "Codigo invalido"}), 400

    nombre = (request.form.get("nombre") or "").strip()
    marca = (request.form.get("marca") or "").strip()
    modelo = (request.form.get("modelo") or "").strip()
    motor = (request.form.get("motor") or "").strip()
    anio = (request.form.get("anio") or "").strip()
    version = (request.form.get("version") or "").strip()
    medidas = (request.form.get("medidas") or "").strip()
    precio_raw = (request.form.get("precio") or "").strip()
    precio_mayor_raw = (request.form.get("precio_mayor") or "").strip()
    oem = (request.form.get("oem") or "").strip()
    alternativo = (request.form.get("alternativo") or "").strip()
    homologados = (request.form.get("homologados") or "").strip()
    categoria_id_raw = (request.form.get("categoria_id") or "").strip()
    subcategoria_id_raw = (request.form.get("subcategoria_id") or "").strip()
    activo_raw = request.form.get("activo", "true").strip().lower()
    activo_val = activo_raw in {"true", "1", "on"}

    def _parse_float(raw):
        try:
            return float(raw.replace(",", ".")) if raw else None
        except ValueError:
            return None

    precio = _parse_float(precio_raw)
    if precio_raw and precio is None:
        return jsonify({"success": False, "error": "Precio invalido"}), 400
    precio_mayor = _parse_float(precio_mayor_raw)

    sess = SessionDB()
    try:
        # Query by codigo only — activo state is being saved by this request
        producto = sess.query(Producto).filter_by(codigo=codigo).first()
        if producto is None:
            return jsonify({"success": False, "error": "No encontrado"}), 404
        before = _producto_snapshot(producto)

        if nombre:      producto.descripcion = nombre
        if marca:       producto.marca = marca
        if modelo:      producto.modelo = modelo
        if motor:       producto.motor = motor
        if anio:        producto.anio = anio
        if version:     producto.version = version
        if medidas:     producto.medidas = medidas
        if precio is not None:        producto.p_publico = precio
        if precio_mayor is not None:  producto.prec_mayor = precio_mayor
        if oem:         producto.codigo_oem = oem
        if alternativo: producto.codigo_alternativo = alternativo
        producto.homologados = homologados if homologados else producto.homologados
        producto.activo = activo_val

        if categoria_id_raw.isdigit():
            cid = int(categoria_id_raw)
            existe = sess.query(Categoria).filter(Categoria.id == cid).first()
            producto.categoria_id = cid if existe else None
        else:
            producto.categoria_id = None

        if not producto.categoria_id:
            producto.subcategoria_id = None
        elif subcategoria_id_raw.isdigit():
            sid = int(subcategoria_id_raw)
            sub = sess.query(Subcategoria).filter(Subcategoria.id == sid).first()
            if sub and sub.categoria_id != producto.categoria_id:
                return jsonify(
                    {
                        "success": False,
                        "error": "La subcategoría no corresponde a la categoría seleccionada",
                    }
                ), 400
            producto.subcategoria_id = sid if sub else None
        else:
            producto.subcategoria_id = None

        after = _producto_snapshot(producto)
        diffs = build_diffs(before, after)
        if diffs:
            register_product_audit(
                sess,
                actor=_actor_usuario(),
                action="update",
                modulo="productos",
                producto_codigo=(producto.codigo or "").strip().upper(),
                req=request,
                diffs=diffs,
                metadata={"changed_fields": [d.get("campo") for d in diffs]},
            )

        sess.commit()
        try:
            sess.refresh(producto)
            _auto_asignar_categoria_si_vacio(sess, producto)
        except Exception:
            sess.rollback()
        estado_msg = "activo" if activo_val else "desactivado"
        return jsonify({"success": True, "activo": activo_val,
                        "message": f"Producto actualizado correctamente ({estado_msg})"})
    except Exception as exc:
        sess.rollback()
        return jsonify({"success": False, "error": str(exc)}), 500
    finally:
        sess.close()


@productos_bp.route("/productos/desactivar", methods=["POST"])
@admin_required
def desactivar_producto():
    codigo = (request.form.get("codigo") or "").strip().upper()
    if not codigo:
        return jsonify({"success": False, "error": "Codigo invalido"}), 400

    sess = SessionDB()
    try:
        producto = sess.query(Producto).filter_by(codigo=codigo).first()
        if producto is None:
            return jsonify({"success": False, "error": "No encontrado"}), 404
        before = _producto_snapshot(producto)
        producto.activo = False
        after = _producto_snapshot(producto)
        register_product_audit(
            sess,
            actor=_actor_usuario(),
            action="deactivate",
            modulo="productos",
            producto_codigo=(producto.codigo or "").strip().upper(),
            req=request,
            diffs=build_diffs(before, after),
        )
        sess.commit()
        return jsonify({"success": True, "message": "Producto desactivado correctamente"})
    except Exception as exc:
        sess.rollback()
        return jsonify({"success": False, "error": str(exc)}), 500
    finally:
        sess.close()


# =========================================
# VER DESACTIVADOS (PAPELERA)
# =========================================
@productos_bp.route("/productos/desactivados")
@admin_required
def ver_desactivados():
    q = (request.args.get("q") or "").strip()
    sess = SessionDB()
    try:
        query = sess.query(Producto).filter(Producto.activo.is_(False))
        if q:
            query = query.filter(
                or_(
                    Producto.codigo.ilike(f"%{q}%"),
                    Producto.descripcion.ilike(f"%{q}%"),
                    Producto.marca.ilike(f"%{q}%"),
                    Producto.modelo.ilike(f"%{q}%"),
                )
            )
        productos = query.order_by(Producto.codigo.asc()).all()
        total_inactivos = sess.query(Producto).filter(Producto.activo.is_(False)).count()
        return render_template(
            "productos/desactivados.html",
            productos=productos,
            online_users=_online_users(),
            q=q,
            total_inactivos=total_inactivos,
        )
    finally:
        sess.close()


@productos_bp.route("/productos/reactivar", methods=["POST"])
@admin_required
def reactivar_producto():
    codigo = (request.form.get("codigo") or "").strip().upper()
    if not codigo:
        return jsonify({"success": False, "error": "Codigo invalido"}), 400

    sess = SessionDB()
    try:
        producto = sess.query(Producto).filter_by(codigo=codigo).first()
        if producto is None:
            return jsonify({"success": False, "error": "No encontrado"}), 404
        if producto.activo:
            return jsonify({"success": False, "error": "El producto ya esta activo"}), 409
        before = _producto_snapshot(producto)
        producto.activo = True
        after = _producto_snapshot(producto)
        register_product_audit(
            sess,
            actor=_actor_usuario(),
            action="reactivate",
            modulo="productos",
            producto_codigo=(producto.codigo or "").strip().upper(),
            req=request,
            diffs=build_diffs(before, after),
        )
        sess.commit()
        return jsonify({"success": True, "message": "Producto reactivado correctamente"})
    except Exception as exc:
        sess.rollback()
        return jsonify({"success": False, "error": str(exc)}), 500
    finally:
        sess.close()


@productos_bp.route("/productos/reactivar_todos", methods=["POST"])
@admin_required
def reactivar_todos():
    sess = SessionDB()
    try:
        inactivos = sess.query(Producto).filter(Producto.activo.is_(False)).all()
        count = len(inactivos)
        codigos = []
        for p in inactivos:
            p.activo = True
            codigos.append((p.codigo or "").strip().upper())
        if count:
            register_product_audit(
                sess,
                actor=_actor_usuario(),
                action="reactivate_bulk",
                modulo="productos",
                req=request,
                metadata={"count": count, "codigos": codigos[:200]},
            )
        sess.commit()
        return jsonify({"success": True, "reactivados": count,
                        "message": f"{count} productos reactivados correctamente"})
    except Exception as exc:
        sess.rollback()
        return jsonify({"success": False, "error": str(exc)}), 500
    finally:
        sess.close()


# =========================================
# VER PRODUCTO (FICHA TECNICA)
# =========================================
@productos_bp.route("/producto/<codigo>")
@login_required
def ver_producto(codigo):

    normalized = (codigo or "").strip().upper()
    db = SessionDB()
    user_perms = get_user_permissions(session.get("user"), session.get("rol"))
    producto = None
    categoria_txt = ""
    subcategoria_txt = ""
    homologados_items: list = []
    homologados_raw = ""
    try:
        producto = (
            db.query(Producto)
            .options(
                joinedload(Producto.categoria_rel),
                joinedload(Producto.subcategoria_rel),
            )
            .filter(Producto.activo.is_(True))
            .filter(
                or_(
                    Producto.codigo == normalized,
                    func.upper(func.trim(Producto.codigo)) == normalized,
                )
            )
            .first()
        )
        if producto:
            _auto_asignar_categoria_si_vacio(db, producto)
            db.refresh(producto)
            if producto.categoria_rel:
                categoria_txt = (producto.categoria_rel.nombre or "").strip()
            if producto.subcategoria_rel:
                subcategoria_txt = (producto.subcategoria_rel.nombre or "").strip()

            homologados_raw = (producto.homologados or "").strip()
            # Modal ficha: prioridad al texto de modelos (campo HOMOLOGADOS). Solo buscar ítems
            # relacionados por OEM si no hay texto (evita chips de códigos cuando hay lista larga).
            if not homologados_raw:
                forward_products = _forward_productos_homologados(db, producto, normalized)
                reverse_products = _reverse_products_listing_homologado(db, normalized)
                homologados_items = _merge_by_codigo(
                    list(forward_products) + list(reverse_products)
                )
            try:
                register_product_audit(
                    db,
                    actor=_actor_usuario(),
                    action="view",
                    modulo="productos",
                    producto_codigo=(producto.codigo or "").strip().upper(),
                    req=request,
                    metadata={"source": "ficha_modal"},
                )
                db.commit()
            except Exception:
                db.rollback()

        if not producto:
            return "Producto no encontrado"

        # =============================
        # BUSCAR IMÁGENES DEL PRODUCTO
        # (mientras la sesión sigue abierta; si se cerrara antes, producto queda detached)
        # =============================

        ruta_imagenes = "app/static/productos_img"
        imagenes = _collect_imagenes_producto_carpeta(ruta_imagenes, producto)

        # =============================
        # BUSCAR IMÁGENES 360 POR CARPETA
        # =============================

        ruta_360 = f"app/static/productos360/{producto.codigo}" if producto.codigo else ""
        imagenes_360 = []

        if os.path.isdir(ruta_360):
            for archivo in os.listdir(ruta_360):
                if archivo.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                    imagenes_360.append(archivo)

        imagenes_360.sort()

        despiece_row = None
        despiece_partes: list = []
        despiece_imagen_url = None
        despiece_titulo = ""
        despiece_notas = ""
        despiece_partes_texto = ""
        has_despiece_panel = True
        if producto:
            try:
                despiece_row = _find_oem_despiece_for_producto(db, producto)
            except Exception:
                despiece_row = None
            if despiece_row:
                despiece_titulo = (despiece_row.titulo or "").strip()
                despiece_notas = (despiece_row.notas or "").strip()
                img_rel = (despiece_row.imagen_static or "").strip()
                if img_rel:
                    despiece_imagen_url = url_for("static", filename=img_rel)
                if despiece_row.partes_json:
                    try:
                        parsed = json.loads(despiece_row.partes_json)
                        if isinstance(parsed, list):
                            despiece_partes = parsed
                    except Exception:
                        despiece_partes = []
                    try:
                        despiece_partes_texto = json.dumps(
                            json.loads(despiece_row.partes_json),
                            indent=2,
                            ensure_ascii=False,
                        )
                    except Exception:
                        despiece_partes_texto = (despiece_row.partes_json or "").strip()

            if not despiece_imagen_url and producto:
                fb_name = _find_epc_despiece_archivo_en_static(producto)
                if fb_name:
                    despiece_imagen_url = url_for(
                        "static", filename=f"epc_despiece/{fb_name}"
                    )

        oem_match = _norm_oem_despiece(getattr(producto, "codigo_oem", None)) if producto else ""
        despiece_erp_precio = getattr(producto, "p_publico", None) if producto else None
        despiece_erp_precio_mayor = (
            getattr(producto, "prec_mayor", None)
            if (producto and bool(user_perms.get("ver_precio_mayor", True)))
            else None
        )

        es_admin = "admin" in (session.get("rol") or "").strip().lower()

        html = render_template(
            "modal_producto.html",
            producto=producto,
            imagenes=imagenes,
            imagenes_360=imagenes_360,
            categoria_txt=categoria_txt,
            subcategoria_txt=subcategoria_txt,
            homologados_items=homologados_items,
            homologados_raw=homologados_raw,
            has_despiece_panel=has_despiece_panel,
            es_admin=es_admin,
            despiece_titulo=despiece_titulo,
            despiece_imagen_url=despiece_imagen_url,
            despiece_partes=despiece_partes,
            despiece_notas=despiece_notas,
            despiece_partes_texto=despiece_partes_texto,
            despiece_oem_match=oem_match,
            despiece_erp_precio=despiece_erp_precio,
            despiece_erp_precio_mayor=despiece_erp_precio_mayor,
            can_view_precio_mayor=bool(user_perms.get("ver_precio_mayor", True)),
        )
        resp = make_response(html)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        return resp
    finally:
        db.close()


@productos_bp.route("/producto/<codigo>/despiece", methods=["POST"])
@admin_required
def guardar_despiece_producto(codigo):
    """Crea o actualiza despiece ligado al código interno (admin). Imagen en static/epc_despiece/."""
    import uuid

    normalized = (codigo or "").strip().upper()
    if not normalized:
        return jsonify(success=False, message="Código inválido"), 400

    titulo = (request.form.get("titulo") or "").strip()[:220] or None
    notas = (request.form.get("notas") or "").strip() or None
    partes_raw = (request.form.get("partes_json") or "").strip()
    borrar_imagen = request.form.get("borrar_imagen") == "1"

    partes_json_val = None
    if partes_raw:
        try:
            parsed = json.loads(partes_raw)
            if not isinstance(parsed, list):
                return jsonify(success=False, message="partes_json debe ser un array JSON []"), 400
            partes_json_val = json.dumps(parsed, ensure_ascii=False)
        except json.JSONDecodeError as exc:
            return jsonify(success=False, message=f"JSON inválido en partes: {exc}"), 400

    static_dir = Path(__file__).resolve().parent.parent / "static" / "epc_despiece"
    static_dir.mkdir(parents=True, exist_ok=True)

    db = SessionDB()
    try:
        producto = (
            db.query(Producto)
            .filter(
                or_(
                    Producto.codigo == normalized,
                    func.upper(func.trim(Producto.codigo)) == normalized,
                )
            )
            .first()
        )
        if not producto:
            return jsonify(success=False, message="Producto no encontrado"), 404

        row = db.query(OemDespiece).filter(OemDespiece.producto_codigo == normalized).first()
        if not row:
            synthetic = _synthetic_oem_norm_for_product_codigo(normalized)
            if db.query(OemDespiece).filter(OemDespiece.oem_norm == synthetic).first():
                synthetic = ("_INT_" + normalized + "_" + uuid.uuid4().hex[:8])[:64]
            row = OemDespiece(
                oem_norm=synthetic,
                producto_codigo=normalized,
                titulo=titulo,
                notas=notas,
                partes_json=partes_json_val,
                updated_at=datetime.utcnow(),
            )
            db.add(row)
        else:
            row.titulo = titulo
            row.notas = notas
            row.partes_json = partes_json_val
            row.updated_at = datetime.utcnow()

        upload = request.files.get("imagen")
        if upload and upload.filename:
            ext = Path(upload.filename).suffix.lower()
            if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                return jsonify(success=False, message="Imagen: use PNG, JPG, WEBP o GIF"), 400
            base_fn = secure_filename(normalized) or "codigo"
            fname = f"{base_fn}_{int(datetime.utcnow().timestamp())}{ext}"
            path = static_dir / fname
            upload.save(path)
            out = process_uploaded_image(path)
            if out is not None:
                path = out
                fname = out.name
            row.imagen_static = f"epc_despiece/{fname}"
        elif borrar_imagen:
            row.imagen_static = None

        try:
            register_product_audit(
                db,
                actor=_actor_usuario(),
                action="despiece_save",
                modulo="productos",
                producto_codigo=normalized,
                req=request,
                metadata={"titulo": titulo, "borrar_imagen": borrar_imagen},
            )
        except Exception:
            pass
        db.commit()

        return jsonify(success=True)
    except Exception as exc:
        db.rollback()
        return jsonify(success=False, message=str(exc)), 500
    finally:
        db.close()


@productos_bp.route("/producto/<codigo>/historial")
@login_required
def historial_producto(codigo):

    normalized = (codigo or "").strip().upper()
    sess = SessionDB()
    try:
        producto = sess.query(Producto).filter(Producto.codigo == normalized).first()

        movimientos = (
            security_db.session.query(MovimientoStock)
            .filter(MovimientoStock.codigo_producto == normalized)
            .order_by(MovimientoStock.fecha.desc(), MovimientoStock.id.desc())
            .limit(200)
            .all()
        )

        return render_template(
            "productos/historial_producto.html",
            producto=producto,
            codigo=normalized,
            movimientos=movimientos,
            online_users=_online_users(),
            active_page="productos_buscar",
            _partial=_wants_modal_fragment(),
        )
    finally:
        sess.close()


@productos_bp.route("/producto/<codigo>/homologados")
@login_required
def homologados_producto(codigo):

    normalized = (codigo or "").strip().upper()
    sess = SessionDB()
    try:
        producto = _find_producto_by_codigo(sess, codigo)
        return render_template(
            "productos/homologados_producto.html",
            producto=producto,
            codigo=normalized,
            online_users=_online_users(),
            active_page="productos_buscar",
            _partial=_wants_modal_fragment(),
        )
    finally:
        sess.close()

# =========================================
# EXPORTAR INVENTARIO
# =========================================
@productos_bp.route("/exportar")
@login_required
def exportar():

    db = SessionDB()
    productos = db.query(Producto).filter(Producto.activo.is_(True)).all()

    perms = get_user_permissions(session.get("user"), session.get("rol"))
    can_view_precio_mayor = bool(perms.get("ver_precio_mayor", True))

    data = []
    for p in productos:
        if p is None:
            continue
        row = {
            "Código": p.codigo or "",
            "Descripción": p.descripcion or "",
            "Modelo": p.modelo or "",
            "Motor": p.motor or "",
            "Marca": p.marca or "",
            "Precio Público": p.p_publico or 0,
            "OEM": p.codigo_oem or "",
            "Alternativo": p.codigo_alternativo or "",
            "Homologados": p.homologados or ""
        }
        if can_view_precio_mayor:
            row["Precio Mayor"] = p.prec_mayor or 0
        data.append(row)

    db.close()

    df = pd.DataFrame(data)

    output = io.BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)

    return send_file(
        output,
        download_name="inventario.xlsx",
        as_attachment=True
    )


# =========================================
# IMPORTAR PRODUCTOS DESDE EXCEL/CSV
# =========================================
@productos_bp.route("/productos/importar_excel", methods=["POST"])
@admin_required
def importar_excel_productos():

    archivo = request.files.get("file")
    if not archivo:
        return jsonify(
            success=False,
            message="No se selecciono archivo",
            inserted=0,
            updated=0,
            skipped=0,
            errors=0,
        ), 400

    filename = (archivo.filename or "").strip()
    if not filename:
        return jsonify(
            success=False,
            message="Archivo invalido",
            inserted=0,
            updated=0,
            skipped=0,
            errors=0,
        ), 400

    extension = os.path.splitext(filename)[1].lower()
    if extension not in {".xlsx", ".xls", ".csv"}:
        return jsonify(
            success=False,
            message="Formato no soportado. Use .xlsx, .xls o .csv",
            inserted=0,
            updated=0,
            skipped=0,
            errors=0,
        ), 400

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            delete=False,
            suffix=extension,
        ) as tmp_file:
            tmp_path = tmp_file.name

        archivo.save(tmp_path)

        summary = import_products_from_excel(tmp_path, batch_size=2000)
        errors_count = summary.get("errors_count", len(summary.get("errors", [])))
        msg = "Importacion completada"
        notes = summary.get("import_notes") or []
        if notes:
            msg += " — " + " ".join(notes)

        return jsonify(
            success=True,
            message=msg,
            inserted=summary.get("inserted", 0),
            updated=summary.get("updated", 0),
            skipped=summary.get("skipped", 0),
            errors=errors_count,
            time_seconds=summary.get("time_seconds", 0.0),
            summary=summary,
        )

    except Exception as exc:
        return jsonify(
            success=False,
            message=str(exc),
            inserted=0,
            updated=0,
            skipped=0,
            errors=0,
        ), 500

    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


# =========================================
# EXPORTAR PRODUCTOS A EXCEL
# =========================================
@productos_bp.route("/productos/exportar_excel", methods=["GET"])
@admin_required
def exportar_excel_productos():

    filtro = request.args.get('filtro', 'activos')
    columnas_param = request.args.get('columnas', '')

    COLUMN_MAP = {
        'codigo':         ('C\u00f3digo',         lambda p: p.codigo or ''),
        'descripcion':    ('Descripci\u00f3n',    lambda p: p.descripcion or ''),
        'marca':          ('Marca',          lambda p: p.marca or ''),
        'modelo':         ('Modelo',         lambda p: p.modelo or ''),
        'motor':          ('Motor',          lambda p: p.motor or ''),
        'oem':            ('OEM',            lambda p: p.codigo_oem or ''),
        'alternativo':    ('Alternativo',    lambda p: p.codigo_alternativo or ''),
        'precio_publico': ('Precio P\u00fablico', lambda p: p.p_publico or 0),
        'precio_mayor':   ('Precio Mayor',   lambda p: p.prec_mayor or 0),
        'homologados':    ('Homologados',    lambda p: p.homologados or ''),
    }

    if columnas_param:
        sel_keys = [k.strip() for k in columnas_param.split(',') if k.strip() in COLUMN_MAP]
        if 'codigo' not in sel_keys:
            sel_keys.insert(0, 'codigo')
    else:
        sel_keys = list(COLUMN_MAP.keys())

    db_session = SessionDB()
    query = db_session.query(Producto)
    if filtro == 'activos':
        query = query.filter(Producto.activo.is_(True))
    elif filtro == 'inactivos':
        query = query.filter(Producto.activo.is_(False))
    # filtro == 'todos' -> no filter applied
    productos = query.all()

    rows = []
    for p in productos:
        if p is None:
            continue
        row = {}
        for key in sel_keys:
            label, getter = COLUMN_MAP[key]
            row[label] = getter(p)
        rows.append(row)

    db_session.close()

    headers = [COLUMN_MAP[k][0] for k in sel_keys]
    df = pd.DataFrame(rows, columns=headers)

    output = io.BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)

    return send_file(
        output,
        as_attachment=True,
        download_name="productos_andes_auto_parts.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

# =========================================
# MODAL CREAR PRODUCTO
# =========================================
@productos_bp.route("/modal_crear")
@login_required
def modal_crear_producto():

    db = SessionDB()

    productos = db.query(Producto.marca, Producto.modelo)\
        .filter(Producto.activo.is_(True))\
        .filter(Producto.marca != None)\
        .filter(Producto.modelo != None)\
        .all()

    db.close()

    # Diccionario marca -> modelos
    marca_modelos = {}

    for marca, modelo in productos:
        if marca not in marca_modelos:
            marca_modelos[marca] = set()
        marca_modelos[marca].add(modelo)

    # Ordenar alfabéticamente
    marca_modelos = {
        k: sorted(list(v))
        for k, v in sorted(marca_modelos.items())
    }

    marcas = list(marca_modelos.keys())

    # Todos los modelos ordenados
    modelos = sorted({modelo for _, modelo in productos})

    return render_template(
        "modal_crear_producto.html",
        modelos=modelos,
        marcas=marcas,
        marca_modelos=marca_modelos
    )


@productos_bp.route("/productos/crear", methods=["POST"])
@admin_required
def crear_producto():
    codigo = (request.form.get("codigo") or "").strip().upper()
    if not codigo:
        return jsonify({"success": False, "error": "Código interno es obligatorio"}), 400

    sess = SessionDB()
    try:
        existente = sess.query(Producto).filter_by(codigo=codigo).first()
        if existente:
            return jsonify({"success": False, "error": f"El código {codigo} ya existe"}), 409

        descripcion = (request.form.get("descripcion") or "").strip()
        marca = (request.form.get("marca_nueva") or request.form.get("marca") or "").strip()
        modelo = (request.form.get("modelo_nuevo") or request.form.get("modelo") or "").strip()
        categoria_txt = (request.form.get("categoria") or "").strip()
        subcategoria_txt = (request.form.get("subcategoria") or "").strip()

        categoria_id, subcategoria_id = _resolve_taxonomia_create(sess, categoria_txt, subcategoria_txt)

        def _f(v):
            if v is None:
                return None
            s = str(v).strip()
            if not s:
                return None
            try:
                return float(s.replace(",", "."))
            except ValueError:
                return None

        p = Producto(
            codigo=codigo,
            descripcion=descripcion,
            marca=marca,
            modelo=modelo,
            motor=(request.form.get("motor") or "").strip(),
            anio=(request.form.get("anio") or "").strip(),
            version=(request.form.get("version") or "").strip(),
            medidas=(request.form.get("medidas") or "").strip(),
            codigo_oem=(request.form.get("codigo_oem") or "").strip(),
            codigo_alternativo=(request.form.get("codigo_alternativo") or "").strip(),
            homologados=(request.form.get("homologados") or "").strip(),
            categoria_id=categoria_id,
            subcategoria_id=subcategoria_id,
            p_publico=_f(request.form.get("precio")),
            prec_mayor=_f(request.form.get("precio_mayor")),
            activo=True,
        )
        sess.add(p)
        sess.flush()

        image_files = request.files.getlist("imagenes")
        image_routes = _save_uploaded_images(codigo, image_files)
        for i, ruta in enumerate(image_routes):
            p.imagenes.append(ProductoImagen(ruta=ruta, es_principal=(i == 0)))

        despiece = request.files.get("despiece_img")
        if despiece and getattr(despiece, "filename", None):
            filename = secure_filename(despiece.filename)
            ext = (filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg")
            if ext in {"jpg", "jpeg", "png", "webp"}:
                target_name = f"{codigo}_despiece.{ext}"
                target = Path("app/static/productos_img") / target_name
                target.parent.mkdir(parents=True, exist_ok=True)
                despiece.save(str(target))
                out = process_uploaded_image(target)
                if out is not None:
                    target_name = out.name
                p.despiece = f"productos_img/{target_name}"

        register_product_audit(
            sess,
            actor=_actor_usuario(),
            action="create",
            modulo="productos",
            producto_codigo=codigo,
            req=request,
            metadata={
                "marca": marca,
                "modelo": modelo,
                "categoria_id": categoria_id,
                "subcategoria_id": subcategoria_id,
                "imagenes_count": len(image_routes),
            },
            diffs=build_diffs({}, _producto_snapshot(p)),
        )
        sess.commit()
        return jsonify({"success": True, "message": f"Producto {codigo} creado correctamente"})
    except Exception as exc:
        sess.rollback()
        return jsonify({"success": False, "error": str(exc)}), 500
    finally:
        sess.close()
# =========================================
# BUSCAR DATOS POR OEM (INTELIGENCIA CREAR)
# =========================================
@productos_bp.route("/buscar_oem")
@login_required
def buscar_oem():

    import os

    oem = request.args.get("oem", "").strip()

    if not oem:
        return {"encontrado": False}

    db = SessionDB()

    producto = db.query(Producto).filter(
        Producto.activo.is_(True),
        Producto.codigo_oem.ilike(f"%{oem}%")
    ).first()

    db.close()

    if not producto:
        return {"encontrado": False}

    # ==============================
    # DETECTOR INTELIGENTE DE IMAGEN
    # ==============================

    base_path = os.path.join("app", "static", "productos_img")
    extensiones = ["jpg", "png", "webp", "jpeg"]

    imagen_detectada = None

    # 1️⃣ Intentar con código OEM
    if producto.codigo_oem:
        for ext in extensiones:
            posible = f"{producto.codigo_oem}.{ext}"
            ruta = os.path.join(base_path, posible)
            if os.path.exists(ruta):
                imagen_detectada = f"productos_img/{posible}"
                break

    # 2️⃣ Si no encontró imagen por OEM, intentar con código interno
    if not imagen_detectada and producto.codigo:
        for ext in extensiones:
            posible = f"{producto.codigo}.{ext}"
            ruta = os.path.join(base_path, posible)
            if os.path.exists(ruta):
                imagen_detectada = f"productos_img/{posible}"
                break

    # ==============================
    # DETECTOR DESPIECE
    # ==============================

    despiece_detectado = None

    if producto.codigo:
        for ext in extensiones:
            posible = f"{producto.codigo}_despiece.{ext}"
            ruta = os.path.join(base_path, posible)
            if os.path.exists(ruta):
                despiece_detectado = f"productos_img/{posible}"
                break

    # ==============================
    # RESPUESTA FINAL
    # ==============================

    imagenes = []

    # 🔥 Si existe relación múltiple nueva
    try:
        if producto.imagenes:
            for img in producto.imagenes:
                if img.ruta:
                    imagenes.append(img.ruta)
    except:
        pass

    # 🔥 Compatibilidad sistema antiguo
    if not imagenes and imagen_detectada:
        imagenes.append(imagen_detectada)

    return {
        "encontrado": True,
        "oem_existe": True,
        "descripcion": producto.descripcion,
        "modelo": producto.modelo,
        "motor": producto.motor,
        "marca": producto.marca,
        "homologados": producto.homologados,
        "codigo_alternativo": producto.codigo_alternativo,
        "despiece": despiece_detectado,
        "imagenes": imagenes
    }


@productos_bp.route("/productos/validar", methods=["POST"])
@admin_required
def validar_producto_form():
    data = request.get_json(silent=True) or {}
    codigo = (data.get("codigo") or "").strip().upper()
    oem = (data.get("codigo_oem") or "").strip()
    categoria_id = data.get("categoria_id")
    subcategoria_id = data.get("subcategoria_id")
    editing_codigo = (data.get("editing_codigo") or "").strip().upper()

    sess = SessionDB()
    try:
        errors = {}
        warnings = {}

        if codigo:
            q = sess.query(Producto).filter(func.upper(func.trim(Producto.codigo)) == codigo)
            if editing_codigo:
                q = q.filter(func.upper(func.trim(Producto.codigo)) != editing_codigo)
            if q.first():
                errors["codigo"] = "Este código ya existe."

        if oem:
            q_oem = sess.query(Producto).filter(Producto.codigo_oem.ilike(f"%{oem}%"))
            if editing_codigo:
                q_oem = q_oem.filter(func.upper(func.trim(Producto.codigo)) != editing_codigo)
            if q_oem.first():
                warnings["codigo_oem"] = "OEM ya existe en otro producto."

        if categoria_id and subcategoria_id:
            try:
                cid = int(categoria_id)
                sid = int(subcategoria_id)
                sub = sess.query(Subcategoria).filter(Subcategoria.id == sid).first()
                if sub and sub.categoria_id != cid:
                    errors["subcategoria_id"] = "La subcategoría no corresponde a la categoría."
            except Exception:
                errors["subcategoria_id"] = "Subcategoría inválida."

        return jsonify({"success": True, "errors": errors, "warnings": warnings, "valid": len(errors) == 0})
    finally:
        sess.close()


@productos_bp.route("/productos/api/sugerencias_taxonomia")
@login_required
def sugerencias_taxonomia():
    q = (request.args.get("q") or "").strip().lower()
    tipo = (request.args.get("tipo") or "categoria").strip().lower()
    categoria_id = (request.args.get("categoria_id") or "").strip()
    limit = max(5, min(request.args.get("limit", 10, type=int) or 10, 30))

    sess = SessionDB()
    try:
        if tipo == "subcategoria":
            query = sess.query(Subcategoria)
            if categoria_id.isdigit():
                query = query.filter(Subcategoria.categoria_id == int(categoria_id))
            if q:
                query = query.filter(func.lower(Subcategoria.nombre).like(f"%{q}%"))
            rows = query.order_by(Subcategoria.nombre.asc()).limit(limit).all()
            return jsonify(
                {
                    "success": True,
                    "items": [{"id": r.id, "nombre": r.nombre or "", "categoria_id": r.categoria_id} for r in rows],
                }
            )

        query = sess.query(Categoria)
        if q:
            query = query.filter(func.lower(Categoria.nombre).like(f"%{q}%"))
        rows = query.order_by(Categoria.nombre.asc()).limit(limit).all()
        return jsonify({"success": True, "items": [{"id": r.id, "nombre": r.nombre or ""} for r in rows]})
    finally:
        sess.close()


@productos_bp.route("/productos/draft", methods=["GET", "POST", "DELETE"])
@admin_required
def productos_draft():
    user = _actor_usuario()
    form_key = (request.values.get("form_key") or "").strip()
    if not form_key:
        return jsonify({"success": False, "error": "form_key es obligatorio"}), 400

    sess = SessionDB()
    try:
        row = (
            sess.query(ProductoDraft)
            .filter(ProductoDraft.user == user, ProductoDraft.form_key == form_key)
            .first()
        )
        if request.method == "GET":
            if not row:
                return jsonify({"success": True, "draft": None})
            payload = {}
            try:
                payload = json.loads(row.payload_json or "{}")
            except Exception:
                payload = {}
            return jsonify(
                {
                    "success": True,
                    "draft": {
                        "form_key": row.form_key,
                        "producto_codigo": row.producto_codigo,
                        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                        "payload": payload,
                    },
                }
            )

        if request.method == "DELETE":
            if row:
                sess.delete(row)
                sess.commit()
            return jsonify({"success": True})

        data = request.get_json(silent=True) or {}
        payload = data.get("payload") or {}
        producto_codigo = (data.get("producto_codigo") or "").strip().upper() or None
        payload_json = json.dumps(payload, ensure_ascii=False, default=str)
        if not row:
            row = ProductoDraft(
                user=user,
                form_key=form_key,
                producto_codigo=producto_codigo,
                payload_json=payload_json,
            )
            sess.add(row)
        else:
            row.producto_codigo = producto_codigo
            row.payload_json = payload_json
        sess.commit()
        return jsonify({"success": True, "updated_at": row.updated_at.isoformat() if row.updated_at else None})
    finally:
        sess.close()


@productos_bp.route("/productos/auditoria")
@admin_required
def productos_auditoria():
    page = request.args.get("page", 1, type=int) or 1
    per_page = request.args.get("per_page", 50, type=int) or 50
    per_page = max(20, min(per_page, 200))

    actor = (request.args.get("actor") or "").strip()
    action = (request.args.get("action") or "").strip()
    codigo = (request.args.get("codigo") or "").strip().upper()
    fecha_desde = (request.args.get("fecha_desde") or "").strip()
    fecha_hasta = (request.args.get("fecha_hasta") or "").strip()

    sess = SessionDB()
    try:
        query = sess.query(ProductoAuditEvent)
        if actor:
            query = query.filter(ProductoAuditEvent.actor.ilike(f"%{actor}%"))
        if action:
            query = query.filter(ProductoAuditEvent.action == action)
        if codigo:
            query = query.filter(ProductoAuditEvent.producto_codigo == codigo)
        if fecha_desde:
            try:
                query = query.filter(ProductoAuditEvent.created_at >= datetime.strptime(fecha_desde, "%Y-%m-%d"))
            except ValueError:
                pass
        if fecha_hasta:
            try:
                hasta = datetime.strptime(fecha_hasta, "%Y-%m-%d") + timedelta(days=1)
                query = query.filter(ProductoAuditEvent.created_at < hasta)
            except ValueError:
                pass

        total_count = query.count()
        total_pages = max(1, (total_count + per_page - 1) // per_page) if total_count else 1
        page = max(1, min(page, total_pages))
        eventos = (
            query.order_by(ProductoAuditEvent.created_at.desc(), ProductoAuditEvent.id.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )
        event_ids = [e.id for e in eventos]
        diffs_map = {}
        if event_ids:
            diffs = (
                sess.query(ProductoAuditDiff)
                .filter(ProductoAuditDiff.event_id.in_(event_ids))
                .order_by(ProductoAuditDiff.event_id.asc(), ProductoAuditDiff.id.asc())
                .all()
            )
            for d in diffs:
                diffs_map.setdefault(d.event_id, []).append(d)

        return render_template(
            "productos/auditoria.html",
            eventos=eventos,
            diffs_map=diffs_map,
            page=page,
            per_page=per_page,
            total_count=total_count,
            total_pages=total_pages,
            filtros={
                "actor": actor,
                "action": action,
                "codigo": codigo,
                "fecha_desde": fecha_desde,
                "fecha_hasta": fecha_hasta,
            },
            online_users=_online_users(),
            active_page="productos_auditoria",
        )
    finally:
        sess.close()


@productos_bp.route("/productos/auditoria/exportar", methods=["GET"])
@admin_required
def exportar_auditoria_productos():
    actor = (request.args.get("actor") or "").strip()
    action = (request.args.get("action") or "").strip()
    codigo = (request.args.get("codigo") or "").strip().upper()
    fecha_desde = (request.args.get("fecha_desde") or "").strip()
    fecha_hasta = (request.args.get("fecha_hasta") or "").strip()

    sess = SessionDB()
    try:
        query = sess.query(ProductoAuditEvent)
        if actor:
            query = query.filter(ProductoAuditEvent.actor.ilike(f"%{actor}%"))
        if action:
            query = query.filter(ProductoAuditEvent.action == action)
        if codigo:
            query = query.filter(ProductoAuditEvent.producto_codigo == codigo)
        if fecha_desde:
            try:
                query = query.filter(ProductoAuditEvent.created_at >= datetime.strptime(fecha_desde, "%Y-%m-%d"))
            except ValueError:
                pass
        if fecha_hasta:
            try:
                hasta = datetime.strptime(fecha_hasta, "%Y-%m-%d") + timedelta(days=1)
                query = query.filter(ProductoAuditEvent.created_at < hasta)
            except ValueError:
                pass
        eventos = query.order_by(ProductoAuditEvent.created_at.desc()).limit(5000).all()
        event_ids = [e.id for e in eventos]
        diffs = []
        if event_ids:
            diffs = (
                sess.query(ProductoAuditDiff)
                .filter(ProductoAuditDiff.event_id.in_(event_ids))
                .order_by(ProductoAuditDiff.event_id.asc(), ProductoAuditDiff.id.asc())
                .all()
            )
        diffs_map = {}
        for d in diffs:
            diffs_map.setdefault(d.event_id, []).append(d)

        rows = []
        for e in eventos:
            diffs_txt = " | ".join(
                f"{d.campo}: {d.valor_anterior or ''} -> {d.valor_nuevo or ''}"
                for d in diffs_map.get(e.id, [])
            )
            rows.append(
                {
                    "Fecha": e.created_at.strftime("%Y-%m-%d %H:%M:%S") if e.created_at else "",
                    "Usuario": e.actor or "",
                    "Accion": e.action or "",
                    "Modulo": e.modulo or "",
                    "Codigo": e.producto_codigo or "",
                    "Path": e.request_path or "",
                    "IP": e.ip or "",
                    "Metadata": e.metadata_json or "",
                    "Cambios": diffs_txt,
                }
            )
        df = pd.DataFrame(rows, columns=["Fecha", "Usuario", "Accion", "Modulo", "Codigo", "Path", "IP", "Metadata", "Cambios"])
        output = io.BytesIO()
        df.to_excel(output, index=False)
        output.seek(0)
        return send_file(
            output,
            as_attachment=True,
            download_name=f"auditoria_productos_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    finally:
        sess.close()


def _static_producto_img_names() -> set[str]:
    base = Path("app/static/productos_img")
    if not base.is_dir():
        return set()
    return {f.name.lower() for f in base.iterdir() if f.is_file()}


def _producto_tiene_imagen_visual(codigo: str, codigos_con_imagen_db: set[str], static_names: set[str]) -> bool:
    c = (codigo or "").strip().upper()
    if not c:
        return False
    if c in codigos_con_imagen_db:
        return True
    cl = c.lower()
    for ext in ("jpg", "jpeg", "png", "webp"):
        if f"{cl}.{ext}" in static_names:
            return True
    prefix = cl + "_"
    for fn in static_names:
        if fn.startswith(prefix) and fn.endswith((".jpg", ".jpeg", ".png", ".webp")):
            return True
    return False


def _stock_total_producto_row(p: Producto) -> float:
    return float(
        (p.stock_10jul or 0)
        + (p.stock_brasil or 0)
        + (p.stock_g_avenida or 0)
        + (p.stock_orientales or 0)
        + (p.stock_b20_outlet or 0)
        + (p.stock_transito or 0)
    )


def _query_productos_sin_categoria_completa(sess):
    """Activos sin categoría ni subcategoría (misma regla que la autoasignación en lote)."""
    return (
        sess.query(Producto)
        .filter(Producto.activo.is_(True))
        .filter(Producto.categoria_id.is_(None))
        .filter(Producto.subcategoria_id.is_(None))
    )


@productos_bp.route("/productos/sin-categoria")
@admin_required
def productos_sin_categoria():
    page = request.args.get("page", 1, type=int) or 1
    per_page = request.args.get("per_page", 50, type=int) or 50
    per_page = max(25, min(int(per_page), 200))
    page = max(1, int(page))
    qtxt = (request.args.get("q") or "").strip()

    sess = SessionDB()
    try:
        base = _query_productos_sin_categoria_completa(sess)
        if qtxt:
            like = f"%{qtxt}%"
            base = base.filter(
                or_(
                    Producto.codigo.ilike(like),
                    Producto.descripcion.ilike(like),
                )
            )
        total = base.count()
        total_pages = max(1, (total + per_page - 1) // per_page) if total else 1
        if page > total_pages:
            page = total_pages
        rows = (
            base.order_by(Producto.codigo.asc())
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )
        items = [
            {
                "codigo": (p.codigo or "").strip().upper(),
                "descripcion": (p.descripcion or "")[:160],
            }
            for p in rows
        ]
        return render_template(
            "productos/sin_categoria.html",
            total=total,
            items=items,
            page=page,
            per_page=per_page,
            total_pages=total_pages,
            q=qtxt,
            online_users=_online_users(),
            active_page="productos_sin_categoria",
        )
    finally:
        sess.close()


@productos_bp.route("/productos/sin-categoria/autoasignar", methods=["POST"])
@admin_required
def productos_sin_categoria_autoasignar():
    sess = SessionDB()
    try:
        n = bulk_auto_asignar_categorias_faltantes(sess)
        pend = _query_productos_sin_categoria_completa(sess).count()
        return jsonify(
            {
                "success": True,
                "asignados": n,
                "pendientes": pend,
            }
        )
    except Exception as exc:
        sess.rollback()
        return jsonify({"success": False, "error": str(exc)}), 500
    finally:
        sess.close()


@productos_bp.route("/productos/panel-calidad")
@admin_required
def panel_calidad_stock():
    """
    Panel de calidad de datos y alertas de stock (solo lectura + enlaces a editar/buscar).
    """
    limite = request.args.get("limite", 40, type=int) or 40
    limite = max(10, min(limite, 100))
    stock_critico = request.args.get("stock_critico", 5, type=int)
    if stock_critico is None:
        stock_critico = 5
    stock_critico = max(0, min(int(stock_critico), 500))
    dias_sin_mov = request.args.get("dias_sin_movimiento", 180, type=int) or 180
    dias_sin_mov = max(30, min(dias_sin_mov, 730))

    sess = SessionDB()
    try:
        total_activos = sess.query(Producto).filter(Producto.activo.is_(True)).count()
        if total_activos == 0:
            return render_template(
                "productos/panel_calidad.html",
                total_activos=0,
                kpis={},
                listas={},
                duplicados_oem=[],
                online_users=_online_users(),
                active_page="productos_panel_calidad",
                params={"limite": limite, "stock_critico": stock_critico, "dias_sin_movimiento": dias_sin_mov},
            )

        productos = (
            sess.query(Producto)
            .filter(Producto.activo.is_(True))
            .options(joinedload(Producto.categoria_rel), joinedload(Producto.subcategoria_rel))
            .all()
        )

        rows_img = sess.query(ProductoImagen.producto_codigo).distinct().all()
        codigos_con_img_db = {(r[0] or "").strip().upper() for r in rows_img if r[0]}
        static_names = _static_producto_img_names()

        sin_imagen = 0
        sin_categoria = 0
        sin_oem = 0
        sin_homologados = 0
        stock_total_cero = 0
        stock_bajo = 0

        lista_sin_imagen = []
        lista_sin_cat = []
        lista_sin_oem = []
        lista_sin_homo = []
        lista_stock_cero = []
        lista_stock_bajo = []

        for p in productos:
            cod = (p.codigo or "").strip().upper()
            if not _producto_tiene_imagen_visual(cod, codigos_con_img_db, static_names):
                sin_imagen += 1
                if len(lista_sin_imagen) < limite:
                    lista_sin_imagen.append(
                        {"codigo": cod, "descripcion": (p.descripcion or "")[:120]}
                    )
            if p.categoria_id is None:
                sin_categoria += 1
                if len(lista_sin_cat) < limite:
                    lista_sin_cat.append(
                        {"codigo": cod, "descripcion": (p.descripcion or "")[:120]}
                    )
            oem_txt = (p.codigo_oem or "").strip()
            if not oem_txt:
                sin_oem += 1
                if len(lista_sin_oem) < limite:
                    lista_sin_oem.append(
                        {"codigo": cod, "descripcion": (p.descripcion or "")[:120]}
                    )
            homo_txt = (p.homologados or "").strip()
            if not homo_txt:
                sin_homologados += 1
                if len(lista_sin_homo) < limite:
                    lista_sin_homo.append(
                        {"codigo": cod, "descripcion": (p.descripcion or "")[:120]}
                    )

            st = _stock_total_producto_row(p)
            if st <= 0:
                stock_total_cero += 1
                if len(lista_stock_cero) < limite:
                    lista_stock_cero.append(
                        {
                            "codigo": cod,
                            "descripcion": (p.descripcion or "")[:120],
                            "stock": st,
                        }
                    )
            elif 0 < st <= stock_critico:
                stock_bajo += 1
                if len(lista_stock_bajo) < limite:
                    lista_stock_bajo.append(
                        {
                            "codigo": cod,
                            "descripcion": (p.descripcion or "")[:120],
                            "stock": st,
                        }
                    )

        def pct(n: int) -> float:
            return round(100.0 * n / total_activos, 1) if total_activos else 0.0

        kpis = {
            "sin_imagen": {"count": sin_imagen, "pct": pct(sin_imagen)},
            "sin_categoria": {"count": sin_categoria, "pct": pct(sin_categoria)},
            "sin_oem": {"count": sin_oem, "pct": pct(sin_oem)},
            "sin_homologados": {"count": sin_homologados, "pct": pct(sin_homologados)},
            "stock_cero": {"count": stock_total_cero, "pct": pct(stock_total_cero)},
            "stock_bajo": {"count": stock_bajo, "pct": pct(stock_bajo)},
        }

        duplicados_oem = []
        try:
            dup_rows = sess.execute(
                text(
                    """
                    SELECT UPPER(TRIM([CODIGO OEM])) AS oem_norm, COUNT(*) AS cnt
                    FROM productos
                    WHERE ACTIVO = 1 AND TRIM(COALESCE([CODIGO OEM], '')) != ''
                    GROUP BY UPPER(TRIM([CODIGO OEM]))
                    HAVING COUNT(*) > 1
                    ORDER BY cnt DESC
                    LIMIT 12
                    """
                )
            ).fetchall()
            for oem_norm, cnt in dup_rows:
                codigos = [
                    r[0]
                    for r in sess.execute(
                        text(
                            """
                            SELECT CODIGO FROM productos
                            WHERE ACTIVO = 1 AND UPPER(TRIM([CODIGO OEM])) = :oem
                            ORDER BY CODIGO ASC
                            LIMIT 8
                            """
                        ),
                        {"oem": oem_norm},
                    ).fetchall()
                ]
                duplicados_oem.append({"oem": oem_norm, "count": int(cnt), "codigos": codigos})
        except Exception:
            duplicados_oem = []

        sin_movimiento = []
        try:
            sm = sess.execute(
                text(
                    """
                    SELECT p.CODIGO, p.DESCRIPCION
                    FROM productos p
                    WHERE p.ACTIVO = 1
                    AND (
                        COALESCE(p.STOCK_10JUL,0)+COALESCE(p.STOCK_BRASIL,0)+COALESCE(p.STOCK_G_AVENIDA,0)+
                        COALESCE(p.STOCK_ORIENTALES,0)+COALESCE(p.STOCK_B20_OUTLET,0)+COALESCE(p.STOCK_TRANSITO,0)
                    ) > 0
                    AND NOT EXISTS (
                        SELECT 1 FROM movimientos_stock m
                        WHERE UPPER(TRIM(m.codigo_producto)) = UPPER(TRIM(p.CODIGO))
                    )
                    ORDER BY p.CODIGO ASC
                    LIMIT :lim
                    """
                ),
                {"lim": min(limite, 50)},
            ).fetchall()
            sin_movimiento = [
                {"codigo": (r[0] or "").strip().upper(), "descripcion": (r[1] or "")[:120]}
                for r in sm
            ]
        except Exception:
            sin_movimiento = []

        movimiento_antiguo = []
        try:
            neg = f"-{int(dias_sin_mov)} days"
            ma = sess.execute(
                text(
                    """
                    SELECT p.CODIGO, p.DESCRIPCION, MAX(m.fecha) AS ultima
                    FROM productos p
                    JOIN movimientos_stock m ON UPPER(TRIM(m.codigo_producto)) = UPPER(TRIM(p.CODIGO))
                    WHERE p.ACTIVO = 1
                    GROUP BY p.CODIGO
                    HAVING MAX(m.fecha) < datetime('now', :neg_days)
                    ORDER BY MAX(m.fecha) ASC
                    LIMIT :lim
                    """
                ),
                {"neg_days": neg, "lim": min(limite, 50)},
            ).fetchall()
            movimiento_antiguo = [
                {
                    "codigo": (r[0] or "").strip().upper(),
                    "descripcion": (r[1] or "")[:120],
                    "ultima": str(r[2])[:19] if r[2] else "",
                }
                for r in ma
            ]
        except Exception:
            movimiento_antiguo = []

        listas = {
            "sin_imagen": lista_sin_imagen,
            "sin_categoria": lista_sin_cat,
            "sin_oem": lista_sin_oem,
            "sin_homologados": lista_sin_homo,
            "stock_cero": lista_stock_cero,
            "stock_bajo": lista_stock_bajo,
            "sin_movimiento": sin_movimiento,
            "movimiento_antiguo": movimiento_antiguo,
        }

        return render_template(
            "productos/panel_calidad.html",
            total_activos=total_activos,
            kpis=kpis,
            listas=listas,
            duplicados_oem=duplicados_oem,
            online_users=_online_users(),
            active_page="productos_panel_calidad",
            params={
                "limite": limite,
                "stock_critico": stock_critico,
                "dias_sin_movimiento": dias_sin_mov,
            },
        )
    finally:
        sess.close()