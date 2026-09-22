# FASE 10.2 — Historial + Memoria 2.0

> Estado: **propuesta**. Sin implementación asociada. Escrita sobre el árbol
> estable (checkpoint `0b3bcfea` + routers experimentales apagados).

---

## 1. Estado actual del historial

**Existe y funciona.** No es trabajo nuevo.

`AssistantConversation` — `id`, `client_conversation_id`, `actor_user`, `title`,
`status`, `created_at`, `updated_at`, `last_turn_at`, `turn_count`,
`retention_until`, `deleted_at`.

`AssistantTurn` — `id`, `conversation_id`, `actor_user`, `seq`,
`correlation_id`, `message_hash`, `message_excerpt`, `reply_excerpt`,
`scenario`, `classification`, `tools_used_json`, `entities_json`,
`evidence_json`, `flags_json`, `llm_latency_ms`, `total_latency_ms`,
`planner_mode`, `created_at`. Con `UNIQUE(conversation_id, seq)`.

`history_store.py` (811 líneas) ya ofrece:

| capacidad | estado |
|---|---|
| persistencia | ✅ SQLite propio, esquema autogestionado |
| crear / continuar conversación | ✅ `ensure_conversation` (resuelve por `client_conversation_id`) |
| añadir turno | ✅ `append_turn`, con `seq` monotónico |
| **paginación de conversaciones** | ✅ **cursor real** `(updated_at, id)`, límite 1–100 |
| **paginación de turnos** | ✅ por `seq`, hacia atrás y hacia delante |
| aislamiento por actor | ✅ todas las consultas filtran por `actor_user` |
| borrado | ✅ `soft_delete_conversation` (marca `deleted_at`) |
| retención | ✅ `retention_until` + `purge_expired` |
| hidratación para anáfora | ✅ `recent_turns_for_resolver(limit=6)` |

Y 5 rutas REST: listar, crear, obtener, turnos, borrar.

**Lo que NO existe:** búsqueda, resumen, fijar conversación, título automático, y
`archivado` como estado distinto de `borrado`.

---

## 2. Estado actual de la memoria

**Existe, y el modelo de datos ya es el que pides.** `AssistantMemorySlot`:
`id`, `actor_user`, `scope`, `conversation_id`, `memory_type`, `key`,
`value_json`, `confidence`, `source`, `permission_epoch`, `sensitivity`,
`created_at`, `updated_at`, `expires_at`, `deleted_at`, `source_turn_id`,
`meta_json`.

Vocabularios cerrados hoy:

```
SCOPES         user · conversation
SOURCES        explicit · derived · ui
MEMORY_TYPES   preference · ui_pref · frequent_entity · pinned_entity
               conversation_summary
SENSITIVITIES  benign · contextual
```

**Dos piezas que conviene conocer antes de diseñar nada:**

**(a) La deduplicación por identidad ya existe.** Hay un índice
`UNIQUE(actor_user, scope, IFNULL(conversation_id,''), memory_type, key)`. Dos
escrituras de la misma clave no crean dos filas: la segunda actualiza la
primera. Eso resuelve el duplicado *exacto*, no el semántico.

**(b) El selector ya es una tubería de diez etapas**, documentada en su propio
encabezado:

```
candidatos → propiedad → scope → TTL → permission_epoch → lista blanca de tipos
→ defensa de contenido prohibido → ranking → dedup → presupuesto → memory_hints
```

Y `memory_derived.py` ya exige umbrales para proponer algo: `MIN_CONFIDENCE
0.75`, `MIN_DISTINCT_CONVERSATIONS`, `MIN_QUALIFIED_HITS`, `MIN_GAP_SECONDS`.

**Lo que NO existe:** el ciclo de vida `suggested/approved/rejected/expired`,
la auditoría, y el scope empresarial.

---

## 3. Qué reutilizamos

Casi todo. En concreto:

