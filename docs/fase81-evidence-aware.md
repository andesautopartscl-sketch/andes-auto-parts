# FASE 8.1 — Evidence-aware Assistant (implementado)

**Base:** `cc54aabe`  
**Flag:** `ANDES_ASSISTANT_AGENT_ENABLED=0` (default).  
**Gate:** AGENT=1 **y** el mismo soft-enable que `LlmPlanner` (`NL=1`, `ORCH=llm`, env local/staging, API key). Si AGENT=1 sin soft-enable → ruta actual.

`soft_llm_ready` / `model=None` en capabilities **no** se corrige aquí (observación 7.6).

## Flujo

```
AGENT=0: mensaje → context/history/memory → LlmPlanner/FakePlanner
         → PlanValidator → ToolRunner (0..N steps) → Composer → derived

AGENT=1: mensaje → context/history/memory (igual)
         → NO se ejecuta un plan inicial multi-tool
         → AgentDecisionClient (evidence=[])  1 acto
         → PlanValidator(1 step) → ToolRunner
         → EvidenceStore
         → AgentDecisionClient (evidence=[e1,...])
         → ...
         → verifier
         → derived (igual que 7B.5)
```

Reuse FASE 5 (`KIND_REUSE` / `reuse_prior_evidence`) **no** abre AgentLoop.

Una decisión = una acción. `call_tool` = exactamente 1 tool. `steps` / `tools[]` / `n_steps` se rechazan.

Traza segura (audit + respuesta local): `decision_index`, `action`, `tool`, `evidence_count_before`, `evidence_count_after`, `correlation_id`, `latency_ms`, `fallback`. Sin prompts ni evidence cruda.

## Cálculos

Cada item en `calculations` debe tener `id`, `op`, `inputs`, `result`. `op` ∈ min|max|sum|diff|ratio. Item incompleto → `AgentDecisionError` → retry 1 → fallback. El verifier recalcula; no confía en `result` del modelo.

## Límites

`MAX_TOOL_CALLS=5` (turno), `MAX_AGENT_STEPS=8`, `MAX_SECONDS=40`, `MAX_OUTPUT_TOKENS=800`, `MAX_COST_PER_TURN=0.05` o `8000` tokens si no hay `ANDES_LLM_COST_*`, `MAX_TOOL_RESULT_CHARS=4000`, `MAX_EVIDENCE_PROMPT_CHARS=6000`, `MAX_SAME_CALL=1`, `MAX_EMPTY_FOLLOWUPS=1`, `MAX_CONSECUTIVE_TOOL_ERRORS=2`. Un retry de decisión; luego Composer.

## Fallback

JSON inválido, timeout, costo, loop, error de tool, verifier, LLM → `fallback_used=true` + `compose_answer` con evidence acumulada. No se publica draft sin verifier.

## Eval

Los 36 casos FASE 4 se corren con **AGENT=0**. Scaffold: `evals/fase81_runner.py` (target 64; gate aún no listo).

Estabilidad 8.1A: `python -m evals.fase81a_stability` (10× P1 + 5 equivalentes; no es gate).

### FASE 4 F01/F02 (no mezclar con este fix)

`F01`/`F02` ("Cuanto vendimos esta semana?") fallan 34/36 porque FASE 5 trata `esta` como anáfora → clarify en ~3ms, sin LLM. **No se corrige en 8.1 ni 8.1A.**

## FASE 8.1A — Goal coverage

Capa mínima, no un planner. Solo corre dentro de AgentLoop (AGENT=1).

```
user_goal_requirements = [{id, type, status, evidence_ids}]
```

**Extracción:** detector determinista del par complementario `stock_movements` + `current_inventory` (stems cerrados). Si no dispara → `extraction=unknown` y no bloquea final. El agente puede proponer types del catálogo cerrado solo cuando la extracción es unknown; types inválidos se ignoran. Proponer un type **no** autoriza tools ni ACL.

**Cobertura:** solo evidence de ESTE turno (`EvidenceStore`). Memory y turnos anteriores no cubren. Un requirement queda `covered` si hay evidence `ok` de una tool del type. `impossible` solo si el agente declara `unresolved` con reason cerrado **y** ya hay evidence de esa tool o no quedan invokes.

**Regla de final:**
- `extraction=detected` y hay `uncovered` → `final_answer` se bloquea una vez (sigue el loop).
- Segundo `final_answer` o sin invokes → final parcial + "No pude resolver: …". No inventa.
- Todos `covered` o `impossible` → final + verifier.

Traza segura añade `goal_covered_count`, `goal_uncovered_count`, `blocked_final`. Snapshot en respuesta/audit: types + status + evidence_ids. Sin prompts ni evidence cruda.

## FASE 8.1B — argumentos + benchmark 64

Causa de P2 `invalid_args` (LLM real, sin prompts/secretos):

- tool=`get_stock_movements`
- el modelo llenó el union-bag: `q="2404"`, `codigo=null`, `limit=100`, `fecha_desde/hasta=2024-04-01/30` (2404 leído como abril 2024)
- `q` no pertenece al contrato de esa tool → se descartaba
- `codigo` quedaba vacío → `arg_schema` `codigo required`

Clase: **A prompt/schema** (el AgentLoop no veía el contrato por tool; el JSON schema es un bolsón union) + **E modelo**. No es binding ni un default de negocio.

Corrección mínima:

