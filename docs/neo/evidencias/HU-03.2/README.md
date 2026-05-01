# HU-03.2 — Dashboard como painel inicial

Data: 2026-05-01

## Implementação

- `static/boot.js` resolve painel inicial por `?panel=dashboard` ou `settings.default_panel`.
- `api/config.py` adiciona `default_panel` com fallback `chat` e suporte a `HERMES_WEBUI_DEFAULT_PANEL`.
- `settings.default_panel` foi adicionado ao painel de Preferences e salvo por `/api/settings`.
- Default upstream permanece `chat` quando não houver query/env/setting válido.

## Validação

- `node --check static/boot.js`
- `pytest tests/test_neo_dashboard_sprint2.py` -> `5 passed in 0.93s`

## Pendências de DoD

- Screenshot/homologação manual com `?panel=dashboard`.
- Homologação manual do setting persistido.
