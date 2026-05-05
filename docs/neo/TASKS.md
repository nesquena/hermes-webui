# TASKS — Neo WebUI (Documento Vivo)

> Plano operacional alinhado ao [PRD.md](./PRD.md) v2.1, ao
> [BACKLOG.md](./BACKLOG.md) e ao [DESIGN-SPEC.md](./DESIGN-SPEC.md) v3.2.
> Este arquivo acompanha execução, evidências e Definition of Done; mudanças
> contratuais devem ser feitas primeiro no PRD/Backlog/Design Spec.

**Atualizado em:** 2026-05-05
**Versão alvo MVP:** `neo-webui-v0.1` ao final da Sprint 6
**Branch de desenvolvimento atual:** `develop`
**Branch de produção:** `main`

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
| Sprint 1 | Rebrand visual/textual + locale pt-BR | 11 | 10 + 1 parcial | 0 | aguardando evidências/homologação |
| Sprint 2 | Dashboard + sidebar/topbar Neo | 11 | 11 | 11 | concluída |
| Sprint 3 | Configurações Neo (embutidas no dashboard) | 6 | 6 | 6 | concluída |
| Sprint 4 | Skills Neo (embutidas no dashboard) | 3 | 3 | 3 | concluída |
| Sprint 5 | Projetos Command Center | 10 | 8 + 2 parciais | 8 | implementada; P1 mobile/refs parcial |
| Sprint 6 | Ações rápidas + Finanças shell visual | 13 | 0 | 0 | aguardando Sprint 5 |
| Transversal | Qualidade, testes e evidências | 5 | 2 parciais | 0 | em andamento contínuo |
| Sprint 7+ | Painel Agentes futuro | 5 | 0 | 0 | depende de PoC |

### Estado atual do worktree

Estado registrado em 2026-05-05:

- Branch local ativa: `develop`, rastreando `origin/develop`.
- Sprint 1 foi incorporada em `develop` por fast-forward a partir de
  `neo/sprint-1`.
- `main` permanece como branch de produção e ainda não recebeu a Sprint 1.
- `docs/neo/` e testes Neo estão versionados; `.gitignore` não ignora mais a
  documentação Neo.
- Worktree estava limpo antes desta atualização documental.
- Validação recente em `develop`:
  - `node --check static/dashboard.js`
  - `.venv/bin/python -m py_compile api/dashboard.py api/health.py api/routes.py api/config.py`
  - `node --check static/i18n.js`
  - `.venv/bin/pytest tests/test_neo_font_ui_inter.py tests/test_neo_dashboard_kpis.py tests/test_neo_skin.py tests/test_neo_branding_assets.py tests/test_neo_pt_br_toasts.py tests/test_neo_dashboard_sprint2.py tests/test_neo_skin_localstorage_persistence.py tests/test_neo_hero_greeting.py tests/test_neo_dashboard_chat_embed.py tests/test_neo_dashboard_shell_visual.py tests/test_neo_dashboard_quick_actions.py tests/test_locale_parity_pt_br.py tests/test_neo_dashboard_admin_personal.py tests/test_neo_health_runtime.py -q`
    (`67 passed in 1.99s`)
- HU-01.2 e HU-01.5 foram implementadas após o merge da Sprint 1 com assets de
  marca, favicon/PWA e teste `tests/test_neo_branding_assets.py`.
- Sprint 2 avançou além do corte inicial: HU-03.1 a HU-03.11 têm
  implementação/testes focados; HU-03.6 e HU-03.11 foram fechadas tecnicamente
  com topbar operacional e métricas VPS em runtime.
- Homologação visual manual de HU-03.1, HU-03.2, HU-03.3, HU-03.4, HU-03.5,
  HU-03.6, HU-03.7, HU-03.8 e HU-03.11 foi informada em 2026-05-02; anexos de
  screenshot permanecem como evidência complementar quando exigidos pelo release.
- Bloqueios técnicos de Sprint 2 removidos em 2026-05-02: busca,
  notificações/help e métricas VPS têm implementação e testes.
- HU-03.1, HU-03.2, HU-03.3, HU-03.4, HU-03.5, HU-03.6, HU-03.7, HU-03.8,
  HU-03.9, HU-03.10 e HU-03.11 foram homologadas e fechadas por DoD em
  2026-05-02; Sprint 2 encerrada com 11/11 HUs concluídas. Screenshots ficam
  como evidência complementar pendente de release.
- Sprint 4 foi reconciliada documentalmente em 2026-05-05: `skills` já está
  em `NEO_SHELL_PANELS`, `mountDashboardSkills()`/`restoreDashboardSkills()`
  existem em `static/dashboard.js`, o painel `#panelSkills` é movido para
  `#mainSkills` no dashboard shell, e `tests/test_neo_dashboard_skills.py`
  cobre o contrato estático.
