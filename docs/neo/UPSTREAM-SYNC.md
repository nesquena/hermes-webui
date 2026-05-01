# Upstream Sync — Estratégia de Manutenção

> Como manter este fork **mergeable** com o upstream `nesquena/hermes-webui`,
> aproveitando releases novas (correções, features, testes) com o mínimo de
> conflito, sem perder a personalização Neo.
>
> **Princípio raiz:** mudanças do Neo são **aditivas e isoláveis**. Quando
> precisar tocar arquivo upstream, fazer cirurgicamente, com comentário
> `# NEO:` no ponto da edição.

---

## 1. Filosofia

Existem dois tipos de arquivos no repo:

- **Core (upstream):** mudam frequentemente no upstream, são caminho-quente
  de merges. Tocar com cautela.
- **Neo-only:** criados especificamente para esta personalização. Upstream
  não toca, então merges são limpos.

Quanto mais novidade do Neo morar em arquivos Neo-only, mais barato fica
acompanhar o upstream. Sempre que uma feature pode ser feita via novo arquivo
em vez de editar um existente, **escolha o arquivo novo**.

---

## 2. Inventário de arquivos

### 🔴 Core upstream — evitar editar

| Arquivo | Por quê é crítico | Política |
|---|---|---|
| `server.py` | Roteador HTTP principal | **Não tocar.** Toda rota nova vai para `api/<modulo>.py` e é registrada via `api/routes.py` (este sim aceita aditivos pequenos). |
| `api/routes.py` | Registro central de rotas | Aditivo apenas: registrar rotas novas no fim do arquivo, dentro de bloco com comentário `# NEO: rotas Neo`. |
| `api/streaming.py`, `api/models.py`, `api/auth.py`, `api/config.py` | Engine | **Cirurgia mínima.** `config.py` aceita campos novos no fim do dict (env vars Neo: `HERMES_WEBUI_DEFAULT_SKIN`, `HERMES_WEBUI_LOCALE`). |
| `static/index.html` | DOM raiz | Aditivo: novos `<button>` no rail e novos `<div class="panel-view">` no fim. **Não reescrever.** Trocar logo via JS no boot, não inline. |
| `static/style.css` | Folha global | Aditivo: bloco `:root[data-skin="neo"]` no fim. **Não editar variáveis upstream existentes.** |
| `static/i18n.js` | Locale | Aditivo: bloco `pt-BR` espelhando o `en`. Sem editar `en` salvo correção pontual (proporá PR upstream). |
| `static/boot.js`, `static/ui.js`, `static/messages.js`, `static/sessions.js`, `static/panels.js`, `static/commands.js`, `static/workspace.js`, `static/terminal.js`, `static/onboarding.js` | Núcleo do frontend | **Mínima invasão.** Hooks: `switchPanel()` aceita `dashboard`/`projects` se houver `loadDashboard()`/`loadProjects()` definidos globalmente — feature detection. |
| `tests/` | Testes upstream | **Não editar testes existentes.** Testes Neo em arquivos novos `tests/test_neo_*.py`. |

### 🟢 Neo-only — livres para criar/editar

| Caminho | Função |
|---|---|
| `docs/neo/**` | Toda esta documentação (PRD, BACKLOG, TASKS, UPSTREAM-SYNC, evidências) |
| `static/dashboard.js` | Painel Dashboard (HU-03.*) |
| `static/kanban.js` | Painel Projetos (HU-04.*) |
| `static/brand/neo-orb.svg`, `static/brand/*.png` | Assets visuais Neo |
| `api/dashboard.py` | Backend agregador do Dashboard (HU-03.4) |
| `api/projects.py` | CRUD de projetos (HU-04.*) |
| `api/agents.py` (futuro) | Endpoint para painel Agentes |
| `tests/test_neo_sprint{N}.py` | Testes Neo por sprint |
| `tests/test_locale_parity_pt_br.py` | Paridade pt-BR ↔ en |
| `scripts/lint_neo_branding.sh` | Lint de chrome ("Hermes" residual) |

