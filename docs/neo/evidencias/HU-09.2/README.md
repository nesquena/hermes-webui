# HU-09.2 — Layout two-column + Neo styling

**Data:** 2026-05-05
**Status:** concluída

## Evidência técnica

- `static/style.css` define o layout do painel Skills dentro do dashboard shell em duas colunas.
- `#panelSkills` passa a ocupar a coluna master de 260px dentro de `#mainSkills`.
- A área de detalhe de skills preserva `flex:1` e `min-width:0`, evitando overflow lateral.
- O DOM upstream de skills permanece intacto: `#panelSkills`, `#skillsList`, `#skillsSearch`, `#mainSkills`, `#skillDetailTitle`, `#skillDetailBody` e `#skillDetailEmpty`.
- A integração não duplica handlers; reaproveita `loadSkills()`, `renderSkills()`, busca, criação, edição e deleção do painel upstream.

## Validação

```bash
node --check static/dashboard.js
.venv/bin/pytest tests/test_neo_dashboard_skills.py -q
```

Resultado registrado em 2026-05-05: `17 passed`.
