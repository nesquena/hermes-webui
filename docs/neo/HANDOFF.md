# HANDOFF — Sprint 1 (interrompido em 2026-04-30)

> Anotação de onde a execução parou para retomada em outra instância.

## Branch
`neo/sprint-1` (criada a partir de `master`). Não há commits ainda — todas as
mudanças estão **uncommitted no working tree**. Rodar `git status` para ver.

## Ambiente local
- venv: `/home/jrmelo/Projetos/neo-webui/.venv` (apenas `pyyaml`)
- `.env` na raiz do repo configura: `BOT_NAME=Neo`, `DEFAULT_SKIN=neo`, `LOCALE=pt-BR`, porta `8788`, state dir `~/.hermes-neo-dev/webui`
- Para subir: `set -a && source .env && set +a && .venv/bin/python server.py` (ou em background com `nohup ... > /tmp/neo-webui.log 2>&1 &`)
- Para skip onboarding: editar `~/.hermes-neo-dev/webui/settings.json` com `"onboarding_completed": true`

## HUs concluídas (validadas visualmente pelo usuário)

| HU | Status | Arquivos tocados |
|---|---|---|
| HU-01.1 | ✅ Topbar/título/composer = "Neo" | `.env` (HERMES_WEBUI_BOT_NAME=Neo) — código já era configurável upstream |
| HU-01.3 | ✅ Skin "neo" (paleta cyan/azul-noite) | `static/style.css` (bloco aditivo `[data-skin="neo"]`), `static/index.html` (linha 18 — boot script com skins object), `static/boot.js` (`_SKINS` array linha ~669), `api/config.py` (`_SETTINGS_SKIN_VALUES` ~linha 2210) |
| HU-01.4 | ✅ Default skin/locale via env, sem flicker | `static/index.html` (script `window.__neoDefaults` antes do boot), `api/config.py` (`HERMES_WEBUI_DEFAULT_SKIN` e `HERMES_WEBUI_LOCALE` em `_SETTINGS_DEFAULTS`), `api/routes.py` (`handle_get` faz replace de `__NEO_DEFAULT_SKIN__` e `__NEO_DEFAULT_LOCALE__` no index.html servido) |
| HU-01.6 | ✅ `/skin neo` slash command | Auto-habilitado por adicionar "Neo" ao `_SKINS` em `boot.js` |

Todas as edições em arquivos upstream estão marcadas com comentário `// NEO:` ou `# NEO:` conforme `docs/neo/UPSTREAM-SYNC.md` §3.

## HUs pendentes da Sprint 1

| HU | Status | Como retomar |
|---|---|---|
| **HU-02.1** | 🟡 **interrompida** — i18n.js já tem locale `pt` (linha 4577–5226) com 623 chaves; `en` tem 698; **faltam 151 chaves no `pt`** | (1) Adicionar bloco `'pt-BR': { ...LOCALES.pt, /* 151 keys traduzidas */ }` logo após o bloco `pt`. Lista em [`_pt-BR-missing-keys.txt`](./_pt-BR-missing-keys.txt). (2) Ler valores em `en` para cada chave faltante e traduzir para PT-BR. (3) Adicionar `'pt-BR'` ao seletor de Settings em `panels.js`/`index.html`. (4) Resolver já lida com fallback `pt-BR → pt` (ver `i18n.js:6044`); usuário já vê parcial pt-BR funcionando hoje. |
| HU-02.2 | 🟡 **estrutura pronta**, falta validar | `HERMES_WEBUI_LOCALE` já lido em `config.py`; `__NEO_DEFAULT_LOCALE__` já injeta no HTML. Após HU-02.1, validar fluxo `localStorage.clear()` + reload → carrega pt-BR sem flicker. |
| HU-01.2 | 🔵 não iniciada | Logo NEO orb (asset `static/brand/neo-orb.svg` + hook em `boot.js applyBotName()` para trocar SVG quando `_botName` ≠ Hermes). Caduceu hoje em `index.html:65-75` e `:212`. |
| HU-01.5 | 🔵 não iniciada | Substituir `static/favicon.svg`, `favicon-32.png`, `favicon.ico`, `apple-touch-icon.png`, ajustar `manifest.json`. Depende da arte do orb (HU-01.2). |

## Arquivos modificados (estado atual no working tree)

```
static/style.css        +33 linhas (bloco [data-skin="neo"] aditivo após sienna)
static/index.html       +1 linha boot pre-script + edição line 18
static/boot.js          +2 linhas no _SKINS array
api/config.py           +4 linhas (envs em _SETTINGS_DEFAULTS, "neo"+"sienna" em _SETTINGS_SKIN_VALUES)
api/routes.py           +9 linhas (replace de __NEO_DEFAULT_*__ em handle_get)
.env                    novo (não comitar! adicionar a .gitignore se for o caso)
docs/neo/HANDOFF.md     novo
docs/neo/_pt-BR-missing-keys.txt  novo (auxiliar)
```

⚠️ **Não commitado.** Conferir com `git diff` antes de commitar.

## Próximos passos sugeridos para a próxima instância

1. **Commit do que está pronto** (Sprint 1, HUs 01.1/01.3/01.4/01.6) num commit limpo:
   `git add -p` selecionando só arquivos rastreados acima; manter `.env` fora.
2. **HU-02.1**: gerar bloco `pt-BR` em `i18n.js` lendo `_pt-BR-missing-keys.txt` e traduzindo a partir do `en`. Depois rodar a UI e validar visualmente que não sobra mais texto em inglês.
3. **HU-01.2 + HU-01.5**: criar/encomendar SVG do orb NEO; substituir caduceu e favicons.
4. Atualizar [`docs/neo/TASKS.md`](./TASKS.md) marcando os checkboxes das HUs concluídas e anexando screenshots em `docs/neo/evidencias/HU-XX.Y/`.

## Notas para qualquer agente continuando

- Comentário `// NEO:` / `# NEO:` é a convenção: toda edição em arquivo upstream precisa dele (ver `UPSTREAM-SYNC.md` §3).
- `pytest tests/` precisa continuar verde. Ainda não rodei após estas mudanças — **rodar antes do commit**.
- O lint `scripts/lint_neo_branding.sh` ainda não existe (HU-06.2 pendente).
