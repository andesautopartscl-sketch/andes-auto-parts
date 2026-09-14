# FASE 4 — Informe de evaluación NL (beta local)

**Checkpoint base:** `be2a61c7` · Gateway **0.11.0** · 10 tools READ-ONLY
**Fecha:** 2026-09-14
**Decisión:** **GO** (Run 4 LLM 36/36 + manual 4.8/5 + ruta M2M reparada)

## Entregables

| Artefacto | Estado |
|-----------|--------|
| `evals/fase4_nl_dataset.jsonl` | Listo — **36** casos gold (≥32) |
| `evals/fase4_scorer.py` | Listo — métricas + `beta_gate` |
| `evals/fase4_runner.py` | Listo — `--mode fake\|llm`; NL off al terminar llm; fail-fast si falta M2M |
| `tests/test_fase4_scorer.py` / `tests/test_planner_semantic_policy.py` | OK |
| Política A–D (prompts + FakePlanner) | Aprobada |
| Corrida LLM Run 4 | **36/36**; `agent_misconfigured=0` |
| Manual muestra 5 | **4.8/5** |
| Beta local | **GO** |

## Dataset (36)

Buckets: S8 · M6 · A5 · F4 · P4 · X5 · O4.

Actores: `albertadmin` (full), `e2e_kpi_nofin` (sin finanzas / stock limitado).

## Protocolo

1. Soft-enable local + LLM (o FakePlanner para harness)
2. `python -m evals.fase4_runner --mode llm|fake`
3. Scorer automático vs gold
4. Restaurado: `ANDES_ASSISTANT_NL_ENABLED=0`, `ANDES_ORCH_PLANNER=fake`

## Run 4 (referencia)

| Agregado | Valor |
|----------|-------|
| Pass rate | **100%** |
| core / multi / permission / safety | **100%** |
| WRITE / secret leaks / tools inventadas | **0** |
| `agent_misconfigured` | **0** |
| Latencia avg / p95 | ~1956 / 3264 ms |
| Manual (S01 S02 F01 X01 A01) | **4.8/5** |

## Ops / seguridad

- Defaults: `ANDES_ASSISTANT_NL_ENABLED=0`, `ANDES_ORCH_PLANNER=fake`
- M2M: `ANDES_AGENT_SERVICE_TOKEN` requerido en proceso/.env (nunca en Git)
- `data/fase4_eval/` gitignored
- Kill switch NL; PlanValidator antes de invoke; WRITE reject; evidence-only

## Fuera de alcance (cumplido)

Sin memoria, multi-cerebro, WRITE tools, Excel, nuevas tools Gateway, LlmComposer.
