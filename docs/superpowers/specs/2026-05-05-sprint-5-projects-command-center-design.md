# Sprint 5 Design — Projetos Command Center

**Data:** 2026-05-05
**Status:** aguardando revisão do owner
**Escopo:** Sprint 5 / EP-04 — página Projetos

## Objetivo

Construir a página **Projetos** como uma central de comando para acompanhar
projetos reais operados pelo Neo, não como um clone de Jira. A Sprint 5 entrega
um painel local-first: o WebUI persiste projetos e tarefas localmente, exibe
Kanban e Lista, e já guarda referências externas (`external_ref`) para Jira,
GitHub, Obsidian e sessões Neo. A sincronização real com Jira fica documentada
como backlog próximo.

## Decisões de Produto

- A página Projetos mostra **tarefas de projetos**, não apenas projetos.
- Um projeto é um agregador: Brabus, FinanPy, Obreiro Virtual, MGI, 300 Soluções
  ou qualquer iniciativa acompanhada pelo Neo.
- Uma tarefa pode ter origem local ou externa.
- Jira, GitHub e Obsidian não são sistemas substituídos pelo Neo WebUI; eles
  continuam sendo fontes de verdade externas quando existirem.
- Sprint 5 não chama APIs do Jira. Ela cria o modelo, a UI e a persistência que
  tornam a integração futura direta.

## Modelo de Dados

Persistência local em `~/.hermes/webui/projects.json`, com migração tolerante do
formato upstream atual, que hoje é uma lista simples de projetos.

Formato alvo:

```json
{
  "schema_version": 2,
  "sources": [],
  "projects": [],
  "tasks": [],
  "activity": []
}
```

### Project

```json
{
  "project_id": "prj_...",
  "name": "Brabus Performance Store",
  "description": "E-commerce de peças automotivas",
  "domain": "projetos",
  "status": "ativo",
  "color": "#00E5FF",
  "default_source_id": "jira_300",
  "refs": {
    "obsidian": [],
    "github": []
  },
  "created_at": 1777939200,
  "updated_at": 1777939200,
  "archived": false
}
```

### Task

```json
{
  "task_id": "tsk_...",
  "project_id": "prj_...",
  "title": "Implementar checkout",
  "description": "",
  "status": "em_andamento",
  "priority": "alta",
  "category": "Backend",
  "owner": "jr",
  "progress": 65,
  "due_date": "2026-05-15",
  "external_ref": {
    "type": "jira",
    "source_id": "jira_300",
    "key": "KAN-123",
    "url": "https://...",
    "status": "In Progress",
    "synced_at": null
  },
  "refs": {
    "github": [],
    "obsidian": [],
    "sessions": []
  },
  "created_at": 1777939200,
  "updated_at": 1777939200,
  "archived": false
}
```

### Source

```json
{
  "source_id": "jira_300",
  "type": "jira",
  "name": "Jira 300 Soluções",
  "base_url": "",
  "sync_enabled": false
}
```

`sync_enabled` permanece `false` na Sprint 5. Ele existe para registrar a
intenção sem acoplar o MVP à autenticação e variações dos três Jiras.

## Backend

Criar `api/projects.py` como módulo Neo-only. `api/routes.py` deve apenas
registrar rotas finas.

Rotas Sprint 5:

- `GET /api/projects` — retorna snapshot `{projects, tasks, sources, counts}`.
- `POST /api/projects` — cria projeto.
- `PATCH /api/projects/{project_id}` — edita projeto.
- `POST /api/project-tasks` — cria tarefa.
- `PATCH /api/project-tasks/{task_id}` — edita tarefa, inclusive status.
- `POST /api/project-tasks/{task_id}/archive` — arquiva tarefa.

Rotas antigas de projeto (`/api/projects/create`, `/rename`, `/delete`) devem
continuar funcionando para compatibilidade upstream. Se possível, elas delegam
ao novo módulo.

## Frontend

Criar `static/kanban.js`.

O painel `#mainProjects` deixa de ser placeholder e passa a ter:

- Header com titulo, subtitulo, filtros, seletor Kanban/Lista e botão Novo.
- Status pills clicáveis: Total, Backlog, Em Andamento, Revisão, Concluído.
- View Kanban aderente ao mockup `neo_projetos_kanban.png`.
- View Lista aderente ao mockup `neo_projetos_lista.png`.
- Modal de novo projeto.
- Modal ou drawer de nova tarefa.
- Drawer de detalhe da tarefa com refs externas e links.

Filtros P0:

- texto
- projeto
- status
- prioridade
- fonte externa
- responsável
- data/prazo

## Visual

Usar o padrão Neo existente, não um tema novo:

- fundo azul-noite e superfícies `--surface` / `--surface-2`
- accent cyan para ação primária e foco
- status com slate, amber, blue e green
- Inter como fonte de UI
- cards compactos, densos e escaneáveis
- bordas finas, sem cards aninhados

Direção de design: painel operacional denso, parecido com command center. A
diferença visível da Sprint 5 será a dupla Kanban/Lista e o tratamento de
origem externa por chips/links discretos.

## Backlog Próximo — Sincronização Jira

Criar épico futuro para sincronização de fontes externas. Escopo:

- cadastrar múltiplos Jiras (`jira_300`, `jira_mgi`, `jira_pessoal` ou nomes reais);
- criar issue no Jira a partir do chat e anexar `external_ref` à tarefa local;
- importar issues existentes por projeto/filtro;
- mapear status Jira para status local (`backlog`, `em_andamento`, `em_revisao`, `concluido`);
- reconciliar conflitos entre alteração local e remota;
- anexar refs GitHub e Obsidian automaticamente quando o Neo operar a tarefa;
- registrar `last_sync_at`, `sync_status` e erros por fonte.

Esse épico não entra na implementação da Sprint 5.

## Testes

Criar cobertura focada:

- `tests/test_neo_projects_api.py`
- `tests/test_neo_projects_kanban.py`

Cobrir:

- migração do formato antigo de `projects.json`;
- criação e edição de projetos;
- criação e edição de tarefas;
- atualização de status por endpoint;
- preservação de `external_ref`;
- contadores por status;
- presença das views Kanban e Lista;
- i18n `en` e `pt-BR`;
- `node --check static/kanban.js`.

## Fora do Escopo da Sprint 5

- Chamada real ao Jira.
- OAuth/token management de Jira.
- Sincronização GitHub.
- Escrita automática no Obsidian.
- Automação completa de "criar KAN no Jira" via chat.
- Timeline view funcional.

## Critérios de Aceite

- Página Projetos deixa de ser placeholder.
- Kanban e Lista funcionam com o mesmo dataset local.
- Criar projeto e tarefa persiste em `projects.json`.
- Arrastar tarefa no Kanban atualiza status e persiste.
- Lista filtra por projeto, status, prioridade, fonte, responsável, data e texto.
- Tarefas podem carregar `external_ref` sem sincronização real.
- Testes focados passam.
- Suite Neo focada passa sem regressão.