- `tool_contracts.py` (única vista nombre/desc/required/optional/tipos; valida `arg_schema`)
- normalizar: `q` que es product code → `codigo`; `codigo` int→string; `limit` dígitos→int; **no** `"desconocido"→0`
- fechas YYYY-MM-DD solo si el usuario las escribió
- retry 1 con `{error,tool,fields}` sin stack ni valores

Benchmark: `evals/fase81_evidence_dataset.jsonl` (64; los tamaños de bucket del spec sumaban 68, se quitaron 4 prompts duplicados) + `python -m evals.fase81_runner`. Gate: **no listo**.

## No tocado

Gateway, registry/policy, ERP ACL, `ALLOWED_TOOLS`, `WRITE_TOOLS`, semántica Memory/epoch/Resolver, Composer, FASE 5, ConversationResolver (`esta semana`).

## FASE 8.1G — progreso estructural y anti-thrashing

Reemplaza el gate de follow-ups vacíos por una noción **estructural** de progreso.
Nada de esto corre con `AGENT=0`.

### El defecto

```python
last_empty = bool(raw_evidence and raw_evidence[-1].get("empty"))
if last_empty and state.empty_followups >= MAX_EMPTY_FOLLOWUPS:
    return _composer_fallback(reason="agent_limit")
```

Dos problemas independientes:

1. contador **global** + "la última evidencia fue vacía" ⇒ bloquea cualquier
   `call_tool`, incluso una tool distinta, no ejecutada, que cubre un requirement
   `uncovered`;
2. el rechazo **terminaba el turno** en vez de rechazar la decisión.

Efecto medido (T07, `get_supplier` → `empty=True` contra el ERP real):
`d1 get_supplier` · `d2-d4 final bloqueado` · `d5 get_purchase_orders` válida pero
rechazada antes de ToolRunner → `agent_limit`, la OC nunca se consultó.

### Progreso (`agent_progress.py`)

`ProgressLedger`, turn-scoped, en RAM. No es memoria, ni historia, ni planner, ni
autoridad de permisos: una call admitida sigue pasando por `agent_schema`,
`ToolRunner` y la ACL del Gateway.

| clase | significado |
|---|---|
| `coverage` | un requirement salió de `uncovered` (covered o impossible) |
| `evidence` | evidencia nueva, `ok`, no vacía y **no equivalente** |
| `none` | ninguna de las dos |

"Evidencia equivalente" se decide con `content_key`: fingerprint de
`tool + ok + empty + data + meta` (`evidence_store.canonical_content_key`).
Dos llamadas con argumentos distintos y el mismo payload **no** son progreso —
ésta es la clase P03 (`get_dashboard_kpis` ×3 con args distintos, `call_key`
distinto, `MAX_SAME_CALL` nunca dispara).

### Admisión

Se rechaza sólo **repetición demostrada sin progreso**:

| regla | resultado |
|---|---|
| misma canonical call | `repeat_call` |
| tool ya ejecutada, devolvió vacío, no cubre nada `uncovered` | `empty_repeat_no_progress` |
| tool ya ejecutada, su **última** ejecución no progresó, no cubre nada `uncovered` | `tool_repeat_no_progress` |
| tool nunca ejecutada | admitida |
| tool que cubre un requirement `uncovered` | admitida |

"Tool distinta ⇒ permitir" **no** es una regla: una tool ya ejecutada sin
progreso se rechaza igual. `productive` mira la última ejecución, no el
histórico: productiva una vez no es productiva siempre.

Una decisión rechazada cuesta **una decisión**, no el turno: el loop sigue con un
hint que nombra la tool bloqueada y los requirements pendientes.

### Límites

`MAX_NO_PROGRESS_STEPS=2` (invokes ejecutados consecutivos sin progreso) y
`MAX_REJECTED_DECISIONS=3` (decisiones rechazadas consecutivas). Al agotarse →
`_composer_fallback(reason="agent_no_progress")`, razón enumerable distinta de
`agent_limit`. `MAX_AGENT_STEPS`, `MAX_TOOL_CALLS`, `MAX_SECONDS` y
`MAX_COST_PER_TURN` **no** se tocaron. `MAX_EMPTY_FOLLOWUPS` se conserva,
re-alcanzado a follow-ups de la *misma* tool que ya devolvió vacío.

Coste: en el escenario de thrashing medido, el turno termina tras **1** invoke de
5 — los rechazos no consumen presupuesto de tools.

`blocked_finals` sigue sin tope propio (acotado por `MAX_AGENT_STEPS`): fue
exactamente lo que permitió a T07 llegar a la decisión correcta en d5.

### Extracción — regla de faceta generalizada

`stock` dentro de una frase que califica **otro** intent no es un goal de
inventario. Las frases faceta (`stock critico`, `movimientos de stock`,
`movimiento de stock`) se eliminan y las **mismas** señales cerradas de
`current_inventory` se reevalúan sobre el resto:

- `"KPIs de 7 días con stock crítico"` → `[dashboard_kpis]`
- `"Movimientos de stock del 2404"` → `[stock_movements]`
- `"Stock y movimientos del 2404"` → ambos (sobrevive señal fuera de la faceta)
- `"productos con stock crítico"` → `[current_inventory]` (faceta sola conserva su requirement)

Antes esto existía sólo para `stock critico` dentro de `dashboard_kpis`.

