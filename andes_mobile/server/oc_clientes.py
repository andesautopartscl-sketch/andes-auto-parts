"""OC Clientes en mobile — reutiliza modelos y servicios del módulo ERP."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import func, or_
from werkzeug.security import check_password_hash

from app.extensions import db
from app.oc_clientes.models import (
    OC_ESTADOS,
    OC_ESTADO_LABELS,
    OrdenCompraCliente,
    OrdenCompraClienteItem,
    oc_estado_label,
)
from app.oc_clientes.services import (
    buscar_oc_por_numero,
    calcular_totales_items,
    codigo_en_inventario,
    descontar_stock_oc,
    resolver_nombre_vendedor_oc,
    timeline_eventos,
)
from app.seguridad.models import Usuario
from app.utils.permissions import has_permission
from app.utils.rut_utils import format_rut
from app.ventas.models import Cliente
from app.ventas.routes import METODO_PAGO_LABELS, METODO_PAGO_OPTIONS, _client_by_id, _entity_snapshot, _full_address


def puede_ver(user: str | None, rol: str | None) -> bool:
    return has_permission(user, rol, "ver_oc_clientes")


def puede_modificar(user: str | None, rol: str | None) -> bool:
    return has_permission(user, rol, "mod_oc_clientes")


def _estado_badge(estado: str) -> str:
    return {
        "recibida": "recibida",
        "entregada": "entregada",
        "pagada": "pagada",
        "anulada": "anulada",
    }.get((estado or "").strip().lower(), "anulada")


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_date(value: str | None, default: datetime | None = None) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return default
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d")
    except ValueError:
        return default


def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _fmt_monto(valor: float | None) -> str:
    if valor is None:
        return "$0"
    s = "{:,.0f}".format(round(float(valor))).replace(",", ".")
    return f"${s}"


def _fmt_fecha(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y")
    if isinstance(value, date):
        return value.strftime("%d/%m/%Y")
    return "—"


def _dias_desde_entrega(oc: OrdenCompraCliente) -> int | None:
    """Días desde entrega real hasta hoy (solo OC entregadas pendientes de pago)."""
    if (oc.estado or "").strip().lower() != "entregada":
        return None
    fecha_entrega = _as_date(oc.fecha_entrega_real)
    if not fecha_entrega:
        return None
    return max(0, (date.today() - fecha_entrega).days)


def normalizar_items(items: list[dict] | None) -> list[dict]:
    out: list[dict] = []
    for raw in items or []:
        codigo = (raw.get("codigo_producto") or raw.get("codigo") or "").strip().upper()
        desc = (raw.get("descripcion") or "").strip()
        if not codigo and not desc:
            continue
        cant = max(_safe_int(raw.get("cantidad"), 1), 1)
        precio = _safe_float(raw.get("precio_unitario") or raw.get("precio"), 0)
        desc_pct = _safe_float(raw.get("descuento_item") or raw.get("descuento"), 0)
        bruto = cant * precio
        subtotal = round(bruto - bruto * (desc_pct / 100.0), 2) if desc_pct else round(bruto, 2)
        out.append(
            {
                "codigo_producto": codigo,
                "descripcion": desc,
                "marca": (raw.get("marca") or "").strip().upper(),
                "bodega": (raw.get("bodega") or "").strip() or "Bodega 1",
                "cantidad": cant,
                "precio_unitario": precio,
                "descuento_item": desc_pct,
                "subtotal": subtotal,
                "en_inventario": codigo_en_inventario(codigo) if codigo else False,
            }
        )
    return out


def listar_oc(*, estado: str = "", q: str = "", limit: int = 300) -> list[dict]:
    estado_q = (estado or "").strip().lower()
    search_q = (q or "").strip().upper()

    query = OrdenCompraCliente.query
    if estado_q in OC_ESTADOS:
        query = query.filter(OrdenCompraCliente.estado == estado_q)
    if search_q:
        query = query.filter(
            or_(
                func.upper(OrdenCompraCliente.numero_oc).like(f"%{search_q}%"),
                func.upper(OrdenCompraCliente.numero_factura).like(f"%{search_q}%"),
            )
        )

    ordenes = (
        query.order_by(OrdenCompraCliente.fecha_oc.desc(), OrdenCompraCliente.id.desc())
        .limit(max(1, min(limit, 500)))
        .all()
    )
    clientes_map: dict[int, Cliente] = {}
    if ordenes:
        cids = {o.cliente_id for o in ordenes if o.cliente_id}
        if cids:
            for c in Cliente.query.filter(Cliente.id.in_(cids)).all():
                clientes_map[c.id] = c

    filas = []
    for oc in ordenes:
        cl = clientes_map.get(oc.cliente_id)
        filas.append(
            {
                "id": oc.id,
                "numero_oc": oc.numero_oc or "",
                "cliente_nombre": cl.nombre if cl else "—",
                "fecha_oc_fmt": _fmt_fecha(oc.fecha_oc),
                "total_fmt": _fmt_monto(oc.total),
                "estado": oc.estado or "",
                "estado_label": oc_estado_label(oc.estado),
                "badge": _estado_badge(oc.estado),
                "dias_desde_entrega": _dias_desde_entrega(oc),
            }
        )
    return filas


def detalle_oc(oid: int) -> dict | None:
    oc = db.session.get(OrdenCompraCliente, oid)
    if oc is None:
        return None

    cliente = db.session.get(Cliente, oc.cliente_id) if oc.cliente_id else None
    party = _entity_snapshot(cliente, False) if cliente else {}
    items = []
    for it in oc.items:
        items.append(
            {
                "id": it.id,
                "codigo_producto": it.codigo_producto or "",
                "descripcion": it.descripcion or "",
                "marca": it.marca or "",
                "bodega": it.bodega or "",
                "cantidad": it.cantidad or 0,
                "precio_unitario": it.precio_unitario or 0,
                "precio_fmt": _fmt_monto(it.precio_unitario),
                "subtotal": it.subtotal or 0,
                "subtotal_fmt": _fmt_monto(it.subtotal),
                "en_inventario": bool(it.en_inventario),
                "stock_descontado": bool(it.stock_descontado),
            }
        )

    metodo = (oc.metodo_pago or "").strip()
    return {
        "id": oc.id,
        "numero_oc": oc.numero_oc or "",
        "estado": oc.estado or "",
        "estado_label": oc_estado_label(oc.estado),
        "badge": _estado_badge(oc.estado),
        "fecha_oc_fmt": _fmt_fecha(oc.fecha_oc),
        "fecha_entrega_comprometida_fmt": _fmt_fecha(oc.fecha_entrega_comprometida),
        "fecha_entrega_real_fmt": _fmt_fecha(oc.fecha_entrega_real),
        "forma_pago": oc.forma_pago or "",
        "vendedor": oc.vendedor or "",
        "direccion_despacho": oc.direccion_despacho or "",
        "observaciones": oc.observaciones or "",
        "numero_guia_despacho": oc.numero_guia_despacho or "",
        "numero_factura": oc.numero_factura or "",
        "fecha_pago_fmt": _fmt_fecha(oc.fecha_pago),
        "metodo_pago": metodo,
        "metodo_pago_label": METODO_PAGO_LABELS.get(metodo, metodo.replace("_", " ").title() if metodo else "—"),
        "neto_fmt": _fmt_monto(oc.neto),
        "iva_fmt": _fmt_monto(oc.iva),
        "total_fmt": _fmt_monto(oc.total),
        "stock_deducted": bool(oc.stock_deducted),
        "cliente_id": oc.cliente_id,
        "cliente_nombre": party.get("name") or (cliente.nombre if cliente else "—"),
        "cliente_rut": format_rut(party.get("rut") or (cliente.rut if cliente else "")) or "—",
        "items": items,
        "timeline": [
            {
                **ev,
                "titulo": ev.get("label") or "",
                "fecha_fmt": _fmt_fecha(ev.get("fecha")),
            }
            for ev in timeline_eventos(oc)
        ],
        "dias_desde_entrega": _dias_desde_entrega(oc),
    }


def metodos_pago_opciones() -> list[tuple[str, str]]:
    return [(k, METODO_PAGO_LABELS.get(k, k)) for k in METODO_PAGO_OPTIONS if k != "saldo_favor"]


def crear_oc(data: dict, usuario: str) -> tuple[bool, int | None, list[str]]:
    now = datetime.now()
    numero_oc = (data.get("numero_oc") or "").strip()
    cliente_id = _safe_int(data.get("cliente_id"))
    items = normalizar_items(data.get("items"))
    totals = calcular_totales_items(items)

    errors: list[str] = []
    if not numero_oc:
        errors.append("El número de OC es obligatorio.")
    else:
        dup = buscar_oc_por_numero(numero_oc)
        if dup:
            errors.append(f"Ya existe una OC con el número {numero_oc}.")
    if cliente_id <= 0 or _client_by_id(cliente_id) is None:
        errors.append("Debe seleccionar un cliente válido.")
    if not items:
        errors.append("Debe agregar al menos un ítem.")
    if errors:
        return False, None, errors

    try:
        cliente = _client_by_id(cliente_id)
        oc = OrdenCompraCliente(
            numero_oc=numero_oc[:100],
            cliente_id=cliente.id,
            fecha_oc=_parse_date(data.get("fecha_oc"), now) or now,
            fecha_entrega_comprometida=_parse_date(data.get("fecha_entrega_comprometida")),
            forma_pago=((data.get("forma_pago") or "").strip()[:100] or None),
            vendedor=resolver_nombre_vendedor_oc(data.get("vendedor")),
            direccion_despacho=((data.get("direccion_despacho") or "").strip()[:300] or None),
            observaciones=(data.get("observaciones") or "").strip() or None,
            estado="recibida",
            neto=totals["neto"],
            iva=totals["iva"],
            total=totals["total"],
            usuario=usuario or "sistema",
        )
        db.session.add(oc)
        db.session.flush()
        for it in items:
            oc.items.append(
                OrdenCompraClienteItem(
                    codigo_producto=it["codigo_producto"],
                    descripcion=it["descripcion"],
                    marca=it["marca"] or None,
                    bodega=it["bodega"],
                    cantidad=it["cantidad"],
                    precio_unitario=it["precio_unitario"],
                    descuento_item=it["descuento_item"],
                    subtotal=it["subtotal"],
                    en_inventario=it["en_inventario"],
                )
            )
        db.session.commit()
        return True, oc.id, []
    except Exception as exc:
        db.session.rollback()
        return False, None, [f"No se pudo guardar: {exc}"]


def marcar_entregada(
    oid: int,
    *,
    fecha_entrega_real: str | None,
    numero_guia_despacho: str | None,
    descontar_stock: bool,
    usuario: str,
) -> tuple[bool, str]:
    oc = db.session.get(OrdenCompraCliente, oid)
    if oc is None:
        return False, "Orden no encontrada."
    if (oc.estado or "") != "recibida":
        return False, "Solo se puede marcar entregada una OC en estado recibida."

    fecha_entrega = _parse_date(fecha_entrega_real, datetime.now()) or datetime.now()
    guia = (numero_guia_despacho or "").strip()[:60]

    try:
        msg_extra = ""
        if descontar_stock:
            n_desc, n_omit, errors = descontar_stock_oc(oc, usuario)
            if errors:
                raise ValueError("; ".join(errors))
            msg_extra = f" Stock descontado en {n_desc} ítem(s); {n_omit} omitido(s)."
        oc.estado = "entregada"
        oc.fecha_entrega_real = fecha_entrega
        oc.numero_guia_despacho = guia or None
        oc.updated_at = datetime.utcnow()
        db.session.commit()
        return True, f"Entrega registrada.{msg_extra}"
    except Exception as exc:
        db.session.rollback()
        return False, f"No se pudo registrar la entrega: {exc}"


def registrar_pago(
    oid: int,
    *,
    numero_factura: str,
    fecha_pago: str | None,
    metodo_pago: str,
) -> tuple[bool, str]:
    oc = db.session.get(OrdenCompraCliente, oid)
    if oc is None:
        return False, "Orden no encontrada."
    if (oc.estado or "") != "entregada":
        return False, "Solo se puede registrar pago desde estado entregada."

    factura = (numero_factura or "").strip()
    metodo = (metodo_pago or "").strip().lower()
    if not factura:
        return False, "El número de factura es obligatorio."
    if metodo not in METODO_PAGO_OPTIONS:
        return False, "Método de pago inválido."

    try:
        oc.estado = "pagada"
        oc.numero_factura = factura[:60]
        oc.fecha_pago = _parse_date(fecha_pago, datetime.now()) or datetime.now()
        oc.metodo_pago = metodo
        oc.updated_at = datetime.utcnow()
        db.session.commit()
        return True, "Pago registrado correctamente."
    except Exception as exc:
        db.session.rollback()
        return False, f"No se pudo registrar el pago: {exc}"


def _validar_autorizacion_anulacion(username: str, password: str) -> tuple[bool, str]:
    user_name = (username or "").strip()
    raw_pass = password or ""
    if not user_name or not raw_pass:
        return False, "Debe ingresar usuario y contraseña para autorizar la anulación."

    u = Usuario.query.filter_by(usuario=user_name).first()
    if u is None:
        return False, "Usuario de autorización no válido."
    if not bool(u.activo):
        return False, "El usuario de autorización está inactivo."
    if bool(getattr(u, "bloqueado_seguridad", False)):
        return False, "El usuario de autorización está bloqueado."

    try:
        ok = check_password_hash(u.password_hash or "", raw_pass)
    except Exception:
        ok = False
    if not ok:
        return False, "Contraseña de autorización incorrecta."

    rol_name = (u.rol.nombre if getattr(u, "rol", None) and u.rol.nombre else "") or ""
    if not has_permission(u.usuario, rol_name, "mod_oc_clientes"):
        return False, "El usuario no tiene permiso para anular OC de clientes."
    return True, ""


def anular_oc(oid: int, *, auth_user: str, auth_password: str) -> tuple[bool, str]:
    oc = db.session.get(OrdenCompraCliente, oid)
    if oc is None:
        return False, "Orden no encontrada."
    if (oc.estado or "") != "recibida":
        return False, "Solo se pueden anular OC en estado recibida."

    auth_ok, auth_err = _validar_autorizacion_anulacion(auth_user, auth_password)
    if not auth_ok:
        return False, auth_err

    try:
        oc.estado = "anulada"
        oc.updated_at = datetime.utcnow()
        db.session.commit()
        return True, "Orden de compra anulada."
    except Exception as exc:
        db.session.rollback()
        return False, f"No se pudo anular: {exc}"


def cliente_party(cliente_id: int) -> dict:
    cliente = _client_by_id(cliente_id) if cliente_id > 0 else None
    if not cliente:
        return {"id": 0, "nombre": "", "rut": "", "direccion": ""}
    snap = _entity_snapshot(cliente, False)
    return {
        "id": cliente.id,
        "nombre": snap.get("name") or cliente.nombre or "",
        "rut": format_rut(snap.get("rut") or cliente.rut or "") or "",
        "direccion": _full_address(cliente) or snap.get("address") or "",
    }
