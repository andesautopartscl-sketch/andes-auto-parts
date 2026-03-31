import base64
import io
import re
from datetime import date, datetime, timedelta

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import or_, text
import barcode
import qrcode
from barcode.writer import ImageWriter

from app.extensions import db
from app.seguridad.models import Usuario
from app.ventas.models import Proveedor
from app.utils.decorators import admin_required, login_required
from app.utils.rut_utils import clean_rut, format_rut, is_valid_rut
from app.models import SessionDB, Producto
from app.utils.product_audit import register_product_audit

from .models import HistorialEtiqueta, IngresoDocumento, IngresoDocumentoItem, MovimientoStock, ProductoVarianteStock
from app.inventario.models import LabelPrintHistory, TransferenciaStock


bodega_bp = Blueprint("bodega", __name__, url_prefix="/bodega")

DEFAULT_BODEGA = "Bodega 1"
DEFAULT_COUNTRY = "Chile"


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
        WHERE UPPER(CODIGO) = :codigo
          AND COALESCE(ACTIVO, 1) = 1
        LIMIT 1
        """
    )
    return db.session.execute(query, {"codigo": codigo.upper()}).mappings().first()


def _normalize_brand(raw: str) -> str:
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
    merged: dict[tuple[str, str, str], dict] = {}
    for row in rows:
        key = (
            (row.get("codigo") or "").strip().upper(),
            _normalize_brand(row.get("marca") or ""),
            _normalize_bodega(row.get("bodega") or ""),
        )
        if key[0] == "":
            continue
        current = merged.get(key)
        if current is None:
            merged[key] = dict(row)
            merged[key]["codigo"] = key[0]
            merged[key]["marca"] = key[1]
            merged[key]["bodega"] = key[2]
        else:
            current["cantidad"] = int(current.get("cantidad") or 0) + int(row.get("cantidad") or 0)
            notes = [n for n in [current.get("nota", "").strip(), (row.get("nota") or "").strip()] if n]
            current["nota"] = " | ".join(dict.fromkeys(notes))[:255]

    return list(merged.values())


def _parse_ingreso_rows() -> tuple[list[dict], list[str]]:
    codes = request.form.getlist("codigo_producto[]")
    brands = request.form.getlist("marca_producto[]")
    warehouses = request.form.getlist("bodega_producto[]")
    quantities = request.form.getlist("cantidad_producto[]")
    notes = request.form.getlist("nota_producto[]")

    max_len = max(len(codes), len(brands), len(warehouses), len(quantities), len(notes), 1)
    rows: list[dict] = []
    errors: list[str] = []

    for idx in range(max_len):
        codigo = (codes[idx] if idx < len(codes) else "").strip().upper()
        marca = _normalize_brand(brands[idx] if idx < len(brands) else "")
        bodega = _normalize_bodega(warehouses[idx] if idx < len(warehouses) else "")
        cantidad_raw = (quantities[idx] if idx < len(quantities) else "").strip()
        nota = (notes[idx] if idx < len(notes) else "").strip()[:255]

        is_empty = not any([codigo, marca, cantidad_raw, nota])
        if is_empty:
            continue

        cantidad = _parse_int(cantidad_raw)
        if not codigo:
            errors.append(f"Fila {idx + 1}: falta el codigo de producto.")
            continue
        if cantidad is None:
            errors.append(f"Fila {idx + 1}: la cantidad debe ser un entero mayor a 0.")
            continue

        rows.append(
            {
                "codigo": codigo,
                "marca": marca,
                "bodega": bodega,
                "cantidad": int(cantidad),
                "nota": nota,
            }
        )

    return _merge_ingreso_rows(rows), errors


def _stock_variantes_por_codigo(codigo: str) -> list[dict]:
    rows = (
        ProductoVarianteStock.query
        .filter_by(codigo_producto=codigo.upper())
        .order_by(ProductoVarianteStock.marca.asc(), ProductoVarianteStock.bodega.asc())
        .all()
    )
    return [
        {
            "id": row.id,
            "codigo": row.codigo_producto,
            "marca": row.marca,
            "proveedor": row.proveedor or "",
            "bodega": row.bodega,
            "stock": int(row.stock or 0),
        }
        for row in rows
    ]


def _sincronizar_stock_base_desde_variantes(codigo: str) -> None:
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


def _obtener_o_crear_variante(codigo: str, marca: str, bodega: str, proveedor: str | None = None) -> ProductoVarianteStock:
    codigo = codigo.upper()
    marca = _normalize_brand(marca)
    bodega = _normalize_bodega(bodega)
    variante = (
        ProductoVarianteStock.query
        .filter_by(codigo_producto=codigo, marca=marca, bodega=bodega)
        .first()
    )
    if variante is None:
        variante = ProductoVarianteStock(
            codigo_producto=codigo,
            marca=marca,
            proveedor=(proveedor or "").strip()[:150] or None,
            bodega=bodega,
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


def _registrar_movimiento(
    codigo: str,
    tipo: str,
    cantidad: int,
    observacion: str,
    proveedor: str | None = None,
    marca: str | None = None,
    bodega: str | None = None,
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
            ingreso_documento_id=ingreso_documento_id,
            observacion=observacion[:255] if observacion else None,
        )
    )
    # Auditoría de cambios de stock ligada al movimiento.
    sess = SessionDB()
    try:
        p = sess.query(Producto).filter(Producto.codigo == codigo_up).first()
        stock_total = 0
        if p:
            stock_total = int(
                (p.stock_10jul or 0)
                + (p.stock_brasil or 0)
                + (p.stock_g_avenida or 0)
                + (p.stock_orientales or 0)
                + (p.stock_b20_outlet or 0)
                + (p.stock_transito or 0)
            )
        register_product_audit(
            sess,
            actor=(session.get("user") or "sistema"),
            action="stock_move",
            modulo="bodega",
            producto_codigo=codigo_up,
            req=request,
            metadata={
                "tipo": tipo,
                "cantidad_movimiento": cantidad,
                "stock_total_actual": stock_total,
                "marca": marca,
                "bodega": bodega,
                "proveedor": proveedor,
                "observacion": observacion,
            },
        )
        sess.commit()
    except Exception:
        sess.rollback()
    finally:
        sess.close()


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
    proveedor: str | None = None,
    nuevo_stock_variante: int | None = None,
) -> int:
    variante = _obtener_o_crear_variante(codigo, marca, bodega, proveedor=proveedor)
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
    )
    db.session.commit()
    return candidato


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


def _parse_codigos(raw_codes: str) -> list[str]:
    expanded_codes = []
    chunks = re.split(r"[\n,;]+", raw_codes or "")

    for raw_part in chunks:
        part = (raw_part or "").strip()
        if not part:
            continue

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


def _build_labels_from_codes(codes: list[str], fp: str):
    labels = []
    missing = []
    for code in codes:
        producto = _producto_por_codigo(code)
        if producto is None:
            missing.append(code)
            continue
        descripcion = (producto.get("descripcion") or "SIN DESCRIPCION").strip()
        modelo = (producto.get("modelo") or "").strip()
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
    return labels, missing


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
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    form_data = {
        "supplier_rut": _normalize_rut(request.form.get("supplier_rut") or ""),
        "supplier_name": (request.form.get("supplier_name") or "").strip(),
        "supplier_giro": (request.form.get("supplier_giro") or "").strip(),
        "supplier_email": (request.form.get("supplier_email") or "").strip(),
        "supplier_address": (request.form.get("supplier_address") or "").strip(),
        "supplier_comuna": (request.form.get("supplier_comuna") or "").strip(),
        "supplier_region": (request.form.get("supplier_region") or "").strip(),
        "supplier_country": (request.form.get("supplier_country") or DEFAULT_COUNTRY).strip() or DEFAULT_COUNTRY,
        "fecha_documento": (request.form.get("fecha_documento") or today_str).strip() or today_str,
        "numero_documento": (request.form.get("numero_documento") or "").strip(),
        "observacion": (request.form.get("observacion") or "").strip()[:255],
    }

    default_rows = [
        {
            "codigo": "",
            "marca": "",
            "bodega": DEFAULT_BODEGA,
            "cantidad": "",
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
                form_data["supplier_name"] = (proveedor.empresa or proveedor.nombre or "").strip()
                form_data["supplier_giro"] = (proveedor.giro or "").strip()
                form_data["supplier_email"] = (proveedor.email or "").strip()
                form_data["supplier_address"] = (proveedor.direccion or "").strip()
                form_data["supplier_comuna"] = (proveedor.comuna or "").strip()
                form_data["supplier_region"] = (proveedor.region or "").strip()
                form_data["supplier_country"] = (proveedor.pais or DEFAULT_COUNTRY).strip() or DEFAULT_COUNTRY
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
                        "text": "Proveedor no encontrado. Completa nombre, direccion, comuna y region para crearlo en linea.",
                    }

            if message is None:
                try:
                    proveedor = _buscar_proveedor_por_rut(form_data["supplier_rut"])
                    if proveedor is None:
                        proveedor = Proveedor(
                            nombre=form_data["supplier_name"][:200],
                            empresa=form_data["supplier_name"][:200],
                            rut=form_data["supplier_rut"],
                            giro=form_data["supplier_giro"][:200],
                            direccion=form_data["supplier_address"][:300],
                            comuna=form_data["supplier_comuna"][:120],
                            region=form_data["supplier_region"][:120],
                            ciudad=form_data["supplier_comuna"][:120],
                            pais=form_data["supplier_country"][:120],
                            email=form_data["supplier_email"][:150],
                            activo=True,
                        )
                        db.session.add(proveedor)
                        db.session.flush()
                        created_supplier_inline = True

                    fecha_documento = datetime.strptime(form_data["fecha_documento"], "%Y-%m-%d").date()

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
                        usuario=session.get("user") or "sistema",
                    )
                    db.session.add(documento)
                    db.session.flush()

                    for row in rows:
                        codigo = row["codigo"]
                        marca = row["marca"]
                        bodega = row["bodega"]
                        cantidad = int(row["cantidad"])
                        nota = (row.get("nota") or "").strip()

                        producto = _producto_por_codigo(codigo)
                        if producto is None:
                            raise ValueError(f"Producto {codigo} no existe o esta inactivo.")

                        if _requiere_variante(codigo, marca):
                            if not marca:
                                raise ValueError(f"El producto {codigo} requiere marca/variante.")
                            variante = _obtener_o_crear_variante(
                                codigo,
                                marca,
                                bodega,
                                proveedor=form_data["supplier_name"],
                            )
                            variante.stock = int(variante.stock or 0) + cantidad
                            _sincronizar_stock_base_desde_variantes(codigo)
                        else:
                            stock_anterior = int(producto["stock_actual"] or 0)
                            _actualizar_stock(codigo, stock_anterior + cantidad)

                        db.session.add(
                            IngresoDocumentoItem(
                                ingreso_documento_id=documento.id,
                                codigo_producto=codigo,
                                descripcion_producto=(producto.get("descripcion") or "")[:255],
                                marca=marca,
                                bodega=bodega,
                                cantidad=cantidad,
                                nota=nota[:255],
                            )
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
                            ingreso_documento_id=documento.id,
                        )

                    db.session.commit()
                    document_created = documento.id
                    message = {
                        "type": "success",
                        "text": (
                            f"Ingreso guardado en documento #{documento.id} con {len(rows)} item(s)."
                            + (" Proveedor creado en linea." if created_supplier_inline else "")
                        ),
                    }
                    rows = default_rows
                except ValueError as exc:
                    db.session.rollback()
                    message = {"type": "error", "text": str(exc)}
                except Exception as exc:
                    db.session.rollback()
                    message = {"type": "error", "text": f"No se pudo guardar el ingreso: {exc}"}

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
        ),
    )


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

    return jsonify(
        {
            "success": True,
            "found": True,
            "proveedor": {
                "rut": format_rut(rut),
                "name": (proveedor.empresa or proveedor.nombre or "").strip(),
                "giro": (proveedor.giro or "").strip(),
                "email": (proveedor.email or "").strip(),
                "address": (proveedor.direccion or "").strip(),
                "comuna": (proveedor.comuna or "").strip(),
                "region": (proveedor.region or "").strip(),
                "country": (proveedor.pais or DEFAULT_COUNTRY).strip() or DEFAULT_COUNTRY,
            },
        }
    )


@bodega_bp.route("/salida", methods=["GET", "POST"])
@admin_required
def salida():
    form_data = {
        "codigo": (request.form.get("codigo") or "").strip().upper(),
        "marca": _normalize_brand(request.form.get("marca") or ""),
        "bodega": _normalize_bodega(request.form.get("bodega") or ""),
        "cantidad": (request.form.get("cantidad") or "").strip(),
        "observacion": (request.form.get("observacion") or "").strip(),
    }
    message = None
    producto = None
    variantes_disponibles = []

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

    return render_template(
        "bodega/salida.html",
        **_base_context(
            "salida",
            form_data=form_data,
            message=message,
            producto=producto,
            variantes=variantes_disponibles,
        ),
    )


@bodega_bp.route("/ajuste", methods=["GET", "POST"])
@admin_required
def ajuste():
    form_data = {
        "codigo": (request.form.get("codigo") or "").strip().upper(),
        "marca": _normalize_brand(request.form.get("marca") or ""),
        "bodega": _normalize_bodega(request.form.get("bodega") or ""),
        "nuevo_stock": (request.form.get("nuevo_stock") or "").strip(),
        "observacion": (request.form.get("observacion") or "").strip(),
    }
    message = None
    producto = None
    variantes_disponibles = []

    if request.method == "POST":
        nuevo_stock = _parse_int(form_data["nuevo_stock"], allow_zero=True)
        if not form_data["codigo"]:
            message = {"type": "error", "text": "Debes ingresar un codigo de producto."}
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

    return render_template(
        "bodega/ajuste.html",
        **_base_context(
            "ajuste",
            form_data=form_data,
            message=message,
            producto=producto,
            variantes=variantes_disponibles,
        ),
    )


@bodega_bp.route("/recepcion", methods=["GET", "POST"])
@admin_required
def recepcion():
    form_data = {
        "proveedor": (request.form.get("proveedor") or "").strip(),
        "codigo": (request.form.get("codigo_producto") or "").strip().upper(),
        "marca": _normalize_brand(request.form.get("marca") or ""),
        "bodega": _normalize_bodega(request.form.get("bodega") or ""),
        "cantidad": (request.form.get("cantidad") or "").strip(),
        "observacion": (request.form.get("observacion") or "").strip(),
    }
    message = None
    producto = None
    variantes_disponibles = []

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
        ),
    )


@bodega_bp.route("/etiquetas", methods=["GET", "POST"])
@admin_required
def etiquetas():
    codigos_raw = (request.values.get("codigos") or "").strip()
    fp = (request.values.get("fp") or "").strip()

    message = None
    labels = []
    missing = []
    codes = _parse_codigos(codigos_raw)

    if codes:
        try:
            labels, missing = _build_labels_from_codes(codes, fp)
        except Exception as exc:
            message = {"type": "error", "text": f"No se pudo generar QR/Barcode: {exc}"}
            labels = []

    if missing:
        message = {
            "type": "error",
            "text": "No encontrados o inactivos: " + ", ".join(missing[:15]),
        }

    is_ajax = request.args.get("ajax") == "1"

    if is_ajax:
        return jsonify(
            {
                "success": True,
                "labels": labels,
                "missing": missing,
            }
        )

    if labels:
        saved, history_error = _registrar_historial_etiquetas(labels)
        if not saved and not message:
            message = {
                "type": "error",
                "text": "Las etiquetas se generaron, pero no se pudo registrar el historial.",
            }

    return render_template(
        "bodega/etiquetas.html",
        **_base_context(
            "etiquetas",
            codigos=codigos_raw,
            fp=fp,
            labels=labels,
            missing=missing,
            message=message,
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

    codigos = f"{item.codigo_producto} x{max(1, int(item.cantidad or 1))}"
    return redirect(url_for("bodega.etiquetas", codigos=codigos))


@bodega_bp.route("/movimientos")
@admin_required
def movimientos():
    codigo = (request.args.get("codigo") or "").strip().upper()
    marca = _normalize_brand(request.args.get("marca") or "")
    bodega = (request.args.get("bodega") or "").strip()
    fecha_desde = (request.args.get("fecha_desde") or "").strip()
    fecha_hasta = (request.args.get("fecha_hasta") or "").strip()
    tipo = (request.args.get("tipo") or "").strip().lower()

    query = MovimientoStock.query

    if codigo:
        query = query.filter(MovimientoStock.codigo_producto.ilike(f"%{codigo}%"))
    if marca:
        query = query.filter(MovimientoStock.marca.ilike(f"%{marca}%"))
    if bodega:
        query = query.filter(MovimientoStock.bodega.ilike(f"%{bodega}%"))
    if tipo:
        query = query.filter(MovimientoStock.tipo == tipo)

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

    return render_template(
        "bodega/movimientos.html",
        **_base_context(
            "movimientos",
            movimientos=movimientos_data,
            filtros={
                "codigo": codigo,
                "marca": marca,
                "bodega": bodega,
                "fecha_desde": fecha_desde,
                "fecha_hasta": fecha_hasta,
                "tipo": tipo,
            },
        ),
    )
