"""FASE 8.4 — la respuesta estructurada: una proyección, dos renderizados.

POR QUÉ EXISTE
--------------
Hoy el asistente devuelve una cadena y el navegador la pinta con
``bubble.textContent = text``. Eso es seguro —no hay forma de inyectar HTML— y es
justamente lo que impide cualquier experiencia rica: no hay ficha de producto, ni
lista de resultados, ni selección, ni navegación, porque no hay estructura que
renderizar. El texto no es una limitación de estilo, es el techo del producto.

El camino fácil sería que el navegador vuelva a formatear el payload crudo de la
herramienta, que es lo que ya hacen los comandos slash en assistant_service.js.
Ese camino se descarta: **saltaría por encima del verifier**. Una tarjeta pintada
desde el payload crudo puede enseñar cifras que el verifier habría tumbado, filas
que el truncado descartó, o campos que el texto nunca expone. Sería una segunda
verdad, sin grounding y sin procedencia.

EL PRINCIPIO
------------
Una proyección, dos renderizados. La vista se deriva del MISMO
``EvidenceItem.data_view`` del que sale el texto, que ya viene:

- filtrado por ACL (lo produjo el Gateway con el actor de la sesión),
- sin secretos (``_FORBIDDEN_KEYS`` del store),
- degradado con su cuenta de filas omitidas (8.2C),
- identificado por ``evidence_id`` (8.2).

Y cada tarjeta lleva su ``evidence_id``, así que la procedencia viaja con el dato
hasta el pixel.

LA REGLA DE CAMPOS
------------------
Lista blanca por herramienta, **derivada de lo que el formateador de texto del
composer ya imprime**. No es una lista negra de lo que hay que ocultar: es una
lista cerrada de lo que se puede enseñar. Dos consecuencias:

- se cumple por construcción la regla "una tarjeta no puede mostrar algo que el
  texto tampoco podría afirmar", porque los campos SON los del texto;
- si mañana el Gateway añade un campo nuevo a una respuesta, no puede aparecer
  en una tarjeta sin que alguien lo añada aquí a propósito.

Por eso no se proyecta ``rut`` como referencia de navegación aunque el texto lo
imprima: un identificador personal no viaja en una URL.

LO QUE NO HACE
--------------
No añade herramientas, no escribe, no consulta nada. Es una función pura sobre
evidencia ya verificada. Las acciones contextuales que la vista habilita son
navegación dentro del ERP, y las decide el frontend con el ``ref`` que la tarjeta
declara; ninguna acción de escritura nace aquí.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.assistant.orchestrator.evidence_store import EvidenceStore

# Por herramienta: qué entidad es, dónde están las filas, y QUÉ campos pueden
# verse. Los campos replican los que _format_tool_evidence ya imprime en texto.
# `ref` es el identificador con el que el frontend puede navegar al ERP; se deja
# en None cuando el único identificador disponible es personal (RUT) o interno.
TOOL_VIEWS: dict[str, dict[str, Any]] = {
    "search_catalog": {
        "entity": "producto", "list": "items", "title": "codigo",
        "fields": ("descripcion", "marca", "modelo"), "ref": "codigo",
        "label": "Catálogo",
    },
    "get_product": {
        "entity": "producto", "list": None, "title": "codigo",
        "fields": ("descripcion", "marca", "modelo"), "ref": "codigo",
        "label": "Producto",
    },
    "get_inventory": {
        "entity": "stock", "list": "items", "title": "bodega",
        "fields": ("stock", "marca"), "ref": None,
        "label": "Stock por bodega", "summary": ("codigo", "total_stock"),
    },
    "check_stock": {
        "entity": "disponibilidad", "list": "items", "title": "codigo",
        "fields": ("solicitado", "disponible", "available"), "ref": "codigo",
        "label": "Disponibilidad",
    },
    "get_stock_movements": {
        "entity": "movimiento", "list": "items", "title": "fecha",
        "fields": ("tipo", "cantidad", "bodega", "marca"), "ref": None,
        "label": "Movimientos", "summary": ("codigo", "descripcion"),
    },
    "get_ingresos": {
        "entity": "ingreso", "list": "items", "title": "fecha",
        "fields": ("cantidad", "proveedor", "bodega"), "ref": None,
        "label": "Ingresos", "summary": ("codigo",),
    },
    "get_purchase_orders": {
        "entity": "orden_compra", "list": "items", "title": "numero",
        "fields": ("fecha", "estado", "proveedor"), "ref": "numero",
        "label": "Órdenes de compra",
    },
    "get_customer": {
        # El texto imprime nombre y RUT; la tarjeta también. Pero el RUT no se
        # usa como `ref`: un identificador personal no viaja en una URL.
        "entity": "cliente", "list": "items", "title": "nombre",
        "fields": ("rut", "ciudad", "giro"), "ref": None,
        "label": "Clientes",
    },
    "get_supplier": {
        "entity": "proveedor", "list": "items", "title": "nombre",
        "fields": ("empresa", "ciudad", "giro"), "ref": None,
        "label": "Proveedores",
    },
    "get_sales": {
        # El titular son los agregados; el detalle es una muestra. Se proyectan
        # como resumen + tarjetas para que la UI no sugiera que las tarjetas son
        # el total: 'detalle_parcial' lo declara y el bloque lo rinde.
        "entity": "venta", "list": "items", "title": "numero",
        "fields": ("fecha", "codigo", "cantidad", "cliente", "estado"),
        "ref": "numero", "label": "Ventas",
        # `periodo` va en el resumen por la misma razon por la que el texto lo
        # dice siempre: una cifra agregada sin su ventana no es verificable.
        "summary": ("periodo", "unidades", "documentos", "ingresos"),
    },
    "get_orders": {
        # Mismo reparto que get_sales: agregados en el resumen, lineas en
        # tarjetas, y el alcance SIEMPRE en el resumen para que una cifra no
        # aparezca sin su ventana.
        "entity": "linea_orden", "list": "items", "title": "numero_oc",
        "fields": ("fecha", "codigo", "descripcion", "marca", "cantidad", "estado"),
        "ref": "numero_oc", "label": "Ordenes de cliente",
        "summary": ("periodo", "ordenes", "lineas", "unidades"),
    },
    "get_equivalences": {
        # 'aplicaciones' NO se proyecta como campo de tarjeta: es una lista de
        # modelos de vehiculo y mezclarla con los codigos haria que la tarjeta
        # pareciera ofrecer piezas que en realidad son coches.
        "entity": "equivalencia", "list": "items", "title": "codigo",
        "fields": ("descripcion", "marca", "modelo", "motor"),
        "ref": "codigo", "label": "Equivalencias",
    },
    "get_dashboard_kpis": {
        "entity": "kpi", "list": None, "title": None,
        "fields": (), "ref": None, "label": "Indicadores",
        "summary": ("ventas_totales", "ventas_7d", "documentos", "documentos_7d",
                    "ticket_promedio", "margen_pct"),
    },
}

_MAX_CARDS = 10
_MAX_FIELD_CHARS = 120


def _scalar(value: Any) -> str | None:
    """Sólo escalares. Un dict o una lista anidada no se proyecta nunca."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        text = value.strip()
        return text[:_MAX_FIELD_CHARS] if text else None
    return None


