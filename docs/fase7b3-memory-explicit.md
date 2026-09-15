# FASE 7B.3 â€” Memoria explÃ­cita controlada

Base: `e7416a1e` (selector 7B.2).
Estado: **escritura explÃ­cita** solamente. Sin derived / summary / frequent automÃ¡ticos.

## Flags

| Variable | Default | Efecto |
|----------|---------|--------|
| `ANDES_ASSISTANT_MEMORY_ENABLED` | `0` | Kill switch memoria |
| `ANDES_ASSISTANT_MEMORY_DERIVED` | `0` | Sin escritura derived |
| `ANDES_ASSISTANT_MEMORY_EXPLICIT` | `0` | SeÃ±ales NL en chat (`recuerdaâ€¦`) |

## Comportamiento

### MEMORY=0
`POST/PUT /assistant/api/memory` â†’ `404 memory_disabled`. Chat intacto.

### MEMORY=1 + EXPLICIT=0
- API estructurada `POST/PUT` **sÃ­** (payload cerrado = intenciÃ³n explÃ­cita del cliente).
- Chat NL **no** crea memoria automÃ¡ticamente.

### MEMORY=1 + EXPLICIT=1
Chat detecta solo seÃ±ales inequÃ­vocas y escribe `preference` / `ui_pref` / `pinned_entity`.

## Tipos escribibles

Permitidos: `preference`, `ui_pref`, `pinned_entity`.
Prohibidos en escritura explÃ­cita: `frequent_entity`, `conversation_summary`.

## SeÃ±ales de intenciÃ³n (ejemplos)

VÃ¡lidas:
- "Recuerda que prefiero respuestas breves."
- "Mi preferencia es respuestas detalladas."
- "Fija el producto 2404 para esta conversaciÃ³n."

No vÃ¡lidas / no permanentes:
- "Hoy prefiero algo breve." (temporal)
- "Ese producto me interesa." (sin seÃ±al de guardar)

## Rechazos

- Poisoning / permisos / WRITE / Bearer / API keys / PII claims
- Tipos no permitidos
- Schema invÃ¡lido
- SanitizaciÃ³n / forbidden fields

## Endpoints

- `POST /assistant/api/memory` â€” create/upsert estructurado
- `PUT /assistant/api/memory/<id>` â€” update owned slot
- GET/DELETE existentes (7B.1)

Actor siempre desde sesiÃ³n. Nunca `actor_user` del cliente.

## Pipeline

```
intent | API payload â†’ closed schema â†’ sanitize â†’ upsert â†’ confirm/fail honest
```

OperaciÃ³n auxiliar: **no** es tool ERP; no altera ToolRunner ni permisos.

## Observability

Solo: `memory_write_attempt|success|rejected|reason|type|scope`. Sin contenido.