### 🟡 Edição cirúrgica permitida (com `# NEO:` comment)

| Arquivo | O que pode mudar |
|---|---|
| `api/routes.py` | Registrar rotas novas no fim. |
| `api/config.py` | Adicionar leitura de envs Neo no `_resolve_settings()`. |
| `static/index.html` | Adicionar `<button data-panel="dashboard">` no rail/sidebar; adicionar `<div id="panelDashboard">`/`#panelProjects` no fim do `<main>`. Não alterar HTML existente. |
| `static/style.css` | Adicionar bloco `[data-skin="neo"]` no fim. |
| `static/i18n.js` | Adicionar bloco `pt-BR` espelhando `en`; e chaves novas (`dashboard_*`, `projects_*`) nos demais locales (placeholder em `en` se for o caso). |
| `static/panels.js` | Adicionar `loadDashboard`/`loadProjects` apenas se necessário; preferível em arquivos próprios. |
| `static/boot.js` | Hook minimal: trocar logo SVG inline por `<img>` quando `_botName` ≠ `Hermes`. |
| `static/commands.js` | Adicionar `neo` à lista de skins do comando `/skin`. |
| `manifest.json` | Trocar `name`/`short_name`. |
| `static/favicon*` | Substituir pelos do Neo. |

---

## 3. Convenção de comentário `# NEO:`

Toda edição em arquivo upstream deve estar **explicitamente marcada** com
um comentário no estilo da linguagem:

```python
# NEO: leitura de HERMES_WEBUI_DEFAULT_SKIN para skin default no boot
"default_skin": os.getenv("HERMES_WEBUI_DEFAULT_SKIN", "default"),
```

```javascript
// NEO: hook para painel Dashboard
if(panel==='dashboard' && typeof loadDashboard==='function') loadDashboard();
```

```html
<!-- NEO: botão do painel Dashboard -->
<button class="rail-btn nav-tab" data-panel="dashboard" ...>
```

```css
/* NEO: skin "neo" — paleta cyan/azul-neon do mockup neo_agent_web_ui.png */
:root[data-skin="neo"] { ... }
```

**Por quê?**
1. Permite encontrar todas as edições Neo via `grep -rn "NEO:" .`.
2. Em conflitos de merge, é trivial reaplicar uma a uma.
3. Se quisermos contribuir a feature de volta para upstream, sabemos exatamente o delta.

---

## 4. Workflow de merge upstream

### 4.1. Antes de cada sprint

```bash
# garantir que o upstream está como remote
git remote -v | grep upstream || git remote add upstream https://github.com/nesquena/hermes-webui.git

# trazer mudanças sem mergear
git fetch upstream

# ver o que mudou desde o último merge
git log --oneline HEAD..upstream/master | head -50

# se houver mudanças relevantes, abrir branch dedicada para o merge
git checkout -b chore/upstream-sync-$(date +%Y%m%d)
git merge upstream/master
```

### 4.2. Resolvendo conflitos

Conflitos esperados são quase sempre em:
- `static/index.html` (rail/sidebar) — escolher upstream + reaplicar `<!-- NEO: ... -->` botões.
- `static/style.css` (final do arquivo) — manter bloco upstream + bloco `[data-skin="neo"]` no fim.
- `static/i18n.js` (locale `en`) — manter chaves upstream + bloco `pt-BR` ao lado.
- `api/config.py` — manter dict upstream + linhas `# NEO:` integradas.

**Regra de ouro:** nunca apagar conteúdo upstream em conflito. O Neo é
**aditivo**.

### 4.3. Após resolver