@dataclass(frozen=True)
class ViewField:
    name: str
    value: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "value": self.value}


@dataclass(frozen=True)
class ViewCard:
    entity: str
    title: str
    fields: tuple[ViewField, ...]
    evidence_id: str
    ref: str | None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "entity": self.entity,
            "title": self.title,
            "fields": [f.as_dict() for f in self.fields],
            "evidence_id": self.evidence_id,
        }
        if self.ref:
            out["ref"] = self.ref
        return out


@dataclass(frozen=True)
class ViewBlock:
    tool: str
    label: str
    entity: str
    evidence_id: str
    summary: tuple[ViewField, ...]
    cards: tuple[ViewCard, ...]
    total_rows: int
    shown_rows: int
    truncated: bool
    omitted_rows: int
    empty: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "label": self.label,
            "entity": self.entity,
            "evidence_id": self.evidence_id,
            "summary": [f.as_dict() for f in self.summary],
            "cards": [c.as_dict() for c in self.cards],
            "total_rows": self.total_rows,
            "shown_rows": self.shown_rows,
            # Una lista de tarjetas no puede insinuar que está completa si no lo
            # está: el truncado de 8.2C ya descartó filas y hay que decirlo.
            "truncated": self.truncated,
            "omitted_rows": self.omitted_rows,
            "empty": self.empty,
        }


@dataclass(frozen=True)
class AnswerView:
    blocks: tuple[ViewBlock, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "blocks": [b.as_dict() for b in self.blocks],
            "evidence_ids": sorted({b.evidence_id for b in self.blocks}),
        }

    def is_empty(self) -> bool:
        return not self.blocks


