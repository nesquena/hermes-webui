# HU-04.5 - Drag-and-drop persistido

**Data:** 2026-05-05
**Status:** implementada

## Evidencia tecnica

- `static/kanban.js` usa HTML5 drag-and-drop sem bibliotecas externas.
- Drop em coluna atualiza status via `PATCH /api/project-tasks/{task_id}`.
- UI aplica atualizacao otimista e rollback em erro.
- `static/style.css` define estados visuais de card arrastado e drop target.

## Validacao

```bash
node --check static/kanban.js
python -m pytest tests/test_neo_projects_kanban.py -q
```