- Validação de fechamento da Sprint 4 em 2026-05-05:
  - `node --check static/dashboard.js`
  - `node --check static/panels.js`
  - `node --check static/i18n.js`
  - `.venv/bin/pytest tests/test_neo_dashboard_skills.py -q`
  - `.venv/bin/pytest tests/test_neo_font_ui_inter.py tests/test_neo_dashboard_kpis.py tests/test_neo_skin.py tests/test_neo_branding_assets.py tests/test_neo_pt_br_toasts.py tests/test_neo_dashboard_sprint2.py tests/test_neo_skin_localstorage_persistence.py tests/test_neo_hero_greeting.py tests/test_neo_dashboard_chat_embed.py tests/test_neo_dashboard_shell_visual.py tests/test_neo_dashboard_quick_actions.py tests/test_locale_parity_pt_br.py tests/test_neo_dashboard_admin_personal.py tests/test_neo_health_runtime.py tests/test_neo_dashboard_settings.py tests/test_neo_dashboard_skills.py -q`
    (`109 passed in 2.47s`)
- Coleta completa em 2026-05-05: `.venv/bin/pytest tests/ --collect-only -q`
  encontrou `3601 tests collected`; ambiente local sem `hermes-agent`, então
  25 testes dependentes do agente seriam pulados numa execução completa.

---

## Inconsistências e decisões registradas

| Item | Inconsistência | Decisão operacional neste TASKS |
|---|---|---|
| I-01 | O `TASKS.md` anterior dizia Sprint 4 = "Ações rápidas + Polimento" e Sprint 5+ = Agentes, mas o PRD/Backlog colocam **Finanças** como EP-06 P0 na Sprint 4. | Sprint 4 passa a conter EP-05 + EP-06. Agentes fica Sprint 5+ / P2. |
| I-02 | O `TASKS.md` anterior especificava Kanban com **3 colunas**; PRD, Backlog e Design Spec exigem **4 colunas**: Backlog / Em Andamento / Em Revisão / Concluído. | Todas as tasks de Projetos foram atualizadas para 4 colunas e status `backlog`. |
| I-03 | O handoff marca HU-01.1, HU-01.3, HU-01.4 e HU-01.6 como validadas visualmente, mas PRD §10 exige testes, evidências e homologação para concluir. | Essas HUs ficam como **implementadas sem DoD** até evidências/testes serem anexados. |
| I-04 | O Backlog original mapeava drag-and-drop como HU-04.4, mas a revisão aprovada em 2026-05-05 adicionou criação de tarefas com `external_ref` antes do drag. | Este TASKS segue o desenho Sprint 5 Command Center: HU-04.4 = criar tarefas com refs; HU-04.5 = drag-and-drop persistido. |
| I-05 | PRD RF-08 exigia `POST /api/projects/{id}` para status, enquanto o novo modelo separa projeto de tarefa. | Sprint 5 passa a persistir status de tarefa via `PATCH /api/project-tasks/{task_id}`; rotas antigas `/api/projects/create`, `/rename`, `/delete` permanecem como compatibilidade upstream/adapters. |
| I-06 | PRD RF-15 pede persistência financeira em `finance.json`, enquanto o texto de não-objetivos fala em "backend financeiro real pós-MVP". | Interpretação: `finance.json` + endpoints locais são P0 do MVP; integrações bancárias/FinanPy/OFX são pós-MVP. |
| I-07 | `DESIGN-SPEC.md §13` listava "Backlog no Kanban" como pendência para atualizar RF-06, mas o PRD já está atualizado com 4 colunas. | Pendência removida do Design Spec em 2026-05-01; PRD/Backlog/TASKS permanecem em 4 colunas. |
| I-08 | A análise inicial indicava risco de `docs/neo/` ignorado no `.gitignore`. | Resolvido na Sprint 1: `docs/neo/` está versionado e `.gitignore` não ignora a documentação Neo. |
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

- [x] Confirmar branch de trabalho (`develop`).
- [x] Decidir se `docs/neo/` será versionado apesar do `.gitignore` atual.
- [x] Rodar baseline de testes relevante antes do commit/merge da Sprint 1.

### HU-01.1 — Topbar, título e notificações exibem "Neo"

**Status:** implementada sem DoD
**Prioridade:** P0
**Épico:** EP-01

**Tasks**

- [x] Confirmar suporte upstream a `bot_name` / `HERMES_WEBUI_BOT_NAME`.
- [x] Registrar no handoff que `.env` local usa `HERMES_WEBUI_BOT_NAME=Neo`.
- [x] Verificar topbar, `<title>` e placeholder inicial do composer por contrato estático.
- [x] Atualizar `static/manifest.json` para `name` e `short_name` Neo.
- [x] Registrar evidência técnica em `docs/neo/evidencias/HU-01.1/`.
- [x] Rodar teste/lint relevante.
- [ ] Anexar screenshots/homologação manual em runtime.

### HU-01.2 — Logo "NEO" e avatar/mark humanoide

**Status:** implementada sem DoD
**Prioridade:** P0
**Épico:** EP-01
**Dependências:** DESIGN-SPEC §9

**Tasks**