`purchase_orders`: los stems sueltos `orden`/`ordenes` exigen ahora un calificador
(`compra`, `compras`, `proveedor`, `proveedores`); `po` se eliminó. `"en orden
alfabético"` ya no produce un requirement.

Los 9 `REQUIREMENT_TYPES` cubren las 10 tools del catálogo, cada tool en
exactamente un type (verificado por test). No se añadieron familias nuevas: un
type sin señal automática sigue siendo proponible por el agente cuando
`extraction=unknown`, y `refresh()` lo mapea igual.

### Verifier — desglose, sin debilitar nada

Ningún check cambió y `failures` conserva su valor. Se añadió un desglose porque
un solo número no distingue dos resultados opuestos:

- `dropped_claims` — el modelo propuso un claim ungrounded, se descartó y se
  publicó respuesta grounded → **el guardrail funcionó**;
- `answer_replaced` + `replaced_reason` — no se pudo producir respuesta grounded
  y Composer la reemplazó → turno degradado;
- `calc_mismatch` / `calc_unresolved` / `calc_error` — recálculo distinto,
  `evidence_id`/path inexistente, u otro fallo.

Éste es el mecanismo de M02/M04: el número lo pone el usuario (`8888`, `25`), no
la evidencia, así que el claim que lo cita se descarta. `verifier_breakdown` se
expone en la respuesta local y en el audit; **el scorer no se modificó**.

### Cierre

```bash
python -m evals.fase81g_closure                # determinista (sin key LLM)
python -m evals.fase81g_closure --llm          # + P03/T07/M02/M04 ×10
python -m evals.fase81g_closure --llm --bench  # + benchmark 64
```

Restaura `AGENT=0 NL=0 ORCH=fake` al salir. Escribe
`data/fase81_eval/fase81_final_report.{json,txt}`.

### No tocado

Gateway, ACL, `ALLOWED_TOOLS`, `WRITE_TOOLS`, `WRITE`, Memory/epoch/Resolver,
History, Composer, FASE 5, contratos por herramienta (`q` vs `codigo`), scorer,
`admin`, `productos`, `import_excel`, templates, `docs/assistant-ops.md`.

## FASE 8.1G.3 — fallo definitivo derivado y final bloqueado como no-progreso

Dos huecos que 8.1G dejó abiertos, ambos en el camino `final_answer`, que el
ProgressLedger no observaba.

### 1. Fallo definitivo → `impossible` derivado

`GoalCoverage` trataba de forma asimétrica dos resultados que significan lo mismo:

```
ok=True  empty=True  error=None        -> covered      (la tool respondió: no hay)
ok=False empty=True  error=not_found   -> uncovered    (indistinguible de "no preguntado")
```

La segunda dejaba el requirement irresoluble para siempre: `allow_partial` veía
una covering tool hermana sin usar, bloqueaba el final, y el turno moría por
presupuesto.

`refresh()` ahora deriva `impossible` de la propia evidencia mediante un
**allowlist cerrado**:

| error_code | reason | scope |
|---|---|---|
| `not_found` | `not_found` | `entity` |
| `permission_denied` | `permission` | `tool` |

Todo lo demás —`timeout`, `agent_unavailable`, `erp_unavailable`, códigos
desconocidos, errores de transporte— **nunca** es definitivo: la puerta queda
abierta para reintentar o probar una hermana. Un solo fallo no definitivo entre
los intentos basta para mantener el requirement `uncovered`.

**Por qué los dos scopes.** `not_found` es un hecho sobre la *entidad*: ninguna
tool hermana puede descubrir que sí existe, porque todas preguntan por la misma
entidad. Además empujar al agente a la hermana es activamente peligroso —
`check_stock` sobre un código inexistente responde `disponible: 0`, es decir
reportaría una ausencia como un cero, violando `null != zero`. `permission_denied`
en cambio es por tool (la ACL lo es), así que sólo se vuelve definitivo cuando
**todas** las covering tools fueron intentadas y denegadas.

`ToolRunner`, `allow_partial_final` y el scorer no se tocaron.

### 2. El `final_answer` bloqueado es repetición

El ledger sólo observaba `call_tool`, así que un modelo que respondía `final`
una y otra vez mientras un requirement seguía `uncovered` quemaba el presupuesto
del turno de forma invisible. Cada bloqueo cuesta un round-trip completo.

`ProgressLedger.record_blocked_final(coverage_signature)` cuenta **bloqueos
consecutivos sin cambio de cobertura**; si algún requirement se movió, la racha
se reinicia (`note_coverage` hace lo mismo cuando una tool cambia la cobertura).
Al alcanzar `MAX_BLOCKED_FINALS_NO_PROGRESS=3` el final **se libera como parcial**
—con la nota "No pude resolver: …"— en vez de convertirse en fallback.

**El umbral es 3 por dato, no por intuición:** en las 10 corridas reales de T07
los PASS ocurren tras exactamente 2 bloqueos. Un umbral de 2 mataría justo los
casos que funcionan. Se mantiene deliberadamente separado de
`MAX_NO_PROGRESS_STEPS=2`, que acota llamadas ejecutadas.

`MAX_AGENT_STEPS` y `MAX_TOOL_CALLS` sin cambios.

### 3. Observabilidad del presupuesto

