# Evidência HU-02.2 — `pt-BR` default via env

**Data:** 2026-05-01  
**Branch:** `neo/sprint-1`  
**Status:** implementação validada por teste automatizado; falta screenshot/homologação visual para fechar DoD.

## Escopo validado

- `api/config.py` lê `HERMES_WEBUI_LOCALE`.
- `api/routes.py` injeta `__NEO_DEFAULT_LOCALE__` no HTML.
- `static/i18n.js` usa `window.__neoDefaults.locale` no carregamento inicial quando não há escolha salva.
- A preferência explícita do usuário em `localStorage['hermes-lang']` continua vencendo o default do servidor.
- Default inválido cai para `en`.

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
.venv/bin/python -m pytest tests/test_locale_parity_pt_br.py tests/test_language_precedence.py tests/test_spanish_locale.py tests/test_sienna_skin.py -q
```

Resultado:

```text
...................                                                      [100%]
19 passed in 1.58s
```

## Observação de compatibilidade

O upstream usa `localStorage['hermes-lang']` como chave persistida de idioma. A implementação manteve essa chave para evitar migração desnecessária e preservar preferências de usuários existentes.

## Pendências para DoD

- Anexar screenshot da primeira renderização em `pt-BR`.
- Registrar homologação manual local ou em staging.
