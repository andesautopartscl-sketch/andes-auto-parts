# FASE 10 — Arquitectura del Asistente Operativo

> Estado: **propuesta de arquitectura**. No hay implementación asociada a este
> documento. Escrito sobre el árbol en `0b3bcfea` (checkpoint Fase 9).

---

## 0. La restricción que manda sobre todo lo demás

Antes de cualquier decisión de diseño hay un número medido:

```
max_total_tokens observado: 11 764 / 12 000     holgura: 236 tokens (2 %)
turno responsable: T09 — 3 herramientas, 5 pasos
```

La Fase 9 dejó el sistema a un 2 % del techo. La Fase 10 propone añadir once
subsistemas. **Si cada uno aporta contexto al prompt, el sistema se rompe antes
de entregar valor.** No es un riesgo a vigilar: es la primera restricción de
diseño, y ordena el roadmap.

De ahí sale la regla que atraviesa este documento:

> **Ningún subsistema nuevo entra al prompt por defecto.** Entra cuando el
> Context Router demuestra que este turno lo necesita, y con un presupuesto
> asignado. Lo demás vive en la base de datos y se consulta por herramienta.

Por eso el orden de implementación empieza por el router y no por las
capacidades, aunque las capacidades sean lo que se ve.

---

## 1. Qué existe ya — inventario medido

Esta es la parte más importante del estudio: **buena parte de las capas 1, 2 y
13 ya están construidas**, y rehacerlas sería el error más caro de la fase.

| capa | estado | evidencia |
|---|---|---|
| **1 · Historial** | **~80 % existe** | `AssistantConversation` + `AssistantTurn`; `history_store.py` (811 líneas) con `ensure_conversation`, `append_turn`, `list_conversations`, `list_turns`, `recent_turns_for_resolver`, `soft_delete_conversation`, `purge_expired`; **5 rutas REST** |
| **2 · Memoria** | **~85 % existe** | `AssistantMemorySlot` con `scope, actor, key, value, source, confidence, created_at, updated_at, expires_at, permission_epoch, sensitivity`; 7 módulos (`store, schema, selector, explicit, derived, epoch, sanitize`); **6 rutas REST** |
| 3 · Vault | **0 %** | no hay `cryptography` ni en `requirements.txt`; cero uso de cifrado en `app/` |
| 4 · Agenda | **0 % como fuente de verdad** | ver §4 |
| 5 · Tasks | 0 % | — |
| 6 · Approvals | **~20 %** | `detect_write_intent()`, `WRITE = False` en las 13 tools, `write_not_allowed` en el normalizador |
| 7 · Documentos | 0 % para el asistente | hay uploads en otros módulos (`admin`, `productos`, `rrhh`), no reutilizables tal cual |
| 8 · Snapshot | 0 % | — |
| 9 · Briefing | 0 % | — |
| 10 · Market intel | 0 % | sin capa de egress |
| 11 · Context Router | **~30 %** | `memory_selector` ya selecciona con presupuesto de caracteres; `EvidenceStore.prompt_pack()` |
| 12 · Capability Router | **~40 %** | `model_facing_tools()`, `_GATED_TOOLS`, contratos comprimidos (9.1) |
| **13 · Observability** | **~90 % existe** | `metrics.py` (551 líneas, ~60 campos por turno), `audit.py`, `correlation_id` extremo a extremo |
| 14 · Security | parcial | ACL por actor en el Gateway, `permission_epoch` conectado al ERP, `input_guard` |
| 15 · UX | drawer existe | `assistant.js` + 7 plantillas, con estado de panel y toggle lateral |

**Tres piezas que hay que reutilizar sí o sí:**

1. **`permission_epoch`.** Ya está cableado: `app/seguridad/routes.py` llama a
   `notify_permission_context_changed()` cuando cambian los permisos de un
   usuario, y la memoria invalida lo que se seleccionó bajo un epoch viejo. Ese
   mecanismo debe extenderse a Vault, Agenda, Tasks y Approvals — no reinventarse.
2. **El `EvidenceStore` y su procedencia.** Todo lo que un subsistema nuevo
   aporte al modelo debe entrar como evidencia con `evidence_id`, o el verifier
   no puede comprobarlo y las tarjetas no pueden citarlo.
