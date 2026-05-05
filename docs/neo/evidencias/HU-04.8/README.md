# HU-04.8 - Vista Lista e filtros

**Data:** 2026-05-05
**Status:** implementada

## Evidencia tecnica

- Kanban e Lista alternam sem recarregar a pagina e usam o mesmo snapshot local.
- Lista tem ordenacao, paginacao e colunas ID, tarefa, projeto, prioridade, responsavel, status e fonte.
- Filtros cobrem texto, projeto, status, prioridade, fonte externa, responsavel e prazo.
- Botao `+ Adicionar tarefa` em cada coluna abre o modal com status pre-selecionado.

## Validacao

```bash
node --check static/kanban.js
python -m pytest tests/test_neo_projects_kanban.py -q
```