- [x] Criar `static/brand/neo-avatar.svg` conforme wireframe humanoide do Design Spec.
- [x] Criar `static/brand/neo-avatar-mono.svg`.
- [x] Criar `static/brand/neo-mark.svg` para sidebar/topbar.
- [x] Trocar caduceu inicial por asset Neo em `static/index.html`.
- [x] Garantir acessibilidade e `<title>` nos SVGs.
- [x] Criar teste automatizado de presença/acessibilidade dos assets.
- [x] Registrar evidência técnica em `docs/neo/evidencias/HU-01.2/`.
- [ ] Testar legibilidade em dark/light.
- [ ] Anexar screenshots.

### HU-01.3 — Skin "neo" selecionável

**Status:** implementada com testes
**Prioridade:** P0
**Épico:** EP-01

**Tasks**

- [x] Adicionar `:root[data-skin="neo"]` em `static/style.css`.
- [x] Adicionar `:root.dark[data-skin="neo"]` em `static/style.css`.
- [x] Adicionar `neo` ao allowlist inicial em `static/index.html`.
- [x] Adicionar `Neo` ao array `_SKINS` em `static/boot.js`.
- [x] Adicionar `neo` a `_SETTINGS_SKIN_VALUES` em `api/config.py`.
- [x] Adicionar/confirmar opção no seletor de Settings se `_SKINS` não for suficiente.
- [x] Ajustar `--font-ui` para Inter conforme PRD RNF-10 e Design Spec §3.
- [x] Rodar testes de skin ou criar `tests/test_neo_sprint1.py`.
- [x] Criar `tests/test_neo_font_ui_inter.py` para validar fonte Inter.
- [ ] Anexar evidência antes/depois.

**Evidência técnica:** [`docs/neo/evidencias/HU-01.3/README.md`](./evidencias/HU-01.3/README.md)

### HU-01.4 — Skin "neo" default via env

**Status:** implementada com testes
**Prioridade:** P0
**Épico:** EP-01
**Dependências:** HU-01.3

**Tasks**

- [x] Ler `HERMES_WEBUI_DEFAULT_SKIN` em `api/config.py`.
- [x] Injetar `__NEO_DEFAULT_SKIN__` em `api/routes.py`.
- [x] Aplicar default no early boot de `static/index.html` quando `localStorage.hermes-skin` estiver vazio.
- [x] Criar teste automatizado para placeholder injetado / allowlist.
- [ ] Testar `localStorage.clear()` + reload com e sem env.
- [ ] Anexar evidências.

### HU-01.5 — Favicon e PWA icons Neo

**Status:** implementada sem DoD
**Prioridade:** P0
**Épico:** EP-01
**Dependências:** HU-01.2

**Tasks**

- [x] Remover `static/favicon.svg` do favicon ativo.
- [x] Criar `static/favicon-16.png`.
- [x] Substituir `static/favicon-32.png`.
- [x] Criar `static/favicon-192.png`.
- [x] Criar `static/favicon-512.png`.
- [x] Substituir `static/favicon.ico`.
- [x] Criar/atualizar `static/apple-touch-icon.png`.
- [x] Atualizar `static/manifest.json`.
- [x] Criar teste automatizado para manifest e assinaturas dos ícones.
- [x] Registrar evidência técnica em `docs/neo/evidencias/HU-01.5/`.
- [ ] Validar aba do navegador e PWA instalada.

### HU-01.6 — `/skin neo` aplica skin ao vivo

**Status:** implementada com testes
**Prioridade:** P1 no Backlog, tratado como P0 operacional por RF-02
**Épico:** EP-01
**Dependências:** HU-01.3

**Tasks**

- [x] Confirmar que `Neo` em `_SKINS` torna o skin disponível para o fluxo comum.
- [x] Verificar se `static/commands.js` usa `_SKINS` dinamicamente ou lista própria.
- [x] Se houver lista própria, adicionar `neo` explicitamente.
- [x] Criar testes automatizados para persistência localStorage/settings.
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

**Status:** implementada com testes
**Prioridade:** P0
**Épico:** EP-02 / EP-07

**Tasks**

- [x] Criar `tests/test_locale_parity_pt_br.py`.
- [x] Falhar se qualquer chave de `en` não existir em `pt-BR`.
- [x] Falhar se houver chave extra órfã sem justificativa.
- [x] Criar testes adicionais: font UI Inter, localStorage persistence.
- [x] Criar checklist de PR em `.claude/PR-CHECKLIST-neo.md`.
- [ ] Rodar checklist de PR e anexar screenshots.

**Evidência técnica:** [`docs/neo/evidencias/HU-02.3/README.md`](./evidencias/HU-02.3/README.md)

### HU-02.4 — Traduções novas de Dashboard, Kanban e Finanças

**Status:** implementada com testes
**Prioridade:** P0
**Épico:** EP-02
**Dependências:** Sprints 2-4

**Tasks**

- [x] Adicionar chaves `dashboard_*`.
- [x] Adicionar chaves `sidebar_*` e `topbar_*`.
- [x] Adicionar chaves `projects_*`.
- [x] Adicionar chaves `finance_*`.
- [x] Garantir que strings novas usem `t(...)` ou `data-i18n`.

### HU-02.5 — Erros e toasts em pt-BR