3. **El `MetricsStore`.** Ya emite ~60 campos por turno. Cada subsistema nuevo
   añade campos ahí, no un sistema de métricas propio.

---

## 2. Arquitectura propuesta

```
                        ┌──────────────────────────────┐
  navegador ──────────▶ │  BFF  /assistant/api/*       │  actor de sesión
                        │  rate limit · rechazo de     │  nunca acepta tool
                        │  tool/actor del cliente      │
                        └──────────────┬───────────────┘
                                       │
                        ┌──────────────▼───────────────┐
                        │      CONTEXT ROUTER          │  ◀── la pieza nueva
                        │  decide QUÉ entra al prompt  │      que hace viable
                        │  con presupuesto por bloque  │      todo lo demás
                        └──────────────┬───────────────┘
                                       │
          ┌────────────────────────────┼────────────────────────────┐
          │                            │                            │
  ┌───────▼────────┐          ┌────────▼────────┐          ┌────────▼────────┐
  │ CAPABILITY     │          │   AGENT LOOP    │          │  ANSWER PATH    │
  │ ROUTER         │─tools──▶ │  1 decisión =   │─claims─▶ │ verifier ·      │
  │ core+opcional  │          │  1 herramienta  │          │ composer ·      │
  └────────────────┘          └────────┬────────┘          │ response_shape  │
                                       │                   └─────────────────┘
                      ┌────────────────┼────────────────┐
                      │                │                │
              ┌───────▼──────┐  ┌──────▼──────┐  ┌──────▼───────┐
              │   LECTURA    │  │  PROPUESTA  │  │  EJECUCIÓN   │
              │ Gateway READ │  │  Approval   │  │  Action      │
              │ (13 tools)   │  │  Engine     │  │  Executor    │
              └──────────────┘  └─────────────┘  └──────┬───────┘
                                                        │ resuelve secretos
                                                 ┌──────▼───────┐
                                                 │ SECRET BROKER│
                                                 │  → Vault     │
                                                 └──────────────┘

  PERSISTENCIA (fuente de verdad, fuera del prompt)
  ├── historial   · conversaciones y turnos
  ├── memoria     · slots con epoch y expiración
  ├── agenda      · eventos / recordatorios
  ├── tasks       · jobs con estado y progreso
  ├── approvals   · propuestas firmadas
  ├── documentos  · originales + extracciones
  └── vault       · metadata separada del ciphertext

  SCHEDULER (ejecutor tonto, sin estado propio)
  └── poll → tasks(pending, due) → Action Executor
```

**Las tres fronteras que definen la fase:**

| frontera | regla |
|---|---|
| **prompt ↔ persistencia** | La persistencia no entra al prompt. El Context Router decide qué fragmento entra, con presupuesto. |
| **propuesta ↔ ejecución** | El modelo propone; nunca ejecuta. Entre ambos hay una aprobación humana firmada sobre un objetivo inmutable. |
| **ejecución ↔ secretos** | Los secretos se resuelven **después** de la aprobación, **dentro** del ejecutor. El bucle de decisión nunca los ve. |

---

## 3. Historial vs Memoria — la distinción, hecha explícita

El usuario pide no mezclarlos. La distinción operativa que propongo:

|  | Historial | Memoria |
|---|---|---|
| qué es | lo que **ocurrió** | lo que el sistema **debe recordar** |
| verdad | inmutable, append-only | mutable, con versión |
| caducidad | retención por política (`retention_until`) | expiración por hecho (`expires_at`) |
| entra al prompt | **no**, salvo los N turnos recientes del resolver | sí, seleccionado y con presupuesto |
| se crea | automáticamente, en cada turno | **nunca automáticamente sin política** |
| borrarlo | pierde trazabilidad | no pierde nada del pasado |

**Política de creación de memoria** — tres fuentes, tres tratos distintos. Hoy
`SOURCES = {explicit, derived, ui}`. Propongo añadir un estado, no una fuente:

```
explicit  → el usuario lo pidió           → activa de inmediato
ui        → el usuario la creó en el panel → activa de inmediato
derived   → el sistema la infirió          → NACE 'suggested', no activa
```

Una memoria `suggested` **no se selecciona para el prompt**. Aparece en el panel
con su evidencia (`source_turn_id` ya existe en el modelo) y el usuario la
promueve o la descarta. Esto responde directamente a *"no guardar como memoria
permanente simplemente porque apareció en una conversación"* y cuesta **una
columna** (`status`) sobre un modelo que ya tiene todo lo demás.

**Lo que falta en Historial** (y es poco): `summary`, `pinned`, búsqueda de
texto, y `archived` como estado distinto de `deleted`. El modelo ya tiene
`status`, así que archivar es un valor nuevo, no una columna.

---

## 4. Agenda — por qué APScheduler no puede ser la fuente de verdad

Medido en `app/__init__.py:100-131`:

- es `BackgroundScheduler`, **sin jobstore**: el estado vive en memoria;
- tiene **un job cableado** (backup a GDrive a las 10:00);
- muere con el proceso, y `atexit` lo apaga;
- **se salta entero en modo debug** salvo que `WERKZEUG_RUN_MAIN == "true"`;
- con varios workers, cada uno tendría su propia copia.

Un recordatorio que vive ahí se pierde en el primer reinicio y se dispara N veces
con N workers. La arquitectura correcta invierte la relación:

```
agenda (tabla)  ──genera──▶  tasks (tabla)  ◀──lee── scheduler (tonto)
  intención humana            trabajo con estado        solo dispara
```

El scheduler pasa a ser un bucle que pregunta *"¿qué tareas vencieron?"* y las
entrega al ejecutor. **No sabe qué son.** Consecuencias que lo hacen correcto:

- sobrevive a reinicios porque el estado está en la base;
- con varios workers hace falta un *lease* (`locked_by`, `locked_until`) para
  que una tarea la tome uno solo — esto **hay que diseñarlo desde el principio**,
  no después;
- se puede reprogramar, cancelar y auditar, porque hay filas.

**Agenda vs Tasks** — la separación que pide el usuario, con un criterio:

