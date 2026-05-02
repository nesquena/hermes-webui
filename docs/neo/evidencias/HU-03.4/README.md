# HU-03.4 — 4 KPI cards com deltas

Data: 2026-05-02

## Implementação

- Criado `api/dashboard.py` com `build_dashboard_summary()`.
- Criado `GET /api/dashboard/summary` no dispatcher de rotas.
- Agregados Projetos Ativos, Tarefas em Andamento, Concluídas e Agentes Online a partir de `projects.json`, com deltas por mês, 24h e 7 dias.
- `static/index.html` inclui `#dashboardKpiGrid` na coluna direita do Dashboard.
- `static/dashboard.js` busca o resumo, renderiza 4 cards e navega com `switchPanel(card.panel)`.
- `static/style.css` define o grid 2x2 e os estados visuais dos cards.
- `static/i18n.js` recebeu labels e deltas em `en` e `pt-BR`.

## Validação

- `node --check static/dashboard.js`
- `node --check static/i18n.js`
- `.venv/bin/python -m py_compile api/dashboard.py api/routes.py`
- `.venv/bin/pytest tests/test_neo_dashboard_kpis.py -q` -> `4 passed in 1.04s`

## Pendências de DoD

- Screenshot desktop/mobile do Dashboard em runtime.
- Homologação manual registrada.