- los dos modelos y sus dos almacenes;
- la paginación por cursor —no hace falta inventarla—;
- el índice único, que ya es la política de deduplicación por identidad;
- la tubería de diez etapas del selector, que es donde va la puerta nueva;
- `permission_epoch`, ya cableado a `app/seguridad/routes.py`: cuando cambian
  los permisos de un usuario, la memoria seleccionada bajo el epoch viejo se
  invalida sola;
- `build_redacted_summary()` de `conversation_context`, que ya resume turnos sin
  filtrar valores;
- las 11 rutas REST existentes;
- `MetricsStore`, para no inventar métricas nuevas.

---

## 4. Qué falta

| # | falta | dónde |
|---|---|---|
| F1 | `status` con ciclo de vida | columna nueva en `AssistantMemorySlot` |
| F2 | que lo `suggested` **no** llegue al prompt | una etapa más en el selector |
| F3 | auditoría de memoria | tabla nueva |
| F4 | scope empresarial | `SCOPES` + índice único |
| F5 | búsqueda de conversaciones | `history_store` + ruta |
| F6 | resumen de conversación | reutiliza el tipo `conversation_summary` |
| F7 | fijar / archivar conversación | valores nuevos de `status` + columna `pinned` |
| F8 | título automático | `history_store.ensure_conversation` |
| F9 | edición de memoria con historial | ruta PUT ya existe; falta que audite |

---

## 5. Modelo propuesto

### 5.1 Memoria — tres columnas nuevas

```sql
ALTER TABLE assistant_memory_slot ADD COLUMN status         TEXT NOT NULL DEFAULT 'approved';
ALTER TABLE assistant_memory_slot ADD COLUMN status_changed_at TEXT;
ALTER TABLE assistant_memory_slot ADD COLUMN status_by      TEXT;
```

`DEFAULT 'approved'` no es pereza: es lo que hace que la migración **no cambie
el comportamiento de ninguna fila existente**. Lo nuevo nace distinto según su
origen, no lo viejo.

```
STATUSES = suggested · approved · rejected · expired
```

### 5.2 Scope empresarial

```
SCOPES = user · conversation · company
```