**Status:** em andamento
**Prioridade:** P0
**Épico:** EP-02

**Tasks**

- [x] Inventariar toasts e erros visíveis.
- [x] Cobrir mensagens novas Neo em chat, login e terminal.
- [x] Criar teste automatizado para erros/toasts pt-BR.
- [x] Registrar evidência técnica em `docs/neo/evidencias/HU-02.5/`.
- [ ] Validar settings, projetos e finanças quando os painéis MVP estiverem disponíveis.

**Evidência técnica:** [`docs/neo/evidencias/HU-02.5/README.md`](./evidencias/HU-02.5/README.md)

### Encerramento Sprint 1

- [ ] HU-01.1 a HU-02.5 concluídas por DoD.
- [ ] `pytest tests/test_neo_branding_assets.py tests/test_neo_skin.py tests/test_locale_parity_pt_br.py -v` passa.
- [ ] Suíte relevante upstream passa.
- [ ] Evidências anexadas.
- [ ] Commit limpo sem `.env`.

---

## Sprint 2 — Dashboard + sidebar/topbar Neo

**Meta:** Dashboard executivo como tela inicial, com chat central, hero, KPIs,
ações rápidas, sidebar fixa de 240px e topbar contextual de 56px.

### HU-03.1 — Painel "Dashboard" na sidebar

**Status:** concluída

**Tasks**

- [x] Adicionar item Dashboard na sidebar de 9 itens.
- [x] Criar painel `dashboard` sem quebrar painel `chat` upstream.
- [x] Criar `static/dashboard.js`.
- [x] Carregar `loadDashboard()` por feature detection.
- [x] Registrar evidência técnica em `docs/neo/evidencias/HU-03.1/`.
- [x] Homologação manual registrada em 2026-05-02.

**Evidência técnica:** [`docs/neo/evidencias/HU-03.1/README.md`](./evidencias/HU-03.1/README.md)

### HU-03.2 — Dashboard como painel inicial

**Status:** concluída

**Tasks**

- [x] Suportar `?panel=dashboard`.
- [x] Adicionar `settings.default_panel`.
- [x] Ler `HERMES_WEBUI_DEFAULT_PANEL` como default inicial quando não houver escolha local.
- [x] Preservar default upstream (`chat`) sem env/setting.
- [x] Registrar evidência técnica em `docs/neo/evidencias/HU-03.2/`.
- [x] Homologação manual registrada em 2026-05-02.

**Evidência técnica:** [`docs/neo/evidencias/HU-03.2/README.md`](./evidencias/HU-03.2/README.md)

### HU-03.3 — Hero avatar humanoide + saudação

**Status:** concluída

**Tasks**

- [x] Renderizar `neo-avatar.svg` na coluna direita.
- [x] Implementar saudação contextual pt-BR.
- [x] Exibir pill `STATUS: OPERACIONAL`.
- [x] Implementar animações `hover-float` e `pulse-glow`.
- [x] Criar teste automatizado para hero, i18n e CSS.
- [x] Registrar evidência técnica em `docs/neo/evidencias/HU-03.3/`.
- [x] Homologação manual registrada em 2026-05-02.

**Evidência técnica:** [`docs/neo/evidencias/HU-03.3/README.md`](./evidencias/HU-03.3/README.md)

### HU-03.4 — 4 KPI cards com deltas

**Status:** concluída

**Tasks**

- [x] Criar `api/dashboard.py`.
- [x] Criar `GET /api/dashboard/summary`.
- [x] Agregar Projetos Ativos, Tarefas em Andamento, Concluídas, Agentes Online.
- [x] Renderizar grid 2x2 responsivo.
- [x] Clicar em card navega para painel correspondente.
- [x] Criar teste automatizado para resumo, rota, HTML, JS, CSS e i18n.
- [x] Registrar evidência técnica em `docs/neo/evidencias/HU-03.4/`.
- [x] Homologação manual registrada em 2026-05-02.

**Evidência técnica:** [`docs/neo/evidencias/HU-03.4/README.md`](./evidencias/HU-03.4/README.md)

### HU-03.5 — Chat central no Dashboard

**Status:** concluída

**Tasks**

- [x] Embutir o mesmo SSE da sessão ativa.
- [x] Reutilizar a lista de mensagens upstream, preservando renderização de markdown, arquivos, tool calls e estados de streaming.
- [x] Reutilizar o composer/toolstrip completo upstream; não criar um segundo composer paralelo em `dashboard.js`.
- [x] Preservar anexos e fluxo de upload/preview.
- [x] Preservar microfone/voz quando disponível no ambiente.
- [x] Preservar seletor de profile ativo.
- [x] Preservar seletor de workspace ativo.
- [x] Preservar seletor de modelo configurado.
- [x] Preservar seletor de reasoning/effort.
- [x] Preservar menus auxiliares e demais controles já existentes no rodapé do chat.
- [x] Aplicar apenas adaptação visual Neo ao container/composer: fundo, borda, radius, espaçamento, botão enviar cyan e responsividade.
- [x] Manter painel `chat` direto funcional.
- [x] Focar composer ao abrir Dashboard.
- [x] Testar troca de modelo, workspace, profile e effort dentro do Dashboard.
- [x] Testar envio com anexo dentro do Dashboard.
- [x] Validar mobile/tablet: toolstrip pode quebrar linha, mas não pode ocultar controles, cortar labels ou sobrepor elementos.
- [x] Homologação manual registrada em 2026-05-02.

