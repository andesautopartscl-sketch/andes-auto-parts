import base64
import io
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import func, or_, select, text
import barcode
import qrcode
from barcode.writer import ImageWriter
from werkzeug.security import check_password_hash

from app.extensions import db
from app.seguridad.models import Usuario
from app.ventas.models import DocumentoVenta, Proveedor
from app.utils.decorators import admin_required, login_required
from app.utils.permissions import has_permission
from app.utils.phone_format import phone_to_compact_e164
from app.utils.rut_utils import clean_rut, format_rut, is_valid_rut

from .models import (
    CatalogoBodega,
    HistorialEtiqueta,
    IngresoDocumento,
    IngresoDocumentoItem,
    MovimientoStock,
    PickingVenta,
    PickingVentaLine,
    ProductoVarianteStock,
    ProveedorCodigoInterno,
)
from . import catalogo as bodega_catalogo
from .marcas_ref_cl import MARCAS_REF_AUTOMOTRIZ_CL
from app.inventario.models import LabelPrintHistory, TransferenciaStock


bodega_bp = Blueprint("bodega", __name__, url_prefix="/bodega")

_CHILE_GEO_JSON = Path(__file__).resolve().parent.parent / "ventas" / "data" / "chile_geo.json"


def _load_chile_geo_ingreso() -> list[dict]:
    try:
        if not _CHILE_GEO_JSON.exists():
            return []
        with _CHILE_GEO_JSON.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _chile_region_names(geo: list[dict]) -> list[str]:
    return [r.get("nombre", "") for r in geo if r.get("nombre")]


@bodega_bp.before_request
def _bodega_module_guard():
    if "user" not in session:
        return None
    if has_permission(session.get("user"), session.get("rol"), "mod_bodega"):
        return None
    is_ajax = request.is_json or (request.headers.get("X-Requested-With") or "").lower() == "xmlhttprequest"
    if is_ajax or request.path.startswith("/bodega/api/"):
        return jsonify({"success": False, "message": "Permiso denegado para modulo Bodega"}), 403
    flash("No tienes permisos para acceder al modulo Bodega.", "error")
    return redirect(url_for("productos.buscar"))


def _deny_bodega_perm(message: str):
    is_ajax = request.is_json or (request.headers.get("X-Requested-With") or "").lower() == "xmlhttprequest"
    if is_ajax or request.path.startswith("/bodega/api/"):
        return jsonify({"ok": False, "message": message}), 403
    flash(message, "error")
    return redirect(url_for("productos.buscar"))

DEFAULT_BODEGA = "Bodega 1"
DEFAULT_COUNTRY = "Chile"
ORIGEN_COMPRA_DEFAULT = "nacional"
ORIGEN_COMPRA_OPCIONES = ("nacional", "importacion")
CHILE_TZ = ZoneInfo("America/Santiago")


def _ingreso_resolve_ciudad_chile(region: str, comuna: str, ciudad_form: str) -> str:
    """Misma idea que Ventas: RM → Santiago si no viene ciudad; si no, comuna."""
    c = (ciudad_form or "").strip()
    if c:
        return c[:120]
    r = (region or "").strip().lower()
    if "metropolitana" in r and (comuna or "").strip():
        return "Santiago"
    return ((comuna or "").strip()[:120])


def _normalize_origen_compra(raw: str | None) -> str:
    v = (raw or "").strip().lower()
    if v in ORIGEN_COMPRA_OPCIONES:
        return v
    return ORIGEN_COMPRA_DEFAULT


def _proveedor_json_ingreso(proveedor: Proveedor) -> dict:
    emp = (proveedor.empresa or "").strip()
    nom = (proveedor.nombre or "").strip()
    contact = ""
    if emp and nom and nom.lower() != emp.lower():
        contact = nom
    name = emp or nom or ""
    return {
        "rut": format_rut(proveedor.rut or ""),
        "name": name,
        "contact": contact,
        "giro": (proveedor.giro or "").strip(),
        "email": (proveedor.email or "").strip(),
        "telefono": (proveedor.telefono or "").strip(),
        "address": (proveedor.direccion or "").strip(),
        "comuna": (proveedor.comuna or "").strip(),
        "region": (proveedor.region or "").strip(),
        "ciudad": (proveedor.ciudad or "").strip(),
        "country": (proveedor.pais or DEFAULT_COUNTRY).strip() or DEFAULT_COUNTRY,
    }

# Métodos de pago habituales hacia proveedores (ingreso de stock); el usuario puede elegir "Otro".
INGRESO_METODOS_PAGO_OPCIONES = [
    "Transferencia bancaria",
    "Transferencia electrónica",
    "Débito en cuenta",
    "Tarjeta débito / crédito",
    "Pago electrónico / Web",
    "Cheque al día",
    "Efectivo",
    "Pago en sucursal",
    "Crédito proveedor",
    "Convenio / pago a plazo",
    "Documento de pago",
]

AJUSTE_OBSERVACION_OPCIONES = [
    "Conteo físico",
    "Diferencia de inventario",
    "Merma / daño",
    "Corrección administrativa",
    "Regularización por recepción",
    "Regularización por despacho",
    "Ajuste por auditoría",
    "Reclasificación interna",
]


def _parse_metodo_pago_ingreso() -> str:
    return (request.form.get("metodo_pago") or "").strip()[:120]


def _online_users() -> list[Usuario]:
    try:
        threshold = datetime.utcnow() - timedelta(minutes=2)
        return (
            db.session.query(Usuario)
            .filter(Usuario.last_seen >= threshold)
            .order_by(Usuario.usuario.asc())
            .all()
        )
    except Exception:
        db.session.rollback()
        return []


def _producto_por_codigo(codigo: str):
    """Resuelve producto por código de catálogo. Acepta sufijo AA (ej. 2417AA → 2417) si no hay fila exacta."""
    raw = (codigo or "").strip().upper()
    if not raw:
        return None

    query = text(
        """
        SELECT
            CODIGO AS codigo,
            DESCRIPCION AS descripcion,
            MARCA AS marca,
            MODELO AS modelo,
                        COALESCE(P_PUBLICO, 0) AS precio_publico,
            COALESCE(STOCK_10JUL, 0) AS stock_actual
        FROM productos
        WHERE UPPER(TRIM(CODIGO)) = :codigo
          AND COALESCE(ACTIVO, 1) = 1
        LIMIT 1
        """
    )
    row = db.session.execute(query, {"codigo": raw}).mappings().first()
    if row:
        return row
    if len(raw) >= 3 and raw.endswith("AA"):
        base = raw[:-2].strip()
        if base:
            row = db.session.execute(query, {"codigo": base}).mappings().first()
            if row:
                return row
    return None


def _normalize_brand(raw: str) -> str:
    return (raw or "").strip().upper()


def _normalize_codigo_proveedor(raw: str) -> str:
    return (raw or "").strip().upper()


def _normalize_bodega(raw: str) -> str:
    value = (raw or "").strip()
    return value or DEFAULT_BODEGA


def _normalize_rut(raw: str) -> str:
    return clean_rut(raw)


def _is_valid_rut(raw: str) -> bool:
    return is_valid_rut(raw)


def _buscar_proveedor_por_rut(rut: str) -> Proveedor | None:
    normalized = _normalize_rut(rut)
    if not normalized:
        return None
    for proveedor in Proveedor.query.filter_by(activo=True).all():
        if _normalize_rut(proveedor.rut or "") == normalized:
            return proveedor
    return None


def _merge_ingreso_rows(rows: list[dict]) -> list[dict]:
    merged: dict[tuple[str, str, str, str], dict] = {}
    for row in rows:
        key = (
            (row.get("codigo") or "").strip().upper(),
            _normalize_brand(row.get("marca") or ""),
            _normalize_bodega(row.get("bodega") or ""),
            _normalize_origen_compra(row.get("origen_compra") or ""),
        )
        if key[0] == "":
            continue
        current = merged.get(key)
        if current is None:
            merged[key] = dict(row)
            merged[key]["codigo"] = key[0]
            merged[key]["marca"] = key[1]
            merged[key]["bodega"] = key[2]
            merged[key]["origen_compra"] = key[3]
        else:
            q_a = int(current.get("cantidad") or 0)
            q_b = int(row.get("cantidad") or 0)
            current["cantidad"] = q_a + q_b
            vn_a = current.get("valor_neto")
            vn_b = row.get("valor_neto")
            current["valor_neto"] = _merge_weighted_avg(vn_a, q_a, vn_b, q_b)
            current["precio_venta_neto"] = _merge_weighted_avg(
                current.get("precio_venta_neto"), q_a,
                row.get("precio_venta_neto"), q_b,
            )
            current["margen_pct"] = _merge_weighted_avg(
                current.get("margen_pct"), q_a,
                row.get("margen_pct"), q_b,
            )
            notes = [n for n in [current.get("nota", "").strip(), (row.get("nota") or "").strip()] if n]
            current["nota"] = " | ".join(dict.fromkeys(notes))[:255]
            cp_c = (current.get("codigo_proveedor") or "").strip()
            cp_r = (row.get("codigo_proveedor") or "").strip()
            if not cp_c and cp_r:
                current["codigo_proveedor"] = cp_r

    return list(merged.values())


def _upsert_mapa_proveedor_codigo(rut: str, codigo_prov_raw: str, codigo_interno: str) -> None:
    """Guarda o actualiza el vínculo código proveedor → código interno para futuras facturas."""
    rut_n = _normalize_rut(rut)
    cp = _normalize_codigo_proveedor(codigo_prov_raw)
    ci = (codigo_interno or "").strip().upper()
    if not rut_n or not cp or not ci:
        return
    row = ProveedorCodigoInterno.query.filter_by(proveedor_rut=rut_n, codigo_proveedor=cp).first()
    if row:
        row.codigo_interno = ci
        row.updated_at = datetime.utcnow()
    else:
        db.session.add(
            ProveedorCodigoInterno(
                proveedor_rut=rut_n,
                codigo_proveedor=cp,
                codigo_interno=ci,
            )
        )


def _parse_valor_neto_chile(raw: str) -> float | None:
    """Parse optional net amount (Chile: miles con punto, decimal con coma). Empty/invalid -> None."""
    s = (raw or "").strip().replace(" ", "")
    if not s:
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    if v < 0:
        return None
    return v


def _parse_margen_pct(raw: str) -> float | None:
    s = (raw or "").strip().replace("%", "").replace(",", ".").replace(" ", "")
    if not s:
        return None
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    if v < 0:
        return None
    return v


def _merge_weighted_avg(
    a: float | None, qa: int,
    b: float | None, qb: int,
) -> float | None:
    if a is None and b is None:
        return None
    if a is None:
        return float(b) if b is not None else None
    if b is None:
        return float(a) if a is not None else None
    qt = int(qa) + int(qb)
    if qt <= 0:
        return None
    return (float(a) * int(qa) + float(b) * int(qb)) / qt


def _parse_ingreso_rows() -> tuple[list[dict], list[str]]:
    codes_prov = request.form.getlist("codigo_proveedor_producto[]")
    codes = request.form.getlist("codigo_producto[]")
    brands = request.form.getlist("marca_producto[]")
    warehouses = request.form.getlist("bodega_producto[]")
    origins = request.form.getlist("origen_compra_producto[]")
    quantities = request.form.getlist("cantidad_producto[]")
    valores_neto = request.form.getlist("valor_neto_producto[]")
    margenes = request.form.getlist("margen_pct_producto[]")
    precios_venta = request.form.getlist("precio_venta_neto_producto[]")
    notes = request.form.getlist("nota_producto[]")

    max_len = max(
        len(codes_prov),
        len(codes),
        len(brands),
        len(warehouses),
        len(quantities),
        len(origins),
        len(valores_neto),
        len(margenes),
        len(precios_venta),
        len(notes),
        1,
    )
    rows: list[dict] = []
    errors: list[str] = []

    for idx in range(max_len):
        codigo = (codes[idx] if idx < len(codes) else "").strip().upper()
        marca = _normalize_brand(brands[idx] if idx < len(brands) else "")
        bodega = _normalize_bodega(warehouses[idx] if idx < len(warehouses) else "")
        origen_compra = _normalize_origen_compra(origins[idx] if idx < len(origins) else "")
        cantidad_raw = (quantities[idx] if idx < len(quantities) else "").strip()
        valor_neto_raw = (valores_neto[idx] if idx < len(valores_neto) else "").strip()
        margen_raw = (margenes[idx] if idx < len(margenes) else "").strip()
        precio_venta_raw = (precios_venta[idx] if idx < len(precios_venta) else "").strip()
        nota = (notes[idx] if idx < len(notes) else "").strip()[:255]
        codigo_prov = (codes_prov[idx] if idx < len(codes_prov) else "").strip()

        is_empty = not any([codigo, codigo_prov, marca, cantidad_raw, nota, valor_neto_raw, margen_raw, precio_venta_raw])
        if is_empty:
            continue

        cantidad = _parse_int(cantidad_raw)
        if not codigo:
            errors.append(f"Fila {idx + 1}: falta el codigo de producto.")
            continue
        if cantidad is None:
            errors.append(f"Fila {idx + 1}: la cantidad debe ser un entero mayor a 0.")
            continue

        valor_neto: float | None
        if valor_neto_raw:
            vn = _parse_valor_neto_chile(valor_neto_raw)
            if vn is None:
                errors.append(f"Fila {idx + 1}: el valor neto no es valido.")
                continue
            valor_neto = vn
        else:
            valor_neto = None

        margen_pct: float | None
        if margen_raw:
            mg = _parse_margen_pct(margen_raw)
            if mg is None:
                errors.append(f"Fila {idx + 1}: el margen % no es valido.")
                continue
            margen_pct = mg
        else:
            margen_pct = None

        precio_venta_neto: float | None
        if precio_venta_raw:
            pv = _parse_valor_neto_chile(precio_venta_raw)
            if pv is None:
                errors.append(f"Fila {idx + 1}: el precio de venta neto no es valido.")
                continue
            precio_venta_neto = pv
        else:
            precio_venta_neto = None

        if margen_pct is None:
            errors.append(f"Fila {idx + 1}: el margen % es obligatorio.")
            continue
        if margen_pct >= 100:
            errors.append(f"Fila {idx + 1}: el margen % debe ser menor a 100.")
            continue
        if precio_venta_neto is None:
            errors.append(f"Fila {idx + 1}: el precio de venta neto (P. neto) es obligatorio.")
            continue
        if precio_venta_neto <= 0:
            errors.append(f"Fila {idx + 1}: el precio de venta neto debe ser mayor a 0.")
            continue

        rows.append(
            {
                "codigo": codigo,
                "codigo_proveedor": codigo_prov,
                "marca": marca,
                "bodega": bodega,
                "origen_compra": origen_compra,
                "cantidad": int(cantidad),
                "valor_neto": valor_neto,
                "margen_pct": margen_pct,
                "precio_venta_neto": precio_venta_neto,
                "nota": nota,
            }
        )

    return _merge_ingreso_rows(rows), errors


def _stock_variantes_por_codigo(codigo: str) -> list[dict]:
    rows = (
        ProductoVarianteStock.query
        .filter_by(codigo_producto=codigo.upper())
        .order_by(
            ProductoVarianteStock.marca.asc(),
            ProductoVarianteStock.bodega.asc(),
            ProductoVarianteStock.origen_compra.asc(),
        )
        .all()
    )
    return [
        {
            "id": row.id,
            "codigo": row.codigo_producto,
            "marca": row.marca,
            "proveedor": row.proveedor or "",
            "bodega": row.bodega,
            "origen_compra": _normalize_origen_compra(getattr(row, "origen_compra", None)),
            "stock": int(row.stock or 0),
        }
        for row in rows
    ]


def _sincronizar_stock_base_desde_variantes(codigo: str) -> None:
    # Sin flush, el SUM puede leer la BD antes de aplicar cambios pendientes en variantes ORM
    # y dejar STOCK_10JUL desincronizado (ej. tras anular ingreso con marca/bodega).
    db.session.flush()
    total = db.session.execute(
        text(
            """
            SELECT COALESCE(SUM(stock), 0)
            FROM productos_variantes_stock
            WHERE UPPER(codigo_producto) = :codigo
            """
        ),
        {"codigo": codigo.upper()},
    ).scalar() or 0
    _actualizar_stock(codigo, int(total))


def _obtener_o_crear_variante(
    codigo: str,
    marca: str,
    bodega: str,
    origen_compra: str = ORIGEN_COMPRA_DEFAULT,
    proveedor: str | None = None,
) -> ProductoVarianteStock:
    codigo = codigo.upper()
    marca = _normalize_brand(marca)
    bodega = _normalize_bodega(bodega)
    origen = _normalize_origen_compra(origen_compra)
    variante = (
        ProductoVarianteStock.query
        .filter_by(codigo_producto=codigo, marca=marca, bodega=bodega, origen_compra=origen)
        .first()
    )
    if variante is None:
        variante = ProductoVarianteStock(
            codigo_producto=codigo,
            marca=marca,
            proveedor=(proveedor or "").strip()[:150] or None,
            bodega=bodega,
            origen_compra=origen,
            stock=0,
        )
        db.session.add(variante)
    elif proveedor:
        variante.proveedor = proveedor.strip()[:150]
    return variante


