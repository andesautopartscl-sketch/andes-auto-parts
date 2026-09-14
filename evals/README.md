# FASE 4 — NL evaluation

Dataset gold and local eval runner for the Andes assistant planner.

## Files

| File | Role |
|------|------|
| `fase4_nl_dataset.jsonl` | 36 gold cases (S/M/A/F/P/X/O; ≥32) |
| `fase4_scorer.py` | Automatic scoring + beta gate |
| `fase4_runner.py` | Runs orchestrator (fake or llm) and writes scores |
| `docs/fase4_nl_eval_report.md` | Metrics + go/no-go beta local |

## Run (fake — CI / no key)

```powershell
cd C:\AndesAutoParts
$env:ANDES_ASSISTANT_NL_ENABLED = "0"
$env:ANDES_ORCH_PLANNER = "fake"
.\.venv\Scripts\python.exe -m evals.fase4_runner --mode fake
```

## Run (llm — local only)

```powershell
$env:ANDES_ENV = "local"
$env:ANDES_ASSISTANT_NL_ENABLED = "1"
$env:ANDES_ORCH_PLANNER = "llm"
# Key only in env or ANDES_LLM_API_KEY_FILE one-shot
.\.venv\Scripts\python.exe -m evals.fase4_runner --mode llm
```

The runner restores `NL=0` / `ORCH=fake` after llm mode.

Outputs under `data/fase4_eval/` (gitignored if under data patterns — keep reports local).

## Beta gate (auto)

See scorer `beta_gate()` and `docs/fase4_nl_eval_report.md`.
