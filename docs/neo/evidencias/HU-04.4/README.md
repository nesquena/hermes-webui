# HU-04.4 - Criar tarefas com external_ref

**Data:** 2026-05-05
**Status:** implementada

## Evidencia tecnica

- `POST /api/project-tasks` cria tarefa vinculada a projeto existente.
- `PATCH /api/project-tasks/{task_id}` edita tarefa.
- `api/projects.py` persiste titulo, descricao, status, categoria, prioridade, responsavel, prazo e progresso.
- `external_ref` e `refs.github`/`refs.obsidian`/`refs.sessions` sao normalizados e preservados.
- O modal de tarefa permite preencher referencia externa principal.

## Validacao

```bash
python -m pytest tests/test_neo_projects_api.py -q
```
