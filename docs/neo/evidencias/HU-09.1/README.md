# HU-09.1 — mountDashboardSkills + NEO_SHELL_PANELS

**Data:** 2026-05-05
**Status:** concluída

## Evidência técnica

- `static/panels.js` inclui `skills` em `NEO_SHELL_PANELS`.
- `switchPanel('skills')` chama `mountDashboardSkills()` antes de `loadSkills()`.
- A saída de outros painéis chama `restoreDashboardSkills()` junto ao padrão já usado por Dashboard Chat e Settings.
- `static/dashboard.js` implementa `mountDashboardSkills()` com anchor comment para mover `#panelSkills` para `#mainSkills`.
- `static/dashboard.js` implementa `restoreDashboardSkills()` para devolver `#panelSkills` à posição original.

## Validação

```bash
node --check static/dashboard.js
node --check static/panels.js
.venv/bin/pytest tests/test_neo_dashboard_skills.py -q
```

Resultado registrado em 2026-05-05: `17 passed`.