def _buscar_productos_para_etiquetas(search_term: str, limit: int = 30):
    term = (search_term or "").strip()
    if not term:
        return []

    compact = term.replace(" ", "")
    is_numeric = compact.isdigit()
    like = f"%{term}%"
    starts = f"{term}%"

    query = text(
        """
        SELECT
            CODIGO AS codigo,
            COALESCE(DESCRIPCION, '') AS descripcion,
            COALESCE(MODELO, '') AS modelo,
            COALESCE([CODIGO OEM], '') AS codigo_oem
        FROM productos
        WHERE COALESCE(ACTIVO, 1) = 1
          AND (
            UPPER(CODIGO) LIKE UPPER(:like)
            OR UPPER(COALESCE([CODIGO OEM], '')) LIKE UPPER(:like)
            OR UPPER(COALESCE(DESCRIPCION, '')) LIKE UPPER(:like)
          )
        ORDER BY
            CASE
                WHEN :is_numeric = 1 AND UPPER(CODIGO) LIKE UPPER(:starts) THEN 0
                WHEN :is_numeric = 1 AND UPPER(COALESCE([CODIGO OEM], '')) LIKE UPPER(:starts) THEN 1
                WHEN :is_numeric = 1 AND UPPER(COALESCE(DESCRIPCION, '')) LIKE UPPER(:starts) THEN 2
                WHEN :is_numeric = 0 AND UPPER(COALESCE(DESCRIPCION, '')) LIKE UPPER(:starts) THEN 0
                WHEN :is_numeric = 0 AND UPPER(COALESCE([CODIGO OEM], '')) LIKE UPPER(:starts) THEN 1
                WHEN :is_numeric = 0 AND UPPER(CODIGO) LIKE UPPER(:starts) THEN 2
                ELSE 3
            END,
            LENGTH(CODIGO) ASC,
            CODIGO ASC
        LIMIT :limit
        """
    )

    rows = db.session.execute(
        query,
        {
            "like": like,
            "starts": starts,
            "is_numeric": 1 if is_numeric else 0,
            "limit": max(1, min(limit, 100)),
        },
    ).mappings().all()
    return [dict(r) for r in rows]


def _actualizar_stock(codigo: str, nuevo_stock: int) -> None:
    db.session.execute(
        text(
            """
            UPDATE productos
            SET STOCK_10JUL = :nuevo_stock
            WHERE UPPER(CODIGO) = :codigo
            """
        ),
        {"codigo": codigo.upper(), "nuevo_stock": nuevo_stock},
    )


def _propagar_precio_venta_ingreso_a_catalogo(
    codigo: str,
    precio_venta_neto: float | None,
    variante: ProductoVarianteStock | None,
) -> None:
    """
    Si el ingreso trae P. venta neto, reflejarlo en catálogo para búsqueda/listados
    (columna P_PUBLICO en productos). En variantes, también precio_publico_neto_override.
    """
    if precio_venta_neto is None:
        return
    try:
        pv = round(float(precio_venta_neto), 2)
    except (TypeError, ValueError):
        return
    if pv <= 0:
        return
    c = (codigo or "").strip()
    if not c:
        return
    db.session.execute(
        text(
            """
            UPDATE productos
            SET P_PUBLICO = :pv
            WHERE UPPER(TRIM(CODIGO)) = UPPER(TRIM(:codigo))
              AND COALESCE(ACTIVO, 1) = 1
            """
        ),
        {"pv": pv, "codigo": c},
    )
    if variante is not None:
        variante.precio_publico_neto_override = pv


def _registrar_movimiento(
    codigo: str,
    tipo: str,
    cantidad: int,
    observacion: str,
    proveedor: str | None = None,
    marca: str | None = None,
    bodega: str | None = None,
    origen_compra: str = ORIGEN_COMPRA_DEFAULT,
    ingreso_documento_id: int | None = None,
) -> None:
    codigo_up = codigo.upper()
    db.session.add(
        MovimientoStock(
            codigo_producto=codigo_up,
            tipo=tipo,
            cantidad=cantidad,
            usuario=session.get("user") or "sistema",
            proveedor=proveedor[:150] if proveedor else None,
            marca=_normalize_brand(marca) if marca else None,
            bodega=_normalize_bodega(bodega) if bodega else None,
            origen_compra=_normalize_origen_compra(origen_compra),
            ingreso_documento_id=ingreso_documento_id,
            observacion=observacion[:255] if observacion else None,
        )
    )
    # Auditoría en la misma sesión que el movimiento (un solo commit al final).
    # SAVEPOINT: si falla el INSERT de auditoría, no se pierde el movimiento (mismo comportamiento que antes).
    try:
        with db.session.begin_nested():
            row = db.session.execute(
                text(
                    """
                    SELECT COALESCE([STOCK_10JUL], 0) + COALESCE(STOCK_BRASIL, 0) + COALESCE(STOCK_G_AVENIDA, 0)
                         + COALESCE(STOCK_ORIENTALES, 0) + COALESCE(STOCK_B20_OUTLET, 0) + COALESCE(STOCK_TRANSITO, 0)
                    FROM productos
                    WHERE UPPER(CODIGO) = :c
                    """
                ),
                {"c": codigo_up},
            ).fetchone()
            stock_total = int(row[0] or 0) if row else 0
            metadata = {
                "tipo": tipo,
                "cantidad_movimiento": cantidad,
                "stock_total_actual": stock_total,
                "marca": marca,
                "bodega": bodega,
                "origen_compra": _normalize_origen_compra(origen_compra),
                "proveedor": proveedor,
                "observacion": observacion,
            }
            meta_text = json.dumps(metadata, ensure_ascii=False, default=str)
            actor = (session.get("user") or "sistema").strip() or "sistema"
            ua = (request.headers.get("User-Agent") or "")[:255] or None
            db.session.execute(
                text(
                    """
                    INSERT INTO productos_audit_eventos
                        (created_at, actor, action, modulo, producto_codigo, ip, user_agent, request_path, metadata_json)
                    VALUES
                        (:created_at, :actor, :action, :modulo, :producto_codigo, :ip, :user_agent, :request_path, :metadata_json)
                    """
                ),
                {
                    "created_at": datetime.utcnow(),
                    "actor": actor,
                    "action": "stock_move",
                    "modulo": "bodega",
                    "producto_codigo": codigo_up,
                    "ip": request.remote_addr,
                    "user_agent": ua,
                    "request_path": request.path,
                    "metadata_json": meta_text,
                },
            )
    except Exception:
        try:
            current_app.logger.warning("Auditoria stock_move no registrada (savepoint revertido).", exc_info=True)
        except Exception:
            pass


def _parse_int(raw_value: str, allow_zero: bool = False):
    try:
        value = int((raw_value or "").strip())
    except (TypeError, ValueError):
        return None
    if allow_zero:
        return value if value >= 0 else None
    return value if value > 0 else None


def _base_context(active_page: str, **extra):
    context = {
        "active_page": active_page,
        "online_users": _online_users(),
    }
    context.update(extra)
    return context


def _aplicar_movimiento_stock(
    codigo: str,
    tipo: str,
    cantidad_movimiento: int,
    nuevo_stock: int,
    observacion: str,
    proveedor: str | None = None,
    marca: str | None = None,
    bodega: str | None = None,
) -> None:
    _actualizar_stock(codigo, nuevo_stock)
    _registrar_movimiento(
        codigo,
        tipo,
        cantidad_movimiento,
        observacion,
        proveedor=proveedor,
        marca=marca,
        bodega=bodega,
    )
    db.session.commit()


def _aplicar_movimiento_variante(
    codigo: str,
    tipo: str,
    cantidad_movimiento: int,
    observacion: str,
    marca: str,
    bodega: str,
    origen_compra: str = ORIGEN_COMPRA_DEFAULT,
    proveedor: str | None = None,
    nuevo_stock_variante: int | None = None,
    *,
    commit: bool = True,
) -> int:
    variante = _obtener_o_crear_variante(codigo, marca, bodega, origen_compra=origen_compra, proveedor=proveedor)
    stock_anterior = int(variante.stock or 0)

    if nuevo_stock_variante is None:
        candidato = stock_anterior + cantidad_movimiento
    else:
        candidato = int(nuevo_stock_variante)

    if candidato < 0:
        raise ValueError(f"No puedes dejar stock negativo en la variante. Disponible: {stock_anterior}")

    variante.stock = candidato
    _sincronizar_stock_base_desde_variantes(codigo)

    _registrar_movimiento(
        codigo,
        tipo,
        cantidad_movimiento,
        observacion,
        proveedor=proveedor,
        marca=marca,
        bodega=bodega,
        origen_compra=origen_compra,
    )
    if commit:
        db.session.commit()
    return candidato


def _lineas_ajuste_desde_form(form) -> list[tuple[str, int]]:
    """Pares (marca, nuevo_stock) desde linea_marca[] / linea_nuevo_stock[]. Última fila gana si hay marca repetida."""
    marcas = form.getlist("linea_marca")
    nuevos = form.getlist("linea_nuevo_stock")
    n = max(len(marcas), len(nuevos))
    por_marca: dict[str, int] = {}
    orden: list[str] = []
    for i in range(n):
        m = _normalize_brand(marcas[i] if i < len(marcas) else "")
        ns_raw = (nuevos[i] if i < len(nuevos) else "").strip()
        if not m and not ns_raw:
            continue
        if not m:
            raise ValueError("Hay una fila con nuevo stock pero sin marca.")
        if not ns_raw:
            continue
        ns = _parse_int(ns_raw, allow_zero=True)
        if ns is None:
            raise ValueError(f"El nuevo stock no es válido para la marca «{m}».")
        if m not in por_marca:
            orden.append(m)
        por_marca[m] = ns
    return [(m, por_marca[m]) for m in orden]


def _form_data_lineas_ajuste(form) -> list[dict]:
    marcas = form.getlist("linea_marca")
    nuevos = form.getlist("linea_nuevo_stock")
    n = max(len(marcas), len(nuevos))
    out = []
    for i in range(n):
        out.append(
            {
                "marca": (marcas[i] if i < len(marcas) else "") or "",
                "nuevo_stock": (nuevos[i] if i < len(nuevos) else "") or "",
            }
        )
    return out


def _ajuste_lineas_con_stock_actual(
    lineas: list[dict], variantes_bodega: list[dict]
) -> list[dict]:
    by_m = {
        _normalize_brand(v.get("marca") or ""): int(v.get("stock") or 0)
        for v in (variantes_bodega or [])
    }
    out: list[dict] = []
    for row in lineas or []:
        m = _normalize_brand(row.get("marca") or "")
        out.append(
            {
                "marca": row.get("marca") or "",
                "nuevo_stock": row.get("nuevo_stock") or "",
                "stock_actual": by_m.get(m) if m in by_m else None,
            }
        )
    return out


def _requiere_variante(codigo: str, marca: str) -> bool:
    marca_norm = _normalize_brand(marca)
    if marca_norm:
        return True
    existe = db.session.execute(
        text(
            """
            SELECT 1
            FROM productos_variantes_stock
            WHERE UPPER(codigo_producto) = :codigo
            LIMIT 1
            """
        ),
        {"codigo": codigo.upper()},
    ).first()
    return existe is not None


def _ingreso_edit_transfer_stock_entre_marcas(
    it: IngresoDocumentoItem,
    doc: IngresoDocumento,
    old_q: int,
    new_q: int,
    old_m_norm: str,
    new_m_norm: str,
    observacion_raw: str,
) -> None:
    """Retira old_q del bucket de marca anterior y registra new_q en la nueva (cambio de marca en línea de ingreso)."""
    codigo = (it.codigo_producto or "").strip().upper()
    bodega = _normalize_bodega(it.bodega or "")
    origen_compra = _normalize_origen_compra(getattr(it, "origen_compra", None))
    base_obs = (observacion_raw or "").strip() or "Edición de ingreso ERP"
    doc_obs = f"Doc {doc.id} editado (cambio marca): {base_obs}"[:255]

    marca_old = _normalize_brand(old_m_norm)
    marca_new = _normalize_brand(new_m_norm)

    if old_q > 0:
        if _requiere_variante(codigo, marca_old):
            variante_old = (
                ProductoVarianteStock.query.filter_by(
                    codigo_producto=codigo,
                    marca=marca_old,
                    bodega=bodega,
                    origen_compra=origen_compra,
                ).first()
            )
            prev = int(variante_old.stock or 0) if variante_old else 0
            candidato = prev - old_q
            if candidato < 0:
                raise ValueError(
                    f"No hay stock suficiente para liberar {codigo} {marca_old or '(sin marca)'} {bodega}."
                )
            if variante_old is not None:
                variante_old.stock = candidato
                _sincronizar_stock_base_desde_variantes(codigo)
        else:
            stock_actual = _stock_actual_catalogo(codigo)
            candidato = stock_actual - old_q
            if candidato < 0:
                raise ValueError(f"No hay stock suficiente para liberar {codigo}.")
            _actualizar_stock(codigo, candidato)

        _registrar_movimiento(
            codigo,
            "salida",
            old_q,
            doc_obs,
            proveedor=doc.proveedor_nombre,
            marca=marca_old or None,
            bodega=bodega,
            origen_compra=origen_compra,
            ingreso_documento_id=doc.id,
        )

    if new_q > 0:
        if _requiere_variante(codigo, marca_new):
            variante_new = _obtener_o_crear_variante(
                codigo,
                marca_new,
                bodega,
                origen_compra=origen_compra,
                proveedor=doc.proveedor_nombre,
            )
            candidato = int(variante_new.stock or 0) + new_q
            if candidato < 0:
                raise ValueError(
                    f"No puedes dejar stock negativo en {codigo} {marca_new or '(sin marca)'}."
                )
            variante_new.stock = candidato
            _sincronizar_stock_base_desde_variantes(codigo)
        else:
            stock_actual = _stock_actual_catalogo(codigo)
            candidato = stock_actual + new_q
            if candidato < 0:
                raise ValueError(f"No puedes dejar stock negativo en {codigo}.")
            _actualizar_stock(codigo, candidato)

        _registrar_movimiento(
            codigo,
            "ingreso",
            new_q,
            doc_obs,
            proveedor=doc.proveedor_nombre,
            marca=marca_new or None,
            bodega=bodega,
            origen_compra=origen_compra,
            ingreso_documento_id=doc.id,
        )


def _stock_actual_catalogo(codigo: str) -> int:
    row = db.session.execute(
        text(
            """
            SELECT COALESCE(STOCK_10JUL, 0)
            FROM productos
            WHERE UPPER(CODIGO) = :codigo
            LIMIT 1
            """
        ),
        {"codigo": (codigo or "").strip().upper()},
    ).fetchone()
    return int(row[0] or 0) if row else 0


def _validar_autorizacion_anulacion_ingreso(username: str, password: str) -> tuple[bool, str, Usuario | None]:
    user_name = (username or "").strip()
    raw_pass = password or ""
    if not user_name or not raw_pass:
        return False, "Debes ingresar usuario y contraseña para autorizar la anulación.", None

    u = Usuario.query.filter_by(usuario=user_name).first()
    if u is None:
        return False, "Usuario de autorización no válido.", None
    if not bool(u.activo):
        return False, "El usuario de autorización está inactivo.", None
    if bool(getattr(u, "bloqueado_seguridad", False)):
        return False, "El usuario de autorización está bloqueado.", None

    try:
        ok = check_password_hash(u.password_hash or "", raw_pass)
    except Exception:
        ok = False
    if not ok:
        return False, "Contraseña de autorización incorrecta.", None

    rol_name = (u.rol.nombre if getattr(u, "rol", None) and u.rol.nombre else "") or ""
    if not has_permission(u.usuario, rol_name, "bodega_ingreso"):
        return False, "El usuario no tiene permiso para anular ingresos.", None
    return True, "", u


def _bodegas_para_select() -> list[str]:
    try:
        return bodega_catalogo.list_bodegas_operativas()
    except Exception:
        names: list[str] = [DEFAULT_BODEGA]
        try:
            from app.utils.stock_control import get_all_warehouses

            names.extend(get_all_warehouses())
        except Exception:
            pass
        for i in range(2, 6):
            names.append(f"Bodega {i}")
        seen: set[str] = set()
        ordered: list[str] = []
        for raw in names:
            b = (raw or "").strip()
            if not b or b in seen:
                continue
            seen.add(b)
            ordered.append(b)

        def _sort_key(name: str) -> tuple:
            m = re.match(r"^Bodega\s+(\d+)$", name, re.I)
            if m:
                return (0, int(m.group(1)))
            return (1, name.lower())

        return sorted(ordered, key=_sort_key)


def _suma_stock_variantes_en_bodega(variantes: list[dict], bodega: str) -> int:
    b = _normalize_bodega(bodega)
    return sum(int(v.get("stock") or 0) for v in variantes if (v.get("bodega") or "").strip() == b)


def _suma_stock_variantes_total(variantes: list[dict]) -> int:
    return sum(int(v.get("stock") or 0) for v in variantes)


def _variantes_agrupadas_por_bodega(variantes: list[dict]) -> list[dict]:
    """Agrupa filas de variante por bodega para resumen en UI (ej. Salida)."""
    from collections import defaultdict

    by: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for v in variantes:
        b = (v.get("bodega") or "").strip()
        if not b:
            continue
        m = (v.get("marca") or "").strip() or "—"
        by[b].append((m, int(v.get("stock") or 0)))

    def _bk(name: str) -> tuple:
        mm = re.match(r"^Bodega\s+(\d+)$", name, re.I)
        if mm:
            return (0, int(mm.group(1)))
        return (1, name.lower())

    out: list[dict] = []
    for bodega in sorted(by.keys(), key=_bk):
        lineas = sorted(by[bodega], key=lambda x: x[0].upper())
        subtotal = sum(s for _, s in lineas)
        out.append(
            {
                "bodega": bodega,
                "lineas": [{"marca": m, "stock": s} for m, s in lineas],
                "subtotal": subtotal,
            }
        )
    return out


def _stock_contexto_para_ajuste(
    codigo: str, marca: str, bodega: str, producto, variantes: list[dict]
) -> int | None:
    if not producto:
        return None
    code = codigo.upper()
    marca_n = _normalize_brand(marca)
    if variantes:
        if marca_n:
            variante = (
                ProductoVarianteStock.query.filter_by(
                    codigo_producto=code,
                    marca=marca_n,
                    bodega=_normalize_bodega(bodega),
                ).first()
            )
            return int(variante.stock or 0) if variante else 0
        return _suma_stock_variantes_en_bodega(variantes, bodega)
    return int(producto["stock_actual"] or 0)


def _cargar_vista_ajuste(form_data: dict) -> tuple:
    codigo = (form_data.get("codigo") or "").strip()
    if not codigo:
        return None, [], None
    producto = _producto_por_codigo(codigo)
    if not producto:
        return None, [], None
    variantes = _stock_variantes_por_codigo(codigo)
    stock_ctx = _stock_contexto_para_ajuste(
        codigo,
        form_data.get("marca") or "",
        form_data.get("bodega") or "",
        producto,
        variantes,
    )
    return producto, variantes, stock_ctx