**Evidência técnica:** [`docs/neo/evidencias/HU-03.5/README.md`](./evidencias/HU-03.5/README.md) — `tests/test_neo_dashboard_chat_embed.py` valida contrato de DOM único do chat/composer, foco pós-montagem do Dashboard, handlers originais de seletores/anexos/envio e hardening responsivo do composer central.

### HU-03.6 — Topbar contextual

**Status:** concluída

**Tasks**

- [x] Criar `GET /api/health/system`.
- [x] Exibir VPS Status, Uptime, Região, Versão.
- [x] Adicionar botão Terminal SSH.
- [x] Adicionar busca/notificações/help/admin dropdown.
- [x] Poll a cada 30s com cache.

**Evidência técnica:** [`docs/neo/evidencias/HU-03.6/README.md`](./evidencias/HU-03.6/README.md)

### HU-03.7 — Ações rápidas grid 2x3

**Status:** concluída

**Tasks**

- [x] Renderizar Novo Projeto.
- [x] Renderizar Novo Documento.
- [x] Renderizar Novo Componente.
- [x] Renderizar Abrir Terminal.
- [x] Renderizar Gerar Relatório.
- [x] Renderizar Deploy Projeto.
- [x] Definir comportamento de placeholders sem backend.
- [x] Homologação visual manual registrada em 2026-05-02.

**Evidência técnica:** [`docs/neo/evidencias/HU-03.7/README.md`](./evidencias/HU-03.7/README.md)

### HU-03.8 — Card de status Neo na sidebar

**Status:** concluída

**Tasks**

- [x] Exibir mark/avatar Neo (neo-ico.png circular).
- [x] Exibir status ONLINE.
- [x] Botão "Conversar agora" removido no refinamento visual de 2026-05-02; navegação via item Dashboard na rail.
- [x] Homologação visual manual registrada em 2026-05-02.

**Evidência técnica:** [`docs/neo/evidencias/HU-03.8/README.md`](./evidencias/HU-03.8/README.md)

### HU-03.9 — Admin dropdown

**Status:** concluída
**Prioridade:** P1

**Tasks**

- [x] Menu Perfil / Configurações / Logout.
- [x] Reusar handlers existentes quando disponíveis.

**Evidência técnica:** [`docs/neo/evidencias/HU-03.9/README.md`](./evidencias/HU-03.9/README.md)

### HU-03.10 — Painel mínimo "Pessoal"

**Status:** concluída
**Prioridade:** P1

**Tasks**

- [x] Criar placeholder útil com perfil + preferências.
- [x] Linkar Settings.
- [x] Definir escopo futuro de notas pessoais.

**Evidência técnica:** [`docs/neo/evidencias/HU-03.10/README.md`](./evidencias/HU-03.10/README.md)

### HU-03.11 — Recursos VPS na sidebar

**Status:** concluída

**Tasks**

- [x] Criar `GET /api/health/vps`.
- [x] Exibir CPU/RAM/Disco/Rede com barras.
- [x] Poll a cada 30s.
- [x] Validar leitura no host/container.

**Evidência técnica:** [`docs/neo/evidencias/HU-03.11/README.md`](./evidencias/HU-03.11/README.md)

---

## Sprint 3 — Configurações Neo (embutidas no dashboard)

**Meta:** ao clicar em "Configurações", o dashboard shell permanece ativo e
exibe a UI de settings embutida — nav lateral + conteúdo à direita — com visual
Neo, preservando 100% dos handlers, guards e autosave do upstream.

### HU-08.1 — mountDashboardSettings + interceptação de navegação

**Status:** concluída
**Prioridade:** P0
**Épico:** EP-08

**Tasks**

- [ ] Implementar `mountDashboardSettings()` em `static/dashboard.js` (move `#panelSettings` side-menu e `#mainSettings` para slots do shell, equivalente a `mountDashboardChat()`).
- [ ] Implementar `restoreDashboardSettings()` para restaurar DOM ao sair.
- [ ] Interceptar `handleDashboardAdminMenu('settings')` para chamar `mountDashboardSettings()` em vez de `switchPanel('settings')`.
- [ ] Interceptar botão "Configurações" da sidebar Neo (`data-panel="settings"`) para o mesmo fluxo.
- [ ] Garantir que `dashboard-shell-mode` permanece ativo durante settings.
- [ ] Registrar evidência técnica em `docs/neo/evidencias/HU-08.1/`.

### HU-08.2 — Seção "Conversa" Neo-skinned

**Status:** disponível
**Prioridade:** P0
**Épico:** EP-08

**Tasks**

