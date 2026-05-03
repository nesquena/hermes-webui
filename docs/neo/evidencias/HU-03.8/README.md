# HU-03.8 — Card de status Neo na sidebar

Data: 2026-05-02

## Implementação

- `static/index.html` renderiza card de status na sidebar esquerda com avatar `neo-ico.png` circular e pill "ONLINE".
- O botão "Conversar agora" foi removido na refinamento visual da Sprint 2 (2026-05-02): o card exibe apenas identidade e status; a navegação para o chat é feita pelo item Dashboard na rail.
- `static/style.css` define `.neo-sidebar-status-card` e estilos do avatar circular e pill de status.

## Validação

- `.venv/bin/pytest tests/test_neo_dashboard_shell_visual.py -q` → passou
- Homologação visual manual realizada em 2026-05-02.

## Pendências de DoD

- Screenshot do card de status na sidebar em runtime.
- Homologação manual registrada em 2026-05-02; anexo de screenshot pendente de release.
