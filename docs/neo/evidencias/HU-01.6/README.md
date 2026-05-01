# Evidência HU-01.6 — `/skin neo` aplica skin ao vivo

**Data:** 2026-05-01  
**Branch:** `neo/sprint-1`  
**Status:** fluxo validado por inspeção automatizada; falta screencast/manual para fechar DoD.

## Escopo validado

- `static/commands.js` monta a lista de skins a partir de `_SKINS`.
- Como `Neo` está em `_SKINS`, o comando `/skin neo` entra pelo fluxo comum de skins.
- O fluxo comum persiste `hermes-skin`, aplica `_applySkin(...)`, sincroniza picker e envia `theme/skin` para `/api/settings`.

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

- Teste manual de `/skin neo` no chat.
- Verificar persistência em `settings.json`.
- Anexar screencast curto.
