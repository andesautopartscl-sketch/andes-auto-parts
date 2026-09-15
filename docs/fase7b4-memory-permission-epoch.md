# FASE 7B.4 â€” permission_epoch e invalidaciÃ³n de memoria contextual

Base: `af752396` (explicit memory 7B.3).
Estado: **invalidaciÃ³n por epoch**. Sin derived / summary / frequent automÃ¡ticos.

## Principio

La memoria **no** autoriza. El ERP sigue siendo la Ãºnica autoridad de permisos.
`permission_epoch` solo invalida memoria **contextual** potencialmente obsoleta.

## Fuente del epoch

Tabla aislada en el DB de memoria del asistente:

```sql
assistant_permission_epoch(actor_user PRIMARY KEY, epoch INTEGER NOT NULL, updated_at TEXT)
```

- **No** es un fingerprint/hash de permisos.
- **No** usa timestamps arbitrarios como epoch.
- Provider real: `SqlitePermissionEpochProvider` (`memory_epoch.py`).
- Inicial: `get_or_init` â†’ epoch `1`.
- Incremento: `bump` atÃ³mico (`BEGIN IMMEDIATE`) ante cambio material de rol/permisos.

Hooks (fail-soft) en `seguridad/routes.py` cuando:
- cambia `rol_id`
- se actualiza el mapa `permisos`
- se crea un usuario (seed de permisos)

## ClasificaciÃ³n sensitivity (servidor)

| Tipo | sensitivity |
|------|-------------|
| `preference` | benign |
| `ui_pref` | benign |
| `pinned_entity` | contextual |
| `frequent_entity` | contextual |
| `conversation_summary` | contextual |

El cliente **no** puede enviar `sensitivity` ni `permission_epoch` (API â†’ `forbidden_field`).

## Reglas de lectura (selector)

1. ownership â†’ scope â†’ TTL/deleted â†’ schema â†’ sensitivity/epoch â†’ budget
2. **benign**: sobrevive aunque el epoch cambie (epoch puede ser NULL).
3. **contextual**: solo si `slot.permission_epoch == current_epoch`.
4. Si el epoch **no estÃ¡ disponible**: FAIL CLOSED â†’ excluir contextual; mantener benign; chat continÃºa.

## Escritura

- Contextual: estampar epoch actual; update refresca al epoch actual.
- Benign: `permission_epoch` NULL (justificaciÃ³n: no depende de autorizaciÃ³n).

## Observability (sin contenido / sin roles)

- `permission_epoch_read`
- `memory_contextual_invalidated`
- `memory_contextual_selected`
- `permission_epoch_error`

## Flags (defaults)

```
ANDES_ASSISTANT_MEMORY_ENABLED=0
ANDES_ASSISTANT_MEMORY_DERIVED=0
ANDES_ASSISTANT_MEMORY_EXPLICIT=0
```