Una memoria `company` no pertenece a un actor: pertenece al negocio ("el
proveedor habitual de filtros es BOSCH"). Consecuencias que **hay que decidir
antes de escribir el código**, no después:

- el índice único pasa a `(scope, IFNULL(actor_user,''), IFNULL(conversation_id,''), memory_type, key)`;
- **solo un rol autorizado puede aprobarla**, porque afecta a todos;
- al seleccionarla, el `permission_epoch` que cuenta es el del **lector**, no el
  del creador.

### 5.3 Auditoría

```sql
CREATE TABLE assistant_memory_audit (
  id TEXT PRIMARY KEY,
  slot_id TEXT NOT NULL,
  actor_user TEXT NOT NULL,          -- quién actúa
  action TEXT NOT NULL,              -- suggest|approve|reject|edit|delete|expire
  old_status TEXT, new_status TEXT,
  old_value_hash TEXT, new_value_hash TEXT,   -- HASH, nunca el valor
  reason TEXT,
  correlation_id TEXT,
  created_at TEXT NOT NULL
);
```

Se guarda el **hash** del valor, no el valor. Así la auditoría demuestra que
algo cambió sin convertirse en una segunda copia de la memoria —y sin
convertirse, por tanto, en una segunda vía de fuga.

### 5.4 Historial — cuatro columnas

```sql
ALTER TABLE assistant_conversation ADD COLUMN pinned      INTEGER NOT NULL DEFAULT 0;
ALTER TABLE assistant_conversation ADD COLUMN summary     TEXT;
ALTER TABLE assistant_conversation ADD COLUMN summary_at  TEXT;
ALTER TABLE assistant_conversation ADD COLUMN search_text TEXT;
```

`status` ya existe: basta añadir `archived` a sus valores. `search_text` es un
campo derivado y **redactado** (título + extractos ya saneados), poblado al
añadir turno; evita un FTS completo, que para este volumen no se justifica.

---

## 6. Flujo suggested → approved → rejected → expired

```
        CONVERSACIÓN
             │
             ▼
   memory_derived propone            (ya existe, con sus umbrales)
             │
             ▼
   ┌──────────────────┐
   │   SUGGESTED      │──── NO entra al prompt. NUNCA.
   └────────┬─────────┘
            │  el usuario la ve en el panel, con su evidencia
            │  (`source_turn_id` ya está en el modelo)
      ┌─────┴─────┐
      ▼           ▼
 ┌─────────┐  ┌──────────┐
 │APPROVED │  │ REJECTED │──── no se reintenta: el rechazo se recuerda
 └────┬────┘  └──────────┘
      │  entra al selector como hoy
      ▼
 ┌─────────┐
 │ EXPIRED │←──── `expires_at` vencido; lo marca `purge_expired`
 └─────────┘
```

**Quién nace con qué estado:**

| source | nace | por qué |
|---|---|---|
| `explicit` | `approved` | lo pidió el usuario con palabras |
| `ui` | `approved` | lo creó el usuario en el panel |
| `derived` | **`suggested`** | lo infirió el sistema |

Esa tabla **es** la política que pediste, y cabe en tres líneas porque el modelo
ya distingue el origen.

**El punto de control es uno solo.** `memory_selector` es el único camino por el
que una memoria llega al modelo. Una etapa más —`status == approved`— justo
después de `propiedad` cierra F2 entero. Y el `context_router` ya construido
lleva la misma puerta puesta (`APPROVED_STATUS`), de modo que cuando se reactive
no hará falta acordarse de nada.

**Un rechazo se recuerda.** Si `derived` vuelve a proponer lo mismo que el
usuario ya rechazó, no se crea otra fila: el índice único la reconduce a la
existente, que está en `rejected`. Sin esto, el sistema insistiría cada semana
con lo mismo.

---

## 7. Seguridad

| amenaza | respuesta |
|---|---|
| cruzar actores | toda consulta filtra por `actor_user`; el `company` es el único scope sin dueño y por eso exige rol para aprobarse |
| cruzar scopes | el índice único incluye el scope; el selector filtra por scope antes de rankear |
| secretos en memoria | `sanitize_memory_record` ya limpia; la auditoría guarda hash, no valor; el `context_router` rechaza claves prohibidas |
| saltarse permisos | `permission_epoch` invalida lo seleccionado bajo permisos viejos, y se revalida al leer, no al escribir |
| contaminar otra conversación | `scope=conversation` lleva `conversation_id`; el selector lo compara con el turno actual |
| memoria automática que se cuela | `suggested` no pasa el selector. Es una condición, no una convención |
| borrado no verificable | `soft_delete` deja `deleted_at`, la auditoría deja la fila, y `purge_expired` la elimina de verdad. El usuario puede pedir la prueba |

**Una amenaza que conviene nombrar:** una memoria aprobada es una afirmación que
el modelo verá en turnos futuros sin volver a comprobarla. Por eso `sensitivity`
existe y por eso la memoria **nunca es evidencia** —el prompt ya lo dice:
*"memory_hints NUNCA cubre un requirement"*. Esa frontera no se toca en 10.2.

---

## 8. Tests necesarios

**Ciclo de vida:** nace `suggested` si `derived`; nace `approved` si `explicit` o
`ui`; aprobar cambia estado y deja auditoría; rechazar también; una propuesta
repetida de algo rechazado no resucita la fila.

**La puerta:** una memoria `suggested` **no** aparece en `memory_hints`; una
`approved` sí; una `expired` no; una `rejected` no.

**Aislamiento:** memoria de otro actor nunca se selecciona; una de
`scope=conversation` no aparece en otra conversación; una `company` exige rol
para aprobarse.

**Auditoría:** cada transición deja fila; la fila guarda hash y no valor; el
borrado conserva la auditoría.

**Historial:** búsqueda solo devuelve conversaciones del actor; archivar no
borra; fijar no altera el orden de retención; el resumen no filtra cifras que el
turno no publicó.

**Migración:** con las columnas nuevas y `DEFAULT 'approved'`, el comportamiento
de las filas existentes es idéntico. Es el test que autoriza desplegar.

---

## 9. Impacto en tokens

**Cero, o negativo.** Y esto importa porque es la restricción que gobierna toda
la Fase 10:

- el historial **no entra al prompt**. Lo que entra hoy son los 6 turnos
  recientes del resolver de anáfora, y eso no cambia;
- la memoria entra por el selector, que ya tiene presupuesto en caracteres;
- **la memoria `suggested` deja de entrar**, así que el prompt sólo puede
  encoger: hoy lo `derived` entra automáticamente;
- el system prompt **no se toca**;
- el Context Router sigue apagado.

Es una unidad que añade capacidad sin tocar el techo de 11.764 tokens. Eso la
hace compatible con el estado estable, que es justo lo que 10.1 no consiguió.

---

## 10. Roadmap 10.2

| unidad | qué | riesgo |
|---|---|---|
| **10.2.1** | `status` + migración con `DEFAULT 'approved'` | bajo: nada cambia hasta que algo nazca `suggested` |
| **10.2.2** | la puerta en el selector + `derived` nace `suggested` | **medio**: cambia qué memoria ve el modelo. Medir |
| **10.2.3** | auditoría | bajo: sólo escribe |
| **10.2.4** | panel: ver, aprobar, rechazar, editar, borrar | bajo |
| **10.2.5** | historial: búsqueda, fijar, archivar, título | bajo |
| **10.2.6** | resumen de conversación | medio: reutiliza `conversation_summary` |
| **10.2.7** | scope `company` | **alto**: toca el índice único y exige rol |

---

## 11. Orden recomendado

**10.2.1 → 10.2.2 → 10.2.3 → 10.2.4** es el núcleo, y en ese orden porque cada
paso deja el sistema coherente:

1. la columna primero, sin cambiar comportamiento;
2. la puerta después, midiendo qué memoria deja de entrar;
3. la auditoría antes que el panel, para que la primera aprobación ya quede
   registrada;
4. el panel al final del núcleo, porque sin él nadie puede aprobar y toda la
   memoria derivada quedaría muerta en `suggested`.

**10.2.5 y 10.2.6** son independientes y se pueden intercalar cuando convenga.

**10.2.7 (`company`) va al final y merece su propia unidad.** Toca el índice
único —una migración con datos— y abre una pregunta que no es técnica: quién
tiene autoridad para afirmar algo en nombre del negocio.

### Dos cosas que decidí no proponer

**Resumen automático de conversaciones largas en cada turno.** Cuesta una llamada
al modelo por turno y hoy `build_redacted_summary` ya cubre la anáfora sin
coste. La propuesta es resumir **bajo demanda o al archivar**, no en caliente.

**Búsqueda de texto completo (FTS5).** Para este volumen, un `search_text`
derivado y un `LIKE` con índice bastan. Añadir FTS traería una tabla virtual,
triggers de sincronización y una migración, a cambio de una latencia que nadie
está notando.

---

## 12. Lo que falta para 10.2.3 (medido al cerrar 10.2.2)

La puerta de aprobación ya funciona y está detrás de
`ANDES_ASSISTANT_MEMORY_APPROVAL`. Lo que **no** existe todavía es la forma de
aprobar. Con la bandera encendida y sin esto, toda la memoria derivada nace
`suggested` y se queda ahí para siempre: el sistema deja de aprender en vez de
aprender con permiso.

### El lado de lectura ya está completo

`GET /api/memory` devuelve las filas tal como las proyecta `_public`, y desde
10.2.1 eso incluye `status`, `status_changed_at` y `status_by`. Un panel puede
listar lo pendiente hoy mismo, sin tocar el backend.

### El lado de escritura es el que falta

`PUT /api/memory/<slot_id>` existe (`app/assistant/routes.py:479`) pero **no
puede cambiar el estado**, y no por un olvido de una línea:

1. **No lee `status` del payload.** Construye la llamada con `memory_type`,
   `key`, `value` y `scope`, y nada más.
2. **Pasa por `write_explicit_memory`, que no tiene parámetro `status`.** La
   ruta no podría reenviarlo aunque lo leyera.
3. **La respuesta pública tampoco lo devuelve** (`id`, `type`, `key`, `value`,
   `scope`, `conversation_id`).

O sea: la columna se puede escribir desde `MemoryStore.upsert`, pero no hay
ningún camino HTTP que llegue hasta ella.

### Lo que 10.2.3 tiene que decidir, no solo implementar

- **Endpoint propio, no un campo más en el PUT.** `PUT /api/memory/<id>` es
  "corrige el contenido"; aprobar es otro acto y merece
  `POST /api/memory/<id>/approve` y `/reject`. Mezclarlos haría que una
  corrección de texto pudiera aprobar de paso, que es justo lo que 10.2.2
  impide dentro del store (una actualización de valor no reetiqueta el estado).
- **Qué transiciones son legales.** `suggested → approved|rejected` es el caso
  central. `approved → rejected` es retirar permiso y debería poder hacerse.
  `expired → approved` probablemente no: resucitar por estado saltaría el TTL.
- **`status_by` no puede venir del cliente.** Es el actor de la sesión, como
  `actor_user`. La lista de campos prohibidos del PUT ya tiene el patrón.
- **La auditoría es parte de la unidad, no un extra.** Una aprobación es una
  decisión sobre lo que el modelo podrá usar; sin registro no hay forma de
  responder "¿quién dejó entrar esto?".

### Lo que NO hace falta tocar

El selector, la política de `derived`, el esquema y la telemetría de sombra ya
están. 10.2.3 es una superficie HTTP más una decisión de transiciones.

---

## 13. FASE 10.2.3 — el motor de aprobación (implementado)

Lo que el §12 describía como pendiente ya existe. Resumen de lo decidido, para
que 10.2.4 no tenga que reabrirlo.

### Contrato

```
POST /assistant/api/memory/<slot_id>/approve
POST /assistant/api/memory/<slot_id>/reject

body: { "version": "<obligatoria>",
        "reason": "<opcional, ≤200>",
        "correlation_id": "<opcional>",
        "conversation_id": "<obligatoria si scope=conversation>",
        "reconsider": true,   // solo approve, para rejected → approved
        "revoke": true }      // solo reject,  para approved → rejected
```

`GET /api/memory` devuelve ahora una `version` por fila. Es el testigo que el
panel tiene que enviar de vuelta.

**Campos prohibidos en el body**: `actor_user`, `status`, `status_by`,
`permission_epoch`, `sensitivity`, `source`, `Authorization`, `token`. El actor
sale de la sesión; el `source` lo fija el servidor.

### Transiciones

| desde | approve | reject |
|---|---|---|
| `suggested` | → `approved` | → `rejected` |
| `approved` | no-op idempotente | **exige `revoke`** |
| `rejected` | **exige `reconsider`** | no-op idempotente |
| `expired` | 409 terminal | 409 terminal |

`expired` es terminal a propósito: aprobar no puede resucitar lo que el TTL
cerró, porque eso sería usar el estado para saltarse otra puerta — justo la
propiedad que 10.2.2 garantiza. Una memoria borrada o caducada por TTL ni se
lee: responde `not_found`.

### Concurrencia

Optimista, con un testigo opaco (`memory_version`) que cubre id, estado, marcas
de tiempo **y contenido**. Incluir el contenido es lo que impide que una
corrección hecha entremedio quede aprobada sin que nadie la haya visto.

La comprobación vive **dentro** de `BEGIN IMMEDIATE`, no antes: leer fuera y
escribir después deja una ventana. Hay un test que lee el fuente y afirma el
orden `BEGIN → comprobar → UPDATE`.

### Auditoría

`event=assistant_memory_status_change` con `memory_id`, `actor_user`,
`previous_status`, `new_status`, `ts`, `reason`, `source`, `correlation_id`,
`permission_epoch`, `operation`, `result`, `changed`. **Sin `value_json`**: para
responder "quién dejó entrar esto" basta el id, y duplicar el contenido en un
log append-only solo multiplica los sitios donde puede filtrarse. Los intentos
bloqueados también se auditan, con `result` = el código de error.

### Lo que falta para 10.2.4 (panel)

Backend: nada. Las tres piezas que el panel necesita —listar con `status` y
`version`, aprobar, rechazar— existen y están probadas.

Queda decidir en la UI:

1. **Dónde vive la bandeja.** Una vista propia de "memorias sugeridas" o una
   sección dentro del panel de memoria ya existente.
2. **Qué se muestra de cada sugerencia.** El valor se puede mostrar (es del
   usuario), pero la lista debe dejar claro de qué conversación salió y por qué
   el sistema la infirió.
3. **Cómo se presenta el conflicto de versión.** Es el único error que el
   usuario verá con alguna frecuencia; la respuesta trae `status_actual` para
   poder decir "alguien ya la rechazó" en vez de un 409 genérico.
4. **Si `reconsider` y `revoke` se exponen.** Recomendación: sí, pero como
   acciones secundarias con confirmación, no como el botón principal.
5. **CSRF.** Las rutas son POST con sesión; el panel debe usar el mismo
   mecanismo que el resto del ERP.
6. **Encender la bandera.** `ANDES_ASSISTANT_MEMORY_APPROVAL=1` solo tiene
   sentido una vez que el panel existe: antes de eso, la memoria derivada nace
   `suggested` y nadie puede aprobarla.

---

## 14. FASE 10.2.4 — el panel (implementado)

### Arquitectura: la que ya había

El asistente vivía en un drawer con dos vistas —conversación e historial—
conmutadas por una sola función, `applyView()`. La memoria es la **tercera
vista**, no un panel administrativo aparte:

```
Assistant (drawer)
 ├─ Conversación
 ├─ Historial
 └─ Memoria
     └─ Pendientes · Aprobadas · Rechazadas · Expiradas
```

Se mantiene **una sola** `applyView()`. Tener dos sitios que oculten paneles es
exactamente como aparecen los estados donde se ven dos a la vez.

Capas, con el mismo reparto que ya existía:

| capa | archivo | qué |
|---|---|---|
| proyección | `orchestrator/memory_panel.py` | qué sale del servidor |
| ruta | `assistant/routes.py` | `GET /api/memory/panel` |
| datos | `static/js/assistant_service.js` | `loadMemoryPanel`, `moderateMemory` |
| vista | `static/js/assistant_memory.js` | pinta y modera |
| esqueleto | `templates/assistant/_memory.html` | sin interpolación |
| estilo | `static/css/assistant.css` | tokens `--ap-as-*` ya existentes |

### Por qué una ruta de listado aparte

`GET /api/memory` devuelve la fila pública: incluye `actor_user` y
`permission_epoch`, oculta las caducadas por TTL y no trae recuentos. Cambiarle
la forma rompería su contrato de 7B.3. `GET /api/memory/panel` proyecta campo
por campo, incluye las caducadas —sin ellas la pestaña Expiradas estaría siempre
vacía— y cuenta por estado.

### La proyección falla cerrada

Cada tipo declara qué se muestra. Un tipo **no declarado no vuelca su valor**:
agregar un tipo nuevo sin tocar `memory_panel.py` lo deja invisible, no
filtrado. Lo mismo con el contenido prohibido: si el valor tuviera algo que el
selector descartaría, el panel no lo muestra y avisa de que aprobarla no
serviría de nada.

### El estado que se muestra es el efectivo

El TTL corre antes que la puerta de estado. Una memoria `approved` con
`expires_at` vencido aparece como **Expirada**, con `stored_status` aparte.
Mostrarla como aprobada sería mentir sobre lo que el sistema hace con ella.

### Lo que el panel no hace

- **No reintenta.** Ante un conflicto de versión muestra "esta memoria cambió
  desde que la abriste" y un botón Actualizar. Reintentar con una versión nueva
  sería aprobar algo que nadie vio.
- **No mueve la tarjeta antes de que el servidor confirme.** Un movimiento
  optimista diría "aprobada" sobre algo que quizá falló.
- **No manda** `actor_user`, `status`, `status_by`, `permission_epoch`,
  `sensitivity` ni `source`. La ruta los rechaza con `forbidden_field`.

### Lo que falta para 10.2.5

1. **`previous_status` en la fila.** Hoy el panel muestra "Aprobada por X el Y";
   la flecha `anterior → nuevo` solo aparece tras actuar en la sesión, porque el
   estado anterior vive únicamente en la auditoría y §9 pide no exponerla. Una
   columna `previous_status` lo resolvería sin abrir el log.
2. **Paginación.** El listado va a 100 filas. Con el volumen actual sobra.
3. **Encender la bandera.** Requiere decidir el momento, no más código.

---

## 15. FASE 10.2.5 — endurecimiento (implementado)

Los tres puntos que 10.2.4-A dejó abiertos. Ninguno cambia el contrato HTTP ni
el comportamiento del selector, el motor de aprobación o el escritor derivado.

### 1. La transición sale de la auditoría, no de una columna

`previous_status` **no** se persiste en la fila. Guardarlo sería inventar estado
para pintar una línea, y duplicaría peor lo que el evento
`assistant_memory_status_change` ya registra. `ultimas_transiciones(actor)` lee
la **cola** del log (256 KB) y proyecta cinco campos: `previous_status`,
`new_status`, `actor_user`, `ts`, `result`. Nada más: `correlation_id`,
`permission_epoch` y `source` son diagnóstico interno.

Solo cuentan los cambios que ocurrieron (`result=ok` y `changed=true`). Un
conflicto de versión está en el log —y debe estarlo— pero no es historia del
registro: pintarlo diría que algo cambió cuando no cambió nada.

El filtro por actor es hoy redundante; si mañana existiera moderación por un
tercero, esa línea hace que el panel deje de mostrar la transición en vez de
enseñar la de otro.

**Consecuencia verificada**: la línea "Pendiente → Aprobada" sobrevive a
recargar la página, porque la fuente es el log y no el estado del navegador.

### 2. Modal propio

`window.confirm`/`window.prompt` fuera del flujo. El diálogo vive como hijo
directo del drawer —dentro de `.ap-assistant-body` quedaría recortado por su
`overflow:auto` y se desplazaría con la lista— y muestra **los mismos campos de
la tarjeta**, así que la decisión se toma mirando el contenido.

Escape cierra el modal y **no** el drawer: el manejador va en fase de captura y
detiene la propagación antes del Escape de `assistant.js`. El foco entra en el
control donde se va a actuar, el tabulador no sale del diálogo, y al cerrar
vuelve al botón que lo abrió.

El error del backend se muestra **dentro** del diálogo sin cerrarlo, para no
perder el contexto de qué se estaba decidiendo. El 409 reemplaza el contenido
por el mensaje unificado del servidor y una única acción, `Actualizar`. Nunca
se reintenta con una versión nueva.

### 3. Tres orígenes, tres frases

| origen | firma | qué dice la tarjeta |
|---|---|---|
| `human` | `status_by` presente | "Aprobada por X · fecha" |
| `migration` | `status_changed_at` NULL | "Disponible desde la migración inicial… Nadie la aprobó." |
| `system` | fecha sí, persona no | "La guardó el asistente y nadie la ha revisado todavía." |

`status_changed_at IS NULL` es la firma exacta de la migración de 10.2.1: el
`ALTER TABLE` + `UPDATE` puso `approved` y no tocó las marcas, mientras que todo
`upsert` posterior siempre escribe la fecha. Las cinco filas reales encajan ahí,
y **no se modificó ninguna**.
