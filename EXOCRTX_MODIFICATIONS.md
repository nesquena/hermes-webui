# EXOCRTX_MODIFICATIONS.md

Catálogo das modificações Exocórtex aplicadas sobre o fork de `nesquena/hermes-webui`.
Cada entrada documenta arquivo, propósito e risco de conflito para guiar o rebase
(`git rebase upstream/master`) e o comando `./ctl.sh update`.

- **Fork:** `elderbernardi/hermes-webui`
- **Branch de produção:** `exocortex/stable` (roda na porta 8787)
- **Base atual:** upstream `v0.51.440` (Release PA, commit `e47f685b`)
- **Política:** todas as modificações vivem no fork — sem PRs upstream (divergência arquitetural elevada).

> Convenção de commit: cada modificação carrega a tag `[MOD-NNN]` no assunto para rastreio no `git log`.

---

## Camada 1 — Skin EXCRTX (rebranding visual)

As cinco modificações abaixo introduzem a skin `excrtx` e o rebranding Hermes → EXCRTX.IA.

### MOD-001: Registrar skin `excrtx` no backend (v0.51.440 base)
- **Arquivos:** `api/config.py`
- **Tipo:** backend
- **Propósito:** adiciona `"excrtx"` ao conjunto `_SETTINGS_SKIN_VALUES` e define `"skin": "excrtx"` como default em `_SETTINGS_DEFAULTS`, para que a skin seja válida e ativa por padrão.
- **Reaplicar se:** upstream alterar `_SETTINGS_DEFAULTS` ou `_SETTINGS_SKIN_VALUES` (ex.: novas skins, refatoração de settings).
- **Conflito provável:** `api/config.py` — médio. Upstream mexe nessa lista ao adicionar skins próprias.

### MOD-002: Adicionar EXCRTX à lista de skins do frontend (v0.51.440 base)
- **Arquivos:** `static/boot.js`
- **Tipo:** frontend
- **Propósito:** adiciona a entrada `{name:'EXCRTX', value:'excrtx', colors:[...]}` ao array `_SKINS`, para a skin aparecer no seletor de aparência com as cores da marca.
- **Reaplicar se:** upstream adicionar/reordenar entradas em `_SKINS`.
- **Conflito provável:** `static/boot.js` — baixo. Inserção de uma linha no fim do array.

### MOD-003: Rebranding do shell para EXCRTX.IA (v0.51.440 base)
- **Arquivos:** `static/index.html`, `static/excrtx-logo.png`, `static/excrtx-titlebar.svg`
- **Tipo:** frontend + assets
- **Propósito:** substitui marca Hermes por EXCRTX.IA — `<title>`, ícone/título da titlebar (usa `excrtx-titlebar.svg`), logo do empty-state (usa `excrtx-logo.png`), headline `Exocórtex.IA — cognição estendida`, e ajusta default de skin/tema nos scripts inline de boot (`excrtx` / `light`).
- **Reaplicar se:** upstream alterar o markup da titlebar, do empty-state ou os scripts inline de inicialização de tema/skin.
- **Conflito provável:** `static/index.html` — alto. Os scripts inline de boot mudam com frequência no upstream e tocam exatamente as linhas modificadas.

### MOD-004: Folha de estilo da skin EXCRTX (v0.51.440 base)
- **Arquivos:** `static/style.css`
- **Tipo:** CSS
- **Propósito:** adiciona o bloco de variáveis/regras da skin `excrtx` (paleta da marca) ao final do stylesheet.
- **Reaplicar se:** upstream reestruturar o sistema de skins/tokens de cor em `style.css`.
- **Conflito provável:** `static/style.css` — baixo. Bloco aditivo no fim do arquivo.

### MOD-005: Listar skin `excrtx` no `cmd_theme` dos locales (v0.51.440 base)
- **Arquivos:** `static/i18n.js`
- **Tipo:** frontend (i18n)
- **Propósito:** acrescenta `/excrtx` à enumeração de skins na string `cmd_theme` em todos os idiomas, para a ajuda do comando refletir a skin disponível.
- **Reaplicar se:** upstream adicionar skins (muda a mesma string em todos os locales) ou novos idiomas.
- **Conflito provável:** `static/i18n.js` — médio. Mesma chave repetida por idioma; upstream toca nela ao introduzir skins.

---

## Workflow de atualização (rebase)

```bash
git fetch upstream
git checkout exocortex/stable
git rebase upstream/master
# resolver conflitos guiado por este catálogo (ver "Conflito provável")
# rodar testes e smoke-test do servidor antes de promover
```

O comando `./ctl.sh update` (v1) automatiza apenas: fetch + diff stat + confirmação.
Rebase, testes e restart permanecem manuais nesta versão.
