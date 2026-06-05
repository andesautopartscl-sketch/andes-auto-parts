"""Ajuste de stock mobile: reutiliza movimientos del módulo Bodega."""
from __future__ import annotations

from flask import session

from app.bodega.routes import (
    DEFAULT_BODEGA,
    _aplicar_movimiento_stock,
    _aplicar_movimiento_variante,
    _bodegas_para_select,
    _normalize_bodega,
    _normalize_brand,
    _parse_int,
    _producto_por_codigo,
    _requiere_variante,
    _stock_contexto_para_ajuste,
    _stock_variantes_por_codigo,
)
from app.extensions import db
from app.utils.permissions import has_permission

from .venta_rapida import MOBILE_ORIGIN_TAG

TIPOS_MOVIMIENTO = ("ingreso", "salida", "ajuste")


def _permiso_para_tipo(tipo: str, user: str | None, rol: str | None) -> str | None:
    t = (tipo or "").strip().lower()
    if t == "ingreso":
        if not has_permission(user, rol, "bodega_ingreso"):
            return "Sin permiso para registrar ingresos de stock."
    elif t == "salida":
        if not has_permission(user, rol, "bodega_salida"):
            return "Sin permiso para registrar salidas de stock."
    elif t == "ajuste":
        if not has_permission(user, rol, "bodega_ajuste"):
            return "Sin permiso para registrar ajustes de stock."
    else:
        return "Tipo de movimiento no válido."
    if not has_permission(user, rol, "mod_bodega"):
        return "Sin permiso para el módulo Bodega."
    return None


def stock_ajuste_contexto(codigo_raw: str) -> dict | None:
    codigo = (codigo_raw or "").strip().upper()
    if not codigo:
        return None
    producto = _producto_por_codigo(codigo)
    if producto is None:
        return None
    variantes = _stock_variantes_por_codigo(codigo)
    bodegas = _bodegas_para_select()
    lineas: list[dict] = []
    if variantes:
        for v in variantes:
            bodega = (v.get("bodega") or DEFAULT_BODEGA).strip()
            marca = (v.get("marca") or "").strip()
            stock = int(v.get("stock") or 0)
            lineas.append(
                {
                    "bodega": bodega,
                    "marca": marca,
                    "stock": stock,
                    "key": f"{bodega}|{marca}",
                }
            )
    else:
        stock_base = int(producto.get("stock_actual") or 0)
        lineas.append(
            {
                "bodega": DEFAULT_BODEGA,
                "marca": (producto.get("marca") or "").strip(),
                "stock": stock_base,
                "key": f"{DEFAULT_BODEGA}|",
            }
        )
    return {
        "codigo": codigo,
        "descripcion": (producto.get("descripcion") or "").strip(),
        "requiere_variante": bool(variantes),
        "bodegas": bodegas,
        "lineas": lineas,
        "stock_total": sum(int(l["stock"]) for l in lineas),
    }


