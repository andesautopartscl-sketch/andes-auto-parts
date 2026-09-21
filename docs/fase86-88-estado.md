# Estado 8.6 / 8.7 / 8.8 — antes de la validación LLM real

Fecha del registro: 2026-09-17.
Estado congelado a la espera de la corrida con LLM que ejecutará el propietario.

---

## 1. Closure determinista: 28/28 en ambos brazos

```
OFF  arm=an0-pv0   modulos=41  tests=910  fallos=0  errores=0  probes=28/28
ON   arm=an1-pv0   modulos=41  tests=910  fallos=0  errores=0  probes=28/28
```

Artefactos por brazo (el harness ya no los pisa):

```
data/fase81_eval/fase81_final_report.an0-pv0.{json,txt}
data/fase81_eval/fase81_final_report.an1-pv0.{json,txt}
```

El brazo ON declara `ab_complete: true` y detecta a `an0-pv0` como hermano en
disco. El brazo OFF, ejecutado primero, lo declara `false` — que es correcto:
un A/B con un solo brazo no es un A/B.

En la corrida ON el limitador del Gateway obligó a `retries: 3`,
`seconds_waited: 70.0`, **`exhausted_calls: 0`**. Ninguna llamada terminó sin
respuesta válida, así que ningún veredicto se emitió sobre una respuesta de
error.

## 2. Ninguna diferencia entre brazos

```
probes que difieren entre brazos: NINGUNO
```

Es la comprobación determinista de que el análisis condicional dejó de
contaminar los turnos normales:

- el schema de un turno no analítico es el **mismo objeto**
  (`plain_schema_untouched: true`) en ambos brazos;
- el prompt de un turno normal mide 4635 caracteres en ambos brazos;
- el bloque analítico (682 caracteres) sólo entra en un turno analítico, donde
  el prompt llega a 5317.

Impuesto de tokens sobre preguntas normales con el flag encendido: **cero**.

### Dos defectos del harness corregidos al hacer esta comparación

Los dos producían falsos fallos, y sólo aparecieron al ejecutar los dos brazos
seguidos:

1. **El paquete `tests` se eclipsaba a sí mismo.** `tests/` del proyecto era un
   paquete *namespace* y `andes_agent/tests/` es *regular*; en Python un paquete
   regular gana siempre, sin importar el orden de `sys.path`. Síntoma medido:
   los 41 módulos dejaron de importar dentro del closure mientras la suite
   seguía pasando por separado. Cerrado con `tests/__init__.py`.

2. **El limitador acusaba al producto.** El Gateway limita a 60 peticiones por
   ventana de 60 s y el closure lanza 28 probes con varias llamadas cada uno.
   `answer_view` devolvía 1 bloque en vez de 2 y `analysis_ladder` 5 fallos —
   **ambos pasaban al ejecutarlos sueltos**. Un primer reintento de 8 s era
   insuficiente porque la ventana es de 60 s: el segundo intento también chocaba
   y el probe interpretaba una respuesta de *error* como fallo del producto.
   Resuelto con backoff escalonado en el único punto de estrangulamiento (había
   27 llamadas directas, y un probe nuevo habría heredado el problema).

Sin correr los dos brazos, se habrían reportado dos regresiones inexistentes.

## 3. get_sales y get_equivalences validados por HTTP real

Cadena completa: orquestador → Gateway (:5055) → ERP (:5000) → base de datos.
Ambos servicios fueron reiniciados sobre el código actual; no tienen reloader,
así que existir en disco no bastaba.

Probes en verde en ambos brazos: `sales_over_http`, `equivalences_over_http`,
`sales_capability`, `finance_redaction`, `limit_coherence`.

**get_sales** — agregados autoritativos calculados en SQL sobre el conjunto
completo, nunca derivados del detalle truncado. Neteo de notas de crédito
obligatorio y declarado. `orden_compra` rechazado en el ERP y en el Gateway: no
es una restricción de permisos sino de corrección, porque contar una compra como
venta invierte el signo. Cotizaciones fuera del alcance por defecto.

**get_equivalences** — comparación normalizada en ambos lados. `0 280 751 089` y
`0280751089` resuelven al mismo producto; con igualdad literal el 72% del
catálogo sería inalcanzable para quien teclee el código como lo teclea la gente.
`HOMOLOGADOS` viaja en su propio campo porque son modelos de vehículo, no
códigos equivalentes.

Sin PII, sin secretos, sin precios en ninguno de los dos payloads.

## 4. `es_venta_operativa = 1` — posible fuente de demanda futura

Medido en la base real:

```
es_venta_operativa = 1   283 movimientos · 414 unidades · 146 productos
                         2026-08-11 .. 2026-09-17  (~5 semanas)
                         T3311RC: 56 unidades en 13 movimientos
```

Contexto que hace útil el dato: `movimientos_stock` tiene 2.629 registros y
1.161 con cantidad negativa, pero **1.154 de esas salidas son `ajuste`** (60.546
unidades) y sólo 7 son `salida` real. Leer las salidas en bruto como demanda
sería un error de la misma clase que contar cotizaciones como ventas.

El ERP ya marca él mismo qué movimiento es una venta operativa, así que la
señal no hay que inferirla.

Cautela registrada: 5 semanas de ventana y ~2,8 unidades por producto de media
son una base fina para proyectar a dos meses. El supuesto `window_days` existe
precisamente para declararlo en vez de esconderlo.

Corrección respecto a lo afirmado en ciclos anteriores: se dijo que el
forecasting estaba bloqueado por ausencia de demanda. La demanda existe; lo que
fallaba era el sitio donde se buscó. Las ventas facturadas netas son cero porque
las dos únicas ventas fueron devueltas íntegras, pero el consumo operativo está
en `movimientos_stock`.

## 5. `get_demand` — BLOQUEADO

**No se implementa hasta terminar la validación con LLM real.**

Motivo: 8.6, 8.7 y 8.8 acumulan tres bloques sin una sola medición con modelo
real. Añadir una capacidad más antes de medir repetiría el patrón que ya costó
caro en 8.5, donde tres fases acumuladas produjeron un A/B que no midió nada.

Orden acordado:

1. corrida con LLM real, brazo `ANALYSIS=0`;
2. corrida con LLM real, brazo `ANALYSIS=1`;
3. análisis OFF vs ON sobre datos reales;
4. sólo entonces, decisión sobre `get_demand`.

---

## Estado congelado

| | |
|---|---|
| Tests | 1054 (suite completa) · 910 (subconjunto del closure) |
| Probes | 28/28 en ambos brazos |
| READ tools | 12 |
| WRITE tools | 0 |
| `AGENT` | 0 |
| `analysis_enabled` | False |
| `provenance_enforced` | False |
| Commit / push | ninguno |

Servicios en ejecución con el código actual: Gateway :5055 y ERP :5000.

Pendiente exclusivo: la corrida con LLM real, que ejecutará el propietario desde
su PowerShell, donde la clave está disponible.
