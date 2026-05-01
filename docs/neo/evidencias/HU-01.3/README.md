# Evidência HU-01.3 — Skin `neo` selecionável

**Data:** 2026-05-01  
**Branch:** `neo/sprint-1`  
**Status:** implementação validada por teste automatizado; falta evidência visual antes/depois para fechar DoD.

## Escopo validado

- `static/boot.js` expõe `{name:'Neo'}` em `_SKINS`.
- `static/index.html` aceita `neo` no early-init allowlist.
- `static/style.css` contém blocos `:root[data-skin="neo"]` e `:root.dark[data-skin="neo"]`.
- `api/config.py` aceita `neo` em `_SETTINGS_SKIN_VALUES`.

## Comando executado

```bash
HERMES_WEBUI_TEST_STATE_DIR=/tmp/neo-webui-test \
HERMES_WEBUI_STATE_DIR=/tmp/neo-webui-test \
HERMES_HOME=/tmp/neo-webui-test \
HERMES_BASE_HOME=/tmp/neo-webui-test \
.venv/bin/python -m pytest tests/test_neo_skin.py tests/test_sienna_skin.py -q
```

Resultado:

```text
..........                                                               [100%]
10 passed in 1.03s
```

## Pendências para DoD

- Screenshot antes/depois com skin `neo`.
- Homologação visual em light/dark.
- Ajuste de fonte Inter, se confirmado como parte da HU-01.3.
