# Evidência HU-02.1 — Locale `pt-BR` com paridade do `en`

**Data:** 2026-05-01  
**Branch:** `neo/sprint-1`  
**Status:** implementação validada por teste automatizado; falta screenshot/homologação visual para fechar DoD.

## Escopo validado

- `static/i18n.js` agora expõe `LOCALES['pt-BR']`.
- O locale `pt-BR` herda a base `pt` e cobre as chaves que faltavam para paridade com `en`.
- Metadados do locale:
  - `_label`: `Português (Brasil)`
  - `_speech`: `pt-BR`
- Chaves representativas validadas:
  - `terminal_title`: `Terminal`
  - `profile_active`: `ativo`
  - `mcp_servers_title`: `Servidores MCP`
  - `composer_send`: `Enviar`
  - `workspace_manage`: `Gerenciar espaços`

## Comandos executados

```bash
node --check static/i18n.js
```

Resultado: passou.

```bash
HERMES_WEBUI_TEST_STATE_DIR=/tmp/neo-webui-test \
HERMES_WEBUI_STATE_DIR=/tmp/neo-webui-test \
HERMES_HOME=/tmp/neo-webui-test \
HERMES_BASE_HOME=/tmp/neo-webui-test \
.venv/bin/python -m pytest tests/test_locale_parity_pt_br.py -q
```

Resultado:

```text
...                                                                      [100%]
3 passed in 1.08s
```

```bash
HERMES_WEBUI_TEST_STATE_DIR=/tmp/neo-webui-test \
HERMES_WEBUI_STATE_DIR=/tmp/neo-webui-test \
HERMES_HOME=/tmp/neo-webui-test \
HERMES_BASE_HOME=/tmp/neo-webui-test \
.venv/bin/python -m pytest tests/test_language_precedence.py tests/test_spanish_locale.py tests/test_sienna_skin.py -q
```

Resultado:

```text
.............                                                            [100%]
13 passed in 1.16s
```

## Observação de ambiente

O pytest que sobe o servidor de teste precisa abrir socket local. Dentro do sandbox, o servidor falhou com `PermissionError: Operation not permitted`; fora do sandbox, o mesmo servidor iniciou normalmente e os testes passaram.

## Pendências para DoD

- Anexar screenshot da UI renderizada em `pt-BR`.
- Registrar homologação manual local ou em staging.