> La **agenda** expresa intención humana situada en el tiempo ("reunión el
> viernes"). Una **task** es trabajo que el sistema ejecuta. Un recordatorio es
> una entrada de agenda que, al vencer, **crea** una task de notificación.

Una reunión no es una task: nadie la "ejecuta". Un "todos los lunes revisa las
ventas" sí genera una task recurrente.

---

## 5. Approval Engine — la pieza que hace segura toda la fase

Sin esto, nada de WRITE puede existir. Tres propiedades no negociables:

**(a) La propuesta es inmutable y firmada sobre su objetivo.**
"Encontré 18 productos para actualizar" debe congelar *cuáles* 18. La propuesta
guarda `target_hash` = hash del conjunto exacto. Si al aprobar el conjunto
cambió, la aprobación **caduca** y hay que reproponer. Esto cierra el
*stale approval* que el usuario lista como amenaza.

**(b) La propuesta lleva su simulación, no su promesa.**
El campo `impact` no es prosa del modelo: es el resultado de un **dry-run**
determinista contra el ERP — "18 filas afectadas, 3 sin cambio real, 0
conflictos". Sin dry-run, "18 productos" es una afirmación sin evidencia, y
todo lo que construimos en Fase 9 dice que eso no se publica.

**(c) La aprobación la concede una persona, no un mensaje.**
"Aprobar" escrito en el chat es *entrada no confiable*: un documento o una
página web podrían contenerlo. La aprobación se concede con un **acto de
interfaz** sobre un `proposal_id` concreto, no interpretando texto libre.

```
proposal
  id · actor · action · target_ref · target_hash · arguments_json
  impact_json (dry-run) · reversible (bool) · inverse_action (nullable)
  status: draft|pending|approved|rejected|expired|executing|done|failed
  created_at · expires_at · approved_by · approved_at
  idempotency_key · correlation_id · result_json
```

`reversible` e `inverse_action` son aportación mía y los justifico en §16.

---

## 6. Vault y Secret Broker

**Modelo de datos, con la separación que el usuario exige:**

```
vault_secret            (metadata — consultable, nunca sensible)
  id · name · kind · scope · owner · created_at · rotated_at
  expires_at · revoked_at · last_used_at · version

vault_material          (ciphertext — nunca sale de aquí)
  secret_id · version · ciphertext · nonce · wrapped_dek · alg
```

**Cifrado en dos niveles.** Cada secreto se cifra con su propia DEK (data
encryption key); la DEK se guarda envuelta por una KEK (key encryption key) que
**no está en la base de datos**. La KEK viene del entorno o de un fichero con
permisos restringidos, y en el futuro de un KMS. Consecuencia práctica: robar
`andes.db` no basta para leer los secretos.

**Sobre "gestionada fuera de SQLite" — sí, y hay una razón concreta medida en
esta sesión.** Este repositorio tiene una trampa documentada: `create_app()` se
ejecuta al importar el paquete y llama `load_project_dotenv(force=True)`, de
modo que **`.env` pisa el entorno del proceso**. Una KEK leída de una variable
de entorno puede ser sustituida silenciosamente por lo que diga `.env`. Por eso
la KEK debe: (1) leerse **una vez**, al arranque, antes de cualquier recarga;
(2) validarse contra un *check value* almacenado; y (3) **fallar el arranque del
Vault** si no cuadra, en vez de degradarse. Un Vault que arranca con la llave
equivocada y cifra con ella es peor que uno que no arranca.

**Secret Broker — el contrato con el modelo.** El modelo nunca nombra un
secreto. Nombra una **capacidad**:

```
modelo:    action = "publicar_precio_mercadolibre", target = ...
              ↓ (aprobación humana)
ejecutor:  necesita capability "mercadolibre.write"
              ↓
broker:    resuelve scope+actor+capability → secret_id → descifra en memoria
              ↓
cliente:   usa el material · lo borra · registra vault_access(quien,cuando,para_qué)
```

Propiedades: no existe `get_secret` como herramienta del modelo; no existe
`get_all_secrets` en ninguna capa; el material nunca cruza la frontera del
ejecutor; y **el broker solo responde a acciones aprobadas**, lo que significa
que el Vault debe implementarse **después** del Approval Engine, no antes.

**Redacción como red de seguridad, no como control primario.** El arnés ya tiene
`_scrub()` en `evals/fase81g_closure.py`, que sustituye el valor de
`ANDES_LLM_API_KEY` en cualquier artefacto. Ese patrón se generaliza: un
*scrubber* central que ningún log, métrica ni artefacto puede saltarse. Pero es
la segunda línea: la primera es que el material nunca llegue ahí.

---

## 7. Documentos

```
document        id · owner · company · kind · title · created_at · status
document_version  document_id · version · sha256 · bytes · stored_path
                  uploaded_by · uploaded_at
document_extract  version_id · extractor · schema_version · payload_json
                  confidence · extracted_at
```

Tres decisiones:

- **el original siempre se conserva**; las extracciones son derivadas y
  re-generables. El `sha256` por versión da el versionado y la comparación.
- **la extracción cita su origen**: cada dato extraído lleva `version_id`, para
  que una afirmación del asistente pueda rastrearse hasta una página de un PDF.
- **un documento de proveedor es entrada no confiable.** Su texto **no** entra
  al prompt de decisión como instrucciones. Entra como evidencia con
  `evidence_id`, igual que la salida de una tool, y el verifier lo trata igual.
  Esto cierra el *malicious document* de la lista de amenazas.

---

## 8. Business Snapshot y Briefing

El **snapshot** es una composición de lecturas que ya existen (ventas, stock,
compras, pedidos) más agenda y tasks. No introduce herramientas nuevas.

La regla dura, heredada de Fase 9: **ninguna alerta sin regla explícita y
evidencia.** Una alerta es una fila:

```
alert_rule   id · name · expresión determinista · umbral · severidad · activa
alert        rule_id · evidence_ids · valores_observados · created_at
```

Si no hay regla, no hay alerta. Esto impide exactamente lo que el usuario teme:
que el modelo "priorice" por su cuenta. El modelo **redacta** el briefing; no
decide qué es importante.

El **briefing** es entonces un `response_kind` nuevo, no un subsistema:
`"Buenos días bro, ¿cómo estamos comenzando?"` se clasifica como intención de
briefing, se compone el snapshot, y se publica con `fuentes`, `timestamp` y
**cobertura** — qué señales estaban disponibles y cuáles no. Un briefing que
calla que el ERP no respondió es un briefing que miente.

---

## 9. Context Router — el corazón de la fase

Presupuesto por bloque, negociado contra el techo:

| bloque | presupuesto | cuándo entra |
|---|---|---|
| system + contratos core | ~1 200 tok | siempre |
| capacidades opcionales | 0–400 tok | solo si el router las activa |
| evidencia del turno | ≤ 6 000 chars | ya existe (`MAX_EVIDENCE_PROMPT_CHARS`) |
| memoria seleccionada | ≤ 600 tok | ya existe con presupuesto |
| historial reciente | ≤ 400 tok | solo anáfora (`recent_turns_for_resolver`) |
| agenda | ≤ 300 tok | solo si la pregunta es temporal |
| tasks | ≤ 200 tok | solo si hay tasks del actor en curso |
| documentos | ≤ 800 tok | solo si la pregunta los referencia |

**Criterio de selección: determinista primero, modelo nunca.** El router no
pregunta al modelo qué contexto necesita — eso costaría un turno y sería
circular. Usa detectores cerrados, como los que ya existen (`analytical_intent`,
`resolve_period`, `detect_write_intent`). Si un detector no dispara, el bloque no
entra.

**Instrumentación obligatoria desde el primer día:** cada turno registra qué
bloques entraron y cuántos tokens costó cada uno. Sin eso, el presupuesto es una
intención. Con eso, se puede medir A/B igual que medimos las banderas en Fase 9.

---

## 10. Capability Router

El problema es real y está medido: en Fase 9, ORDERS cuesta **+37 tokens en
todos los turnos** por una capacidad que **0 de 76 casos eligieron**. Con 30
herramientas, el catálogo solo se come el presupuesto.

```
CORE      siempre visibles, pocas, las que cubren el 80 % de las preguntas
          (inventario, producto, ventas, movimientos, catálogo)
OPTIONAL  se inyectan cuando un detector determinista las activa
          (pedidos, equivalencias, KPIs, agenda, documentos)
GATED     requieren bandera Y aprobación (cualquier WRITE)
```

La infraestructura ya está a medias: `model_facing_tools()` filtra por banderas y
`format_contracts_for_prompt(only=...)` ya acepta un subconjunto. Falta el
**activador por intención** y la medición de su acierto (¿cuántas veces el
router ocultó una tool que el turno necesitaba?). Esa métrica —*capability
miss*— es la que dice si el router es seguro.

---

## 11. Seguridad — amenazas y respuesta

| amenaza | respuesta arquitectónica |
|---|---|
| **prompt injection** (documento, web, campo de ERP) | contenido externo entra como **evidencia**, nunca como instrucción; el verifier lo trata igual que una tool |
| **tool abuse** | allowlist derivada; el navegador no elige tool; `WRITE=False` en las 13 actuales |
| **write escalation** | toda escritura pasa por propuesta + aprobación humana con `target_hash` |
| **stale approval** | `target_hash` + `expires_at`; si el objetivo cambió, caduca |
| **replay** | `idempotency_key` por propuesta; el ejecutor rechaza repeticiones |
| **secret leakage** | material nunca en prompt, log, métrica ni artefacto; scrubber central como segunda línea; `vault_access` audita cada uso |
| **exfiltración** | sin egress hasta la Capa 10; cuando exista, allowlist de destinos y **prohibición de enviar datos del ERP a terceros** sin aprobación |
| **contaminación entre conversaciones** | memoria con `scope` + `conversation_id`; el selector ya filtra |
| **aislamiento por actor** | ACL en el Gateway con el actor de sesión; `permission_epoch` invalida lo seleccionado bajo permisos viejos |
| **aprobación falsificada** | la aprobación es un acto de interfaz sobre un `proposal_id`, **no** texto en el chat |

Una amenaza que el usuario no listó y que la Fase 10 introduce:
**escalada por tarea en segundo plano.** Una task creada bajo los permisos de hoy
podría ejecutarse mañana, cuando el actor ya no tiene ese permiso. Respuesta: la
task guarda el `permission_epoch` de creación y **revalida los permisos en el
momento de ejecutar**, no en el de crear.

---

## 12. UX — estructura propuesta

El drawer actual (7 plantillas, `assistant.js`) es una sola vista de
conversación. Propuesta: **no** convertirlo en un panel de administración.

```
┌─ Andes Assistant ─────────────┐
│ [conversación]  ▾ historial   │  ← un selector, no pestañas
├───────────────────────────────┤
│                               │
│   hilo de conversación        │
│   + tarjetas de evidencia     │
│                               │
│   ┌─────────────────────────┐ │
│   │ ⚠ Aprobación pendiente  │ │  ← la aprobación vive EN el hilo,
│   │ 18 productos · ver → │ ✓│ │     donde está el contexto
│   └─────────────────────────┘ │
├───────────────────────────────┤
│ [ escribir…            ] ➤    │
└───────────────────────────────┘
        ⋯ menú:  Memoria · Agenda · Tareas · Documentos · Bóveda
```

Principio: **lo que necesita contexto conversacional vive en el hilo**
(aprobaciones, tarjetas, evidencia). Lo que es gestión vive detrás de un menú y
se abre como vista completa, no como pestaña apretada en un drawer de 400 px.

La Bóveda merece una excepción: **no se administra desde el asistente.** Vive en
el módulo de seguridad del ERP, con su propia autenticación. El asistente solo
la *usa* a través del broker.

---

## 13. Observabilidad

`metrics.py` ya emite ~60 campos por turno. Cada subsistema añade los suyos al
mismo metric, con la misma disciplina de Fase 9: **contadores y nombres, nunca
contenido**.

Campos nuevos mínimos: `context_blocks[]` y su coste en tokens; `capability_miss`;
`proposal_id` y `approval_latency_ms`; `task_id`, `retries`, `duration_ms`;
`vault_access_count` (nunca qué secreto); `document_ids` citados.

Y una regla operativa que esta sesión aprendió a base de perder corridas: **toda
medición larga se lanza desacoplada, con salida a fichero**, y su brazo se
verifica leyendo el artefacto, no lo que se creyó exportar.

---

## 14. Riesgos

| # | riesgo | probabilidad | mitigación |
|---|---|---|---|
| R1 | **techo de tokens** — la fase lo cruza | **alta** | Context Router primero; medir cada bloque; A/B por subsistema como en Fase 9 |
| R2 | rehacer historial/memoria por no inventariar | media | este documento; §1 es la respuesta |
| R3 | Vault antes que Approvals → secretos usables sin puerta | media | orden de implementación §15 |
| R4 | `.env` pisa la KEK y el Vault cifra con llave equivocada | **alta** | *check value* y fallo duro al arranque |
| R5 | SQLite + jobs en segundo plano + web → bloqueos | media | WAL, transacciones cortas, *lease* con `locked_until` |
| R6 | recordatorios duplicados con varios workers | alta si no se diseña | *lease* desde el primer día |
| R7 | el modelo "prioriza" en el briefing | media | alertas solo por regla con evidencia |
| R8 | documento malicioso como instrucción | media | entra como evidencia, nunca como instrucción |
| R9 | crecimiento de herramientas degrada la selección | **alta** | Capability Router + métrica `capability_miss` |
| R10 | T07 (abierto de Fase 9) empeora al tocar el prompt | media | el prompt es zona de cambio medido: A/B obligatorio |

---

## 15. Orden recomendado de implementación

El criterio: **primero lo que hace posible lo demás, después lo que da valor
visible.** Cada unidad cierra con medición y checkpoint, como en Fase 9.

| # | unidad | por qué va aquí | medición de cierre |
|---|---|---|---|
| **10.1** | **Context Router + presupuesto instrumentado** | sin esto, todo lo demás empeora el techo | benchmark 76 sin regresión; `max_total_tokens` **baja** |
| **10.2** | **Capability Router** (core/optional) | libera presupuesto para las capas nuevas | `capability_miss = 0` en los 76 casos |
| 10.3 | Historial: búsqueda, resumen, pin, archivado | reutiliza el 80 %; valor inmediato; riesgo mínimo | tests + UI |
| 10.4 | Memoria: estado `suggested` + panel de aprobación | cierra la política que falta | memoria automática no entra al prompt |
| 10.5 | **Task Engine** con *lease* | base de agenda y de todo lo asíncrono | jobs sobreviven reinicio; sin duplicados con 2 workers |
| 10.6 | **Agenda** sobre tasks + notificación en app | primera capacidad "operativa" visible | recordatorio dispara una vez tras reinicio |
| 10.7 | **Approval Engine** con dry-run | **prerrequisito de cualquier WRITE** | ninguna acción sin propuesta firmada |
| 10.8 | **Vault + Secret Broker** | solo tiene sentido con approvals | secreto nunca en prompt/log/métrica (test) |
| 10.9 | Primera acción WRITE real, una sola | valida la cadena completa | reversible, idempotente, auditada |
| 10.10 | Documentos | independiente; alto valor | original recuperable; extracción citable |
| 10.11 | Snapshot + Briefing | compone lo anterior | cobertura declarada; cero alertas sin regla |
| 10.12 | Market intelligence | último: necesita egress y política | no empezar sin §16.8 |

**10.1 y 10.2 no son negociables como primeros.** Todo lo demás se puede
reordenar según prioridad de negocio.

---

## 16. Qué más necesita Andes Assistant — propuestas propias

Ocho cosas que no están en la lista y que separan un asistente operativo de una
demo. Cada una con su justificación, dependencia, riesgo y prueba.

### 16.1 Idempotencia y ejecución exactamente-una-vez
**Para qué:** que un reintento no cree dos facturas.
**Valor:** sin esto, cualquier fallo de red durante un WRITE es un daño real.
**Dependencia:** Approval Engine (la `idempotency_key` nace en la propuesta).
**Riesgo:** claves mal elegidas bloquean reintentos legítimos.
**Prueba:** ejecutar la misma propuesta dos veces → un solo efecto, segundo
intento devuelve el resultado del primero.

### 16.2 Dry-run obligatorio en toda acción de escritura
**Para qué:** que `impact` sea evidencia y no promesa.
**Valor:** es lo que hace que "18 productos" se pueda aprobar con confianza; es
la misma disciplina de grounding de Fase 9 aplicada a las acciones.
**Dependencia:** cada acción declara su simulador.
**Riesgo:** un dry-run que no refleja el efecto real es peor que ninguno.
**Prueba:** para cada acción, dry-run y ejecución real sobre datos de prueba
producen el mismo conjunto afectado.

### 16.3 Reversibilidad declarada
**Para qué:** distinguir lo que se puede deshacer de lo que no.
**Valor:** permite una puerta más fuerte para lo irreversible (doble
confirmación, ventana de gracia) sin molestar en lo reversible.
**Dependencia:** catálogo de acciones con `inverse_action`.
**Riesgo:** declarar reversible algo que no lo es.
**Prueba:** toda acción del catálogo declara `reversible`; las irreversibles
exigen confirmación reforzada en un test de integración.

### 16.4 Gobernador de coste y cuota por actor
**Para qué:** hoy el rate limit son 10/min **en memoria y por proceso**: con N
workers son N×10 y un reinicio lo borra. Con trabajos en segundo plano, un bucle
puede gastar sin límite.
**Valor:** control de gasto real y protección ante bucles.
**Dependencia:** persistencia compartida.
**Riesgo:** cuotas mal calibradas bloquean trabajo legítimo.
**Prueba:** superar la cuota devuelve `429` coherente entre workers.

### 16.5 Canal de notificación con estado de entrega
**Para qué:** un recordatorio que solo existe en una tabla no es un recordatorio.
**Valor:** cierra el ciclo de la agenda; es lo que la vuelve útil.
**Dependencia:** Task Engine.
**Riesgo:** notificación duplicada o perdida.
**Prueba:** un recordatorio genera exactamente una entrega; reintentos no
duplican.

### 16.6 Trazabilidad visible al usuario ("¿por qué dijiste eso?")
**Para qué:** la procedencia ya existe internamente (`evidence_id`,
`cited_evidence_ids`); no se muestra.
**Valor:** es la base de la confianza para aprobar escrituras. Barato: los datos
ya están.
**Dependencia:** ninguna nueva.
**Riesgo:** exponer campos que la ACL ocultó — usar la misma lista blanca de
`answer_view`.
**Prueba:** la traza nunca contiene un campo que la tarjeta no pueda mostrar.

### 16.7 Contrato de degradación
**Para qué:** declarar qué hace el asistente cuando el Gateway cae, el LLM falla
o el Vault está bloqueado.
**Valor:** hoy hay fallbacks dispersos; con 11 subsistemas hace falta una matriz
explícita, o el comportamiento en fallo será una sorpresa.
**Dependencia:** ninguna; es diseño y tests.
**Riesgo:** degradar en silencio y que el usuario no sepa que le falta señal.
**Prueba:** por cada dependencia caída, la respuesta declara la pérdida de
cobertura (lo mismo que exige el briefing).

### 16.8 Capa de egress para Internet, antes de cualquier búsqueda web
**Para qué:** la Capa 10 necesita salir a Internet. Eso es una frontera nueva.
**Valor:** hace posible la inteligencia de mercado sin abrir un agujero.
**Dependencia:** allowlist de dominios, proxy con auditoría, presupuesto de
peticiones, y **prohibición de enviar datos del ERP al exterior** sin aprobación
explícita.
**Riesgo:** exfiltración e inyección desde páginas web; el mayor de la fase.
**Prueba:** ningún dato del ERP sale sin propuesta aprobada; el contenido web
entra como evidencia no confiable y nunca como instrucción.

---

## 17. Estrategia de pruebas

La disciplina de Fase 9 se mantiene y se extiende:

1. **Casos gold ANTES de implementar.** El benchmark de 76 casos cubre lectura.
   WRITE, agenda y approvals necesitan sus propios casos dorados **escritos
   primero** — si no, se mide lo que se construyó, no lo que se quería.
2. **A/B por bandera para todo lo que toque el prompt.** Ya está el instrumento
   (`evals/fase9_flag_measurement.py`), con verificación de radio.
3. **Radio de impacto antes de repetir el benchmark completo.** Igual que en 9.9.
4. **Tests de seguridad como regresiones, no como auditoría puntual:** el secreto
   no aparece en prompt/log/métrica; la aprobación caducada no ejecuta; el
   documento malicioso no cambia la decisión.
5. **Prueba de concurrencia** para el Task Engine: dos workers, cero duplicados.

---

## 18. Dependencias externas nuevas

| qué | para qué | cuándo |
|---|---|---|
| `cryptography` | AES-GCM del Vault | 10.8 |
| jobstore persistente (o tabla propia + poll) | Task Engine | 10.5 |
| extractores PDF/Excel/OCR | Documentos | 10.10 |
| proxy de egress con allowlist | Market intelligence | 10.12 |

Ninguna hace falta antes de 10.5. **Las dos primeras unidades no añaden ninguna
dependencia** — solo reorganizan lo que ya existe.

---

## 19. Lo que deliberadamente no propongo

- **Migrar de SQLite** todavía. Duele, pero el cuello de botella hoy es el techo
  de tokens, no la base. Migrar ahora sería optimizar lo que no duele.
- **Multi-tenant real.** El aislamiento por actor cubre el caso actual. Diseñar
  para inquilinos que no existen añade superficie sin usuario.
- **Que el asistente escriba código o ejecute SQL arbitrario.** Es la vía más
  rápida a una escalada irreversible y no aporta nada que un catálogo de acciones
  declaradas no dé con mucho menos riesgo.
- **Streaming de respuestas.** Mejora la percepción de latencia, pero complica
  el verifier —que hoy opera sobre la respuesta completa— y la Fase 9 demostró
  que ese verifier es lo que sostiene la veracidad.
