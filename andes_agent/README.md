# Andes Agent Gateway — Paso 11 (`get_dashboard_kpis`)

Servicio **aislado** del ERP. READ-ONLY. **Sin LLM** (el planner NL vive en el ERP).

Versión Gateway: **0.11.0** — 10 tools.

Operación conjunta ERP+Gateway+NL: ver [`docs/assistant-ops.md`](../docs/assistant-ops.md).

## Comandos (slash vía BFF ERP)

- `/kpis` → snapshot
- `/kpis 7d` / `/kpis 30d` / `/kpis hoy` / `/kpis mes`
- `/kpis YYYY-MM-DD YYYY-MM-DD` → rango custom (máx. 90 días)
- Quick actions: `sales_7d`, `top_products`

Montos requieren `ver_finanzas` (si no → `null`, nunca `0` fingido).
Stock crítico requiere `ver_stock` (si no → sección omitida).