- [ ] Verificar que `#settingsPaneConversation` (transcript, JSON, import, clear) renderiza corretamente dentro do shell.
- [ ] Aplicar estilo Neo ao pane: fundo `var(--surface)`, borda `var(--border)`, botões com `.settings-action-btn` Neo.
- [ ] Confirmar que handlers `btnDownload`, `btnExportJSON`, `btnImportJSON`, `btnClearConvModal` funcionam no contexto embutido.
- [ ] Registrar evidência técnica em `docs/neo/evidencias/HU-08.2/`.

### HU-08.3 — Seção "Aparência" Neo-skinned (live preview preservado)

**Status:** disponível
**Prioridade:** P0
**Épico:** EP-08

**Tasks**

- [ ] Verificar que `#settingsPaneAppearance` (tema, skin picker, font-size) renderiza no shell.
- [ ] Confirmar live preview de tema/skin e autosave timer (`_settingsAppearanceAutosaveTimer`) intactos.
- [ ] Confirmar skin Neo aparece selecionado por padrão.
- [ ] Aplicar CSS Neo ao skin picker e theme toggle dentro do shell.
- [ ] Registrar evidência técnica em `docs/neo/evidencias/HU-08.3/`.

### HU-08.4 — Seções "Preferências", "Provedores" e "Sistema" Neo-skinned

**Status:** disponível
**Prioridade:** P0
**Épico:** EP-08

**Tasks**

- [ ] Verificar renderização de `#settingsPanePreferences`, `#settingsPaneProviders`, `#settingsPaneSystem` no shell.
- [ ] Aplicar CSS Neo consistente às três seções.
- [ ] Confirmar que cada seção é acessível via `switchSettingsSection()` dentro do contexto embutido.
- [ ] Registrar evidência técnica em `docs/neo/evidencias/HU-08.4/`.

### HU-08.5 — Dirty guard e autosave preservados

**Status:** disponível
**Prioridade:** P0
**Épico:** EP-08

**Tasks**

- [ ] Garantir que `mountDashboardSettings()` chama `_beginSettingsPanelSession()` (ou equivalente via `switchPanel` interno) para ativar `_settingsDirty`.
- [ ] Testar fluxo: editar skin → tentar navegar para outro painel → guard de unsaved deve aparecer.
- [ ] Testar fluxo: editar skin → confirmar discard → painel troca sem alerta.
- [ ] Garantir que `restoreDashboardSettings()` chama `_revertSettingsPreview()` quando dirty.

### HU-08.6 — Testes automáticos de settings embutido

**Status:** disponível
**Prioridade:** P0
**Épico:** EP-08

**Tasks**

- [ ] Criar `tests/test_neo_dashboard_settings.py`.
- [ ] Testar: `dashboard.js` contém `mountDashboardSettings` e `restoreDashboardSettings`.
- [ ] Testar: CSS Neo presente para `.dashboard-shell-mode .settings-main`, `.dashboard-shell-mode .side-menu-item`.
- [ ] Testar: `index.html` mantém `#panelSettings`, `#mainSettings`, `#settingsPaneAppearance` e outros panes existentes.
- [ ] Testar: i18n contém chaves de settings em pt-BR e en.

### Encerramento Sprint 3

- [ ] HU-08.1 a HU-08.6 concluídas por DoD.
- [ ] `pytest tests/test_neo_dashboard_settings.py -v` passa.
- [ ] Suite completa Neo passa sem regressão.
- [ ] Evidências em `docs/neo/evidencias/HU-08.*`.
- [ ] Commit limpo em `develop`.

---

## Sprint 4 — Skills Neo (embutidas no dashboard)

**Meta:** ao clicar em "Skills" na sidebar Neo, o dashboard shell permanece ativo
e exibe o painel de skills embutido em layout master-detail: lista à esquerda
(260px) + detalhe à direita. Toda a lógica upstream preservada.

### HU-09.1 — mountDashboardSkills + NEO_SHELL_PANELS

**Status:** concluída
**Prioridade:** P0
**Épico:** EP-09

**Tasks**

- [x] Adicionar `'skills'` a `NEO_SHELL_PANELS` em `static/panels.js`.
- [x] Adicionar chamada `mountDashboardSkills()` no bloco `nextPanel === 'skills'` de `switchPanel()`.
- [x] Adicionar chamada `restoreDashboardSkills()` na guard de saída (junto a `restoreDashboardSettings`).
- [x] Implementar `mountDashboardSkills()` em `static/dashboard.js`: move `#panelSkills` inteiro para `#mainSkills` como primeiro filho (anchor pattern).
- [x] Implementar `restoreDashboardSkills()`: devolve `#panelSkills` à posição original via anchor.
- [x] Registrar evidência técnica em `docs/neo/evidencias/HU-09.1/`.

**Evidência técnica:** [`docs/neo/evidencias/HU-09.1/README.md`](./evidencias/HU-09.1/README.md)

### HU-09.2 — Layout two-column + Neo styling

**Status:** concluída
**Prioridade:** P0
**Épico:** EP-09

**Tasks**

