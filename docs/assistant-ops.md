# Andes Assistant — operación local (FASE 3)

Runbook para ERP + Gateway + asistente NL. **No incluye secretos ni contraseñas.**

## Arquitectura

```
UI (slash | NL) → ERP BFF /assistant/api/* → Orchestrator → Gateway :5055 → ERP /internal/agent/v1
```

- LLM Planner vive **solo en el ERP** (nunca en el Gateway).
- Gateway **0.11.0**, 10 tools READ-ONLY.
- Default: `ANDES_ASSISTANT_NL_ENABLED=0`, `ANDES_ORCH_PLANNER=fake`.

## Instalar

```powershell
cd C:\AndesAutoParts
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
# Completar ANDES_AGENT_SERVICE_TOKEN y demás secretos en .env (nunca en Git)
```

## Variables relevantes

| Variable | Default seguro | Notas |
|----------|----------------|-------|
| `ANDES_ENV` | `local` | Soft-enable solo con `local` o `staging` |
| `ANDES_ASSISTANT_NL_ENABLED` | `0` | Kill switch NL |
| `ANDES_ORCH_PLANNER` | `fake` | `llm` solo con soft-enable |
| `ANDES_LLM_API_KEY` | vacío | Nunca en Git / logs / audit |
| `ANDES_LLM_*` | ver `.env.example` | timeout 20s, retries ≤2, max tokens 800 |
| `ANDES_AGENT_URL` | `http://127.0.0.1:5055` | BFF → Gateway |
| `ANDES_AGENT_SERVICE_TOKEN` | vacío | M2M; no al browser ni al LLM |
| `ANDES_ERP_BASE_URL` | `http://127.0.0.1:5000` | Gateway → ERP |
| `ANDES_ASSISTANT_CHAT_RATE_LIMIT` | `10` | req/min/usuario; `0` desactiva |
| `ANDES_ORCH_AUDIT_PATH` | `data/orchestrator_audit.jsonl` | sin secretos |
| `ANDES_ASSISTANT_METRICS_PATH` | `data/assistant_metrics.jsonl` | métricas agregadas (FASE 6) |
| `ANDES_LLM_COST_INPUT_PER_1K` | vacío | costo estimado opcional |
| `ANDES_LLM_COST_OUTPUT_PER_1K` | vacío | costo estimado opcional |
| `ANDES_ASSISTANT_MEMORY_ENABLED` | `0` | Kill switch memoria (7B) |
| `ANDES_ASSISTANT_MEMORY_DERIVED` | `0` | Frequent/summary automáticos (7B.5) |
| `ANDES_ASSISTANT_MEMORY_EXPLICIT` | `0` | Escritura explícita (7B.3) |

Al arrancar el ERP se registra una línea `assistant_config nl=… planner=… soft_llm=…` **sin API key**.

## Iniciar ERP

```powershell
cd C:\AndesAutoParts
.\.venv\Scripts\python.exe run.py
# Puerto: 127.0.0.1:5000
```

O `iniciar_andes.bat` (solo ERP).

Health: `GET http://127.0.0.1:5000/health`

## Iniciar Gateway

```powershell
cd C:\AndesAutoParts\andes_agent
$env:PYTHONPATH = "C:\AndesAutoParts\andes_agent"
# Cargar .env del repo raíz (token, ANDES_ERP_BASE_URL, etc.)
C:\AndesAutoParts\.venv\Scripts\python.exe -m andes_agent
# Puerto: 127.0.0.1:5055
```

Health: `GET http://127.0.0.1:5055/health` → `version` `0.11.0`

Script opcional (Windows): [`scripts/start_local_assistant.ps1`](../scripts/start_local_assistant.ps1) — detecta puertos ocupados, no imprime secretos.

## Habilitar NL (local / staging controlado)

Checklist **todas** las condiciones:

1. `ANDES_ENV=local` **o** `ANDES_ENV=staging`
2. `ANDES_ASSISTANT_NL_ENABLED=1`
3. `ANDES_ORCH_PLANNER=llm`
4. `ANDES_LLM_API_KEY` no vacío
5. `ANDES_LLM_PROVIDER=openai_compatible`
6. Reiniciar el proceso ERP
7. Verificar `GET /assistant/api/capabilities` → `soft_llm_ready: true`
8. Badge del widget: **NL**

**No habilitar en production** en esta fase.

## Apagar NL (kill switch)

1. `ANDES_ASSISTANT_NL_ENABLED=0` (y/o `ANDES_ORCH_PLANNER=fake`)
2. Reiniciar ERP
3. Capabilities → `soft_llm_ready: false`
4. Badge → **Slash**; texto libre sugiere comandos `/buscar`, `/kpis`, etc.

Slash commands **siempre** funcionan vía `/assistant/api/invoke`.

## Tests

```powershell
# ERP + orchestrator (+ go-live smoke)
$env:ANDES_ASSISTANT_NL_ENABLED = "0"
$env:ANDES_ORCH_PLANNER = "fake"
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -q

# Gateway
cd andes_agent
$env:PYTHONPATH = "C:\AndesAutoParts\andes_agent"
C:\AndesAutoParts\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -q
```

Go-live smoke: `tests/test_assistant_golive_smoke.py` (fake, sin imprimir secretos).

## Health / capabilities

| Check | URL |
|-------|-----|
| ERP | `GET /health` |
| Gateway | `GET /health` |
| Asistente | `GET /assistant/api/capabilities` (sesión) |
| Métricas (local/staging) | `GET /assistant/api/metrics/summary` (sesión) |

CLI local (sin PII):

```powershell
.\.venv\Scripts\python.exe scripts/assistant_metrics_summary.py
```

Métricas: hashes + agregados (tools, latencias, tokens, hotspots). **Nunca** prompts, respuestas completas, API keys ni PII.

## Diagnóstico rápido

| Síntoma | Causa probable |
|---------|----------------|
| 401 / redirect login | Sesión expirada |
| 400 CSRF | Falta `X-CSRF-Token` en POST |
| `agent_unavailable` | Gateway apagado o puerto 5055 |
| `llm_unavailable` | Soft-enable off / provider caído / key |
| `permission_denied` | Permisos ERP del actor |
| `rate_limited` | >10 chat/min (ajustable) |
| Montos `null` / “no disponible” | Sin `ver_finanzas` (correcto; no es cero fingido) |
| Badge Slash con NL=1 | Falta key, `ORCH!=llm`, o `ANDES_ENV` no allowlisted |

## Usuario de prueba `e2e_kpi_nofin`

Usuario **local de prueba** usado en la matriz E2E de producto (sin `ver_finanzas`).
No se crean permisos automáticamente en código. **No documentar contraseñas.**

## Memoria (FASE 7B)

Default **OFF**. Store `assistant_memory_slot`. Selector 12/800. Explicit > derived.

- `ANDES_ASSISTANT_MEMORY_DERIVED=1` (y `MEMORY=1`): post-turn, best-effort. `frequent_entity` (user) y `conversation_summary` (conversation). Summary 100% determinista, sin LLM.
- Contadores pre-threshold: tabla técnica `assistant_derived_freq_counter` (mismo SQLite). **No** es memoria: no GET, no selector, no hints.
- POST/PUT explícitos siguen rechazando `frequent_entity` y `conversation_summary`.
- Detalle: [`docs/fase7b5-memory-derived.md`](fase7b5-memory-derived.md)

## Fuera de alcance (aún)

FASE 7C, aprendizaje, entrenamiento, tools WRITE, Excel, tareas pendientes, tools nuevas.
