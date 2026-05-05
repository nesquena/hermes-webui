# HU-04.6 - Cards com chips e progresso

**Data:** 2026-05-05
**Status:** implementada

## Evidencia tecnica

- Cards exibem projeto, titulo, categoria, prioridade, responsavel, prazo e fonte externa discreta.
- Tarefas nao concluidas mostram barra de progresso.
- Tarefas concluidas exibem chip `Concluido` sem barra de progresso.
- Categorias e prioridades usam chaves i18n e classes de estilo dedicadas.

## Validacao

```bash
python -m pytest tests/test_neo_projects_kanban.py -q
```
