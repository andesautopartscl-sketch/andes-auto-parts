"""Ingreso rápido mobile: reutiliza persistencia de bodega/ingreso ERP."""
from __future__ import annotations

from datetime import datetime

from flask import session

from app.bodega.models import IngresoDocumento, IngresoDocumentoItem, ProductoVarianteStock
from app.bodega.routes import (
    DEFAULT_BODEGA,
    INGRESO_METODOS_PAGO_OPCIONES,
    _actualizar_stock,
    _buscar_proveedor_por_rut,
    _ingreso_resolve_ciudad_chile,
    _normalize_bodega,
    _normalize_brand,
    _normalize_origen_compra,
    _obtener_o_crear_variante,
    _parse_valor_neto_chile,
    _producto_por_codigo,
    _propagar_precio_venta_ingreso_a_catalogo,
    _proveedor_json_ingreso,
    _registrar_movimiento,
    _requiere_variante,
    _sincronizar_stock_base_desde_variantes,
    _upsert_mapa_proveedor_codigo,
)
from app.extensions import db
from app.utils.permissions import has_permission
from app.utils.phone_format import phone_to_compact_e164
from app.utils.rut_utils import clean_rut
from app.ventas.models import Proveedor

MOBILE_ORIGIN_TAG = "[Andes Mobile Ingreso]"


def puede_registrar_ingreso(user: str | None, rol: str | None) -> bool:
    return has_permission(user, rol, "bodega_ingreso")


def metodos_pago_opciones() -> list[dict]:
    return [{"value": v, "label": lbl} for v, lbl in INGRESO_METODOS_PAGO_OPCIONES]


def producto_linea_ingreso(codigo: str) -> dict | None:
    producto = _producto_por_codigo((codigo or "").strip().upper())
    if producto is None:
        return None
    return {
        "codigo": (producto.get("codigo") or "").strip().upper(),
        "descripcion": (producto.get("descripcion") or "").strip(),
        "marca": (producto.get("marca") or "").strip(),
        "valor_neto": 0,
        "cantidad": 1,
        "bodega": DEFAULT_BODEGA,
    }


def _rows_from_payload(items: list[dict]) -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    errors: list[str] = []
    for idx, raw in enumerate(items or []):
        codigo = (raw.get("codigo") or "").strip().upper()
        cantidad = int(raw.get("cantidad") or 0)
        if not codigo:
            errors.append(f"Ítem {idx + 1}: falta código.")
            continue
        if cantidad <= 0:
            errors.append(f"Ítem {idx + 1}: cantidad inválida.")
            continue
        vn_raw = raw.get("valor_neto")
        valor_neto = float(vn_raw) if vn_raw not in (None, "") else None
        rows.append(
            {
                "codigo": codigo,
                "marca": _normalize_brand(raw.get("marca") or ""),
                "bodega": _normalize_bodega(raw.get("bodega") or DEFAULT_BODEGA),
                "origen_compra": _normalize_origen_compra(raw.get("origen_compra") or ""),
                "cantidad": cantidad,
                "valor_neto": valor_neto,
                "margen_pct": None,
                "precio_venta_neto": None,
                "nota": (raw.get("nota") or "")[:255],
                "codigo_proveedor": (raw.get("codigo_proveedor") or "").strip(),
            }
        )
    if not rows:
        errors.append("Agrega al menos un producto al ingreso.")
    return rows, errors