def generar_qr(codigo: str) -> str:
    qr = qrcode.QRCode(version=1, box_size=8, border=1)
    qr.add_data(codigo)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


def generar_barcode(codigo: str) -> str:
    code128 = barcode.get("code128", codigo, writer=ImageWriter())
    buffer = io.BytesIO()
    code128.write(
        buffer,
        options={
            "module_width": 0.38,
            "module_height": 18,
            "quiet_zone": 2,
            "font_size": 0,
            "text_distance": 0,
            "dpi": 300,
            "write_text": False,
        },
    )
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


def _normalize_codigos_input(raw: str) -> str:
    """Unifica signos de multiplicación y espacios para que x2 / ×2 / *2 se interpreten igual."""
    if not raw:
        return ""
    t = raw.replace("\u00a0", " ").replace("\u202f", " ").replace("\u2009", " ")
    for ch in ("\u00d7", "\u2715", "\u2716", "\u2717"):
        t = t.replace(ch, "x")
    t = t.replace("\uff58", "x")
    return t


def _split_glued_x_qty(part: str) -> str:
    """CODE123x4 -> CODE123 x4. Solo 'x' minúscula (evita trocear códigos que terminen en X mayúscula)."""
    return re.sub(r"(?<=[A-Za-z0-9])(x)(\d+)$", r" \1\2", part or "")


def _parse_codigos(raw_codes: str) -> list[str]:
    raw_codes = _normalize_codigos_input((raw_codes or "").strip())
    expanded_codes = []
    chunks = re.split(r"[\n,;]+", raw_codes or "")

    for raw_part in chunks:
        part = (raw_part or "").strip()
        if not part:
            continue
        # "CODE x" o "CODE *" sin número (estado intermedio al escribir) → solo el código, cantidad 1
        incomplete = re.match(
            r"^([A-Za-z0-9._\-/]+)\s*(?:x|\*)\s*$",
            part,
            flags=re.IGNORECASE,
        )
        if incomplete:
            part = incomplete.group(1)
        part = _split_glued_x_qty(part)

        match = re.match(r"^([A-Za-z0-9._\-/]+)(?:\s*(?:x|\*)\s*(\d+))?$", part, flags=re.IGNORECASE)
        if not match:
            code = part.upper()
            qty = 1
        else:
            code = (match.group(1) or "").upper()
            qty = int(match.group(2) or "1")

        if not code:
            continue

        qty = max(1, min(qty, 50))
        for _ in range(qty):
            expanded_codes.append(code)
            if len(expanded_codes) >= 400:
                return expanded_codes

    return expanded_codes


def _font_class_for_name(nombre: str) -> str:
    n = len((nombre or "").strip())
    if n > 50:
        return "name-xs"
    if n > 34:
        return "name-sm"
    return ""


def _build_labels_from_codes(
    codes: list[str],
    fp: str,
    enrich_prev: dict[str, dict[str, str]] | None = None,
    fp_por_codigo: dict[str, str] | None = None,
):
    """Una etiqueta por entrada expandida. Código en etiqueta = lo escrito (p. ej. TOU001AA).
    Si `enrich_prev` trae descripcion/modelo de la última vista (sesión), se reutiliza: así x2/x3 no
    vuelven a la BD y no cambian textos. Si no, se enriquece con catálogo o placeholders.
    F° P: con más de un código distinto, solo cuenta `fp_por_codigo` por código; el `fp` global no rellena huecos."""
    enrich_prev = enrich_prev or {}
    labels = []
    missing = []
    fp_global = (fp or "").strip()
    distinct_display = {(c or "").strip().upper() for c in codes if (c or "").strip()}
    # Un solo código en el lote: el F° P del formulario aplica (ingreso manual / sin historial).
    # Varios códigos: cada etiqueta solo usa el F° P de su mapa (último ingreso); no mezclar con el global.
    use_global_fp_fallback = len(distinct_display) <= 1
    fp_map = fp_por_codigo or {}
    for code in codes:
        display = (code or "").strip().upper()
        if not display:
            continue
        frozen = enrich_prev.get(display)
        if frozen is not None:
            descripcion = (frozen.get("descripcion") or "").strip() or "SIN DESCRIPCION"
            modelo = (frozen.get("modelo") or "").strip()
        else:
            producto = _producto_por_codigo(display)
            if producto is None:
                missing.append(display)
                descripcion = "SIN DESCRIPCION"
                modelo = ""
            else:
                descripcion = (producto.get("descripcion") or "SIN DESCRIPCION").strip()
                modelo = (producto.get("modelo") or "").strip()
        label_fp = fp_global if use_global_fp_fallback else ""
        mapped = (fp_map.get(display) or "").strip()
        if mapped:
            label_fp = mapped
        labels.append(
            {
                "codigo": display,
                "nombre": descripcion,
                "descripcion": descripcion,
                "modelo": modelo,
                "fp": label_fp,
                "name_class": _font_class_for_name(descripcion),
                "qr_base64": generar_qr(display),
                "barcode_base64": generar_barcode(display),
            }
        )
    return labels, missing


MAX_HISTORIAL_REPRINT_IDS = 80
MAX_LABELS_EN_VISTA = 400
# Textos por código mostrado (p. ej. TOU001AA): evita que x2/x3 reconsulten la BD y cambien descripción/modelo.
ETIQUETAS_ENRICH_SESSION_KEY = "bodega_etiquetas_enrich"


def _etiquetas_enrich_prev_for_codes(codes: list[str]) -> dict[str, dict[str, str]]:
    seen = {(c or "").strip().upper() for c in codes if (c or "").strip()}
    raw = session.get(ETIQUETAS_ENRICH_SESSION_KEY)
    if not isinstance(raw, dict) or not seen:
        return {}
    out: dict[str, dict[str, str]] = {}
    for k in seen:
        v = raw.get(k)
        if isinstance(v, dict):
            out[k] = {
                "descripcion": (v.get("descripcion") or "").strip(),
                "modelo": (v.get("modelo") or "").strip(),
            }
    return out


def _persist_etiquetas_enrich_from_labels(labels: list[dict]) -> None:
    enrich: dict[str, dict[str, str]] = {}
    for lbl in labels:
        c = (lbl.get("codigo") or "").strip().upper()
        if not c:
            continue
        enrich[c] = {
            "descripcion": (lbl.get("descripcion") or lbl.get("nombre") or "").strip(),
            "modelo": (lbl.get("modelo") or "").strip(),
        }
    session[ETIQUETAS_ENRICH_SESSION_KEY] = enrich
    session.modified = True


def _historial_reprint_qty_from_request(hid: int) -> int | None:
    """Cantidad por fila en reimpresión masiva (historial_qty_<id>). None = usar la del registro."""
    raw = request.values.get(f"historial_qty_{hid}")
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return max(1, min(int(raw), 50))
    except (ValueError, TypeError):
        return None


def _build_labels_from_historial_row(
    item: HistorialEtiqueta, fp: str, override_qty: int | None = None
) -> list[dict]:
    """Reimprime usando solo el snapshot guardado (código inexistente en catálogo)."""
    code = (item.codigo_producto or "").strip().upper()
    if not code:
        return []
    descripcion = (item.descripcion or "SIN DESCRIPCION").strip()
    modelo = (item.modelo or "").strip()
    if override_qty is not None:
        qty = max(1, min(int(override_qty), 50))
    else:
        qty = max(1, min(int(item.cantidad or 1), 50))
    labels: list[dict] = []
    for _ in range(qty):
        labels.append(
            {
                "codigo": code,
                "nombre": descripcion,
                "descripcion": descripcion,
                "modelo": modelo,
                "fp": fp,
                "name_class": _font_class_for_name(descripcion),
                "qr_base64": generar_qr(code),
                "barcode_base64": generar_barcode(code),
            }
        )
    return labels


def _registrar_historial_etiquetas(labels: list[dict]) -> tuple[bool, str | None]:
    if not labels:
        return True, None

    aggregated: dict[tuple[str, str, str], int] = {}
    for label in labels:
        codigo = (label.get("codigo") or "").strip().upper()
        descripcion = (label.get("descripcion") or label.get("nombre") or "").strip()
        modelo = (label.get("modelo") or "").strip()
        if not codigo or not descripcion:
            continue
        key = (codigo, descripcion, modelo)
        aggregated[key] = aggregated.get(key, 0) + 1

    if not aggregated:
        return True, None

    usuario = session.get("user") or "sistema"
    items = [
        HistorialEtiqueta(
            codigo_producto=codigo,
            descripcion=descripcion,
            modelo=modelo,
            cantidad=cantidad,
            usuario=usuario,
        )
        for (codigo, descripcion, modelo), cantidad in aggregated.items()
    ]

    try:
        print(f"[BODEGA_LABEL_HISTORY] Attempting to save {len(items)} record(s)")
        db.session.add_all(items)
        db.session.commit()
        print("[BODEGA_LABEL_HISTORY] Commit successful")
        return True, None
    except Exception as exc:
        db.session.rollback()
        print(f"[BODEGA_LABEL_HISTORY] Commit failed: {exc}")
        return False, str(exc)


@bodega_bp.route("/etiquetas/historial/register", methods=["POST"])
@admin_required
def etiquetas_historial_register():
    payload = request.get_json(silent=True) or {}
    labels = payload.get("labels") or []

    print(f"[BODEGA_LABEL_HISTORY] Register endpoint payload labels: {len(labels) if isinstance(labels, list) else 'invalid'}")

    if not isinstance(labels, list) or not labels:
        return jsonify({"ok": False, "error": "No se recibieron etiquetas para registrar"}), 400

    saved, err = _registrar_historial_etiquetas(labels)
    if not saved:
        return jsonify({"ok": False, "error": err or "No se pudo guardar historial"}), 500

    return jsonify({"ok": True, "saved": len(labels)})


@bodega_bp.route("/bodegas", methods=["GET", "POST"])
@admin_required
def bodegas_catalogo():
    bodega_catalogo.seed_catalogo_if_empty()
    nombres_pre = {(r.nombre or "").strip() for r in CatalogoBodega.query.all()}
    conteos_pre = bodega_catalogo.conteos_variantes_por_bodega()
    recien_agregadas = sorted(
        [n for n in conteos_pre if n not in nombres_pre],
        key=str.lower,
    )
    bodega_catalogo.sync_new_warehouses_into_catalogo()

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        if action == "add":
            nombre = (request.form.get("nombre") or "").strip()
            orden_raw = (request.form.get("orden") or "").strip()
            orden_val = int(orden_raw) if orden_raw.lstrip("-").isdigit() else None
            err = bodega_catalogo.crear_bodega_catalogo(nombre, orden_val)
            if err:
                flash(err, "error")
            else:
                flash(f"Bodega «{nombre}» creada.", "success")
        elif action == "update":
            row_id = request.form.get("id", type=int)
            row = CatalogoBodega.query.get(row_id) if row_id else None
            if not row:
                flash("Registro no encontrado.", "error")
            else:
                nuevo = (request.form.get("nombre") or "").strip()
                orden_raw = (request.form.get("orden") or "0").strip()
                try:
                    orden_v = int(orden_raw)
                except ValueError:
                    orden_v = row.orden or 0
                activo = request.form.get("activo") in ("1", "on", "true", "yes")
                nota = (request.form.get("nota") or "").strip()
                err = bodega_catalogo.actualizar_fila_catalogo(row, nuevo, orden_v, activo, nota)
                if err:
                    flash(err, "error")
                else:
                    flash("Bodega actualizada.", "success")
        return redirect(url_for("bodega.bodegas_catalogo"))

    filas = (
        CatalogoBodega.query.order_by(CatalogoBodega.orden.asc(), CatalogoBodega.nombre.asc()).all()
    )
    conteos = bodega_catalogo.conteos_variantes_por_bodega()

    return render_template(
        "bodega/bodegas_catalogo.html",
        **_base_context(
            "bodegas",
            filas=filas,
            conteos=conteos,
            recien_agregadas=recien_agregadas,
        ),
    )


@bodega_bp.route("/")
@admin_required
def index():
    total_movimientos = MovimientoStock.query.count()
    movimientos_hoy = MovimientoStock.query.filter(
        MovimientoStock.fecha >= datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    ).count()

    resumen = db.session.execute(
        text(
            """
            SELECT
                COUNT(*) AS total_productos,
                SUM(CASE WHEN COALESCE(STOCK_10JUL, 0) <= 0 THEN 1 ELSE 0 END) AS sin_stock,
                SUM(CASE WHEN COALESCE(STOCK_10JUL, 0) BETWEEN 1 AND 5 THEN 1 ELSE 0 END) AS bajo_stock
            FROM productos
            WHERE COALESCE(ACTIVO, 1) = 1
            """
        )
    ).mappings().first()

    recientes = (
        MovimientoStock.query.order_by(MovimientoStock.fecha.desc())
        .limit(8)
        .all()
    )

    return render_template(
        "bodega/index.html",
        **_base_context(
            "index",
            total_movimientos=total_movimientos,
            movimientos_hoy=movimientos_hoy,
            resumen=resumen,
            recientes=recientes,
        ),
    )


