from __future__ import annotations

from app.extensions import db


PERMISSION_CATALOG: list[dict] = [
    {
        "section": "Modulos",
        "items": [
            {"key": "mod_productos", "label": "Ver modulo Productos"},
            {"key": "mod_ventas", "label": "Ver modulo Ventas"},
            {"key": "mod_bodega", "label": "Ver modulo Bodega"},
            {"key": "mod_inventario", "label": "Ver modulo Inventario / Transferencias"},
            {"key": "mod_seguridad", "label": "Ver modulo Seguridad / Usuarios"},
            {"key": "mod_dashboard", "label": "Ver modulo Dashboard"},
            {"key": "mod_informes", "label": "Ver modulo Informes"},
            {"key": "mod_finanzas", "label": "Ver modulo Finanzas / Contabilidad"},
            {"key": "mod_chat", "label": "Ver modulo Chat"},
            {"key": "mod_postventa", "label": "Ver modulo Postventa / Garantías"},
            {"key": "mod_rrhh", "label": "Ver modulo RRHH / Nómina"},
            {"key": "mod_sii_sync", "label": "Ver modulo SII Sync (DTE emitidos)"},
        ],
    },
    {
        "section": "SII Sync",
        "items": [
            {"key": "sii_ver", "label": "Ver documentos sincronizados del SII"},
            {"key": "sii_sincronizar", "label": "Sincronizar periodos desde API SII"},
        ],
    },
    {
        "section": "Ventas",
        "items": [
            {"key": "ventas_guardar_documento", "label": "Guardar / editar documentos de venta"},
            {"key": "ventas_convertir_documento", "label": "Convertir documentos (CO->OV, OV->FA/BO, FA->NC, OC->Ingreso)"},
            {"key": "ventas_enviar_documento", "label": "Enviar documentos por email/WhatsApp"},
            {"key": "ventas_autorizar_margen_bajo", "label": "Autorizar margen bajo en OV"},
            {"key": "ver_precio_costo", "label": "Ver costos y margenes internos"},
            {"key": "ver_oc_clientes", "label": "Ver órdenes de compra de clientes"},
            {"key": "mod_oc_clientes", "label": "Gestionar órdenes de compra de clientes"},
        ],
    },
    {
        "section": "Bodega",
        "items": [
            {"key": "bodega_ingreso", "label": "Registrar ingreso de stock"},
            {"key": "bodega_salida", "label": "Registrar salida de stock"},
            {"key": "bodega_ajuste", "label": "Registrar ajustes de stock"},
            {"key": "bodega_variantes_gestionar", "label": "Gestionar variantes de stock (crear/editar/eliminar con autorización)"},
            {"key": "bodega_picking", "label": "Gestionar picking de venta"},
            {"key": "bodega_etiquetas", "label": "Imprimir y gestionar etiquetas"},
        ],
    },
    {
        "section": "Productos",
        "items": [
            {"key": "productos_crear_editar", "label": "Crear y editar productos"},
            {"key": "productos_desactivar_reactivar", "label": "Desactivar / reactivar productos"},
            {"key": "productos_importar_exportar", "label": "Importar / exportar productos"},
            {"key": "ver_stock", "label": "Ver stock en buscador de etiquetas"},
            {"key": "ver_oem", "label": "Ver código OEM en buscador de etiquetas"},
        ],
    },
    {
        "section": "Seguridad",
        "items": [
            {"key": "seguridad_gestion_usuarios", "label": "Gestionar usuarios (crear/editar/bloquear/eliminar)"},
            {"key": "seguridad_gestion_permisos", "label": "Gestionar permisos por usuario"},
            {"key": "seguridad_reset_password", "label": "Resolver solicitudes de cambio de clave"},
        ],
    },
    {
        "section": "Postventa",
        "items": [
            {"key": "postventa_crear", "label": "Crear garantías"},
            {"key": "postventa_editar_estado", "label": "Cambiar estado de garantías"},
            {"key": "postventa_vincular_nc", "label": "Vincular nota de crédito a garantía"},
            {"key": "postventa_eliminar", "label": "Eliminar garantías"},
        ],
    },
    {
        "section": "Finanzas / Contabilidad",
        "items": [
            {"key": "finanzas_gestion_cuentas", "label": "Crear y activar/inactivar cuentas contables"},
            {"key": "finanzas_registrar_movimientos", "label": "Registrar movimientos contables"},
        ],
    },
    {
        "section": "RRHH / Nómina",
        "items": [
            {"key": "rrhh_ver", "label": "Ver nómina y liquidaciones"},
            {"key": "rrhh_editar", "label": "Generar/cerrar liquidaciones y editar datos RRHH"},
            {"key": "rrhh_pagar", "label": "Registrar pago de liquidaciones"},
        ],
    },
]


