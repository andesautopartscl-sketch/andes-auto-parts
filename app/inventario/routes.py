from __future__ import annotations

from datetime import datetime

from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import func, text

from app.extensions import db
from app.utils.decorators import login_required
from app.bodega.models import MovimientoStock, ProductoVarianteStock
from .models import LabelPrintHistory, TransferenciaStock

inventario_bp = Blueprint(
    "inventario", __name__, url_prefix="/inventario",
    template_folder="../../templates"
)


def _current_user() -> str:
    return session.get("user") or "sistema"


def _get_bodegas() -> list[str]:
    """Return all distinct warehouses that have stock."""
    rows = (
        db.session.query(ProductoVarianteStock.bodega)
        .distinct()
        .order_by(ProductoVarianteStock.bodega)
        .all()
    )
    return [r[0] for r in rows if r[0]]


def _stock_for_product(codigo: str, marca: str, bodega: str) -> int:
    variante = ProductoVarianteStock.query.filter_by(
        codigo_producto=codigo.strip().upper(),
        marca=marca.strip().upper(),
        bodega=bodega.strip(),
    ).first()
    return int(variante.stock) if variante else 0


def _resolve_product_by_code(codigo: str) -> tuple[int | None, str]:
    row = db.session.execute(
        text(
            """
            SELECT id, COALESCE(DESCRIPCION, '') AS descripcion
            FROM productos
            WHERE UPPER(CODIGO) = :codigo
            LIMIT 1
            """
        ),
        {"codigo": (codigo or "").strip().upper()},
    ).mappings().first()
    if not row:
        return None, ""
    pid = int(row.get("id") or 0)
    pname = (row.get("descripcion") or "").strip()
    return (pid if pid > 0 else None), pname


@inventario_bp.route("/transferencias", methods=["GET", "POST"])
@login_required
def transferencias():
    bodegas = _get_bodegas()
    historial = (
        TransferenciaStock.query
        .order_by(TransferenciaStock.fecha.desc())
        .limit(100)
        .all()
    )
    return render_template(
        "inventario/transferencias.html",
        bodegas=bodegas,
        historial=historial,
        active_page="transferencias",
    )


@inventario_bp.route("/api/transferencia", methods=["POST"])
@login_required
def api_transferencia():
    data = request.get_json(force=True) or {}
    codigo = (data.get("codigo") or "").strip().upper()
    marca = (data.get("marca") or "").strip().upper()
    bodega_origen = (data.get("bodega_origen") or "").strip()
    bodega_destino = (data.get("bodega_destino") or "").strip()
    cantidad = int(data.get("cantidad") or 0)
    observacion = (data.get("observacion") or "").strip()

    if not codigo:
        return jsonify({"ok": False, "error": "Código de producto requerido"}), 400
    if not bodega_origen or not bodega_destino:
        return jsonify({"ok": False, "error": "Bodegas de origen y destino requeridas"}), 400
    if bodega_origen == bodega_destino:
        return jsonify({"ok": False, "error": "Origen y destino no pueden ser la misma bodega"}), 400
    if cantidad < 1:
        return jsonify({"ok": False, "error": "Cantidad debe ser mayor a 0"}), 400

    stock_origen = _stock_for_product(codigo, marca, bodega_origen)
    if stock_origen < cantidad:
        return jsonify({
            "ok": False,
            "error": f"Stock insuficiente en {bodega_origen}: disponible {stock_origen}, solicitado {cantidad}",
        }), 400

    try:
        usuario = _current_user()
        fecha = datetime.utcnow()

        # Reduce stock in origin
        v_origen = ProductoVarianteStock.query.filter_by(
            codigo_producto=codigo, marca=marca, bodega=bodega_origen
        ).first()
        v_origen.stock -= cantidad
        v_origen.updated_at = fecha

        # Increase stock in destination (create if not exists)
        v_destino = ProductoVarianteStock.query.filter_by(
            codigo_producto=codigo, marca=marca, bodega=bodega_destino
        ).first()
        if v_destino is None:
            from app.ventas.models import DocumentoVenta  # avoid circular; just for context
            v_destino = ProductoVarianteStock(
                codigo_producto=codigo,
                marca=marca,
                bodega=bodega_destino,
                proveedor=v_origen.proveedor if v_origen else "",
                stock=0,
                created_at=fecha,
                updated_at=fecha,
            )
            db.session.add(v_destino)

        v_destino.stock += cantidad
        v_destino.updated_at = fecha

        # Log movement: salida from origin
        mov_salida = MovimientoStock(
            codigo_producto=codigo,
            tipo="salida",
            cantidad=cantidad,
            fecha=fecha,
            usuario=usuario,
            marca=marca,
            bodega=bodega_origen,
            observacion=f"Transferencia a {bodega_destino}: {observacion}",
        )
        db.session.add(mov_salida)
        db.session.flush()

        # Log movement: ingreso to destination
        mov_ingreso = MovimientoStock(
            codigo_producto=codigo,
            tipo="ingreso",
            cantidad=cantidad,
            fecha=fecha,
            usuario=usuario,
            marca=marca,
            bodega=bodega_destino,
            observacion=f"Transferencia desde {bodega_origen}: {observacion}",
        )
        db.session.add(mov_ingreso)
        db.session.flush()

        # Record transfer
        transferencia = TransferenciaStock(
            codigo_producto=codigo,
            marca=marca,
            cantidad=cantidad,
            bodega_origen=bodega_origen,
            bodega_destino=bodega_destino,
            fecha=fecha,
            usuario=usuario,
            observacion=observacion,
            movimiento_salida_id=mov_salida.id,
            movimiento_entrada_id=mov_ingreso.id,
        )
        db.session.add(transferencia)
        db.session.commit()

        return jsonify({
            "ok": True,
            "transferencia_id": transferencia.id,
            "stock_origen_nuevo": v_origen.stock,
            "stock_destino_nuevo": v_destino.stock,
        })
    except Exception as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 500