# Alcances que hacen honesta a una cifra agregada: {clave: (desde, hasta)}.
# `_scalar` se niega a proyectar un dict, y con razon — una estructura anidada en
# una tarjeta se lee mal. Pero negarse sin mas dejaba el alcance FUERA de la
# tarjeta mientras el texto si lo decia, y una tarjeta que muestre "0 unidades en
# 2 documentos" sin decir sobre que ventana es exactamente la falsedad de V02 en
# forma de tarjeta. Se aplana a un escalar con las mismas palabras que el texto.
_SCOPE_KEYS: dict[str, tuple[str, str]] = {"periodo": ("desde", "hasta")}


def _with_flat_scope(data: dict[str, Any]) -> dict[str, Any]:
    flat = dict(data)
    for key, (desde_k, hasta_k) in _SCOPE_KEYS.items():
        raw = data.get(key)
        if not isinstance(raw, dict):
            continue
        desde, hasta = raw.get(desde_k), raw.get(hasta_k)
        flat[key] = (f"{desde} a {hasta}" if desde and hasta
                     else f"desde {desde}" if desde
                     else f"hasta {hasta}" if hasta
                     else "sin filtro de fecha")
    return flat


def _fields_for(row: dict[str, Any], names: tuple[str, ...], evidence_id: str) -> tuple[ViewField, ...]:
    out: list[ViewField] = []
    for name in names:
        if name not in row:
            continue
        value = _scalar(row.get(name))
        if value is None:
            continue
        out.append(ViewField(name, value))
    return tuple(out)


def _rows_of(data: Any, path: str | None) -> list[dict[str, Any]]:
    if path is None:
        return [data] if isinstance(data, dict) else []
    if not isinstance(data, dict):
        return []
    raw = data.get(path)
    if not isinstance(raw, list):
        return []
    return [r for r in raw if isinstance(r, dict)]


def build_answer_view(
    store: EvidenceStore,
    *,
    scope: set[str] | None = None,
) -> AnswerView:
    """Proyecta la evidencia verificada del turno a bloques renderizables.

    ``scope`` restringe a ciertos ``evidence_id``. Se usa para que la vista
    muestre exactamente la evidencia que sostiene la respuesta publicada y no
    todo lo que el agente llegó a consultar: enseñar en tarjetas algo que el
    verifier dejó fuera del texto sería reabrir por la interfaz el agujero que
    8.1 cerró en el lenguaje.
    """
    blocks: list[ViewBlock] = []
    for item in store.items:
        if scope is not None and item.evidence_id not in scope:
            continue
        spec = TOOL_VIEWS.get(item.tool)
        if spec is None:
            # Herramienta sin proyección declarada: no se inventa una. El texto
            # sigue respondiendo; simplemente no hay tarjeta.
            continue
        data = item.data_view if isinstance(item.data_view, dict) else {}
        data = _with_flat_scope(data)
        rows = _rows_of(data, spec["list"])
        summary = _fields_for(data, tuple(spec.get("summary") or ()), item.evidence_id)

        cards: list[ViewCard] = []
        for row in rows[:_MAX_CARDS]:
            title_key = spec["title"]
            title = _scalar(row.get(title_key)) if title_key else None
            if title is None and title_key:
                continue
            ref_key = spec.get("ref")
            ref = _scalar(row.get(ref_key)) if ref_key else None
            cards.append(ViewCard(
                entity=str(spec["entity"]),
                title=title or str(spec["label"]),
                fields=_fields_for(row, tuple(spec["fields"]), item.evidence_id),
                evidence_id=item.evidence_id,
                ref=ref,
            ))
        omitted = sum(int(v) for v in (item.omitted_rows or {}).values())
        if not cards and not summary:
            # Sin nada que enseñar no se emite un bloque vacío: una tarjeta en
            # blanco es peor que ninguna.
            if not item.empty:
                continue
        blocks.append(ViewBlock(
            tool=item.tool,
            label=str(spec["label"]),
            entity=str(spec["entity"]),
            evidence_id=item.evidence_id,
            summary=summary,
            cards=tuple(cards),
            total_rows=len(rows) + omitted,
            shown_rows=len(cards),
            truncated=bool(item.truncated) or len(rows) > len(cards),
            omitted_rows=omitted,
            empty=bool(item.empty),
        ))
    return AnswerView(tuple(blocks))
