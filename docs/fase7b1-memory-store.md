# FASE 7B.1 — Memory store (sin integración al planner)

Estado: **storage only**. Checkpoint base de diseño: post-`5e7aa106`.

## Flags (defaults seguros)

| Variable | Default | Efecto |
|----------|---------|--------|
| `ANDES_ASSISTANT_MEMORY_ENABLED` | `0` | Kill switch memoria |
| `ANDES_ASSISTANT_MEMORY_DERIVED` | `0` | Reservado (7B.1 no escribe derived desde chat) |
| `ANDES_ASSISTANT_MEMORY_MAX_PER_USER` | `50` | Slots activos / usuario (LRU por `updated_at`) |
| `ANDES_ASSISTANT_MEMORY_MAX_PER_CONVERSATION` | `10` | Slots conversation-scoped activos |

Independiente de `ANDES_ASSISTANT_HISTORY_ENABLED`.

## Comportamiento observable

### MEMORY=0

El Assistant funciona **exactamente como antes** (FASE 5 + 7A opcional).
No lee ni escribe memoria. Endpoints `/assistant/api/memory*` → `404 memory_disabled`.

### MEMORY=1

El **storage** está disponible (CRUD admin vía BFF delete/list + upsert interno del store).
Con FASE 7B.2, si el chat llega al planner, puede adjuntar `memory_hints` seleccionados (lectura controlada).
**No hay escritura automática** desde el chat ni derived.

## Tabla

`assistant_memory_slot` — tipos cerrados: `preference`, `ui_pref`, `frequent_entity`, `pinned_entity`, `conversation_summary`.

Pipeline: validación schema → redacción secretos/PII/finanzas → persistencia. Nunca raw message/prompt → DB.

## BFF (sesión)

- `GET /assistant/api/memory`
- `DELETE /assistant/api/memory/<id>`
- `DELETE /assistant/api/memory` (todas del actor)
- `DELETE /assistant/api/memory/conversation/<conversation_id>`

Actor siempre desde sesión. Ownership estricto.

## Migración / purge

```powershell
.\.venv\Scripts\python.exe scripts/migrate_assistant_memory.py
.\.venv\Scripts\python.exe scripts/assistant_memory_purge.py
```

## Límites LRU

Activo = `deleted_at IS NULL` y (`expires_at IS NULL` OR `expires_at > now`).
Si al upsert el conteo supera el máximo, se soft-deletean los más antiguos por `updated_at ASC` hasta cumplir el tope.