@bodega_bp.route("/ingreso", methods=["GET", "POST"])
@admin_required
def ingreso():
    if request.method == "POST" and not has_permission(session.get("user"), session.get("rol"), "bodega_ingreso"):
        return _deny_bodega_perm("No tienes permiso para registrar ingresos de stock.")
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    form_data = {
        "supplier_rut": _normalize_rut(request.form.get("supplier_rut") or ""),
        "supplier_name": (request.form.get("supplier_name") or "").strip(),
        "supplier_contact": (request.form.get("supplier_contact") or "").strip(),
        "supplier_giro": (request.form.get("supplier_giro") or "").strip(),
        "supplier_email": (request.form.get("supplier_email") or "").strip(),
        "supplier_telefono": (request.form.get("supplier_telefono") or "").strip()[:80],
        "supplier_address": (request.form.get("supplier_address") or "").strip(),
        "supplier_comuna": (request.form.get("supplier_comuna") or "").strip(),
        "supplier_region": (request.form.get("supplier_region") or "").strip(),
        "supplier_ciudad": (request.form.get("supplier_ciudad") or "").strip(),
        "supplier_country": (request.form.get("supplier_country") or DEFAULT_COUNTRY).strip() or DEFAULT_COUNTRY,
        "fecha_documento": (request.form.get("fecha_documento") or today_str).strip() or today_str,
        "numero_documento": (request.form.get("numero_documento") or "").strip(),
        "observacion": (request.form.get("observacion") or "").strip()[:255],
        "metodo_pago": _parse_metodo_pago_ingreso() if request.method == "POST" else "",
        "total_factura": (request.form.get("total_factura") or "").strip()
        if request.method == "POST"
        else "",
    }

    default_rows = [
        {
            "codigo_proveedor": "",
            "codigo": "",
            "marca": "",
            "bodega": DEFAULT_BODEGA,
            "origen_compra": ORIGEN_COMPRA_DEFAULT,
            "cantidad": "",
            "valor_neto": None,
            "margen_pct": None,
            "precio_venta_neto": None,
            "nota": "",
        }
    ]
    rows = default_rows
    supplier_found = False
    created_supplier_inline = False
    document_created = None
    message = None

    if request.method == "POST":
        rows, row_errors = _parse_ingreso_rows()
        if not rows:
            rows = default_rows

        if not form_data["supplier_rut"]:
            message = {"type": "error", "text": "Debes ingresar el RUT del proveedor."}
        elif not form_data["numero_documento"]:
            message = {"type": "error", "text": "Debes ingresar el número de factura/documento del proveedor."}
        elif not form_data["metodo_pago"]:
            message = {"type": "error", "text": "Debes seleccionar el método de pago."}
        elif not _is_valid_rut(form_data["supplier_rut"]):
            message = {"type": "error", "text": "El RUT del proveedor no es valido."}
        elif row_errors:
            message = {"type": "error", "text": row_errors[0]}
        elif not rows or not rows[0].get("codigo"):
            message = {"type": "error", "text": "Debes agregar al menos un producto para el ingreso."}
        else:
            proveedor = _buscar_proveedor_por_rut(form_data["supplier_rut"])
            if proveedor is not None:
                supplier_found = True
                pj = _proveedor_json_ingreso(proveedor)
                form_data["supplier_name"] = pj["name"]
                form_data["supplier_contact"] = pj["contact"]
                form_data["supplier_giro"] = pj["giro"]
                form_data["supplier_email"] = pj["email"]
                form_data["supplier_telefono"] = pj["telefono"][:80]
                form_data["supplier_address"] = pj["address"]
                form_data["supplier_comuna"] = pj["comuna"]
                form_data["supplier_region"] = pj["region"]
                form_data["supplier_ciudad"] = pj["ciudad"]
                form_data["supplier_country"] = pj["country"]
            else:
                required_for_new = [
                    form_data["supplier_name"],
                    form_data["supplier_address"],
                    form_data["supplier_comuna"],
                    form_data["supplier_region"],
                ]
                if not all(required_for_new):
                    message = {
                        "type": "error",
                        "text": "Proveedor no encontrado. Completa empresa, calle y número, comuna y región para crearlo en línea.",
                    }

            if message is None:
                try:
                    proveedor = _buscar_proveedor_por_rut(form_data["supplier_rut"])
                    if proveedor is None:
                        emp = form_data["supplier_name"][:200]
                        con = (form_data.get("supplier_contact") or "").strip()[:200]
                        nom = con if con else emp
                        ciudad_n = _ingreso_resolve_ciudad_chile(
                            form_data["supplier_region"],
                            form_data["supplier_comuna"],
                            form_data.get("supplier_ciudad") or "",
                        )
                        tel_n = phone_to_compact_e164(
                            form_data["supplier_telefono"], form_data["supplier_country"]
                        )[:50]
                        proveedor = Proveedor(
                            nombre=nom,
                            empresa=emp,
                            rut=form_data["supplier_rut"],
                            giro=form_data["supplier_giro"][:200],
                            direccion=form_data["supplier_address"][:300],
                            comuna=form_data["supplier_comuna"][:120],
                            region=form_data["supplier_region"][:120],
                            ciudad=ciudad_n[:120],
                            pais=form_data["supplier_country"][:120],
                            email=form_data["supplier_email"][:150],
                            telefono=tel_n,
                            activo=True,
                        )
                        db.session.add(proveedor)
                        db.session.flush()
                        created_supplier_inline = True

                    fecha_documento = datetime.strptime(form_data["fecha_documento"], "%Y-%m-%d").date()

                    sum_neto_lines = sum(
                        float(r.get("valor_neto") or 0) * int(r.get("cantidad") or 0)
                        for r in rows
                    )
                    total_factura_raw = (form_data.get("total_factura") or "").strip()
                    total_factura_val: float | None = None
                    iva_factura_val: float | None = None
                    if total_factura_raw:
                        tf = _parse_valor_neto_chile(total_factura_raw)
                        if tf is None:
                            raise ValueError("El total de factura (con IVA) no es válido.")
                        if sum_neto_lines <= 0:
                            raise ValueError(
                                "Para cuadrar con la factura física, ingresá el valor neto en cada línea y el total con IVA."
                            )
                        if float(tf) + 1e-6 < float(sum_neto_lines):
                            raise ValueError(
                                "El total con IVA no puede ser menor que la suma de los netos de las líneas."
                            )
                        total_factura_val = float(tf)
                        iva_factura_val = round(total_factura_val - float(sum_neto_lines), 2)

                    documento = IngresoDocumento(
                        numero_documento=form_data["numero_documento"][:60] or None,
                        fecha_documento=fecha_documento,
                        proveedor_id=proveedor.id,
                        proveedor_rut=form_data["supplier_rut"],
                        proveedor_nombre=form_data["supplier_name"][:200],
                        proveedor_giro=form_data["supplier_giro"][:200],
                        proveedor_email=form_data["supplier_email"][:150],
                        proveedor_direccion=form_data["supplier_address"][:300],
                        proveedor_comuna=form_data["supplier_comuna"][:120],
                        proveedor_region=form_data["supplier_region"][:120],
                        proveedor_pais=form_data["supplier_country"][:120],
                        observacion=form_data["observacion"],
                        metodo_pago=form_data["metodo_pago"],
                        total_factura=total_factura_val,
                        iva_factura=iva_factura_val,
                        usuario=session.get("user") or "sistema",
                    )
                    db.session.add(documento)
                    db.session.flush()

                    for row in rows:
                        codigo = row["codigo"]
                        marca = row["marca"]
                        bodega = row["bodega"]
                        origen_compra = _normalize_origen_compra(row.get("origen_compra") or "")
                        cantidad = int(row["cantidad"])
                        nota = (row.get("nota") or "").strip()

                        producto = _producto_por_codigo(codigo)
                        if producto is None:
                            raise ValueError(f"Producto {codigo} no existe o esta inactivo.")

                        variante_ing: ProductoVarianteStock | None = None
                        if _requiere_variante(codigo, marca):
                            if not marca:
                                raise ValueError(f"El producto {codigo} requiere marca/variante.")
                            variante_ing = _obtener_o_crear_variante(
                                codigo,
                                marca,
                                bodega,
                                origen_compra=origen_compra,
                                proveedor=form_data["supplier_name"],
                            )
                            variante_ing.stock = int(variante_ing.stock or 0) + cantidad
                            _sincronizar_stock_base_desde_variantes(codigo)
                        else:
                            stock_anterior = int(producto["stock_actual"] or 0)
                            _actualizar_stock(codigo, stock_anterior + cantidad)

                        vn_item = row.get("valor_neto")
                        mg_item = row.get("margen_pct")
                        pv_item = row.get("precio_venta_neto")
                        db.session.add(
                            IngresoDocumentoItem(
                                ingreso_documento_id=documento.id,
                                codigo_producto=codigo,
                                descripcion_producto=(producto.get("descripcion") or "")[:255],
                                marca=marca,
                                bodega=bodega,
                                origen_compra=origen_compra,
                                cantidad=cantidad,
                                valor_neto=float(vn_item) if vn_item is not None else None,
                                margen_pct=float(mg_item) if mg_item is not None else None,
                                precio_venta_neto=float(pv_item) if pv_item is not None else None,
                                nota=nota[:255],
                            )
                        )
                        _propagar_precio_venta_ingreso_a_catalogo(
                            codigo, pv_item, variante_ing
                        )

                        base_obs = form_data["observacion"] or "Ingreso ERP por documento"
                        observacion = f"Doc {documento.id}: {base_obs}"[:255]
                        _registrar_movimiento(
                            codigo,
                            "ingreso",
                            cantidad,
                            observacion,
                            proveedor=form_data["supplier_name"],
                            marca=marca or None,
                            bodega=bodega,
                            origen_compra=origen_compra,
                            ingreso_documento_id=documento.id,
                        )

                        _upsert_mapa_proveedor_codigo(
                            form_data["supplier_rut"],
                            row.get("codigo_proveedor") or "",
                            codigo,
                        )

                    db.session.commit()
                    document_created = documento.id
                    n_items_guardados = len(rows)
                    msg_extra = " Proveedor creado en linea." if created_supplier_inline else ""
                    message = {
                        "type": "success",
                        "text": (
                            f"Ingreso guardado en documento #{documento.id} con {n_items_guardados} item(s)."
                            + msg_extra
                        ),
                    }
                    rows = default_rows
                    form_data = {
                        "supplier_rut": "",
                        "supplier_name": "",
                        "supplier_contact": "",
                        "supplier_giro": "",
                        "supplier_email": "",
                        "supplier_telefono": "",
                        "supplier_address": "",
                        "supplier_comuna": "",
                        "supplier_region": "",
                        "supplier_ciudad": "",
                        "supplier_country": DEFAULT_COUNTRY,
                        "fecha_documento": today_str,
                        "numero_documento": "",
                        "observacion": "",
                        "metodo_pago": "",
                        "total_factura": "",
                    }
                    supplier_found = False
                    created_supplier_inline = False
                except ValueError as exc:
                    db.session.rollback()
                    message = {"type": "error", "text": str(exc)}
                except Exception as exc:
                    db.session.rollback()
                    message = {"type": "error", "text": f"No se pudo guardar el ingreso: {exc}"}

    bodegas_opciones = _bodegas_para_select()
    for r in rows:
        b = (r.get("bodega") or "").strip()
        r["origen_compra"] = _normalize_origen_compra(r.get("origen_compra") or "")
        if b and b not in bodegas_opciones:
            bodegas_opciones = sorted(bodegas_opciones + [b], key=str.lower)

    ingreso_mostrar_resumen_proveedor = False
    ingreso_mostrar_tarjeta_proveedor = False
    rut_ui = (form_data.get("supplier_rut") or "").strip()
    if rut_ui and _is_valid_rut(rut_ui):
        prov_ui = _buscar_proveedor_por_rut(rut_ui)
        if prov_ui is not None:
            ingreso_mostrar_resumen_proveedor = True
        else:
            ingreso_mostrar_tarjeta_proveedor = True

    _geo_ingreso = _load_chile_geo_ingreso()

    return render_template(
        "bodega/ingreso.html",
        **_base_context(
            "ingreso",
            form_data=form_data,
            message=message,
            rows=rows,
            supplier_found=supplier_found,
            created_supplier_inline=created_supplier_inline,
            document_created=document_created,
            bodegas_opciones=bodegas_opciones,
            origen_compra_opciones=ORIGEN_COMPRA_OPCIONES,
            default_bodega=DEFAULT_BODEGA,
            metodos_pago_opciones=INGRESO_METODOS_PAGO_OPCIONES,
            ingreso_mostrar_resumen_proveedor=ingreso_mostrar_resumen_proveedor,
            ingreso_mostrar_tarjeta_proveedor=ingreso_mostrar_tarjeta_proveedor,
            chile_geo_ingreso=_geo_ingreso,
            chile_regions_ingreso=_chile_region_names(_geo_ingreso),
        ),
    )


@bodega_bp.route("/ingreso/codigo_interno_por_proveedor", methods=["GET"])
@admin_required
def ingreso_codigo_interno_por_proveedor():
    """Devuelve el último código interno guardado para (proveedor RUT, código proveedor)."""
    rut = _normalize_rut(request.args.get("rut") or "")
    cp = _normalize_codigo_proveedor(request.args.get("codigo_proveedor") or "")
    if not rut or not _is_valid_rut(rut):
        return jsonify({"ok": False, "error": "rut_invalido", "codigo_interno": None}), 400
    if not cp:
        return jsonify({"ok": True, "codigo_interno": None})
    row = ProveedorCodigoInterno.query.filter_by(proveedor_rut=rut, codigo_proveedor=cp).first()
    return jsonify({"ok": True, "codigo_interno": row.codigo_interno if row else None})


def _ajuste_marcas_desde_bd(limit: int = 500) -> list[str]:
    """Marcas distintas ya usadas en variantes (acotado por rendimiento)."""
    try:
        rows = db.session.execute(
            text(
                """
                SELECT DISTINCT TRIM(marca) AS m
                FROM productos_variantes_stock
                WHERE marca IS NOT NULL AND TRIM(marca) != ''
                ORDER BY m COLLATE NOCASE
                LIMIT :lim
                """
            ),
            {"lim": limit},
        ).fetchall()
    except Exception:
        db.session.rollback()
        return []
    return sorted({(r[0] or "").strip() for r in rows if (r[0] or "").strip()}, key=str.upper)


def _marcas_sugeridas_variante(codigo: str | None) -> list[str]:
    """
    Misma lista que Ajuste de stock: referencia CL + marcas usadas en variantes (global)
    + marcas ya registradas para el código (si aplica).
    """
    c = (codigo or "").strip().upper() or None
    registradas_codigo: list[str] = []
    if c:
        rows = (
            db.session.query(ProductoVarianteStock.marca)
            .filter_by(codigo_producto=c)
            .distinct()
            .order_by(ProductoVarianteStock.marca.asc())
            .all()
        )
        for r in rows:
            s = (r[0] or "").strip().upper()
            if s and s not in registradas_codigo:
                registradas_codigo.append(s)

    # Resto de sugerencias: referencia CL + marcas globales, excluyendo las ya registradas en este código.
    resto: set[str] = set(MARCAS_REF_AUTOMOTRIZ_CL)
    for m in _ajuste_marcas_desde_bd(500):
        mm = (m or "").strip().upper()
        if mm:
            resto.add(mm)
    for r in registradas_codigo:
        if r in resto:
            resto.remove(r)

    # Orden final: primero variantes ya registradas para el código, luego el resto.
    ordered = registradas_codigo + sorted(resto, key=str.upper)
    return ordered[:800]


@bodega_bp.route("/ingreso/marcas_por_codigo", methods=["GET"])
@admin_required
def ingreso_marcas_por_codigo():
    """Sugerencias de marca en ingreso: mismo criterio que Ajuste (homogéneo)."""
    codigo = (request.args.get("codigo") or "").strip().upper()
    if not codigo:
        return jsonify({"ok": False, "error": "codigo requerido", "marcas": []}), 400
    out = _marcas_sugeridas_variante(codigo)
    registradas_rows = (
        db.session.query(ProductoVarianteStock.marca)
        .filter_by(codigo_producto=codigo)
        .distinct()
        .order_by(ProductoVarianteStock.marca.asc())
        .all()
    )
    registradas = []
    for r in registradas_rows:
        mm = (r[0] or "").strip().upper()
        if mm and mm not in registradas:
            registradas.append(mm)
    existe = _producto_por_codigo(codigo) is not None
    return jsonify(
        {
            "ok": True,
            "codigo": codigo,
            "marcas": out,
            "marcas_registradas": registradas,
            "existe": existe,
        }
    )


@bodega_bp.route("/ajuste/marcas_sugeridas", methods=["GET"])
@admin_required
def ajuste_marcas_sugeridas():
    """
    Sugerencias para Marca/Variante en ajuste: referencia común + marcas ya usadas en el sistema
    + marcas del código indicado (si tiene variantes).
    """
    codigo = (request.args.get("codigo") or "").strip().upper()
    out = _marcas_sugeridas_variante(codigo)
    return jsonify({"ok": True, "codigo": codigo or None, "marcas": out})


@bodega_bp.route("/ingreso/proveedor", methods=["GET"])
@admin_required
def ingreso_proveedor_por_rut():
    rut = _normalize_rut(request.args.get("rut") or "")
    if not rut:
        return jsonify({"success": False, "message": "RUT vacio"}), 400
    if not _is_valid_rut(rut):
        return jsonify({"success": False, "message": "RUT invalido"}), 400

    proveedor = _buscar_proveedor_por_rut(rut)
    if proveedor is None:
        return jsonify({"success": True, "found": False, "rut": format_rut(rut)})

    return jsonify({"success": True, "found": True, "proveedor": _proveedor_json_ingreso(proveedor)})


@bodega_bp.route("/api/analizar-factura", methods=["POST"])
@admin_required
def api_analizar_factura():
    """Analiza imagen de factura chilena con Google Cloud Vision OCR y devuelve JSON estructurado."""
    if not has_permission(session.get("user"), session.get("rol"), "bodega_ingreso"):
        return jsonify(success=False, message="No tienes permiso para registrar ingresos."), 403

    payload = request.get_json(silent=True) or {}
    image_b64 = (payload.get("image_base64") or payload.get("image") or "").strip()
    media_type = (payload.get("media_type") or "image/jpeg").strip().lower()

    if not image_b64:
        return jsonify(success=False, message="Debe enviar la imagen en base64."), 400

    try:
        from app.utils.invoice_vision import analizar_factura

        data = analizar_factura(image_b64, media_type)
        current_app.logger.info("Resultado análisis: %s", data)
        print("=== TEXTO OCR ===", flush=True)
        print(data.get("ocr_texto_crudo", "NO HAY TEXTO"), flush=True)
        print("=== FIN ===", flush=True)
        print("=== RESULTADO COMPLETO (analizar-factura) ===", flush=True)
        print(repr(data), flush=True)
        print("=== productos ===", flush=True)
        print(data.get("productos"), flush=True)
        print("=== FIN RESULTADO ===", flush=True)
        return jsonify(success=True, data=data)
    except ValueError as exc:
        return jsonify(success=False, message=str(exc)), 400
    except Exception:
        current_app.logger.exception("api_analizar_factura")
        return jsonify(success=False, message="Error al analizar la factura."), 500


@bodega_bp.route("/ingreso/proveedor/guardar", methods=["POST"])
@admin_required
def ingreso_guardar_proveedor():
    """Crea o actualiza el proveedor desde la ficha de ingreso (sin guardar el movimiento aún)."""
    if not has_permission(session.get("user"), session.get("rol"), "bodega_ingreso"):
        return jsonify({"ok": False, "message": "No tienes permiso para registrar ingresos."}), 403
    data = request.get_json(silent=True) or {}
    rut = _normalize_rut(data.get("rut") or "")
    if not rut or not _is_valid_rut(rut):
        return jsonify({"ok": False, "message": "RUT inválido."}), 400
    empresa = (data.get("name") or "").strip()
    contact = (data.get("contact") or "").strip()
    address = (data.get("address") or "").strip()
    comuna = (data.get("comuna") or "").strip()
    region = (data.get("region") or "").strip()
    ciudad_in = (data.get("ciudad") or "").strip()
    if not all([empresa, address, comuna, region]):
        return jsonify({"ok": False, "message": "Completa empresa, calle y número, comuna y región."}), 400

    giro = (data.get("giro") or "").strip()[:200]
    email = (data.get("email") or "").strip()[:150]
    telefono_raw = (data.get("telefono") or "").strip()[:80]
    country = (data.get("country") or DEFAULT_COUNTRY).strip() or DEFAULT_COUNTRY
    ciudad = _ingreso_resolve_ciudad_chile(region, comuna, ciudad_in)
    nombre = contact[:200] if contact else empresa[:200]
    telefono = phone_to_compact_e164(telefono_raw, country)[:50]

    try:
        prov = _buscar_proveedor_por_rut(rut)
        if prov is None:
            prov = Proveedor(
                nombre=nombre,
                empresa=empresa[:200],
                rut=rut,
                giro=giro[:200],
                direccion=address[:300],
                comuna=comuna[:120],
                region=region[:120],
                ciudad=ciudad[:120],
                pais=country[:120],
                email=email[:150],
                telefono=telefono[:50],
                activo=True,
            )
            db.session.add(prov)
        else:
            prov.nombre = nombre
            prov.empresa = empresa[:200]
            prov.giro = giro[:200]
            prov.direccion = address[:300]
            prov.comuna = comuna[:120]
            prov.region = region[:120]
            prov.ciudad = ciudad[:120]
            prov.pais = country[:120]
            prov.email = email[:150]
            prov.telefono = telefono[:50]
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        return jsonify({"ok": False, "message": f"No se pudo guardar: {exc}"}), 500

    prov2 = _buscar_proveedor_por_rut(rut)
    return jsonify({"ok": True, "proveedor": _proveedor_json_ingreso(prov2) if prov2 else {}})


