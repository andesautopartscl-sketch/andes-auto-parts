"""Importar imágenes mobile — reutiliza subida Cloudinary del módulo Productos."""
from __future__ import annotations

from flask import jsonify, request

from app.models import SessionDB
from app.productos.routes import (
    _actor_usuario,
    _procesar_subida_imagen_cloudinary,
)
from app.utils.cloudinary_config import is_configured as cloudinary_is_configured
from app.utils.cloudinary_product_import import (
    normalize_tipo_imagen,
    resolver_producto_por_codigo,
    search_productos_for_assign,
)
from app.utils.permissions import get_user_permissions
from app.utils.product_audit import register_product_audit

TIPO_OPCIONES = [
    {"value": "producto", "label": "Producto"},
    {"value": "360", "label": "360°"},
    {"value": "despiece", "label": "Despiece"},
    {"value": "oem", "label": "OEM"},
]


def puede_importar_imagenes(user: str | None, rol: str | None) -> bool:
    return bool(
        get_user_permissions(user, rol).get("productos_crear_editar", False)
    )


def _deny_json():
    return jsonify({"success": False, "ok": False, "error": "Permiso denegado"}), 403


def buscar_productos(q: str, limit: int = 12) -> list[dict]:
    term = (q or "").strip()
    if len(term) < 1:
        return []
    sess = SessionDB()
    try:
        return search_productos_for_assign(sess, term, limit=limit)
    finally:
        sess.close()


def resolver_codigo(codigo: str) -> dict:
    cod = (codigo or "").strip().upper()
    if not cod:
        return {"success": True, "found": False}
    sess = SessionDB()
    try:
        info = resolver_producto_por_codigo(sess, cod)
        return {"success": True, **info}
    finally:
        sess.close()


def subir_imagen(file_obj, *, codigo: str, archivo_nombre: str, tipo_imagen: str) -> dict:
    if not cloudinary_is_configured():
        return {
            "ok": False,
            "success": False,
            "error": "Cloudinary no está configurado.",
            "estado": "error",
        }
    tipo = normalize_tipo_imagen(tipo_imagen if tipo_imagen != "oem" else "producto")
    cod = (codigo or "").strip().upper()
    fname = (archivo_nombre or "").strip() or "imagen.jpg"
    sess = SessionDB()
    try:
        row = _procesar_subida_imagen_cloudinary(
            sess,
            file_obj,
            codigo_asignado=cod,
            archivo_nombre=fname,
            tipo_imagen=tipo,
        )
        if row.get("estado") == "vinculado":
            register_product_audit(
                sess,
                actor=_actor_usuario(),
                producto_codigo=row.get("producto_codigo"),
                action="update",
                modulo="productos",
                req=request,
                metadata={
                    "cloudinary_image_upload": True,
                    "archivo": fname[:120],
                    "tipo_imagen": tipo,
                    "mobile": True,
                },
            )
        sess.commit()
        ok = row.get("estado") != "error"
        return {"ok": ok, "success": ok, **row}
    except Exception as exc:
        sess.rollback()
        return {
            "ok": False,
            "success": False,
            "error": str(exc),
            "mensaje": str(exc),
            "estado": "error",
        }
    finally:
        sess.close()
