# TASKS — Neo WebUI (Documento Vivo)

> Plano operacional alinhado ao [PRD.md](./PRD.md) v2.1, ao
> [BACKLOG.md](./BACKLOG.md) e ao [DESIGN-SPEC.md](./DESIGN-SPEC.md) v3.2.
> Este arquivo acompanha execução, evidências e Definition of Done; mudanças
> contratuais devem ser feitas primeiro no PRD/Backlog/Design Spec.

**Atualizado em:** 2026-05-01
**Versão alvo MVP:** `neo-webui-v0.1` ao final da Sprint 4
**Branch atual registrada no handoff:** `neo/sprint-1` sem commits

---

## Como usar este documento

1. Cada HU usa o mesmo ID do [BACKLOG.md](./BACKLOG.md).
2. Tasks técnicas ficam em checklist (`- [ ]`) e podem ser marcadas antes da HU fechar.
3. Uma HU só vira **concluída** quando cumprir a Definition of Done do [PRD.md §10](./PRD.md#10-critérios-globais-de-aceite-definition-of-done).
4. Evidências ficam em `docs/neo/evidencias/<HU-ID>/`.
5. Status da HU:
   - `disponível`: pronta para iniciar
   - `em andamento`: há código/documentação em progresso
   - `implementada sem DoD`: implementação existe, mas faltam testes/evidências/homologação
   - `bloqueada`: há dependência explícita
   - `concluída`: DoD completo

---

## Resumo de status

| Sprint | Tema | HUs | Implementadas no worktree | Concluídas por DoD | Status |
|---|---:|---:|---:|---:|---|
| Sprint 1 | Rebrand visual/textual + locale pt-BR | 11 | 7 parciais | 0 | em andamento |
| Sprint 2 | Dashboard + sidebar/topbar Neo | 11 | 0 | 0 | aguardando Sprint 1 |
| Sprint 3 | Projetos/Kanban 4 colunas | 10 | 0 | 0 | aguardando Sprint 2 |
| Sprint 4 | Ações rápidas + Finanças shell visual | 13 | 0 | 0 | aguardando Sprint 3 |
| Transversal | Qualidade, testes e evidências | 5 | 0 | 0 | em andamento contínuo |
| Sprint 5+ | Painel Agentes futuro | 5 | 0 | 0 | depende de PoC |

### Estado atual do worktree

Arquivos modificados e não commitados nesta análise:

- `.gitignore`
- `api/config.py`
- `api/routes.py`
- `static/boot.js`
- `static/index.html`
- `static/style.css`

Achados do worktree:

- `static/style.css`, `static/index.html`, `static/boot.js`, `api/config.py` e
  `api/routes.py` já têm parte da implementação de `skin=neo` e defaults via
  `HERMES_WEBUI_DEFAULT_SKIN` / `HERMES_WEBUI_LOCALE`.
- Não há alterações em `static/i18n.js`, `static/commands.js`,
  `static/manifest.json`, favicons, assets `static/brand/*`, testes Neo ou
  scripts de lint.
- A DoD ainda não está fechada para nenhuma HU porque não há execução de testes,
  evidências anexadas ou homologação registrada neste arquivo.

---

## Inconsistências e decisões registradas

| Item | Inconsistência | Decisão operacional neste TASKS |
|---|---|---|
| I-01 | O `TASKS.md` anterior dizia Sprint 4 = "Ações rápidas + Polimento" e Sprint 5+ = Agentes, mas o PRD/Backlog colocam **Finanças** como EP-06 P0 na Sprint 4. | Sprint 4 passa a conter EP-05 + EP-06. Agentes fica Sprint 5+ / P2. |
| I-02 | O `TASKS.md` anterior especificava Kanban com **3 colunas**; PRD, Backlog e Design Spec exigem **4 colunas**: Backlog / Em Andamento / Em Revisão / Concluído. | Todas as tasks de Projetos foram atualizadas para 4 colunas e status `backlog`. |
| I-03 | O handoff marca HU-01.1, HU-01.3, HU-01.4 e HU-01.6 como validadas visualmente, mas PRD §10 exige testes, evidências e homologação para concluir. | Essas HUs ficam como **implementadas sem DoD** até evidências/testes serem anexados. |
| I-04 | `DESIGN-SPEC.md §12` mapeava drag-and-drop de Projetos para `HU-04.7`, mas o Backlog define drag-and-drop como `HU-04.4` e `HU-04.7` como "+ Adicionar tarefa". | Corrigido no Design Spec em 2026-05-01. Este TASKS segue o Backlog: drag-and-drop = HU-04.4; adicionar tarefa = HU-04.7. |
| I-05 | PRD RF-08 exige `POST /api/projects/{id}`, enquanto o upstream atual já tem rotas `/api/projects/create`, `/rename`, `/delete`. | Implementar rota Neo compatível com RF-08 ou documentar adapter; não reutilizar somente `/rename` para mudança de status. |
| I-06 | PRD RF-15 pede persistência financeira em `finance.json`, enquanto o texto de não-objetivos fala em "backend financeiro real pós-MVP". | Interpretação: `finance.json` + endpoints locais são P0 do MVP; integrações bancárias/FinanPy/OFX são pós-MVP. |
| I-07 | `DESIGN-SPEC.md §13` listava "Backlog no Kanban" como pendência para atualizar RF-06, mas o PRD já está atualizado com 4 colunas. | Pendência removida do Design Spec em 2026-05-01; PRD/Backlog/TASKS permanecem em 4 colunas. |
| I-08 | `.gitignore` modificado passa a ignorar `docs/neo/`, mas PRD §5 inclui documentação técnica completa em `docs/neo/` como entrega. | Se estes docs devem entrar no repositório, remover `docs/neo/` do `.gitignore` ou usar `git add -f` deliberadamente. |
| I-09 | UPSTREAM-SYNC lista `HERMES_WEBUI_DEFAULT_PANEL`, mas PRD RF-04 fala em `settings.default_panel="dashboard"` e `?panel=dashboard`. | Sprint 2 deve suportar ambos: setting persistido e env como default inicial quando não houver escolha local. |
| I-10 | O checklist anterior citava `localStorage.hermes-locale`, mas o upstream usa `localStorage['hermes-lang']` em `static/i18n.js`, `static/boot.js` e `static/panels.js`. | Manter `hermes-lang` para preservar compatibilidade e preferências já salvas; documentação operacional passa a citar essa chave. |

---

## Definition of Done por HU

Antes de marcar uma HU como `concluída`, preencher:

- [ ] Código segue padrão upstream e comentários `NEO:` em edições de core.
- [ ] Testes Neo específicos passam (`pytest tests/test_neo_*` ou arquivo dedicado).
- [ ] Suíte relevante upstream passa, ou há justificativa explícita se não rodada.
- [ ] Homologação manual local registrada.
- [ ] Evidências em `docs/neo/evidencias/<HU-ID>/`.
- [ ] Documentação atualizada quando houver rota, setting, endpoint, skin ou contrato novo.

---

## Sprint 1 — Rebrand visual/textual + locale pt-BR

**Meta:** abrir a WebUI com identidade Neo, skin cyan/azul-neon e pt-BR completo,
sem regressão das capacidades upstream.

**Pré-condições**

- [ ] Confirmar branch de trabalho (`git status --short`, `git branch --show-current`).
- [ ] Decidir se `docs/neo/` será versionado apesar do `.gitignore` atual.
- [ ] Rodar baseline de testes relevante antes do commit.

### HU-01.1 — Topbar, título e notificações exibem "Neo"

**Status:** implementada sem DoD
**Prioridade:** P0
**Épico:** EP-01

**Tasks**

- [x] Confirmar suporte upstream a `bot_name` / `HERMES_WEBUI_BOT_NAME`.
- [x] Registrar no handoff que `.env` local usa `HERMES_WEBUI_BOT_NAME=Neo`.
- [ ] Verificar topbar, `<title>`, placeholder do composer e notificações em runtime.
- [ ] Atualizar `static/manifest.json` para `name` e `short_name` Neo, se ainda fizer parte desta HU.
- [ ] Anexar screenshots em `docs/neo/evidencias/HU-01.1/`.
- [ ] Rodar teste/lint relevante.

### HU-01.2 — Logo "NEO" e avatar/mark humanoide

**Status:** disponível
**Prioridade:** P0
**Épico:** EP-01
**Dependências:** DESIGN-SPEC §9

**Tasks**

- [ ] Criar `static/brand/neo-avatar.svg` conforme wireframe humanoide do Design Spec.
- [ ] Criar `static/brand/neo-avatar-mono.svg`.
- [ ] Criar `static/brand/neo-mark.svg` para sidebar/topbar.
- [ ] Trocar caduceu por asset Neo via hook mínimo em `static/boot.js` ou módulo Neo-only.
- [ ] Garantir `aria-label` e `<title>` nos SVGs.
- [ ] Testar legibilidade em dark/light.
- [ ] Anexar screenshots.

### HU-01.3 — Skin "neo" selecionável

**Status:** implementada sem DoD
**Prioridade:** P0
**Épico:** EP-01

**Tasks**

- [x] Adicionar `:root[data-skin="neo"]` em `static/style.css`.
- [x] Adicionar `:root.dark[data-skin="neo"]` em `static/style.css`.
- [x] Adicionar `neo` ao allowlist inicial em `static/index.html`.
- [x] Adicionar `Neo` ao array `_SKINS` em `static/boot.js`.
- [x] Adicionar `neo` a `_SETTINGS_SKIN_VALUES` em `api/config.py`.
- [x] Adicionar/confirmar opção no seletor de Settings se `_SKINS` não for suficiente.
- [ ] Ajustar `--font-ui` para Inter conforme PRD RNF-10 e Design Spec §3.
- [x] Rodar testes de skin ou criar `tests/test_neo_sprint1.py`.
- [ ] Anexar evidência antes/depois.

**Evidência técnica:** [`docs/neo/evidencias/HU-01.3/README.md`](./evidencias/HU-01.3/README.md)

### HU-01.4 — Skin "neo" default via env

**Status:** implementada sem DoD
**Prioridade:** P0
**Épico:** EP-01
**Dependências:** HU-01.3

**Tasks**

- [x] Ler `HERMES_WEBUI_DEFAULT_SKIN` em `api/config.py`.
- [x] Injetar `__NEO_DEFAULT_SKIN__` em `api/routes.py`.
- [x] Aplicar default no early boot de `static/index.html` quando `localStorage.hermes-skin` estiver vazio.
- [ ] Testar `localStorage.clear()` + reload com e sem env.
- [ ] Criar teste automatizado para placeholder injetado / allowlist.
- [ ] Anexar evidências.

### HU-01.5 — Favicon e PWA icons Neo

**Status:** disponível
**Prioridade:** P0
**Épico:** EP-01
**Dependências:** HU-01.2

**Tasks**

- [ ] Substituir `static/favicon.svg`.
- [ ] Substituir `static/favicon-32.png`.
- [ ] Substituir `static/favicon.ico`.
- [ ] Criar/atualizar `static/apple-touch-icon.png`, se o arquivo existir no projeto.
- [ ] Atualizar `static/manifest.json`.
- [ ] Validar aba do navegador e PWA instalada.

### HU-01.6 — `/skin neo` aplica skin ao vivo

**Status:** implementada sem DoD
**Prioridade:** P1 no Backlog, tratado como P0 operacional por RF-02
**Épico:** EP-01
**Dependências:** HU-01.3

**Tasks**

- [x] Confirmar que `Neo` em `_SKINS` torna o skin disponível para o fluxo comum.
- [x] Verificar se `static/commands.js` usa `_SKINS` dinamicamente ou lista própria.
- [x] Se houver lista própria, adicionar `neo` explicitamente.
- [ ] Testar autocomplete e persistência em `localStorage` + `settings.json`.
- [ ] Anexar screencast curto.

**Evidência técnica:** [`docs/neo/evidencias/HU-01.6/README.md`](./evidencias/HU-01.6/README.md)

### HU-02.1 — Locale `pt-BR` com paridade do `en`

**Status:** implementada sem DoD
**Prioridade:** P0
**Épico:** EP-02

**Tasks**

- [x] Em `static/i18n.js`, criar bloco `pt-BR` cobrindo 100% das chaves de `en`.
- [x] Usar `docs/neo/_pt-BR-missing-keys.txt` como checklist auxiliar.
- [x] Traduzir termos conforme vocabulário do Neo: Sessão, Conversa, Memória, Skills/Habilidades, Jobs Cron, Áreas de trabalho.
- [x] Adicionar `pt-BR` ao seletor de idiomas se não for auto-descoberto.
- [x] Criar `tests/test_locale_parity_pt_br.py`.
- [ ] Anexar screenshot da UI em pt-BR.

**Evidência técnica:** [`docs/neo/evidencias/HU-02.1/README.md`](./evidencias/HU-02.1/README.md)

### HU-02.2 — `pt-BR` default via env

**Status:** implementada sem DoD
**Prioridade:** P0
**Épico:** EP-02
**Dependências:** HU-02.1

**Tasks**

- [x] Ler `HERMES_WEBUI_LOCALE` em `api/config.py`.
- [x] Injetar `__NEO_DEFAULT_LOCALE__` em `api/routes.py`.
- [x] Aplicar locale default no boot quando `localStorage['hermes-lang']` estiver vazio.
- [x] Testar `localStorage.clear()` + reload com `HERMES_WEBUI_LOCALE=pt-BR`.
- [x] Garantir fallback seguro quando `pt-BR` não existir.

**Evidência técnica:** [`docs/neo/evidencias/HU-02.2/README.md`](./evidencias/HU-02.2/README.md)

### HU-02.3 — Teste de paridade pt-BR vs en

**Status:** implementada sem DoD
**Prioridade:** P0
**Épico:** EP-02 / EP-07

**Tasks**

- [x] Criar `tests/test_locale_parity_pt_br.py`.
- [x] Falhar se qualquer chave de `en` não existir em `pt-BR`.
- [x] Falhar se houver chave extra órfã sem justificativa.
- [ ] Adicionar comando de execução ao checklist de PR.

**Evidência técnica:** [`docs/neo/evidencias/HU-02.3/README.md`](./evidencias/HU-02.3/README.md)

### HU-02.4 — Traduções novas de Dashboard, Kanban e Finanças

**Status:** disponível
**Prioridade:** P0
**Épico:** EP-02
**Dependências:** Sprints 2-4

**Tasks**

- [ ] Adicionar chaves `dashboard_*`.
- [ ] Adicionar chaves `sidebar_*` e `topbar_*`.
- [ ] Adicionar chaves `projects_*`.
- [ ] Adicionar chaves `finance_*`.
- [ ] Garantir que strings novas usem `t(...)` ou `data-i18n`.

### HU-02.5 — Erros e toasts em pt-BR

**Status:** disponível
**Prioridade:** P0
**Épico:** EP-02

**Tasks**

- [ ] Inventariar toasts e erros visíveis.
- [ ] Cobrir mensagens novas Neo.
- [ ] Validar login, settings, projetos e finanças.

### Encerramento Sprint 1

- [ ] HU-01.1 a HU-02.5 concluídas por DoD.
- [ ] `pytest tests/test_neo_sprint1.py tests/test_locale_parity_pt_br.py -v` passa.
- [ ] Suíte relevante upstream passa.
- [ ] Evidências anexadas.
- [ ] Commit limpo sem `.env`.

---

## Sprint 2 — Dashboard + sidebar/topbar Neo

**Meta:** Dashboard executivo como tela inicial, com chat central, hero, KPIs,
ações rápidas, sidebar fixa de 240px e topbar contextual de 56px.

### HU-03.1 — Painel "Dashboard" na sidebar

**Status:** aguardando Sprint 1

**Tasks**

- [ ] Adicionar item Dashboard na sidebar de 9 itens.
- [ ] Criar painel `dashboard` sem quebrar painel `chat` upstream.
- [ ] Criar `static/dashboard.js`.
- [ ] Carregar `loadDashboard()` por feature detection.

### HU-03.2 — Dashboard como painel inicial

**Status:** aguardando Sprint 1

**Tasks**

- [ ] Suportar `?panel=dashboard`.
- [ ] Adicionar `settings.default_panel`.
- [ ] Ler `HERMES_WEBUI_DEFAULT_PANEL` como default inicial quando não houver escolha local.
- [ ] Preservar default upstream (`chat`) sem env/setting.

### HU-03.3 — Hero avatar humanoide + saudação

**Status:** aguardando HU-01.2

**Tasks**

- [ ] Renderizar `neo-avatar.svg` na coluna direita.
- [ ] Implementar saudação contextual pt-BR.
- [ ] Exibir pill `STATUS: OPERACIONAL`.
- [ ] Implementar animações `hover-float` e `pulse-glow`.

### HU-03.4 — 4 KPI cards com deltas

**Status:** aguardando Sprint 1

**Tasks**

- [ ] Criar `api/dashboard.py`.
- [ ] Criar `GET /api/dashboard/summary`.
- [ ] Agregar Projetos Ativos, Tarefas em Andamento, Concluídas, Agentes Online.
- [ ] Renderizar grid 2x2 responsivo.
- [ ] Clicar em card navega para painel correspondente.

### HU-03.5 — Chat central no Dashboard

**Status:** aguardando Sprint 1

**Tasks**

- [ ] Embutir o mesmo SSE da sessão ativa.
- [ ] Reutilizar a lista de mensagens upstream, preservando renderização de markdown, arquivos, tool calls e estados de streaming.
- [ ] Reutilizar o composer/toolstrip completo upstream; não criar um segundo composer paralelo em `dashboard.js`.
- [ ] Preservar anexos e fluxo de upload/preview.
- [ ] Preservar microfone/voz quando disponível no ambiente.
- [ ] Preservar seletor de profile ativo.
- [ ] Preservar seletor de workspace ativo.
- [ ] Preservar seletor de modelo configurado.
- [ ] Preservar seletor de reasoning/effort.
- [ ] Preservar menus auxiliares e demais controles já existentes no rodapé do chat.
- [ ] Aplicar apenas adaptação visual Neo ao container/composer: fundo, borda, radius, espaçamento, botão enviar cyan e responsividade.
- [ ] Manter painel `chat` direto funcional.
- [ ] Focar composer ao abrir Dashboard.
- [ ] Testar troca de modelo, workspace, profile e effort dentro do Dashboard.
- [ ] Testar envio com anexo dentro do Dashboard.
- [ ] Validar mobile/tablet: toolstrip pode quebrar linha, mas não pode ocultar controles, cortar labels ou sobrepor elementos.

### HU-03.6 — Topbar contextual

**Status:** aguardando Sprint 1

**Tasks**

- [ ] Criar `GET /api/health/system`.
- [ ] Exibir VPS Status, Uptime, Região, Versão.
- [ ] Adicionar botão Terminal SSH.
- [ ] Adicionar busca/notificações/help/admin dropdown.
- [ ] Poll a cada 30s com cache.

### HU-03.7 — Ações rápidas grid 2x3

**Status:** aguardando Sprint 1

**Tasks**

- [ ] Renderizar Novo Projeto.
- [ ] Renderizar Novo Documento.
- [ ] Renderizar Novo Componente.
- [ ] Renderizar Abrir Terminal.
- [ ] Renderizar Gerar Relatório.
- [ ] Renderizar Deploy Projeto.
- [ ] Definir comportamento de placeholders sem backend.

### HU-03.8 — Card de status Neo na sidebar

**Status:** aguardando Sprint 1

**Tasks**

- [ ] Exibir mark/avatar Neo.
- [ ] Exibir status ONLINE.
- [ ] Botão "Conversar agora" navega para Dashboard e foca composer.

### HU-03.9 — Admin dropdown

**Status:** aguardando Sprint 1
**Prioridade:** P1

**Tasks**

- [ ] Menu Perfil / Configurações / Logout.
- [ ] Reusar handlers existentes quando disponíveis.

### HU-03.10 — Painel mínimo "Pessoal"

**Status:** aguardando Sprint 2
**Prioridade:** P1

**Tasks**

- [ ] Criar placeholder útil com perfil + preferências.
- [ ] Linkar Settings.
- [ ] Definir escopo futuro de notas pessoais.

### HU-03.11 — Recursos VPS na sidebar

**Status:** aguardando Sprint 1

**Tasks**

- [ ] Criar `GET /api/health/vps`.
- [ ] Exibir CPU/RAM/Disco/Rede com barras.
- [ ] Poll a cada 30s.
- [ ] Validar leitura no host/container.

---

## Sprint 3 — Projetos (Kanban 4 colunas)

**Meta:** página Projetos full-page com Kanban de 4 colunas, cards com chips,
progresso, status pills e drag-and-drop persistido.

### HU-04.1 — Página Projetos com header

**Status:** aguardando Sprint 2

**Tasks**

- [ ] Adicionar item Projetos na sidebar.
- [ ] Criar painel `projects`.
- [ ] Criar `static/kanban.js`.
- [ ] Header: título, subtítulo, Filtros, Kanban, + Novo Projeto.

### HU-04.2 — Kanban 4 colunas

**Status:** aguardando Sprint 2

**Tasks**

- [ ] Implementar colunas `backlog`, `em_andamento`, `em_revisao`, `concluido`.
- [ ] Aplicar top-border slate/amber/blue/green.
- [ ] Contagem por coluna.
- [ ] Mobile: 1 coluna com tabs.
- [ ] Tablet: scroll horizontal interno.

### HU-04.3 — Criar projeto via modal

**Status:** aguardando Sprint 2

**Tasks**

- [ ] Criar `api/projects.py` Neo-only ou adapter de rotas.
- [ ] `POST /api/projects` cria item.
- [ ] Persistir em `~/.hermes/webui/projects.json`.
- [ ] Campos: nome, categoria, prioridade, descrição, status.

### HU-04.4 — Drag-and-drop persistido

**Status:** aguardando HU-04.2

**Tasks**

- [ ] Implementar HTML5 drag-and-drop sem libs.
- [ ] `POST /api/projects/{id}` atualiza `{ status }`.
- [ ] UI otimista com rollback em erro.
- [ ] Visual drag: glow cyan, rotate 2deg, drop target destacado.

### HU-04.5 — Cards com chips e progresso

**Status:** aguardando HU-04.2

**Tasks**

- [ ] Chips de categoria: Design, Frontend, Backend, Database, Infra, DevOps, Docs, QA, Segurança.
- [ ] Chips de prioridade: Baixa, Média, Alta.
- [ ] Barra de progresso nas colunas não concluídas.
- [ ] Chip verde `Concluído` sem barra na coluna concluído.

### HU-04.6 — Status pills clicáveis

**Status:** aguardando HU-04.2

**Tasks**

- [ ] Total, Backlog, Em Andamento, Revisão, Concluído.
- [ ] Clique filtra/destaca a coluna.
- [ ] Contadores sincronizados após drag/criação.

### HU-04.7 — "+ Adicionar tarefa" por coluna

**Status:** aguardando HU-04.2

**Tasks**

- [ ] Botão dashed no footer de cada coluna.
- [ ] Criar card inline no status da coluna.
- [ ] Persistir com o mesmo endpoint de criação.

### HU-04.8 — Vincular sessão existente a projeto

**Status:** aguardando HU-04.3
**Prioridade:** P1

**Tasks**

- [ ] Usar `session.project_id` já existente.
- [ ] UI para atribuir sessão a projeto.
- [ ] Exibir vínculo no detalhe do projeto.

### HU-04.9 — Arquivar projetos concluídos

**Status:** aguardando HU-04.3
**Prioridade:** P1

**Tasks**

- [ ] Status/filtro `arquivado`.
- [ ] Toggle "Mostrar arquivados".
- [ ] Não contar arquivados em Projetos Ativos.

### HU-04.10 — Mobile com tabs

**Status:** aguardando HU-04.2
**Prioridade:** P1

**Tasks**

- [ ] Tabs por status no topo.
- [ ] Drag fallback via menu "Mover para".

---

## Sprint 4 — Ações rápidas + Finanças

**Meta:** fechar o MVP com ações rápidas operacionais e página Finanças com
shell visual completo, SVG vanilla e persistência local.

### HU-05.1 — Atalho "Salvar memória"

**Status:** aguardando Dashboard

**Tasks**

- [ ] Botão/modal pré-preenchido para salvar memória.
- [ ] Integrar com skill/fluxo existente quando disponível.

### HU-05.2 — Atalho "Novo terminal"

**Status:** aguardando Dashboard

**Tasks**

- [ ] Botão abre painel `terminal` upstream.
- [ ] Validar foco/retorno para Dashboard.

### HU-05.3 — Seletor "Executar skill"

**Status:** aguardando Dashboard

**Tasks**

- [ ] Listar skills do runtime.
- [ ] Abrir composer/comando com skill selecionada.

### HU-05.4 — Indicador de job cron concluído

**Status:** aguardando Dashboard

**Tasks**

- [ ] Consolidar evento/estado de cron no Dashboard.
- [ ] Exibir indicador visual recente.

### HU-06.1 — Página Finanças com header

**Status:** aguardando Sprint 2
**Prioridade:** P0

**Tasks**

- [ ] Adicionar item Finanças na sidebar.
- [ ] Criar painel `finance`.
- [ ] Criar `static/finance.js`.
- [ ] Header: título, subtítulo, Terminal SSH, + Nova Finança.

### HU-06.2 — 4 KPI cards financeiros

**Status:** aguardando HU-06.1

**Tasks**

- [ ] Receitas.
- [ ] Despesas.
- [ ] Saldo Líquido.
- [ ] Investimentos.
- [ ] Formatar BRL pt-BR.

### HU-06.3 — Gráfico de linha SVG vanilla

**Status:** aguardando HU-06.1

**Tasks**

- [ ] Implementar `renderLineChart(svgEl, series, options)`.
- [ ] Séries Receitas e Despesas.
- [ ] Toggle de séries.
- [ ] Dropdown de período.
- [ ] Tooltip no hover.
- [ ] Animação `stroke-dashoffset`.

### HU-06.4 — Donut "Gastos por Categoria"

**Status:** aguardando HU-06.1

**Tasks**

- [ ] Implementar `renderDonutChart(svgEl, slices, options)`.
- [ ] Legenda por categoria.
- [ ] Estado vazio.
- [ ] Animação de entrada.

### HU-06.5 — Coluna lateral financeira

**Status:** aguardando HU-06.1

**Tasks**

- [ ] Card Orçamentos.
- [ ] Card Transações Recentes.
- [ ] Card Metas Financeiras.
- [ ] Estados vazios próprios.

### HU-06.6 — Modal "+ Nova Finança"

**Status:** aguardando HU-06.1

**Tasks**

- [ ] Tabs Receita / Despesa / Investimento.
- [ ] Campos: descrição, valor, data, categoria, método, recorrência, anotações.
- [ ] Máscara/formatador BRL.
- [ ] Validação required.

### HU-06.7 — Persistência local `finance.json`

**Status:** aguardando HU-06.6

**Tasks**

- [ ] Criar `api/finance.py`.
- [ ] `GET /api/finance/summary`.
- [ ] `GET /api/finance/transactions`.
- [ ] `POST /api/finance/transactions`.
- [ ] Persistir em `~/.hermes/webui/finance.json`.
- [ ] Manter integrações FinanPy/OFX fora do MVP.

### HU-06.8 — Estados vazios financeiros

**Status:** aguardando HU-06.1

**Tasks**

- [ ] Estado vazio no Resumo Financeiro.
- [ ] CTA "Adicione sua primeira transação".
- [ ] Estados vazios nos cards laterais.

### HU-06.9 — Animações dos gráficos

**Status:** aguardando HU-06.3 / HU-06.4
**Prioridade:** P1

**Tasks**

- [ ] Linha: 800ms ease-out.
- [ ] Donut: 600ms ease-out.
- [ ] Respeitar `prefers-reduced-motion`.

### Encerramento Sprint 4

- [ ] Todas as HUs MVP concluídas por DoD.
- [ ] `docs/neo/CHANGELOG.md` criado com release `neo-webui-v0.1`.
- [ ] Auditoria de skins: default, ares, mono, slate, poseidon, sisyphus, charizard, sienna, neo.
- [ ] Auditoria pt-BR completa.
- [ ] Deploy staging validado.
- [ ] Tag `neo-webui-v0.1`.

---

## Transversal — EP-07 Qualidade, testes e evidências

### HU-07.1 — Testes por sprint

- [ ] Criar `tests/test_neo_sprint1.py`.
- [ ] Criar `tests/test_neo_sprint2.py`.
- [ ] Criar `tests/test_neo_sprint3.py`.
- [ ] Criar `tests/test_neo_sprint4.py`.

### HU-07.2 — Lint de branding

- [ ] Criar `scripts/lint_neo_branding.sh`.
- [ ] Falhar para "Hermes" no chrome visível.
- [ ] Permitir exceções técnicas: `HERMES_HOME`, env vars, comentários necessários.

### HU-07.3 — Evidências por HU

- [ ] Criar diretórios `docs/neo/evidencias/HU-*/` conforme necessário.
- [ ] Padronizar nomes de screenshots.
- [ ] Registrar ambiente e data em README de cada HU quando útil.

### HU-07.4 — Paridade pt-BR

- [ ] Implementar junto com HU-02.3.
- [ ] Rodar em PRs Neo.

### HU-07.5 — CI / suíte completa

- [ ] Validar CI upstream existente.
- [ ] Garantir que testes Neo entram no comando esperado.

---

## Sprint 5+ — Painel Agentes (futuro / P2)

> Não iniciar produção sem PoC de custo na VPS. Referência visual:
> `pablodelucca/pixel-agents`, sem compromisso de reprodução 1:1.

### Pré-trabalho

- [ ] Identificar fonte de dados de subagentes ativos.
- [ ] Avaliar logs `~/.hermes/logs/agent.log` e `~/.hermes/sessions/`.
- [ ] Testar reaproveitamento de SSE existente.
- [ ] Medir RAM/CPU 24h com painel ativo.
- [ ] Definir versão leve default e modo pixel opt-in.

### HUs futuras

- [ ] HU-AG.1 — Cards de subagentes ativos.
- [ ] HU-AG.2 — Histórico recente.
- [ ] HU-AG.3 — Timeline de tool calls.
- [ ] HU-AG.4 — Animação opt-in estilo pixel.
- [ ] HU-AG.5 — Custo aceitável (< 50 MB RAM, ≤ 1 req/s).

---

## Retro

### Retro Sprint 1

_A preencher após DoD da sprint._

### Retro Sprint 2

_A preencher._

### Retro Sprint 3

_A preencher._

### Retro Sprint 4

_A preencher._