@bodega_bp.route("/ingreso/historial")
@admin_required
def ingreso_historial():
    q = (request.args.get("q") or "").strip()
    anulado_filter = (request.args.get("anulado") or "").strip().lower()
    page = _parse_int(request.args.get("page") or "1", allow_zero=False) or 1
    per_page = 30

    query = IngresoDocumento.query
    if q:
        like = f"%{q}%"
        q_norm = _normalize_rut(q)
        query = query.filter(
            or_(
                IngresoDocumento.numero_documento.ilike(like),
                IngresoDocumento.proveedor_nombre.ilike(like),
                IngresoDocumento.proveedor_rut.ilike(like),
                IngresoDocumento.observacion.ilike(like),
                (IngresoDocumento.id == int(q) if q.isdigit() else False),
                (func.upper(func.replace(func.replace(IngresoDocumento.proveedor_rut, ".", ""), "-", "")).ilike(f"%{q_norm}%") if q_norm else False),
            )
        )
    if anulado_filter == "si":
        query = query.filter(IngresoDocumento.anulado.is_(True))
    elif anulado_filter == "no":
        query = query.filter(or_(IngresoDocumento.anulado.is_(False), IngresoDocumento.anulado.is_(None)))

    total = query.count()
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(1, page), total_pages)
    offset = (page - 1) * per_page
    rows = (
        query.order_by(IngresoDocumento.created_at.desc(), IngresoDocumento.id.desc())
        .offset(offset)
        .limit(per_page)
        .all()
    )

    # Totales netos por documento para mostrar en grilla.
    totales_por_doc: dict[int, float] = {}
    codigos_proveedor_por_doc: dict[int, list[str]] = {}
    detalles_items_por_doc: dict[int, list[dict[str, object]]] = {}
    codigos_internos_por_doc: dict[int, list[str]] = {}
    numero_documento_por_doc: dict[int, str] = {
        int(r.id): ((r.numero_documento or "").strip() or f"ING-{r.id}") for r in rows
    }
    if rows:
        ids = [r.id for r in rows]
        sums = (
            db.session.query(
                IngresoDocumentoItem.ingreso_documento_id,
                func.sum(
                    func.coalesce(IngresoDocumentoItem.valor_neto, 0)
                    * func.coalesce(IngresoDocumentoItem.cantidad, 0)
                ),
            )
            .filter(IngresoDocumentoItem.ingreso_documento_id.in_(ids))
            .group_by(IngresoDocumentoItem.ingreso_documento_id)
            .all()
        )
        for doc_id, total_neto in sums:
            totales_por_doc[int(doc_id)] = float(total_neto or 0)

        # Referencias de códigos proveedor por documento (vía mapa proveedor<->código interno).
        rut_por_doc = {int(r.id): _normalize_rut(r.proveedor_rut or "") for r in rows}
        items = (
            IngresoDocumentoItem.query
            .filter(IngresoDocumentoItem.ingreso_documento_id.in_(ids))
            .order_by(IngresoDocumentoItem.ingreso_documento_id.asc(), IngresoDocumentoItem.id.asc())
            .all()
        )
        codigos_internos = sorted({((it.codigo_producto or "").strip().upper()) for it in items if (it.codigo_producto or "").strip()})
        ruts = sorted({rut for rut in rut_por_doc.values() if rut})
        cp_por_rut_ci: dict[tuple[str, str], str] = {}
        if ruts and codigos_internos:
            links = (
                ProveedorCodigoInterno.query
                .filter(ProveedorCodigoInterno.proveedor_rut.in_(ruts))
                .filter(ProveedorCodigoInterno.codigo_interno.in_(codigos_internos))
                .order_by(ProveedorCodigoInterno.updated_at.desc(), ProveedorCodigoInterno.id.desc())
                .all()
            )
            for lk in links:
                rut_n = _normalize_rut(lk.proveedor_rut or "")
                cod_int = (lk.codigo_interno or "").strip().upper()
                cod_prov = (lk.codigo_proveedor or "").strip()
                if rut_n and cod_int and cod_prov and (rut_n, cod_int) not in cp_por_rut_ci:
                    cp_por_rut_ci[(rut_n, cod_int)] = cod_prov

        tmp_codigos_doc: dict[int, set[str]] = {}
        seen_ci_por_doc: dict[int, set[str]] = {}
        for it in items:
            doc_id = int(it.ingreso_documento_id or 0)
            rut_n = rut_por_doc.get(doc_id, "")
            cod_int = (it.codigo_producto or "").strip().upper()
            if doc_id and cod_int:
                seen = seen_ci_por_doc.setdefault(doc_id, set())
                if cod_int not in seen:
                    seen.add(cod_int)
                    codigos_internos_por_doc.setdefault(doc_id, []).append(cod_int)
            if not doc_id or not rut_n or not cod_int:
                continue
            cod_prov = cp_por_rut_ci.get((rut_n, cod_int), "").strip()
            if cod_prov:
                tmp_codigos_doc.setdefault(doc_id, set()).add(cod_prov)
            detalles_items_por_doc.setdefault(doc_id, []).append(
                {
                    "codigo_producto": cod_int,
                    "codigo_proveedor": cod_prov,
                    "descripcion_producto": (it.descripcion_producto or "").strip(),
                    "marca": (it.marca or "").strip(),
                    "bodega": (it.bodega or "").strip(),
                    "origen_compra": (it.origen_compra or "").strip(),
                    "cantidad": int(it.cantidad or 0),
                    "valor_neto": float(it.valor_neto) if it.valor_neto is not None else None,
                    "nota": (it.nota or "").strip(),
                }
            )
        codigos_proveedor_por_doc = {doc_id: sorted(vals, key=str.upper) for doc_id, vals in tmp_codigos_doc.items() if vals}

    return render_template(
        "bodega/ingreso_historial.html",
        **_base_context(
            "ingreso_historial",
            rows=rows,
            q=q,
            anulado=anulado_filter,
            page=page,
            per_page=per_page,
            total=total,
            total_pages=total_pages,
            totales_por_doc=totales_por_doc,
            codigos_proveedor_por_doc=codigos_proveedor_por_doc,
            detalles_items_por_doc=detalles_items_por_doc,
            codigos_internos_por_doc=codigos_internos_por_doc,
            numero_documento_por_doc=numero_documento_por_doc,
        ),
    )


@bodega_bp.route("/ingreso/editar/<int:doc_id>", methods=["GET", "POST"])
@admin_required
def ingreso_editar(doc_id: int):
    if not has_permission(session.get("user"), session.get("rol"), "bodega_ingreso"):
        return _deny_bodega_perm("No tienes permiso para editar ingresos de stock.")

    doc = db.session.get(IngresoDocumento, doc_id)
    if doc is None:
        flash("Ingreso no encontrado.", "error")
        return redirect(url_for("bodega.ingreso_historial"))
    if bool(doc.anulado):
        flash("No se puede editar un ingreso anulado.", "error")
        return redirect(url_for("bodega.ingreso_historial"))

    items = (
        IngresoDocumentoItem.query
        .filter_by(ingreso_documento_id=doc.id)
        .order_by(IngresoDocumentoItem.id.asc())
        .all()
    )

    if request.method == "POST":
        fecha_raw = (request.form.get("fecha_documento") or "").strip()
        numero_raw = (request.form.get("numero_documento") or "").strip()
        observacion_raw = (request.form.get("observacion") or "").strip()
        total_factura_raw = (request.form.get("total_factura") or "").strip()

        if not fecha_raw:
            flash("La fecha del documento es obligatoria.", "error")
            return redirect(url_for("bodega.ingreso_editar", doc_id=doc.id))
        try:
            nueva_fecha = datetime.strptime(fecha_raw, "%Y-%m-%d").date()
        except ValueError:
            flash("La fecha del documento no es válida.", "error")
            return redirect(url_for("bodega.ingreso_editar", doc_id=doc.id))

        if len(numero_raw) > 60:
            flash("El número de documento no puede superar 60 caracteres.", "error")
            return redirect(url_for("bodega.ingreso_editar", doc_id=doc.id))
        if len(observacion_raw) > 255:
            flash("La observación no puede superar 255 caracteres.", "error")
            return redirect(url_for("bodega.ingreso_editar", doc_id=doc.id))
        total_factura_val: float | None = None
        if total_factura_raw:
            total_factura_val = _parse_valor_neto_chile(total_factura_raw)
            if total_factura_val is None or float(total_factura_val) <= 0:
                flash("El total factura (c/IVA) no es válido.", "error")
                return redirect(url_for("bodega.ingreso_editar", doc_id=doc.id))

        # En edición de ingreso siempre guardamos encabezado + ítems en una sola operación.
        action = "save_full"

        try:
            old_fecha = doc.fecha_documento
            doc.fecha_documento = nueva_fecha
            doc.numero_documento = numero_raw or None
            doc.observacion = observacion_raw
            doc.metodo_pago = _parse_metodo_pago_ingreso()
            doc.usuario = (session.get("user") or doc.usuario or "sistema")

            if action == "save_full":
                item_by_id = {int(it.id): it for it in items}
                line_ids = request.form.getlist("line_id[]")
                cp_values = request.form.getlist("line_codigo_proveedor[]")
                qty_values = request.form.getlist("line_cantidad[]")
                net_values = request.form.getlist("line_valor_neto[]")
                margen_values = request.form.getlist("line_margen_pct[]")
                precio_venta_values = request.form.getlist("line_precio_venta_neto[]")
                note_values = request.form.getlist("line_nota[]")
                desc_values = request.form.getlist("line_descripcion_producto[]")
                marca_values = request.form.getlist("line_marca[]")
                del_values = request.form.getlist("line_delete[]")
                del_set = {str(v) for v in del_values if str(v).strip()}

                n = len(line_ids)
                if n == 0:
                    raise ValueError("El ingreso no tiene líneas para editar.")

                rows_to_delete: list[IngresoDocumentoItem] = []
                rows_to_update: list[
                    tuple[IngresoDocumentoItem, int, float | None, float | None, float | None, str, str, str, str]
                ] = []
                stock_deltas: list[tuple[IngresoDocumentoItem, int]] = []
                stock_marca_transfers: list[tuple[IngresoDocumentoItem, int, int, str, str]] = []

                for i in range(n):
                    line_id_raw = (line_ids[i] if i < len(line_ids) else "").strip()
                    if not line_id_raw:
                        continue
                    try:
                        line_id = int(line_id_raw)
                    except ValueError:
                        raise ValueError("Se recibió una línea inválida.")
                    it = item_by_id.get(line_id)
                    if it is None:
                        raise ValueError("Hay líneas que no pertenecen a este ingreso.")

                    qty_raw = (qty_values[i] if i < len(qty_values) else "").strip()
                    vn_raw = (net_values[i] if i < len(net_values) else "").strip()
                    margen_raw = (margen_values[i] if i < len(margen_values) else "").strip()
                    pv_raw = (precio_venta_values[i] if i < len(precio_venta_values) else "").strip()
                    nt_raw = (note_values[i] if i < len(note_values) else "").strip()
                    cp_raw = (cp_values[i] if i < len(cp_values) else "").strip()
                    will_delete = line_id_raw in del_set

                    old_qty = int(it.cantidad or 0)
                    if will_delete:
                        rows_to_delete.append(it)
                        stock_deltas.append((it, -old_qty))
                        continue

                    new_qty = _parse_int(qty_raw, allow_zero=False)
                    if new_qty is None:
                        raise ValueError(f"La cantidad no es válida para la línea #{line_id}.")

                    vn_new = _parse_valor_neto_chile(vn_raw) if vn_raw else None
                    if vn_raw and vn_new is None:
                        raise ValueError(f"El valor neto no es válido para la línea #{line_id}.")
                    margen_new = _parse_margen_pct(margen_raw) if margen_raw else None
                    if margen_raw and margen_new is None:
                        raise ValueError(f"El margen % no es válido para la línea #{line_id}.")
                    if margen_new is not None and margen_new >= 100:
                        raise ValueError(f"El margen % debe ser menor a 100 (línea #{line_id}).")
                    precio_venta_new = _parse_valor_neto_chile(pv_raw) if pv_raw else None
                    if pv_raw and precio_venta_new is None:
                        raise ValueError(f"El precio venta neto no es válido para la línea #{line_id}.")
                    if precio_venta_new is not None and float(precio_venta_new) <= 0:
                        raise ValueError(f"El precio venta neto debe ser mayor a 0 (línea #{line_id}).")
                    if len(nt_raw) > 255:
                        raise ValueError(f"La nota no puede superar 255 caracteres (línea #{line_id}).")

                    desc_raw = (desc_values[i] if i < len(desc_values) else "").strip()
                    marca_raw = (marca_values[i] if i < len(marca_values) else "").strip()
                    if len(desc_raw) > 255:
                        raise ValueError(f"La descripción no puede superar 255 caracteres (línea #{line_id}).")
                    if len(marca_raw) > 120:
                        raise ValueError(f"La marca no puede superar 120 caracteres (línea #{line_id}).")

                    marca_guardada = marca_raw[:120]
                    om = _normalize_brand(it.marca or "")
                    nm = _normalize_brand(marca_guardada)

                    delta = int(new_qty) - old_qty
                    rows_to_update.append(
                        (
                            it,
                            int(new_qty),
                            vn_new,
                            margen_new,
                            precio_venta_new,
                            nt_raw[:255],
                            cp_raw[:120],
                            desc_raw[:255],
                            marca_guardada,
                        )
                    )
                    if om == nm:
                        if delta != 0:
                            stock_deltas.append((it, delta))
                    else:
                        stock_marca_transfers.append((it, old_qty, int(new_qty), om, nm))

                remaining_count = len(rows_to_update)
                if remaining_count <= 0:
                    raise ValueError("Debe quedar al menos un ítem en el ingreso.")

                for it, delta in stock_deltas:
                    if delta >= 0:
                        continue
                    codigo = (it.codigo_producto or "").strip().upper()
                    marca = _normalize_brand(it.marca or "")
                    bodega = _normalize_bodega(it.bodega or "")
                    origen_compra = _normalize_origen_compra(getattr(it, "origen_compra", None))
                    needed = abs(int(delta))
                    if _requiere_variante(codigo, marca):
                        variante = (
                            ProductoVarianteStock.query
                            .filter_by(
                                codigo_producto=codigo,
                                marca=marca,
                                bodega=bodega,
                                origen_compra=origen_compra,
                            )
                            .first()
                        )
                        actual = int(variante.stock or 0) if variante else 0
                        if actual < needed:
                            raise ValueError(
                                f"No hay stock suficiente para reducir {codigo} {marca or '(sin marca)'} {bodega}: "
                                f"actual {actual}, requiere {needed}."
                            )
                    else:
                        actual = _stock_actual_catalogo(codigo)
                        if actual < needed:
                            raise ValueError(
                                f"No hay stock suficiente para reducir {codigo}: actual {actual}, requiere {needed}."
                            )

                for it, old_q, _new_q, om, _nm in stock_marca_transfers:
                    if old_q <= 0:
                        continue
                    codigo = (it.codigo_producto or "").strip().upper()
                    marca = om
                    bodega = _normalize_bodega(it.bodega or "")
                    origen_compra = _normalize_origen_compra(getattr(it, "origen_compra", None))
                    needed = int(old_q)
                    if _requiere_variante(codigo, marca):
                        variante = (
                            ProductoVarianteStock.query.filter_by(
                                codigo_producto=codigo,
                                marca=marca,
                                bodega=bodega,
                                origen_compra=origen_compra,
                            ).first()
                        )
                        actual = int(variante.stock or 0) if variante else 0
                        if actual < needed:
                            raise ValueError(
                                f"No hay stock suficiente para reasignar marca en {codigo} "
                                f"{marca or '(sin marca)'} {bodega}: actual {actual}, requiere {needed}."
                            )
                    else:
                        actual = _stock_actual_catalogo(codigo)
                        if actual < needed:
                            raise ValueError(
                                f"No hay stock suficiente para reasignar marca en {codigo}: "
                                f"actual {actual}, requiere {needed}."
                            )

                for it, delta in stock_deltas:
                    if delta == 0:
                        continue
                    codigo = (it.codigo_producto or "").strip().upper()
                    marca = _normalize_brand(it.marca or "")
                    bodega = _normalize_bodega(it.bodega or "")
                    origen_compra = _normalize_origen_compra(getattr(it, "origen_compra", None))
                    if _requiere_variante(codigo, marca):
                        variante = _obtener_o_crear_variante(
                            codigo,
                            marca,
                            bodega,
                            origen_compra=origen_compra,
                            proveedor=doc.proveedor_nombre,
                        )
                        candidato = int(variante.stock or 0) + int(delta)
                        if candidato < 0:
                            raise ValueError(
                                f"No puedes dejar stock negativo en {codigo} {marca or '(sin marca)'}."
                            )
                        variante.stock = candidato
                        _sincronizar_stock_base_desde_variantes(codigo)
                    else:
                        stock_actual = _stock_actual_catalogo(codigo)
                        candidato = stock_actual + int(delta)
                        if candidato < 0:
                            raise ValueError(f"No puedes dejar stock negativo en {codigo}.")
                        _actualizar_stock(codigo, candidato)

                    base_obs = observacion_raw or "Edición de ingreso ERP"
                    obs_move = f"Doc {doc.id} editado: {base_obs}"[:255]
                    _registrar_movimiento(
                        codigo,
                        "ingreso" if delta > 0 else "salida",
                        abs(int(delta)),
                        obs_move,
                        proveedor=doc.proveedor_nombre,
                        marca=marca or None,
                        bodega=bodega,
                        origen_compra=origen_compra,
                        ingreso_documento_id=doc.id,
                    )

                for it, old_q, new_q, om, nm in stock_marca_transfers:
                    _ingreso_edit_transfer_stock_entre_marcas(
                        it, doc, old_q, new_q, om, nm, observacion_raw
                    )

                for it, qty_new, vn_new, margen_new, precio_venta_new, nt_new, cp_new, desc_new, marca_new in rows_to_update:
                    it.cantidad = qty_new
                    it.valor_neto = float(vn_new) if vn_new is not None else None
                    it.margen_pct = float(margen_new) if margen_new is not None else None
                    it.precio_venta_neto = float(precio_venta_new) if precio_venta_new is not None else None
                    it.nota = nt_new
                    it.descripcion_producto = desc_new
                    it.marca = marca_new
                    _upsert_mapa_proveedor_codigo(
                        doc.proveedor_rut or "",
                        cp_new,
                        (it.codigo_producto or "").strip().upper(),
                    )

                for it in rows_to_delete:
                    db.session.delete(it)

            db.session.flush()
            total_neto_actual = (
                db.session.query(
                    func.sum(
                        func.coalesce(IngresoDocumentoItem.valor_neto, 0)
                        * func.coalesce(IngresoDocumentoItem.cantidad, 0)
                    )
                )
                .filter(IngresoDocumentoItem.ingreso_documento_id == doc.id)
                .scalar()
                or 0
            )
            if total_factura_val is not None:
                doc.total_factura = float(total_factura_val)
                doc.iva_factura = float(total_factura_val) - float(total_neto_actual or 0)
            else:
                doc.total_factura = None
                doc.iva_factura = None

            if old_fecha != nueva_fecha:
                # Si cambian la fecha del documento, reflejarla también en los movimientos
                # ligados al ingreso, usando la hora actual de Chile.
                now_cl = datetime.now(CHILE_TZ).replace(second=0, microsecond=0)
                target_cl = datetime.combine(nueva_fecha, now_cl.timetz().replace(tzinfo=None))
                target_utc_naive = target_cl.replace(tzinfo=CHILE_TZ).astimezone(timezone.utc).replace(tzinfo=None)
                (
                    MovimientoStock.query
                    .filter(MovimientoStock.ingreso_documento_id == doc.id)
                    .update({MovimientoStock.fecha: target_utc_naive}, synchronize_session=False)
                )
            db.session.commit()
            flash(f"Ingreso #{doc.id} (encabezado + ítems) actualizado correctamente.", "success")
            return redirect(url_for("bodega.ingreso_historial"))
        except Exception as exc:
            db.session.rollback()
            flash(f"No se pudo actualizar el ingreso: {exc}", "error")
            return redirect(url_for("bodega.ingreso_editar", doc_id=doc.id))

    total_neto = (
        db.session.query(
            func.sum(
                func.coalesce(IngresoDocumentoItem.valor_neto, 0)
                * func.coalesce(IngresoDocumentoItem.cantidad, 0)
            )
        )
        .filter(IngresoDocumentoItem.ingreso_documento_id == doc.id)
        .scalar()
        or 0
    )
    total_con_iva = (
        float(doc.total_factura)
        if getattr(doc, "total_factura", None) is not None
        else float(total_neto or 0) * 1.19
    )
    iva_referencia = (
        float(doc.iva_factura)
        if getattr(doc, "iva_factura", None) is not None
        else float(total_con_iva or 0) - float(total_neto or 0)
    )
    proveedor_codes: dict[int, str] = {}
    rut_doc = _normalize_rut(doc.proveedor_rut or "")
    if rut_doc and items:
        codigos_internos = sorted({(it.codigo_producto or "").strip().upper() for it in items if (it.codigo_producto or "").strip()})
        if codigos_internos:
            links = (
                ProveedorCodigoInterno.query
                .filter_by(proveedor_rut=rut_doc)
                .filter(ProveedorCodigoInterno.codigo_interno.in_(codigos_internos))
                .order_by(ProveedorCodigoInterno.updated_at.desc(), ProveedorCodigoInterno.id.desc())
                .all()
            )
            cp_by_codigo: dict[str, str] = {}
            for lk in links:
                ci = (lk.codigo_interno or "").strip().upper()
                if ci and ci not in cp_by_codigo:
                    cp_by_codigo[ci] = (lk.codigo_proveedor or "").strip()
            for it in items:
                proveedor_codes[int(it.id)] = cp_by_codigo.get((it.codigo_producto or "").strip().upper(), "")
    return render_template(
        "bodega/ingreso_editar.html",
        **_base_context(
            "ingreso_historial",
            doc=doc,
            items=items,
            total_neto=float(total_neto or 0),
            iva_referencia=float(iva_referencia or 0),
            total_con_iva=float(total_con_iva or 0),
            total_factura_referencia=float(doc.total_factura) if getattr(doc, "total_factura", None) is not None else None,
            proveedor_codes=proveedor_codes,
            metodos_pago_opciones=INGRESO_METODOS_PAGO_OPCIONES,
        ),
    )