def registrar_ingreso_rapido(data: dict) -> tuple[bool, dict]:
    if not puede_registrar_ingreso(session.get("user"), session.get("rol")):
        return False, {"message": "No tienes permiso para registrar ingresos."}

    proveedor_id = int(data.get("proveedor_id") or 0)
    proveedor = None
    if proveedor_id > 0:
        proveedor = db.session.get(Proveedor, proveedor_id)
        if proveedor is None or not proveedor.activo:
            return False, {"message": "Proveedor no encontrado."}
    else:
        rut = clean_rut(data.get("proveedor_rut") or "")
        if rut:
            proveedor = _buscar_proveedor_por_rut(rut)

    if proveedor is None:
        return False, {"message": "Selecciona un proveedor válido."}

    rows, row_errors = _rows_from_payload(data.get("items") or [])
    if row_errors:
        return False, {"message": row_errors[0], "errors": row_errors}

    numero_documento = (data.get("numero_documento") or "").strip()[:60]
    fecha_raw = (data.get("fecha_documento") or datetime.now().strftime("%Y-%m-%d")).strip()
    metodo_pago = (data.get("metodo_pago") or "efectivo").strip()
    observacion = (data.get("observacion") or MOBILE_ORIGIN_TAG).strip()[:500]
    total_factura_raw = (data.get("total_factura") or "").strip()

    try:
        fecha_documento = datetime.strptime(fecha_raw, "%Y-%m-%d").date()
    except ValueError:
        return False, {"message": "Fecha de documento inválida."}

    pj = _proveedor_json_ingreso(proveedor)
    supplier_rut = clean_rut(proveedor.rut or "")
    supplier_name = (pj.get("name") or proveedor.empresa or proveedor.nombre or "").strip()[:200]

    try:
        sum_neto_lines = sum(float(r.get("valor_neto") or 0) * int(r.get("cantidad") or 0) for r in rows)
        total_factura_val = None
        iva_factura_val = None
        if total_factura_raw:
            tf = _parse_valor_neto_chile(total_factura_raw)
            if tf is None:
                return False, {"message": "Total factura (c/IVA) inválido."}
            if sum_neto_lines <= 0:
                return False, {"message": "Ingresa valor neto en las líneas para cuadrar con factura."}
            total_factura_val = float(tf)
            iva_factura_val = round(total_factura_val - float(sum_neto_lines), 2)

        documento = IngresoDocumento(
            numero_documento=numero_documento or None,
            fecha_documento=fecha_documento,
            proveedor_id=proveedor.id,
            proveedor_rut=supplier_rut,
            proveedor_nombre=supplier_name,
            proveedor_giro=(pj.get("giro") or "")[:200],
            proveedor_email=(pj.get("email") or "")[:150],
            proveedor_direccion=(pj.get("address") or "")[:300],
            proveedor_comuna=(pj.get("comuna") or "")[:120],
            proveedor_region=(pj.get("region") or "")[:120],
            proveedor_pais=(pj.get("country") or "Chile")[:120],
            observacion=observacion,
            metodo_pago=metodo_pago,
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
            origen_compra = row["origen_compra"]
            cantidad = int(row["cantidad"])

            producto = _producto_por_codigo(codigo)
            if producto is None:
                raise ValueError(f"Producto {codigo} no existe o está inactivo.")

            variante_ing: ProductoVarianteStock | None = None
            if _requiere_variante(codigo, marca):
                if not marca:
                    raise ValueError(f"El producto {codigo} requiere marca.")
                variante_ing = _obtener_o_crear_variante(
                    codigo, marca, bodega, origen_compra=origen_compra, proveedor=supplier_name
                )
                variante_ing.stock = int(variante_ing.stock or 0) + cantidad
                _sincronizar_stock_base_desde_variantes(codigo)
            else:
                stock_anterior = int(producto.get("stock_actual") or 0)
                _actualizar_stock(codigo, stock_anterior + cantidad)

            db.session.add(
                IngresoDocumentoItem(
                    ingreso_documento_id=documento.id,
                    codigo_producto=codigo,
                    descripcion_producto=(producto.get("descripcion") or "")[:255],
                    marca=marca,
                    bodega=bodega,
                    origen_compra=origen_compra,
                    cantidad=cantidad,
                    valor_neto=row.get("valor_neto"),
                    margen_pct=row.get("margen_pct"),
                    precio_venta_neto=row.get("precio_venta_neto"),
                    nota=row.get("nota") or "",
                )
            )
            _propagar_precio_venta_ingreso_a_catalogo(codigo, row.get("precio_venta_neto"), variante_ing)
            _registrar_movimiento(
                codigo,
                "ingreso",
                cantidad,
                f"Doc {documento.id}: {observacion}"[:255],
                proveedor=supplier_name,
                marca=marca or None,
                bodega=bodega,
                origen_compra=origen_compra,
                ingreso_documento_id=documento.id,
            )
            _upsert_mapa_proveedor_codigo(supplier_rut, row.get("codigo_proveedor") or "", codigo)

        db.session.commit()
        return True, {"doc_id": documento.id, "numero": numero_documento or f"#{documento.id}"}
    except Exception as exc:
        db.session.rollback()
        return False, {"message": str(exc)}
