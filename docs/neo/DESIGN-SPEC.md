# Design Spec — Neo WebUI

> Tradução textual dos mockups [`docs/neo/neo_agent_web_ui.png`](./neo_agent_web_ui.png)
> (Dashboard), [`docs/neo/neo_projetos.png`](./neo_projetos.png) (Projetos) e
> [`docs/neo/neo_financas.png`](./neo_financas.png) (Finanças), e do protótipo
> de referência hospedado em
> `https://dd84ecdc-9bfc-44ed-abb7-eed4b5b43eb6-00-29ndcxm0907el.kirk.replit.dev/neo-dashboard/`.
>
> Este documento existe para que qualquer instância (humana ou agente) consiga
> reproduzir a UI **sem ver as imagens**.
>
> O protótipo Replit é apenas referência **visual** — a stack permanece
> **vanilla JS, sem build, sem framework**, conforme PRD §7 e RNF-01.
> Sempre que houver dúvida entre o código atual e este spec, **vence o spec**.
> Se o spec estiver errado, atualizar aqui antes de mexer no código.

> **Versão:** 3.3 — 2026-05-09 (adiciona §7-bis Painel Agentes — EP-AG híbrido com pixel-agents)

---

## 1. Layout geral

A WebUI tem **uma sidebar fixa à esquerda** e uma **área principal** com
**topbar superior** e conteúdo trocado por painel. **Três páginas** estão no
escopo desta spec: **Dashboard** (tela inicial), **Projetos** (Kanban
full-page) e **Finanças** (controle financeiro).

### 1.1. Layout — Dashboard

```
┌────────────┬─────────────────────────────────────────────────────────────────┐
│ SIDEBAR    │ TOPBAR — VPS Status · Uptime · Região · Versão · ações · admin │
│ (240px)    ├──────────────────────────────────────────────┬──────────────────┤
│ fixed      │  CHAT COM NEO                                │ HERO — avatar IA │
│ full ht    │  (coluna central, flex 1)                    ├──────────────────┤
│ vertical:  │  ┌────────────────────────────────────────┐  │ Bem-vindo +      │
│ - logo     │  │ header interno + bullet "Online"       │  │ 4 stat cards     │
│ - nav      │  ├────────────────────────────────────────┤  │ (com deltas)     │
│ - status   │  │ messages list (scroll-y)               │  ├──────────────────┤
│   card     │  │ + arquivos anexados                    │  │ AÇÕES RÁPIDAS    │
│ - VPS res. │  ├────────────────────────────────────────┤  │ grid 2 colunas   │
│ - footer   │  │ composer                               │  │ (6 botões)       │
│            │  └────────────────────────────────────────┘  │                  │
└────────────┴──────────────────────────────────────────────┴──────────────────┘
```

### 1.2. Layout — Projetos

```
┌────────────┬─────────────────────────────────────────────────────────────────┐
│ SIDEBAR    │ TOPBAR — (mesma do Dashboard)                                   │
│ (240px)    ├─────────────────────────────────────────────────────────────────┤
│ fixed      │  PROJETOS                              [Filtros] [Kanban▾] [+] │
│ full ht    │  Gerencie e acompanhe todos os seus projetos                    │
│            │  ┌─────────────────────────────────────────────────────────┐   │
│            │  │ Total 30 · Backlog 6 · Em And. 8 · Revisão 4 · Concl.12 │   │
│            │  └─────────────────────────────────────────────────────────┘   │
│            │  ┌────────┬────────────┬─────────┬────────────┐                 │
│            │  │ Backlog│ Em Andam.  │ Revisão │ Concluído  │  Kanban         │
│            │  │   (6)  │    (8)     │   (4)   │    (12)    │  4 colunas      │
│            │  │ cards  │   cards    │  cards  │   cards    │  full-width     │
│            │  │   …    │     …      │    …    │     …      │                 │
│            │  │ + Add  │   + Add    │  + Add  │   + Add    │                 │
│            │  └────────┴────────────┴─────────┴────────────┘                 │
└────────────┴─────────────────────────────────────────────────────────────────┘
```

### 1.3. Layout — Finanças

```
┌────────────┬─────────────────────────────────────────────────────────────────┐
│ SIDEBAR    │ TOPBAR — (mesma do Dashboard)                                   │
│ (240px)    ├─────────────────────────────────────────────────────────────────┤
│ fixed      │  FINANÇAS                       [Terminal SSH] [+ Nova Finança] │
│            │  Controle financeiro pessoal e empresarial                      │
│            │  ┌────────┬────────┬───────────┬───────────────┐                │
│            │  │Receitas│Despesas│Saldo Líq. │Investimentos │  4 KPI cards   │
│            │  │R$1.564 │R$ 0,00 │ R$ 0,00   │   R$ 0,00    │                │
│            │  └────────┴────────┴───────────┴───────────────┘                │
│            │  ┌──────────────────────────────────────┬──────────────────┐    │
│            │  │ RESUMO FINANCEIRO   [Rec][Desp] [▾]  │  Orçamentos      │    │
│            │  │ ┌──────────────────────────────────┐ │                  │    │
│            │  │ │ line chart (R$ x mês)            │ │  Transações      │    │
│            │  │ │  cyan = receitas                 │ │  Recentes        │    │
│            │  │ │  red  = despesas                 │ │   (lista de 5)   │    │
│            │  │ └──────────────────────────────────┘ │                  │    │
│            │  ├──────────────────────────────────────┤  Metas           │    │
│            │  │ Gastos por Categoria   [Datas]       │  Financeiras     │    │
│            │  │ (donut/lista) — placeholder vazio    │                  │    │
│            │  └──────────────────────────────────────┴──────────────────┘    │
└────────────┴─────────────────────────────────────────────────────────────────┘
```

Proporções de referência (desktop ≥ 1280px):
- **Sidebar:** 240px fixa.
- **Topbar:** 56px de altura, full-width da área principal.
- **Dashboard grid central:** `1fr 320px` com gap 24px.
- **Projetos:** Kanban full-width, 4 colunas equilibradas (`repeat(4, 1fr)` com gap 16px).
- **Finanças:** linha de KPI cards full-width (`repeat(4, 1fr)`); abaixo, grid `1fr 320px` com gap 16px (área principal de gráfico/categorias à esquerda; coluna lateral de orçamentos/transações/metas à direita).

**Mobile (< 900px):**
- Sidebar vira off-canvas (hamburger no topbar). Já existe upstream.
- Dashboard: coluna direita empilha **acima** do chat.
- Projetos: Kanban empilha em 1 coluna com tabs no topo.
- Finanças: KPI cards viram grid 2x2; coluna lateral empilha abaixo do gráfico.

**Tablet (900–1280px):**
- Sidebar permanece fixa (240px).
- Dashboard: grid central vira 1 coluna.
- Projetos: 4 colunas mantidas com scroll horizontal interno.
- Finanças: KPI cards mantêm 4 colunas; coluna lateral colapsa abaixo se largura < 1100px.

---

## 2. Paleta (skin "neo" — já implementada em `style.css`)

**Dark mode (default no deploy Neo):**

| Variável | Valor | Uso |
|---|---|---|
| `--bg` | `#070B17` | Fundo geral (azul-noite quase preto) |
| `--sidebar` | `#0B1224` | Sidebar e topbar |
| `--surface` | `#121A2E` | Cards, modais, painéis suspensos |
| `--surface-2` | `#0E1426` | Variante mais escura para fundos secundários (chat list, áreas internas) |
| `--border` | `#1F2A44` | Bordas sutis |
| `--border2` | `#2C3A5A` | Bordas mais visíveis (hover, focus) |
| `--text` | `#E6F4FF` | Texto primário |
| `--muted` | `#8AA0BD` | Texto secundário (labels, descrições, datas) |
| `--strong` | `#FFFFFF` | Títulos, números grandes |
| `--accent` | `#00E5FF` | Cyan neon — hero, links, foco, botões primários |
| `--accent-hover` | `#00B8D4` | Hover do accent |
| `--accent-text` | `#5EE9FF` | Texto sobre accent (ex: links em cards) |
| `--accent-bg` | `rgba(0,229,255,0.08)` | Fundos sutis (chips, focus glow) |
| `--accent-bg-strong` | `rgba(0,229,255,0.18)` | Fundos mais densos (mensagem do usuário no chat) |
| `--success` | `#22C55E` | Status online, "Concluído", deltas positivos, receitas |
| `--success-bg` | `rgba(34,197,94,0.10)` | Fundos de pills "Online" / "Concluído" / KPI receitas |
| `--warning` | `#F0B429` | Em Andamento, prioridade Média, saldo neutro |
| `--warning-bg` | `rgba(240,180,41,0.10)` | KPI Saldo Líquido |
| `--info` | `#2196F3` | Em Revisão |
| `--danger` | `#EF5350` | Atrasado / prioridade Alta / erros / despesas |
| `--danger-bg` | `rgba(239,83,80,0.10)` | KPI Despesas |
| `--violet` | `#A78BFA` | Investimentos, categorias secundárias |
| `--violet-bg` | `rgba(167,139,250,0.10)` | KPI Investimentos |

**Cores de status (Kanban, badges):**

| Estado | Hex | Uso |
|---|---|---|
| Backlog | `#8AA0BD` (slate) | Header da coluna; cards mais sóbrios |
| Em Andamento | `#F0B429` (amber) | Header da coluna, barras de progresso ativas |
| Em Revisão | `#2196F3` (blue) | Header da coluna |
| Concluído | `#22C55E` (green) | Header da coluna, status "online" |