@bodega_bp.route("/ingreso/anular/<int:doc_id>", methods=["POST"])
@admin_required
def ingreso_anular(doc_id: int):
    if not has_permission(session.get("user"), session.get("rol"), "bodega_ingreso"):
        return _deny_bodega_perm("No tienes permiso para anular ingresos de stock.")

    doc = db.session.get(IngresoDocumento, doc_id)
    if doc is None:
        flash("Ingreso no encontrado.", "error")
        return redirect(url_for("bodega.ingreso_historial"))
    if bool(doc.anulado):
        flash(f"El ingreso #{doc.id} ya estaba anulado.", "warning")
        return redirect(url_for("bodega.ingreso_historial"))

    motivo = (request.form.get("motivo") or "").strip()[:255]
    auth_user = (request.form.get("auth_user") or "").strip()
    auth_pass = request.form.get("auth_password") or ""
    auth_ok, auth_err, auth_actor = _validar_autorizacion_anulacion_ingreso(auth_user, auth_pass)
    if not auth_ok:
        flash(auth_err, "error")
        return redirect(url_for("bodega.ingreso_historial"))
    items = (
        IngresoDocumentoItem.query
        .filter_by(ingreso_documento_id=doc.id)
        .order_by(IngresoDocumentoItem.id.asc())
        .all()
    )
    if not items:
        flash(f"El ingreso #{doc.id} no tiene ítems para anular.", "error")
        return redirect(url_for("bodega.ingreso_historial"))

    # 1) Validación previa: debe existir stock suficiente para revertir cada línea.
    errores: list[str] = []
    for it in items:
        codigo = (it.codigo_producto or "").strip().upper()
        marca = _normalize_brand(it.marca or "")
        bodega = _normalize_bodega(it.bodega or "")
        origen_compra = _normalize_origen_compra(getattr(it, "origen_compra", None))
        cantidad = int(it.cantidad or 0)
        if not codigo or cantidad <= 0:
            continue
        if _requiere_variante(codigo, marca):
            variante = (
                ProductoVarianteStock.query
                .filter_by(codigo_producto=codigo, marca=marca, bodega=bodega, origen_compra=origen_compra)
                .first()
            )
            actual = int(variante.stock or 0) if variante else 0
            if actual < cantidad:
                errores.append(f"{codigo} {marca or '(sin marca)'} {bodega}: stock {actual}, requiere {cantidad}.")
        else:
            actual = _stock_actual_catalogo(codigo)
            if actual < cantidad:
                errores.append(f"{codigo}: stock {actual}, requiere {cantidad}.")

    if errores:
        flash(
            "No se puede anular porque algunos ítems ya no tienen stock suficiente para revertir: "
            + " | ".join(errores[:5]),
            "error",
        )
        return redirect(url_for("bodega.ingreso_historial"))

    # 2) Reversa efectiva (salida) + marcado de documento anulado.
    try:
        for it in items:
            codigo = (it.codigo_producto or "").strip().upper()
            marca = _normalize_brand(it.marca or "")
            bodega = _normalize_bodega(it.bodega or "")
            origen_compra = _normalize_origen_compra(getattr(it, "origen_compra", None))
            cantidad = int(it.cantidad or 0)
            if not codigo or cantidad <= 0:
                continue
            if _requiere_variante(codigo, marca):
                variante = (
                    ProductoVarianteStock.query
                    .filter_by(codigo_producto=codigo, marca=marca, bodega=bodega, origen_compra=origen_compra)
                    .first()
                )
                if variante is None:
                    raise ValueError(f"No existe la variante {codigo} / {marca} en {bodega} ({origen_compra}).")
                variante.stock = int(variante.stock or 0) - cantidad
                _sincronizar_stock_base_desde_variantes(codigo)
            else:
                actual = _stock_actual_catalogo(codigo)
                _actualizar_stock(codigo, actual - cantidad)

            obs = f"Anulación ingreso #{doc.id} ({doc.numero_documento or 'sin N°'})"
            _registrar_movimiento(
                codigo,
                "salida",
                cantidad,
                obs,
                proveedor=doc.proveedor_nombre,
                marca=marca or None,
                bodega=bodega,
                origen_compra=origen_compra,
                ingreso_documento_id=doc.id,
            )

        doc.anulado = True
        doc.anulado_at = datetime.utcnow()
        doc.anulado_por = (auth_actor.usuario if auth_actor else (session.get("user") or "sistema"))[:100]
        doc.anulacion_motivo = motivo
        db.session.commit()
        flash(f"Ingreso #{doc.id} anulado correctamente. Stock revertido.", "success")
    except Exception as exc:
        db.session.rollback()
        flash(f"No se pudo anular el ingreso: {exc}", "error")

    return redirect(url_for("bodega.ingreso_historial"))


@bodega_bp.route("/salida", methods=["GET", "POST"])
@admin_required
def salida():
    if request.method == "POST" and not has_permission(session.get("user"), session.get("rol"), "bodega_salida"):
        return _deny_bodega_perm("No tienes permiso para registrar salidas de stock.")
    if request.method == "POST":
        form_data = {
            "codigo": (request.form.get("codigo") or "").strip().upper(),
            "marca": _normalize_brand(request.form.get("marca") or ""),
            "bodega": _normalize_bodega(request.form.get("bodega") or ""),
            "cantidad": (request.form.get("cantidad") or "").strip(),
            "observacion": (request.form.get("observacion") or "").strip(),
        }
    else:
        form_data = {
            "codigo": (request.args.get("codigo") or "").strip().upper(),
            "marca": _normalize_brand(request.args.get("marca") or ""),
            "bodega": _normalize_bodega(request.args.get("bodega") or ""),
            "cantidad": (request.args.get("cantidad") or "").strip(),
            "observacion": (request.args.get("observacion") or "").strip(),
        }
    message = None
    producto = None
    variantes_disponibles = []

    bodegas_opciones = _bodegas_para_select()
    sel_bodega = (form_data.get("bodega") or "").strip()
    if sel_bodega and sel_bodega not in bodegas_opciones:
        bodegas_opciones = sorted(bodegas_opciones + [sel_bodega], key=str.lower)

    if request.method == "GET" and form_data["codigo"]:
        producto = _producto_por_codigo(form_data["codigo"])
        variantes_disponibles = _stock_variantes_por_codigo(form_data["codigo"])
        if producto is None:
            message = {"type": "error", "text": "El producto no existe o esta inactivo."}

    if request.method == "POST":
        cantidad = _parse_int(form_data["cantidad"])
        if not form_data["codigo"]:
            message = {"type": "error", "text": "Debes ingresar un codigo de producto."}
        elif cantidad is None:
            message = {"type": "error", "text": "La cantidad debe ser un entero mayor a 0."}
        else:
            producto = _producto_por_codigo(form_data["codigo"])
            variantes_disponibles = _stock_variantes_por_codigo(form_data["codigo"])
            if producto is None:
                message = {"type": "error", "text": "El producto no existe o esta inactivo."}
            else:
                observacion = form_data["observacion"] or f"Salida manual de bodega (-{cantidad})"
                try:
                    if _requiere_variante(form_data["codigo"], form_data["marca"]):
                        if not form_data["marca"]:
                            message = {
                                "type": "error",
                                "text": "Este codigo trabaja por variantes. Debes indicar una marca.",
                            }
                        else:
                            disp_var = _stock_contexto_para_ajuste(
                                form_data["codigo"],
                                form_data["marca"],
                                form_data["bodega"],
                                producto,
                                variantes_disponibles,
                            )
                            if cantidad > disp_var:
                                message = {
                                    "type": "error",
                                    "text": f"No hay stock suficiente en esa variante. Disponible: {disp_var}.",
                                }
                            else:
                                nuevo_stock_variante = _aplicar_movimiento_variante(
                                    form_data["codigo"],
                                    "salida",
                                    -cantidad,
                                    observacion,
                                    marca=form_data["marca"],
                                    bodega=form_data["bodega"],
                                )
                                producto = _producto_por_codigo(form_data["codigo"])
                                variantes_disponibles = _stock_variantes_por_codigo(form_data["codigo"])
                                message = {
                                    "type": "success",
                                    "text": f"Salida aplicada a variante {form_data['marca']} ({form_data['bodega']}). Stock variante: {nuevo_stock_variante}.",
                                }
                    else:
                        stock_anterior = int(producto["stock_actual"] or 0)
                        if cantidad > stock_anterior:
                            message = {
                                "type": "error",
                                "text": f"No puedes dejar stock negativo. Disponible actual: {stock_anterior}.",
                            }
                        else:
                            nuevo_stock = stock_anterior - cantidad
                            _aplicar_movimiento_stock(
                                form_data["codigo"],
                                "salida",
                                -cantidad,
                                nuevo_stock,
                                observacion,
                            )
                            producto = _producto_por_codigo(form_data["codigo"])
                            message = {
                                "type": "success",
                                "text": f"Salida aplicada correctamente. Stock actual: {nuevo_stock}.",
                            }
                except Exception as exc:
                    db.session.rollback()
                    message = {"type": "error", "text": f"No se pudo registrar la salida: {exc}"}

    salida_variantes_por_bodega = None
    salida_stock_max = None
    if producto:
        if variantes_disponibles:
            salida_variantes_por_bodega = _variantes_agrupadas_por_bodega(variantes_disponibles)
            salida_stock_max = _stock_contexto_para_ajuste(
                form_data["codigo"],
                form_data["marca"],
                form_data["bodega"],
                producto,
                variantes_disponibles,
            )
        else:
            salida_stock_max = int(producto["stock_actual"] or 0)

    if (
        request.method == "GET"
        and producto
        and form_data["marca"]
        and salida_stock_max is not None
        and salida_stock_max >= 1
        and not (form_data.get("cantidad") or "").strip()
    ):
        form_data["cantidad"] = str(salida_stock_max)

    return render_template(
        "bodega/salida.html",
        **_base_context(
            "salida",
            form_data=form_data,
            message=message,
            producto=producto,
            variantes=variantes_disponibles,
            bodegas_opciones=bodegas_opciones,
            salida_variantes_por_bodega=salida_variantes_por_bodega,
            salida_stock_max=salida_stock_max,
        ),
    )


