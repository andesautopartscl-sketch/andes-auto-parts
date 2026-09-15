# FASE 7B.5 — Memoria derivada controlada

Base: `0e663d83` (permission_epoch 7B.4).
Estado: **derived post-turn**. Sin LLM. Sin auto-preference / auto-pin / WRITE.

## Flags

| Variable | Default | Efecto |
|----------|---------|--------|
| `ANDES_ASSISTANT_MEMORY_ENABLED` | `0` | Kill switch |
| `ANDES_ASSISTANT_MEMORY_DERIVED` | `0` | Sin frequent/summary automáticos |
| `ANDES_ASSISTANT_MEMORY_EXPLICIT` | `0` | Sin cambio |

`DERIVED=0`: evaluator no corre. Comportamiento = 7B.4.

## Contadores pre-threshold (NO son memoria)

Tabla técnica en el mismo SQLite de memoria:

`assistant_derived_freq_counter(actor_user, kind, value, first_seen_at, last_seen_at, hits_json)`

- No es `assistant_memory_slot`
- No aparece en GET `/assistant/api/memory`
- No entra al selector ni a `memory_hints`
- Solo cuenta observaciones calificadas hasta el umbral

Al superar el umbral: upsert de `frequent_entity` en `assistant_memory_slot`.

## Schemas

### frequent_entity (user, contextual)

```json
{"kind": "codigo", "value": "2404", "hit_count": 5}
```

Derived allowlist: `codigo`, `sku` (normalizado a codigo), `producto_id`, `proveedor_id`, `cliente_id`, `bodega_id`.

### conversation_summary (conversation, contextual)

```json
{"text": "Inventario; entidades: 2404; tools: get_inventory", "tools": ["get_inventory"]}
```

Plantilla determinista. `text` ≤ 160 (hard 240). ≤ 3 tools. Key `summary.v1`. Sin LLM.

## Threshold frequent_entity

```
qualified_hits >= 5
AND distinct_conversations >= 3
AND ventana <= 14 días
AND gap >= 120 s
AND confidence >= 0.75
AND kind allowlist
AND schema/sanitize OK
AND MEMORY=1 AND DERIVED=1
```

Qualified hit: tool evidence OK (preferente); mensaje + tool de lectura misma entidad mismo turno (secundaria). Texto solo / `recuerda...` = no hit. Máximo 1 observación por turno. **Reuse / `reuse_prior_evidence` no cuenta** (evidencia de un turno anterior no es hit nuevo).

## Hook

Tras `_save_turn` y antes de `return _finish` en el path de tools OK (`service.py`). Best-effort: el reply ya está compuesto.

## Ranking

explicit > derived:

1. preference
2. ui_pref
3. pinned conversation
4. pinned user
5. conversation_summary
6. frequent_entity

Dedup por `kind|value` para no duplicar pin explícito y frequent derived. Máx. 1 summary y 2 frequent en el budget (12 / 800).

## Epoch

Ambos contextuales. Stamp al write. Mismatch / unavailable → fail-closed en selector. Write derived sin epoch → no persiste.

## API

POST/PUT siguen rechazando `frequent_entity` y `conversation_summary` (allowlist 7B.3). GET/DELETE existentes aplican a slots derived ya promovidos.
