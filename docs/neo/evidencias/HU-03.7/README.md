# HU-03.7 — Ações rápidas grid 2x3

Data: 2026-05-02

## Implementação

- `static/index.html` renderiza o grid de ações rápidas na coluna direita do Dashboard com 6 botões: Novo Projeto, Novo Documento, Novo Componente, Abrir Terminal, Gerar Relatório, Deploy Projeto.
- `static/dashboard.js` registra os handlers de clique; ações sem backend exibem toast informativo.
- `static/style.css` define `.quick-actions-grid`, `.quick-action-btn` com ícone 26px em container de fundo, min-height 46px e efeito hover Neo.

## Validação

- `node --check static/dashboard.js`
- `.venv/bin/pytest tests/test_neo_dashboard_quick_actions.py -q` → passou
- Homologação visual manual realizada em 2026-05-02.

## Pendências de DoD

- Screenshot do grid de ações rápidas em runtime.
- Homologação manual registrada em 2026-05-02; anexo de screenshot pendente de release.
