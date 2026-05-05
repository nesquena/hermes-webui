# HU-04.10 - Arquivar e mobile

**Data:** 2026-05-05
**Status:** parcial

## Evidencia tecnica

- `api/projects.py` suporta arquivamento de tarefas e projetos.
- `GET /api/projects?include_archived=1` alimenta o toggle `Mostrar arquivados`.
- Contadores ativos ignoram itens arquivados.
- `static/style.css` reduz o Kanban para uma coluna em mobile e usa as pills como filtro por status no topo.
- Fallback por menu `Mover para` ainda fica pendente para uso sem drag-and-drop.

## Validacao

```bash
python -m pytest tests/test_neo_projects_api.py tests/test_neo_projects_kanban.py -q
```