**Cores de tag de categoria (cards de projeto):**

| Categoria | Cor da chip |
|---|---|
| Design | `#E879F9` (magenta claro) |
| Frontend | `#A78BFA` (violeta) |
| Backend | `#60A5FA` (azul claro) |
| Database | `#34D399` (verde-água) |
| Infra | `#94A3B8` (slate) |
| DevOps | `#FBBF24` (amber claro) |
| Docs | `#A3A3A3` (cinza claro) |
| QA | `#F472B6` (rosa) |
| Segurança | `#F87171` (vermelho claro) |

**Cores de prioridade (chip ao lado da categoria):**

| Prioridade | Cor |
|---|---|
| Baixa | `#94A3B8` slate, fundo `rgba(148,163,184,0.10)` |
| Média | `#F0B429` amber, fundo `rgba(240,180,41,0.10)` |
| Alta | `#EF5350` vermelho, fundo `rgba(239,83,80,0.10)` |

**Cores de séries em gráficos (Finanças):**

| Série | Cor da linha | Cor do fill (área) |
|---|---|---|
| Receitas | `#00E5FF` (--accent) | `rgba(0,229,255,0.15)` |
| Despesas | `#EF5350` (--danger) | `rgba(239,83,80,0.15)` |
| Saldo Líquido | `#F0B429` (--warning) | `rgba(240,180,41,0.15)` |
| Investimentos | `#A78BFA` (--violet) | `rgba(167,139,250,0.15)` |

**Glow do hero:**
- Halo principal: `radial-gradient(circle, rgba(0,229,255,0.4), transparent 70%)`
- Reflexo no fundo: `rgba(0,229,255,0.1)` em blur grande
- Wireframe sobre o avatar: traços `rgba(0,229,255,0.6)` com `mix-blend-mode: screen`

---

## 3. Tipografia

**Fonte primária:** `Inter` (400/500/600/700) carregada via Google Fonts no
`<head>` do `index.html`:

```html
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
```

`--font-ui` no skin "neo" passa a ser:
`'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif`.

| Elemento | Tamanho | Peso | Cor |
|---|---|---|---|
| Logo "NEO" sidebar | 18px | 700 | `--strong` |
| Sub-label "AGENTE 24/7" | 10px | 500, letter-spacing 0.15em uppercase | `--muted` |
| Nav item sidebar | 13px | 500 | `--text`; ativo: `--accent` |
| Status card (sidebar) — "NEO" | 13px | 600 | `--strong` |
| Status card — "ONLINE" | 11px | 600 uppercase | `--success` |
| Status card — descrição | 11px | 400 | `--muted` |
| Recursos VPS — label (CPU, RAM…) | 10px | 500 uppercase letter-spacing 0.08em | `--muted` |
| Recursos VPS — valor (24%) | 10px | 600 | `--accent` (verde quando crítico inverso) |
| Topbar — labels ("VPS STATUS:", "UPTIME:") | 10px | 500 uppercase letter-spacing 0.1em | `--muted` |
| Topbar — valores ("Root", "127d 04h 13m") | 12px | 500 | `--text` |
| Topbar — pill "ONLINE" | 11px | 600 | `--success` |
| Page title ("Projetos", "Finanças") | 24px | 700 | `--strong` |
| Page subtitle | 13px | 400 | `--muted` |
| Saudação "Bem-vindo de volta!" | 18px | 600 | `--strong` |
| Subtítulo de saudação | 12px | 400 | `--muted` |
| Stat card — número | 32px | 700 | `--strong`, com leve glow `--accent` |
| Stat card — label | 10px | 500, uppercase, letter-spacing 0.08em | `--muted` |
| Stat card — delta ("✓ +2 este mês") | 11px | 500 | `--success` (positivo) / `--danger` (negativo) |
| KPI Finanças — valor (R$ 1.564,99) | 22px | 700 | `--strong` |
| KPI Finanças — label | 11px | 500 uppercase letter-spacing 0.08em | `--muted` |
| Chart axis label | 10px | 500 | `--muted` |
| Section title ("Ações Rápidas", "Resumo Financeiro") | 14px | 600 | `--text` |
| Card título (Kanban) | 13px | 600 | `--strong` |
| Card chips (categoria, prioridade) | 10px | 500 | depende da cor da chip |
| Card progresso (75%) | 10px | 500 | `--muted` |
| Chat header "Chat com Neo" | 14px | 600 | `--text` |
| Chat mensagem | 13px | 400 | `--text` |
| Chat timestamp | 10px | 400 | `--muted` |
| Transação — título | 12px | 500 | `--text` |
| Transação — sub (data, método) | 10px | 400 | `--muted` |
| Transação — valor | 12px | 600 | `--success` (receita) / `--danger` (despesa) |

---

## 4. Sidebar (esquerda) — 240px de largura

Estrutura vertical, do topo para o fundo:

### 4.1. Header — 72px de altura

- Avatar circular do orb/avatar Neo (40x40, `--accent` com glow) à esquerda
- Stack de duas linhas à direita:
  - "NEO" em 18px/700 `--strong`
  - "AGENTE 24/7" em 10px/500 uppercase, `--muted`, letter-spacing 0.15em
- Border-bottom `--border`

### 4.2. Lista de navegação — gap 4px entre itens, padding 12px lateral

Cada item: ícone 18x18 (`stroke-width:1.5`) à esquerda + label, padding 10px 14px,
border-radius 8px.

Item ativo: fundo `--accent-bg`, **border-left 2px solid --accent**, ícone e
texto em `--accent`. Hover: fundo `--accent-bg`, sem border-left.

**Itens (ordem do mockup):**

1. **Dashboard** (ícone: `layout-grid` / 4 retângulos)
2. **Projetos** (ícone: `folder`)
3. **Tarefas** (ícone: `check-square`) — agrega `tasks` (cron) + `todos` upstream
4. **Pessoal** (ícone: `user`) — perfil, contatos próximos, notas pessoais (placeholder pós-MVP)
5. **Finanças** (ícone: `dollar-sign` ou `wallet`) — página spec'd em §8
6. **Agentes** (ícone: `cpu` ou `bot`) — mapa pixel-art em tempo real do Neo orquestrador + subagentes (Sprint 7 / EP-AG, ver §7-bis abaixo e PRD RF-11 / RF-AG.*)
7. **Skills** (ícone: `zap` / raio) — listagem de skills disponíveis no runtime
8. **Automação** (ícone: `settings-2` ou `workflow`) — painel `tasks` (cron) na visão de automações
9. **Configurações** (ícone: `settings` / engrenagem)

> Itens parcialmente placeholders no MVP (Sprints 1–4):
> - **Pessoal**: bloco mínimo com avatar, dados do usuário e link para Settings
> - **Agentes**: estado vazio no MVP; entrega completa na **Sprint 7** com pixel-agents híbrido (EP-AG, ver §7-bis)
>
> **Finanças** ganha página dedicada (§8) — entrega no MVP é **shell visual**
> (KPIs e listas vazios ou com dados de demonstração). Backend financeiro real
> entra em sprint posterior.

### 4.3. Card de status do Neo — bloco fixo abaixo da nav

Container: padding 12px, background `--surface`, border `--border`, border-radius 12px, margin lateral 12px.

- Cabeçalho: avatar do orb (32x32) + stack
  - "NEO" 13px/600 `--strong`
  - Linha com bullet verde pulsante 8x8 + "ONLINE" 11px/600 uppercase `--success`
- Sub-linha: "Ativo 24/7" 11px/500 `--text`
- Descrição: "Respondendo e executando tarefas." 11px/400 `--muted`
- **Botão "⚡ Conversar agora"** — full-width 100%, padding 8px, fundo `--accent-bg-strong`, borda `--accent`, texto `--accent` 12px/600, ícone `zap` à esquerda. Hover: fundo `--accent`, texto `--bg`. Clique: foca o composer do chat (atalho para `panel=dashboard` se não estiver na tela do chat).

### 4.4. Recursos VPS — bloco fixo abaixo do status card

Container: padding 12px lateral, gap 8px entre métricas.

- Header: "RECURSOS VPS" 10px/500 uppercase letter-spacing 0.1em `--muted` (com `padding-top: 16px`)
- 4 linhas, cada uma com:
  - Label (CPU / RAM / DISCO / REDE) 10px/500 uppercase `--muted` à esquerda
  - Valor (24%) 10px/600 `--accent` à direita
  - Barra de progresso 3px logo abaixo, fundo `--border`, fill `--accent` (regra de cor: ≥ 80% vira `--warning`, ≥ 95% vira `--danger`)

Atualização: poll a cada 30s ao backend (`GET /api/health/vps`) — endpoint novo a especificar.

### 4.5. Footer da sidebar — 36px de altura

- Padding 12px lateral, border-top `--border`
- Dois links inline: **Documentação** (esquerda) · **Suporte** (direita)
- 11px/500 `--muted`, hover `--accent-text`

---

## 5. Topbar — 56px de altura, full-width da área principal

Background `--sidebar`, border-bottom `--border`. Padding lateral 24px.

Da esquerda para direita (com separadores em pipe `|` 1px de altura `--border`):

