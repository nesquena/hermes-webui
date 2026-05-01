# HU-03.1 — Painel "Dashboard" na sidebar

Data: 2026-05-01

## Implementação

- Adicionado item Dashboard na rail e na navegação mobile/sidebar.
- Criado `panelDashboard` na sidebar sem remover ou alterar o `panelChat`.
- Criado `mainDashboard` como view principal independente.
- Criado `static/dashboard.js` com `loadDashboard()` idempotente.
- `switchPanel()` passa a tratar `dashboard` como main view e chama `loadDashboard()` por feature detection.

## Validação

- `node --check static/dashboard.js`
- `node --check static/panels.js`
- `pytest tests/test_neo_dashboard_sprint2.py` -> `5 passed in 0.93s`

## Pendências de DoD

- Screenshot desktop/mobile do Dashboard em runtime.
- Homologação manual registrada.
