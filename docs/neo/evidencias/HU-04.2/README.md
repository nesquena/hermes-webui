# HU-04.2 - Kanban 4 colunas

**Data:** 2026-05-05
**Status:** implementada

## Evidencia tecnica

- `static/index.html` define colunas `backlog`, `em_andamento`, `em_revisao` e `concluido`.
- `static/kanban.js` renderiza tarefas por status e atualiza contadores por coluna.
- `static/style.css` aplica bordas slate, amber, blue e green por status.
- O layout suporta rolagem interna e adaptacao responsiva do painel Projetos.

## Validacao

```bash
python -m pytest tests/test_neo_projects_kanban.py -q
```
