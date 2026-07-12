"""Consultas mobile: reutiliza helpers del ERP sin duplicar lógica de negocio."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, or_

from app.bodega.models import (
    IngresoDocumento,
    IngresoDocumentoItem,
    MovimientoStock,
    ProductoVarianteStock,
    ProveedorCodigoInterno,
)
from app.bodega.routes import _ingreso_total_con_iva
from app.dashboard.routes import _count_documentos_periodo, _ventas_periodo
from app.extensions import db
from app.models import Producto
from app.productos.routes import (
    _collect_imagenes_360,
    _collect_imagenes_producto,
    _ficha_despiece_payload,
    _ficha_stock_repuestos,
    _find_producto_by_codigo,
)
from app.utils.product_image_url import product_image_src
from app.utils.format_currency_cl import format_precio_publico_con_iva
from app.utils.stock_control import get_available_stock
from app.ventas.models import DocumentoVenta, DocumentoVentaItem
from app.ventas.routes import (
    _search_products,
    _serialize_document,
    _ultimo_ingreso_ref,
)

_FACTURA_TIPOS = ("factura", "boleta")
_STOCK_CRITICO_UMBRAL = 3


def _catalog_sync_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mask_monto(valor: float | None, puede_ver: bool) -> float | None:
    return valor if puede_ver else None


def _fmt_monto(valor: float | None, puede_ver: bool) -> str:
    if not puede_ver:
        return "—"
    if valor is None:
        return "$0"
    s = "{:,.0f}".format(round(float(valor))).replace(",", ".")
    return f"${s}"


def _ventas_query_base():
    return DocumentoVenta.query.filter(
        DocumentoVenta.tipo.in_(_FACTURA_TIPOS),
        DocumentoVenta.status != "anulada",
    )


def _ingresos_query_base():
    return IngresoDocumento.query.filter(
        or_(IngresoDocumento.anulado.is_(False), IngresoDocumento.anulado.is_(None))
    )


def _rango_filtro(periodo: str) -> tuple[date, date]:
    today = date.today()
    p = (periodo or "hoy").strip().lower()
    if p == "ayer":
        d = today - timedelta(days=1)
        return d, d
    if p == "semana":
        return today - timedelta(days=6), today
    return today, today


def _top_productos_periodo(fecha_inicio: date, fecha_fin: date, limit: int = 5) -> list[dict]:
    rows = (
        db.session.query(
            DocumentoVentaItem.codigo_producto,
            DocumentoVentaItem.descripcion,
            func.sum(DocumentoVentaItem.cantidad).label("total_qty"),
            func.sum(DocumentoVentaItem.subtotal).label("total_venta"),
        )
        .join(DocumentoVenta, DocumentoVentaItem.documento_id == DocumentoVenta.id)
        .filter(
            DocumentoVenta.tipo.in_(_FACTURA_TIPOS),
            DocumentoVenta.status != "anulada",
            func.date(DocumentoVenta.fecha_documento) >= fecha_inicio,
            func.date(DocumentoVenta.fecha_documento) <= fecha_fin,
        )
        .group_by(DocumentoVentaItem.codigo_producto, DocumentoVentaItem.descripcion)
        .order_by(func.sum(DocumentoVentaItem.subtotal).desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "codigo": r.codigo_producto,
            "descripcion": r.descripcion or r.codigo_producto,
            "qty": int(r.total_qty or 0),
            "venta": float(r.total_venta or 0),
        }
        for r in rows
    ]


def _top_clientes_periodo(fecha_inicio: date, fecha_fin: date, limit: int = 5) -> list[dict]:
    rows = (
        db.session.query(
            DocumentoVenta.cliente_nombre,
            func.sum(DocumentoVenta.total).label("total_venta"),
            func.count(DocumentoVenta.id).label("num_docs"),
        )
        .filter(
            DocumentoVenta.tipo.in_(_FACTURA_TIPOS),
            DocumentoVenta.status != "anulada",
            func.date(DocumentoVenta.fecha_documento) >= fecha_inicio,
            func.date(DocumentoVenta.fecha_documento) <= fecha_fin,
        )
        .group_by(DocumentoVenta.cliente_nombre)
        .order_by(func.sum(DocumentoVenta.total).desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "nombre": r.cliente_nombre or "Sin nombre",
            "total": float(r.total_venta or 0),
            "docs": int(r.num_docs or 0),
        }
        for r in rows
    ]


def _ingreso_neto_por_docs(doc_ids: list[int]) -> dict[int, float]:
    if not doc_ids:
        return {}
    sums = (
        db.session.query(
            IngresoDocumentoItem.ingreso_documento_id,
            func.sum(
                func.coalesce(IngresoDocumentoItem.valor_neto, 0)
                * func.coalesce(IngresoDocumentoItem.cantidad, 0)
            ),
        )
        .filter(IngresoDocumentoItem.ingreso_documento_id.in_(doc_ids))
        .group_by(IngresoDocumentoItem.ingreso_documento_id)
        .all()
    )
    return {int(doc_id): float(total or 0) for doc_id, total in sums}


def ingresos_periodo_stats(fecha_inicio: date, fecha_fin: date) -> tuple[int, float]:
    docs = (
        _ingresos_query_base()
        .filter(
            func.date(IngresoDocumento.fecha_documento) >= fecha_inicio,
            func.date(IngresoDocumento.fecha_documento) <= fecha_fin,
        )
        .all()
    )
    if not docs:
        return 0, 0.0
    ids = [int(d.id) for d in docs]
    netos = _ingreso_neto_por_docs(ids)
    total_neto = sum(neto for neto in netos.values())
    total_con_iva = sum(
        _ingreso_total_con_iva(d.total_factura, netos.get(int(d.id), 0.0)) for d in docs
    )
    return len(docs), round(total_con_iva, 2)


def count_productos_activos() -> int:
    return db.session.query(Producto).filter(Producto.activo.is_(True)).count()


def count_stock_critico(threshold: int = _STOCK_CRITICO_UMBRAL) -> int:
    return (
        ProductoVarianteStock.query.filter(
            ProductoVarianteStock.stock <= threshold,
            ProductoVarianteStock.stock >= 0,
        ).count()
    )


def ultimas_ventas_hoy(limit: int = 5) -> list[dict]:
    today = date.today()
    rows = (
        _ventas_query_base()
        .filter(func.date(DocumentoVenta.fecha_documento) == today)
        .order_by(DocumentoVenta.fecha_documento.desc(), DocumentoVenta.id.desc())
        .limit(limit)
        .all()
    )
    return [_venta_resumen(r) for r in rows]


def ventas_por_periodo(periodo: str, limit: int = 100) -> list[dict]:
    desde, hasta = _rango_filtro(periodo)
    rows = (
        _ventas_query_base()
        .filter(
            func.date(DocumentoVenta.fecha_documento) >= desde,
            func.date(DocumentoVenta.fecha_documento) <= hasta,
        )
        .order_by(DocumentoVenta.fecha_documento.desc(), DocumentoVenta.id.desc())
        .limit(limit)
        .all()
    )
    return [_venta_resumen(r) for r in rows]


def _venta_resumen(doc: DocumentoVenta) -> dict:
    fd = doc.fecha_documento
    hora = ""
    if isinstance(fd, datetime):
        hora = fd.strftime("%H:%M")
    return {
        "id": doc.id,
        "numero": (doc.numero or "").strip(),
        "tipo": (doc.tipo or "").strip(),
        "cliente": (doc.cliente_nombre or "Sin cliente").strip(),
        "total": float(doc.total or 0),
        "fecha": fd.strftime("%Y-%m-%d") if fd else "",
        "hora": hora,
        "descripcion": f"{(doc.tipo or 'doc').title()} {doc.numero or doc.id}",
    }


def ultimos_ingresos_hoy(limit: int = 5) -> list[dict]:
    today = date.today()
    rows = (
        _ingresos_query_base()
        .filter(func.date(IngresoDocumento.fecha_documento) == today)
        .order_by(IngresoDocumento.created_at.desc(), IngresoDocumento.id.desc())
        .limit(limit)
        .all()
    )
    return _ingresos_resumen(rows)


def ultimos_ingresos(limit: int = 20) -> list[dict]:
    rows = (
        _ingresos_query_base()
        .order_by(IngresoDocumento.created_at.desc(), IngresoDocumento.id.desc())
        .limit(limit)
        .all()
    )
    return _ingresos_resumen(rows)


def _ingresos_resumen(rows: list[IngresoDocumento]) -> list[dict]:
    if not rows:
        return []
    ids = [int(r.id) for r in rows]
    netos = _ingreso_neto_por_docs(ids)
    out = []
    for doc in rows:
        doc_id = int(doc.id)
        neto = netos.get(doc_id, 0.0)
        total = _ingreso_total_con_iva(doc.total_factura, neto)
        fd = doc.fecha_documento
        out.append(
            {
                "id": doc_id,
                "numero": (doc.numero_documento or "").strip() or f"ING-{doc_id}",
                "proveedor": (doc.proveedor_nombre or "Sin proveedor").strip(),
                "total": round(total, 2),
                "fecha": fd.strftime("%Y-%m-%d") if fd else "",
            }
        )
    return out


def ingreso_detalle(doc_id: int) -> dict | None:
    doc = _ingresos_query_base().filter(IngresoDocumento.id == doc_id).first()
    if doc is None:
        return None
    items = (
        IngresoDocumentoItem.query.filter_by(ingreso_documento_id=doc_id)
        .order_by(IngresoDocumentoItem.id.asc())
        .all()
    )
    neto = sum(float(it.valor_neto or 0) * int(it.cantidad or 0) for it in items)
    total = _ingreso_total_con_iva(doc.total_factura, neto)
    lineas = [
        {
            "codigo": (it.codigo_producto or "").strip(),
            "descripcion": (it.descripcion_producto or "").strip(),
            "marca": (it.marca or "").strip(),
            "bodega": (it.bodega or "").strip(),
            "origen_compra": (it.origen_compra or "").strip(),
            "cantidad": int(it.cantidad or 0),
            "valor_neto": float(it.valor_neto or 0),
        }
        for it in items
    ]
    return {
        "id": doc_id,
        "numero": (doc.numero_documento or "").strip() or f"ING-{doc_id}",
        "proveedor": (doc.proveedor_nombre or "").strip(),
        "proveedor_rut": (doc.proveedor_rut or "").strip(),
        "fecha": doc.fecha_documento.strftime("%Y-%m-%d") if doc.fecha_documento else "",
        "metodo_pago": (doc.metodo_pago or "").strip(),
        "total_neto": round(neto, 2),
        "total": round(total, 2),
        "items": lineas,
    }


def stock_critico_lista(threshold: int = _STOCK_CRITICO_UMBRAL, limit: int = 200) -> list[dict]:
    rows = (
        ProductoVarianteStock.query.filter(
            ProductoVarianteStock.stock <= threshold,
            ProductoVarianteStock.stock >= 0,
        )
        .order_by(ProductoVarianteStock.stock.asc(), ProductoVarianteStock.codigo_producto.asc())
        .limit(limit)
        .all()
    )
    codigos = sorted({(r.codigo_producto or "").strip().upper() for r in rows if r.codigo_producto})
    desc_map: dict[str, str] = {}
    if codigos:
        productos = db.session.query(Producto.codigo, Producto.descripcion).filter(
            func.upper(Producto.codigo).in_(codigos)
        ).all()
        desc_map = {(c or "").strip().upper(): (d or "").strip() for c, d in productos}
    return [
        {
            "codigo": (r.codigo_producto or "").strip(),
            "descripcion": desc_map.get((r.codigo_producto or "").strip().upper(), r.codigo_producto),
            "marca": (r.marca or "").strip(),
            "bodega": (r.bodega or "").strip(),
            "stock": int(r.stock or 0),
            "minimo": threshold,
        }
        for r in rows
    ]


def dashboard_payload(puede_ver_finanzas: bool) -> dict:
    today = date.today()
    ventas_monto = _ventas_periodo(today, today)
    ventas_count = _count_documentos_periodo(today, today)
    ingresos_count, ingresos_monto = ingresos_periodo_stats(today, today)
    return {
        "ventas_hoy_count": ventas_count,
        "ventas_hoy_monto": _mask_monto(ventas_monto, puede_ver_finanzas),
        "ventas_hoy_monto_fmt": _fmt_monto(ventas_monto, puede_ver_finanzas),
        "ingresos_hoy_count": ingresos_count,
        "ingresos_hoy_monto": _mask_monto(ingresos_monto, puede_ver_finanzas),
        "ingresos_hoy_monto_fmt": _fmt_monto(ingresos_monto, puede_ver_finanzas),
        "stock_critico_count": count_stock_critico(),
        "productos_total": count_productos_activos(),
        "ultimas_ventas": [
            {
                **v,
                "total_fmt": _fmt_monto(v["total"], puede_ver_finanzas),
            }
            for v in ultimas_ventas_hoy(5)
        ],
        "ultimos_ingresos": [
            {
                **i,
                "total_fmt": _fmt_monto(i["total"], puede_ver_finanzas),
            }
            for i in ultimos_ingresos_hoy(5)
        ],
    }


def reportes_payload(puede_ver_finanzas: bool) -> dict:
    today = date.today()
    semana_inicio = today - timedelta(days=6)
    mes_inicio = today.replace(day=1)
    ventas_semana_monto = _ventas_periodo(semana_inicio, today)
    ventas_semana_count = _count_documentos_periodo(semana_inicio, today)
    ventas_mes_monto = _ventas_periodo(mes_inicio, today)
    ventas_mes_count = _count_documentos_periodo(mes_inicio, today)
    top_prod = _top_productos_periodo(mes_inicio, today, 5)
    top_cli = _top_clientes_periodo(mes_inicio, today, 5)
    return {
        "ventas_semana_count": ventas_semana_count,
        "ventas_semana_monto": _mask_monto(ventas_semana_monto, puede_ver_finanzas),
        "ventas_semana_monto_fmt": _fmt_monto(ventas_semana_monto, puede_ver_finanzas),
        "ventas_mes_count": ventas_mes_count,
        "ventas_mes_monto": _mask_monto(ventas_mes_monto, puede_ver_finanzas),
        "ventas_mes_monto_fmt": _fmt_monto(ventas_mes_monto, puede_ver_finanzas),
        "top_productos": [
            {**p, "venta_fmt": _fmt_monto(p["venta"], puede_ver_finanzas)}
            for p in top_prod
        ],
        "top_clientes": [
            {**c, "total_fmt": _fmt_monto(c["total"], puede_ver_finanzas)}
            for c in top_cli
        ],
    }


def _codigos_por_proveedor(term: str) -> list[str]:
    t = (term or "").strip().upper()
    if not t:
        return []
    rows = (
        ProveedorCodigoInterno.query.filter(
            func.upper(func.trim(ProveedorCodigoInterno.codigo_proveedor)).like(f"%{t}%")
        )
        .limit(30)
        .all()
    )
    exact = (
        ProveedorCodigoInterno.query.filter(
            func.upper(func.trim(ProveedorCodigoInterno.codigo_proveedor)) == t
        )
        .all()
    )
    codes: list[str] = []
    seen: set[str] = set()
    for row in list(exact) + list(rows):
        c = (row.codigo_interno or "").strip().upper()
        if c and c not in seen:
            seen.add(c)
            codes.append(c)
    return codes


def buscar_productos(term: str, limit: int = 30) -> list[dict]:
    """Búsqueda legacy (venta rápida / api/buscar) — delega al ERP _search_products."""
    items = _search_products(term, limit=limit)
    if not items:
        for codigo in _codigos_por_proveedor(term):
            p = _find_producto_by_codigo(db.session, codigo)
            if p is not None and p.activo is not False:
                precio = float(p.p_publico or 0)
                if precio <= 0:
                    ref = _ultimo_ingreso_ref(codigo, None, "Bodega 1")
                    if ref and ref.get("precio_sugerido_neto"):
                        precio = float(ref["precio_sugerido_neto"])
                items.append(
                    {
                        "codigo": (p.codigo or "").strip(),
                        "descripcion": (p.descripcion or "").strip(),
                        "marca": (p.marca or "").strip(),
                        "precio": precio,
                        "stock": int(get_available_stock(codigo)),
                        "precio_fmt": format_precio_publico_con_iva(precio) if precio > 0 else "—",
                    }
                )
                if len(items) >= limit:
                    break
    else:
        enriched = []
        for row in items:
            codigo = (row.get("codigo") or "").strip()
            stock = int(get_available_stock(codigo)) if codigo else int(row.get("stock") or 0)
            precio = float(row.get("precio") or 0)
            if precio <= 0 and codigo:
                ref = _ultimo_ingreso_ref(codigo, None, "Bodega 1")
                if ref and ref.get("precio_sugerido_neto"):
                    precio = float(ref["precio_sugerido_neto"])
            enriched.append(
                {
                    "codigo": codigo,
                    "descripcion": (row.get("descripcion") or "").strip(),
                    "marca": (row.get("marca") or "").strip(),
                    "precio": precio,
                    "stock": stock,
                    "precio_fmt": format_precio_publico_con_iva(precio) if precio > 0 else "—",
                }
            )
        items = enriched
    return items[:limit]


def _producto_caracteristicas(producto: Producto) -> list[dict]:
    items: list[dict] = []

    def _add(label: str, value: str | None) -> None:
        v = (value or "").strip()
        if v:
            items.append({"label": label, "value": v})

    _add("Motor", producto.motor)
    _add("Versión", producto.version)
    _add("Año", producto.anio)
    _add("Medidas", producto.medidas)
    aplicacion: list[str] = []
    for part in (producto.modelo,):
        p = (part or "").strip()
        if p:
            aplicacion.append(p)
    if aplicacion:
        _add("Modelo", " · ".join(aplicacion))
    _add("OEM", producto.codigo_oem)
    _add("Código alternativo", producto.codigo_alternativo)
    _add("Homologados", producto.homologados)
    _add("Despiece", producto.despiece)
    try:
        if producto.categoria_rel and (producto.categoria_rel.nombre or "").strip():
            _add("Categoría", producto.categoria_rel.nombre)
        if producto.subcategoria_rel and (producto.subcategoria_rel.nombre or "").strip():
            _add("Subcategoría", producto.subcategoria_rel.nombre)
    except Exception:
        pass
    return items


def producto_detalle(codigo_raw: str, *, puede_ver_precio: bool = True) -> dict | None:
    producto = _find_producto_by_codigo(db.session, codigo_raw)
    if producto is None or producto.activo is False:
        return None
    codigo = (producto.codigo or "").strip().upper()
    imagenes = _collect_imagenes_producto(producto)
    imagenes_urls = [product_image_src(img) for img in imagenes if img]
    imagenes_360 = _collect_imagenes_360(producto)
    imagenes_360_urls = [
        product_image_src(f"productos360/{codigo}/{name}") for name in imagenes_360
    ]
    desp = _ficha_despiece_payload(db.session, producto)
    despiece_src = (desp.get("despiece_imagen_src") or "").strip()
    galeria: list[dict] = []
    seen_urls: set[str] = set()

    def _add_galeria(url: str, tipo: str, label: str = "") -> None:
        u = (url or "").strip()
        if not u or u in seen_urls:
            return
        seen_urls.add(u)
        galeria.append({"url": u, "tipo": tipo, "label": label})

    for url in imagenes_urls:
        _add_galeria(url, "foto", "Producto")
    if despiece_src:
        _add_galeria(despiece_src, "despiece", "Despiece")
    ficha_stock = _ficha_stock_repuestos(producto)
    ref = _ultimo_ingreso_ref(codigo, (producto.marca or "").strip().upper() or None, "Bodega 1")
    precio_neto = float(producto.p_publico or 0)
    if precio_neto <= 0 and ref and ref.get("precio_sugerido_neto"):
        precio_neto = float(ref["precio_sugerido_neto"])
    codigos_proveedor = [
        (r.codigo_proveedor or "").strip()
        for r in ProveedorCodigoInterno.query.filter(
            func.upper(ProveedorCodigoInterno.codigo_interno) == codigo
        )
        .order_by(ProveedorCodigoInterno.updated_at.desc())
        .limit(5)
        .all()
        if (r.codigo_proveedor or "").strip()
    ]
    movs = (
        MovimientoStock.query.filter(func.upper(MovimientoStock.codigo_producto) == codigo)
        .order_by(MovimientoStock.fecha.desc(), MovimientoStock.id.desc())
        .limit(10)
        .all()
    )
    movimientos = [
        {
            "tipo": (m.tipo or "").strip(),
            "cantidad": int(m.cantidad or 0),
            "bodega": (m.bodega or "").strip(),
            "marca": (m.marca or "").strip(),
            "fecha": m.fecha.strftime("%Y-%m-%d %H:%M") if m.fecha else "",
            "observacion": (m.observacion or "").strip(),
        }
        for m in movs
    ]
    origen = (ref or {}).get("origen_compra") or "nacional"
    etiquetas_nombres: list[str] = []
    try:
        for et in producto.etiquetas or []:
            nombre = (getattr(et, "nombre", None) or "").strip()
            if nombre:
                etiquetas_nombres.append(nombre)
    except Exception:
        pass
    categoria = ""
    subcategoria = ""
    try:
        if producto.categoria_rel and (producto.categoria_rel.nombre or "").strip():
            categoria = producto.categoria_rel.nombre.strip()
        if producto.subcategoria_rel and (producto.subcategoria_rel.nombre or "").strip():
            subcategoria = producto.subcategoria_rel.nombre.strip()
    except Exception:
        pass
    return {
        "codigo": codigo,
        "descripcion": (producto.descripcion or "").strip(),
        "marca": (producto.marca or "").strip(),
        "modelo": (producto.modelo or "").strip(),
        "motor": (producto.motor or "").strip(),
        "anio": (producto.anio or "").strip(),
        "version": (producto.version or "").strip(),
        "medidas": (producto.medidas or "").strip(),
        "codigo_oem": (producto.codigo_oem or "").strip(),
        "codigo_alternativo": (producto.codigo_alternativo or "").strip(),
        "homologados": (producto.homologados or "").strip(),
        "despiece": (producto.despiece or "").strip(),
        "categoria": categoria,
        "subcategoria": subcategoria,
        "etiquetas": etiquetas_nombres,
        "codigos_proveedor": codigos_proveedor,
        "imagen": imagenes_urls[0] if imagenes_urls else None,
        "imagenes_urls": imagenes_urls,
        "imagenes_360_urls": imagenes_360_urls,
        "tiene_360": bool(imagenes_360_urls),
        "despiece_imagen_src": despiece_src or None,
        "galeria": galeria,
        "precio_neto": precio_neto if puede_ver_precio else None,
        "precio_venta_fmt": format_precio_publico_con_iva(precio_neto) if puede_ver_precio and precio_neto > 0 else "—",
        "precio_neto_fmt": _fmt_monto(precio_neto, puede_ver_precio),
        "puede_ver_precio": puede_ver_precio,
        "ficha_stock": ficha_stock,
        "origen": origen,
        "movimientos": movimientos,
        "caracteristicas": _producto_caracteristicas(producto),
    }


def venta_detalle(doc_id: int) -> dict | None:
    doc = (
        _ventas_query_base()
        .filter(DocumentoVenta.id == doc_id)
        .first()
    )
    if doc is None:
        return None
    return _serialize_document(doc)


def catalogo_completo() -> list[dict]:
    """Catálogo activo para cache offline con campos de búsqueda extendidos."""
    from . import productos_buscar as mobile_productos_buscar

    productos = (
        db.session.query(Producto)
        .filter(Producto.activo.is_(True))
        .order_by(Producto.codigo.asc())
        .all()
    )
    if not productos:
        return []
    codigos = sorted({(p.codigo or "").strip().upper() for p in productos if p.codigo})
    stock_map: dict[str, list[dict]] = {}
    if codigos:
        for row in ProductoVarianteStock.query.filter(
            func.upper(ProductoVarianteStock.codigo_producto).in_(codigos)
        ).all():
            codigo = (row.codigo_producto or "").strip().upper()
            if not codigo:
                continue
            stock_map.setdefault(codigo, []).append(
                {
                    "bodega": (row.bodega or "").strip(),
                    "marca": (row.marca or "").strip(),
                    "stock": int(row.stock or 0),
                }
            )
    out: list[dict] = []
    for producto in productos:
        item = mobile_productos_buscar.catalogo_item(producto, stock_map, puede_ver_precio=True)
        if item:
            out.append(item)
    return out


def catalogo_pagina(offset: int = 0, limit: int = 1500) -> tuple[list[dict], int]:
    """Página del catálogo activo para sincronización offline por lotes."""
    from . import productos_buscar as mobile_productos_buscar

    limit = max(1, min(int(limit or 1500), 2500))
    offset = max(0, int(offset or 0))
    base_q = db.session.query(Producto).filter(Producto.activo.is_(True))
    total = base_q.count()
    productos = (
        base_q.order_by(Producto.codigo.asc()).offset(offset).limit(limit).all()
    )
    if not productos:
        return [], total
    codigos = sorted({(p.codigo or "").strip().upper() for p in productos if p.codigo})
    stock_map: dict[str, list[dict]] = {}
    if codigos:
        for row in ProductoVarianteStock.query.filter(
            func.upper(ProductoVarianteStock.codigo_producto).in_(codigos)
        ).all():
            codigo = (row.codigo_producto or "").strip().upper()
            if not codigo:
                continue
            stock_map.setdefault(codigo, []).append(
                {
                    "bodega": (row.bodega or "").strip(),
                    "marca": (row.marca or "").strip(),
                    "stock": int(row.stock or 0),
                }
            )
    out: list[dict] = []
    for producto in productos:
        item = mobile_productos_buscar.catalogo_item(producto, stock_map, puede_ver_precio=True)
        if item:
            out.append(item)
    return out, total


def stock_vista_rapida(codigo_raw: str) -> dict | None:
    """Vista compacta de stock (escáner barcode)."""
    detalle = producto_detalle(codigo_raw)
    if detalle is None:
        return None
    bodegas: list[dict] = []
    ficha = detalle.get("ficha_stock") or {}
    for b in ficha.get("bodegas") or []:
        nombre = (b.get("nombre") or "").strip()
        subtotal = int(b.get("subtotal") or 0)
        if nombre and subtotal > 0:
            bodegas.append({"nombre": nombre, "stock": subtotal})
    for o in ficha.get("otras_lineas") or []:
        nombre = (o.get("bodega") or "OTRAS").strip()
        stock = int(o.get("stock") or 0)
        if stock > 0:
            bodegas.append({"nombre": nombre, "stock": stock})
    total = int(ficha.get("stotal") or 0)
    return {
        "codigo": detalle["codigo"],
        "descripcion": detalle["descripcion"],
        "imagen": detalle.get("imagen"),
        "stock_total": total,
        "bodegas": bodegas,
    }
