# HU-03.6 — Topbar contextual

Data: 2026-05-02

## Implementação

- `static/dashboard.js` implementa popover reutilizável para ações do topbar.
- Busca usa `/api/sessions/search` e carrega a conversa selecionada com `loadSession()`.
- Notificações solicitam permissão do navegador e persistem `notifications_enabled` via `/api/settings`.
- Ajuda reutiliza `cmdHelp()` e oferece acesso rápido a Settings e ao chat.
- Admin dropdown permanece integrado a Perfil, Configurações e Logout.
- `static/style.css` adiciona estilos responsivos para busca, notificações e ajuda.
- `static/i18n.js` recebeu labels de busca/notificações em `en` e `pt-BR`.

## Validação

- `node --check static/dashboard.js`
- `node --check static/i18n.js`
- `.venv/bin/python -m py_compile api/health.py api/routes.py api/config.py`
- `.venv/bin/pytest tests/test_neo_dashboard_shell_visual.py::test_dashboard_topbar_actions_have_final_behaviors tests/test_neo_dashboard_shell_visual.py::test_dashboard_visual_shell_css_present tests/test_neo_health_runtime.py -q` -> `4 passed in 0.99s`

## Pendências de DoD

- Screenshot desktop/mobile do topbar em runtime quando exigido pelo release.
