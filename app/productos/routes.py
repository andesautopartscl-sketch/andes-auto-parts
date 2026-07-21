from flask import Blueprint, current_app, render_template, request, session, send_file, jsonify, make_response, url_for, redirect
from datetime import datetime, timedelta
import re
import time
import unicodedata
import json
from pathlib import Path

from markupsafe import Markup, escape
from sqlalchemy import and_, case, func, literal, or_, text
from sqlalchemy.orm import joinedload, noload
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
from app.bodega.models import (
    IngresoDocumento,
    IngresoDocumentoItem,
    MovimientoStock,
    ProductoVarianteStock,
)
from ..utils.decorators import login_required, admin_required
from app.utils.permissions import DEFAULT_PERMISSIONS, get_user_permissions
from app.utils.finance_visibility import redact_compra_historial_row, user_can_view_finanzas
from ..import_excel import import_products_from_excel
from ..utils.product_audit import build_diffs, register_product_audit, resolve_producto_audit_action_filter
from ..utils.variante_comercial import merge_ingreso_ref_variante_overrides
from ..utils.precio_lista import batch_precio_neto_desde_ingreso_o_variante
from ..utils.categoria_autodetect import (
    auto_asignar_categoria_si_vacio as _auto_asignar_categoria_si_vacio,
    bulk_auto_asignar_categorias_faltantes,
)
from ..utils.product_image_postprocess import process_uploaded_image
from ..utils.catalog_cache import (
    get_or_load,
    get_or_load_ttl,
    invalidate_ficha_despiece,
    invalidate_ficha_despiece_for_oem,
    invalidate_taxonomia,
)
from ..utils.cloudinary_config import is_configured as cloudinary_is_configured
from ..utils.cloudinary_config import upload_image as cloudinary_upload_image
from ..utils.cloudinary_config import delete_image_by_url
from ..utils.cloudinary_config import image_ref_dedupe_key
from ..utils.cloudinary_product_import import (
    cloudinary_storage_key,
    codigo_from_filename,
    describe_upload_file,
    download_image_from_url,
    find_producto_by_image_code,
    link_cloudinary_url_to_despiece,
    link_cloudinary_url_to_360,
    link_cloudinary_url_to_producto,
    list_cloudinary_product_urls_by_storage_key,
    imagen_ordenes_para_producto,
    build_import_public_id,
    normalize_tipo_imagen,
    producto_resolver_payload,
    resolve_upload_extension,
    resolver_producto_por_codigo,
    search_productos_for_assign,
    TIPO_IMAGEN_360,
    TIPO_IMAGEN_DESPIECE,
    TIPO_IMAGEN_PRODUCTO,
)
from ..utils.cloudinary_static_map import keys_with_prefix
from ..utils.product_image_url import (
    is_remote_image_url,
    normalize_stored_image_ref,
    product_image_src,
    static_filename_from_ref,
)


productos_bp = Blueprint("productos", __name__)
BUSCAR_PER_PAGE_DEFAULT = 50
BUSCAR_PER_PAGE_MAX = 50
MAX_IMAGE_UPLOAD_BYTES = 8 * 1024 * 1024
ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif"}


def _validate_image_upload(file_obj, allowed_exts: set[str] | None = None, max_bytes: int = MAX_IMAGE_UPLOAD_BYTES) -> str:
    if not file_obj or not getattr(file_obj, "filename", None):
        raise ValueError("Archivo inválido")
    filename = secure_filename(file_obj.filename)
    ext = (filename.rsplit(".", 1)[-1].lower() if "." in filename else "")
    if not ext or ext not in (allowed_exts or ALLOWED_IMAGE_EXTENSIONS):
        raise ValueError("Formato de imagen no permitido")
    stream = getattr(file_obj, "stream", None)
    if stream is not None:
        current_pos = stream.tell()
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(current_pos)
        if size <= 0:
            raise ValueError("Archivo vacío")
        if size > max_bytes:
            raise ValueError("La imagen excede el tamaño máximo permitido")
    return ext


def _wants_modal_fragment() -> bool:
    """True when UI requests a partial HTML fragment (e.g. app modal), not a full page."""
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    # Some embedded iframes request full HTML without XHR headers.
    return (request.args.get("embed") or "").strip() == "1"


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
    """Busca despiece por OEM del producto (compartido) y luego por código interno."""
    cod = (getattr(producto, "codigo", None) or "").strip().upper()
    oem = _norm_oem_despiece(getattr(producto, "codigo_oem", None))
    if oem:
        try:
            row_oem = db.query(OemDespiece).filter(OemDespiece.oem_norm == oem).first()
            if row_oem:
                return row_oem
        except Exception:
            pass
    if cod:
        try:
            return db.query(OemDespiece).filter(OemDespiece.producto_codigo == cod).first()
        except Exception:
            pass
    return None


