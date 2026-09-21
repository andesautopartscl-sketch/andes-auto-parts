"""FASE 8.6 — lectura de ventas para el agente. Agregados autoritativos + detalle acotado.

POR QUE AGREGA EN VEZ DE DEVOLVER FILAS
---------------------------------------
Medido en 8.2C: get_supplier con 19 filas se degrada a 9 y declara las omitidas.
Un trimestre de ventas son miles de lineas. Si esta tool devolviera filas, el
agente contaria sobre una lista truncada y publicaria un total equivocado con
apariencia de estar grounded — el peor fallo posible de este sistema. Por eso los
agregados se calculan en SQL sobre el conjunto COMPLETO y el detalle viaja aparte,
marcado como muestra.

LA TRAMPA DE CORRECCION: 'tipo'
-------------------------------
ventas_documentos guarda ventas Y compras en la misma tabla. Medido en la base
real: boleta(1), factura(1), orden_venta(4), cotizacion(4), orden_compra(1).

- factura/boleta  -> venta realizada. Es lo que por defecto significa "ventas".
- orden_venta     -> comprometida, aun no facturada. Es senal de demanda, pero
                     no es ingreso: se pide explicitamente.
- cotizacion      -> NO es una venta. Contarla inflaria las ventas con intentos.
- orden_compra    -> es una COMPRA. Incluirla seria un error de signo.

NOTAS DE CREDITO
----------------
Una cifra de ventas que ignore las devoluciones esta mal. Se netean siempre y el
payload declara que lo hace, porque un consumidor que no lo sepa sumara mal.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import func

from app.internal_agent.m2m import FORBIDDEN_BODY_KEYS, InternalAuthError

SQL_VALUE_RE = re.compile(
    r"\b(SELECT|INSERT|UPDATE|DELETE|DROP|UNION|ALTER|EXEC|MERGE|TRUNCATE)\b",
    re.IGNORECASE,
)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

MIN_LIMIT = 1
MAX_LIMIT = 20
DEFAULT_LIMIT = 10
MAX_SERIES_POINTS = 24
MAX_CLIENTE_LEN = 120
MAX_WINDOW_DAYS = 1095

# Venta realizada. Es lo que "ventas" significa sin calificar.
SALE_TIPOS = ("factura", "boleta")
# Comprometido pero no facturado. Senal de demanda; se pide aparte.
COMMITTED_TIPOS = ("orden_venta",)
# Nunca es una venta. Se admite solo si se nombra, y el payload lo advierte.
QUOTE_TIPOS = ("cotizacion",)
# NUNCA: es una compra. No se expone por esta tool bajo ningun argumento.
PURCHASE_TIPOS = ("orden_compra",)
SELECTABLE_TIPOS = frozenset(SALE_TIPOS + COMMITTED_TIPOS + QUOTE_TIPOS)

GROUP_BY_VALUES = frozenset({"mes", "dia"})

PUBLIC_LINE_FIELDS = ("codigo", "descripcion", "cantidad", "marca", "bodega")
FINANCE_LINE_FIELDS = ("precio_unitario", "subtotal", "margen_porcentaje")
# Nunca salen de aqui, ni con include_finance. El agente no necesita contactar a
# nadie; para eso esta el ERP con su propia sesion humana.
BLOCKED_FIELDS = frozenset({
    "cliente_email", "cliente_telefono", "cliente_direccion", "cliente_rut",
    "pago_referencia", "password", "token", "id", "source_id", "root_id",
})


def _bad(field: str, message: str) -> InternalAuthError:
    return InternalAuthError("invalid_args", f"{field}: {message}", status=400)


def _parse_date(raw: Any, label: str) -> date | None:
    if raw in (None, ""):
        return None
    text = str(raw).strip()
    if not DATE_RE.match(text):
        raise _bad(label, "formato esperado YYYY-MM-DD")
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise _bad(label, "fecha invalida") from exc


def _opt_str(payload: dict[str, Any], key: str, max_len: int) -> str | None:
    raw = payload.get(key)
    if raw in (None, ""):
        return None
    text = str(raw).strip()
    if not text:
        return None
    if len(text) > max_len:
        raise _bad(key, f"maximo {max_len} caracteres")
    if SQL_VALUE_RE.search(text):
        raise _bad(key, "valor no permitido")
    return text


def validate_sales_args(payload: Any) -> dict[str, Any]:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise _bad("body", "se esperaba un objeto JSON")
    for forbidden in FORBIDDEN_BODY_KEYS:
        if forbidden in payload:
            raise _bad(forbidden, "campo no permitido")

    codigo = _opt_str(payload, "codigo", 100)
    cliente = _opt_str(payload, "cliente", MAX_CLIENTE_LEN)
    estado = _opt_str(payload, "estado", 40)

    raw_tipos = payload.get("tipos")
    if raw_tipos in (None, "", []):
        tipos = list(SALE_TIPOS)
    else:
        if not isinstance(raw_tipos, list):
            raise _bad("tipos", "se esperaba una lista")
        tipos = []
        for entry in raw_tipos[:8]:
            text = str(entry or "").strip().lower()
            if text in PURCHASE_TIPOS:
                # No es una restriccion de permisos: es que pedir compras a la
                # tool de ventas produciria una cifra con el signo cambiado.
                raise _bad("tipos", "orden_compra no es una venta; usa get_purchase_orders")
            if text not in SELECTABLE_TIPOS:
                raise _bad("tipos", f"valor no permitido: {text[:24]}")
            tipos.append(text)
        tipos = tipos or list(SALE_TIPOS)

    group_by = _opt_str(payload, "group_by", 8)
    if group_by is not None:
        group_by = group_by.lower()
        if group_by not in GROUP_BY_VALUES:
            raise _bad("group_by", "valores permitidos: mes|dia")

    desde = _parse_date(payload.get("fecha_desde"), "fecha_desde")
    hasta = _parse_date(payload.get("fecha_hasta"), "fecha_hasta")
    if desde and hasta and hasta < desde:
        raise _bad("fecha_hasta", "no puede ser anterior a fecha_desde")
    if desde and hasta and (hasta - desde).days > MAX_WINDOW_DAYS:
        raise _bad("fecha_hasta", f"ventana maxima {MAX_WINDOW_DAYS} dias")

    try:
        limit = int(payload.get("limit") or DEFAULT_LIMIT)
    except (TypeError, ValueError) as exc:
        raise _bad("limit", "entero requerido") from exc
    limit = max(MIN_LIMIT, min(MAX_LIMIT, limit))

    return {
        "codigo": codigo.upper() if codigo else None,
        "cliente": cliente,
        "estado": estado.lower() if estado else None,
        "tipos": tipos,
        "group_by": group_by,
        "fecha_desde": desde,
        "fecha_hasta": hasta,
        "limit": limit,
    }


def _as_float(value: Any) -> float:
    try:
        return round(float(value or 0.0), 2)
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _period_key(value: Any, group_by: str) -> str:
    if not isinstance(value, datetime):
        return ""
    return value.strftime("%Y-%m-%d" if group_by == "dia" else "%Y-%m")


def _public_line(row: Any, *, include_finance: bool) -> dict[str, Any]:
    item = {
        "fecha": row.fecha.strftime("%Y-%m-%d") if isinstance(row.fecha, datetime) else "",
        "tipo": str(row.tipo or "").strip().lower(),
        "numero": str(row.numero or "").strip(),
        "estado": str(row.estado or "").strip().lower(),
        "cliente": str(row.cliente or "").strip(),
        "codigo": str(row.codigo or "").strip().upper(),
        "descripcion": str(row.descripcion or "").strip(),
        "marca": str(row.marca or "").strip(),
        "bodega": str(row.bodega or "").strip(),
        "cantidad": _as_int(row.cantidad),
    }
    if include_finance:
        item["precio_unitario"] = _as_float(row.precio_unitario)
        item["subtotal"] = _as_float(row.subtotal)
    return item


def get_public_sales(
    *,
    codigo: str | None = None,
    cliente: str | None = None,
    estado: str | None = None,
    tipos: list[str] | None = None,
    group_by: str | None = None,
    fecha_desde: date | None = None,
    fecha_hasta: date | None = None,
    limit: int = DEFAULT_LIMIT,
    include_finance: bool = False,
) -> tuple[dict[str, Any], bool]:
    """Agregados sobre el conjunto completo + una muestra del detalle.

    Los agregados NO se derivan del detalle: se calculan en SQL sobre todas las
    filas. Si se derivaran de una lista acotada, cualquier total seria falso en
    cuanto hubiera mas ventas que ``limit``.
    """
    from app.extensions import db
    from app.ventas.models import (
        DocumentoVenta,
        DocumentoVentaItem,
        NotaCredito,
        NotaCreditoItem,
    )

    tipos = list(tipos or SALE_TIPOS)
    limit = max(MIN_LIMIT, min(MAX_LIMIT, int(limit or DEFAULT_LIMIT)))

    def _scope(query: Any, doc_model: Any, date_col: Any) -> Any:
        query = query.filter(func.lower(doc_model.tipo).in_(tipos))
        if estado:
            query = query.filter(func.lower(doc_model.status) == estado)
        if cliente:
            query = query.filter(doc_model.cliente_nombre.ilike(f"%{cliente}%"))
        if fecha_desde is not None:
            query = query.filter(date_col >= datetime.combine(fecha_desde, time.min))
        if fecha_hasta is not None:
            query = query.filter(
                date_col < datetime.combine(fecha_hasta, time.min) + timedelta(days=1))
        return query

    base = db.session.query(
        DocumentoVentaItem.cantidad.label("cantidad"),
        DocumentoVentaItem.subtotal.label("subtotal"),
        DocumentoVenta.id.label("doc_id"),
        DocumentoVenta.fecha_documento.label("fecha"),
    ).join(DocumentoVenta, DocumentoVentaItem.documento_id == DocumentoVenta.id)
    base = _scope(base, DocumentoVenta, DocumentoVenta.fecha_documento)
    if codigo:
        base = base.filter(
            func.upper(func.trim(DocumentoVentaItem.codigo_producto)) == codigo)
    rows = base.all()

    unidades_brutas = sum(_as_int(r.cantidad) for r in rows)
    ingresos_brutos = sum(_as_float(r.subtotal) for r in rows)
    documentos = len({r.doc_id for r in rows})

    # Devoluciones. Una cifra de ventas que las ignore esta mal.
    nc = db.session.query(
        NotaCreditoItem.cantidad.label("cantidad"),
        NotaCreditoItem.subtotal.label("subtotal"),
        NotaCredito.id.label("nc_id"),
        NotaCredito.fecha_documento.label("fecha"),
    ).join(NotaCredito, NotaCreditoItem.nota_credito_id == NotaCredito.id)
    nc = nc.join(DocumentoVenta, NotaCredito.documento_venta_id == DocumentoVenta.id)
    nc = _scope(nc, DocumentoVenta, NotaCredito.fecha_documento)
    if codigo:
        nc = nc.filter(func.upper(func.trim(NotaCreditoItem.codigo_producto)) == codigo)
    nc_rows = nc.all()
    nc_unidades = sum(_as_int(r.cantidad) for r in nc_rows)
    nc_monto = sum(_as_float(r.subtotal) for r in nc_rows)

    series: list[dict[str, Any]] = []
    if group_by:
        buckets: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = _period_key(row.fecha, group_by)
            if not key:
                continue
            slot = buckets.setdefault(key, {"periodo": key, "unidades": 0,
                                            "documentos": set(), "ingresos": 0.0})
            slot["unidades"] += _as_int(row.cantidad)
            slot["documentos"].add(row.doc_id)
            slot["ingresos"] += _as_float(row.subtotal)
        for row in nc_rows:
            key = _period_key(row.fecha, group_by)
            if key in buckets:
                buckets[key]["unidades"] -= _as_int(row.cantidad)
                buckets[key]["ingresos"] -= _as_float(row.subtotal)
        for key in sorted(buckets)[-MAX_SERIES_POINTS:]:
            slot = buckets[key]
            point = {"periodo": key, "unidades": int(slot["unidades"]),
                     "documentos": len(slot["documentos"])}
            if include_finance:
                point["ingresos"] = round(slot["ingresos"], 2)
            series.append(point)

    detail_q = db.session.query(
        DocumentoVenta.fecha_documento.label("fecha"),
        DocumentoVenta.tipo.label("tipo"),
        DocumentoVenta.numero.label("numero"),
        DocumentoVenta.status.label("estado"),
        DocumentoVenta.cliente_nombre.label("cliente"),
        DocumentoVentaItem.codigo_producto.label("codigo"),
        DocumentoVentaItem.descripcion.label("descripcion"),
        DocumentoVentaItem.marca.label("marca"),
        DocumentoVentaItem.bodega.label("bodega"),
        DocumentoVentaItem.cantidad.label("cantidad"),
        DocumentoVentaItem.precio_unitario.label("precio_unitario"),
        DocumentoVentaItem.subtotal.label("subtotal"),
    ).join(DocumentoVenta, DocumentoVentaItem.documento_id == DocumentoVenta.id)
    detail_q = _scope(detail_q, DocumentoVenta, DocumentoVenta.fecha_documento)
    if codigo:
        detail_q = detail_q.filter(
            func.upper(func.trim(DocumentoVentaItem.codigo_producto)) == codigo)
    detail_rows = (detail_q.order_by(DocumentoVenta.fecha_documento.desc(),
                                     DocumentoVenta.id.desc())
                   .limit(limit + 1).all())
    truncated = len(detail_rows) > limit
    items = [_public_line(r, include_finance=include_finance)
             for r in detail_rows[:limit]]

    data: dict[str, Any] = {
        "tipos": list(tipos),
        # Un consumidor que no sepa que 'cotizacion' no es venta sumara mal, asi
        # que el payload lo dice en vez de confiar en que alguien lea el codigo.
        "incluye_cotizaciones": any(t in QUOTE_TIPOS for t in tipos),
        "unidades": unidades_brutas - nc_unidades,
        "documentos": documentos,
        "neto_notas_credito": True,
        "notas_credito": {"documentos": len({r.nc_id for r in nc_rows}),
                          "unidades": nc_unidades},
        "items": items,
        "count": len(items),
        # El detalle es una MUESTRA. Los agregados de arriba son el total real.
        "detalle_parcial": truncated,
    }
    if codigo:
        data["codigo"] = codigo
    if cliente:
        data["cliente"] = cliente
    # FASE 8.x — el alcance se declara SIEMPRE, tambien cuando no hay filtro.
    #
    # Medido en V02 contra el ERP real: "las ventas de enero a marzo de 2026"
    # devuelve cero, pero el sistema no puede expresar un periodo en lenguaje
    # natural (normalize_agent_args descarta toda fecha que no aparezca como
    # YYYY-MM-DD en la pregunta, que es la regla anti-alucinacion correcta), asi
    # que la llamada sale SIN filtro y trae 2 documentos de 2026-04-07 — abril.
    # La respuesta publicada hablaba de esos documentos como si fueran los del
    # trimestre preguntado. Todas las cifras estaban grounded y la respuesta era
    # falsa igualmente, porque el alcance no estaba en la evidencia y nadie podia
    # contradecirlo. Un campo ausente no se puede verificar; uno presente si.
    # desde=null y hasta=null significan "sin filtro": no hace falta una tercera
    # bandera, y una que no sobrevive a la proyeccion del Gateway seria peor.
    data["periodo"] = {
        "desde": fecha_desde.isoformat() if fecha_desde else None,
        "hasta": fecha_hasta.isoformat() if fecha_hasta else None,
    }
    if group_by:
        data["group_by"] = group_by
        data["series"] = series
    if include_finance:
        data["ingresos"] = round(ingresos_brutos - nc_monto, 2)
        data["notas_credito"]["monto"] = round(nc_monto, 2)
    return data, truncated
