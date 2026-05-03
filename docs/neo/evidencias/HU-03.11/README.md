# HU-03.11 — Recursos VPS na sidebar

Data: 2026-05-02

## Implementação

- `api/health.py` passou a expor métricas com fonte declarada em cada item.
- CPU usa `/proc/stat` com fallback em load average.
- RAM usa `/proc/meminfo`.
- Disco usa `shutil.disk_usage(Path.home())`.
- Rede usa `/proc/net/dev` e normalização por taxa de bytes desde a leitura anterior.
- `static/dashboard.js` mantém polling de `/api/health/vps` a cada 30s e atualiza as barras.

## Validação

- `.venv/bin/python -m py_compile api/health.py api/routes.py api/config.py`
- `.venv/bin/pytest tests/test_neo_health_runtime.py -q` -> coberto na suíte Neo atual.
- `.venv/bin/pytest tests/test_neo_font_ui_inter.py tests/test_neo_dashboard_kpis.py tests/test_neo_skin.py tests/test_neo_branding_assets.py tests/test_neo_pt_br_toasts.py tests/test_neo_dashboard_sprint2.py tests/test_neo_skin_localstorage_persistence.py tests/test_neo_hero_greeting.py tests/test_neo_dashboard_chat_embed.py tests/test_neo_dashboard_shell_visual.py tests/test_neo_dashboard_quick_actions.py tests/test_locale_parity_pt_br.py tests/test_neo_dashboard_admin_personal.py tests/test_neo_health_runtime.py -q` -> `67 passed in 1.99s`

## Pendências de DoD

- Screenshot desktop/mobile da sidebar VPS em runtime quando exigido pelo release.