1. **VPS STATUS:** label `--muted` + "Root" 12px/500 `--text` + pill "● ONLINE" (bullet pulsante verde + texto 11px/600 `--success` em pill com fundo `--success-bg`)
2. **UPTIME:** label + "127d 04h 13m" 12px/500 `--text`
3. **REGIÃO:** label + "São Paulo / BR" 12px/500 `--text`
4. **VERSÃO:** label + "v2.5.0-neo" 12px/500 `--text`

À direita (alinhado em `flex-end`, gap 12px):

5. **Botão "Terminal SSH"** — pill outline `--accent`, ícone `terminal` 14x14, texto 12px/600 `--accent`. Hover: fundo `--accent-bg`. Clique: abre painel `terminal` upstream.
6. **Ícone busca** (`search` 18x18 `--muted`) — abre command palette / busca global
7. **Ícone notificações** (`bell` 18x18 `--muted`) — badge vermelho com contagem quando há pendências
8. **Ícone help** (`help-circle` 18x18 `--muted`) — abre `?command=help`
9. **Avatar admin (32x32)** + label "Admin" 12px/500 `--text` + chevron-down 14x14 `--muted`. Clique: abre menu (Perfil / Configurações / Logout).

> ⚠️ **Backend novo necessário:** `GET /api/health/system` deve retornar
> `{ vps_status, hostname, uptime_seconds, region, version }`. Polling 30s.
> Endpoint precisa ser definido em `api/routes.py` (Neo-only) e cacheado.

---

## 6. Página **Dashboard**

Grid de duas colunas + ausência de Kanban (o Kanban tem página dedicada).

### 6.1. Coluna central — Chat com Neo

Painel principal do Dashboard, ocupa toda a altura disponível abaixo da topbar.
Background `--surface`, border `--border`, border-radius 12px.

1. **Header interno** — 56px, padding 16px 20px, border-bottom `--border`
   - Título "Chat com Neo" 14px/600 `--text`
   - À direita: pill "● Online" 11px/600 `--success` em fundo `--success-bg`

2. **Lista de mensagens** — flex 1, scroll-y, padding 20px, fundo `--surface-2`
   - Mensagem do agente (esquerda):
     - Avatar circular 32x32 do Neo à esquerda
     - Bolha `--surface`, border `--border`, border-radius 12px, padding 12px 16px, max-width 70%
     - Texto 13px `--text`. Suporta emoji inline e listas numeradas
     - Timestamp 10px `--muted` abaixo da bolha
   - Mensagem do usuário (direita):
     - Sem avatar
     - Bolha `--accent-bg-strong`, border-radius 12px, padding 12px 16px, max-width 70%
     - Texto 13px `--text`
     - Timestamp 10px `--muted` à direita da bolha + ícone "checks" `✓✓` (lido) `--accent`
   - **Cards de arquivo dentro de mensagem:**
     - Container inline-flex, padding 8px 12px, fundo `--surface`, border `--border`, border-radius 8px
     - Ícone `file-text` 16x16 `--accent`
     - Stack: nome do arquivo 12px/600 `--text` + tamanho "12.4 KB" 10px `--muted`
     - Ícone `check` 14x14 `--success` à direita (gerado / salvo com sucesso)
   - Mensagens com lista numerada renderizam com numeração colorida (cyan).

3. **Composer / toolstrip do Hermes preservado** — altura flexível, padding
   12px 16px, border-top `--border`
   - O rodapé do chat deve manter o **composer completo já existente no Hermes
     WebUI**, sem reimplementar lógica em paralelo. A aparência deve seguir o
     mockup Neo (fundo `--surface-2`, borda `--border`, cantos arredondados e
     botão enviar circular cyan), mas os controles e handlers continuam sendo
     os mesmos do chat atual.
   - Controles obrigatórios a preservar no Dashboard:
     - **Anexos** (`paperclip`) e fluxo atual de upload/preview.
     - **Microfone/voz**, quando habilitado pelo ambiente/navegador.
     - **Profile ativo** com label e dropdown (ex: `default`).
     - **Workspace ativo** com seletor/dropdown.
     - **Modelo configurado** com seletor/dropdown (ex: `GPT-5.4 Mini`).
     - **Reasoning/effort** com seletor/dropdown (ex: `high`).
     - Ações auxiliares já existentes no composer upstream (ex: menu de mais
       opções, emoji, toolstrip), quando presentes.
     - Botão de envio atual, com o mesmo fluxo de submit/cancel/stream.
   - Placeholder: usar o nome configurado do agente, por exemplo
     "Message Neo..." ou a tradução pt-BR equivalente quando o locale estiver
     em `pt-BR`; não hardcodar "Neo" quando `HERMES_WEBUI_BOT_NAME` for outro.
   - Auto-foco quando o painel é aberto e quando "Conversar agora" é clicado.
   - Em larguras menores, os controles podem quebrar para uma segunda linha,
     mas não podem sumir, sobrepor texto ou perder acessibilidade.

> **Decisão arquitetural:** o chat central é o **mesmo SSE** da sessão ativa do
> Hermes — não é uma instância separada. Painel `chat` upstream continua
> existindo como rota direta (ex: `?panel=chat`); no Dashboard ele aparece
> embutido no grid. Pela mesma razão, o Dashboard deve **reaproveitar o
> composer/toolstrip upstream** em vez de criar um segundo composer. Essa
> decisão reduz risco de regressão em seleção de modelo, workspace, profile,
> uploads, voz e reasoning/effort.

### 6.2. Coluna direita — Hero

Card grande, height ~220px, background `--surface`, border `--border`, border-radius 12px, padding 16px, com efeito de **profundidade estelar**:

- Base: gradient radial centrado de `rgba(0,229,255,0.08)` para `--surface` para `transparent`
- Overlay sutil de "código matriz" (texto verde-cyan em opacidade 0.15) atrás do avatar — fonte mono 9px, gap denso
- Pequenos pontos brilhantes (12-15) distribuídos como estrelas: `box-shadow: 0 0 4px var(--accent)`
- No centro: **avatar humanoide holográfico** (rosto/torso vista frontal):
  - Silhueta antropomórfica em traços wireframe cyan
  - Cor base: gradient azul `#00B8D4 → #0288A8 → #04101A`
  - Sobreposição: linhas finas de polígonos (opacidade 0.7) cyan
  - Texto "NEO" em 14px/700 `--strong` glow centrado abaixo do rosto
- Pill no rodapé: **"STATUS: OPERACIONAL"** 9px/600 uppercase letter-spacing 0.2em, fundo `--bg` semi-transparente, borda `--accent`, texto `--accent`

Asset definitivo: §9.

### 6.3. Coluna direita — Bem-vindo + KPIs

- "Bem-vindo de volta! 👋" 18px/600 `--strong` (margin-top 16px)
- Sub-linha: "Aqui está o resumo do que está acontecendo com seus projetos." 12px/400 `--muted`
- Grid 2x2 de **stat cards** com gap 12px (margin-top 16px):

```
┌────────────────┬────────────────┐
│ PROJETOS       │ TAREFAS EM     │
│ ATIVOS         │ ANDAMENTO      │
│ 12             │ 28             │
│ ✓ +2 este mês  │ ✓ +5 desde ont.│
├────────────────┼────────────────┤
│ CONCLUÍDAS     │ AGENTES ONLINE │
│ 156            │ 7              │
│ ✓ +18 esta sem.│ ✓ Todos op.    │
└────────────────┴────────────────┘
```

Cada card:
- Padding 14px, background `--surface` com gradient sutil para `--accent-bg` no canto inferior direito
- Border `1px solid --border`, border-radius 12px
- **Label** no topo (10px uppercase `--muted`), pode quebrar em 2 linhas
- **Número** (32px/700 `--strong`, com `text-shadow: 0 0 12px rgba(0,229,255,0.4)`)
- **Delta/trend** no rodapé: ícone `check` 12x12 `--success` + texto 11px/500 `--success`
- Hover: borda muda para `--accent-bg-strong`, scale 1.02, transition 150ms

> Mapeamento real para o backend (RF-05):
> - **Projetos Ativos:** total de projetos com status ≠ `arquivado` em `~/.hermes/webui/projects.json`. Delta: comparado ao 1º dia do mês.
> - **Tarefas em Andamento:** soma de cards na coluna `em_andamento` no Kanban. Delta: comparado a 24h atrás.
> - **Concluídas:** total histórico de cards `concluido`. Delta: comparado a 7 dias atrás.
> - **Agentes Online:** subagentes ativos no runtime (RF-11, BACKLOG futuro). Status: "Todos operacionais" se nenhum tem erro recente.

### 6.4. Coluna direita — Ações Rápidas

Header: "Ações Rápidas" 14px/600 `--text` (margin-top 24px)

Grid 2 colunas de **6 botões empilhados**, gap 8px:

| | |
|---|---|
| 📁 Novo Projeto | 📄 Novo Documento |
| 🧩 Novo Componente | 💻 Abrir Terminal |
| 📊 Gerar Relatório | 🚀 Deploy Projeto |

Cada botão:
- Padding 10px 12px
- Estilo: `--surface-2` com borda `--border`, border-radius 10px
- Ícone 16x16 à esquerda (cor `--muted`) + label 11px/500 `--text` (pode quebrar em 2 linhas)
- Hover: borda `--accent`, fundo `--accent-bg`, ícone vira `--accent`
- Focus: outline `--accent` 2px

