# HU-03.3 — Hero avatar humanoide + saudação

Data: 2026-05-02

## Implementação

- `static/index.html` renderiza o hero do Dashboard na coluna direita com `static/brand/neo-avatar.svg`.
- `static/dashboard.js` calcula a saudação contextual por hora do dia e atualiza `#heroGreetingTime`.
- `static/i18n.js` contém as chaves `greeting_*` e `hero_status_online` em `en` e `pt-BR`.
- `static/style.css` define `hero-card`, `hero-avatar`, `hero-status-pill`, `hero-greeting`, `hover-float`, `pulse-glow` e fallback para `prefers-reduced-motion`.

## Validação

- `node --check static/dashboard.js`
- `.venv/bin/pytest tests/test_neo_hero_greeting.py -q` -> `8 passed in 1.09s`

## Pendências de DoD

- Screenshot desktop/mobile do Dashboard em runtime.
- Homologação manual registrada.