@bodega_bp.route("/ajuste", methods=["GET", "POST"])
@admin_required
def ajuste():
    if request.method == "POST" and not has_permission(session.get("user"), session.get("rol"), "bodega_ajuste"):
        return _deny_bodega_perm("No tienes permiso para registrar ajustes de stock.")
    if request.method == "POST":
        obs_motivo = (request.form.get("observacion_motivo") or "").strip()
        obs_detalle = (request.form.get("observacion_detalle") or "").strip()
        obs_compuesta = f"{obs_motivo}. {obs_detalle}" if obs_motivo and obs_detalle else (obs_motivo or obs_detalle)
        form_data = {
            "codigo": (request.form.get("codigo") or "").strip().upper(),
            "marca": _normalize_brand(request.form.get("marca") or ""),
            "bodega": _normalize_bodega(request.form.get("bodega") or ""),
            "nuevo_stock": (request.form.get("nuevo_stock") or "").strip(),
            "observacion_motivo": obs_motivo,
            "observacion_detalle": obs_detalle[:255],
            "observacion": obs_compuesta[:255],
            "ajuste_lineas": _form_data_lineas_ajuste(request.form),
        }
    else:
        obs_motivo = (request.args.get("observacion_motivo") or "").strip()
        obs_detalle = (request.args.get("observacion_detalle") or "").strip()
        obs_legacy = (request.args.get("observacion") or "").strip()
        obs_compuesta = (
            f"{obs_motivo}. {obs_detalle}" if obs_motivo and obs_detalle else (obs_motivo or obs_detalle or obs_legacy)
        )
        form_data = {
            "codigo": (request.args.get("codigo") or "").strip().upper(),
            "marca": _normalize_brand(request.args.get("marca") or ""),
            "bodega": _normalize_bodega(request.args.get("bodega") or ""),
            "nuevo_stock": (request.args.get("nuevo_stock") or "").strip(),
            "observacion_motivo": obs_motivo,
            "observacion_detalle": obs_detalle[:255],
            "observacion": obs_compuesta[:255],
            "ajuste_lineas": [],
        }

    message = None
    bodegas_opciones = _bodegas_para_select()
    sel_bodega = (form_data.get("bodega") or "").strip()
    if sel_bodega and sel_bodega not in bodegas_opciones:
        bodegas_opciones = sorted(bodegas_opciones + [sel_bodega], key=str.lower)

    if request.method != "POST":
        producto, variantes_disponibles, stock_contexto_actual = _cargar_vista_ajuste(form_data)
        if (
            producto
            and (form_data.get("bodega") or "").strip()
            and variantes_disponibles
        ):
            vb = [
                v
                for v in variantes_disponibles
                if (v.get("bodega") or "").strip() == (form_data.get("bodega") or "").strip()
            ]
            if vb:
                form_data["ajuste_lineas"] = [
                    {"marca": v["marca"], "nuevo_stock": str(v["stock"])}
                    for v in vb
                ]
            else:
                form_data["ajuste_lineas"] = [{"marca": "", "nuevo_stock": ""}]
    else:
        producto, variantes_disponibles, stock_contexto_actual = None, [], None

    if request.method == "GET" and producto and not form_data["nuevo_stock"]:
        if stock_contexto_actual is not None and not form_data.get("ajuste_lineas"):
            form_data["nuevo_stock"] = str(stock_contexto_actual)

    if request.method == "POST":
        multipost = request.form.get("ajuste_multivariante") == "1"
        nuevo_stock = _parse_int(form_data["nuevo_stock"], allow_zero=True)
        if not form_data["codigo"]:
            message = {"type": "error", "text": "Debes ingresar un codigo de producto."}
        elif not (form_data.get("observacion_motivo") or "").strip():
            message = {"type": "error", "text": "Debes seleccionar el motivo del ajuste."}
        elif multipost:
            if not (form_data.get("bodega") or "").strip():
                message = {"type": "error", "text": "Debes ingresar la bodega."}
            else:
                try:
                    lineas = _lineas_ajuste_desde_form(request.form)
                except ValueError as ve:
                    message = {"type": "error", "text": str(ve)}
                    lineas = []
                else:
                    lineas = lineas or []
                if message is None and not lineas:
                    message = {
                        "type": "error",
                        "text": "Indicá al menos una fila con marca y nuevo stock.",
                    }
                elif message is None:
                    producto = _producto_por_codigo(form_data["codigo"])
                    variantes_disponibles = _stock_variantes_por_codigo(form_data["codigo"])
                    if producto is None:
                        message = {"type": "error", "text": "El producto no existe o esta inactivo."}
                    else:
                        base_observacion = form_data["observacion"] or "Ajuste manual de inventario"
                        cambios: list[tuple[str, int, int, int]] = []
                        try:
                            for marca, nuevo_s in lineas:
                                variante = _obtener_o_crear_variante(
                                    form_data["codigo"],
                                    marca,
                                    form_data["bodega"],
                                )
                                stock_anterior = int(variante.stock or 0)
                                delta = int(nuevo_s) - stock_anterior
                                if delta != 0:
                                    cambios.append((marca, nuevo_s, stock_anterior, delta))
                            if not cambios:
                                message = {
                                    "type": "error",
                                    "text": "No hay cambios para aplicar en ninguna variante.",
                                }
                            else:
                                for marca, nuevo_s, stock_anterior, delta in cambios:
                                    observacion = (
                                        f"{base_observacion}. Variante {marca} / {form_data['bodega']} "
                                        f"{stock_anterior} -> {nuevo_s}"
                                    )
                                    _aplicar_movimiento_variante(
                                        form_data["codigo"],
                                        "ajuste",
                                        delta,
                                        observacion,
                                        marca=marca,
                                        bodega=form_data["bodega"],
                                        nuevo_stock_variante=int(nuevo_s),
                                        commit=False,
                                    )
                                db.session.commit()
                                producto = _producto_por_codigo(form_data["codigo"])
                                variantes_disponibles = _stock_variantes_por_codigo(form_data["codigo"])
                                resumen = ", ".join(f"{m}→{ns}" for m, ns, _, _ in cambios)
                                message = {
                                    "type": "success",
                                    "text": f"Ajuste aplicado a {len(cambios)} variante(s) en {form_data['bodega']}: {resumen}.",
                                }
                        except Exception as exc:
                            db.session.rollback()
                            message = {"type": "error", "text": f"No se pudo registrar el ajuste: {exc}"}
        elif nuevo_stock is None:
            message = {"type": "error", "text": "El nuevo stock debe ser un entero igual o mayor a 0."}
        else:
            producto = _producto_por_codigo(form_data["codigo"])
            variantes_disponibles = _stock_variantes_por_codigo(form_data["codigo"])
            if producto is None:
                message = {"type": "error", "text": "El producto no existe o esta inactivo."}
            else:
                base_observacion = form_data["observacion"] or "Ajuste manual de inventario"
                try:
                    if _requiere_variante(form_data["codigo"], form_data["marca"]):
                        if not form_data["marca"]:
                            message = {
                                "type": "error",
                                "text": "Este codigo trabaja por variantes. Debes indicar una marca.",
                            }
                        else:
                            variante_actual = _obtener_o_crear_variante(
                                form_data["codigo"],
                                form_data["marca"],
                                form_data["bodega"],
                            )
                            stock_anterior = int(variante_actual.stock or 0)
                            delta = int(nuevo_stock) - stock_anterior
                            if delta == 0:
                                message = {"type": "error", "text": "No hay cambios para aplicar en la variante seleccionada."}
                            else:
                                observacion = (
                                    f"{base_observacion}. Variante {form_data['marca']} / {form_data['bodega']} "
                                    f"{stock_anterior} -> {nuevo_stock}"
                                )
                                _aplicar_movimiento_variante(
                                    form_data["codigo"],
                                    "ajuste",
                                    delta,
                                    observacion,
                                    marca=form_data["marca"],
                                    bodega=form_data["bodega"],
                                    nuevo_stock_variante=int(nuevo_stock),
                                )
                                producto = _producto_por_codigo(form_data["codigo"])
                                variantes_disponibles = _stock_variantes_por_codigo(form_data["codigo"])
                                message = {
                                    "type": "success",
                                    "text": f"Ajuste aplicado a variante {form_data['marca']} ({form_data['bodega']}). Stock variante: {nuevo_stock}.",
                                }
                    else:
                        stock_anterior = int(producto["stock_actual"] or 0)
                        delta = nuevo_stock - stock_anterior
                        if delta == 0:
                            message = {"type": "error", "text": "No hay cambios para aplicar en el stock."}
                        else:
                            observacion = f"{base_observacion}. Stock {stock_anterior} -> {nuevo_stock}"
                            _aplicar_movimiento_stock(
                                form_data["codigo"],
                                "ajuste",
                                delta,
                                nuevo_stock,
                                observacion,
                            )
                            producto = _producto_por_codigo(form_data["codigo"])
                            message = {
                                "type": "success",
                                "text": f"Ajuste aplicado correctamente. Stock actual: {nuevo_stock}.",
                            }
                except Exception as exc:
                    db.session.rollback()
                    message = {"type": "error", "text": f"No se pudo registrar el ajuste: {exc}"}

        if message and message.get("type") == "success":
            form_data = {
                "codigo": "",
                "marca": "",
                "bodega": DEFAULT_BODEGA,
                "nuevo_stock": "",
                "observacion_motivo": "",
                "observacion_detalle": "",
                "observacion": "",
                "ajuste_lineas": [],
            }
            producto = None
            variantes_disponibles = []
            stock_contexto_actual = None
        else:
            producto, variantes_disponibles, stock_contexto_actual = _cargar_vista_ajuste(form_data)

    stock_suma_variantes_bodega = None
    stock_suma_variantes_total = None
    if variantes_disponibles:
        stock_suma_variantes_total = _suma_stock_variantes_total(variantes_disponibles)
        stock_suma_variantes_bodega = _suma_stock_variantes_en_bodega(
            variantes_disponibles, form_data.get("bodega") or ""
        )

    variantes_bodega = [
        v
        for v in (variantes_disponibles or [])
        if (v.get("bodega") or "").strip() == (form_data.get("bodega") or "").strip()
    ]
    mostrar_ajuste_multi = bool(
        producto
        and (form_data.get("bodega") or "").strip()
        and variantes_disponibles
    )
    if mostrar_ajuste_multi and not (form_data.get("ajuste_lineas") or []):
        form_data["ajuste_lineas"] = [{"marca": "", "nuevo_stock": ""}]
    ajuste_lineas_ui = _ajuste_lineas_con_stock_actual(
        form_data.get("ajuste_lineas") or [], variantes_bodega
    )

    return render_template(
        "bodega/ajuste.html",
        **_base_context(
            "ajuste",
            form_data=form_data,
            message=message,
            producto=producto,
            variantes=variantes_disponibles,
            variantes_bodega=variantes_bodega,
            mostrar_ajuste_multi=mostrar_ajuste_multi,
            ajuste_lineas_ui=ajuste_lineas_ui,
            bodegas_opciones=bodegas_opciones,
            stock_contexto_actual=stock_contexto_actual,
            stock_suma_variantes_bodega=stock_suma_variantes_bodega,
            stock_suma_variantes_total=stock_suma_variantes_total,
            ajuste_observacion_opciones=AJUSTE_OBSERVACION_OPCIONES,
        ),
    )


@bodega_bp.route("/recepcion", methods=["GET", "POST"])
@admin_required
def recepcion():
    if request.method == "POST":
        form_data = {
            "proveedor": (request.form.get("proveedor") or "").strip(),
            "codigo": (request.form.get("codigo_producto") or "").strip().upper(),
            "marca": _normalize_brand(request.form.get("marca") or ""),
            "bodega": _normalize_bodega(request.form.get("bodega") or ""),
            "cantidad": (request.form.get("cantidad") or "").strip(),
            "observacion": (request.form.get("observacion") or "").strip(),
        }
    else:
        form_data = {
            "proveedor": (request.args.get("proveedor") or "").strip(),
            "codigo": (
                request.args.get("codigo_producto") or request.args.get("codigo") or ""
            ).strip().upper(),
            "marca": _normalize_brand(request.args.get("marca") or ""),
            "bodega": _normalize_bodega(request.args.get("bodega") or ""),
            "cantidad": (request.args.get("cantidad") or "").strip(),
            "observacion": (request.args.get("observacion") or "").strip(),
        }
    message = None
    producto = None
    variantes_disponibles = []

    bodegas_opciones = _bodegas_para_select()
    sel_bodega = (form_data.get("bodega") or "").strip()
    if sel_bodega and sel_bodega not in bodegas_opciones:
        bodegas_opciones = sorted(bodegas_opciones + [sel_bodega], key=str.lower)

    if request.method == "GET" and form_data["codigo"]:
        producto = _producto_por_codigo(form_data["codigo"])
        variantes_disponibles = _stock_variantes_por_codigo(form_data["codigo"])
        if producto is None:
            message = {"type": "error", "text": "El producto no existe o esta inactivo."}

    if request.method == "POST":
        cantidad = _parse_int(form_data["cantidad"])
        if not form_data["proveedor"]:
            message = {"type": "error", "text": "Debes indicar el proveedor."}
        elif not form_data["codigo"]:
            message = {"type": "error", "text": "Debes ingresar un codigo de producto."}
        elif cantidad is None:
            message = {"type": "error", "text": "La cantidad debe ser un entero mayor a 0."}
        else:
            producto = _producto_por_codigo(form_data["codigo"])
            variantes_disponibles = _stock_variantes_por_codigo(form_data["codigo"])
            if producto is None:
                message = {"type": "error", "text": "El producto no existe o esta inactivo."}
            else:
                base_observacion = form_data["observacion"] or "Recepcion de proveedor"
                observacion = f"{base_observacion}. Proveedor: {form_data['proveedor']}"
                try:
                    marca_recepcion = form_data["marca"] or (producto.get("marca") or "")
                    if _requiere_variante(form_data["codigo"], marca_recepcion):
                        if not marca_recepcion:
                            message = {
                                "type": "error",
                                "text": "Debes indicar una marca para registrar la recepcion por variantes.",
                            }
                        else:
                            nuevo_stock_variante = _aplicar_movimiento_variante(
                                form_data["codigo"],
                                "ingreso",
                                cantidad,
                                observacion,
                                marca=marca_recepcion,
                                bodega=form_data["bodega"],
                                proveedor=form_data["proveedor"],
                            )
                            producto = _producto_por_codigo(form_data["codigo"])
                            variantes_disponibles = _stock_variantes_por_codigo(form_data["codigo"])
                            message = {
                                "type": "success",
                                "text": (
                                    f"Recepcion registrada para {form_data['proveedor']} "
                                    f"en variante {marca_recepcion} ({form_data['bodega']}). "
                                    f"Stock variante: {nuevo_stock_variante}."
                                ),
                            }
                    else:
                        stock_anterior = int(producto["stock_actual"] or 0)
                        nuevo_stock = stock_anterior + cantidad
                        _aplicar_movimiento_stock(
                            form_data["codigo"],
                            "ingreso",
                            cantidad,
                            nuevo_stock,
                            observacion,
                            proveedor=form_data["proveedor"],
                        )
                        producto = _producto_por_codigo(form_data["codigo"])
                        message = {
                            "type": "success",
                            "text": f"Recepcion registrada correctamente para {form_data['proveedor']}. Stock actual: {nuevo_stock}.",
                        }
                except Exception as exc:
                    db.session.rollback()
                    message = {"type": "error", "text": f"No se pudo registrar la recepcion: {exc}"}

    return render_template(
        "bodega/recepcion.html",
        **_base_context(
            "recepcion",
            form_data=form_data,
            message=message,
            producto=producto,
            variantes=variantes_disponibles,
            bodegas_opciones=bodegas_opciones,
            default_bodega=DEFAULT_BODEGA,
        ),
    )


@bodega_bp.route("/etiquetas", methods=["GET", "POST"])
@admin_required
def etiquetas():
    if request.method == "POST" and not has_permission(session.get("user"), session.get("rol"), "bodega_etiquetas"):
        return _deny_bodega_perm("No tienes permiso para gestionar etiquetas.")
    codigos_raw = (request.values.get("codigos") or "").strip()
    fp = (request.values.get("fp") or "").strip()
    fp_por_codigo: dict[str, str] = {}
    fp_map_raw = (request.values.get("fp_por_codigo_json") or "").strip()
    if fp_map_raw:
        try:
            loaded = json.loads(fp_map_raw)
            if isinstance(loaded, dict):
                for k, v in loaded.items():
                    kk = (str(k) or "").strip().upper()
                    vv = (str(v) if v is not None else "").strip()
                    if kk and vv:
                        fp_por_codigo[kk] = vv
        except (json.JSONDecodeError, TypeError, ValueError):
            fp_por_codigo = {}
    etiquetas_ingreso_doc_id = ""
    raw_ing_doc = (request.values.get("etiquetas_ingreso_doc_id") or "").strip()
    if raw_ing_doc.isdigit():
        etiquetas_ingreso_doc_id = raw_ing_doc
    etiquetas_ingreso_codigos_json = ""
    raw_ing_codes = (request.values.get("etiquetas_ingreso_codigos_json") or "").strip()
    if raw_ing_codes and etiquetas_ingreso_doc_id:
        try:
            arr = json.loads(raw_ing_codes)
            if isinstance(arr, list):
                clean = sorted({str(x or "").strip().upper() for x in arr if str(x or "").strip()})
                etiquetas_ingreso_codigos_json = json.dumps(clean)
        except (json.JSONDecodeError, TypeError, ValueError):
            etiquetas_ingreso_codigos_json = ""
            etiquetas_ingreso_doc_id = ""
    historial_id = request.values.get("historial_id", type=int)
    skip_historial_register = (request.values.get("skip_historial_register") or "").strip() == "1"
    bulk_historial = (request.values.get("bulk_historial") or "").strip() == "1"

    message = None
    labels = []
    missing = []
    codes = _parse_codigos(codigos_raw)

    ids_to_load: list[int] = []
    if historial_id:
        ids_to_load = [historial_id]
    else:
        for x in request.values.getlist("historial_ids"):
            try:
                ids_to_load.append(int(x))
            except (ValueError, TypeError):
                pass
        ids_to_load = list(dict.fromkeys(ids_to_load))[:MAX_HISTORIAL_REPRINT_IDS]

    if bulk_historial and not historial_id and not ids_to_load:
        q_back = (request.values.get("q") or "").strip()
        page_back = (request.values.get("page") or "1").strip() or "1"
        return redirect(url_for("bodega.etiquetas_historial", q=q_back, page=page_back, err="no_sel"))

    if ids_to_load:
        rows_hi = HistorialEtiqueta.query.filter(HistorialEtiqueta.id.in_(ids_to_load)).all()
        by_id = {r.id: r for r in rows_hi}
        if len(ids_to_load) == 1 and ids_to_load[0] not in by_id:
            message = {"type": "error", "text": "No se encontró el registro de historial de etiquetas."}
        else:
            try:
                truncated = False
                for hid in ids_to_load:
                    if hid not in by_id:
                        continue
                    chunk = _build_labels_from_historial_row(
                        by_id[hid], fp, override_qty=_historial_reprint_qty_from_request(hid)
                    )
                    for lbl in chunk:
                        if len(labels) >= MAX_LABELS_EN_VISTA:
                            truncated = True
                            break
                        labels.append(lbl)
                    if len(labels) >= MAX_LABELS_EN_VISTA:
                        truncated = True
                        break
                missing = []
                if not codigos_raw:
                    parts = []
                    for hid in ids_to_load:
                        if hid in by_id:
                            it = by_id[hid]
                            ov = _historial_reprint_qty_from_request(hid)
                            n = ov if ov is not None else max(1, int(it.cantidad or 1))
                            parts.append(f"{it.codigo_producto} x{n}")
                    codigos_raw = ", ".join(parts)
                skip_historial_register = True
                if truncated:
                    message = {
                        "type": "success",
                        "text": f"Se muestran hasta {MAX_LABELS_EN_VISTA} etiquetas por vista. Si necesitas más, reimprime en otro lote.",
                    }
                if not labels and not message:
                    message = {
                        "type": "error",
                        "text": "No se encontraron registros de historial de etiquetas.",
                    }
            except Exception as exc:
                message = {"type": "error", "text": f"No se pudo generar QR/Barcode: {exc}"}
                labels = []
    elif codes:
        try:
            enrich_prev = _etiquetas_enrich_prev_for_codes(codes)
            labels, missing = _build_labels_from_codes(
                codes,
                fp,
                enrich_prev,
                fp_por_codigo=fp_por_codigo if fp_por_codigo else None,
            )
        except Exception as exc:
            message = {"type": "error", "text": f"No se pudo generar QR/Barcode: {exc}"}
            labels = []

    missing = sorted(set(missing))
    if missing:
        codes_txt = ", ".join(missing[:15])
        if labels:
            message = {
                "type": "warning",
                "text": "Algunos códigos no se encontraron o están inactivos: " + codes_txt,
            }
        else:
            message = {
                "type": "error",
                "text": "No encontrados o inactivos: " + codes_txt,
            }

    if labels:
        _persist_etiquetas_enrich_from_labels(labels)
    else:
        session.pop(ETIQUETAS_ENRICH_SESSION_KEY, None)
        session.modified = True

    # GET ?ajax=1 o POST (cuerpo) ajax=1 — la previsualización envía POST con ajax en el formulario.
    is_ajax = (request.values.get("ajax") or "").strip() == "1"

    if is_ajax:
        return jsonify(
            {
                "success": True,
                "labels": labels,
                "missing": missing,
            }
        )

    # El historial de impresión se registra solo vía POST /etiquetas/historial/register
    # al imprimir (beforeprint en etiquetas.html), no al cargar la página — evita duplicados.

    return render_template(
        "bodega/etiquetas.html",
        **_base_context(
            "etiquetas",
            codigos=codigos_raw,
            fp=fp,
            fp_por_codigo_json=fp_map_raw,
            labels=labels,
            missing=missing,
            message=message,
            etiquetas_ingreso_doc_id=etiquetas_ingreso_doc_id,
            etiquetas_ingreso_codigos_json=etiquetas_ingreso_codigos_json,
        ),
    )


@bodega_bp.route("/etiquetas/buscar_productos")
@admin_required
def etiquetas_buscar_productos():
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"success": True, "items": []})

    try:
        items = _buscar_productos_para_etiquetas(q, limit=40)
        return jsonify({"success": True, "items": items})
    except Exception:
        db.session.rollback()
        return jsonify({"success": False, "items": []}), 500