- [x] CSS: `body.dashboard-shell-mode main.main.showing-skills > #mainSkills` -> `display:flex; flex-direction:row; overflow:hidden; padding:0`.
- [x] CSS: `#panelSkills` dentro do shell -> `width:260px; flex-shrink:0; border-right:1px solid var(--border); display:flex; flex-direction:column; overflow:hidden`.
- [x] CSS: área de detalhe (`#skillDetailBody`, `#skillDetailEmpty`, header) -> `flex:1; min-width:0`.
- [x] Confirmar que `loadSkills()`, `renderSkills()`, `filterSkills()`, `openSkillCreate()`, edição e deleção funcionam sem alteração.
- [x] Registrar evidência técnica em `docs/neo/evidencias/HU-09.2/`.

**Evidência técnica:** [`docs/neo/evidencias/HU-09.2/README.md`](./evidencias/HU-09.2/README.md)

### HU-09.3 — Testes automáticos

**Status:** concluída
**Prioridade:** P0
**Épico:** EP-09

**Tasks**

- [x] Criar `tests/test_neo_dashboard_skills.py`.
- [x] Testar: `'skills'` em `NEO_SHELL_PANELS` em `panels.js`.
- [x] Testar: `mountDashboardSkills` e `restoreDashboardSkills` definidos em `dashboard.js`.
- [x] Testar: CSS two-column presente em `style.css`.
- [x] Testar: `#panelSkills`, `#skillsList`, `#skillsSearch`, `#mainSkills` presentes em `index.html`.
- [x] Suite Neo focada passa sem regressão; coleta completa registrada separadamente.

**Evidência técnica:** [`docs/neo/evidencias/HU-09.3/README.md`](./evidencias/HU-09.3/README.md)

### Encerramento Sprint 4

- [x] HU-09.1 a HU-09.3 concluídas por DoD.
- [x] `pytest tests/test_neo_dashboard_skills.py -v` passa.
- [x] Suite Neo focada passa sem regressão.
- [x] Evidências em `docs/neo/evidencias/HU-09.*`.
- [ ] Commit limpo em `develop`.

---

## Sprint 5 — Projetos Command Center

**Meta:** página Projetos full-page como central de comando local-first:
Kanban de 4 colunas, vista Lista, filtros operacionais, persistência local e
campos `external_ref` preparados para Jira/GitHub/Obsidian. A sincronização
real com Jira fica documentada no EP-10 e não entra nesta sprint.

### HU-04.1 — Página Projetos com header

**Status:** disponível

**Tasks**

- [x] Adicionar item Projetos na sidebar.
- [x] Criar painel `projects`.
- [x] Criar `static/kanban.js`.
- [x] Header: título, subtítulo, Filtros, Kanban, Lista, + Novo Projeto.
- [x] Carregar `static/kanban.js` em `index.html`.

**Evidência técnica:** [`docs/neo/evidencias/HU-04.1/README.md`](./evidencias/HU-04.1/README.md)

### HU-04.2 — Kanban 4 colunas

**Status:** concluída

**Tasks**

- [x] Implementar colunas `backlog`, `em_andamento`, `em_revisao`, `concluido`.
- [x] Aplicar top-border slate/amber/blue/green.
- [x] Contagem por coluna.
- [x] Mobile: 1 coluna com tabs/filtro por status no topo.
- [x] Tablet: scroll horizontal interno/layout em 2 colunas.

**Evidência técnica:** [`docs/neo/evidencias/HU-04.2/README.md`](./evidencias/HU-04.2/README.md)

### HU-04.3 — Criar projeto via modal

**Status:** concluída

**Tasks**

- [x] Criar `api/projects.py` Neo-only.
- [x] Migrar formato antigo de `projects.json` (lista simples) para schema v2 tolerante.
- [x] `GET /api/projects` retorna `{projects, tasks, sources, counts}`.
- [x] `POST /api/projects` cria projeto.
- [x] `PATCH /api/projects/{project_id}` edita projeto.
- [x] Persistir em `~/.hermes/webui/projects.json`.
- [x] Campos: nome, descrição, domínio, cor, fonte externa padrão opcional.

**Evidência técnica:** [`docs/neo/evidencias/HU-04.3/README.md`](./evidencias/HU-04.3/README.md)

### HU-04.4 — Criar tarefas com `external_ref`

**Status:** concluída

**Tasks**

- [x] `POST /api/project-tasks` cria tarefa vinculada a projeto.
- [x] `PATCH /api/project-tasks/{task_id}` edita tarefa.
- [x] Campos: título, descrição, status, categoria, prioridade, responsável, prazo, progresso.
- [x] Persistir `external_ref` opcional (`type`, `source_id`, `key`, `url`, `status`, `synced_at`).
- [x] Persistir refs opcionais (`github`, `obsidian`, `sessions`).

**Evidência técnica:** [`docs/neo/evidencias/HU-04.4/README.md`](./evidencias/HU-04.4/README.md)

### HU-04.5 — Drag-and-drop persistido

**Status:** concluída

**Tasks**

- [x] Implementar HTML5 drag-and-drop sem libs.
- [x] `PATCH /api/project-tasks/{task_id}` atualiza `{ status }`.
- [x] UI otimista com rollback em erro.
- [x] Visual drag: glow cyan, rotate 2deg, drop target destacado.

