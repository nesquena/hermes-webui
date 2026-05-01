# Evidência HU-02.3 — Teste de paridade `pt-BR` vs `en`

**Data:** 2026-05-01  
**Branch:** `neo/sprint-1`  
**Status:** teste implementado e executado com sucesso; falta incluir o comando no checklist de PR.

## Escopo validado

O arquivo `tests/test_locale_parity_pt_br.py` valida:

- existência de `LOCALES['pt-BR']`;
- metadados `_label` e `_speech`;
- ausência de chaves faltantes em relação ao locale `en`;
- ausência de chaves extras em relação ao locale `en`;
- traduções representativas para áreas críticas da UI.

## Comando executado

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

## Regressão relacionada

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
