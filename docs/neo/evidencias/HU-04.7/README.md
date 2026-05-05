# HU-04.7 - Status pills clicaveis

**Data:** 2026-05-05
**Status:** implementada

## Evidencia tecnica

- `#projectsStatusPills` mostra Total, Backlog, Em Andamento, Revisao e Concluido.
- `static/kanban.js` usa as pills como atalho de filtro por status.
- Contadores sao recalculados depois de carregamento, criacao, edicao e drag-and-drop.
- `static/style.css` diferencia hover e estado ativo.

## Validacao

```bash
python -m pytest tests/test_neo_projects_kanban.py -q
```
