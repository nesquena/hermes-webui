# PRD — Neo WebUI

> **Status:** rascunho aprovado para execução
> **Versão:** 2.2
> **Última atualização:** 2026-05-09
> **Owner:** Júnior Melo (`@melojrx`)
> **Repositório:** este fork de `nesquena/hermes-webui`
>
> **Changelog v2.2 (2026-05-09)**
> Painel Agentes (EP-AG) sai de "in-scope futuro genérico" e ganha **arquitetura
> definida**: caminho **🅲 Híbrido** — front bundlado do `pixel-agents-standalone`
> servido pelo `server.py` Neo + adaptador Python (`api/agents_activity.py`) que
> traduz `state.db` + SSE Hermes para o protocolo `ServerMessage` que o front
> já entende. Justificativa, RFs e RNFs atualizados; HUs detalhadas em
> `BACKLOG.md` EP-AG; plano de execução em `TASKS.md` Sprint 7+.

---

## 1. Visão

Transformar o Hermes WebUI em uma interface web personalizada do **Neo**, agente
pessoal/executivo de Júnior Melo. A interface é composta por **3 páginas
principais** — **Dashboard** (chat + hero + KPIs + ações rápidas), **Projetos**
(command center local-first com Kanban + Lista para tarefas de projetos e
referências externas) e **Finanças** (KPIs + gráficos SVG + transações) —
unificadas por uma **sidebar fixa de 240px** com 9 itens de
navegação e uma **topbar contextual** com status da VPS em tempo real.

A identidade visual segue o skin "neo" (paleta cyan/azul-neon escuro,
tipografia Inter, avatar humanoide holográfico) conforme os mockups
[`neo_agent_web_ui.png`](./neo_agent_web_ui.png) (Dashboard),
[`neo_projetos.png`](./neo_projetos.png) (Projetos) e
[`neo_financas.png`](./neo_financas.png) (Finanças), e o
[DESIGN-SPEC.md](./DESIGN-SPEC.md) v3.2 que é a **fonte de verdade visual**.

A WebUI continua sendo a **mesma instância** que conversa com o runtime Hermes
em `127.0.0.1:8642`; o que muda é o **chrome** (branding, layout, novos painéis)
e a inclusão de visualizações específicas do uso pessoal/profissional do Júnior
(dashboard executivo, quadro de projetos, controle financeiro, mapa de
subagentes futuro).

---

## 2. Persona e contexto

### Usuário primário — Júnior Melo

- Servidor público no MGI/COTIN, empreendedor (300 Soluções), pai, dev
  (`melojrx`), em terapia. Domínios distintos com regras de segurança
  distintas (MGI sensível vs. pessoal/finanças/projetos vs. terapia).
- Já opera o Neo via Telegram, WhatsApp, CLI e WebUI. Esta WebUI é o canal
  preferido em desktop — quer uma **central de comando** que mostre o estado
  do trabalho de relance e permita executar ações rápidas.
- Domínio técnico avançado (Python, Django, agentes). Prefere **manutenibilidade
  e código limpo** acima de novidades.

### Cenários de uso recorrentes

1. **Bom dia operacional.** Abrir a WebUI, ver no dashboard: jobs cron rodando,
   sessões abertas, projetos em andamento por status, atalhos rápidos (nova
   conversa, novo projeto, salvar memória).
2. **Foco em projeto.** Abrir um projeto no Kanban (ex: Brabus, FinanPy,
   Obreiro Virtual), ver tasks por coluna (Em Andamento / Em Revisão /
   Concluído), conversar com o Neo nesse contexto.
3. **Operação de projeto.** Pedir ao Neo no chat para implementar algo em um
   projeto, acompanhar a tarefa no painel Projetos e, em sprint futura, criar
   ou sincronizar a KAN/issue no Jira correto.