El techo de tokens/coste ahora reporta `agent_token_budget` en vez de confundirse
con `agent_limit` (que queda para invokes agotados). Sólo es una razón enumerable
distinta: **ningún presupuesto se subió**. Medido: con el prompt de sistema en
928 tokens y `MAX_EVIDENCE_PROMPT_CHARS=6000`, el peor caso por decisión ronda los
2566 tokens, así que `TOKEN_BUDGET_FALLBACK=8000` permite ~3 decisiones y es el
límite que realmente dispara, no `MAX_AGENT_STEPS=8`.

### No tocado

M02, M04, scorer, checks del verifier, `content_key`, la política de búsqueda
refinada tras vacío (M3 de la auditoría), `MAX_AGENT_STEPS`, `MAX_TOOL_CALLS`,
Gateway, ACL, WRITE, Memory, History.

## FASE 8.1H — grounding numérico por token

### Causa

`_claim_grounded` validaba cada cifra del claim así:

```python
for num in _NUM_RE.findall(text_u):
    if num in blob or ...:          # substring, SIN límites
        continue
    if not re.search(rf"(?<!\d){re.escape(num)}(?!\d)", blob):
        return False
```

La primera condición es containment de substring sobre el JSON serializado. Como
casi cualquier dígito aparece dentro de otro número o de una fecha, el `continue`
se disparaba siempre y **el chequeo con límites de la línea siguiente era código
muerto**. En la práctica cualquier dígito suelto quedaba "grounded".

C04 lo demostró en producción: el modelo publicó *"suman 3 unidades (2 + 1)"*
mientras su `calculation` fallaba (`calc_unresolved=1`). Ningún campo de la
evidencia vale 3; el "3" venía de `numero_documento="340273"`, de
`margen_pct=62.38` y de las fechas.

### Regla

Una cifra es grounded **solo** si:

1. aparece como **token numérico completo** en la evidencia de este turno, o
2. es el resultado de una `calculation` que el verifier **recalculó y validó**
   (sus inputs son paths de evidencia por construcción).

Nunca por substring.

### Implementación

- `_NUM_TOKEN_RE = (?<![\d.,])-?\d+(?:[.,]\d+)?(?!\d)` — el lookbehind impide que
  `5` matchee dentro de `12345`.
- `_collect_evidence_numbers` recorre `data`, `meta` y `arguments`, y **enmascara
  fechas antes de tokenizar**, así `2026-09-16` no aporta 2026, 9 ni 16.
- `_norm_number` canonicaliza: `2`, `"2"`, `2.0` y `"2,0"` son la misma cifra;
  `"1,5"` y `1.5` también.
- Negativos: `-5` aporta `-5` **y** `5`, porque la magnitud está igualmente
  atestiguada ("bajó 5 unidades"). El signo es asunto del composer, no del
  grounding.
- `grounded_numbers(store, verified_results)` construye el conjunto cerrado:
  tokens de evidencia + resultados de calculations validadas.
- `_claim_number_tokens` enmascara fechas y códigos de producto del claim antes
  de buscar cifras — se validan por sus propias reglas y no deben partirse en
  dígitos sueltos.

Ningún otro check del verifier cambió: códigos, fechas, `null != zero`,
`reply_contains_only_evidence_values` y el desglose de 8.1G siguen igual.

### Antes / después (evidencia real del Gateway)

| | antes | después |
|---|---|---|
| `"3"` es token grounded | sí (substring) | **no** |
| C04 con calc sin resolver | publica "suman 3 unidades" | **claim descartado**, se publica el claim grounded |
| C04 con calc bien formada | publica | **publica** (sin cambio) |
| M02 | `answer_replaced`, sin 8888 | **igual** |
| M04 | claim descartado, respuesta grounded | **igual** |

### No tocado

AgentLoop, GoalCoverage, ProgressLedger, allow_partial, ToolRunner, Gateway, ACL,
memory, history, scorer, `MAX_*`.

## FASE 8.1H.2 — grounding semántico de fechas

### Regresión que corrige

8.1H acertó con el grounding numérico por token, pero enmascaraba **solo el
formato ISO**. Como además quitaba las fechas del conjunto de cifras citables,
cualquier fecha escrita de otra forma se descomponía en números sueltos que ya no
matcheaban nada:

```
evidencia: 2026-07-31

"El ingreso fue el 2026-07-31."          grounded ✔
"El ingreso fue el 31 de julio de 2026." grounded ✘   tokens huérfanos: 31, 2026
"Hubo un ingreso el 31/07/2026."         grounded ✘   tokens huérfanos: 31, 07, 2026
```

Ambas pasaban antes de 8.1H y son información correcta. Alcance medido: 3 casos
de 64 (C04, T05, T08). La respuesta baseline de C04 decía literalmente
*"ingresos de 2 unidades el 31 de julio de 2026"*.

### Regla

Una fecha de un claim está grounded cuando **nombra una fecha que la evidencia
contiene**, en cualquier forma soportada, comparada tras normalizar:

| forma | ejemplo |
|---|---|
| ISO | `2026-07-31` |
| dd/mm/yyyy (`/`, `-`, `.`) | `31/07/2026`, `31-07-2026`, `31.07.2026` |
| año de 2 dígitos | `31/07/26` |
| textual español | `31 de julio de 2026`, `31 de Julio del 2026` |
| parcial (día+mes) | `31 de julio` → cubre si la evidencia tiene ese día/mes |

