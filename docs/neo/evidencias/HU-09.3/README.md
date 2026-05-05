# HU-09.3 — Testes automáticos

**Data:** 2026-05-05
**Status:** concluída

## Evidência técnica

- Criado `tests/test_neo_dashboard_skills.py`.
- O teste cobre presença de `skills` em `NEO_SHELL_PANELS`.
- O teste cobre definição de `mountDashboardSkills()` e `restoreDashboardSkills()`.
- O teste cobre regras CSS do layout two-column no shell Neo.
- O teste cobre presença dos elementos DOM upstream necessários para lista, busca e detalhe de skills.
- O teste cobre chaves i18n essenciais em `en` e `pt-BR`.

## Validação

```bash
.venv/bin/pytest tests/test_neo_dashboard_skills.py -q
.venv/bin/pytest tests/test_neo_font_ui_inter.py tests/test_neo_dashboard_kpis.py tests/test_neo_skin.py tests/test_neo_branding_assets.py tests/test_neo_pt_br_toasts.py tests/test_neo_dashboard_sprint2.py tests/test_neo_skin_localstorage_persistence.py tests/test_neo_hero_greeting.py tests/test_neo_dashboard_chat_embed.py tests/test_neo_dashboard_shell_visual.py tests/test_neo_dashboard_quick_actions.py tests/test_locale_parity_pt_br.py tests/test_neo_dashboard_admin_personal.py tests/test_neo_health_runtime.py tests/test_neo_dashboard_settings.py tests/test_neo_dashboard_skills.py -q
```

Resultados registrados em 2026-05-05:

- `tests/test_neo_dashboard_skills.py`: `17 passed`.
- Suite Neo focada: `109 passed in 2.47s`.