**Mapeamento de ação:**
1. **Novo Projeto** → abre modal de criar projeto Kanban (HU-04.x)
2. **Novo Documento** → cria nota no vault Obsidian via skill `obsidian.create_note`
3. **Novo Componente** → abre seletor de scaffolds (futuro — placeholder no MVP)
4. **Abrir Terminal** → atalho para painel `terminal` upstream
5. **Gerar Relatório** → abre composer pré-preenchido com `/report` (placeholder no MVP)
6. **Deploy Projeto** → abre seletor de projeto + skill `deploy.run` (placeholder no MVP)

> Mapeamento HU-05.* em [BACKLOG.md](./BACKLOG.md#ep-05--ações-rápidas-e-integrações-locais).

---

## 7. Página **Projetos** (Command Center)

Página dedicada acessada via sidebar → Projetos. Substitui o que era o "bloco
Kanban" do Dashboard na v1/v2 do spec. A página é uma central de comando para
acompanhar projetos e tarefas operados pelo Neo, com referências a Jira, GitHub,
Obsidian e sessões. Não é um clone de Jira: o MVP é local-first e guarda
`external_ref`, mas não sincroniza com APIs externas na Sprint 5.

### 7.1. Header da página

Bloco de 80px de altura, padding bottom 16px, border-bottom `--border` (margin-bottom 16px).

- **Título** "Projetos" 24px/700 `--strong`
- **Subtítulo** "Gerencie e acompanhe todos os seus projetos" 13px/400 `--muted`
- À direita (alinhado verticalmente com o título):
  - Botão **Filtros** — outline `--border`, padding 8px 12px, ícone `sliders-horizontal` 14x14 + label 12px/500 `--text`. Clique: abre dropdown de filtros (categoria, prioridade, prazo, atribuído a).
  - Segmented control **Kanban / Lista** — outline `--border`, padding 8px 12px. Kanban é default; Lista usa o mesmo dataset em tabela agrupada por status.
  - Botão **+ Novo Projeto** — primário sólido `--accent`, padding 8px 14px, texto `--bg` 12px/600. Clique: abre modal de criar projeto.

### 7.2. Barra de status (pills)

Linha horizontal abaixo do header, gap 16px, fundo `--surface-2`, padding 12px 16px, border-radius 12px, border `--border`.

Cada pill:
- "Total **30**" — label 11px/500 `--muted` + número 13px/700 `--text`
- "Backlog **6**" — bullet slate 8x8 + label `--muted` + número `--text`
- "Em Andamento **8**" — bullet amber 8x8 + label + número
- "Revisão **4**" — bullet azul 8x8 + label + número
- "Concluído **12**" — bullet verde 8x8 + label + número

Pills são **clicáveis** e funcionam como atalho de filtro (clique em "Em Andamento" → mostra só essa coluna em destaque, ou filtra a vista de lista).

### 7.2.1. Filtros

O botão **Filtros** abre dropdown/painel compacto com:

- texto livre (busca por título, ID externo e descrição)
- projeto
- status
- prioridade
- fonte externa (`local`, `jira`, `github`, `obsidian`)
- responsável
- data/prazo

Filtros afetam Kanban e Lista com o mesmo estado.

### 7.3. Kanban — 4 colunas equilibradas

Grid `repeat(4, 1fr)` com gap 16px, margin-top 16px.

**Cada coluna** (`--surface`, border `--border`, border-radius 12px, padding 12px):

- **Header da coluna** (height 36px, border-bottom `--border`, margin-bottom 12px):
  - Bullet circular 8x8 da cor do status à esquerda
  - Nome do estado (13px/600 `--strong`) — não-uppercase no novo mockup
  - À direita: contagem (10px/600 `--muted`) em pill `--surface-2`, padding 2px 8px
- **Lista de cards** (flex column, gap 8px, scroll-y interno se > 6 cards)
- **Footer da coluna**: botão `+ Adicionar tarefa` full-width, fundo transparente, borda dashed `--border`, texto 11px/500 `--muted`. Hover: borda `--accent`, texto `--accent`. Clique: cria card inline na coluna.

**Cores do top-border 2px de cada coluna:**
- Backlog → `#8AA0BD` (slate)
- Em Andamento → `#F0B429` (amber)
- Em Revisão → `#2196F3` (blue)
- Concluído → `#22C55E` (green)

### 7.4. Card de tarefa (Kanban)

Container: padding 12px, fundo `--surface-2`, border `--border`, border-radius 10px. Hover: `transform: translateY(-2px)`, box-shadow `0 4px 12px rgba(0,229,255,0.12)`, borda `--accent-bg-strong`.

Estrutura interna (vertical, gap 8px):

1. **Título** — 13px/600 `--strong` (1 linha truncada com ellipsis se exceder)
2. **Linha de chips** — gap 6px:
   - Chip de **categoria** (Design / Frontend / Backend / Database / Infra / DevOps / Docs / QA / Segurança):
     padding 2px 8px, border-radius 4px, 10px/500, cor de texto e fundo conforme tabela §2.
   - Chip de **prioridade** (Baixa / Média / Alta) ou **status concluído** (✓ Concluído):
     mesmo estilo, cores conforme tabela §2.
   - Chip de **fonte externa** quando houver `external_ref`, por exemplo `Jira KAN-123`, discreto e clicável no detalhe.
3. **Barra de progresso** (somente colunas que não são Concluído):
   - Track: 4px height, fundo `--border`, border-radius 2px
   - Fill: cor da coluna (amber / blue), com percentual à direita 10px/500 `--muted` ("75%")
4. **Para coluna Concluído:** **sem** barra de progresso. A chip de status verde "✓ Concluído" basta.
5. **(Opcional)** Avatares 16x16 dos colaboradores no canto inferior direito quando houver.

**Cards de exemplo (mockup visual; não usar como dados reais persistidos):**

| Coluna | Cards |
|---|---|
| **Backlog** | • Redesign Landing Page (Design / Baixa) • Integração API Pagamentos (Backend / Média) • Documentação do Sistema (Docs / Baixa) |
| **Em Andamento** | • Dashboard Analytics (Frontend / Alta) — 75% • Refatoração de Código (Backend / Média) — 60% • Autenticação 2FA (Segurança / Alta) — 40% |
| **Revisão** | • Componente de UI (Design / Média) — 90% • Correção de Bugs (Backend / Alta) — 80% • Testes Automatizados (QA / Média) — 70% |
| **Concluído** | • Setup do Projeto (Infra / ✓ Concluído) • Modelagem do Banco (Database / ✓ Concluído) • Configuração de CI/CD (DevOps / ✓ Concluído) |

### 7.5. Vista Lista

Tabela densa agrupada por status, aderente ao mockup `neo_projetos_lista.png`.

Colunas:

- checkbox de seleção
- ID local/externo (`PRJ-001`, `KAN-123`, etc.)
- tarefa + chip de categoria/fonte
- prioridade
- responsável
- estado

Grupos: Backlog, Em Andamento, Revisão, Concluído. Cada grupo mostra contagem e
usa bullet da cor do status. A lista respeita os mesmos filtros do Kanban.

### 7.6. Drag-and-drop (RF-08)

- Card arrastável muda cursor para `grab` no hover do título
- Durante drag: card ganha `box-shadow: 0 8px 24px rgba(0,229,255,0.25)`, leve `rotate: 2deg`, opacidade 0.9
- Coluna alvo destaca borda inteira em `--accent` enquanto o card está sobre ela
- Drop persiste via `PATCH /api/project-tasks/{task_id}` com `{ status: "em_andamento" }` (status: `backlog | em_andamento | em_revisao | concluido`)

### 7.7. Estado vazio

Quando não há projetos:
- Ilustração centralizada do orb com mensagem "Nenhum projeto ainda."
- Sub-linha "Crie seu primeiro projeto para começar." 12px `--muted`
- CTA centralizado: botão **+ Novo Projeto** (mesmo estilo da §7.1)

### 7.8. Referências externas

Cada tarefa pode guardar:

- `external_ref`: origem principal (`jira`, `github`, `obsidian`, `local`), chave, URL, status remoto e `synced_at`.
- `refs.github`: issues, PRs, branches ou commits relacionados.
- `refs.obsidian`: notas do vault relacionadas.
- `refs.sessions`: sessões Neo relacionadas.

Na Sprint 5 esses campos são persistidos e exibidos; sincronização automática
fica para o épico futuro de fontes externas.

---

## 7-bis. Página **Agentes** (mapa pixel-art em tempo real)

> **Status:** Sprint 7 / EP-AG — pós-MVP imediato.
> **Caminho técnico:** 🅲 Híbrido — bundle do `pixel-agents-standalone` servido
> estaticamente por `static/agents-app/` + adaptador Python
> `api/agents_activity.py` que traduz `state.db` + SSE Hermes para o protocolo
> `ServerMessage` que o front já consome.
> **Decisão arquitetural completa:** [`BACKLOG.md` § EP-AG](./BACKLOG.md#ep-ag--painel-agentes-pixel-agents-híbrido).
> **Contratos formais (RF/RNF):** [`PRD.md`](./PRD.md) RF-11, RF-AG.1–7,
> RNF-08, RNF-12, RNF-13, RNF-14.

### 7-bis.1. Objetivo

Visualizar **em tempo real** o Neo orquestrador e os subagentes que ele
despacha (MGI / Projetos / Finanças / Terapia / Pessoal) como personagens
pixel-art trabalhando em um escritório virtual. Cada personagem reage ao que
está sendo feito de fato pelo Hermes: lendo arquivo, rodando comando,
delegando para subagente, salvando memória, aguardando permissão, etc.

### 7-bis.2. Layout geral

```
┌────────────┬─────────────────────────────────────────────────────────────────┐
│ SIDEBAR    │ TOPBAR — (mesma do Dashboard)                                   │
│ (240px)    ├─────────────────────────────────────────────────────────────────┤
│ fixed      │  AGENTES                              🟢 streaming   [filtros] │
│            │  Mapa pixel-art em tempo real do Neo e subagentes ativos        │
│            │  ┌───────────────────────────────────────────┬─────────────┐    │
│            │  │  CANVAS PIXEL-ART                          │ HISTÓRICO   │    │
│            │  │  (escritório com Neo + subagentes)         │ últimos 20  │    │
│            │  │  ┌──────────────────────────────────────┐  │ subagentes  │    │
│            │  │  │ Neo (cyan) — "Delegando para MGI"    │  │             │    │
│            │  │  │   ╲                                  │  │ • MGI · 1m  │    │
│            │  │  │    ╲                                 │  │ • Proj · 3m │    │
│            │  │  │     MGI (laranja) — "Lendo arquivo"  │  │ • Fin · 7m  │    │
│            │  │  │                                      │  │   …         │    │
│            │  │  └──────────────────────────────────────┘  │             │    │
│            │  └───────────────────────────────────────────┴─────────────┘    │
└────────────┴─────────────────────────────────────────────────────────────────┘
```

Proporções desktop: grid `1fr 320px` com gap 16px (mesma proporção que o
Dashboard e Finanças). Mobile: histórico colapsa abaixo do canvas.

### 7-bis.3. Header da página

Bloco de 80px de altura, padding bottom 16px, border-bottom `--border`
(margin-bottom 16px).

- **Título** "Agentes" 24px/700 `--strong`
- **Subtítulo** "Mapa pixel-art em tempo real do Neo e subagentes ativos" 13px/400 `--muted`
- À direita (alinhado verticalmente com o título, gap 12px):
  - **Indicador de conexão SSE** — pill compacta:
    - 🟢 `streaming` (`--success` + bullet pulsante 8x8)
    - 🟡 `reconectando` (`--warning` + spinner)
    - ⚫ `offline` (`--muted`)
    - Texto 11px/600 uppercase letter-spacing 0.08em
  - Botão **Filtros** (futuro / opcional) — outline `--border`, padding 8px 12px, ícone `sliders-horizontal` 14x14. Filtra histórico por domínio.

### 7-bis.4. Canvas pixel-art (coluna esquerda)

Container principal: `--surface`, border `--border`, border-radius 12px,
padding 0 (o canvas ocupa 100%). Altura mínima 480px, máxima 70vh.

**Conteúdo:** o bundle do `pixel-agents-standalone` (`static/agents-app/`)
renderiza um Canvas 2D com:

1. **Layout default** do escritório (paredes, piso, móveis básicos do upstream).
   MVP usa apenas o tileset gratuito; **não** comprar o pacote pago de 452
   peças do `donarg.itch.io`.
2. **Personagem central "Neo"** — cor cyan `--accent`, sempre presente quando
   há sessão ativa. Quando idle, fica sentado na mesa principal.
3. **Subagentes** — entram no mapa quando o Neo executa `delegate_task` e
   saem quando o `tool_result` correspondente chega. Cada um tem cor fixa
   por domínio:

   | Domínio | Cor base | CSS variable proposta |
   |---|---|---|
   | MGI | laranja | `--agent-mgi: #F59E0B` |
   | Projetos | roxo | `--agent-projetos: #A78BFA` |
   | Finanças | verde | `--agent-financas: #10B981` |
   | Terapia | rosa | `--agent-terapia: #EC4899` |
   | Pessoal | cinza-claro | `--agent-pessoal: #94A3B8` |

4. **Linha de delegação** — traço fino tênue (`stroke 1px`, `opacity 0.4`,
   cor do subagente) ligando Neo ao subagente enquanto a delegação está
   ativa. Some no `subagentClear`.
5. **Texto de status** sobre a cabeça do personagem — fonte mono 9px, fundo
   `--surface-2` semi-transparente, padding 2px 6px, border-radius 4px.
   Exemplos:
   - `Lendo arquivo X.md`
   - `Rodando: pytest tests/...`
   - `Salvando memória`
   - `Consultando vault`
   - `Aguardando sua resposta`
   - `Delegando para MGI`
6. **Animações** — herdadas do `pixel-agents-standalone` upstream (sprite
   walking, idle, typing, reading). Sem alterações no engine.

### 7-bis.5. Histórico de subagentes (coluna direita, 320px)

Container: `--surface`, border `--border`, border-radius 12px, padding 14px.

- **Header:**
  - Título "Histórico recente" 13px/600 `--text`
  - Link "Ver todos →" 11px/500 `--accent` (futuro — abre painel completo).
- **Lista de até 20 itens**, gap 10px, scroll-y interno se exceder:
  - Bullet 8x8 com a cor do domínio
  - Stack à direita do bullet:
    - Linha 1: nome do domínio 12px/500 `--text` (ex: "MGI", "Projetos")
    - Linha 2: "duração · N tools" 10px/400 `--muted` (ex: "1m 23s · 4 tools")
  - Ícone à direita: ✓ `--success` (sucesso) ou ✗ `--danger` (falha)
  - Hover do item: fundo `--surface-2`, cursor pointer (clica para expandir
    timeline da sessão — HU-AG.11).

### 7-bis.6. Estado vazio

Quando não há sessão ativa do Neo:
- Canvas mostra o escritório vazio com avatar Neo idle sentado.
- Banner discreto sobre o canvas: "Nenhum agente trabalhando agora.
  Comece uma conversa com o Neo no Dashboard." 12px `--muted`.
- CTA centralizado (link, não botão sólido): "Ir para o Dashboard →"
  `--accent`.
- Histórico mostra os últimos 20 subagentes anteriores, mesmo sem atividade
  ao vivo.

### 7-bis.7. Mapa de tools Hermes → status pixel-agents

Tradução implementada em `api/agents_activity.py::_format_tool_status()`,
espelhando `formatToolStatus()` do upstream `pixel-agents-standalone`.

| Tool Hermes | Activity (pixel-agents) | Texto pt-BR sobre o personagem |
|---|---|---|
| `delegate_task` | `typing` (Neo) + `agentCreated` do subagente | `Delegando para <domínio>` |
| `memory` | `typing` | `Salvando memória` |
| `obsidian-mcp` (escrita) | `typing` | `Atualizando vault` |
| `obsidian-mcp` (leitura) | `reading` | `Consultando vault` |
| `web_search` | `reading` | `Buscando na web` |
| `web_fetch` | `reading` | `Lendo página` |
| `execute_code` | `typing` | `Executando código` |
| `terminal_run` / `bash` | `typing` | `Rodando: <cmd curto>` (truncado em 30 chars + `…`) |
| `clarify` | `waiting` | `Aguardando sua resposta` |
| `send_message` | `typing` | `Enviando para <canal>` |
| `read` (arquivo) | `reading` | `Lendo <basename(path)>` |
| `edit` / `write` | `typing` | `Editando <basename(path)>` / `Escrevendo <basename(path)>` |
| Tool não mapeada | `typing` (fallback) | `Usando <tool_name>` |

### 7-bis.8. Comportamento de conexão SSE

- O `EventSource` para `/api/agents/stream` é **aberto somente quando o
  painel Agentes está visível** (RNF-08 — sem SSE em background).
- `mountDashboardAgents()` abre a conexão; `restoreDashboardAgents()` fecha.
- Reconexão automática com backoff exponencial: 1s → 2s → 4s → 8s → 16s
  (cap em 30s).
- Toast em pt-BR após 5 falhas seguidas: "Não foi possível conectar ao
  stream de agentes. Verifique sua conexão." (`--danger-bg`).
- Heartbeat de 30s pelo servidor para manter a conexão viva atrás de
  proxies/timeouts.

### 7-bis.9. Integração com sidebar Neo

- Item **Agentes** já reservado em `static/index.html` (l. 750–762,
  `<div id="mainAgents">`) e em `static/panels.js`
  (`NEO_SHELL_PANELS`, l. 24).
- `mountDashboardAgents()` segue o mesmo padrão de `mountDashboardSettings()`
  e `mountDashboardSkills()` (anchor pattern), mantendo
  `body.dashboard-shell-mode` ativo para preservar sidebar e topbar.
- Quando `HERMES_WEBUI_ENABLE_AGENTS_PANEL=false`, o item da sidebar fica
  oculto e a rota `/api/agents/stream` retorna `404`.

### 7-bis.10. Implementação dos sprites e canvas

Diferente das demais páginas Neo (vanilla JS), o canvas de Agentes **reusa o
front do `pixel-agents-standalone`** (Vite/React/Canvas2D). Isso é exceção
explicitamente permitida pelo PRD RNF-12 porque:

1. O bundle é gerado **fora do runtime** (`npm run build` no fork) e
   versionado em `static/agents-app/`.
2. A neo-webui em produção **continua sem Node, sem `npm install`, sem
   build step**.
3. O fork mantém arquivos originais intactos (`webview-ui/src/neo/*` é
   adição) — RNF-13.

> **Alternativa rejeitada:** reescrever o engine Canvas2D em vanilla JS
> dentro de `static/agents.js`. Custo de ~1500–2000 linhas, perda de
> mergeability com upstream `pablodelucca/pixel-agents`. Ver
> `BACKLOG.md` § EP-AG → tabela de comparação dos 3 caminhos.

### 7-bis.11. Restrições explícitas (fora do escopo da Sprint 7)

- ❌ **Tileset pago** de 452 peças do `donarg.itch.io` ($2). MVP usa apenas
  o layout default do `pixel-agents-standalone`.
- ❌ **Editor de layout in-app** (recurso do upstream que não agrega no caso
  Neo).
- ❌ **Sons** (já vem desligado por padrão no `pixel-agents-standalone`).
- ❌ **Visualização de outros canais** (WhatsApp / Telegram / Cron / API
  como personagens separados). Fica para iteração seguinte; MVP foca em
  sessões do Neo + subagentes.
- ❌ **Customização de personagens** pelo usuário (escolher avatar/cor).
  Cores fixas por domínio no MVP.

---

## 8. Página **Finanças** (controle financeiro)

Página dedicada acessada via sidebar → Finanças. Mostra resumo financeiro
pessoal e empresarial em KPIs, gráfico temporal e listas de orçamentos,
transações recentes e metas.

> **Escopo MVP (Sprint 1–4):** entregar **shell visual** completo (mesmo
> layout, mesmos cards, dataset de demonstração). Backend financeiro real
> (sincronização com bancos / OFX / planilhas / FinanPy) é **pós-MVP**.
> Decisão registrada em §13.

### 8.1. Header da página

Bloco de 80px de altura, padding bottom 16px, border-bottom `--border`
(margin-bottom 16px).

- **Título** "Finanças" 24px/700 `--strong`
- **Subtítulo** "Controle financeiro pessoal e empresarial" 13px/400 `--muted`
- À direita:
  - Botão **Terminal SSH** — outline `--accent`, padding 8px 12px, ícone `terminal` 14x14 + label 12px/600 `--accent`. (Replica o atalho da topbar para acesso rápido a partir desta página de operação.)
  - Botão **+ Nova Finança** — primário sólido `--accent`, padding 8px 14px, texto `--bg` 12px/600, ícone `plus` 14x14. Clique: abre modal de nova transação (Receita / Despesa / Investimento).

### 8.2. KPI cards — linha de 4 cards

Grid `repeat(4, 1fr)` com gap 16px, margin-bottom 16px. Cada card:

- Padding 16px, fundo `--surface`, border `--border`, border-radius 12px
- Layout horizontal: ícone circular 40x40 à esquerda + stack à direita
- **Stack à direita:**
  - Label 11px/500 uppercase letter-spacing 0.08em `--muted`
  - Valor 22px/700 `--strong` (formato monetário pt-BR: "R$ 1.564,99")
- **Hover:** borda `--accent-bg-strong`, scale 1.02, transition 150ms

**KPIs (esquerda → direita):**

| # | Label | Valor exemplo | Ícone | Fundo do ícone |
|---|---|---|---|---|
| 1 | **Receitas** | R$ 1.564,99 | `trending-up` 18x18 `--success` | `--success-bg` |
| 2 | **Despesas** | R$ 0,00 | `trending-down` 18x18 `--danger` | `--danger-bg` |
| 3 | **Saldo Líquido** | R$ 0,00 | `wallet` 18x18 `--warning` | `--warning-bg` |
| 4 | **Investimentos** | R$ 0,00 | `pie-chart` 18x18 `--violet` | `--violet-bg` |

> **Nota de cor:** o valor do KPI **Saldo Líquido** colore conforme sinal:
> positivo `--success`, negativo `--danger`, zero `--strong`.

### 8.3. Grid principal — duas colunas

Abaixo da linha de KPIs: grid `1fr 320px` com gap 16px.

#### 8.3.1. Coluna esquerda — Resumo Financeiro + Gastos por Categoria

**Card "Resumo Financeiro"** — fundo `--surface`, border `--border`, border-radius 12px, padding 16px:

- **Header da seção:**
  - Título "Resumo Financeiro" 14px/600 `--text`
  - À direita (gap 8px):
    - **Toggle de séries** — pills clicáveis lado a lado:
      - "Em Receitas" (ativo: fundo `--accent-bg`, texto `--accent`; inativo: texto `--muted`)
      - "Em Despesas" (ativo: fundo `--danger-bg`, texto `--danger`; inativo: texto `--muted`)
      - As duas podem estar ativas simultaneamente (tipo checkbox visual). Padrão: ambas ativas.
    - **Dropdown "Últimos 5 meses ▾"** — outline `--border`, padding 6px 10px, 11px/500 `--muted`. Opções: Últimos 30 dias / Últimos 3 meses / Últimos 5 meses / Últimos 12 meses / Ano atual / Personalizado.

- **Gráfico de linha** — área de ~300px de altura:
  - Renderizado em **SVG inline** (sem libs externas — manter constraint "sem build").
  - X-axis: meses (ex: Dez, Jan, Fev, Mar, Abr, Mai). Labels 10px/500 `--muted`, ticks de 12px abaixo do eixo.
  - Y-axis: valores em R$, escala linear automática com 5–7 grid lines horizontais. Labels 10px/500 `--muted` à esquerda. Grid lines: stroke `--border`, dasharray 4,4.
  - **Série Receitas** (quando ativa): linha cyan `--accent`, stroke 2px, com pontos 4px nos vértices. Área abaixo: fill `rgba(0,229,255,0.15)`.
  - **Série Despesas** (quando ativa): linha vermelha `--danger`, stroke 2px, com pontos 4px nos vértices. Área abaixo: fill `rgba(239,83,80,0.15)`.
  - **Tooltip** ao hover sobre vértice: card flutuante `--surface-2` com sombra, mostrando mês + valor de cada série ativa naquele ponto.
  - **Animação de entrada:** `stroke-dashoffset` de cada linha de 100% → 0% em 800ms ease-out.

**Card "Gastos por Categoria"** — segundo bloco, fundo `--surface`, border `--border`, border-radius 12px, padding 16px, margin-top 16px:

- **Header:**
  - Título "Gastos por Categoria" 13px/600 `--text`
  - Tab inline "Datas" 11px/500 `--text` ativo (futuras tabs: "Categorias", "Métodos") — estilo: borda inferior `--accent` 2px no ativo
- **Conteúdo:**
  - **Visualização principal:** donut chart (SVG inline, raio 80px, gap 4px entre fatias) à esquerda + legenda à direita
  - **Legenda:** cada item com bullet 8x8 da cor da categoria + nome 12px/500 `--text` + valor 12px/600 `--text`
  - **Estado vazio** (quando sem dados): ícone `pie-chart` 48x48 `--muted` centralizado + texto "Sem gastos no período" 12px `--muted` + valor `−R$ 0,00` 22px/700 `--muted` em destaque

#### 8.3.2. Coluna direita — Orçamentos / Transações Recentes / Metas

Largura fixa 320px. Stack vertical com gap 16px.

**8.3.2.1. Card "Orçamentos"**

- Container: fundo `--surface`, border `--border`, border-radius 12px, padding 14px
- Header:
  - Título "Orçamentos" 13px/600 `--text`
  - Link "Ver todos →" 11px/500 `--accent`
- Lista de até 4 orçamentos, gap 12px. Cada item:
  - Linha 1: nome da categoria 12px/500 `--text` à esquerda + valor "R$ 200/500" 11px/500 `--muted` à direita
  - Linha 2: barra de progresso 4px (track `--border`, fill cor da categoria; vira `--danger` quando ≥ 100%)
- Estado vazio: "Nenhum orçamento configurado." 12px `--muted` centralizado + link "Criar orçamento" `--accent`

**8.3.2.2. Card "Transações Recentes"**

- Container: fundo `--surface`, border `--border`, border-radius 12px, padding 14px
- Header:
  - Título "Transações Recentes" 13px/600 `--text`
  - Link "Ver todas →" 11px/500 `--accent`
- Lista de até 5 transações, gap 10px. Cada item:
  - Ícone circular 32x32 da categoria (cor de fundo conforme tabela §2 com opacidade 0.15, ícone `--accent` ou cor da categoria)
  - Stack à direita do ícone:
    - Linha 1: nome 12px/500 `--text` (ex: "Curso Cris...", "Combustível", "Almoço", "Barbearia")
    - Linha 2: "data · método" 10px/400 `--muted` (ex: "Hoje · Pix", "Ontem · Cartão")
  - Valor à direita 12px/600:
    - Receita: `--success` com prefixo `+` (ex: "+ R$ 1.500,00")
    - Despesa: `--danger` com prefixo `−` (ex: "− R$ 80,00")
- Hover do item: fundo `--surface-2`, cursor pointer (clica para abrir detalhes da transação).

**Itens de exemplo (mockup):** Curso Cristão, Combustível, Almoço, Barbearia.

**8.3.2.3. Card "Metas Financeiras"**

- Container: fundo `--surface`, border `--border`, border-radius 12px, padding 14px
- Header:
  - Título "Metas Financeiras" 13px/600 `--text`
  - Link "+ Nova meta" 11px/500 `--accent`
- Lista de até 3 metas, gap 12px. Cada item:
  - Linha 1: nome da meta 12px/500 `--text` + ícone `target` 12x12 `--accent`
  - Linha 2: valor atual / objetivo "R$ 0,00 / R$ 5.000,00" 10px/500 `--muted` + percentual "0%" `--accent` à direita
  - Linha 3: barra de progresso 4px (track `--border`, fill `--accent`)
- Estado vazio: "Nenhuma meta ainda." 12px `--muted` centralizado + link "Criar primeira meta" `--accent`

### 8.4. Modal "+ Nova Finança"

Disparado pelo botão da §8.1. Modal centralizado, max-width 480px, fundo
`--surface`, border `--border`, border-radius 14px, padding 20px.

- Header: "Nova Finança" 16px/600 + botão `x` no canto direito
- Tabs no topo: **Receita** / **Despesa** / **Investimento** (default Receita)
- Campos:
  - Descrição (input texto) — required
  - Valor (input numérico com máscara R$) — required
  - Data (input date, default hoje) — required
  - Categoria (select com cores conforme tabela §2)
  - Método (select: Pix / Cartão / Dinheiro / Transferência / Boleto)
  - Recorrência (select: Única / Mensal / Anual)
  - Anotações (textarea opcional)
- Footer: botão **Cancelar** (outline) + botão **Salvar** (primário `--accent`)

### 8.5. Estado vazio (sem dados financeiros)

Quando não há transações ainda:
- Header e KPIs continuam visíveis (com R$ 0,00)
- Card "Resumo Financeiro" mostra ilustração centralizada do orb (mono) + texto "Sem dados financeiros ainda." 14px/500 `--muted` + sub "Adicione sua primeira transação para começar." 12px `--muted`
- CTA centralizado: botão **+ Nova Finança** (mesmo estilo da §8.1)
- Cards da coluna direita mostram seus próprios estados vazios (definidos em §8.3.2.*)

### 8.6. Implementação dos gráficos (SVG vanilla)

Dado o constraint "sem build / sem libs", os gráficos (linha + donut) são
implementados em **SVG inline** com helpers em um novo módulo:

- Arquivo sugerido: `static/finance.js`
- Funções:
  - `renderLineChart(svgEl, series, options)` — series = `[{ label, color, data: [{x, y}] }]`
  - `renderDonutChart(svgEl, slices, options)` — slices = `[{ label, value, color }]`
- Sem animações JS pesadas — só CSS animations em `stroke-dashoffset` e transitions de hover.

> **Alternativa rejeitada:** Chart.js / D3 / ApexCharts. Todas exigem bundling
> ou aumentam payload do `static/`. SVG vanilla é suficiente para os 2
> gráficos desta página e mantém a stack.

---

## 9. Avatar humanoide NEO (hero) — especificação para HU-01.2

- **Vetor (SVG):**
  - Silhueta humanoide vista de frente (cabeça + ombros + parte do torso)
  - Estilo: wireframe holográfico — linhas finas cyan (`stroke: var(--accent)`, `stroke-width: 1`) descrevendo polígonos triangulares na superfície
  - Camadas:
    1. Silhueta de fundo preenchida com gradient radial cyan→azul-escuro (`#00B8D4` → `#04101A`)
    2. Malha de polígonos triangulares (15–25 polígonos) sobre a silhueta
    3. Pontos brilhantes (8-12) nos vértices da malha — `fill: var(--accent)`, `filter: drop-shadow(0 0 3px var(--accent))`
    4. Linhas de "scan" horizontais sutis cruzando o avatar (efeito hologram)
    5. **Text overlay**: "NEO" centralizado em 14px/700 `--strong` glow
- **Cores:** usar `currentColor` para os traços quando possível, para herdar do skin.
- **Acessibilidade:** `aria-label="Avatar do agente Neo"`, `<title>` no SVG.
- **Variantes:**
  - `static/brand/neo-avatar.svg` (color, hero do Dashboard, 240x220)
  - `static/brand/neo-avatar-mono.svg` (mono para favicon e contextos pequenos)
  - `static/brand/neo-mark.svg` (mark compacto 40x40 para sidebar header e topbar)
- **Animação CSS opcional** (hero apenas):
  - `@keyframes hover-float { 0%,100%{transform:translateY(0)} 50%{transform:translateY(-4px)} }` 4s ease-in-out infinite
  - `@keyframes pulse-glow { 0%,100%{filter:drop-shadow(0 0 6px var(--accent))} 50%{filter:drop-shadow(0 0 14px var(--accent))} }` 3s

> Asset 3D / Canvas / WebGL fica como **opt-in pós-MVP** — em Sprint 1 a
> entrega é SVG estático com animação CSS, conforme PRD §5 e RNF-03.

---

## 10. Iconografia geral

Usar o mesmo conjunto SVG inline já presente no upstream Hermes (Lucide-style,
stroke 1.5–2). Quando precisar de ícone novo, pegar do Lucide
(`https://lucide.dev`) e copiar o path como SVG inline em `static/icons.js`.
Manter `stroke-linecap=round`, `stroke-linejoin=round`.

Ícones específicos usados pelos mockups:

| Onde | Ícones |
|---|---|
| Sidebar nav | `layout-grid`, `folder`, `check-square`, `user`, `dollar-sign`, `cpu` (ou `bot`), `zap`, `settings-2`, `settings` |
| Sidebar status card | `zap` (botão Conversar) |
| Topbar | `terminal`, `search`, `bell`, `help-circle`, `chevron-down` |
| Chat | `paperclip`, `more-horizontal`, `smile`, `send`, `file-text`, `check` |
| KPI cards Dashboard | `check` (delta), `arrow-up`, `arrow-down` (deltas alternativos) |
| KPI cards Finanças | `trending-up`, `trending-down`, `wallet`, `pie-chart` |
| Ações Rápidas | `folder-plus`, `file-plus`, `box`, `terminal`, `bar-chart-2`, `rocket` |
| Projetos header | `sliders-horizontal`, `chevron-down`, `plus` |
| Card Kanban | `more-vertical` (menu), `users` (avatares), `clock` (prazo) |
| Finanças header | `terminal`, `plus` |
| Finanças coluna direita | `target` (metas), `arrow-up-right` (receita item), `arrow-down-right` (despesa item) |

---

## 11. Animações e transições

- Padrão global: `transition: 150ms ease-out`
- Hover em cards: `transform: translateY(-2px)`, `box-shadow: 0 4px 12px rgba(0,229,255,0.12)`
- Pulsação dos bullets de status (topbar, sidebar status card): `@keyframes pulse { 0%,100%{opacity:.6} 50%{opacity:1} }` 2s
- Glow do avatar hero: `@keyframes pulse-glow { ... }` 3s (ver §9)
- Hover-float do avatar hero: `@keyframes hover-float { ... }` 4s
- Drag no Kanban: card ganha `box-shadow: 0 8px 24px rgba(0,229,255,0.25)` e leve `rotate: 2deg`
- Transição entre painéis: fade-in 150ms quando `switchPanel` troca o conteúdo
- Mensagens novas no chat: `slide-up 200ms ease-out` na entrada da bolha
- Linhas do gráfico Finanças: `stroke-dashoffset 100% → 0%` em 800ms ease-out na entrada
- Donut chart: rotação de 0 a valor final em 600ms ease-out

---

## 12. Mapeamento mockup → painéis Hermes existentes

| Item do mockup | Mapeia para | Status |
|---|---|---|
| Sidebar **Dashboard** | **NOVO** painel `dashboard` | EP-03 |
| Sidebar **Projetos** | **NOVO** painel `projects` (Kanban full-page) | EP-04 |
| Sidebar **Tarefas** | Consolida `tasks` (cron) + `todos` upstream em uma vista unificada | HU-03.7 |
| Sidebar **Pessoal** | **NOVO** painel mínimo (perfil + preferências), reusa `settings` parcialmente | HU-03.10 |
| Sidebar **Finanças** | **NOVO** painel `finance` com KPIs + gráfico + listas | EP-06 (NOVO) |
| Sidebar **Agentes** | placeholder no MVP; **Sprint 7** entrega pixel-agents híbrido (EP-AG) | Sprint 7 · EP-AG |
| Sidebar **Skills** | Painel `skills` upstream | já existe |
| Sidebar **Automação** | Painel `tasks` (cron) na visão de automações | já existe |
| Sidebar **Configurações** | Painel `settings` upstream | já existe |
| Sidebar — status card "NEO ONLINE" + Conversar agora | **NOVO** componente | HU-03.8 |
| Sidebar — Recursos VPS | **NOVO** componente + endpoint `/api/health/vps` | HU-03.11 |
| Sidebar — Documentação / Suporte | links externos (configuráveis em Settings) | trivial |
| Topbar — VPS Status / Uptime / Região / Versão | **NOVO** topbar contextual + endpoint `/api/health/system` | HU-03.6 |
| Topbar — Terminal SSH | atalho para painel `terminal` upstream | trivial |
| Topbar — busca / notif / help | reusa command palette + `notifications` upstream | reusa |
| Topbar — Admin dropdown | **NOVO** menu de perfil | HU-03.9 |
| Hero (avatar humanoide + STATUS: OPERACIONAL) | **NOVO** asset + componente | HU-01.2 + HU-03.3 |
| Bem-vindo + 4 KPIs com deltas | **NOVO** componente Dashboard | HU-03.3, HU-03.4 |
| Ações Rápidas (grid 2x3, 6 botões) | **NOVO** lista de botões | EP-05 |
| Chat central (Dashboard) | Mesmo SSE da sessão ativa, embutido no grid | EP-03 |
| Página Projetos — header | **NOVO** página dedicada | HU-04.1 |
| Página Projetos — Kanban 4 colunas | **NOVO** com `backlog` adicionado | HU-04.2 |
| Página Projetos — modal de criação | **NOVO** modal + persistência inicial | HU-04.3 |
| Página Projetos — criar tarefa com refs | **NOVO** tarefa local-first com `external_ref` opcional | HU-04.4 |
| Página Projetos — drag-and-drop entre colunas | persistir via `PATCH /api/project-tasks/{task_id}` | HU-04.5 |
| Página Projetos — card com chips e progresso | **NOVO** componente | HU-04.6 |
| Página Projetos — status pills | contadores clicáveis Total / Backlog / Em Andamento / Revisão / Concluído | HU-04.7 |
| Página Projetos — Lista e filtros | **NOVO** view lista agrupada por status + filtros operacionais | HU-04.8 |
| Página Finanças — header + KPI cards | **NOVO** página dedicada | HU-06.1, HU-06.2 |
| Página Finanças — gráfico de linha (SVG vanilla) | **NOVO** módulo `static/finance.js` | HU-06.3 |
| Página Finanças — Gastos por Categoria (donut) | **NOVO** | HU-06.4 |
| Página Finanças — Orçamentos / Transações / Metas | **NOVO** componentes da coluna direita | HU-06.5 |
| Página Finanças — modal + Nova Finança | **NOVO** modal com tabs Receita/Despesa/Investimento | HU-06.6 |
| Página Finanças — persistência | `~/.hermes/webui/finance.json` (Neo-only); endpoints `GET/POST /api/finance/*` | HU-06.7 |
| Página Agentes — canvas pixel-art | bundle do `pixel-agents-standalone` em `static/agents-app/` (caminho 🅲 Híbrido) | HU-AG.1, HU-AG.2 |
| Página Agentes — stream de eventos | adaptador `api/agents_activity.py` + endpoint SSE `GET /api/agents/stream` | HU-AG.3, HU-AG.4 |
| Página Agentes — mount/restore embutido | `mountDashboardAgents()` / `restoreDashboardAgents()` em `static/dashboard.js` | HU-AG.5 |
| Página Agentes — cores por domínio + textos pt-BR | mapeamento de tools Hermes → status visual (§7-bis.7) | HU-AG.6, HU-AG.7 |
| Página Agentes — histórico recente | `GET /api/agents/recent?limit=20` lendo `state.db` | HU-AG.10 |
| Página Agentes — feature flag | env `HERMES_WEBUI_ENABLE_AGENTS_PANEL` lido em `api/config.py` | HU-AG.12 |

---

## 13. O que ainda precisa ser definido (perguntar ao Júnior)

- [ ] **Tarefas vs Automação na sidebar:** ambos existem como itens. "Tarefas" agrega todos + cron na vista de produtividade; "Automação" mostra cron na vista de schedule. Confirmar separação ou consolidar.
- [ ] **Pessoal:** o que vai dentro? Sugestão: perfil do usuário (avatar, nome, role), preferências (tema, locale), atalhos de "minhas notas pessoais" (vault Obsidian filtrado por tag).
- [ ] **Recursos VPS na sidebar:** endpoint `/api/health/vps` precisa ler do host (não do container do Hermes). Decisão: ler de `/proc/stat` + `/proc/meminfo` + `psutil`? Cachear quanto tempo? Sugestão: 30s.
- [ ] **VPS Status / Uptime na topbar:** diferente de Recursos VPS — é metadata estática (hostname, região, versão deployed). Pode vir de variáveis de ambiente em deploy + uptime via `/proc/uptime`.
- [ ] **KPI "Concluídas" — janela de tempo:** total histórico (156) ou últimos 30 dias? O delta "+18 esta semana" sugere comparação rolling de 7 dias.
- [ ] **Ações Rápidas — placeholders:** Novo Componente / Gerar Relatório / Deploy Projeto não têm backend definido. Decidir se Sprint 1 entrega (a) botão funcional para os 3 que já existem (Projeto, Documento, Terminal) e (b) `disabled + tooltip "Em breve"` para os outros 3, **OU** mostra todos com handler que abre modal genérico.
- [ ] **Chips de categoria do card Kanban:** lista de categorias é fechada (Design/Frontend/Backend/Database/Infra/DevOps/Docs/QA/Segurança) ou livre (usuário cria)? Sugestão: começar fechada com sementes e permitir custom em pós-MVP.
- [ ] **Card de projeto vs Card de tarefa:** o mockup chama de "Projetos" o quadro inteiro mas os cards parecem **tarefas** ("Redesign Landing Page", "Correção de Bugs"). Definição arquitetural: o que é "projeto" e o que é "tarefa"? Sugestão:
  - **Projeto** = container (Brabus, FinanPy, Obreiro Virtual…)
  - **Tarefa** = item de Kanban dentro de um projeto
  - A página `Projetos` sem filtro mostra **todas as tarefas de todos os projetos**; com filtro de projeto, mostra só dele.
- [ ] **Avatar humanoide:** silhueta neutra (atual) ou personalizar com traços do Júnior? Sugestão: começar neutra; permitir upload em Settings (futuro).
- [ ] **Finanças — fonte de dados:** no MVP é manual (modal "+ Nova Finança") com persistência local em `finance.json`? Ou tem alguma integração planejada (OFX import / planilha sincronizada / bancos via Pluggy/Belvo)? Sugestão MVP: 100% manual; pós-MVP integrações.
- [ ] **Finanças — separação pessoal × empresarial:** o subtítulo cita ambos. Separar com tab/toggle no header (Pessoal / Empresarial / Tudo), ou só uma tag por transação? Sugestão: tag/escopo na própria transação + filtro no header.
- [ ] **Finanças — moedas:** suportar mais de uma (BRL/USD)? Sugestão MVP: só BRL com formatação pt-BR; multi-moeda fica para depois.
- [ ] **Finanças — categorias de transação:** lista fechada ou livre? Sugestão: começar fechada com sementes (Alimentação, Transporte, Educação, Saúde, Lazer, Moradia, Salário, Investimento, Outros) e deixar custom para pós-MVP.

---

## 14. Como atualizar este spec

1. Toda mudança de mockup → atualizar este arquivo **antes** de mexer no código.
2. Toda mudança de paleta → refletir em `style.css` no bloco `[data-skin="neo"]` e atualizar §2 deste doc.
3. Versionar mockups: salvar variações em `docs/neo/mockups/<YYYY-MM-DD>-<descricao>.png` (não comitar PNGs grandes em master se possível — usar Git LFS quando crescer).
4. Em PR, anexar screenshot da implementação ao lado do trecho do spec correspondente.
5. Bumpar a versão no cabeçalho deste arquivo (`Versão: X.Y — YYYY-MM-DD`) sempre que o layout/estrutura mudar.

---

## 15. Histórico de versões

| Versão | Data | Mudanças principais |
|---|---|---|
| 3.3 | 2026-05-09 | Adiciona §7-bis **Página Agentes (pixel-agents híbrido)** — mapa pixel-art em tempo real do Neo orquestrador + subagentes (MGI / Projetos / Finanças / Terapia / Pessoal). Caminho 🅲 Híbrido: bundle do `pixel-agents-standalone` servido por `static/agents-app/` + adaptador Python `api/agents_activity.py` que traduz Hermes → `ServerMessage` via SSE. Atualiza referências "placeholder Em breve" da §4.2, §6.3 e §12 para apontar para EP-AG / Sprint 7. |
| 3.2 | 2026-05-01 | Registra que o chat do Dashboard deve preservar o composer/toolstrip completo do Hermes WebUI, incluindo anexos, microfone/voz, profile, workspace, seletor de modelo, reasoning/effort e handlers atuais, com skin visual Neo. |
| 3.1 | 2026-05-01 | Adiciona página **Finanças** (§8) com header + 4 KPI cards (Receitas/Despesas/Saldo Líquido/Investimentos) + gráfico de linha (SVG vanilla) + Gastos por Categoria (donut) + coluna direita com Orçamentos / Transações Recentes / Metas Financeiras + modal "+ Nova Finança". Renumera §8–§14 antigas para §9–§15. Adiciona EP-06 e HU-06.* no mapeamento. Inclui pendências de Finanças no §13. |
| 3.0 | 2026-05-01 | Consolida Dashboard refinado + nova página **Projetos**. Sidebar com 9 itens + status card + Recursos VPS + footer. Topbar com VPS Status/Uptime/Região/Versão + Terminal SSH + ícones + Admin. KPIs com deltas. Ações Rápidas em grid 2x3. Hero ganha "STATUS: OPERACIONAL". Kanban migra para página dedicada Projetos com 4 colunas, status pills e cards com chips/progresso. |
| 2.0 | 2026-05-01 | Reescrito após 1ª nova fonte da verdade. Chat passa a ser central; coluna direita acumula Hero + KPIs + Ações; Kanban ganha 4 colunas (Backlog adicionado); hero passa de orb-esfera para avatar humanoide; Inter como fonte primária. |
| 1.0 | 2026-04-30 | Versão inicial baseada no mockup `static/neo_agent_web_ui.png` original. |