Todas normalizan a `YYYY-MM-DD` antes de comparar. Meses en español, con
`septiembre`/`setiembre`. Día 1-31, mes 1-12, año 1900-2999; fuera de rango no es
fecha y sus cifras se validan como números normales.

### El bug numérico no vuelve

El enmascarado sigue en pie **en ambos lados**: una fecha nunca aporta su día,
mes ni año al conjunto de cifras citables.

```
evidencia 2026-07-31  →  grounded_numbers NO contiene 7, 31, 2026, 26, 6

"Hubo 7 salidas."                      ✘
"Hubo 31 salidas."                     ✘
"Hubo 2026 salidas."                   ✘
"El ingreso fue el 30 de julio de 2026."  ✘  (fecha que no está en evidencia)
"El 31 de julio ingresaron 2 unidades."   ✔  (fecha real + cifra grounded)
```

Y la regla de 8.1H queda intacta: `3 unidades` solo está grounded si 3 aparece
como número independiente en la evidencia **o** una calculation verificable lo
produce. C04 sigue descartando su "3" no verificado.

### Implementación

- `mask_dates(text) -> (texto_sin_fechas, [canónicas])` — se aplica a claims y a
  evidencia; el orden es ISO → dd/mm/yyyy → textual, blanqueando cada span para
  que no se reinterprete.
- `grounded_dates(store)` — fechas canónicas que la evidencia contiene.
- `_date_grounded(canónica, fechas)` — igualdad exacta, o sufijo `-MM-DD` para la
  referencia parcial.
- En `_claim_grounded` las fechas se validan **primero**; después la cifra se
  busca sobre el texto ya sin fechas.

### No tocado

AgentLoop, GoalCoverage, ProgressLedger, allow_partial, ToolRunner, Gateway, ACL,
memory, history, scorer, prompts, `MAX_*`.

## FASE 8.1I — cardinalidad verificable (`count`)

### El hueco

Un payload puede contener una colección de N registros **sin contener N en ningún
campo**. El KPI real es el caso: `stock_critico` trae 10 filas y ningún escalar
vale 10.

Con eso, "Hay 10 productos con stock crítico" no podía demostrarse por ninguna de
las dos reglas de grounding: no es token de evidencia (regla 1), y `CALC_OPS` era
`min|max|sum|diff|ratio` — **sin operación de conteo**, así que la regla 2 era
inalcanzable por construcción. Resultado: se descartaba información correcta
(G03/R03/P04).

### `count`

Una op explícita que lee **estructura** en vez de números:

```
count(["e1.data.stock_critico"]) = 10
```

Reglas:

- exactamente **un** input, validado en `agent_schema` y en `recompute_calculation`;
- el path debe resolver a una **lista JSON real**. Un string y un dict también
  tienen longitud, y contar sus caracteres o sus claves sería una cifra
  fabricada → `CollectionRequired`;
- `resolve_path` solo navega la evidencia de este turno, así que el blob crudo,
  un substring o una fecha son inalcanzables por construcción;
- el resultado entra en `grounded_numbers` **solo** si la colección existe, es
  lista, y el verifier reconstruye el mismo número.

El diseño es general, no por herramienta: las 10 tools exponen sus colecciones
como listas JSON (`data.items` en 9 de ellas; el KPI además `data.chart_data`,
`data.top_productos`, `data.top_clientes`, `data.stock_critico`). No hay ningún
`if tool == ...` ni referencia a un case_id.

Declarado en tres sitios que deben coincidir: `agent_schema.CALC_OPS` (autoridad
Python), el enum de `llm/plan_schema.py` (lo que ve el modelo) y
`llm/agent_prompts.py`. Sin el prompt la op sería inalcanzable en la práctica; un
test lo fija.

### Verificado contra evidencia real

| escenario | resultado |
|---|---|
| sin `count` | claim descartado, `answer_replaced` |
| `count` correcto (=10) | `failures=0`, claim publicado |
| `count` equivocado (=11) | `calc_mismatch=1`, claim descartado |
| `count` sobre un escalar | `calc_error=1`, claim descartado |

### Lo que NO cambió

- **C04**: `sum` con path inexistente sigue en `calc_unresolved=1` y el 3 no se
  publica. Y `count` no puede blanquearlo: apuntar `count` a `data.items`
  (2 filas) para afirmar una suma de 3 da `calc_mismatch=1`.
- **M02**: 8888 sigue sin grounding; `count` con `result=8888` sobre una lista de
  1 elemento da `calc_mismatch=1`.
- **M04**: el 25 recordado se sigue descartando.
- Token grounding (8.1H) y fechas semánticas (8.1H.2) intactos.
- `null != zero`: `ventas_periodo=None` con un claim "ventas son 0" se sigue
  reemplazando, y `count`=0 sobre una lista vacía **no** habilita reportar un
  nulo financiero como cero.

### No tocado

AgentLoop, GoalCoverage, ProgressLedger, allow_partial, ToolRunner, Gateway, ACL,
memory, history, scorer, `MAX_*`.

## FASE 8.1J — cierre: estado estructurado al modelo y eje verifier por resultado

Review de cierre. Tres causas raíz verificadas contra código, no contra hipótesis.

### Causa raíz 1 — el sistema sabía qué tool faltaba y nunca lo decía

