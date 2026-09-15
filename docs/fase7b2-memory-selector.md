# FASE 7B.2 — Memory selector (lectura controlada)

Base: `5f155ab7` (FASE 7B.1 storage).
Estado: **read path only**. Sin escritura automática, sin derived, sin summary automático.

## Flags

| Variable | Default | Efecto |
|----------|---------|--------|
| `ANDES_ASSISTANT_MEMORY_ENABLED` | `0` | Kill switch lectura+storage admin |
| `ANDES_ASSISTANT_MEMORY_DERIVED` | `0` | Sin cambios en 7B.2 (no escritura derived) |
| `ANDES_ASSISTANT_MEMORY_SELECTOR_MAX_SLOTS` | `12` | Tope de hints al planner |
| `ANDES_ASSISTANT_MEMORY_SELECTOR_MAX_CHARS` | `800` | Tope de caracteres del JSON de hints |

Independiente de `ANDES_ASSISTANT_HISTORY_ENABLED`.

## MEMORY=0

El Assistant funciona **exactamente como antes**.
No consulta MemoryStore, no envía `memory_hints`, no escribe memoria, no cambia prompts/routing/permisos.

## MEMORY=1

El orchestrator, **solo en el camino hacia el planner**, selecciona memorias del actor y las adjunta como `context["memory_hints"]`.

Orden del flujo:

```
mensaje → conversation resolution (FASE 5) → history hydrate (FASE 7A opcional)
→ memory selection (7B.2) → planner → ToolRunner (única autoridad)
```

## Pipeline de seguridad

```
candidates → ownership → scope → TTL/soft-delete → permission_epoch
→ type whitelist → prohibited-content defense → rank → dedup → budget → planner
```

Fallos de memoria: `memory_hints=[]`, chat continúa, logs sin secretos.

## Ownership / scope

- `actor_user` siempre de la sesión BFF (nunca del mensaje).
- **user**: usable en cualquier conversación del actor (`conversation_id` NULL).
- **conversation**: solo si `conversation_id` coincide **exactamente** con la conversación actual. Sin fallback.

## Ranking (determinista)

1. `preference`
2. `ui_pref`
3. `pinned_entity` (scope conversation)
4. `pinned_entity` (scope user)
5. `conversation_summary`
6. `frequent_entity`

Dentro del mismo nivel: conversation > user, luego `updated_at` más reciente, luego mayor `confidence`.

Deduplicación: `scope|memory_type|key` — se conserva la mejor rankeada.

## Budget

- Máx. **12** slots.
- Máx. **~800** caracteres del JSON final (`separators=(',', ':')`).
- Si un slot no cabe: **omitir** (nunca truncar JSON).

## Contrato `memory_hints`

Lista de objetos mínimos enviados al planner:

```json
[
  {"type": "preference", "key": "pref.answer_style", "value": {"answer_style": "brief"}},
  {"type": "pinned_entity", "key": "pin.2404", "value": {"kind": "codigo", "value": "2404"}}
]
```

**No incluye:** timestamps, `actor_user`, `permission_epoch`, `source_turn_id`, `deleted_at`, meta interna, IDs de DB.

La memoria es **contexto auxiliar**, nunca autoridad de permisos ni WRITE.

## permission_epoch

Abstracción: `memory_epoch.resolve_actor_permission_epoch`.
Hoy **no hay fuente robusta de epoch en el ERP** → resolución **neutral** (`available=False`): no se excluye por mismatch.
Cuando exista fuente real: `contextual` con epoch incompatible se excluye; `benign` puede sobrevivir.
**No crea ni modifica permisos.**

## Observability

Métricas/audit solo:

- `memory_candidates`
- `memory_selected`
- `memory_budget_chars`
- `memory_types` (nombres de tipo)

Nunca valores de memoria ni secretos.

## Módulo

`app/assistant/orchestrator/memory_selector.py` — `select_memory_hints(...)`.
