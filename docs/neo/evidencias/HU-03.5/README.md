# HU-03.5 — Chat central no Dashboard

Data: 2026-05-02

## Implementação

- `static/dashboard.js` implementa `mountDashboardChat()` e `restoreDashboardChat()` que movem o DOM do chat upstream para o slot central do Dashboard sem duplicar compositor ou handlers.
- O compositor upstream (toolstrip completo: anexos, voz, seletores de modelo/profile/workspace/reasoning) é preservado sem modificação; apenas adaptações visuais Neo são aplicadas via CSS.
- `focusDashboardComposer()` foca o textarea ao abrir o painel.
- `static/style.css` aplica fundo, borda, radius e botão enviar cyan ao container do chat no Dashboard.

## Validação

- `node --check static/dashboard.js`
- `.venv/bin/pytest tests/test_neo_dashboard_chat_embed.py -q` → `8 passed`
- Troca de modelo, workspace, profile e effort validada dentro do Dashboard.

## Pendências de DoD

- Screenshot desktop/mobile do Dashboard com chat ativo em runtime.
- Homologação manual registrada em 2026-05-02; anexo de screenshot pendente de release.