`GoalCoverage` conoce `covering_tools(req.type)`, pero al modelo solo le llegaba
el **tipo de requirement**, vocabulario interno que no es 1:1 con nombres de tool
(`current_inventory` → `get_inventory|check_stock`, `ingresos` → `get_ingresos`):

```
antes  goal_pack     {"type":"purchase_orders","status":"uncovered"}
antes  blocked_note  "uncovered=purchase_orders. Llama UNA tool allowlisted que cubra el faltante."
```

El modelo tenía que hacer el mapeo tipo→tool por su cuenta en cada decisión
bloqueada. Ahora:

```
ahora  goal_pack     {"type":"purchase_orders","status":"uncovered","tools":["get_purchase_orders"],"reason":null}
ahora  blocked_note  "final_answer bloqueado. Falta cubrir: purchase_orders(get_purchase_orders).
                      Llama UNA de estas tools: get_purchase_orders."
```

`blocked_note(store)` descuenta las tools ya ejecutadas, así que señala una ruta
que todavía puede producir algo. **Es informativo, no autoridad**: el allowlist
sigue en `agent_schema`, `ToolRunner` y la ACL del Gateway, y una tool nombrada
puede seguir siendo rechazada por cualquiera de los tres.

El `reason` del `impossible` derivado también viaja ya en el snapshot (hueco de
observabilidad abierto desde 8.1G.3).

### Causa raíz 2 — la gramática de los paths de calculation no estaba documentada

`resolve_path` acepta `<evidence_id>.data|meta|arguments.<campo>` con índices de
lista por posición. El prompt mostraba **un solo ejemplo**, y era el de `count`
sobre la lista entera. Un modelo que quiera sumar dos cantidades tenía que
inventar `e1.data.items.0.cantidad` sin ninguna guía: `calc_unresolved` es el
resultado predecible. Ahora la gramática está escrita, con ejemplo indexado.

### Causa raíz 3 — la regla de cardinalidad se había vuelto desproporcionada

8.1I.2 dejó las tres líneas de calculations girando en torno a contar. Se
conserva la regla (una cantidad exige `count`) pero se generaliza a **cualquier
cifra derivada** y se le devuelve el peso que le corresponde. Un test fija que la
guía de conteo no vuelva a monopolizar el contrato.

### Eje verifier del scorer: proceso → resultado

`_verifier_ok` fallaba con `verifier_failures > 0`, es decir **cuando el guardrail
se disparaba**. Eso es un criterio de proceso: marcaba como fallo corridas donde
el claim descartado nunca llegó al usuario y la respuesta publicada estaba
grounded. El desenlace degradado tiene una forma observable —el verifier no pudo
construir respuesta grounded y Composer la reemplazó— y sigue siendo fallo salvo
que el gold lo tolere explícitamente.

Las activaciones del guardrail pasan a `guardrail_events` (reportado, no puntuado)
y `verifier_failures` se conserva como métrica para que la redefinición no
esconda nada. **Ningún check del verifier se debilitó.**

Efecto medido sobre la última corrida real: **59/64 → 62/64**. Cambian C04, G03 y
M02, los tres con respuesta publicada grounded. Esto es una corrección de
definición del benchmark, no una mejora de producto.

### No tocado

AgentLoop (presupuestos, fallback, one-decision/one-tool), ProgressLedger,
EvidenceStore, checks del verifier, `agent_schema`, ToolRunner, Gateway, ACL,
memory, history, `MAX_*`.

## FASE 8.1K — cierre, hardening y política del benchmark

Corrida real con LLM (ejecutada por el operador): **63/64**, único fallo K05.
T07, M02, M04, N04 y P03 estables 10/10; C04, G03, P04 y R03 en PASS.

### K05 — decisión: es política del benchmark, no producto

`get_inventory` y `check_stock` son la misma familia `current_inventory` y ambas
responden "¿hay N unidades?". El gold exigía `check_stock` exacto. Medido contra
el Gateway real:

| | producto existe | producto NO existe |
|---|---|---|
| `get_inventory` | detalle por bodega | `ok=False`, `not_found` |
| `check_stock` | `available:true, disponible:2` | `ok=True`, **`disponible: 0`** |

`check_stock` reporta la ausencia **como cero**. El gold no sólo era estricto:
apuntaba a la tool con peor semántica de nulos, justo lo que `null != zero`
intenta evitar. La elección del agente (`get_inventory`) es defendiblemente la
mejor.

**Corregido en evaluación, no en producción**: el gold pasa a
`tool_count_policy: family` con `acceptable_tool_families`. Un test fija la
política, incluida la parte que no se afloja — una tool de otra familia, o
responder sin tool, siguen fallando.

### Hardening (deuda convertida en invariante)

| deuda | riesgo | corrección |
|---|---|---|
| `empty_followups` escrito y nunca leído desde 8.1G | estado muerto que induce a error al leer el código | eliminado |
| `coverage_hint` nunca se limpiaba | una guía de un final bloqueado seguía inyectándose decisiones después, ya obsoleta, gastando tokens del presupuesto | se limpia al admitirse la llamada que la resuelve |
| error de transporte no registraba la llamada | `MAX_SAME_CALL` no la veía: el modelo podía repetirla idéntica hasta agotar `MAX_CONSECUTIVE_TOOL_ERRORS` | la llamada se cuenta; una sola regla de repetición |
| el harness de cierre escribía artefactos sin escrubir | defensa en profundidad perdida frente al runner de referencia | `_scrub` en los 3 escritores JSONL y en el informe |