@inventario_bp.route("/api/stock/producto", methods=["GET"])
@login_required
def api_stock_producto():
    codigo = (request.args.get("codigo") or "").strip().upper()
    marca = (request.args.get("marca") or "").strip().upper()
    if not codigo:
        return jsonify({"ok": False, "variantes": []})
    variantes = (
        ProductoVarianteStock.query
        .filter_by(codigo_producto=codigo, marca=marca)
        .all() if marca else
        ProductoVarianteStock.query
        .filter_by(codigo_producto=codigo)
        .all()
    )
    return jsonify({
        "ok": True,
        "variantes": [
            {"bodega": v.bodega, "marca": v.marca, "stock": v.stock}
            for v in variantes
        ],
    })


@inventario_bp.route("/api/bodegas", methods=["GET"])
@login_required
def api_bodegas():
    return jsonify({"ok": True, "bodegas": _get_bodegas()})


@inventario_bp.route("/labels/history", methods=["GET"])
@login_required
def labels_history():
    q = (request.args.get("q") or "").strip()
    try:
        page = max(1, int(request.args.get("page") or 1))
    except ValueError:
        page = 1
    per_page = 30

    query = LabelPrintHistory.query
    if q:
        like = f"%{q}%"
        query = query.filter(
            (LabelPrintHistory.product_name.ilike(like))
            | (LabelPrintHistory.document_reference.ilike(like))
            | (LabelPrintHistory.user_id.ilike(like))
        )

    total = query.count()
    rows = (
        query.order_by(LabelPrintHistory.date_time.desc(), LabelPrintHistory.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    total_pages = max(1, (total + per_page - 1) // per_page)

    return render_template(
        "inventario/labels_history.html",
        rows=rows,
        q=q,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=total_pages,
        active_page="labels_history",
    )


@inventario_bp.route("/labels/history/register", methods=["POST"])
@login_required
def labels_history_register():
    payload = request.get_json(silent=True) or {}
    labels = payload.get("labels") or []
    reference = (payload.get("document_reference") or "").strip()[:120]

    print(f"[LABEL_HISTORY] Received {len(labels)} labels for registration")
    print(f"[LABEL_HISTORY] Payload: {payload}")

    if not isinstance(labels, list) or not labels:
        print("[LABEL_HISTORY] ERROR: No labels in payload")
        return jsonify({"ok": False, "error": "No se recibieron etiquetas para registrar"}), 400

    aggregated: dict[int, dict] = {}
    for label in labels:
        codigo = (label.get("codigo") or "").strip().upper()
        if not codigo:
            print(f"[LABEL_HISTORY] Skipping label with empty codigo")
            continue
        
        product_id, product_name = _resolve_product_by_code(codigo)
        if not product_id:
            print(f"[LABEL_HISTORY] WARNING: Could not resolve product '{codigo}'")
            continue

        qty = int(label.get("cantidad") or 1)
        qty = 1 if qty < 1 else qty

        entry = aggregated.get(product_id)
        if entry is None:
            aggregated[product_id] = {
                "product_name": product_name or codigo,
                "quantity": qty,
            }
        else:
            entry["quantity"] += qty

        print(f"[LABEL_HISTORY] Processed '{codigo}': product_id={product_id}, qty={qty}")

    if not aggregated:
        print("[LABEL_HISTORY] ERROR: No aggregated records after processing")
        return jsonify({"ok": False, "error": "No fue posible mapear etiquetas a productos"}), 400

    user_id = _current_user()
    now = datetime.utcnow()

    try:
        saved_count = 0
        for product_id, item in aggregated.items():
            record = LabelPrintHistory(
                product_id=product_id,
                product_name=item["product_name"][:255],
                quantity=int(item["quantity"]),
                user_id=user_id,
                date_time=now,
                document_reference=reference,
            )
            db.session.add(record)
            saved_count += 1
            print(f"[LABEL_HISTORY] Added record: product_id={product_id}, qty={item['quantity']}, user={user_id}")

        db.session.commit()
        print(f"[LABEL_HISTORY] SUCCESS: Committed {saved_count} records to database")
        return jsonify({"ok": True, "saved": saved_count})
    except Exception as exc:
        db.session.rollback()
        print(f"[LABEL_HISTORY] EXCEPTION: {exc}")
        return jsonify({"ok": False, "error": str(exc)}), 500