4. **Delegação visual (futuro próximo — EP-AG).** Abrir a aba **Agentes** e ver,
   em tempo real, o Neo orquestrador e os subagentes (MGI / Projetos / Finanças
   / Terapia / Pessoal) trabalhando em paralelo, com a tool ativa, o domínio,
   o profile, o estado (ativo/aguardando/idle/permission). A visualização é
   inspirada em [`pablodelucca/pixel-agents`](https://github.com/pablodelucca/pixel-agents)
   (escritório pixel-art com personagens andando), reaproveitando o fork
   `pixel-agents-standalone` por **embedding híbrido** (ver §6 RF-AG.1).
5. **Operação rápida.** Salvar uma nota no vault Obsidian, abrir terminal,
   executar uma skill — tudo sem sair da WebUI.

---

## 3. Problema

O Hermes WebUI atual entrega paridade técnica com a CLI, mas tem chrome
genérico (sidebar de sessões + chat + workspace). Para o uso pessoal do
Júnior, falta:

- **Identidade visual** alinhada ao Neo (não "Hermes" com gold).
- **Visão executiva**: o que está acontecendo agora, o que está pendente,
  ações rápidas — sem precisar abrir cada painel.
- **Visão de projetos**: hoje, "tasks" são jobs cron e "todos" são tasks de
  sessão; o conceito de **projeto pessoal/profissional como agregador**
  (Brabus, FinanPy, MGI, …) não existe na UI.
- **Controle financeiro**: não há visão de receitas, despesas, saldo, metas
  financeiras ou gráficos de evolução na WebUI.
- **Localização pt-BR completa** (atualmente o i18n cobre en/es/zh/ru parciais;
  pt-BR não existe).
- **Sidebar e topbar contextuais**: a sidebar atual é genérica (lista de
  sessões); falta navegação dedicada por domínio, status do agente, recursos
  da VPS.
- **Mapa de delegação** (futuro): hoje as `subagent cards` aparecem em linha
  no chat, sem visão consolidada de "o que cada subagente está fazendo".

---

## 4. Objetivos e métricas de sucesso

### Objetivos

| ID | Objetivo | Como medimos |
|---|---|---|
| O1 | Rebrand completo Hermes → Neo, sem regredir capacidades | Suíte `pytest tests/` continua passando (3309 testes); 0 referências a "Hermes" no chrome visível ao usuário (logo, título, placeholder, notificações) |
| O2 | Dashboard executivo funcional como tela inicial | Tempo até primeira ação útil ≤ 3s após login; cards exibem dados reais (sessões, cron, projetos) com auto-refresh |
| O3 | Command Center de projetos com Kanban + Lista integrado a sessões, tarefas e refs externas | Drag-and-drop persistido server-side; 4 colunas (Backlog / Em Andamento / Em Revisão / Concluído); Lista agrupada por status; filtros por projeto/status/prioridade/fonte/responsável/data; tasks com `external_ref` preparado para Jira/GitHub/Obsidian |
| O4 | Localização pt-BR completa | 100% das chaves do `i18n.js` traduzidas para `pt-BR`; lint de paridade de chaves passa |
| O5 | Manutenibilidade do fork preservada | Merge upstream `nesquena/hermes-webui` em < 30 min para releases minor; conflitos isolados a arquivos Neo-only |
| O6 | Skin "neo" como tema oficial e default no ambiente Neo | `HERMES_WEBUI_DEFAULT_SKIN=neo` ativa skin sem flicker; alternar skin em `/skin neo` funciona |
| O7 | Página Finanças com shell visual completo | KPI cards (Receitas/Despesas/Saldo/Investimentos), gráfico de linha SVG, donut de categorias, lista de transações — com dados demo no MVP; backend real pós-MVP |

### Não-objetivos (explícitos — não está no escopo agora)

- ❌ **Painel "Design"** mostrado no mockup → removido do escopo nesta iteração.
- ❌ Substituir framework / introduzir build step / SPA / React/Svelte.
- ❌ Reescrever backend ou alterar APIs públicas do Hermes.
- ❌ Mobile-first redesign (manter responsividade existente; não reinventar).
- ⏳ Painel "Agentes" com mapeamento de delegação ao vivo continua **fora do
  MVP (Sprints 1–6)**, mas passa a ter arquitetura definida (caminho 🅲
  Híbrido) e entra como sprint dedicada **pós-MVP** — ver EP-AG no `BACKLOG.md`
  e Sprint 7+ no `TASKS.md`. Não pré-requisita reprodução 1:1 do `pixel-agents`.
- ❌ Backend financeiro real (sincronização com bancos / OFX / FinanPy) —
  MVP entrega **shell visual** com dados de demonstração; integração real
  é pós-MVP.
- ❌ Gráficos com libs externas (Chart.js, D3, ApexCharts) — SVG vanilla
  conforme DESIGN-SPEC §8.6.

---

## 5. Escopo

### In-scope (MVP — Sprints 1 a 4)

1. **Rebrand visual e textual** (logo, favicon, título, placeholders, paleta
   skin "neo", avatar humanoide SVG, tipografia Inter).
2. **Localização pt-BR** completa.
3. **Sidebar fixa (240px)** com 9 itens de navegação, card de status do Neo,
   recursos VPS (CPU/RAM/Disco/Rede) e footer.
4. **Topbar contextual (56px)** com VPS Status, Uptime, Região, Versão,
   Terminal SSH, busca, notificações, admin dropdown.
5. **Página Dashboard** (chat central SSE + hero avatar holográfico + KPIs
   com deltas + ações rápidas grid 2×3).
6. **Página Projetos (Command Center local-first)** com **Kanban de 4 colunas**
   (Backlog / Em Andamento / Em Revisão / Concluído), vista Lista, filtros
   operacionais, cards com chips de categoria/prioridade/fonte externa,
   barra de progresso, drag-and-drop e persistência local com `external_ref`.
7. **Página Finanças (shell visual)** com 4 KPI cards (Receitas / Despesas /
   Saldo Líquido / Investimentos), gráfico de linha SVG vanilla, donut de
   gastos por categoria, coluna lateral (Orçamentos / Transações Recentes /
   Metas Financeiras), modal "+ Nova Finança".
8. **Documentação técnica** completa (este diretório `docs/neo/`).
9. **Testes** seguindo padrão dos sprints upstream (arquivo
   `tests/test_neo_sprintN.py` por sprint, conftest reutilizado).

### In-scope (pós-MVP — Sprint 5+)

10. **Painel Agentes (mapa de delegação multi-agente — EP-AG)** —
    visualização do Neo orquestrador + subagentes ativos no painel `agents`,
    seguindo o **caminho 🅲 Híbrido** decidido em 2026-05-09:
    - Front: bundle Vite/React/Canvas2D do `pixel-agents-standalone` servido
      estaticamente pelo `server.py` Neo em `/static/agents-app/`.
    - Backend: novo módulo Neo-only `api/agents_activity.py` que lê
      `state.db` (relação parent/child de sessões) + assina o SSE existente
      em `streaming.py` (eventos `tool_use`/`tool_result`/`delegate_task`) e
      traduz para o protocolo `ServerMessage` (`agentCreated`,
      `agentToolStart`, `agentToolDone`, `subagentToolStart`, …) que o front
      do pixel-agents já consome — sem fork divergente, sem WebSocket extra,
      sem build step na neo-webui.
    - Entregável: a aba `agents` deixa de ser placeholder e mostra o Neo +
      subagentes em tempo real. Ver `BACKLOG.md` EP-AG e `TASKS.md` Sprint 7+.
11. **Backend financeiro real** — integração com FinanPy API, OFX import,
    sincronização de bancos.
12. **Sincronização Jira / fontes externas** — múltiplos Jiras, criação de KAN
    a partir do chat, importação de issues, sync de status, refs GitHub e
    Obsidian.
13. **Refinamentos** identificados em homologação.

### Out-of-scope (esta iniciativa)

- Painel "Design".
- Mobile redesign além do que já existe.
- Geração de imagens / vídeos (já está no backlog do Neo, fora desta WebUI).
- Substituir transports/providers do runtime (responsabilidade do Hermes).
- Libs de gráficos externas (Chart.js, D3, ApexCharts).

---

## 6. Requisitos

### Funcionais (RF)

| ID | Requisito | Prioridade |
|---|---|---|
| RF-01 | Topbar, favicon, logo, título da aba e notificações exibem "Neo" (ou nome configurado em `HERMES_WEBUI_BOT_NAME`) | P0 |
| RF-02 | Skin "neo" disponível no seletor de Settings e via slash command `/skin neo`; persiste em `localStorage` + `settings.json` | P0 |
| RF-03 | Locale pt-BR disponível e selecionável; chaves cobrem 100% do que o `en` cobre hoje | P0 |
| RF-04 | Painel "Dashboard" carrega como tela inicial quando `?panel=dashboard` ou quando `settings.default_panel="dashboard"` | P0 |
| RF-05 | Dashboard exibe: chat central (mesmo SSE da sessão ativa), hero avatar holográfico, 4 KPI cards com deltas (Projetos Ativos / Tarefas em Andamento / Concluídas / Agentes Online), ações rápidas grid 2×3 (6 botões). O chat do Dashboard deve preservar o composer completo do Hermes WebUI, incluindo anexos, microfone/voz quando disponível, seletor de profile, workspace, modelo configurado, reasoning/effort e demais controles atuais do rodapé do chat | P0 |
| RF-06 | Painel "Projetos" exibe Kanban full-page com **4 colunas**: `backlog`, `em_andamento`, `em_revisao`, `concluido`, e vista Lista agrupada por status | P0 |
| RF-07 | Cada card de tarefa exibe: título, chip de categoria (Design/Frontend/Backend/Database/Infra/DevOps/Docs/QA/Segurança), chip de prioridade (Baixa/Média/Alta), fonte externa quando houver, barra de progresso com percentual | P0 |
| RF-08 | Drag-and-drop entre colunas atualiza status da tarefa e persiste via `PATCH /api/project-tasks/{task_id}` com `{ status: "backlog|em_andamento|em_revisao|concluido" }` | P0 |
| RF-09 | Criar/editar/arquivar projeto e tarefa via UI; projeto/tarefa pode ser vinculado a sessões existentes via `session.project_id` e refs locais | P1 |
| RF-10 | Locale pt-BR cobre Dashboard, Kanban e Finanças (chaves novas) | P0 |
| RF-11 | Painel Agentes (EP-AG, pós-MVP): aba `agents` deixa de ser placeholder e exibe o Neo orquestrador + subagentes em execução em tempo real, reusando o front do `pixel-agents-standalone` (embedding híbrido). Detalhamento em RF-AG.* | P1 (pós-MVP) |
| RF-12 | Sidebar fixa (240px) com 9 itens de navegação (Dashboard, Projetos, Tarefas, Pessoal, Finanças, Agentes, Skills, Automação, Configurações), card de status do Neo com botão "Conversar agora", recursos VPS (CPU/RAM/Disco/Rede com barras de progresso, poll 30s via `GET /api/health/vps`), footer com links Documentação/Suporte | P0 |
| RF-13 | Topbar contextual (56px) com VPS Status + pill ONLINE, Uptime, Região, Versão, botão Terminal SSH, ícones busca/notificações/help, admin dropdown. Dados via `GET /api/health/system` com poll 30s | P0 |
| RF-14 | Página Finanças com: header + 4 KPI cards (Receitas/Despesas/Saldo Líquido/Investimentos), gráfico de linha SVG vanilla (receitas × despesas por mês), donut de gastos por categoria, coluna lateral (Orçamentos/Transações Recentes/Metas Financeiras), modal "+ Nova Finança" com tabs Receita/Despesa/Investimento | P0 |
| RF-15 | Persistência financeira em `~/.hermes/webui/finance.json` (Neo-only); endpoints `GET/POST /api/finance/*` | P0 |
| RF-16 | Barra de status (pills) na página Projetos com contadores clicáveis: Total, Backlog, Em Andamento, Revisão, Concluído | P0 |
| RF-17 | Avatar humanoide Neo em SVG (wireframe holográfico cyan) com 3 variantes: hero (240×220), mark (40×40), mono (favicon). Animações CSS: hover-float 4s, pulse-glow 3s | P0 |
| RF-18 | Tarefas de projeto persistem `external_ref` opcional (`type`, `source_id`, `key`, `url`, `status`, `synced_at`) sem chamar APIs externas na Sprint 5 | P0 |
| RF-AG.1 | O front do `pixel-agents-standalone` é servido pelo `server.py` Neo em `/static/agents-app/` (bundle estático produzido por `npm run build` no fork e copiado em build-time). Nenhum processo Node novo roda em produção na VPS; a neo-webui continua sendo o único serviço Python/HTTP exposto pelo `hermes-webui.service`. | P0-AG |
| RF-AG.2 | O front se conecta a um endpoint SSE Neo-only `GET /api/agents/stream` (em vez do WebSocket original do pixel-agents). O endpoint emite mensagens no mesmo formato `ServerMessage` esperado pelo front (`agentCreated`, `agentClosed`, `agentStatus`, `agentToolStart`, `agentToolDone`, `agentToolsClear`, `subagentToolStart`, `subagentToolDone`, `subagentClear`). | P0-AG |
| RF-AG.3 | A fonte de dados do `/api/agents/stream` é o módulo `api/agents_activity.py`, que: (a) lê `state.db` para descobrir sessões ativas e suas relações pai/filho via `parent_session_id`; (b) assina os eventos SSE já existentes em `streaming.py` (tool_use/tool_result/delegate_task); (c) traduz tools Hermes (`delegate_task`, `memory`, `obsidian-mcp`, `web_search`, `execute_code`, `terminal_run`, …) para textos curtos pt-BR equivalentes ao `formatToolStatus()` do upstream pixel-agents. | P0-AG |
| RF-AG.4 | A aba `agents` da sidebar Neo, hoje placeholder, passa a montar o app embarcado via `mountDashboardAgents()` (mesmo padrão de `mountDashboardSettings`/`mountDashboardSkills`): carrega o bundle de `/static/agents-app/` em um `<iframe sandbox="allow-scripts allow-same-origin">` ou em um container `<div>` com `<script type="module">` — decisão fechada na PoC (HU-AG.0). | P0-AG |
| RF-AG.5 | A fonte (Neo orquestrador) e os subagentes do Neo (MGI / Projetos / Finanças / Terapia / Pessoal) são exibidos com nomes pt-BR e cor por domínio; o orquestrador aparece como o personagem central e cada `delegate_task` em vôo aparece como subagente vinculado (linha tênue ou ícone de "trabalhando para X"). | P1-AG |
| RF-AG.6 | O painel só carrega o bundle (≈ XKB) quando a aba `agents` é aberta pela primeira vez (lazy-load); o SSE só abre quando o painel está visível e fecha quando o usuário sai da aba — para zero custo em runtime quando ninguém está olhando. | P0-AG |
| RF-AG.7 | O painel exibe um estado vazio amigável (ilustração + copy pt-BR) quando não há sessões ativas: "Nenhum agente trabalhando agora. O Neo aparece aqui quando você ou um job iniciar uma conversa." | P0-AG |

### Não-funcionais (RNF)

| ID | Requisito | Critério |
|---|---|---|
| RNF-01 | Sem build step | Servir `static/*` direto, sem bundler/transpiler |
| RNF-02 | Compatibilidade com upstream | `pytest tests/` continua verde após mudanças (3309+ testes) |
| RNF-03 | Performance de carga | Dashboard renderiza em ≤ 200 ms com dados em cache; API agregada custa ≤ 1 round-trip |
| RNF-04 | Acessibilidade | Manter `aria-label`, `role`, contraste WCAG AA; novos painéis seguem o padrão dos existentes |
| RNF-05 | Internacionalização | Toda string nova passa por `t(...)` ou `data-i18n` — proibido hardcode em pt-BR ou en no DOM |
| RNF-06 | Manutenibilidade | Nenhum patch monkey-patch em arquivos upstream; novas features em arquivos Neo-only quando viável (ver `UPSTREAM-SYNC.md`) |
| RNF-07 | Segurança | Reaproveitar middleware de auth existente; não introduzir novas rotas sem cookie de auth quando aplicável |
| RNF-08 | Footprint na VPS — Painel Agentes | Adicional ≤ **30 MB de RAM** com painel aberto (≤ 5 MB com painel fechado, pois o SSE só é aberto on-demand); CPU adicional ≤ **2 %** em idle e ≤ **5 %** durante atividade típica de 1 sessão; nenhum processo extra fora do `hermes-webui.service` (sem Node, sem WebSocket separado, sem novo systemd). Medido com `ps_mem` + `pidstat` em PoC de 24 h antes do release. |
| RNF-09 | Gráficos SVG vanilla | Gráficos de linha e donut implementados em SVG inline sem libs externas; animações via CSS (`stroke-dashoffset`). Módulo: `static/finance.js` |
| RNF-10 | Tipografia Inter | Fonte `Inter` (400/500/600/700) via Google Fonts; `--font-ui` no skin "neo" usa `'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif` |
| RNF-11 | Baixa regressão no chat | O Dashboard não deve duplicar nem reimplementar lógica de chat/composer. Deve reaproveitar o fluxo upstream sempre que possível, para manter compatibilidade com seleção de modelo, workspace, profile, uploads, voz, reasoning e transporte SSE |
| RNF-12 | Painel Agentes — sem build step na neo-webui | O bundle do `pixel-agents-standalone` é gerado **fora do runtime** (no fork dele, com `npm run build`) e versionado/copiado para `static/agents-app/` na neo-webui. A neo-webui em produção **não roda Node** e **não exige `npm install`**. RNF-01 (sem build step) permanece intacto para o repositório principal. |
| RNF-13 | Painel Agentes — fork do `pixel-agents-standalone` mínimo e isolável | Toda customização Neo (cliente SSE em vez de WebSocket, paleta cyan, copy pt-BR) é feita em **arquivos próprios** no fork do `pixel-agents-standalone` (ex: `webview-ui/src/neo/sse-client.ts`, `webview-ui/src/neo/i18n.ts`), nunca por monkey-patch dos arquivos originais. Objetivo: poder dar `git pull` no upstream `pablodelucca/pixel-agents` (via base `pixel-agents-standalone`) sem conflito. |
| RNF-14 | Painel Agentes — segurança e auth | O endpoint `/api/agents/stream` reusa **o mesmo middleware de auth do resto da WebUI** (cookie de sessão Neo). O bundle `/static/agents-app/` é servido como qualquer outro asset estático e respeita o `<base href>` dinâmico do `index.html` para subpath mount. Nenhuma porta nova é aberta na VPS. |

---

## 7. Restrições

- **Stack:** Python 3.12, vanilla JS, sem framework, sem build step.
- **Hospedagem:** VPS modesta (`srvjosemaria`, Ubuntu 24.04 KVM). Soluções
  pesadas (websocket-pesado para Agentes ao vivo) precisam de prova de
  conceito antes de virar default.
- **Domínio:** `neo.investiorion.com` (HTTPS via nginx + Let's Encrypt). Não
  vamos quebrar `subpath mount` (existe em `index.html` o `<base href>`
  dinâmico — manter).
- **Upstream:** continuar mergeable com `nesquena/hermes-webui`. Mudanças que
  exigem editar arquivos "core" (ex: `routes.py`, `streaming.py`) precisam
  de justificativa em PRD/BACKLOG.

---

## 8. Riscos

| Risco | Impacto | Mitigação |
|---|---|---|
| Conflito de merge ao puxar upstream | Médio | Estratégia "aditiva" + arquivos Neo-only documentada em `UPSTREAM-SYNC.md`; merge upstream antes de cada sprint |
| Painel Agentes pesar na VPS | Médio | Caminho 🅲 Híbrido limita custo (RNF-08); PoC de 24 h obrigatória antes do release (HU-AG.0); SSE só aberto quando o painel está visível (RF-AG.6) |
| Adapter `state.db` ↔ `ServerMessage` ficar acoplado a internals do Hermes | Médio | Isolar leitura em `api/agents_activity.py` (Neo-only, sem editar arquivos upstream); cobrir contratos com testes em `tests/test_neo_agents_*.py`; documentar em `UPSTREAM-SYNC.md` que mudanças de schema do `state.db` no Hermes podem exigir refatoração só desse módulo |
| Fork divergente do `pixel-agents-standalone` | Médio | RNF-13 — customização Neo em arquivos novos (`webview-ui/src/neo/*`), nunca por edição dos originais; documento `pixel-agents-standalone/NEO-FORK.md` lista as 3 ou 4 alterações exatas |
| Bundle do pixel-agents pesar muito (assets de tilesets) | Baixo | MVP do EP-AG usa apenas tileset default (sem o pacote pago de 452 peças); medir tamanho final do bundle e fazer code-splitting se passar de 500 KB gzipped |
| Hardcodes residuais "Hermes" pelo código | Baixo | Lint próprio (`scripts/lint_neo_branding.sh`) na CI antes do release; aceita exceções em comentários e termos técnicos (`HERMES_HOME`, `~/.hermes/`) |
| pt-BR ficar parcial / dessincronizado | Médio | Teste de paridade de chaves (já existe para `es` em `tests/`); replicar para `pt-BR` |
| Drag-and-drop instável em mobile | Baixo | Aceitar fallback "mover para coluna X" via menu de contexto no mobile |

---

## 9. Histórias de usuário (resumo, detalhes em `BACKLOG.md`)

```
Como Júnior Melo
Quero ver, ao abrir a WebUI, um Dashboard com estado do meu Neo
Para começar o dia já operacional, sem precisar entrar em cada painel.

Como Júnior Melo
Quero organizar minhas sessões e jobs cron em projetos com Kanban
Para enxergar prioridades de Brabus, FinanPy, MGI, etc. em uma tela.

Como Júnior Melo
Quero usar a WebUI 100% em pt-BR
Para evitar fricção de tradução em uso diário.

Como Júnior Melo
Quero a UI vestida de Neo (paleta cyan, hero, logo)
Para que a interface reflita a marca do meu segundo cérebro.

Como Júnior Melo
Quero abrir a aba Agentes da WebUI Neo e ver, em pixel-art, o Neo
orquestrador junto dos subagentes ativos (MGI, Projetos, Finanças, etc.)
trabalhando — com a tool atual e o domínio de cada um
Para entender visualmente a delegação em paralelo sem precisar abrir log.

Como mantenedor do fork
Quero que o Painel Agentes não introduza Node em produção, não consuma
mais que ~30 MB de RAM e seja desligável
Para não comprometer a VPS modesta nem a manutenibilidade do fork.
```

---

## 10. Critérios globais de aceite ("Definition of Done")

Uma HU só é considerada **concluída** quando todos os itens abaixo são
verdadeiros e marcados em `TASKS.md`:

- [ ] **Código** segue padrão do projeto (mesmo estilo dos `*.js` upstream;
      Python segue `api/` modular).
- [ ] **Testes automatizados** novos passam (`pytest tests/test_neo_*`) e a
      suíte completa segue verde.
- [ ] **Homologação manual** em ambiente local (`localhost:8787`) e em
      staging (`https://neo.investiorion.com`) com checklist de cenários.
- [ ] **Evidências** anexadas: screenshots no caminho `docs/neo/evidencias/<HU-ID>/`
      ou link para PR/comentário.
- [ ] **Sem regressão**: nenhuma chave i18n perdida, nenhum painel upstream
      quebrado, suíte completa verde.
- [ ] **Documentação** atualizada quando houver mudança contratual (rota
      nova, setting novo, skin nova).

---

## 11. Referências

- **DESIGN-SPEC (fonte de verdade visual):** [`DESIGN-SPEC.md`](./DESIGN-SPEC.md) v3.1
- Mockups: [`neo_agent_web_ui.png`](./neo_agent_web_ui.png) (Dashboard), [`neo_projetos.png`](./neo_projetos.png) (Projetos), [`neo_financas.png`](./neo_financas.png) (Finanças)
- Protótipo Replit (referência visual apenas): `https://dd84ecdc-9bfc-44ed-abb7-eed4b5b43eb6-00-29ndcxm0907el.kirk.replit.dev/neo-dashboard/`
- Doc operacional do Neo: `~/Documentos/Obsidian Vault/02-Projetos/Neo-Segundo-Cerebro-Documentacao.md`
- Arquitetura WebUI: [`ARCHITECTURE.md`](../../ARCHITECTURE.md) (raiz do repo)
- Sprints históricas upstream: [`SPRINTS.md`](../../SPRINTS.md), [`ROADMAP.md`](../../ROADMAP.md)
- Inspiração para painel Agentes: https://github.com/pablodelucca/pixel-agents
- Fork standalone (base do EP-AG): `/home/jrmelo/Projetos/pixel-agents-standalone`
- Análise de viabilidade EP-AG (caminho 🅲 Híbrido): registrada em
  `BACKLOG.md` § EP-AG → "Decisão arquitetural (2026-05-09)"
