"""FASE 9.2 — ordenes de cliente. El hueco comercial del asistente.

POR QUE ESTA TOOL, MEDIDO EN LA BASE REAL

    oc_clientes          71 ordenes · 93 lineas · 50 pagos      SIN TOOL
       precio_unitario 100% · estado 100% · total 100% · fecha_oc 100%
    ventas_documentos    11 documentos                          get_sales

``get_sales`` lee la tabla con 11 filas. Donde este negocio transacta de verdad
—con precios reales por linea— no miraba nadie. Esa asimetria es la razon de
esta tool, y no es una opinion: son 93 lineas de venta con precio al 100%
contra 14.

DOS DECISIONES DE CORRECCION QUE NO SON DE PERMISOS

1. **`anulada` NO cuenta.** Estados medidos: pagada 65, recibida 4, anulada 2.
   Sumar una orden anulada a los ingresos es el mismo error de clase que contar
   una ``orden_compra`` como venta en 8.6: invierte el signo de la realidad. Se
   excluye por defecto y hay que nombrarla para verla; el payload lo declara.

2. **`recibida` no es `pagada`.** Cuatro ordenes estan recibidas y sin cobrar.
   Un agregado que las mezcle con las cobradas responde otra pregunta. El
   desglose por estado viaja siempre.

POR QUE ALLOWLIST DE SALIDA Y NO BLOCKLIST

``sales.py`` usa ``BLOCKED_FIELDS``, y aqui no sirve. La superficie de datos
personales de una orden es mas densa —``direccion_despacho``, ``vendedor``,
``usuario``, ``referencia_pago``, y el cliente entero colgando de
``ventas_clientes``— y sobre todo incluye ``observaciones``, texto libre donde
cabe cualquier cosa. Una lista de lo prohibido no protege del campo que alguien
anada manana; una lista de lo permitido si. Lo que no esta aqui no sale, y el
dia que ``oc_clientes`` gane una columna, seguira sin salir.

``vendedor`` queda fuera a proposito aunque sea util para gestion: es un dato
personal de un empleado y exponerlo habilita vigilancia de desempeno. Es una
decision revisable, no un olvido.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Any

from app.internal_agent.m2m import InternalAuthError

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SQL_VALUE_RE = re.compile(
    r"(--|;|/\*|\*/|\bunion\b|\bselect\b|\bdrop\b|\bdelete\b|\bupdate\b|\binsert\b)",
    re.IGNORECASE,
)

MIN_LIMIT = 1
MAX_LIMIT = 20
DEFAULT_LIMIT = 10
MAX_TEXT_LEN = 120
MAX_WINDOW_DAYS = 1095
MAX_SERIES_POINTS = 24

# Estados medidos en la base: pagada 65, recibida 4, anulada 2.
ESTADO_COBRADO = "pagada"
ESTADO_ENTREGADO = "recibida"
ESTADO_ANULADO = "anulada"
# Lo que cuenta como orden viva. `anulada` NO esta, y esa ausencia es una regla
# de correccion: hay que pedirla por su nombre para verla.
DEFAULT_ESTADOS = (ESTADO_COBRADO, ESTADO_ENTREGADO)
SELECTABLE_ESTADOS = frozenset(DEFAULT_ESTADOS + (ESTADO_ANULADO,))

GROUP_BY_VALUES = frozenset({"mes", "dia"})

# --- allowlist de salida. Lo que no esta aqui no sale nunca. -----------------
PUBLIC_ORDER_FIELDS = ("numero_oc", "fecha", "estado", "lineas")
FINANCE_ORDER_FIELDS = ("neto", "iva", "total")
PUBLIC_LINE_FIELDS = ("codigo", "descripcion", "marca", "cantidad")
FINANCE_LINE_FIELDS = ("precio_unitario", "subtotal")

# Columnas reales que NUNCA se proyectan, escritas para que la decision sea
# legible y el dia que alguien las eche de menos encuentre el motivo aqui.
NEVER_PROJECTED = frozenset({
    "cliente_id", "direccion_despacho", "vendedor", "usuario", "observaciones",
    "referencia_pago", "pago_grupo_id", "numero_guia_despacho", "numero_factura",
    "monto_pago_grupo", "stock_deducted", "created_at", "updated_at", "id",
    "oc_id", "forma_pago", "metodo_pago",
})


def _bad(field: str, message: str) -> InternalAuthError:
    return InternalAuthError("invalid_args", f"{field}: {message}", status=400)


def _clean_text(raw: Any, field: str) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise _bad(field, "debe ser texto")
    value = raw.strip()
    if not value:
        return None
    if len(value) > MAX_TEXT_LEN:
        raise _bad(field, f"maximo {MAX_TEXT_LEN} caracteres")
    if SQL_VALUE_RE.search(value):
        raise _bad(field, "contenido no permitido")
    return value


def _parse_date(raw: Any, field: str) -> date | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not DATE_RE.match(raw.strip()):
        raise _bad(field, "formato esperado YYYY-MM-DD")
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise _bad(field, "fecha invalida") from exc


def validate_orders_args(payload: Any) -> dict[str, Any]:
    """Mismo contrato de validacion que get_sales: el ERP no confia en nadie."""
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise _bad("payload", "debe ser un objeto")

    codigo = _clean_text(payload.get("codigo"), "codigo")
    cliente = _clean_text(payload.get("cliente"), "cliente")

    raw_estados = payload.get("estados")
    if raw_estados is None:
        estados = list(DEFAULT_ESTADOS)
    else:
        if not isinstance(raw_estados, list) or not raw_estados:
            raise _bad("estados", "debe ser una lista no vacia")
        estados = []
        for entry in raw_estados[:4]:
            if not isinstance(entry, str):
                raise _bad("estados", "las entradas deben ser texto")
            value = entry.strip().lower()
            if value not in SELECTABLE_ESTADOS:
                raise _bad("estados",
                           f"valor no permitido: {value}. "
                           f"Admitidos: {', '.join(sorted(SELECTABLE_ESTADOS))}")
            estados.append(value)

    group_by = _clean_text(payload.get("group_by"), "group_by")
    if group_by is not None:
        group_by = group_by.lower()
        if group_by not in GROUP_BY_VALUES:
            raise _bad("group_by", f"admitidos: {', '.join(sorted(GROUP_BY_VALUES))}")

    desde = _parse_date(payload.get("fecha_desde"), "fecha_desde")
    hasta = _parse_date(payload.get("fecha_hasta"), "fecha_hasta")
    if desde and hasta:
        if hasta < desde:
            raise _bad("fecha_hasta", "no puede ser anterior a fecha_desde")
        if (hasta - desde).days > MAX_WINDOW_DAYS:
            raise _bad("fecha_hasta", f"ventana maxima {MAX_WINDOW_DAYS} dias")

    raw_limit = payload.get("limit")
    if raw_limit is None:
        limit = DEFAULT_LIMIT
    else:
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
            raise _bad("limit", "debe ser un entero")
        if raw_limit < MIN_LIMIT or raw_limit > MAX_LIMIT:
            raise _bad("limit", f"entre {MIN_LIMIT} y {MAX_LIMIT}")
        limit = raw_limit

    return {"codigo": codigo, "cliente": cliente, "estados": estados,
            "group_by": group_by, "fecha_desde": desde, "fecha_hasta": hasta,
            "limit": limit}


def _public_line(row: Any, *, include_finance: bool) -> dict[str, Any]:
    out: dict[str, Any] = {
        "codigo": str(getattr(row, "codigo", "") or "").strip().upper(),
        "descripcion": str(getattr(row, "descripcion", "") or "").strip() or None,
        "marca": str(getattr(row, "marca", "") or "").strip() or None,
        "cantidad": int(getattr(row, "cantidad", 0) or 0),
    }
    if include_finance:
        out["precio_unitario"] = round(float(getattr(row, "precio_unitario", 0) or 0), 2)
        out["subtotal"] = round(float(getattr(row, "subtotal", 0) or 0), 2)
    return out


def get_public_orders(
    *,
    codigo: str | None = None,
    cliente: str | None = None,
    estados: list[str] | None = None,
    group_by: str | None = None,
    fecha_desde: date | None = None,
    fecha_hasta: date | None = None,
    limit: int = DEFAULT_LIMIT,
    include_finance: bool = False,
) -> tuple[dict[str, Any], bool]:
    """Agregados sobre el conjunto COMPLETO; el detalle es una muestra marcada.

    El mismo reparto que get_sales, y por la misma razon medida en 8.6: si los
    totales se derivan del detalle truncado, la cifra cambia con el `limit` y
    deja de ser un dato.
    """
    from sqlalchemy import func

    from app.extensions import db
    from app.oc_clientes.models import OrdenCompraCliente, OrdenCompraClienteItem

    estados = list(estados or DEFAULT_ESTADOS)
    limit = max(MIN_LIMIT, min(MAX_LIMIT, int(limit or DEFAULT_LIMIT)))

    def _scope(query: Any) -> Any:
        query = query.filter(func.lower(OrdenCompraCliente.estado).in_(estados))
        if fecha_desde is not None:
            query = query.filter(
                OrdenCompraCliente.fecha_oc >= datetime.combine(fecha_desde, time.min))
        if fecha_hasta is not None:
            query = query.filter(
                OrdenCompraCliente.fecha_oc
                < datetime.combine(fecha_hasta, time.min) + timedelta(days=1))
        if cliente:
            from app.ventas.models import Cliente

            query = query.join(Cliente, Cliente.id == OrdenCompraCliente.cliente_id) \
                         .filter(Cliente.nombre.ilike(f"%{cliente}%"))
        return query

    lineas = db.session.query(
        OrdenCompraClienteItem.codigo_producto.label("codigo"),
        OrdenCompraClienteItem.descripcion.label("descripcion"),
        OrdenCompraClienteItem.marca.label("marca"),
        OrdenCompraClienteItem.cantidad.label("cantidad"),
        OrdenCompraClienteItem.precio_unitario.label("precio_unitario"),
        OrdenCompraClienteItem.subtotal.label("subtotal"),
        OrdenCompraCliente.numero_oc.label("numero_oc"),
        OrdenCompraCliente.fecha_oc.label("fecha"),
        OrdenCompraCliente.estado.label("estado"),
    ).join(OrdenCompraCliente,
           OrdenCompraCliente.id == OrdenCompraClienteItem.oc_id)
    lineas = _scope(lineas)
    if codigo:
        lineas = lineas.filter(
            func.upper(func.trim(OrdenCompraClienteItem.codigo_producto)) == codigo.upper())

    rows = lineas.order_by(OrdenCompraCliente.fecha_oc.desc()).all()

    # Agregados sobre TODAS las filas del alcance, no sobre la muestra.
    unidades = sum(int(r.cantidad or 0) for r in rows)
    ordenes = {str(r.numero_oc) for r in rows}
    # Cuenta LINEAS, y el nombre tiene que decirlo: llamarlo `por_estado` a
    # secas se lee como "ordenes por estado" y son dos cifras distintas (91
    # lineas contra 69 ordenes en el conjunto completo).
    lineas_por_estado: dict[str, int] = {}
    ordenes_por_estado: dict[str, set] = {}
    for r in rows:
        clave = str(r.estado or "").strip().lower() or "sin_estado"
        lineas_por_estado[clave] = lineas_por_estado.get(clave, 0) + 1
        ordenes_por_estado.setdefault(clave, set()).add(str(r.numero_oc))

    data: dict[str, Any] = {
        "ordenes": len(ordenes),
        "lineas": len(rows),
        "unidades": unidades,
        "count": len(rows),
        "lineas_por_estado": lineas_por_estado,
        "ordenes_por_estado": {k: len(v) for k, v in ordenes_por_estado.items()},
        "estados_consultados": estados,
        # 8.x — el alcance viaja SIEMPRE, tambien cuando no hubo filtro.
        "periodo": {
            "desde": fecha_desde.isoformat() if fecha_desde else None,
            "hasta": fecha_hasta.isoformat() if fecha_hasta else None,
        },
        # Que el lector sepa que NO esta contando sin tener que preguntarlo.
        "anuladas_excluidas": ESTADO_ANULADO not in estados,
    }
    if codigo:
        data["codigo"] = codigo.upper()

    truncated = len(rows) > limit
    data["detalle_parcial"] = truncated
    data["items"] = [_public_line(r, include_finance=include_finance)
                     for r in rows[:limit]]
    for item, row in zip(data["items"], rows[:limit]):
        item["numero_oc"] = str(row.numero_oc)
        item["fecha"] = row.fecha.date().isoformat() if row.fecha else None
        item["estado"] = str(row.estado or "").strip().lower() or None

    if include_finance:
        # NO se llama `neto`: `oc_clientes.neto` es el neto de la CABECERA de la
        # orden, y esto suma los subtotales de las lineas que entraron en el
        # alcance. Con un filtro por codigo son cifras distintas, y darles el
        # mismo nombre invitaria a leer una por la otra.
        data["monto_lineas"] = round(sum(float(r.subtotal or 0) for r in rows), 2)

    if group_by:
        fmt = "%Y-%m" if group_by == "mes" else "%Y-%m-%d"
        buckets: dict[str, dict[str, Any]] = {}
        for r in rows:
            if not r.fecha:
                continue
            clave = r.fecha.strftime(fmt)
            slot = buckets.setdefault(clave, {"periodo": clave, "unidades": 0,
                                              "lineas": 0})
            slot["unidades"] += int(r.cantidad or 0)
            slot["lineas"] += 1
        data["group_by"] = group_by
        data["series"] = [buckets[k] for k in sorted(buckets)][:MAX_SERIES_POINTS]

    return data, truncated
