# HU-04.1 - Pagina Projetos com header

**Data:** 2026-05-05
**Status:** implementada

## Evidencia tecnica

- `static/index.html` substitui o placeholder `#mainProjects` por um painel Projetos completo.
- Header inclui titulo, subtitulo, botao Filtros, alternancia Kanban/Lista e acao `+ Novo Projeto`.
- `static/index.html` carrega `static/kanban.js` antes de `static/panels.js`.
- `static/panels.js` chama `loadProjectsCommandCenter()` ao abrir o painel `projects`.

## Validacao

```bash
node --check static/kanban.js
python -m pytest tests/test_neo_projects_kanban.py -q
```