Cada uno tiene test de invariante.

### Scorer: eje verifier por resultado (8.1J)

`verifier_failures > 0` marcaba como fallo **el guardrail funcionando**. El eje
mide ahora el desenlace: el verifier no pudo construir respuesta grounded y
Composer la reemplazó, lo cual sigue siendo fallo salvo que el gold lo tolere.
Las activaciones se reportan en `guardrail_events`. Ningún check se debilitó.

### Límites: una inconsistencia conocida

`MAX_AGENT_STEPS=8` no es el límite que dispara. Con `ANDES_LLM_COST_*` sin
definir aplica `TOKEN_BUDGET_FALLBACK=8000`, y el prompt de sistema (~1000 tokens)
más `MAX_EVIDENCE_PROMPT_CHARS=6000` dan un peor caso de ~2500 tokens por
decisión: **~3 decisiones alcanzables**. No se ha subido ningún presupuesto; el
techo de tokens se reporta como `agent_token_budget`, distinto de `agent_limit`
(invokes agotados), para que la causa real sea visible en la traza.

## FASE 8.2 (inicio) — procedencia de claims y vinculación con la evidencia

### El agujero

`evidence_ids` se parseaba en `agent_schema`, se truncaba a 12 y **nunca se
validaba**. Un claim podía citar `e1` mientras todas sus cifras venían de `e2`,
o citar un `evidence_id` inexistente, y el verifier lo aceptaba porque medía
contra el blob **global** del turno.

Consecuencia: 8.1H prueba que una cifra *existe* en la evidencia, no que
*proviene* de donde el claim dice. Es también la limitación conocida de `count`:
demuestra cardinalidad, no pertenencia.

### La regla

Cuando un claim cita `evidence_ids`, sus cifras, fechas y códigos se validan
**contra el subconjunto citado**, no contra todo el turno:

- `evidence_id` inexistente → `unknown_evidence_id`
- cifra/fecha que solo existe fuera de lo citado → `figure_outside_cited_evidence`
- una `calculation` solo respalda al claim que cita **la evidencia que esa
  calculation lee** (`evidence_ids_in_paths` sobre sus `inputs`)

Un claim sin `evidence_ids` no afirma procedencia y no se comprueba.

### Modo observación por defecto

`ANDES_ASSISTANT_PROVENANCE_ENFORCE=0` (default): la violación se **cuenta y se
reporta**, el claim **no** se descarta. Endurecer de golpe descartaría claims
correctos pero mal atribuidos, y todavía no sabemos la tasa real. Con `=1` la
violación descarta el claim como cualquier otro fallo de grounding.

El desglose lleva `provenance_violations`, `provenance_kinds` y
`provenance_enforced`, así que la tasa se puede medir sobre el benchmark antes de
decidir.

### Qué NO cambia

Todos los checks de 8.1H/8.1H.2/8.1I siguen actuando primero y con el mismo rigor:
una cifra ungrounded se descarta aunque la procedencia esté bien.

### Otras dos deudas cerradas en esta etapa

**Colisión de `content_key` por truncado.** El fingerprint se calculaba sobre la
vista ya truncada a `MAX_TOOL_RESULT_CHARS`, así que dos payloads distintos con el
mismo prefijo se consideraban "evidencia equivalente". Ahora entra el tamaño real
del payload, sin volver a mirar el contenido cortado.

**Consulta refinada tras un resultado vacío.** Un `ok=True, empty=True` dejaba
`last_progress=none` y cerraba la tool para siempre: `search_catalog q="filtro"`
sin resultados impedía probar `"filtro de aceite"`. Cero resultados no es lo mismo
que "esta tool no aporta nada": ahora un vacío admite **exactamente un**
refinamiento (`ADMIT_REFINE_AFTER_EMPTY`), acotado por `MAX_EMPTY_FOLLOWUPS=2`; si
el refinamiento también vuelve vacío, no hay progreso y el ledger corta.

## FASE 8.2B — pack de evidencia, presupuesto y medición de procedencia

### Bug: el pack de evidencia llegaba roto al modelo

`prompt_pack` serializaba todo y cortaba a `MAX_EVIDENCE_PROMPT_CHARS`, partiendo
la estructura a mitad. Con `MAX_TOOL_RESULT_CHARS × MAX_TOOL_CALLS = 20000` muy
por encima del cap de 6000, eso era alcanzable en operación normal — y ocurría
justo en los turnos multi-tool, que es donde más importa. **El modelo recibía JSON
no parseable y ninguna señal de que faltara contenido.**

Ahora se degrada por items: se conservan enteros los más **recientes** y los que
no caben pasan a un stub con su identidad (`evidence_id`, `tool`, `ok`, `empty`,
`omitted_from_prompt`). El pack siempre es JSON válido, siempre respeta el cap, y
**todos los `evidence_id` siguen visibles** — condición necesaria para que la
procedencia de 8.2 funcione, porque un id que desaparece del pack no se puede citar.

### Presupuesto: medido antes de tocarlo

Sobre **114 turnos con LLM real**:

| | declarado | observado |
|---|---|---|
| decisiones | 8 | **máx 4**, p95 4, media 2.19 |
| invokes | 5 | **máx 2** |
| segundos | 40 | **máx 10.7** |
| `cost_est` | — | **None en 114/114** |