def registrar_ajuste_stock(payload: dict) -> tuple[bool, dict]:
    user = session.get("user")
    rol = session.get("rol")
    codigo = (payload.get("codigo") or "").strip().upper()
    tipo = (payload.get("tipo") or "").strip().lower()
    bodega = _normalize_bodega((payload.get("bodega") or "").strip() or DEFAULT_BODEGA)
    marca = _normalize_brand((payload.get("marca") or "").strip())
    motivo = (payload.get("motivo") or "").strip()
    cantidad = _parse_int(str(payload.get("cantidad") or "").strip())

    perm_err = _permiso_para_tipo(tipo, user, rol)
    if perm_err:
        return False, {"message": perm_err}
    if not codigo:
        return False, {"message": "Código de producto obligatorio."}
    if not motivo:
        return False, {"message": "El motivo u observación es obligatorio."}
    if cantidad is None or cantidad <= 0:
        return False, {"message": "La cantidad debe ser un entero mayor a 0."}

    producto = _producto_por_codigo(codigo)
    if producto is None:
        return False, {"message": "El producto no existe o está inactivo."}

    variantes = _stock_variantes_por_codigo(codigo)
    observacion = f"{MOBILE_ORIGIN_TAG} {motivo}"[:255]

    try:
        if _requiere_variante(codigo, marca):
            if not marca:
                return False, {"message": "Este código trabaja por variantes. Indica la marca."}
            stock_actual = _stock_contexto_para_ajuste(
                codigo, marca, bodega, producto, variantes
            )
            if tipo == "salida":
                if cantidad > stock_actual:
                    return False, {
                        "message": f"Stock insuficiente en esa variante. Disponible: {stock_actual}.",
                    }
                nuevo = _aplicar_movimiento_variante(
                    codigo,
                    "salida",
                    -cantidad,
                    observacion,
                    marca=marca,
                    bodega=bodega,
                    commit=False,
                )
            elif tipo == "ingreso":
                nuevo = _aplicar_movimiento_variante(
                    codigo,
                    "ingreso",
                    cantidad,
                    f"{observacion}. Proveedor: Ajuste mobile",
                    marca=marca,
                    bodega=bodega,
                    proveedor="Ajuste mobile",
                    commit=False,
                )
            else:
                nuevo_stock_objetivo = cantidad
                if nuevo_stock_objetivo == stock_actual:
                    return False, {"message": "El stock objetivo es igual al actual."}
                delta = nuevo_stock_objetivo - stock_actual
                observacion_ajuste = (
                    f"{observacion}. Variante {marca} / {bodega} {stock_actual} -> {nuevo_stock_objetivo}"
                )
                nuevo = _aplicar_movimiento_variante(
                    codigo,
                    "ajuste",
                    delta,
                    observacion_ajuste[:255],
                    marca=marca,
                    bodega=bodega,
                    nuevo_stock_variante=nuevo_stock_objetivo,
                    commit=False,
                )
        else:
            stock_anterior = int(producto.get("stock_actual") or 0)
            if tipo == "salida":
                if cantidad > stock_anterior:
                    return False, {
                        "message": f"No puedes dejar stock negativo. Disponible: {stock_anterior}.",
                    }
                nuevo = stock_anterior - cantidad
                _aplicar_movimiento_stock(codigo, "salida", -cantidad, nuevo, observacion)
                db.session.commit()
                return True, {
                    "message": "Salida registrada.",
                    "stock_nuevo": nuevo,
                    "codigo": codigo,
                }
            if tipo == "ingreso":
                nuevo = stock_anterior + cantidad
                _aplicar_movimiento_stock(
                    codigo,
                    "ingreso",
                    cantidad,
                    nuevo,
                    f"{observacion}. Proveedor: Ajuste mobile",
                    proveedor="Ajuste mobile",
                    bodega=bodega,
                    marca=marca or None,
                )
                db.session.commit()
                return True, {
                    "message": "Ingreso registrado.",
                    "stock_nuevo": nuevo,
                    "codigo": codigo,
                }
            nuevo_stock_objetivo = cantidad
            if nuevo_stock_objetivo == stock_anterior:
                return False, {"message": "El stock objetivo es igual al actual."}
            delta = nuevo_stock_objetivo - stock_anterior
            observacion_ajuste = f"{observacion}. Stock {stock_anterior} -> {nuevo_stock_objetivo}"
            _aplicar_movimiento_stock(
                codigo,
                "ajuste",
                delta,
                nuevo_stock_objetivo,
                observacion_ajuste[:255],
                bodega=bodega,
                marca=marca or None,
            )
            db.session.commit()
            return True, {
                "message": "Ajuste registrado.",
                "stock_nuevo": nuevo_stock_objetivo,
                "codigo": codigo,
            }

        db.session.commit()
        return True, {
            "message": "Movimiento registrado.",
            "stock_nuevo": nuevo,
            "codigo": codigo,
        }
    except Exception as exc:
        db.session.rollback()
        return False, {"message": str(exc)}
