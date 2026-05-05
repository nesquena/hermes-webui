# HU-04.9 - Vincular sessao e refs externas

**Data:** 2026-05-05
**Status:** parcial

## Evidencia tecnica

- `api/projects.py` persiste `refs.sessions`, `refs.github` e `refs.obsidian` em tarefas.
- `static/kanban.js` exibe refs existentes no modal de edicao de tarefa.
- A UI de escrita para atribuir sessao Neo diretamente a uma tarefa ainda fica pendente.
- Sincronizacao automatica com GitHub, Obsidian ou Jira permanece fora da Sprint 5.

## Validacao

```bash
python -m pytest tests/test_neo_projects_api.py tests/test_neo_projects_kanban.py -q
```