Dos hallazgos: `MAX_COST_PER_TURN` **nunca se evalúa** (sin `ANDES_LLM_COST_*` el
guard activo es siempre el techo de tokens), y `MAX_AGENT_STEPS=8` era inalcanzable
— el techo financia ~5 decisiones en el mejor caso y 2 en el peor.

**Decisión: `MAX_AGENT_STEPS` 8 → 6.** Baja el techo de coste, no lo sube. 6 cubre
el camino más largo por diseño (3 finales bloqueados + la tool + el final = 5) con
un paso de margen, y queda dentro de lo que el presupuesto paga. No se subió ningún
presupuesto: no hay evidencia que lo justifique — cero muertes por presupuesto en
114 turnos.

`decisions_affordable()` deriva la relación y un test falla si alguien sube los
pasos sin subir el presupuesto, o encoge el presupuesto sin bajar los pasos.
`budget_snapshot()` expone qué límite está activo y viaja al audit y a la respuesta.

### Procedencia: severidad y gate de activación

Tres clases, no un porcentaje global:

| violación | severidad | por qué |
|---|---|---|
| `unknown_evidence_id` | **alta** | la cita no significa nada |
| `derived_figure_outside_cited_evidence` | **alta** | la cifra no existe salvo por la calculation, atribuida a otra evidencia |
| `figure_outside_cited_evidence` | media | el dato es cierto, la atribución no |
| `date_outside_cited_evidence` | media | la fecha sí existe en el turno |

`enforcement_ready` tiene **tres estados**, no un booleano:

- `unknown (n/m casos medidos)` — la señal no se midió en todos los casos;
- `blocked` — hay severidad alta, o hay claims publicados que el enforcing tumbaría;
- `ready` — medición completa y cero claims en riesgo.

El estado `unknown` existe porque una corrida anterior a 8.2 no lleva la señal y
"0 violaciones" sería indistinguible de "nunca se midió": un gate que dice listo
por datos ausentes es peor que no tener gate. Verificado: re-puntuando la última
corrida real da `unknown (0/64 casos medidos)`.

**La señal se reporta, nunca puntúa.** El scorer no cambia de veredicto por
procedencia; encender el enforcing es decisión del flag, con datos delante.

## FASE 8.2C — degradación de evidencia consciente de estructura

### El problema: truncar añadía ruido y borraba verdad a la vez

Un payload por encima de `MAX_TOOL_RESULT_CHARS` se sustituía por
`{"truncated": True, "preview": "<json crudo>"}`. Medido sobre 80 filas reales:

| | antes |
|---|---|
| paths | **todos muertos** → cualquier calculation daba `calc_unresolved` |
| grounding | **73 tokens minados del preview**, incluidos fragmentos cortados a mitad de número (`0036`) |
| valores | **44 de 80 legítimos dejaban de ser citables** |

El `preview` es un string, y el tokenizador numérico de 8.1H lo minaba: texto
serializado se convertía en cifras citables que nunca fueron valores. Y al mismo
tiempo los valores que no cabían desaparecían.

### La regla: encoger colecciones, no aplastar la forma

`_truncate_view` ahora recorre la estructura y **reduce las listas** —la más
grande primero, para que una colección enorme no le cueste sus filas a las
demás— conservando objetos, claves y tipos. Devuelve además `omitted_rows`, un
mapa `path → filas descartadas`.

| | después |
|---|---|
| forma | preservada; `resolve_path("e1.data.items.0.cantidad")` → `1000` |
| grounding | 40 tokens, **todos valores reales retenidos**; cero minados de texto |
| valores descartados | **ninguno queda citable** |
| aritmética sobre filas visibles | sigue verificando |

Si tras encoger todas las listas el payload sigue excedido (escalares enormes,
no filas), se conservan las **claves** y se descartan los valores: nunca se emite
texto que el tokenizador pueda minar.

### `count` sobre una colección degradada se refuta

La longitud de una lista encogida no es la real, así que contarla publicaría una
cardinalidad falsa — justo la clase de cifra segura-pero-falsa que el verifier
existe para impedir. `CollectionTruncated` la rechaza; el claim se descarta y la
cifra no se publica. `count` sobre una colección intacta sigue funcionando.

El pack lleva `truncated` y `omitted_rows`, así que el modelo ve explícitamente
qué se degradó y cuánto.

### Seguridad de la cadena — verificado

| vector | resultado |
|---|---|
| contaminación entre turnos | stores independientes; `EvidenceStore` es por turno y en RAM |
| spoofing de `evidence_id` | citar ids inexistentes no ayuda: la cifra ungrounded se descarta antes de evaluar procedencia |
| escape de paths (`../`, `__class__`, `e1.__dict__`) | `KeyError`; `resolve_path` solo recorre la estructura de evidencia |
| PII y secretos | eliminados antes de degradar, así que la vista degradada no los contiene; tampoco el pack |

### Contrato para quien venga después

- La evidencia se **degrada**, nunca se aplasta: si tocas `_truncate_view`,
  mantén la forma o romperás paths, `count` y grounding a la vez.
- Nada que provenga de serializar evidencia a texto puede acabar en el conjunto
  de cifras citables.
- Una colección con filas descartadas **no puede sostener una cardinalidad**.
- Los `evidence_id` deben seguir visibles en el pack aunque su contenido se
  omita: la procedencia de 8.2 se apoya en ellos.
