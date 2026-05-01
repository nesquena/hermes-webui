# PRD — Neo WebUI

> **Status:** rascunho aprovado para execução
> **Versão:** 2.1
> **Última atualização:** 2026-05-01
> **Owner:** Júnior Melo (`@melojrx`)
> **Repositório:** este fork de `nesquena/hermes-webui`

---

## 1. Visão

Transformar o Hermes WebUI em uma interface web personalizada do **Neo**, agente
pessoal/executivo de Júnior Melo. A interface é composta por **3 páginas
principais** — **Dashboard** (chat + hero + KPIs + ações rápidas), **Projetos**
(Kanban full-page com 4 colunas) e **Finanças** (KPIs + gráficos SVG +
transações) — unificadas por uma **sidebar fixa de 240px** com 9 itens de
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
3. **Delegação visual (futuro).** Ver subagentes que o Neo está orquestrando
   em paralelo, com domínio, profile, status — análogo à proposta do
   `pixel-agents` (https://github.com/pablodelucca/pixel-agents).
4. **Operação rápida.** Salvar uma nota no vault Obsidian, abrir terminal,
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
| O3 | Kanban de projetos com 4 colunas integrado a sessões e tasks | Drag-and-drop persistido server-side; 4 colunas (Backlog / Em Andamento / Em Revisão / Concluído); cards com chips de categoria/prioridade e barra de progresso |
| O4 | Localização pt-BR completa | 100% das chaves do `i18n.js` traduzidas para `pt-BR`; lint de paridade de chaves passa |
| O5 | Manutenibilidade do fork preservada | Merge upstream `nesquena/hermes-webui` em < 30 min para releases minor; conflitos isolados a arquivos Neo-only |
| O6 | Skin "neo" como tema oficial e default no ambiente Neo | `HERMES_WEBUI_DEFAULT_SKIN=neo` ativa skin sem flicker; alternar skin em `/skin neo` funciona |
| O7 | Página Finanças com shell visual completo | KPI cards (Receitas/Despesas/Saldo/Investimentos), gráfico de linha SVG, donut de categorias, lista de transações — com dados demo no MVP; backend real pós-MVP |

### Não-objetivos (explícitos — não está no escopo agora)

- ❌ **Painel "Design"** mostrado no mockup → removido do escopo nesta iteração.
- ❌ Substituir framework / introduzir build step / SPA / React/Svelte.
- ❌ Reescrever backend ou alterar APIs públicas do Hermes.
- ❌ Mobile-first redesign (manter responsividade existente; não reinventar).
- ❌ Painel "Agentes" com mapeamento de delegação ao vivo → vai para
  **backlog futuro** (EP-AG, ver `BACKLOG.md`); avaliar viabilidade na VPS
  antes de comprometer.
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
6. **Página Projetos (Kanban full-page)** com **4 colunas** (Backlog / Em
   Andamento / Em Revisão / Concluído), status pills, cards com chips de
   categoria/prioridade e barra de progresso, drag-and-drop.
7. **Página Finanças (shell visual)** com 4 KPI cards (Receitas / Despesas /
   Saldo Líquido / Investimentos), gráfico de linha SVG vanilla, donut de
   gastos por categoria, coluna lateral (Orçamentos / Transações Recentes /
   Metas Financeiras), modal "+ Nova Finança".
8. **Documentação técnica** completa (este diretório `docs/neo/`).
9. **Testes** seguindo padrão dos sprints upstream (arquivo
   `tests/test_neo_sprintN.py` por sprint, conftest reutilizado).

### In-scope (pós-MVP — Sprint 5+)

10. **Painel Agentes (mapa de delegação)** — explorar opção leve para a VPS
    atual, inspirado em `pablodelucca/pixel-agents`; entrega depende de
    prova de conceito de custo de runtime.
11. **Backend financeiro real** — integração com FinanPy API, OFX import,
    sincronização de bancos.
12. **Refinamentos** identificados em homologação.

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
| RF-06 | Painel "Projetos" exibe um Kanban full-page com **4 colunas**: `backlog`, `em_andamento`, `em_revisao`, `concluido` | P0 |
| RF-07 | Cada card de projeto exibe: título, chip de categoria (Design/Frontend/Backend/Database/Infra/DevOps/Docs/QA/Segurança), chip de prioridade (Baixa/Média/Alta), barra de progresso com percentual | P0 |
| RF-08 | Drag-and-drop entre colunas atualiza status e persiste via `POST /api/projects/{id}` com `{ status: "backlog|em_andamento|em_revisao|concluido" }` | P0 |
| RF-09 | Criar/editar/arquivar projeto via UI; um projeto pode ser vinculado a sessões existentes via `session.project_id` (campo já presente em `models.py`) | P1 |
| RF-10 | Locale pt-BR cobre Dashboard, Kanban e Finanças (chaves novas) | P0 |
| RF-11 | Painel Agentes (BACKLOG): rota `/api/agents/active` enumera subagentes ativos pelo runtime; UI lista cards animados estilo "pixel-agents" | P2 |
| RF-12 | Sidebar fixa (240px) com 9 itens de navegação (Dashboard, Projetos, Tarefas, Pessoal, Finanças, Agentes, Skills, Automação, Configurações), card de status do Neo com botão "Conversar agora", recursos VPS (CPU/RAM/Disco/Rede com barras de progresso, poll 30s via `GET /api/health/vps`), footer com links Documentação/Suporte | P0 |
| RF-13 | Topbar contextual (56px) com VPS Status + pill ONLINE, Uptime, Região, Versão, botão Terminal SSH, ícones busca/notificações/help, admin dropdown. Dados via `GET /api/health/system` com poll 30s | P0 |
| RF-14 | Página Finanças com: header + 4 KPI cards (Receitas/Despesas/Saldo Líquido/Investimentos), gráfico de linha SVG vanilla (receitas × despesas por mês), donut de gastos por categoria, coluna lateral (Orçamentos/Transações Recentes/Metas Financeiras), modal "+ Nova Finança" com tabs Receita/Despesa/Investimento | P0 |
| RF-15 | Persistência financeira em `~/.hermes/webui/finance.json` (Neo-only); endpoints `GET/POST /api/finance/*` | P0 |
| RF-16 | Barra de status (pills) na página Projetos com contadores clicáveis: Total, Backlog, Em Andamento, Revisão, Concluído | P0 |
| RF-17 | Avatar humanoide Neo em SVG (wireframe holográfico cyan) com 3 variantes: hero (240×220), mark (40×40), mono (favicon). Animações CSS: hover-float 4s, pulse-glow 3s | P0 |

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
| RNF-08 | Footprint na VPS | Painel Agentes (futuro) não pode adicionar mais de 50 MB de RAM nem requisição extra > 1/s |
| RNF-09 | Gráficos SVG vanilla | Gráficos de linha e donut implementados em SVG inline sem libs externas; animações via CSS (`stroke-dashoffset`). Módulo: `static/finance.js` |
| RNF-10 | Tipografia Inter | Fonte `Inter` (400/500/600/700) via Google Fonts; `--font-ui` no skin "neo" usa `'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif` |
| RNF-11 | Baixa regressão no chat | O Dashboard não deve duplicar nem reimplementar lógica de chat/composer. Deve reaproveitar o fluxo upstream sempre que possível, para manter compatibilidade com seleção de modelo, workspace, profile, uploads, voz, reasoning e transporte SSE |

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
| Painel Agentes pesar na VPS | Médio | Manter como BACKLOG (P2); só ativar após PoC com métrica de custo de RAM/CPU |
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

Como Júnior Melo (futuro)
Quero ver os subagentes que o Neo está orquestrando agora
Para entender visualmente a delegação em paralelo (estilo pixel-agents).
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
