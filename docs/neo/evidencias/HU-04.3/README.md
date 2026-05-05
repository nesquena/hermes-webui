# HU-04.3 - Criar projeto via modal

**Data:** 2026-05-05
**Status:** implementada

## Evidencia tecnica

- `api/projects.py` implementa schema v2 local-first para `projects.json`.
- `GET /api/projects` retorna `{projects, tasks, sources, counts}`.
- `POST /api/projects` cria projeto e `PATCH /api/projects/{project_id}` edita projeto.
- Rotas legadas `/api/projects/create`, `/rename` e `/delete` delegam ao modulo novo.
- `static/index.html` e `static/kanban.js` implementam modal de criar/editar projeto.

## Validacao

```bash
python -m pytest tests/test_neo_projects_api.py -q
```