**Evidência técnica:** [`docs/neo/evidencias/HU-04.5/README.md`](./evidencias/HU-04.5/README.md)

### HU-04.6 — Cards com chips e progresso

**Status:** concluída

**Tasks**

- [x] Chips de categoria: Design, Frontend, Backend, Database, Infra, DevOps, Docs, QA, Segurança.
- [x] Chips de prioridade: Baixa, Média, Alta.
- [x] Barra de progresso nas colunas não concluídas.
- [x] Chip verde `Concluído` sem barra na coluna concluído.
- [x] Mostrar chip discreto de fonte externa quando `external_ref` existir.

**Evidência técnica:** [`docs/neo/evidencias/HU-04.6/README.md`](./evidencias/HU-04.6/README.md)

### HU-04.7 — Status pills clicáveis

**Status:** concluída

**Tasks**

- [x] Total, Backlog, Em Andamento, Revisão, Concluído.
- [x] Clique filtra/destaca a coluna.
- [x] Contadores sincronizados após drag/criação.

**Evidência técnica:** [`docs/neo/evidencias/HU-04.7/README.md`](./evidencias/HU-04.7/README.md)

### HU-04.8 — Vista Lista + filtros

**Status:** concluída

**Tasks**

- [x] Alternar Kanban/List sem recarregar a página.
- [x] Lista agrupada por status com colunas ID, tarefa, prioridade, responsável e estado.
- [x] Filtros por texto, projeto, status, prioridade, fonte externa, responsável e data/prazo.
- [x] Botão dashed `+ Adicionar tarefa` no footer de cada coluna.
- [x] Persistir nova tarefa com o endpoint de criação.

**Evidência técnica:** [`docs/neo/evidencias/HU-04.8/README.md`](./evidencias/HU-04.8/README.md)

### HU-04.9 — Vincular sessão e refs externas

**Status:** parcial
**Prioridade:** P1

**Tasks**

- [x] Usar `session.project_id` já existente.
- [ ] UI para atribuir sessão Neo a tarefa/projeto.
- [x] Campos persistidos para refs GitHub e Obsidian sem sync automática.
- [x] Exibir vínculos no detalhe da tarefa quando já existirem.

**Evidência técnica:** [`docs/neo/evidencias/HU-04.9/README.md`](./evidencias/HU-04.9/README.md)

### HU-04.10 — Arquivar e mobile

**Status:** parcial
**Prioridade:** P1

**Tasks**

- [x] Status/filtro `arquivado`.
- [x] Toggle "Mostrar arquivados".
- [x] Não contar arquivados em Projetos Ativos.
- [x] Tabs/filtro por status no topo.
- [ ] Drag fallback via menu "Mover para".

**Evidência técnica:** [`docs/neo/evidencias/HU-04.10/README.md`](./evidencias/HU-04.10/README.md)

### Backlog pós-Sprint 5 — Sincronização Jira

**Status:** documentado em EP-10

**Tasks futuras**

- [ ] Cadastrar múltiplos Jiras.
- [ ] Criar issue Jira a partir do chat e gravar `external_ref`.
- [ ] Importar issues existentes.
- [ ] Sincronizar status remoto/local com mapeamento por fonte.
- [ ] Reconciliar conflitos sem sobrescrita silenciosa.

---

## Sprint 6 — Ações rápidas + Finanças

**Meta:** fechar o MVP com ações rápidas operacionais e página Finanças com
shell visual completo, SVG vanilla e persistência local.

### HU-05.1 — Atalho "Salvar memória"

**Status:** aguardando Sprint 5

**Tasks**

- [ ] Botão/modal pré-preenchido para salvar memória.
- [ ] Integrar com skill/fluxo existente quando disponível.

### HU-05.2 — Atalho "Novo terminal"

**Status:** aguardando Sprint 5

**Tasks**

- [ ] Botão abre painel `terminal` upstream.
- [ ] Validar foco/retorno para Dashboard.

### HU-05.3 — Seletor "Executar skill"

**Status:** aguardando Sprint 5

**Tasks**

- [ ] Listar skills do runtime.
- [ ] Abrir composer/comando com skill selecionada.

### HU-05.4 — Indicador de job cron concluído

**Status:** aguardando Sprint 5

**Tasks**

- [ ] Consolidar evento/estado de cron no Dashboard.
- [ ] Exibir indicador visual recente.

### HU-06.1 — Página Finanças com header

**Status:** aguardando Sprint 5
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

### Encerramento Sprint 6

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
- [x] Criar cobertura focada da Sprint 2 em `tests/test_neo_dashboard_sprint2.py` e testes Neo complementares.
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

- [x] Implementar junto com HU-02.3.
- [ ] Rodar em PRs Neo.

### HU-07.5 — CI / suíte completa

- [ ] Validar CI upstream existente.
- [ ] Garantir que testes Neo entram no comando esperado.

---

## Sprint 7+ — Painel Agentes (futuro / P2)

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

### Retro Sprint 5

_A preencher._

### Retro Sprint 6

_A preencher._
