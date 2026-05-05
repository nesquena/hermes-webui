# Backlog — Neo WebUI

> Backlog completo organizado por **épicos**. Cada épico agrupa HUs relacionadas
> e tem objetivo, dependências e prioridade. Detalhamento de tasks técnicas
> e critérios de aceite vai para [TASKS.md](./TASKS.md) (documento vivo).
>
> **Legenda de prioridade:** P0 (MVP), P1 (próximo ciclo), P2 (futuro / depende
> de PoC), P3 (ideia, sem compromisso de data).

---

## Mapa de épicos

| Épico | Tema | Prioridade | Sprint-alvo |
|---|---|---|---|
| [EP-01](#ep-01--rebrand-visual-e-textual) | Rebrand visual e textual | P0 | Sprint 1 |
| [EP-02](#ep-02--localização-pt-br) | Localização pt-BR | P0 | Sprint 1 |
| [EP-03](#ep-03--painel-dashboard) | Página Dashboard | P0 | Sprint 2 |
| [EP-08](#ep-08--configurações-neo-embutidas-no-dashboard) | Configurações Neo (embutidas no dashboard) | P0 | Sprint 3 |
| [EP-09](#ep-09--skills-neo-embutidas-no-dashboard) | Skills Neo (embutidas no dashboard) | P0 | Sprint 4 |
| [EP-04](#ep-04--página-projetos-command-center-local-first) | Página Projetos (Command Center local-first) | P0 | Sprint 5 |
| [EP-10](#ep-10--sincronização-jira-e-fontes-externas) | Sincronização Jira e fontes externas | P0 futuro | Sprint 6+ |
| [EP-05](#ep-05--ações-rápidas-e-integrações-locais) | Ações rápidas e integrações locais | P1 | Sprint 6 |
| [EP-06](#ep-06--página-finanças) | Página Finanças (shell visual) | P0 | Sprint 6 |
| [EP-07](#ep-07--qualidade-testes-e-evidências) | Qualidade, testes e evidências | P0 | Transversal |
| [EP-AG](#ep-ag--painel-agentes-futuro) | Painel Agentes (futuro) | P2 | Sprint 7+ |

---

## EP-01 — Rebrand visual e textual

**Objetivo:** A interface, ao abrir, comunica "Neo" — não "Hermes". Mantém
todas as capacidades técnicas; troca apenas chrome (logo, paleta, copy).

**Por que aditivo:** o backend já suporta `bot_name` configurável e o frontend
já lê `window._botName`. A maior parte é configuração + skin novo + assets
de marca.

### HUs

| HU | Descrição | Prioridade |
|---|---|---|
| HU-01.1 | Como Júnior, quero que a topbar, o título da aba e as notificações exibam "Neo" | P0 |
| HU-01.2 | Como Júnior, quero um logo "NEO" (orb azul-neon) no lugar do caduceu dourado | P0 |
| HU-01.3 | Como Júnior, quero um skin "neo" (paleta cyan/azul-neon do mockup) selecionável em Settings | P0 |
| HU-01.4 | Como Júnior, quero que o skin "neo" seja default quando `HERMES_WEBUI_DEFAULT_SKIN=neo` | P0 |
| HU-01.5 | Como Júnior, quero um favicon e PWA icons atualizados para o Neo | P0 |
| HU-01.6 | Como Júnior, quero o slash command `/skin neo` aplicar o skin ao vivo | P1 |

**Dependências:** nenhuma (pode começar imediatamente).
**Arquivos tocados:** `static/style.css` (bloco aditivo), `static/index.html`
(SVG do logo + meta tags), `static/favicon*`, `api/config.py` (env var), `static/i18n.js` (string do nome do skin).

---

## EP-02 — Localização pt-BR

**Objetivo:** A WebUI inteira em pt-BR para uso nativo do Júnior, com paridade
de chaves com o `en` (locale base).

### HUs

| HU | Descrição | Prioridade |
|---|---|---|
| HU-02.1 | Como Júnior, quero um locale `pt-BR` cobrindo 100% das chaves do `en` atual | P0 |
| HU-02.2 | Como Júnior, quero que o locale `pt-BR` seja default quando `HERMES_WEBUI_LOCALE=pt-BR` | P0 |
| HU-02.3 | Como mantenedor do fork, quero teste de paridade de chaves `pt-BR vs en` igual ao que existe para `es` | P0 |
| HU-02.4 | Como Júnior, quero traduções para chaves novas dos painéis Dashboard e Kanban | P0 |
| HU-02.5 | Como Júnior, quero traduções de mensagens de erro e toasts em pt-BR | P0 |

**Dependências:** começa em paralelo com EP-01; chaves do EP-03/EP-04 entram
quando aqueles painéis forem implementados.
**Arquivos tocados:** `static/i18n.js`, `tests/test_locale_parity_pt_br.py` (novo).

---

## EP-03 — Página Dashboard

**Objetivo:** Tela inicial executiva com layout de 2 colunas (chat central +
coluna direita com hero/KPIs/ações). Júnior abre a WebUI e vê de relance:
chat ativo com o Neo, estado dos projetos, atalhos rápidos. Conforme
[DESIGN-SPEC §6](./DESIGN-SPEC.md#6-página-dashboard).

### HUs

| HU | Descrição | Prioridade |
|---|---|---|
| HU-03.1 | Como Júnior, quero um novo painel "Dashboard" no rail/sidebar | P0 |
| HU-03.2 | Como Júnior, quero que o Dashboard seja a tela inicial quando `settings.default_panel="dashboard"` | P0 |
| HU-03.3 | Como Júnior, quero ver no Dashboard um hero com avatar humanoide holográfico + saudação contextual ("Bem-vindo de volta!") + pill "STATUS: OPERACIONAL" | P0 |
| HU-03.4 | Como Júnior, quero ver 4 KPI cards com deltas: Projetos Ativos (+N este mês), Tarefas em Andamento (+N desde ontem), Concluídas (+N esta semana), Agentes Online (todos operacionais) | P0 |
| HU-03.5 | Como Júnior, quero o chat central (mesmo SSE da sessão ativa) embutido no grid do Dashboard com header, lista de mensagens e o composer completo atual do Hermes WebUI (modelo, workspace, profile, anexos, voz, reasoning/effort e envio) | P0 |
| HU-03.6 | Como Júnior, quero a topbar contextual com VPS Status/Uptime/Região/Versão + Terminal SSH + busca/notif/help + admin dropdown (dados via `GET /api/health/system`, poll 30s) | P0 |
| HU-03.7 | Como Júnior, quero ações rápidas em grid 2×3 (6 botões): Novo Projeto, Novo Documento, Novo Componente, Abrir Terminal, Gerar Relatório, Deploy Projeto | P0 |
| HU-03.8 | Como Júnior, quero o card de status do Neo na sidebar com botão "Conversar agora" que foca o composer | P0 |
| HU-03.9 | Como Júnior, quero o admin dropdown na topbar (Perfil / Configurações / Logout) | P1 |
| HU-03.10 | Como Júnior, quero um painel mínimo "Pessoal" na sidebar (perfil + preferências) | P1 |
| HU-03.11 | Como Júnior, quero ver recursos VPS na sidebar (CPU/RAM/Disco/Rede com barras de progresso, poll 30s via `GET /api/health/vps`) | P0 |

**Dependências:** EP-01 (visual), EP-02 (strings).
**Arquivos tocados:**
- Novo: `static/dashboard.js`, painel HTML inline em `index.html`
- Novo: `api/health.py` (rotas `GET /api/health/system` e `GET /api/health/vps`)
- Aditivo: `api/dashboard.py` (rota `GET /api/dashboard/summary` agregando
  contadores existentes; reusa `models.py`, cron, etc.)
- Aditivo: `static/i18n.js` (chaves `dashboard_*`, `sidebar_*`, `topbar_*`)
- Aditivo: `static/style.css` (blocos `.dashboard-*`, `.sidebar-*`, `.topbar-*`)

---

## EP-08 — Configurações Neo (embutidas no dashboard)

**Objetivo:** Ao clicar em "Configurações" (sidebar ou admin dropdown), o
dashboard shell permanece ativo e exibe a UI de settings embutida: nav lateral
com seções (Conversa / Aparência / Preferências / Provedores / Sistema) +
conteúdo à direita. Toda a semântica de rotas, handlers, localStorage e guards
de unsaved changes do upstream são preservados; muda apenas a moldura visual —
que passa a usar o padrão Neo estabelecido na Sprint 2.

**Por que aditivo:** o DOM do `#panelSettings` + `#mainSettings` já existe com
toda a lógica implementada. O padrão de `mountDashboardChat()` / `restoreDashboardChat()`
(Sprint 2) prova que é possível reposicionar DOM existente dentro do shell sem
duplicar handlers. Esta sprint aplica o mesmo padrão a settings.

### HUs

| HU | Descrição | Prioridade |
|---|---|---|
| HU-08.1 | Como Júnior, quero que clicar em "Configurações" mantenha o dashboard shell ativo e abra settings embutido via `mountDashboardSettings()` — mesma abordagem de `mountDashboardChat()` | P0 |
| HU-08.2 | Como Júnior, quero a seção "Conversa" com exportar transcript/JSON, importar e clear — handlers upstream preservados, Neo-skinned | P0 |
| HU-08.3 | Como Júnior, quero a seção "Aparência" com seletor de tema (dark/light/system), skin picker (Neo selecionável e ativo por padrão), tamanho de fonte — live preview e autosave preservados | P0 |
| HU-08.4 | Como Júnior, quero as seções "Preferências", "Provedores" e "Sistema" — panes upstream reaproveitados, Neo-skinned | P0 |
| HU-08.5 | Como Júnior, quero que o dirty guard e o autosave de aparência (`_settingsDirty`, `_beginSettingsPanelSession`) continuem funcionando dentro do settings embutido | P0 |
| HU-08.6 | Como mantenedor, quero testes automáticos para mount/restore do settings embutido, CSS Neo e preservação dos handlers | P0 |

**Dependências:** EP-03 (dashboard shell e padrão mount/restore).
**Arquivos tocados:**
- Aditivo: `static/dashboard.js` (`mountDashboardSettings`, `restoreDashboardSettings`, interceptação do admin menu e sidebar)
- Aditivo: `static/style.css` (overrides Neo para `.dashboard-shell-mode .settings-*`, nav lateral settings dentro do shell)
- Mínimo: `static/index.html` (ajuste de slot ou wrapper se necessário)
- Novo: `tests/test_neo_dashboard_settings.py`

**Risco:** `_beginSettingsPanelSession()` em `panels.js` precisa ser chamado
pelo `mountDashboardSettings()` para ativar o dirty guard — requer atenção
na integração com o fluxo de `switchPanel()`.

---

## EP-09 — Skills Neo (embutidas no dashboard)

**Objetivo:** Ao clicar em "Skills" na sidebar Neo, o dashboard shell permanece
ativo e exibe o painel de skills embutido — lista à esquerda (master) +
detalhe à direita — com visual Neo, preservando 100% da lógica upstream:
`GET /api/skills`, renderização por categoria, busca, criação e edição.

**Por que aditivo:** `#panelSkills` (lista + busca) e `#mainSkills` (detalhe)
já existem com toda a lógica implementada. O padrão de `mountDashboardSettings()`
(Sprint 3) prova que mover o DOM da sidebar para dentro da main é seguro e
mantém todos os handlers funcionando por `getElementById`. Skills é ainda
mais simples: sem dirty guard, sem autosave, sem session state.

### HUs

| HU | Descrição | Prioridade |
|---|---|---|
| HU-09.1 | Como Júnior, quero que clicar em "Skills" mantenha o dashboard shell ativo e abra o painel embutido via `mountDashboardSkills()` — `#panelSkills` inteiro move para `#mainSkills` como coluna esquerda (260px) | P0 |
| HU-09.2 | Como Júnior, quero o layout two-column no shell: lista de skills à esquerda com busca e botão "+ Nova", detalhe à direita — todos os handlers upstream preservados (`loadSkills`, `renderSkills`, `filterSkills`, `openSkillCreate`, edição e deleção) | P0 |
| HU-09.3 | Como mantenedor, quero testes automáticos para mount/restore do painel de skills embutido, CSS Neo e preservação dos elementos DOM | P0 |

**Dependências:** EP-03 (dashboard shell), EP-08 (padrão mount/restore).
**Arquivos tocados:**
- Aditivo: `static/panels.js` (`'skills'` em `NEO_SHELL_PANELS` + chamadas mount/restore)
- Aditivo: `static/dashboard.js` (`mountDashboardSkills`, `restoreDashboardSkills`)
- Aditivo: `static/style.css` (two-column layout no shell para `#mainSkills`)
- Novo: `tests/test_neo_dashboard_skills.py`

---

## EP-04 — Página Projetos (Command Center local-first)

**Objetivo:** Página dedicada full-page para acompanhamento diário dos projetos
operados pelo Neo. Não é um clone de Jira: é uma central local-first que agrega
projetos, tarefas, sessões Neo e referências externas. A Sprint 5 entrega
Kanban e Lista com persistência local e campos `external_ref`; sincronização real
com Jira/GitHub/Obsidian fica no épico futuro [EP-10](#ep-10--sincronização-jira-e-fontes-externas).

### HUs

| HU | Descrição | Prioridade |
|---|---|---|
| HU-04.1 | Como Júnior, quero uma página "Projetos" na sidebar com header (título 24px + subtítulo + botões Filtros/Kanban/Lista/+ Novo Projeto) | P0 |
| HU-04.2 | Como Júnior, quero **4 colunas** Kanban: Backlog, Em Andamento, Em Revisão, Concluído — com top-border colorido (slate/amber/blue/green) e contagem | P0 |
| HU-04.3 | Como Júnior, quero criar projeto via modal (nome, descrição, domínio, cor, fonte externa padrão opcional) | P0 |
| HU-04.4 | Como Júnior, quero criar tarefas vinculadas a projetos, com categoria, prioridade, responsável, prazo e `external_ref` opcional | P0 |
| HU-04.5 | Como Júnior, quero arrastar cards entre colunas (drag: glow cyan + rotate 2deg; drop: persiste via `PATCH /api/project-tasks/{id}`) | P0 |
| HU-04.6 | Como Júnior, quero barra de status (pills) com contadores clicáveis: Total, Backlog, Em Andamento, Revisão, Concluído | P0 |
| HU-04.7 | Como Júnior, quero uma vista Lista agrupada por status com colunas ID, tarefa, prioridade, responsável e estado | P0 |
| HU-04.8 | Como Júnior, quero filtros por texto, projeto, status, prioridade, fonte externa, responsável e data | P0 |
| HU-04.9 | Como Júnior, quero vincular sessões Neo e refs GitHub/Obsidian a uma tarefa | P1 |
| HU-04.10 | Como Júnior, no mobile quero Kanban empilhado em 1 coluna com tabs no topo e fallback de mover por menu | P1 |

**Dependências:** EP-01, EP-02, EP-08. Reaproveita modelo `Session.project` existente.
**Arquivos tocados:**
- Novo: `static/kanban.js`, `api/projects.py` (novo módulo, CRUD local-first de projetos/tarefas)
- Persistência: `~/.hermes/webui/projects.json`
- Aditivo: rotas em `api/routes.py` (`GET/POST/PATCH /api/projects`, `GET/POST/PATCH /api/project-tasks`, etc.)
- Aditivo: `static/i18n.js` (chaves `projects_*`)
- Aditivo: `static/style.css` (`.kanban-*`)

**Categorias de card (lista fechada no MVP):** Design, Frontend, Backend,
Database, Infra, DevOps, Docs, QA, Segurança. Cores conforme DESIGN-SPEC §2.

**Prioridades:** Baixa (slate), Média (amber), Alta (vermelho). Cores conforme
DESIGN-SPEC §2.

**Fontes externas no MVP:** Sprint 5 só persiste metadados (`external_ref`) e
links. Não chama APIs de Jira/GitHub/Obsidian.

---

## EP-10 — Sincronização Jira e fontes externas

**Objetivo:** Conectar a central de projetos do Neo aos sistemas de origem que
Júnior já usa: múltiplos Jiras, GitHub, Obsidian e sessões Neo. Este épico é
próximo ao MVP, mas fora da Sprint 5.

### HUs

| HU | Descrição | Prioridade |
|---|---|---|
| HU-10.1 | Como Júnior, quero cadastrar múltiplas fontes Jira com nome, base URL, projeto/chave padrão e credencial referenciada fora do repo | P0 futuro |
| HU-10.2 | Como Júnior, quero que o Neo crie uma issue Jira a partir do chat e vincule a tarefa local via `external_ref` | P0 futuro |
| HU-10.3 | Como Júnior, quero importar issues existentes por projeto/filtro para a central Projetos | P0 futuro |
| HU-10.4 | Como Júnior, quero sincronizar status remoto do Jira com status local mapeado | P0 futuro |
| HU-10.5 | Como Júnior, quero reconciliar conflitos entre status local e remoto sem sobrescrever trabalho silenciosamente | P1 futuro |
| HU-10.6 | Como Júnior, quero anexar refs GitHub, Obsidian e sessões Neo automaticamente quando o Neo operar uma tarefa | P1 futuro |

**Dependências:** Sprint 5 concluída; política de credenciais definida para os
três Jiras; decisão de mapeamento de status por fonte.

---

## EP-05 — Ações rápidas e integrações locais

**Objetivo:** Reduzir cliques para tarefas que o Júnior faz com mais
frequência. Ações pontuais que não justificam um painel próprio.

### HUs

| HU | Descrição | Prioridade |
|---|---|---|
| HU-05.1 | Como Júnior, quero um atalho "Salvar memória" que pré-preenche um modal apontando para a skill `salvar-obsidian` | P1 |
| HU-05.2 | Como Júnior, quero um atalho "Novo terminal" que abre um terminal embutido (já existe em `terminal.js` — só botão) | P1 |
| HU-05.3 | Como Júnior, quero um atalho "Executar skill" com seletor das skills do Neo (`~/.hermes/skills/neo/`) | P1 |
| HU-05.4 | Como Júnior, quero um indicador visual quando um job cron acabou de rodar (já existe em parte; consolidar no Dashboard) | P1 |

**Dependências:** EP-03 (Dashboard hospeda atalhos).

---

## EP-06 — Página Finanças

**Objetivo:** Página dedicada de controle financeiro pessoal e empresarial com
KPI cards, gráfico de linha temporal (SVG vanilla), donut de gastos por
categoria, coluna lateral com orçamentos/transações recentes/metas, e modal
de criação. Conforme [DESIGN-SPEC §8](./DESIGN-SPEC.md#8-página-finanças-controle-financeiro).

> **Escopo MVP:** entregar **shell visual** completo (layout, cards, dataset de
> demonstração). Backend financeiro real (sincronização com FinanPy / OFX /
> planilhas) é **pós-MVP**.

### HUs

| HU | Descrição | Prioridade |
|---|---|---|
| HU-06.1 | Como Júnior, quero uma página "Finanças" na sidebar com header (título + subtítulo + botões Terminal SSH / + Nova Finança) | P0 |
| HU-06.2 | Como Júnior, quero 4 KPI cards em linha: Receitas (verde), Despesas (vermelho), Saldo Líquido (amber), Investimentos (violeta) — com ícone circular 40x40 e valor monetário pt-BR | P0 |
| HU-06.3 | Como Júnior, quero gráfico de linha SVG vanilla (receitas cyan + despesas vermelho) com toggle de séries, dropdown de período e tooltip no hover | P0 |
| HU-06.4 | Como Júnior, quero card "Gastos por Categoria" com donut chart SVG + legenda | P0 |
| HU-06.5 | Como Júnior, quero coluna lateral (320px) com cards: Orçamentos (barras de progresso), Transações Recentes (5 itens com ícone/nome/data/valor), Metas Financeiras (barras + %) | P0 |
| HU-06.6 | Como Júnior, quero modal "+ Nova Finança" com tabs Receita/Despesa/Investimento e campos: descrição, valor (máscara R$), data, categoria, método, recorrência, anotações | P0 |
| HU-06.7 | Como Júnior, quero persistência em `~/.hermes/webui/finance.json` com endpoints `GET/POST /api/finance/transactions`, `GET /api/finance/summary` | P0 |
| HU-06.8 | Como Júnior, quero estados vazios em todos os cards (ilustração + CTA "Adicione sua primeira transação") | P0 |
| HU-06.9 | Como Júnior, quero animação de entrada nos gráficos: `stroke-dashoffset` 800ms para linha, rotação 600ms para donut | P1 |

**Dependências:** EP-01 (visual), EP-02 (strings), EP-03 (sidebar/topbar).
**Arquivos tocados:**
- Novo: `static/finance.js` (módulo com `renderLineChart`, `renderDonutChart` + lógica da página)
- Novo: `api/finance.py` (rotas `GET/POST /api/finance/*`)
- Persistência: `~/.hermes/webui/finance.json`
- Aditivo: `static/i18n.js` (chaves `finance_*`)
- Aditivo: `static/style.css` (`.finance-*`)

**Categorias de transação (lista fechada no MVP):** Alimentação, Transporte,
Educação, Saúde, Lazer, Moradia, Salário, Investimento, Outros.

**Métodos de pagamento:** Pix, Cartão, Dinheiro, Transferência, Boleto.

---

## EP-07 — Qualidade, testes e evidências

**Objetivo:** Estabelecer disciplina de testes e evidências para que cada HU
feche com confiança e o fork não regrida.

### HUs

| HU | Descrição | Prioridade |
|---|---|---|
| HU-07.1 | Como mantenedor, quero um arquivo de testes `tests/test_neo_sprint{N}.py` por sprint, seguindo o padrão upstream | P0 |
| HU-07.2 | Como mantenedor, quero um lint custom `scripts/lint_neo_branding.sh` que falha se houver "Hermes" no chrome visível ao usuário (excluindo termos técnicos como `HERMES_HOME`) | P0 |
| HU-07.3 | Como mantenedor, quero um diretório `docs/neo/evidencias/<HU-ID>/` com screenshots de homologação por HU | P0 |
| HU-07.4 | Como mantenedor, quero um teste de paridade de chaves do `pt-BR` vs `en` no estilo do que já existe para `es` | P0 |
| HU-07.5 | Como mantenedor, quero CI rodando `pytest tests/` em PRs (já existe upstream — só validar que continua verde) | P0 |

**Dependências:** transversal a todos os épicos.

---

## EP-AG — Painel Agentes (FUTURO)

> ⚠️ **P2 — depende de PoC.** Avaliar custo na VPS antes de comprometer com
> entrega. Inspirado em [`pablodelucca/pixel-agents`](https://github.com/pablodelucca/pixel-agents):
> visualização animada e leve dos subagentes em execução.

**Objetivo:** Visualizar a delegação multi-agente do Neo (Neo orquestrador →
subagentes MGI / Projetos / Finanças / Terapia / Pessoal) em tempo real,
com domínio, profile, modelo, status, ETA.

### HUs (rascunho)

| HU | Descrição | Prioridade |
|---|---|---|
| HU-AG.1 | Como Júnior, quero ver subagentes ativos no momento, com domínio, modelo, profile e tempo decorrido | P2 |
| HU-AG.2 | Como Júnior, quero histórico recente de subagentes (últimos N concluídos) com sumário e duração | P2 |
| HU-AG.3 | Como Júnior, quero clicar num subagente e ver as ferramentas que ele chamou (timeline) | P2 |
| HU-AG.4 | Como Júnior, quero animação leve estilo "pixel-agents" para tornar a delegação tangível | P3 |
| HU-AG.5 | Como mantenedor, quero que o painel custe < 50 MB de RAM e ≤ 1 req/s | P2 |

**Pré-requisitos para sair do backlog:**

1. **Identificar fonte de dados.** O Hermes runtime já loga subagentes em
   `~/.hermes/logs/agent.log` e tool calls em `~/.hermes/sessions/`. Avaliar
   se já existe rota/endpoint que enumere subagentes ativos. Se não, propor
   um endpoint mínimo no upstream `hermes-agent` (PR colaborativo).
2. **PoC de SSE leve** reutilizando o canal SSE existente em `streaming.py`
   (sem abrir novo websocket).
3. **Métrica de custo** rodando 24h com painel ativo → comparar consumo de
   RAM/CPU antes/depois.

**Risco:** o `pixel-agents` é uma referência visual; reproduzir 1:1 com
animações pesadas pode comprometer a performance da VPS. Solução proposta:
versão "estática" (cards quietos com badge animado) como default; modo
"pixel" como opt-in.

---

## Itens não-priorizados (P3 — ideias)

- **Integração financeira real:** conectar página Finanças ao FinanPy API
  (`http://127.0.0.1:8001/api/v1/`) para dados reais em vez de `finance.json`.
- **Multi-moeda:** suportar BRL + USD na página Finanças.
- **Separação pessoal × empresarial:** toggle/tab no header da página Finanças.
- Atalhos de teclado globais customizáveis (`Cmd+1..9` para switchPanel).
- Dashboard de uso por modelo (Z.AI vs OpenAI vs Groq vs Databricks) — quanto
  cada um foi usado hoje/semana/mês.
- Atalho de export do dia ("brief executivo" gerado pelo Neo no fim do dia).
- Integração visual com Obsidian Vault (pré-visualizar a nota antes de salvar).
- Modo "kiosk" para tablet na mesa (sem rail, só Dashboard + Chat).