def _flatten_catalog_keys() -> list[str]:
    keys: list[str] = []
    for section in PERMISSION_CATALOG:
        for item in section.get("items", []):
            k = (item.get("key") or "").strip()
            if k:
                keys.append(k)
    return keys


ALL_PERMISSION_KEYS = _flatten_catalog_keys()


LEGACY_KEY_MAP = {
    "ver_finanzas": "mod_finanzas",
    "ver_precio_mayor": "ver_precio_costo",
}


DEFAULT_PERMISSIONS = {key: False for key in ALL_PERMISSION_KEYS}
# Compatibilidad con llaves antiguas usadas en código existente.
DEFAULT_PERMISSIONS["ver_finanzas"] = False
DEFAULT_PERMISSIONS["ver_precio_mayor"] = False


def superadmin_permissions() -> dict:
    perms = {key: True for key in ALL_PERMISSION_KEYS}
    perms["ver_finanzas"] = True
    perms["ver_precio_mayor"] = True
    return perms


def has_permission(username: str | None, role_name: str | None, permission_key: str) -> bool:
    if not permission_key:
        return False
    perms = get_user_permissions(username, role_name)
    if permission_key in perms:
        return bool(perms.get(permission_key))
    mapped = LEGACY_KEY_MAP.get(permission_key)
    if mapped:
        return bool(perms.get(mapped))
    return False


def get_user_permissions(username: str | None, role_name: str | None = None) -> dict:
    # Import local para evitar ciclo: seguridad.routes -> decorators -> permissions -> seguridad.models
    from app.seguridad.models import Usuario, UsuarioPermiso, UsuarioPermisoDetalle

    if not username:
        return dict(DEFAULT_PERMISSIONS)

    import time as _time
    try:
        from flask import session as _sess
        _cached = _sess.get("_perms_cache")
        _cached_perms = _cached.get("perms") if isinstance(_cached, dict) else None
        if (
            isinstance(_cached, dict)
            and _cached.get("username") == username
            and _time.time() - _cached.get("_ts", 0) < 60
            and isinstance(_cached_perms, dict)
            # Si se agregaron llaves al catálogo, forzar rebuild (evita SuperAdmin sin ver_stock/ver_oem).
            and all(k in _cached_perms for k in ALL_PERMISSION_KEYS)
        ):
            return _cached_perms
    except RuntimeError:
        pass  # sin contexto de request (startup, seeds)

    role = (role_name or "").strip().lower()
    if role == "superadmin":
        result = superadmin_permissions()
    else:
        try:
            user = db.session.query(Usuario).filter_by(usuario=username).first()
            if user is None:
                result = dict(DEFAULT_PERMISSIONS)
            elif user.rol and (user.rol.nombre or "").strip().lower() == "superadmin":
                result = superadmin_permissions()
            else:
                perms = dict(DEFAULT_PERMISSIONS)
                # Nuevo esquema normalizado.
                rows = db.session.query(UsuarioPermisoDetalle).filter_by(usuario_id=user.id).all()
                if rows:
                    for row in rows:
                        key = (row.permiso_key or "").strip()
                        if key:
                            perms[key] = bool(row.allowed)
                    # Alias legacy para no romper llamadas existentes.
                    perms["ver_finanzas"] = bool(perms.get("mod_finanzas"))
                    perms["ver_precio_mayor"] = bool(perms.get("ver_precio_costo"))
                    result = perms
                else:
                    # Fallback temporal: tabla legacy.
                    perm = db.session.query(UsuarioPermiso).filter_by(usuario_id=user.id).first()
                    if perm is not None:
                        perms["mod_finanzas"] = bool(perm.ver_finanzas)
                        perms["ver_finanzas"] = bool(perm.ver_finanzas)
                        perms["ver_precio_costo"] = bool(perm.ver_precio_mayor)
                        perms["ver_precio_mayor"] = bool(perm.ver_precio_mayor)
                    result = perms
        except Exception:
            # Cualquier fallo de ORM/esquema/datos no debe tumbar la vista (p. ej. Render).
            db.session.rollback()
            result = dict(DEFAULT_PERMISSIONS)

    try:
        from flask import session as _sess
        _sess["_perms_cache"] = {
            "username": username,
            "perms": result,
            "_ts": _time.time(),
        }
        _sess.modified = True
    except (RuntimeError, Exception):
        pass

    return result