def _find_shared_despiece_image_fallback(db, producto: Producto) -> str | None:
    """
    Cuando el despiece OEM compartido no tiene imagen, intenta reutilizar una imagen
    de un despiece legado por código interno (_INT_*) de cualquier producto con el mismo OEM.
    """
    oem = _norm_oem_despiece(getattr(producto, "codigo_oem", None))
    if not oem:
        return None
    try:
        siblings = (
            db.query(Producto.codigo)
            .filter(func.upper(func.trim(Producto.codigo_oem)) == oem)
            .all()
        )
        sibling_codes = [((r[0] or "").strip().upper()) for r in siblings if (r and r[0])]
        if not sibling_codes:
            return None
        legacy_row = (
            db.query(OemDespiece)
            .filter(
                OemDespiece.producto_codigo.in_(sibling_codes),
                OemDespiece.imagen_static.isnot(None),
            )
            .order_by(OemDespiece.updated_at.desc(), OemDespiece.id.desc())
            .first()
        )
        if legacy_row and (legacy_row.imagen_static or "").strip():
            return legacy_row.imagen_static.strip()
    except Exception:
        return None
    return None


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
    Relaciona archivo de imagen con OEM / código interno / alternativo sin falsos positivos.

    Problema evitado: código corto numérico «1000» no debe coincidir con «DF100K4DAA01000.png»
    (subcadena «1000» al final del nombre de otro repuesto).

    Reglas:
    - Nombre sin extensión igual al needle (ej. 1000.jpg).
    - O prefijo needle + '_' o '-' (ej. 2417_1.png, 3770100-e06_2.png).
    - Subcadena (literal o compacta sin espacios / _ / -) solo si el needle medido en compacto
      tiene longitud >= 8, típico de OEM o referencias largas.
    """
    n = (needle or "").strip().lower()
    if not n:
        return False
    stem = nombre_archivo_lower.rsplit(".", 1)[0] if "." in nombre_archivo_lower else nombre_archivo_lower
    if stem == n:
        return True
    if stem.startswith(n + "_") or stem.startswith(n + "-"):
        return True
    compact_needle = n.replace(" ", "").replace("_", "").replace("-", "")
    if len(compact_needle) < 8:
        return False
    if n in stem:
        return True
    compact_stem = stem.replace(" ", "").replace("_", "").replace("-", "")
    return compact_needle in compact_stem


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


STATIC_PRODUCTOS_IMG = Path(__file__).resolve().parent.parent / "static" / "productos_img"


def _delete_product_image_ref(ref: str | None) -> None:
    """Elimina imagen previa (Cloudinary o archivo local)."""
    r = (ref or "").strip()
    if not r:
        return
    if is_remote_image_url(r):
        if cloudinary_is_configured():
            delete_image_by_url(r)
        return
    rel = static_filename_from_ref(r)
    if not rel:
        return
    path = Path(__file__).resolve().parent.parent / "static" / rel.replace("/", os.sep)
    if path.is_file():
        try:
            path.unlink()
        except OSError:
            pass


def _upload_product_image_file(
    file_obj,
    *,
    codigo: str,
    suffix: str,
    allowed_exts: set[str] | None = None,
    storage_codigo: str | None = None,
    archivo_nombre: str = "",
    cloud_folder: str = "andes_erp/productos",
    public_id_path: str | None = None,
    local_subdir: str | None = None,
) -> str | None:
    """
    Sube imagen a Cloudinary (si está configurado) o disco local.
    Retorna URL https o ruta relativa productos_img/...
    storage_codigo: nombre en Cloudinary/disco (p. ej. OEM); si no se indica, usa codigo.
    """
    if not file_obj:
        raise ValueError("No se recibió archivo de imagen")
    allowed = allowed_exts or ALLOWED_IMAGE_EXTENSIONS
    ext, _resolved_name = resolve_upload_extension(
        file_obj,
        fallback_filename=archivo_nombre or getattr(file_obj, "filename", None) or "",
        allowed_exts=allowed,
    )
    # Validar tamaño (reutiliza lógica existente con nombre ya resuelto)
    class _NamedProxy:
        def __init__(self, inner, filename: str):
            self._inner = inner
            self.filename = filename
            self.stream = getattr(inner, "stream", inner)
            self.content_type = getattr(inner, "content_type", None) or getattr(inner, "mimetype", None)

        def save(self, dst):
            return self._inner.save(dst)

    proxy = _NamedProxy(file_obj, _resolved_name)
    _validate_image_upload(proxy, allowed_exts=allowed)

    stem = re.sub(r"[^a-zA-Z0-9_\-]", "_", (storage_codigo or codigo or "").strip().upper()) or "producto"
    target_name = f"{stem}{suffix}.{ext}"

    if cloudinary_is_configured():
        import tempfile

        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}") as tmp:
                file_obj.save(tmp.name)
                tmp_path = Path(tmp.name)
            processed = process_uploaded_image(tmp_path)
            upload_path = processed if processed is not None else tmp_path
            if public_id_path:
                public_id = public_id_path
            else:
                public_id = f"{cloud_folder}/{Path(target_name).stem}"
            result = cloudinary_upload_image(upload_path, public_id=public_id)
            url = normalize_stored_image_ref(result.get("url"))
            if processed is not None and processed != tmp_path and processed.is_file():
                processed.unlink(missing_ok=True)
            if not url:
                raise ValueError("Cloudinary no devolvió URL de la imagen")
            return url
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"Error al subir a Cloudinary: {exc}") from exc
        finally:
            if tmp_path and tmp_path.is_file():
                tmp_path.unlink(missing_ok=True)
        return None

    if local_subdir == "productos360":
        codigo_dir = re.sub(r"[^a-zA-Z0-9_\-]", "_", (codigo or "").strip().upper()) or "producto"
        static_dir = STATIC_PRODUCTOS_IMG.parent / "productos360" / codigo_dir
    elif local_subdir == "epc_despiece":
        static_dir = STATIC_PRODUCTOS_IMG.parent / "epc_despiece"
    else:
        static_dir = STATIC_PRODUCTOS_IMG
    static_dir.mkdir(parents=True, exist_ok=True)
    target = static_dir / target_name
    file_obj.save(str(target))
    out = process_uploaded_image(target)
    if out is not None:
        target_name = out.name
    if local_subdir == "productos360":
        codigo_dir = re.sub(r"[^a-zA-Z0-9_\-]", "_", (codigo or "").strip().upper()) or "producto"
        return f"productos360/{codigo_dir}/{target_name}"
    if local_subdir == "epc_despiece":
        return f"epc_despiece/{target_name}"
    return f"productos_img/{target_name}"


def _producto_imagen_sort_key(img) -> tuple:
    """Orden de galería: orden numérico (0=portada), desempate id."""
    raw = getattr(img, "orden", None)
    try:
        orden = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        orden = None
    if orden is None:
        orden = 0 if getattr(img, "es_principal", False) else 999
    return (orden, getattr(img, "id", 0) or 0)


def _imagenes_por_oem_compartido(producto: Producto) -> list[str]:
    """Hereda imágenes de otro producto activo con el mismo OEM."""
    oem = (producto.codigo_oem or "").strip().upper()
    if not oem:
        return []
    try:
        from sqlalchemy.orm import object_session

        sess = object_session(producto)
        if sess is None:
            return []
        rows = (
            sess.query(ProductoImagen)
            .join(Producto, ProductoImagen.producto_codigo == Producto.codigo)
            .filter(Producto.activo.is_(True))
            .filter(func.upper(func.trim(Producto.codigo_oem)) == oem)
            .filter(ProductoImagen.ruta.isnot(None))
            .filter(ProductoImagen.ruta != "")
            .order_by(ProductoImagen.orden.asc(), ProductoImagen.id.asc())
            .all()
        )
        seen_keys: set[str] = set()
        out: list[str] = []
        rows_sorted = sorted(rows, key=_producto_imagen_sort_key)
        for r in rows_sorted:
            url = (r.ruta or "").strip()
            if not url:
                continue
            key = image_ref_dedupe_key(url) or url.lower()
            if key in seen_keys:
                continue
            seen_keys.add(key)
            out.append(url)
        return out
    except Exception:
        return []


def _collect_imagenes_producto(producto: Producto) -> list[str]:
    """URLs o rutas relativas para mostrar imágenes (BD + carpeta local)."""
    seen: set[str] = set()
    out: list[str] = []

    def add(val: str | None) -> None:
        v = (val or "").strip()
        if not v:
            return
        key = image_ref_dedupe_key(v) or v.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(v)

    db_rows: list[str] = []
    own_rutas: list[str] = []
    try:
        ordered = sorted(
            producto.imagenes or [],
            key=_producto_imagen_sort_key,
        )
        for img in ordered:
            if img.ruta:
                own_rutas.append((img.ruta or "").strip())
    except Exception:
        pass
    # Siempre usar OEM compartido si el producto tiene OEM configurado
    # (incluye las propias + las de otros productos con mismo OEM)
    oem = (getattr(producto, "codigo_oem", "") or "").strip()
    if oem:
        db_rows = _imagenes_por_oem_compartido(producto)
        # Si OEM no devolvió nada (no hay query results), usar propias
        if not db_rows:
            db_rows = own_rutas
    else:
        db_rows = own_rutas
    if not db_rows and getattr(producto, "imagen_url", None):
        db_rows.append((producto.imagen_url or "").strip())
    for r in db_rows:
        add(r)

    folder = str(STATIC_PRODUCTOS_IMG)
    if STATIC_PRODUCTOS_IMG.is_dir():
        for name in _collect_imagenes_producto_carpeta(folder, producto):
            add(f"productos_img/{name}")

    if not out and cloudinary_is_configured():
        storage_keys: list[str] = []
        sk_oem = cloudinary_storage_key(producto)
        if sk_oem:
            storage_keys.append(sk_oem)
        codigo_interno = re.sub(
            r"[^a-zA-Z0-9_\-]", "_", (producto.codigo or "").strip().upper()
        )
        if codigo_interno and codigo_interno not in storage_keys:
            storage_keys.append(codigo_interno)
        for sk in storage_keys:
            for url in list_cloudinary_product_urls_by_storage_key(sk):
                add(url)
            if out:
                break
    return out


def _galeria_imagenes_query(sess, producto: Producto):
    """Filas ProductoImagen visibles en galería (mismo OEM compartido o solo este código)."""
    oem = (producto.codigo_oem or "").strip().upper()
    codigo = (producto.codigo or "").strip().upper()
    q = (
        sess.query(ProductoImagen)
        .join(Producto, ProductoImagen.producto_codigo == Producto.codigo)
        .filter(Producto.activo.is_(True))
        .filter(ProductoImagen.ruta.isnot(None))
        .filter(ProductoImagen.ruta != "")
    )
    if oem:
        q = q.filter(func.upper(func.trim(Producto.codigo_oem)) == oem)
    elif codigo:
        q = q.filter(func.upper(func.trim(Producto.codigo)) == codigo)
    else:
        return []
    return q.all()


def _galeria_edit_payload(sess, producto: Producto) -> dict:
    """Imágenes vinculadas + huérfanas en Cloudinary para el gestor en edición."""
    rows = sorted(_galeria_imagenes_query(sess, producto), key=_producto_imagen_sort_key)
    seen_keys: set[str] = set()
    imagenes: list[dict] = []
    for row in rows:
        ruta = (row.ruta or "").strip()
        if not ruta:
            continue
        key = image_ref_dedupe_key(ruta) or ruta.lower()
        if key in seen_keys:
            continue
        seen_keys.add(key)
        try:
            orden = int(row.orden) if row.orden is not None else 999
        except (TypeError, ValueError):
            orden = 999
        imagenes.append(
            {
                "id": row.id,
                "ruta": ruta,
                "url": product_image_src(ruta),
                "orden": orden,
                "es_principal": bool(getattr(row, "es_principal", False)),
                "producto_codigo": (row.producto_codigo or "").strip().upper(),
            }
        )
    imagenes.sort(key=lambda x: (x["orden"], x["id"]))

    huerfanas: list[dict] = []
    if cloudinary_is_configured():
        keys_try: list[str] = []
        sk = cloudinary_storage_key(producto)
        if sk:
            keys_try.append(sk)
        codigo_sk = re.sub(
            r"[^a-zA-Z0-9_\-]", "_", (producto.codigo or "").strip().upper()
        )
        if codigo_sk and codigo_sk not in keys_try:
            keys_try.append(codigo_sk)
        for storage_key in keys_try:
            for url in list_cloudinary_product_urls_by_storage_key(storage_key):
                ukey = image_ref_dedupe_key(url) or url.lower()
                if ukey in seen_keys:
                    continue
                seen_keys.add(ukey)
                huerfanas.append({"url": url, "thumb": product_image_src(url)})

    ord_info = imagen_ordenes_para_producto(sess, producto)
    return {
        "imagenes": imagenes,
        "huerfanas": huerfanas,
        "imagen_siguiente_orden": ord_info.get("imagen_siguiente_orden", 0),
    }


def _galeria_row_en_scope(sess, producto: Producto, img_id: int) -> ProductoImagen | None:
    if not img_id:
        return None
    scope_ids = {r.id for r in _galeria_imagenes_query(sess, producto)}
    row = sess.query(ProductoImagen).filter(ProductoImagen.id == img_id).first()
    if row and row.id in scope_ids:
        return row
    return None


def _collect_imagenes_360(producto: Producto) -> list[str]:
    """Nombres de archivo 360; el template arma productos360/CODIGO/archivo."""
    codigo = (producto.codigo or "").strip()
    if not codigo:
        return []
    prefix = f"productos360/{codigo}/"
    seen: set[str] = set()
    out: list[str] = []

    for key in keys_with_prefix(prefix):
        basename = key.rsplit("/", 1)[-1]
        if basename and basename not in seen:
            seen.add(basename)
            out.append(basename)

    folder = STATIC_PRODUCTOS_IMG.parent / "productos360" / codigo
    if folder.is_dir():
        for archivo in sorted(os.listdir(folder)):
            low = archivo.lower()
            if low.endswith((".jpg", ".jpeg", ".png", ".webp")) and archivo not in seen:
                seen.add(archivo)
                out.append(archivo)

    def _basename_from_360_ref(ref: str) -> str | None:
        r = (ref or "").strip()
        if not r:
            return None
        marker = f"productos360/{codigo}/"
        if r.startswith(marker):
            base = r[len(marker) :].split("?")[0].strip()
            return base or None
        if "productos360/" in r and f"/{codigo}/" in r:
            tail = r.split(f"/{codigo}/", 1)[-1]
            base = tail.split("?")[0].strip()
            return base or None
        return None

    try:
        for img in producto.imagenes or []:
            base = _basename_from_360_ref(getattr(img, "ruta", None))
            if base and base not in seen:
                seen.add(base)
                out.append(base)
    except Exception:
        pass
    return sorted(out, key=str.lower)


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
            invalidate_taxonomia()
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
            invalidate_taxonomia()
        subcategoria_id = sub.id

    return categoria_id, subcategoria_id


def _save_uploaded_images(codigo: str, files: list, producto: Producto | None = None) -> list[str]:
    if not codigo or not files:
        return []
    rutas: list[str] = []
    for idx, f in enumerate(files):
        if idx == 0 and producto is not None:
            if getattr(producto, "imagen_url", None):
                _delete_product_image_ref(producto.imagen_url)
            for old in list(producto.imagenes or []):
                if old.ruta:
                    _delete_product_image_ref(old.ruta)
        suffix = "" if idx == 0 else f"_{idx + 1}"
        stored = _upload_product_image_file(
            f,
            codigo=codigo,
            suffix=suffix,
            allowed_exts={"jpg", "jpeg", "png", "webp"},
        )
        if stored:
            rutas.append(stored)
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


# Solo para ordenar resultados multi-palabra; el AND de búsqueda sigue usando todos los tokens.
_ORDEN_BUSCAR_SKIP_TOKENS = frozenset(
    {
        "de",
        "del",
        "el",
        "la",
        "los",
        "las",
        "un",
        "una",
        "unos",
        "unas",
        "y",
        "o",
        "en",
        "con",
        "por",
        "a",
    }
)


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


def _producto_buscar_load_options():
    """Evita JOINs eager (etiquetas, categoría, imágenes) en listados de búsqueda."""
    return (
        noload(Producto.etiquetas),
        noload(Producto.categoria_rel),
        noload(Producto.subcategoria_rel),
        noload(Producto.imagenes),
    )


def _producto_q(sess, *options_extra):
    opts = _producto_buscar_load_options()
    if options_extra:
        opts = opts + options_extra
    return sess.query(Producto).options(*opts)


def _paginate_producto_query(query, page: int, per_page: int):
    """Pagina resultados; omite COUNT si la página actual es la última (parcial)."""
    offset = (page - 1) * per_page
    rows = query.offset(offset).limit(per_page).all()
    if len(rows) < per_page:
        total = offset + len(rows)
        pages = max(1, page) if total else 1
        return rows, total, pages
    total = query.order_by(None).count()
    pages = max(1, (total + per_page - 1) // per_page) if total else 1
    page = max(1, min(page, pages))
    return rows, total, pages


def _load_taxonomia_productos(sess):
    categorias = sess.query(Categoria).order_by(Categoria.nombre.asc()).all()
    subs = sess.query(Subcategoria).order_by(Subcategoria.nombre.asc()).all()
    subs_by_cat: dict[int, list] = {}
    for sub in subs:
        if sub.categoria_id is not None:
            subs_by_cat.setdefault(sub.categoria_id, []).append(sub)
    data = []
    for cat in categorias:
        cat_subs = subs_by_cat.get(cat.id, [])
        data.append(
            {
                "id": cat.id,
                "nombre": cat.nombre or "",
                "subcategorias": [
                    {"id": s.id, "nombre": s.nombre or ""} for s in cat_subs
                ],
            }
        )
    return data


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

    t0 = time.time()
    q = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int) or 1
    per_page = request.args.get("per_page", BUSCAR_PER_PAGE_DEFAULT, type=int) or BUSCAR_PER_PAGE_DEFAULT
    per_page = max(25, min(int(per_page), BUSCAR_PER_PAGE_MAX))

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

        query = _producto_q(sess).filter(Producto.activo.is_(True))
        productos = []
        total_count = 0
        total_pages = 1

        is_exact_codigo_search = False
        _fts_used = False
        if q:
            termino = q.lower().strip()
            # Separar por espacios, comas o ; (p. ej. "sen, pos, cig vigu 2.4")
            palabras = [p for p in re.split(r"[\s,;]+", termino) if p]

            qstrip = q.strip()

            # Un solo término que coincide al 100 % con un código interno activo → solo ese ítem
            # (evita ruido por OEM/descripciones que contienen la misma subcadena, p. ej. "2903").
            exacto_codigo_interno = None
            if len(palabras) == 1 and palabras[0].strip():
                codigo_token = palabras[0].strip()
                exacto_codigo_interno = (
                    _producto_q(sess)
                    .filter(Producto.activo.is_(True))
                    .filter(func.upper(func.trim(Producto.codigo)) == codigo_token.upper())
                    .first()
                )
                is_exact_codigo_search = exacto_codigo_interno is not None

            exacto_modelo = None
            exacto_oem = None

            if exacto_codigo_interno is not None:
                # PK del modelo es CODIGO (no hay columna id).
                query = (
                    _producto_q(sess)
                    .filter(Producto.activo.is_(True))
                    .filter(Producto.codigo == exacto_codigo_interno.codigo)
                    .order_by(Producto.codigo.asc())
                )
            else:
                # Texto completo = modelo exacto (ej. "WINGLE 5 VGT 2.0 DIESEL GREAT WALL") →
                # todos los repuestos de ese vehículo, ordenados por descripción.
                if qstrip:
                    exacto_modelo = (
                        _producto_q(sess)
                        .filter(Producto.activo.is_(True))
                        .filter(Producto.modelo.isnot(None))
                        .filter(func.trim(Producto.modelo) != "")
                        .filter(func.upper(func.trim(Producto.modelo)) == qstrip.upper())
                        .first()
                    )
            if exacto_modelo is not None:
                _desc_orden = func.lower(
                    func.coalesce(func.trim(Producto.descripcion), "")
                )
                query = (
                    _producto_q(sess)
                    .filter(Producto.activo.is_(True))
                    .filter(func.upper(func.trim(Producto.modelo)) == qstrip.upper())
                    .order_by(_desc_orden, Producto.codigo.asc())
                )
            elif exacto_codigo_interno is None:
                # Texto completo = OEM exacto (ej. "T15-1109111") →
                # todos los ítems con ese OEM, por modelo alfabético.
                if qstrip:
                    exacto_oem = (
                        _producto_q(sess)
                        .filter(Producto.activo.is_(True))
                        .filter(Producto.codigo_oem.isnot(None))
                        .filter(func.trim(Producto.codigo_oem) != "")
                        .filter(func.upper(func.trim(Producto.codigo_oem)) == qstrip.upper())
                        .first()
                    )
            if exacto_oem is not None:
                _modelo_orden_oem = func.lower(
                    func.coalesce(func.trim(Producto.modelo), "")
                )
                _desc_orden_oem = func.lower(
                    func.coalesce(func.trim(Producto.descripcion), "")
                )
                query = (
                    _producto_q(sess)
                    .filter(Producto.activo.is_(True))
                    .filter(func.upper(func.trim(Producto.codigo_oem)) == qstrip.upper())
                    .order_by(_modelo_orden_oem, _desc_orden_oem, Producto.codigo.asc())
                )
            elif exacto_codigo_interno is None and exacto_modelo is None:
                # Cada palabra debe aparecer en al menos un campo (AND entre palabras, OR entre columnas).
                # Comparación "plegada" (sin tildes / ü / ñ): "cigueñal" encuentra "CIGÜEÑAL"; "sen" encuentra "SENSOR".
                def _token_match_any_column(palabra: str):
                    p_norm = _norm_busqueda_token(palabra)
                    if not p_norm:
                        return literal(True)
                    return _fold_like_contains(_producto_busqueda_blob_expr(), p_norm)

                _fts_used = False
                try:
                    from app.utils.fts_productos import fts_match_query

                    fts_terms = fts_match_query(palabras)
                    if fts_terms.strip():
                        fts_rows = sess.execute(
                            text(
                                "SELECT codigo FROM productos_fts "
                                "WHERE blob MATCH :q ORDER BY rank"
                            ),
                            {"q": fts_terms},
                        ).fetchall()
                        codigos_fts = [r[0] for r in fts_rows]
                        if codigos_fts:
                            total_count = len(codigos_fts)
                            total_pages = max(
                                1, (total_count + per_page - 1) // per_page
                            )
                            page = max(1, min(page, total_pages))
                            page_codes = codigos_fts[
                                (page - 1) * per_page : page * per_page
                            ]
                            prods_dict = {
                                p.codigo: p
                                for p in _producto_q(sess)
                                .filter(Producto.activo.is_(True))
                                .filter(Producto.codigo.in_(page_codes))
                                .all()
                            }
                            productos = [
                                prods_dict[c] for c in page_codes if c in prods_dict
                            ]
                            _fts_used = True
                except Exception:
                    pass

                if not _fts_used:
                    if len(palabras) == 1:
                        query = query.filter(_token_match_any_column(palabras[0]))
                    else:
                        query = query.filter(
                            and_(*(_token_match_any_column(p) for p in palabras))
                        )

                    compact_term = termino.replace(" ", "")
                    is_numeric = compact_term.isdigit()

                    if len(palabras) > 1:
                        # Relevancia: más palabras (útiles) en modelo/motor/descripción arriba; desempate código.
                        modelo_pts = None
                        motor_pts = None
                        desc_pts = None
                        for p in palabras:
                            pn = _norm_busqueda_token(p)
                            if not pn or pn in _ORDEN_BUSCAR_SKIP_TOKENS:
                                continue
                            cm = case((_fold_like_contains(Producto.modelo, pn), 1), else_=0)
                            cmt = case((_fold_like_contains(Producto.motor, pn), 1), else_=0)
                            cd = case((_fold_like_contains(Producto.descripcion, pn), 1), else_=0)
                            modelo_pts = cm if modelo_pts is None else modelo_pts + cm
                            motor_pts = cmt if motor_pts is None else motor_pts + cmt
                            desc_pts = cd if desc_pts is None else desc_pts + cd
                        if modelo_pts is None:
                            modelo_pts = literal(0)
                        if motor_pts is None:
                            motor_pts = literal(0)
                        if desc_pts is None:
                            desc_pts = literal(0)
                        query = query.order_by(
                            modelo_pts.desc(),
                            motor_pts.desc(),
                            desc_pts.desc(),
                            Producto.codigo.asc(),
                        )
                    elif is_numeric:
                        priority = case(
                            (Producto.codigo.ilike(f"{compact_term}%"), 0),
                            (Producto.codigo_oem.ilike(f"{compact_term}%"), 1),
                            (
                                _fold_like_contains(
                                    Producto.descripcion, _norm_busqueda_token(termino)
                                ),
                                2,
                            ),
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

        if is_exact_codigo_search:
            total_count = 1
            total_pages = 1
            page = 1
            productos = query.limit(1).all()
        elif _fts_used:
            page = max(1, min(page, total_pages))
        else:
            productos, total_count, total_pages = _paginate_producto_query(
                query, page, per_page
            )
            page = max(1, min(page, total_pages))

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

        sin_precio_cat = [
            (p.codigo or "").strip().upper()
            for p in productos
            if (p.codigo or "").strip() and not float(p.p_publico or 0)
        ]
        if sin_precio_cat:
            precio_fallback = batch_precio_neto_desde_ingreso_o_variante(
                security_db.session, sin_precio_cat
            )
            for p in productos:
                key = (p.codigo or "").strip().upper()
                if key and not float(p.p_publico or 0):
                    pv = precio_fallback.get(key)
                    if pv:
                        p.p_publico = pv

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

        try:
            current_app.logger.info(
                "Búsqueda productos: %.3fs | término=%r | resultados=%s | página=%s/%s | per_page=%s",
                time.time() - t0,
                q,
                total_count,
                page,
                total_pages,
                per_page,
            )
        except Exception:
            pass

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
        )
    except Exception as exc:
        print("ERROR EN BUSCAR:", exc)
        try:
            current_app.logger.exception(
                "Búsqueda productos falló en %.3fs | término=%r",
                time.time() - t0,
                q,
            )
        except Exception:
            pass
        return render_template(
            "buscar.html",
            productos=[],
            q=q,
            page=1,
            per_page=BUSCAR_PER_PAGE_DEFAULT,
            total_count=0,
            total_pages=1,
            session=session,
            stock_total=stock_total,
            variant_map=variant_map,
            highlight_match=highlight_match,
            online_users=online_users,
            active_page="productos_buscar",
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
        data = get_or_load("taxonomia_productos", lambda: _load_taxonomia_productos(sess))
        return jsonify({"success": True, "categorias": data})
    finally:
        sess.close()


def _ultimo_ingreso_ref_variante(codigo: str, marca: str | None, bodega: str | None) -> dict | None:
    """
    Costo neto promedio ponderado y precio público neto (última línea o derivado de margen)
    para código + marca + bodega. Ignora ingresos anulados.
    """
    code = (codigo or "").strip().upper()
    if not code:
        return None
    marca_n = (marca or "").strip().upper()
    bodega_n = (bodega or "").strip() or "Bodega 1"

    q = (
        security_db.session.query(IngresoDocumentoItem)
        .join(IngresoDocumento, IngresoDocumento.id == IngresoDocumentoItem.ingreso_documento_id)
        .filter(or_(IngresoDocumento.anulado.is_(False), IngresoDocumento.anulado.is_(None)))
        .filter(func.upper(IngresoDocumentoItem.codigo_producto) == code)
        .filter(IngresoDocumentoItem.bodega == bodega_n)
    )
    if marca_n:
        q = q.filter(func.upper(func.trim(IngresoDocumentoItem.marca)) == marca_n)
    else:
        q = q.filter(
            or_(
                IngresoDocumentoItem.marca.is_(None),
                IngresoDocumentoItem.marca == "",
                func.upper(func.trim(IngresoDocumentoItem.marca)) == "",
            )
        )

    rows = q.all()
    if not rows:
        return None

    total_qty = 0
    total_vn = 0.0
    for it in rows:
        qty = int(it.cantidad or 0)
        vn = float(it.valor_neto or 0)
        if qty > 0 and vn > 0:
            total_qty += qty
            total_vn += qty * vn
    costo_u = (total_vn / total_qty) if total_qty > 0 and total_vn > 0 else None

    item = q.order_by(IngresoDocumento.created_at.desc(), IngresoDocumentoItem.id.desc()).first()
    pv = item.precio_venta_neto if item is not None else None
    mg = item.margen_pct if item is not None else None
    precio_sug: float | None = None
    if pv is not None:
        precio_sug = round(float(pv), 2)
    elif costo_u is not None and mg is not None and float(mg) < 100:
        denom = 1.0 - float(mg) / 100.0
        if denom > 0:
            precio_sug = round(costo_u / denom, 2)

    return {
        "costo_unitario_neto": round(costo_u, 2) if costo_u is not None else None,
        "precio_sugerido_neto": precio_sug,
        "margen_registrado_pct": float(mg) if mg is not None else None,
    }


def _resolver_producto_busqueda_edicion(sess, q_raw: str) -> Producto | None:
    """
    Orden: código interno exacto → OEM exacto → alternativo (contiene, min 2 chars) → descripción (contiene, min 2 chars).
    Un solo carácter solo puede resolver por código u OEM exacto.
    """
    q = (q_raw or "").strip()
    if not q:
        return None
    qu = q.upper()
    opts = (
        joinedload(Producto.categoria_rel),
        joinedload(Producto.subcategoria_rel),
    )
    base = (
        sess.query(Producto)
        .options(*opts)
        .filter(Producto.activo.is_(True))
    )

    p = base.filter(func.upper(func.trim(Producto.codigo)) == qu).first()
    if p:
        return p
    p = base.filter(
        Producto.codigo_oem.isnot(None),
        func.upper(func.trim(Producto.codigo_oem)) == qu,
    ).first()
    if p:
        return p
    if len(q) >= 2:
        p = (
            base.filter(
                Producto.codigo_alternativo.isnot(None),
                Producto.codigo_alternativo != "",
            )
            .filter(func.instr(func.upper(Producto.codigo_alternativo), qu) > 0)
            .order_by(Producto.codigo.asc())
            .first()
        )
        if p:
            return p
        p = (
            base.filter(Producto.descripcion.isnot(None))
            .filter(func.instr(func.upper(Producto.descripcion), qu) > 0)
            .order_by(Producto.codigo.asc())
            .first()
        )
        if p:
            return p
    return None


@productos_bp.route("/productos/buscar_para_editar", methods=["POST"])
@admin_required
def buscar_para_editar():
    q_in = (request.form.get("q") or request.form.get("codigo") or "").strip()
    if not q_in:
        return jsonify({"success": False, "error": "Ingresa un termino de busqueda."}), 400

    sess = SessionDB()
    try:
        producto = _resolver_producto_busqueda_edicion(sess, q_in)
        if producto is None:
            return jsonify({"success": False, "error": "No encontrado"}), 404

        codigo = (producto.codigo or "").strip().upper()

        # Total alineado a bodega/ventas: variantes en productos_variantes_stock + tránsito; si no hay variantes, columnas legacy.
        ficha_stock = _ficha_stock_repuestos(producto)
        stock = int(ficha_stock.get("stotal") or 0)

        variantes_rows = (
            security_db.session.query(ProductoVarianteStock)
            .filter(func.upper(ProductoVarianteStock.codigo_producto) == codigo)
            .order_by(ProductoVarianteStock.bodega.asc(), ProductoVarianteStock.marca.asc())
            .all()
        )
        variantes_stock = []
        for r in variantes_rows:
            marca_v = (r.marca or "").strip() or "—"
            bodega_v = (r.bodega or "").strip() or "Bodega 1"
            ref_raw = _ultimo_ingreso_ref_variante(codigo, r.marca, r.bodega)
            merged = merge_ingreso_ref_variante_overrides(
                ref_raw,
                r.margen_override_pct,
                r.precio_publico_neto_override,
            )
            variantes_stock.append(
                {
                    "variante_id": r.id,
                    "marca": marca_v,
                    "bodega": bodega_v,
                    "stock": int(r.stock or 0),
                    "costo_ingreso_neto": merged.get("costo_unitario_neto"),
                    "margen_pct": merged.get("margen_registrado_pct"),
                    "precio_publico_neto": merged.get("precio_sugerido_neto"),
                }
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
            "stock": stock,
            "variantes_stock": variantes_stock,
            "oem": producto.codigo_oem or "",
            "alternativo": producto.codigo_alternativo or "",
            "homologados": producto.homologados or "",
            "activo": bool(producto.activo),
            "estado": "ACTIVO" if producto.activo else "INACTIVO",
            "galeria": _galeria_edit_payload(sess, producto),
        })
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500
    finally:
        sess.close()


@productos_bp.route("/productos/<codigo>/imagenes/galeria", methods=["GET"])
@admin_required
def obtener_galeria_producto(codigo):
    denied = _require_productos_crear_editar_json()
    if denied:
        return denied
    normalized = (codigo or "").strip().upper()
    sess = SessionDB()
    try:
        producto = _find_producto_by_codigo(sess, normalized)
        if producto is None:
            return jsonify({"success": False, "error": "Producto no encontrado"}), 404
        return jsonify({"success": True, "codigo": normalized, **_galeria_edit_payload(sess, producto)})
    finally:
        sess.close()


@productos_bp.route("/productos/imagenes/galeria/orden", methods=["POST"])
@admin_required
def guardar_galeria_orden():
    denied = _require_productos_crear_editar_json()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    codigo = (data.get("codigo") or "").strip().upper()
    orden_ids_raw = data.get("orden_ids") or []
    if not codigo:
        return jsonify({"success": False, "error": "Código inválido"}), 400
    if not isinstance(orden_ids_raw, list) or not orden_ids_raw:
        return jsonify({"success": False, "error": "orden_ids debe ser una lista no vacía"}), 400

    orden_ids: list[int] = []
    for raw in orden_ids_raw:
        try:
            orden_ids.append(int(raw))
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "orden_ids contiene ids inválidos"}), 400

    sess = SessionDB()
    try:
        producto = _find_producto_by_codigo(sess, codigo)
        if producto is None:
            return jsonify({"success": False, "error": "Producto no encontrado"}), 404

        scope_rows = {r.id: r for r in _galeria_imagenes_query(sess, producto)}
        if len(orden_ids) != len(set(orden_ids)):
            return jsonify({"success": False, "error": "Hay ids duplicados en la lista"}), 400
        for img_id in orden_ids:
            if img_id not in scope_rows:
                return jsonify({"success": False, "error": f"Imagen {img_id} no pertenece a este producto/OEM"}), 400

        portada_url: str | None = None
        for idx, img_id in enumerate(orden_ids):
            row = scope_rows[img_id]
            row.orden = idx
            row.es_principal = idx == 0
            if idx == 0:
                portada_url = (row.ruta or "").strip() or None

        if portada_url:
            producto.imagen_url = portada_url
            oem = (producto.codigo_oem or "").strip().upper()
            if oem:
                otros = (
                    sess.query(Producto)
                    .filter(Producto.activo.is_(True))
                    .filter(func.upper(func.trim(Producto.codigo_oem)) == oem)
                    .all()
                )
                for otro in otros:
                    otro.imagen_url = portada_url

        register_product_audit(
            sess,
            actor=_actor_usuario(),
            action="update",
            modulo="productos",
            producto_codigo=codigo,
            req=request,
            metadata={"galeria_reorden": True, "orden_ids": orden_ids},
        )
        sess.commit()
        return jsonify(
            {
                "success": True,
                "message": "Orden de galería guardado",
                "galeria": _galeria_edit_payload(sess, producto),
            }
        )
    except Exception as exc:
        sess.rollback()
        return jsonify({"success": False, "error": str(exc)}), 500
    finally:
        sess.close()


@productos_bp.route("/productos/imagenes/galeria/vincular", methods=["POST"])
@admin_required
def vincular_galeria_imagen():
    denied = _require_productos_crear_editar_json()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    codigo = (data.get("codigo") or "").strip().upper()
    url = (data.get("url") or "").strip()
    orden_raw = data.get("orden")
    if not codigo or not url:
        return jsonify({"success": False, "error": "Faltan codigo o url"}), 400

    try:
        orden = int(orden_raw) if orden_raw is not None else None
    except (TypeError, ValueError):
        orden = None

    sess = SessionDB()
    try:
        producto = _find_producto_by_codigo(sess, codigo)
        if producto is None:
            return jsonify({"success": False, "error": "Producto no encontrado"}), 404

        if orden is None:
            orden = imagen_ordenes_para_producto(sess, producto).get("imagen_siguiente_orden", 0)

        link_cloudinary_url_to_producto(sess, producto, url, orden=orden)
        register_product_audit(
            sess,
            actor=_actor_usuario(),
            action="update",
            modulo="productos",
            producto_codigo=codigo,
            req=request,
            metadata={"galeria_vincular": True, "url": url[:200], "orden": orden},
        )
        sess.commit()
        return jsonify(
            {
                "success": True,
                "message": "Imagen vinculada a la galería",
                "galeria": _galeria_edit_payload(sess, producto),
            }
        )
    except Exception as exc:
        sess.rollback()
        return jsonify({"success": False, "error": str(exc)}), 500
    finally:
        sess.close()


@productos_bp.route("/productos/imagenes/galeria/eliminar", methods=["POST"])
@admin_required
def eliminar_galeria_imagen():
    denied = _require_productos_crear_editar_json()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    codigo = (data.get("codigo") or "").strip().upper()
    img_id_raw = data.get("id")
    try:
        img_id = int(img_id_raw)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "id inválido"}), 400

    if not codigo:
        return jsonify({"success": False, "error": "Código inválido"}), 400

    sess = SessionDB()
    try:
        producto = _find_producto_by_codigo(sess, codigo)
        if producto is None:
            return jsonify({"success": False, "error": "Producto no encontrado"}), 404

        row = _galeria_row_en_scope(sess, producto, img_id)
        if row is None:
            return jsonify({"success": False, "error": "Imagen no encontrada en la galería"}), 404

        era_principal = bool(getattr(row, "es_principal", False)) or int(row.orden or 999) == 0
        ruta_borrada = (row.ruta or "").strip()
        sess.delete(row)
        sess.flush()

        if era_principal:
            restantes = sorted(_galeria_imagenes_query(sess, producto), key=_producto_imagen_sort_key)
            nueva_portada = (restantes[0].ruta or "").strip() if restantes else None
            producto.imagen_url = nueva_portada
            oem = (producto.codigo_oem or "").strip().upper()
            if oem:
                otros = (
                    sess.query(Producto)
                    .filter(Producto.activo.is_(True))
                    .filter(func.upper(func.trim(Producto.codigo_oem)) == oem)
                    .all()
                )
                for otro in otros:
                    otro.imagen_url = nueva_portada
            if restantes:
                restantes[0].es_principal = True
                restantes[0].orden = 0

        register_product_audit(
            sess,
            actor=_actor_usuario(),
            action="update",
            modulo="productos",
            producto_codigo=codigo,
            req=request,
            metadata={"galeria_eliminar": True, "imagen_id": img_id, "ruta": ruta_borrada[:200]},
        )
        sess.commit()
        return jsonify(
            {
                "success": True,
                "message": "Imagen quitada de la galería (el archivo en Cloudinary no se borra)",
                "galeria": _galeria_edit_payload(sess, producto),
            }
        )
    except Exception as exc:
        sess.rollback()
        return jsonify({"success": False, "error": str(exc)}), 500
    finally:
        sess.close()


@productos_bp.route("/productos/guardar_variante_comercial", methods=["POST"])
@admin_required
def guardar_variante_comercial():
    codigo = (request.form.get("codigo") or "").strip().upper()
    vid_raw = (request.form.get("variante_id") or "").strip()
    m_raw = request.form.get("margen_override", "")
    p_raw = request.form.get("precio_publico_neto_override", "")
    if not codigo or not vid_raw.isdigit():
        return jsonify({"success": False, "error": "Datos invalidos"}), 400
    vid = int(vid_raw)

    def _opt_float(raw: str) -> float | None:
        s = (raw or "").strip()
        if s == "":
            return None
        try:
            return float(s.replace(",", "."))
        except ValueError:
            raise ValueError("nan")

    try:
        om = _opt_float(m_raw)
        op = _opt_float(p_raw)
    except ValueError:
        return jsonify({"success": False, "error": "Valores numericos invalidos"}), 400

    if om is not None and (om < 0 or om >= 100):
        return jsonify({"success": False, "error": "Margen debe estar entre 0 y 100"}), 400
    if op is not None and op < 0:
        return jsonify({"success": False, "error": "Precio no puede ser negativo"}), 400

    v = security_db.session.query(ProductoVarianteStock).filter_by(id=vid).first()
    if v is None:
        return jsonify({"success": False, "error": "Variante no encontrada"}), 404
    if (v.codigo_producto or "").strip().upper() != codigo:
        return jsonify({"success": False, "error": "Codigo no coincide con la variante"}), 400

    v.margen_override_pct = om
    v.precio_publico_neto_override = op

    ref_raw = _ultimo_ingreso_ref_variante(codigo, v.marca, v.bodega)
    merged = merge_ingreso_ref_variante_overrides(
        ref_raw,
        v.margen_override_pct,
        v.precio_publico_neto_override,
    )
    pv_cat = merged.get("precio_sugerido_neto")
    if pv_cat is not None:
        try:
            pv_round = round(float(pv_cat), 2)
        except (TypeError, ValueError):
            pv_round = None
        # Solo precios > 0: nunca pisar el catálogo con 0 (evita $0 al vaciar campos / margen que da 0).
        if pv_round is not None and pv_round > 0:
            security_db.session.execute(
                text(
                    """
                    UPDATE productos
                    SET P_PUBLICO = :pv
                    WHERE UPPER(TRIM(CODIGO)) = UPPER(TRIM(:codigo))
                      AND COALESCE(ACTIVO, 1) = 1
                    """
                ),
                {"pv": pv_round, "codigo": codigo},
            )

    security_db.session.commit()

    return jsonify(
        {
            "success": True,
            "row": {
                "variante_id": v.id,
                "bodega": (v.bodega or "").strip() or "Bodega 1",
                "marca": (v.marca or "").strip() or "—",
                "stock": int(v.stock or 0),
                "costo_ingreso_neto": merged.get("costo_unitario_neto"),
                "margen_pct": merged.get("margen_registrado_pct"),
                "precio_publico_neto": merged.get("precio_sugerido_neto"),
            },
        }
    )


@productos_bp.route("/productos/guardar_edicion", methods=["POST"])
@admin_required
def guardar_edicion():
    if not get_user_permissions(session.get("user"), session.get("rol")).get("productos_crear_editar", False):
        return jsonify({"success": False, "error": "Permiso denegado"}), 403
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
        if precio is not None:
            producto.p_publico = precio
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
            from app.models import engine
            from app.utils.fts_productos import fts_blob_de_producto, fts_delete, fts_upsert

            with engine.begin() as conn:
                codigo_fts = (producto.codigo or "").strip()
                if activo_val:
                    fts_upsert(conn, codigo_fts, fts_blob_de_producto(producto))
                else:
                    fts_delete(conn, codigo_fts)
        except Exception:
            pass
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
    if not get_user_permissions(session.get("user"), session.get("rol")).get("productos_desactivar_reactivar", False):
        return jsonify({"success": False, "error": "Permiso denegado"}), 403
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
        try:
            from app.models import engine
            from app.utils.fts_productos import fts_delete

            with engine.begin() as conn:
                fts_delete(conn, codigo)
        except Exception:
            pass
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
    if not get_user_permissions(session.get("user"), session.get("rol")).get("productos_desactivar_reactivar", False):
        return jsonify({"success": False, "error": "Permiso denegado"}), 403
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
        try:
            from app.models import engine
            from app.utils.fts_productos import fts_blob_de_producto, fts_upsert

            with engine.begin() as conn:
                fts_upsert(conn, codigo, fts_blob_de_producto(producto))
        except Exception:
            pass
        return jsonify({"success": True, "message": "Producto reactivado correctamente"})
    except Exception as exc:
        sess.rollback()
        return jsonify({"success": False, "error": str(exc)}), 500
    finally:
        sess.close()


@productos_bp.route("/productos/reactivar_todos", methods=["POST"])
@admin_required
def reactivar_todos():
    if not get_user_permissions(session.get("user"), session.get("rol")).get("productos_desactivar_reactivar", False):
        return jsonify({"success": False, "error": "Permiso denegado"}), 403
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
        try:
            from app.models import engine
            from app.utils.fts_productos import fts_rebuild

            with engine.begin() as conn:
                fts_rebuild(conn)
        except Exception:
            pass
        return jsonify({"success": True, "reactivados": count,
                        "message": f"{count} productos reactivados correctamente"})
    except Exception as exc:
        sess.rollback()
        return jsonify({"success": False, "error": str(exc)}), 500
    finally:
        sess.close()


def _ficha_stock_repuestos(producto: Producto) -> dict:
    """Stock por bodega alineado a variantes ERP. Incluye marca por línea cuando hay productos_variantes_stock."""
    codigo = (producto.codigo or "").strip().upper()
    s6 = int(float(producto.stock_transito or 0))
    marca_catalogo = (producto.marca or "").strip() or "—"

    rows = (
        security_db.session.query(ProductoVarianteStock)
        .filter(func.upper(ProductoVarianteStock.codigo_producto) == codigo)
        .all()
    )

    if not rows:
        stocks = [
            int(float(producto.stock_10jul or 0)),
            int(float(producto.stock_brasil or 0)),
            int(float(producto.stock_g_avenida or 0)),
            int(float(producto.stock_orientales or 0)),
            int(float(producto.stock_b20_outlet or 0)),
        ]
        bodegas = []
        for idx, sn in enumerate(stocks, start=1):
            lineas = [{"marca": marca_catalogo, "stock": sn}]
            bodegas.append(
                {
                    "nombre": f"Bodega {idx}",
                    "lineas": lineas,
                    "subtotal": int(sn),
                }
            )
        st = sum(stocks) + s6
        return {
            "bodegas": bodegas,
            "otras_lineas": [],
            "s6": s6,
            "otras": 0,
            "stotal": st,
        }

    bucket_marcas: dict[int, dict[str, int]] = {i: {} for i in range(1, 6)}
    otras_acc: dict[tuple[str, str], int] = {}

    for r in rows:
        qty = int(r.stock or 0)
        marca = (r.marca or "").strip() or "SIN MARCA"
        b = (r.bodega or "").strip()
        m = re.match(r"(?i)^Bodega\s+(\d+)$", b)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 5:
                d = bucket_marcas[n]
                d[marca] = d.get(marca, 0) + qty
            else:
                key = (b, marca)
                otras_acc[key] = otras_acc.get(key, 0) + qty
        else:
            key = (b or "OTRAS", marca)
            otras_acc[key] = otras_acc.get(key, 0) + qty

    bodegas = []
    for n in range(1, 6):
        d = bucket_marcas[n]
        if d:
            lineas = [{"marca": k, "stock": v} for k, v in sorted(d.items(), key=lambda x: x[0])]
        else:
            lineas = [{"marca": "—", "stock": 0}]
        subtotal = sum(int(x["stock"]) for x in lineas)
        bodegas.append({"nombre": f"Bodega {n}", "lineas": lineas, "subtotal": subtotal})

    otras_lineas = [
        {"bodega": bk, "marca": mk, "stock": v}
        for (bk, mk), v in sorted(otras_acc.items(), key=lambda x: (x[0][0], x[0][1]))
    ]
    otras = sum(otras_acc.values())
    stotal = sum(int(r.stock or 0) for r in rows) + s6

    return {
        "bodegas": bodegas,
        "otras_lineas": otras_lineas,
        "s6": s6,
        "otras": otras,
        "stotal": stotal,
    }


FICHA_CACHE_TTL = 600


def _ficha_stock_quick(producto: Producto) -> dict:
    """Stock resumido desde columnas del producto (sin consultar variantes)."""
    s6 = int(float(producto.stock_transito or 0))
    marca_catalogo = (producto.marca or "").strip() or "—"
    stocks = [
        int(float(producto.stock_10jul or 0)),
        int(float(producto.stock_brasil or 0)),
        int(float(producto.stock_g_avenida or 0)),
        int(float(producto.stock_orientales or 0)),
        int(float(producto.stock_b20_outlet or 0)),
    ]
    bodegas = []
    for idx, sn in enumerate(stocks, start=1):
        bodegas.append(
            {
                "nombre": f"Bodega {idx}",
                "lineas": [{"marca": marca_catalogo, "stock": sn}],
                "subtotal": int(sn),
            }
        )
    return {
        "bodegas": bodegas,
        "otras_lineas": [],
        "s6": s6,
        "otras": 0,
        "stotal": sum(stocks) + s6,
        "partial": True,
    }


def _ficha_homologados_payload(db, producto: Producto, normalized: str) -> dict:
    def loader() -> dict:
        homologados_raw = (producto.homologados or "").strip()
        items: list[dict] = []
        if not homologados_raw:
            forward_products = _forward_productos_homologados(db, producto, normalized)
            reverse_products = _reverse_products_listing_homologado(db, normalized)
            merged = _merge_by_codigo(list(forward_products) + list(reverse_products))
            items = [
                {
                    "codigo": (p.codigo or "").strip(),
                    "descripcion": (p.descripcion or "").strip(),
                }
                for p in merged
            ]
        return {"homologados_raw": homologados_raw, "homologados_items": items}

    return get_or_load_ttl(f"ficha_homologados:{normalized}", loader, FICHA_CACHE_TTL)


def _ficha_despiece_payload(db, producto: Producto) -> dict:
    normalized = (producto.codigo or "").strip().upper()

    def loader() -> dict:
        despiece_row = None
        despiece_partes: list = []
        despiece_imagen_url = None
        despiece_titulo = ""
        despiece_notas = ""
        despiece_partes_texto = ""
        try:
            despiece_row = _find_oem_despiece_for_producto(db, producto)
        except Exception:
            despiece_row = None
        if despiece_row:
            despiece_titulo = (despiece_row.titulo or "").strip()
            despiece_notas = (despiece_row.notas or "").strip()
            img_rel = (despiece_row.imagen_static or "").strip()
            if img_rel:
                despiece_imagen_url = img_rel
            else:
                img_fallback_rel = _find_shared_despiece_image_fallback(db, producto)
                if img_fallback_rel:
                    despiece_imagen_url = img_fallback_rel
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
        if not despiece_imagen_url:
            fb_name = _find_epc_despiece_archivo_en_static(producto)
            if fb_name:
                despiece_imagen_url = f"epc_despiece/{fb_name}"
        oem_match = _norm_oem_despiece(getattr(producto, "codigo_oem", None))
        return {
            "despiece_titulo": despiece_titulo,
            "despiece_imagen_url": despiece_imagen_url,
            "despiece_imagen_src": product_image_src(despiece_imagen_url) if despiece_imagen_url else "",
            "despiece_partes": despiece_partes,
            "despiece_notas": despiece_notas,
            "despiece_partes_texto": despiece_partes_texto,
            "despiece_oem_match": oem_match,
        }

    return get_or_load_ttl(f"ficha_despiece:{normalized}", loader, FICHA_CACHE_TTL)


def _get_producto_ficha(db, normalized: str) -> Producto | None:
    return (
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


# =========================================
# VER PRODUCTO (FICHA TECNICA)
# =========================================
@productos_bp.route("/producto/<codigo>")
@login_required
def ver_producto(codigo):
    t0 = time.time()
    normalized = (codigo or "").strip().upper()
    db = SessionDB()
    user_perms = get_user_permissions(session.get("user"), session.get("rol"))
    producto = None
    categoria_txt = ""
    subcategoria_txt = ""
    try:
        producto = _get_producto_ficha(db, normalized)
        if producto:
            _auto_asignar_categoria_si_vacio(db, producto)
            db.refresh(producto)
            if producto.categoria_rel:
                categoria_txt = (producto.categoria_rel.nombre or "").strip()
            if producto.subcategoria_rel:
                subcategoria_txt = (producto.subcategoria_rel.nombre or "").strip()
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

        es_admin = "admin" in (session.get("rol") or "").strip().lower()
        ficha_stock = _ficha_stock_quick(producto)

        html = render_template(
            "modal_producto.html",
            producto=producto,
            ficha_stock=ficha_stock,
            imagenes=[],
            imagenes_360=[],
            categoria_txt=categoria_txt,
            subcategoria_txt=subcategoria_txt,
            homologados_items=[],
            homologados_raw="",
            has_despiece_panel=True,
            es_admin=es_admin,
            despiece_titulo="",
            despiece_imagen_url=None,
            despiece_partes=[],
            despiece_notas="",
            despiece_partes_texto="",
            despiece_oem_match=_norm_oem_despiece(getattr(producto, "codigo_oem", None)),
            despiece_erp_precio=getattr(producto, "p_publico", None),
            despiece_erp_precio_mayor=(
                getattr(producto, "prec_mayor", None)
                if bool(user_perms.get("ver_precio_mayor", True))
                else None
            ),
            can_view_precio_mayor=bool(user_perms.get("ver_precio_mayor", True)),
            lazy_ficha=True,
        )
        resp = make_response(html)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        current_app.logger.info(
            "Modal producto %s: %.3fs",
            normalized,
            time.time() - t0,
        )
        return resp
    finally:
        db.close()


@productos_bp.route("/producto/<codigo>/ficha/extras")
@login_required
def ver_producto_ficha_extras(codigo):
    """Carga diferida: imágenes, stock detallado, homologados y despiece."""
    t0 = time.time()
    normalized = (codigo or "").strip().upper()
    db = SessionDB()
    user_perms = get_user_permissions(session.get("user"), session.get("rol"))
    try:
        producto = _get_producto_ficha(db, normalized)
        if not producto:
            return jsonify(success=False, message="Producto no encontrado"), 404

        imagenes = _collect_imagenes_producto(producto)
        imagenes_360 = _collect_imagenes_360(producto)
        codigo_dir = (producto.codigo or "").strip()
        imagenes_urls = [product_image_src(img) for img in imagenes]
        portada_raw = imagenes[0] if imagenes else None
        imagenes_360_urls = [
            product_image_src(f"productos360/{codigo_dir}/{name}") for name in imagenes_360
        ]

        hom = _ficha_homologados_payload(db, producto, normalized)
        desp = _ficha_despiece_payload(db, producto)
        ficha_stock = _ficha_stock_repuestos(producto)

        elapsed = time.time() - t0
        current_app.logger.info(
            "Modal producto %s extras: %.3fs",
            normalized,
            elapsed,
        )
        return jsonify(
            success=True,
            codigo=producto.codigo,
            imagenes=imagenes_urls,
            imagen_portada=product_image_src(portada_raw) if portada_raw else None,
            imagenes_360=imagenes_360_urls,
            ficha_stock=ficha_stock,
            homologados_raw=hom["homologados_raw"],
            homologados_items=hom["homologados_items"],
            despiece={
                **desp,
                "erp_precio": getattr(producto, "p_publico", None),
                "erp_precio_mayor": (
                    getattr(producto, "prec_mayor", None)
                    if bool(user_perms.get("ver_precio_mayor", True))
                    else None
                ),
            },
            timing_s=round(elapsed, 3),
        )
    finally:
        db.close()


@productos_bp.route("/producto/<codigo>/despiece", methods=["POST"])
@admin_required
def guardar_despiece_producto(codigo):
    if not get_user_permissions(session.get("user"), session.get("rol")).get("productos_crear_editar", False):
        return jsonify(success=False, message="Permiso denegado"), 403
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

        oem_norm = _norm_oem_despiece(getattr(producto, "codigo_oem", None))
        if oem_norm:
            # Si el producto tiene OEM, guardar en catálogo compartido por OEM.
            row = db.query(OemDespiece).filter(OemDespiece.oem_norm == oem_norm).first()
            if not row:
                row = OemDespiece(
                    oem_norm=oem_norm,
                    producto_codigo=None,
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
        else:
            # Fallback histórico: productos sin OEM se ligan a su código interno.
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
            if row.imagen_static:
                _delete_product_image_ref(row.imagen_static)
            stored = _upload_product_image_file(
                upload,
                codigo=normalized,
                suffix=f"_despiece_{int(datetime.utcnow().timestamp())}",
                allowed_exts={"jpg", "jpeg", "png", "webp"},
            )
            if not stored:
                return jsonify(success=False, message="No se pudo guardar la imagen"), 400
            row.imagen_static = stored
        elif borrar_imagen:
            if row.imagen_static:
                _delete_product_image_ref(row.imagen_static)
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
        try:
            invalidate_ficha_despiece(normalized)
            invalidate_ficha_despiece_for_oem(db, row.oem_norm or "")
        except Exception:
            pass

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
    embed_modal = (request.args.get("embed") or "").strip() == "1"
    solo_mov = (request.args.get("solo_mov") or "").strip() == "1"
    filtro = (request.args.get("filtro") or "todos").strip().lower()
    if filtro not in {"todos", "boleta", "factura", "ingreso", "mov_stock", "ajuste", "salida"}:
        filtro = "todos"

    perms = get_user_permissions(session.get("user"), session.get("rol"))
    can_open_ingreso = bool(perms.get("bodega_ingreso", False))
    can_open_facturacion = bool(perms.get("mod_ventas", False))

    def _extract_doc_ref(obs: str | None) -> tuple[str, str | None]:
        text_obs = (obs or "").strip()
        low = text_obs.lower()
        if "boleta" in low:
            m = re.search(r"\bboleta\s*(?:n[°ºo.]*)?\s*([a-z0-9\-\/]+)", text_obs, re.IGNORECASE)
            return "boleta", (m.group(1).strip() if m else None)
        if "factura" in low:
            m = re.search(r"\bfactura\s*(?:n[°ºo.]*)?\s*([a-z0-9\-\/]+)", text_obs, re.IGNORECASE)
            return "factura", (m.group(1).strip() if m else None)
        if "doc " in low or low.startswith("doc"):
            m = re.search(r"\bdoc\s*([0-9]+)\b", text_obs, re.IGNORECASE)
            return "ingreso", (m.group(1).strip() if m else None)
        return "otros", None

    sess = SessionDB()
    try:
        producto = sess.query(Producto).filter(Producto.codigo == normalized).first()

        movimientos_raw = (
            security_db.session.query(MovimientoStock)
            .filter(MovimientoStock.codigo_producto == normalized)
            .order_by(MovimientoStock.fecha.desc(), MovimientoStock.id.desc())
            .limit(200)
            .all()
        )
        movimientos = []
        for m in movimientos_raw:
            ref_type, ref_number = _extract_doc_ref(getattr(m, "observacion", None))
            view_url = None
            view_label = None
            if ref_type == "ingreso":
                ingreso_id = int(getattr(m, "ingreso_documento_id", 0) or 0)
                if ingreso_id > 0 and can_open_ingreso:
                    view_kwargs = {"doc_id": ingreso_id}
                    if embed_modal:
                        view_kwargs["embed"] = 1
                    view_url = url_for("bodega.ingreso_ver", **view_kwargs)
                    view_label = f"Ingreso #{ingreso_id}"
            elif ref_type == "factura":
                if ref_number and can_open_facturacion:
                    view_url = url_for("ventas.facturacion", numero=ref_number, tipo_documento="factura")
                    view_label = f"Factura {ref_number}"
            elif ref_type == "boleta":
                if ref_number and can_open_facturacion:
                    view_url = url_for("ventas.facturacion", numero=ref_number, tipo_documento="boleta")
                    view_label = f"Boleta {ref_number}"

            mov_type = ((getattr(m, "tipo", None) or "").strip().lower())
            if filtro == "mov_stock":
                if mov_type not in {"ingreso", "salida"}:
                    continue
            elif filtro == "ajuste":
                if mov_type != "ajuste":
                    continue
            elif filtro == "salida":
                if mov_type != "salida":
                    continue
            elif filtro != "todos" and ref_type != filtro:
                continue

            movimientos.append(
                {
                    "row": m,
                    "ref_type": ref_type,
                    "ref_number": ref_number,
                    "view_url": view_url,
                    "view_label": view_label,
                }
            )

        compras_rows = (
            security_db.session.query(IngresoDocumentoItem, IngresoDocumento)
            .join(IngresoDocumento, IngresoDocumento.id == IngresoDocumentoItem.ingreso_documento_id)
            .filter(or_(IngresoDocumento.anulado.is_(False), IngresoDocumento.anulado.is_(None)))
            .filter(func.upper(func.trim(IngresoDocumentoItem.codigo_producto)) == normalized)
            .order_by(IngresoDocumento.created_at.desc(), IngresoDocumentoItem.id.desc())
            .limit(200)
            .all()
        )
        compras = []
        for item, doc in compras_rows:
            cantidad = int(getattr(item, "cantidad", 0) or 0)
            costo_unit = float(getattr(item, "valor_neto", 0) or 0)
            total_neto = costo_unit * cantidad
            compras.append(
                {
                    "fecha": getattr(doc, "fecha_documento", None) or getattr(doc, "created_at", None),
                    "proveedor": (getattr(doc, "proveedor_nombre", None) or "—").strip() or "—",
                    "numero_documento": (getattr(doc, "numero_documento", None) or "").strip() or "—",
                    "marca": (getattr(item, "marca", None) or "").strip() or "—",
                    "bodega": (getattr(item, "bodega", None) or "").strip() or "—",
                    "origen_compra": (getattr(item, "origen_compra", None) or "").strip() or "—",
                    "cantidad": cantidad,
                    "costo_unitario": costo_unit,
                    "total_neto": total_neto,
                    "precio_venta_neto": float(getattr(item, "precio_venta_neto", 0) or 0),
                    "doc_id": int(getattr(doc, "id", 0) or 0),
                    "doc_url": (
                        url_for(
                            "bodega.ingreso_ver",
                            doc_id=doc.id,
                            **({"embed": 1} if embed_modal else {}),
                        )
                        if int(getattr(doc, "id", 0) or 0) > 0 and can_open_ingreso
                        else None
                    ),
                }
            )

        puede_ver_finanzas = user_can_view_finanzas(session.get("user"), session.get("rol"))
        if not puede_ver_finanzas:
            compras = [redact_compra_historial_row(c) for c in compras]

        from app.utils.relmap_auth import is_superadmin_session

        return render_template(
            "productos/historial_producto.html",
            producto=producto,
            codigo=normalized,
            movimientos=movimientos,
            compras=compras,
            filtro=filtro,
            solo_mov=solo_mov,
            puede_ver_finanzas=puede_ver_finanzas,
            online_users=_online_users(),
            active_page="productos_buscar",
            _partial=_wants_modal_fragment(),
            embed_modal=embed_modal,
            relmap_direct_access=is_superadmin_session(),
        )
    finally:
        sess.close()


@productos_bp.route("/productos/api/relmap/authorize", methods=["POST"])
@login_required
def api_relmap_authorize():
    """Autoriza mapa de relaciones (usable sin mod_ventas)."""
    from app.utils.relmap_auth import authorize_relmap_credentials

    data = request.get_json(silent=True) or {}
    ok, message = authorize_relmap_credentials(data.get("usuario") or "", data.get("password") or "")
    if not ok:
        return jsonify({"success": False, "message": message}), 403
    return jsonify({"success": True, "message": message})


@productos_bp.route("/productos/api/producto/<codigo>/relationship-map", methods=["GET"])
@login_required
def api_producto_relationship_map(codigo: str):
    """Mapa adicional por producto (no reemplaza el historial tabular)."""
    from app.utils.relmap_auth import can_access_relmap
    from app.ventas.routes import _build_product_relationship_map

    if not can_access_relmap():
        return jsonify(
            {
                "success": False,
                "auth_required": True,
                "message": "Se requiere autorización (usuario y clave) para ver el mapa de relaciones.",
            }
        ), 403

    code = (codigo or "").strip().upper()
    if not code:
        return jsonify({"success": False, "message": "Código vacío"}), 400

    rel_map = _build_product_relationship_map(code)
    return jsonify({"success": True, "codigo": code, "map": rel_map})


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
    if not get_user_permissions(session.get("user"), session.get("rol")).get("productos_importar_exportar", False):
        return redirect(url_for("productos.buscar"))

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
    if not get_user_permissions(session.get("user"), session.get("rol")).get("productos_importar_exportar", False):
        return jsonify(success=False, message="Permiso denegado"), 403

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
        try:
            from app.models import engine
            from app.utils.fts_productos import fts_create_table, fts_rebuild
            with engine.begin() as conn:
                fts_create_table(conn)
                fts_rebuild(conn)
        except Exception:
            pass
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
    if not get_user_permissions(session.get("user"), session.get("rol")).get("productos_importar_exportar", False):
        return redirect(url_for("productos.buscar"))

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
    if not get_user_permissions(session.get("user"), session.get("rol")).get("productos_crear_editar", False):
        return jsonify({"success": False, "error": "Permiso denegado"}), 403
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
        image_routes = _save_uploaded_images(codigo, image_files, producto=p)
        for i, ruta in enumerate(image_routes):
            p.imagenes.append(ProductoImagen(ruta=ruta, es_principal=(i == 0)))
        if image_routes:
            p.imagen_url = image_routes[0]

        despiece = request.files.get("despiece_img")
        if despiece and getattr(despiece, "filename", None):
            if p.despiece:
                _delete_product_image_ref(p.despiece)
            stored_despiece = _upload_product_image_file(
                despiece,
                codigo=codigo,
                suffix="_despiece",
                allowed_exts={"jpg", "jpeg", "png", "webp"},
            )
            if stored_despiece:
                p.despiece = stored_despiece

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
        try:
            from app.models import engine
            from app.utils.fts_productos import fts_blob_de_producto, fts_upsert

            with engine.begin() as conn:
                fts_upsert(conn, codigo, fts_blob_de_producto(p))
        except Exception:
            pass
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
    if request.method in {"POST", "DELETE"} and not get_user_permissions(session.get("user"), session.get("rol")).get("productos_crear_editar", False):
        return jsonify({"success": False, "error": "Permiso denegado"}), 403
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
            acode = resolve_producto_audit_action_filter(action)
            if acode:
                query = query.filter(ProductoAuditEvent.action == acode)
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
    if not get_user_permissions(session.get("user"), session.get("rol")).get("productos_importar_exportar", False):
        return redirect(url_for("productos.productos_auditoria"))
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
            acode = resolve_producto_audit_action_filter(action)
            if acode:
                query = query.filter(ProductoAuditEvent.action == acode)
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


# =========================================
# IMPORTAR IMÁGENES DESDE CLOUDINARY
# =========================================


def _require_productos_crear_editar_json():
    if not get_user_permissions(session.get("user"), session.get("rol")).get(
        "productos_crear_editar", False
    ):
        return jsonify({"success": False, "error": "Permiso denegado"}), 403
    return None


def _procesar_subida_imagen_cloudinary(
    sess,
    file_obj,
    *,
    codigo_asignado: str,
    archivo_nombre: str,
    tipo_imagen: str = TIPO_IMAGEN_PRODUCTO,
    es_principal: bool | None = None,
    orden: int | None = None,
) -> dict:
    """Sube una imagen a Cloudinary y vincula al producto / 360 / despiece si aplica."""
    codigo = (codigo_asignado or "").strip().upper()
    fname = (archivo_nombre or getattr(file_obj, "filename", None) or "imagen.jpg").strip()
    tipo = normalize_tipo_imagen(tipo_imagen)
    row = {
        "archivo": fname,
        "codigo_asignado": codigo,
        "tipo_imagen": tipo,
        "producto_codigo": None,
        "producto_descripcion": None,
        "cloudinary_url": None,
        "estado": "",
        "mensaje": "",
    }
    if not codigo:
        row["estado"] = "omitido"
        row["mensaje"] = "Sin código asignado"
        return row

    producto, match_type = find_producto_by_image_code(sess, codigo)
    storage_key = cloudinary_storage_key(producto, codigo)
    codigo_interno = (producto.codigo or codigo).strip().upper() if producto else codigo

    public_id_path, local_subdir = build_import_public_id(
        tipo,
        storage_key=storage_key,
        producto_codigo_interno=codigo_interno,
        archivo_nombre=fname,
    )

    try:
        stored = _upload_product_image_file(
            file_obj,
            codigo=codigo_interno,
            storage_codigo=storage_key,
            suffix="",
            allowed_exts=ALLOWED_IMAGE_EXTENSIONS,
            archivo_nombre=fname,
            cloud_folder=public_id_path.rsplit("/", 1)[0],
            public_id_path=public_id_path,
            local_subdir=local_subdir,
        )
    except ValueError as exc:
        row["estado"] = "error"
        row["mensaje"] = str(exc)
        return row
    except Exception as exc:
        row["estado"] = "error"
        row["mensaje"] = f"Error al subir: {exc}"
        return row
    if not stored:
        row["estado"] = "error"
        row["mensaje"] = "Cloudinary no devolvió URL"
        return row

    row["cloudinary_url"] = stored
    row["cloudinary_name"] = storage_key
    row["match_type"] = match_type
    row["cloud_folder"] = public_id_path.rsplit("/", 2)[0] if tipo == TIPO_IMAGEN_360 else public_id_path.rsplit("/", 1)[0]

    if tipo == TIPO_IMAGEN_PRODUCTO:
        if producto:
            link_cloudinary_url_to_producto(
                sess,
                producto,
                stored,
                es_principal=es_principal,
                orden=orden,
            )
            info = producto_resolver_payload(producto, match_type or "interno")
            row["producto_codigo"] = info["codigo"]
            row["producto_descripcion"] = info["descripcion"]
            row["producto_oem"] = info.get("oem") or ""
            row["display_codigo"] = info["display_codigo"]
            row["estado"] = "vinculado"
            row["mensaje"] = "Vinculado (producto)"
        else:
            row["display_codigo"] = codigo
            row["estado"] = "sin_producto"
            row["mensaje"] = "Subida a Cloudinary; producto no encontrado en BD"
        return row

    if tipo == TIPO_IMAGEN_360:
        if not producto:
            row["display_codigo"] = codigo
            row["estado"] = "sin_producto"
            row["mensaje"] = "Subida a Cloudinary; producto no encontrado (360 requiere producto)"
            return row
        rel_key = link_cloudinary_url_to_360(
            sess,
            producto,
            stored,
            codigo_interno=codigo_interno,
            archivo_nombre=fname,
        )
        info = producto_resolver_payload(producto, match_type or "interno")
        row["producto_codigo"] = info["codigo"]
        row["producto_descripcion"] = info["descripcion"]
        row["display_codigo"] = info["display_codigo"]
        row["static_key"] = rel_key
        row["estado"] = "vinculado"
        row["mensaje"] = "Vinculado (360°)"
        return row

    linked, oem_norm = link_cloudinary_url_to_despiece(sess, producto, codigo, stored)
    row["oem_norm"] = oem_norm
    if producto:
        info = producto_resolver_payload(producto, match_type or "interno")
        row["producto_codigo"] = info["codigo"]
        row["producto_descripcion"] = info["descripcion"]
        row["display_codigo"] = info["display_codigo"]
    else:
        row["display_codigo"] = codigo
    if linked:
        row["estado"] = "vinculado"
        row["mensaje"] = "Vinculado (despiece EPC)"
    else:
        row["estado"] = "sin_producto"
        row["mensaje"] = "Subida a Cloudinary; no se pudo vincular despiece"
    return row


@productos_bp.route("/productos/importar-imagenes-cloudinary")
@admin_required
def importar_imagenes_cloudinary_view():
    if not get_user_permissions(session.get("user"), session.get("rol")).get(
        "productos_crear_editar", False
    ):
        return redirect(url_for("productos.buscar"))
    return render_template(
        "productos/importar_imagenes_cloudinary.html",
        cloudinary_ok=cloudinary_is_configured(),
        online_users=_online_users(),
        active_page="productos_importar_imagenes",
    )


@productos_bp.route("/productos/importar-imagenes-cloudinary/subir", methods=["POST"])
@admin_required
def importar_imagenes_cloudinary_subir():
    denied = _require_productos_crear_editar_json()
    if denied:
        return denied
    if not cloudinary_is_configured():
        return jsonify(
            {"success": False, "error": "Cloudinary no está configurado (variables CLOUDINARY_*)."}
        ), 503

    files = request.files.getlist("imagenes") or request.files.getlist("files") or []
    codigos = request.form.getlist("codigos")
    if not files:
        single = request.files.get("file") or request.files.get("imagen")
        if single and getattr(single, "filename", None):
            files = [single]
            codigos = [request.form.get("codigo") or ""]
    if not files:
        return jsonify({"success": False, "error": "No se seleccionaron archivos."}), 400

    sess = SessionDB()
    resultados: list[dict] = []
    vinculados = omitidos = sin_producto = errores = 0

    try:
        for idx, f in enumerate(files):
            fname = (getattr(f, "filename", None) or "").strip() or f"imagen_{idx + 1}.jpg"
            codigo = (codigos[idx] if idx < len(codigos) else "").strip().upper()
            if not codigo:
                codigo = codigo_from_filename(fname)
            row = _procesar_subida_imagen_cloudinary(sess, f, codigo_asignado=codigo, archivo_nombre=fname)
            resultados.append(row)
            st = row.get("estado")
            if st == "vinculado":
                vinculados += 1
            elif st == "sin_producto":
                sin_producto += 1
            elif st == "omitido":
                omitidos += 1
            else:
                errores += 1

        if vinculados:
            register_product_audit(
                sess,
                actor=_actor_usuario(),
                action="update",
                modulo="productos",
                producto_codigo=None,
                req=request,
                metadata={
                    "bulk_cloudinary_images": True,
                    "vinculados": vinculados,
                    "sin_producto": sin_producto,
                    "errores": errores,
                },
            )
        sess.commit()
    except Exception as exc:
        sess.rollback()
        return jsonify({"success": False, "error": str(exc)}), 500
    finally:
        sess.close()

    return jsonify(
        {
            "success": True,
            "total": len(resultados),
            "vinculados": vinculados,
            "sin_producto": sin_producto,
            "omitidos": omitidos,
            "errores": errores,
            "resultados": resultados,
        }
    )


@productos_bp.route("/productos/importar-imagenes-cloudinary/subir-uno", methods=["POST"])
@admin_required
def importar_imagenes_cloudinary_subir_uno():
    import logging
    import traceback

    log = logging.getLogger(__name__)
    try:
        denied = _require_productos_crear_editar_json()
        if denied:
            return denied
        if not cloudinary_is_configured():
            return jsonify(
                {
                    "ok": False,
                    "success": False,
                    "error": "Cloudinary no está configurado (variables CLOUDINARY_*).",
                    "mensaje": "Cloudinary no está configurado (variables CLOUDINARY_*).",
                }
            ), 503

        file_obj = request.files.get("imagen") or request.files.get("file")
        codigo = (request.form.get("codigo") or "").strip().upper()
        archivo_nombre = (request.form.get("archivo_nombre") or "").strip()
        tipo_imagen = normalize_tipo_imagen(request.form.get("tipo_imagen"))

        log.info(
            "subir-uno: content_type=%s codigo=%s archivo_nombre=%s files=%s",
            request.content_type,
            codigo,
            archivo_nombre,
            list(request.files.keys()),
        )
        if file_obj:
            log.info("subir-uno: file_meta=%s", describe_upload_file(file_obj, archivo_nombre))

        if not file_obj:
            return jsonify(
                {
                    "ok": False,
                    "success": False,
                    "error": "Falta archivo de imagen en la petición.",
                    "mensaje": "Falta archivo de imagen en la petición.",
                    "estado": "error",
                }
            ), 400
        if not codigo:
            return jsonify(
                {
                    "ok": False,
                    "success": False,
                    "error": "Falta código de producto.",
                    "mensaje": "Falta código de producto.",
                    "estado": "error",
                }
            ), 400

        fname = (
            archivo_nombre
            or (getattr(file_obj, "filename", None) or "").strip()
            or "imagen.jpg"
        )
        orden_raw = (request.form.get("orden") or "0").strip()
        try:
            orden = int(orden_raw)
        except ValueError:
            orden = 0
        es_principal = orden == 0
        sess = SessionDB()
        try:
            row = _procesar_subida_imagen_cloudinary(
                sess,
                file_obj,
                codigo_asignado=codigo,
                archivo_nombre=fname,
                tipo_imagen=tipo_imagen,
                es_principal=es_principal,
                orden=orden,
            )
            if row.get("estado") == "vinculado":
                register_product_audit(
                    sess,
                    actor=_actor_usuario(),
                    producto_codigo=row.get("producto_codigo"),
                    action="update",
                    modulo="productos",
                    req=request,
                    metadata={"cloudinary_image_upload": True, "archivo": fname[:120], "tipo_imagen": tipo_imagen},
                )
            sess.commit()
            ok = row.get("estado") != "error"
            if not ok:
                log.warning(
                    "subir-uno: fallo estado=%s mensaje=%s codigo=%s archivo=%s",
                    row.get("estado"),
                    row.get("mensaje"),
                    codigo,
                    fname,
                )
            return jsonify({"ok": ok, "success": ok, **row})
        except Exception as exc:
            sess.rollback()
            tb = traceback.format_exc()
            log.error(
                "subir-uno: excepción codigo=%s archivo=%s\n%s",
                codigo,
                fname,
                tb,
            )
            return jsonify(
                {
                    "ok": False,
                    "success": False,
                    "error": str(exc),
                    "mensaje": str(exc),
                    "traceback": tb,
                    "estado": "error",
                }
            ), 500
        finally:
            sess.close()
    except Exception as e:
        tb = traceback.format_exc()
        log.error("subir-uno: error fatal\n%s", tb)
        return jsonify(
            {
                "ok": False,
                "success": False,
                "mensaje": str(e),
                "error": str(e),
                "traceback": tb,
                "estado": "error",
            }
        ), 500


@productos_bp.route("/productos/importar-imagenes-cloudinary/resolver")
@admin_required
def importar_imagenes_cloudinary_resolver():
    denied = _require_productos_crear_editar_json()
    if denied:
        return denied
    codigo = (request.args.get("codigo") or "").strip().upper()
    if not codigo:
        return jsonify({"success": True, "found": False})
    sess = SessionDB()
    try:
        info = resolver_producto_por_codigo(sess, codigo)
        return jsonify({"success": True, **info})
    finally:
        sess.close()


@productos_bp.route("/productos/importar-imagenes-cloudinary/desde-url", methods=["POST"])
@admin_required
def importar_imagenes_cloudinary_desde_url():
    """Obtiene imagen desde URL (drag desde web) sin descargar en el cliente."""
    import base64
    import logging
    import traceback

    log = logging.getLogger(__name__)
    denied = _require_productos_crear_editar_json()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or request.form.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return jsonify({"success": False, "error": "URL inválida."}), 400

    try:
        raw, ext, filename = download_image_from_url(url, max_bytes=MAX_IMAGE_UPLOAD_BYTES)
        b64 = base64.b64encode(raw).decode("ascii")
        return jsonify(
            {
                "success": True,
                "filename": filename,
                "mime": f"image/{ext}",
                "data_base64": b64,
            }
        )
    except ValueError as exc:
        log.warning("desde-url: rechazado url=%s error=%s", url[:120], exc)
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        log.error("desde-url: excepción url=%s\n%s", url[:120], traceback.format_exc())
        return jsonify({"success": False, "error": str(exc)}), 500


@productos_bp.route("/productos/importar-imagenes-cloudinary/asignar", methods=["POST"])
@admin_required
def importar_imagenes_cloudinary_asignar():
    denied = _require_productos_crear_editar_json()
    if denied:
        return denied

    data = request.get_json(silent=True) or {}
    cloudinary_url = (data.get("cloudinary_url") or request.form.get("cloudinary_url") or "").strip()
    codigo = (data.get("codigo") or request.form.get("codigo") or "").strip().upper()
    if not cloudinary_url or not codigo:
        return jsonify({"success": False, "error": "Faltan cloudinary_url o codigo."}), 400

    sess = SessionDB()
    try:
        producto = _find_producto_by_codigo(sess, codigo)
        if producto is None:
            return jsonify({"success": False, "error": "Producto no encontrado."}), 404
        link_cloudinary_url_to_producto(sess, producto, cloudinary_url)
        register_product_audit(
            sess,
            actor=_actor_usuario(),
            producto_codigo=codigo,
            action="update",
            modulo="productos",
            req=request,
            metadata={"cloudinary_image_assign": True, "url": cloudinary_url[:200]},
        )
        sess.commit()
        return jsonify(
            {
                "success": True,
                "codigo": codigo,
                "descripcion": (producto.descripcion or "")[:120],
            }
        )
    except Exception as exc:
        sess.rollback()
        return jsonify({"success": False, "error": str(exc)}), 500
    finally:
        sess.close()


@productos_bp.route("/productos/importar-imagenes-cloudinary/buscar")
@admin_required
def importar_imagenes_cloudinary_buscar():
    denied = _require_productos_crear_editar_json()
    if denied:
        return denied
    q = (request.args.get("q") or "").strip()
    if len(q) < 1:
        return jsonify({"success": True, "items": []})
    sess = SessionDB()
    try:
        items = search_productos_for_assign(sess, q, limit=12)
        return jsonify({"success": True, "items": items})
    finally:
        sess.close()