```bash
# rodar suíte completa (upstream + neo)
pytest tests/ -q

# rodar lint Neo (branding + paridade pt-BR)
bash scripts/lint_neo_branding.sh
pytest tests/test_locale_parity_pt_br.py -v

# se tudo verde, abrir PR contra neo/main
gh pr create --base neo/main --title "chore: upstream sync $(date +%Y-%m-%d)" \
  --body "Sync com upstream. Sem mudanças funcionais Neo."
```

### 4.4. Após cada release upstream com mudança grande

Atualizar este documento se uma seção do upstream mudou estrutura
significativamente (ex: split de arquivo, nova convenção). Versão deste doc
sobe junto.

---

## 5. Releases e versionamento

- **Tag de sincronização:** `upstream-sync-YYYYMMDD` após cada merge limpo.
- **Tag de release Neo:** `neo-webui-v0.1`, `v0.2`, ... ao fim de cada sprint
  consolidado.
- `docs/neo/CHANGELOG.md` (a ser criado na Sprint 4) registra release Neo
  com referência ao commit upstream em que foi sincronizado.

---

## 6. O que NÃO contribuir de volta para o upstream

| Tipo de mudança | Por quê |
|---|---|
| Branding, logo, paleta "neo" | Customização pessoal — fora do escopo de uma WebUI genérica |
| Locale `pt-BR` parcial | OK contribuir **se** estivermos cobrindo 100% das chaves do `en` e o teste de paridade passar (proposta de PR documentada em [PRD.md §11](./PRD.md#11-referências)) |
| Campo `Project` específico do Neo | Genérico o suficiente: pode virar PR upstream se a comunidade quiser |
| Painel Agentes especulativo | Só após PoC e estabilização — talvez vire feature upstream se útil |

Coisas como **correções de bugs genéricos** ou **refatoração que quebra menos
o merge** são candidatas naturais a PR upstream — sempre revisar `nesquena/hermes-webui` `CONTRIBUTING.md` e abrir issue antes do PR.

---

## 7. Lista de envs Neo introduzidas

| Env | Default | Função |
|---|---|---|
| `HERMES_WEBUI_BOT_NAME` | `Hermes` | Já existe upstream — Neo apenas configura como `Neo` |
| `HERMES_WEBUI_DEFAULT_SKIN` | `default` | **Neo:** skin default quando localStorage vazio |
| `HERMES_WEBUI_LOCALE` | (vazio) | **Neo:** locale default quando localStorage vazio |
| `HERMES_WEBUI_DEFAULT_PANEL` | `chat` | **Neo:** painel inicial (suporte a `dashboard`) |

Todas as envs Neo lidas em `api/config.py` com fallback no comportamento
upstream — desativar a env restaura o comportamento original.

---

## 8. Quando algo der errado

- Conflito gigante em arquivo upstream → abrir issue interna em
  `docs/neo/evidencias/sync-conflicts/<data>.md` com diff e decisão tomada.
- Suíte upstream quebrou após merge → reverter merge, pedir ajuda no upstream
  via issue, **não** patch local sem entender root cause.
- Skin/Locale/Panel quebrou em release nova → checar `THEMES.md` e changelog
  upstream; muitas vezes o upstream mudou o nome de uma variável CSS ou
  chave i18n (ajustar bloco Neo em vez de alterar upstream).

---

## 9. Checklist de PR para qualquer mudança Neo

- [ ] Toquei só arquivos da lista 🟢 ou edição cirúrgica em 🟡?
- [ ] Toda edição em arquivo upstream tem comentário `// NEO:`/`# NEO:`?
- [ ] Suíte `pytest tests/` continua verde?
- [ ] Lint de branding passou (`scripts/lint_neo_branding.sh`)?
- [ ] Paridade pt-BR vs en passou (`tests/test_locale_parity_pt_br.py`)?
- [ ] Documentação atualizada (HU em [TASKS.md](./TASKS.md), e PRD/BACKLOG se mudou contrato)?
- [ ] Evidências adicionadas em `docs/neo/evidencias/<HU-ID>/`?