@bodega_bp.route("/etiquetas/ultimas_facturas_ingreso", methods=["GET"])
@admin_required
def etiquetas_ultimas_facturas_ingreso():
    """Últimas facturas (N° documento proveedor) desde ingresos vigentes que incluyen el código."""
    codigo = (request.args.get("codigo") or "").strip().upper()
    if not codigo:
        return jsonify({"success": True, "codigo": "", "items": []})

    try:
        row_ids = (
            db.session.query(IngresoDocumento.id)
            .join(
                IngresoDocumentoItem,
                IngresoDocumentoItem.ingreso_documento_id == IngresoDocumento.id,
            )
            .filter(
                func.upper(func.trim(IngresoDocumentoItem.codigo_producto)) == codigo,
                IngresoDocumento.anulado.is_(False),
            )
            .order_by(IngresoDocumento.created_at.desc(), IngresoDocumento.id.desc())
            .limit(80)
            .all()
        )
        seen: set[int] = set()
        ids: list[int] = []
        for (rid,) in row_ids:
            if rid in seen:
                continue
            seen.add(rid)
            ids.append(int(rid))
            if len(ids) >= 2:
                break

        if not ids:
            return jsonify({"success": True, "codigo": codigo, "items": []})

        docs = db.session.query(IngresoDocumento).filter(IngresoDocumento.id.in_(ids)).all()
        by_id = {int(d.id): d for d in docs}
        items: list[dict] = []
        for rid in ids:
            d = by_id.get(rid)
            if d is None:
                continue
            num = (d.numero_documento or "").strip()
            if not num:
                num = f"ING-{d.id}"
            fd = d.fecha_documento.isoformat() if d.fecha_documento else ""
            prov = (d.proveedor_nombre or "").strip() or "—"
            items.append(
                {
                    "ingreso_id": int(d.id),
                    "numero_documento": num,
                    "fecha_documento": fd,
                    "proveedor_nombre": prov,
                }
            )
        return jsonify({"success": True, "codigo": codigo, "items": items})
    except Exception:
        db.session.rollback()
        return jsonify({"success": False, "codigo": codigo, "items": []}), 500


@bodega_bp.route("/etiquetas/historial")
@admin_required
def etiquetas_historial():
    q = (request.args.get("q") or "").strip()
    page = _parse_int(request.args.get("page") or "1", allow_zero=False) or 1
    per_page = 20

    query = HistorialEtiqueta.query
    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(
                HistorialEtiqueta.codigo_producto.ilike(like),
                HistorialEtiqueta.descripcion.ilike(like),
            )
        )

    total = query.count()
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(1, page), total_pages)
    offset = (page - 1) * per_page

    rows = (
        query.order_by(HistorialEtiqueta.fecha.desc(), HistorialEtiqueta.id.desc())
        .offset(offset)
        .limit(per_page)
        .all()
    )

    return render_template(
        "bodega/etiquetas_historial.html",
        **_base_context(
            "etiquetas_historial",
            rows=rows,
            q=q,
            page=page,
            per_page=per_page,
            total=total,
            total_pages=total_pages,
        ),
    )


@bodega_bp.route("/etiquetas/historial/reimprimir/<int:historial_id>")
@admin_required
def etiquetas_historial_reimprimir(historial_id: int):
    item = db.session.get(HistorialEtiqueta, historial_id)
    if not item:
        return redirect(url_for("bodega.etiquetas_historial"))

    # Usa snapshot del historial (descripción/modelo) para códigos no existentes en catálogo.
    return redirect(
        url_for(
            "bodega.etiquetas",
            historial_id=historial_id,
            skip_historial_register="1",
        )
    )


@bodega_bp.route("/movimientos")
@admin_required
def movimientos():
    codigo = (request.args.get("codigo") or "").strip().upper()
    codigo_proveedor_raw = (request.args.get("codigo_proveedor") or "").strip()
    codigo_proveedor_norm = _normalize_codigo_proveedor(codigo_proveedor_raw) if codigo_proveedor_raw else ""
    marca = _normalize_brand(request.args.get("marca") or "")
    bodega = (request.args.get("bodega") or "").strip()
    origen_raw = (request.args.get("origen_compra") or "").strip().lower()
    origen_compra = _normalize_origen_compra(origen_raw) if origen_raw else ""
    fecha_desde = (request.args.get("fecha_desde") or "").strip()
    fecha_hasta = (request.args.get("fecha_hasta") or "").strip()
    tipo = (request.args.get("tipo") or "").strip().lower()
    ingreso_documento_id = _parse_int(request.args.get("ingreso_documento_id") or "", allow_zero=False)

    query = MovimientoStock.query

    if codigo_proveedor_norm:
        links_cp = (
            ProveedorCodigoInterno.query.filter(
                func.upper(func.trim(ProveedorCodigoInterno.codigo_proveedor)) == codigo_proveedor_norm
            ).all()
        )
        internos_por_cp = sorted({(lk.codigo_interno or "").strip().upper() for lk in links_cp if lk.codigo_interno})
        if not internos_por_cp:
            query = query.filter(text("1=0"))
        else:
            query = query.filter(MovimientoStock.codigo_producto.in_(internos_por_cp))
    if codigo:
        query = query.filter(MovimientoStock.codigo_producto.ilike(f"%{codigo}%"))
    if marca:
        query = query.filter(MovimientoStock.marca.ilike(f"%{marca}%"))
    if bodega:
        query = query.filter(MovimientoStock.bodega.ilike(f"%{bodega}%"))
    if origen_compra:
        query = query.filter(MovimientoStock.origen_compra == origen_compra)
    if tipo:
        query = query.filter(MovimientoStock.tipo == tipo)
    if ingreso_documento_id:
        query = query.filter(MovimientoStock.ingreso_documento_id == int(ingreso_documento_id))

    if fecha_desde:
        try:
            query = query.filter(MovimientoStock.fecha >= datetime.strptime(fecha_desde, "%Y-%m-%d"))
        except ValueError:
            fecha_desde = ""

    if fecha_hasta:
        try:
            hasta = datetime.strptime(fecha_hasta, "%Y-%m-%d") + timedelta(days=1)
            query = query.filter(MovimientoStock.fecha < hasta)
        except ValueError:
            fecha_hasta = ""

    movimientos_data = query.order_by(MovimientoStock.fecha.desc()).limit(500).all()

    # Enriquecer grilla: código proveedor asociado y valor neto unitario del ítem de ingreso.
    ingreso_rows = [
        mv for mv in movimientos_data
        if (mv.tipo or "").strip().lower() == "ingreso" and mv.ingreso_documento_id
    ]
    if ingreso_rows:
        ingreso_ids = sorted({int(mv.ingreso_documento_id) for mv in ingreso_rows if mv.ingreso_documento_id})

        docs = (
            IngresoDocumento.query
            .filter(IngresoDocumento.id.in_(ingreso_ids))
            .all()
        )
        rut_by_doc: dict[int, str] = {
            int(d.id): _normalize_rut(d.proveedor_rut or "")
            for d in docs
        }

        items = (
            IngresoDocumentoItem.query
            .filter(IngresoDocumentoItem.ingreso_documento_id.in_(ingreso_ids))
            .all()
        )
        item_by_key: dict[tuple[int, str, str, str, str], IngresoDocumentoItem] = {}
        codigos_internos: set[str] = set()
        for it in items:
            key = (
                int(it.ingreso_documento_id or 0),
                (it.codigo_producto or "").strip().upper(),
                _normalize_brand(it.marca or ""),
                _normalize_bodega(it.bodega or ""),
                _normalize_origen_compra(getattr(it, "origen_compra", None)),
            )
            item_by_key[key] = it
            if key[1]:
                codigos_internos.add(key[1])

        ruts = sorted({r for r in rut_by_doc.values() if r})
        cp_reverse: dict[tuple[str, str], str] = {}
        if ruts and codigos_internos:
            links = (
                ProveedorCodigoInterno.query
                .filter(ProveedorCodigoInterno.proveedor_rut.in_(ruts))
                .filter(ProveedorCodigoInterno.codigo_interno.in_(list(codigos_internos)))
                .order_by(ProveedorCodigoInterno.updated_at.desc(), ProveedorCodigoInterno.id.desc())
                .all()
            )
            for lk in links:
                rk = _normalize_rut(lk.proveedor_rut or "")
                ck = (lk.codigo_interno or "").strip().upper()
                if not rk or not ck:
                    continue
                cp_reverse.setdefault((rk, ck), lk.codigo_proveedor or "")

        for mv in movimientos_data:
            mv.codigo_proveedor_ref = ""
            mv.valor_neto_ref = None
            if (mv.tipo or "").strip().lower() != "ingreso" or not mv.ingreso_documento_id:
                continue
            doc_id = int(mv.ingreso_documento_id or 0)
            key = (
                doc_id,
                (mv.codigo_producto or "").strip().upper(),
                _normalize_brand(mv.marca or ""),
                _normalize_bodega(mv.bodega or ""),
                _normalize_origen_compra(getattr(mv, "origen_compra", None)),
            )
            it = item_by_key.get(key)
            if it is not None and it.valor_neto is not None:
                mv.valor_neto_ref = float(it.valor_neto)
            rut = rut_by_doc.get(doc_id, "")
            if rut:
                mv.codigo_proveedor_ref = cp_reverse.get(
                    (rut, (mv.codigo_producto or "").strip().upper()),
                    "",
                )

    return render_template(
        "bodega/movimientos.html",
        **_base_context(
            "movimientos",
            movimientos=movimientos_data,
            filtros={
                "codigo": codigo,
                "codigo_proveedor": codigo_proveedor_raw,
                "marca": marca,
                "bodega": bodega,
                "origen_compra": origen_compra,
                "fecha_desde": fecha_desde,
                "fecha_hasta": fecha_hasta,
                "tipo": tipo,
                "ingreso_documento_id": ingreso_documento_id or "",
            },
        ),
    )


def _picking_line_stock_disponible(codigo: str, marca: str, bodega: str) -> int | None:
    code = (codigo or "").strip().upper()
    if not code:
        return None
    variantes = _stock_variantes_por_codigo(code)
    if not variantes:
        producto = _producto_por_codigo(code)
        if not producto:
            return None
        return int(producto.get("stock_actual") or 0)
    marca_n = _normalize_brand(marca)
    b = _normalize_bodega(bodega)
    if marca_n:
        for v in variantes:
            if (v.get("marca") or "").strip().upper() == marca_n and (v.get("bodega") or "").strip() == b:
                return int(v.get("stock") or 0)
        return 0
    return _suma_stock_variantes_en_bodega(variantes, b)


@bodega_bp.route("/picking-venta")
@login_required
def picking_venta_lista():
    if not has_permission(session.get("user"), session.get("rol"), "bodega_picking"):
        return _deny_bodega_perm("No tienes permiso para gestionar picking de venta.")
    q_raw = (request.args.get("q") or "").strip()
    q = q_raw.upper()
    estado_filtro = (request.args.get("estado") or "activos").strip().lower()
    if estado_filtro not in ("pendiente", "en_preparacion", "entregado", "activos", "todos"):
        estado_filtro = "activos"

    query = PickingVenta.query.join(DocumentoVenta, DocumentoVenta.id == PickingVenta.orden_venta_id)
    if q:
        query = query.filter(
            or_(
                func.upper(DocumentoVenta.numero).like(f"%{q}%"),
                DocumentoVenta.cliente_nombre.ilike(f"%{q_raw}%"),
            )
        )
    if estado_filtro in ("pendiente", "en_preparacion", "entregado"):
        query = query.filter(PickingVenta.status == estado_filtro)
    elif estado_filtro == "activos":
        query = query.filter(PickingVenta.status.in_(["pendiente", "en_preparacion"]))
    # estado_filtro == "todos": sin filtro por estado

    pickings = query.order_by(PickingVenta.updated_at.desc()).limit(100).all()
    ordenes = {p.orden_venta_id: db.session.get(DocumentoVenta, p.orden_venta_id) for p in pickings}

    picking_ids = [p.id for p in pickings]
    lineas_por_picking: dict[int, int] = {}
    if picking_ids:
        rows = db.session.execute(
            select(PickingVentaLine.picking_id, func.count(PickingVentaLine.id))
            .where(PickingVentaLine.picking_id.in_(picking_ids))
            .group_by(PickingVentaLine.picking_id)
        ).all()
        lineas_por_picking = {int(r[0]): int(r[1]) for r in rows}

    stats = {
        "pendiente": PickingVenta.query.filter_by(status="pendiente").count(),
        "en_preparacion": PickingVenta.query.filter_by(status="en_preparacion").count(),
        "entregado": PickingVenta.query.filter_by(status="entregado").count(),
    }
    stats["activos"] = stats["pendiente"] + stats["en_preparacion"]
    stats["total"] = stats["pendiente"] + stats["en_preparacion"] + stats["entregado"]

    return render_template(
        "bodega/picking_venta.html",
        **_base_context(
            "picking_venta",
            pickings=pickings,
            ordenes=ordenes,
            search_q=q_raw,
            estado_filtro=estado_filtro,
            picking_stats=stats,
            lineas_por_picking=lineas_por_picking,
        ),
    )


@bodega_bp.route("/picking-venta/<int:pid>")
@login_required
def picking_venta_detalle(pid: int):
    if not has_permission(session.get("user"), session.get("rol"), "bodega_picking"):
        return _deny_bodega_perm("No tienes permiso para gestionar picking de venta.")
    picking = db.session.get(PickingVenta, pid)
    if picking is None:
        flash("Picking no encontrado.", "error")
        return redirect(url_for("bodega.picking_venta_lista"))
    orden = db.session.get(DocumentoVenta, picking.orden_venta_id)
    lineas = (
        PickingVentaLine.query.filter_by(picking_id=picking.id).order_by(PickingVentaLine.orden_linea.asc()).all()
    )
    lineas_ctx = []
    total_pedido = 0
    total_entregado = 0
    for ln in lineas:
        disp = _picking_line_stock_disponible(ln.codigo_producto, ln.marca, ln.bodega)
        lineas_ctx.append({"linea": ln, "stock_disp": disp})
        total_pedido += int(ln.cantidad_pedida or 0)
        total_entregado += int(ln.cantidad_entregada or 0)
    pct_avance = round(100 * total_entregado / total_pedido, 0) if total_pedido > 0 else 0
    return render_template(
        "bodega/picking_venta_detalle.html",
        **_base_context(
            "picking_venta",
            picking=picking,
            orden=orden,
            lineas_ctx=lineas_ctx,
            total_pedido=total_pedido,
            total_entregado=total_entregado,
            pct_avance=pct_avance,
        ),
    )


@bodega_bp.route("/api/picking-venta/<int:pid>", methods=["DELETE"])
@login_required
def api_picking_venta_eliminar(pid: int):
    if not has_permission(session.get("user"), session.get("rol"), "bodega_picking"):
        return jsonify({"ok": False, "message": "Sin permiso para picking"}), 403
    """Elimina picking y lineas solo si aun no fue entregado al vendedor (no altera stock ni la OV)."""
    picking = db.session.get(PickingVenta, pid)
    if picking is None:
        return jsonify({"success": False, "message": "Picking no encontrado"}), 404
    if (picking.status or "").strip().lower() == "entregado":
        return jsonify(
            {
                "success": False,
                "message": "No se puede eliminar un picking ya entregado al vendedor.",
            }
        ), 400
    PickingVentaLine.query.filter_by(picking_id=picking.id).delete(synchronize_session=False)
    db.session.delete(picking)
    db.session.commit()
    return jsonify({"success": True, "redirect_url": url_for("bodega.picking_venta_lista")})


@bodega_bp.route("/api/picking-venta/<int:pid>/lineas", methods=["POST"])
@login_required
def api_picking_venta_guardar_lineas(pid: int):
    if not has_permission(session.get("user"), session.get("rol"), "bodega_picking"):
        return jsonify({"ok": False, "message": "Sin permiso para picking"}), 403
    picking = db.session.get(PickingVenta, pid)
    if picking is None:
        return jsonify({"success": False, "message": "Picking no encontrado"}), 404
    if (picking.status or "").strip().lower() == "entregado":
        return jsonify({"success": False, "message": "El picking ya fue marcado como entregado."}), 400

    data = request.get_json(silent=True) or {}
    updates = data.get("lineas") if isinstance(data.get("lineas"), list) else []
    by_id = {ln.id: ln for ln in PickingVentaLine.query.filter_by(picking_id=picking.id).all()}
    for row in updates:
        lid = int(row.get("id") or 0)
        if lid not in by_id:
            continue
        ln = by_id[lid]
        try:
            ent = int(row.get("cantidad_entregada"))
        except (TypeError, ValueError):
            continue
        ent = max(0, min(ent, int(ln.cantidad_pedida or 0)))
        ln.cantidad_entregada = ent

    picking.status = "en_preparacion"
    picking.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"success": True})


@bodega_bp.route("/api/picking-venta/<int:pid>/entregar", methods=["POST"])
@login_required
def api_picking_venta_entregar(pid: int):
    if not has_permission(session.get("user"), session.get("rol"), "bodega_picking"):
        return jsonify({"ok": False, "message": "Sin permiso para picking"}), 403
    picking = db.session.get(PickingVenta, pid)
    if picking is None:
        return jsonify({"success": False, "message": "Picking no encontrado"}), 404
    if (picking.status or "").strip().lower() == "entregado":
        return jsonify({"success": False, "message": "Ya estaba entregado."}), 400

    lineas = PickingVentaLine.query.filter_by(picking_id=picking.id).all()
    for ln in lineas:
        if int(ln.cantidad_entregada or 0) < int(ln.cantidad_pedida or 0):
            return jsonify(
                {
                    "success": False,
                    "message": f"Falta mercaderia por marcar: {ln.codigo_producto} (pedido {ln.cantidad_pedida}, entregado {ln.cantidad_entregada}).",
                }
            ), 400

    picking.status = "entregado"
    picking.usuario_entrega = (session.get("user") or "").strip() or None
    picking.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"success": True})
