# Fix: PR #2676 — Inline Imports + i18n Consistency

> **Branch:** `feat/skills-toggle-ui` (hermes-webui)

**Goal:** Corrigir 2 problemas estruturais apontados na revisão.

**Problemas:**
1. **Inline imports** — `_handle_skill_toggle` importa `_get_config_path`, `_load_yaml_config_file`, `_save_yaml_config_file`, `reload_config`, `_cfg_lock` dentro da função. `api.config` já é importado no topo do módulo (linha 907). Esses 5 símbolos pertencem ao topo.

2. **i18n inconsistente** — As 3 keys novas (`skill_enabled`, `skill_disabled`, `skill_toggle_failed`) existem em en, es, fr, ja, zh, ru (com valores em inglês) mas NÃO existem em it, de, pt, ko. Os testes de locale (zh, ja, ru) exigem paridade de keys com en. Solução: adicionar as keys nos 4 locales faltantes com valores em inglês (consistente com o padrão atual de fallback).

---

## Task 1: routes.py — mover inline imports pro topo

**Objective:** Mover os 5 símbolos de `api.config` do corpo de `_handle_skill_toggle` para o `from api.config import (...)` no topo do módulo.

**Files:** `api/routes.py:907-943` (topo) e `api/routes.py:10945-10995` (handler)

**Passo 1:** Adicionar ao bloco de import existente (linha 907-943):

```python
from api.config import (
    STATE_DIR,
    ...
    _get_config_path,
    _load_yaml_config_file,
    _save_yaml_config_file,
    reload_config,
    _cfg_lock,
)
```

**Passo 2:** Remover a inline import de `_handle_skill_toggle`:

```python
    # Deletar esta linha:
    from api.config import _get_config_path, _load_yaml_config_file, _save_yaml_config_file, reload_config, _cfg_lock
```

**Verificação:** `pytest tests/test_skills_toggle.py -v` — 8/8 PASSING

**Commit:** `git commit -m "refactor: move api.config inline imports to module level in routes.py"`

---

## Task 2: i18n.js — adicionar keys em it, de, pt, ko

**Objective:** Adicionar `skill_enabled`, `skill_disabled`, `skill_toggle_failed` nos 4 locales que estão sem elas.

**Files:** `static/i18n.js`

**Locais a modificar:** it (1239-2459), de (6011-7172), pt (9569-10613), ko (10614-11827)

**Padrão:** Inserir entre `skills_no_match:` e `linked_files:` em cada locale, com valores em inglês (fallback).

```javascript
    skills_no_match: '...',
    skill_enabled: 'Enabled',
    skill_disabled: 'Disabled',
    skill_toggle_failed: 'Failed to toggle skill: ',
    linked_files: '...',
```

**Verificação:**
```bash
pytest tests/test_skills_toggle.py -v
pytest tests/test_chinese_locale.py tests/test_japanese_locale.py tests/test_russian_locale.py -v
```

**Commit:** `git commit -m "fix(i18n): add skill toggle keys to it, de, pt, ko locales for consistency"`

---

## Task 3: push + verificar CI

**Passos:**
1. `git push fork feat/skills-toggle-ui`
2. Aguardar CI
3. Verificar que todas as checks passam

---

## Comandos de verificação final

```bash
cd /home/lucas/projects/hermes-webui
python3 -m pytest tests/test_skills_toggle.py tests/test_chinese_locale.py tests/test_japanese_locale.py tests/test_russian_locale.py -v
timeout 120 python3 -m pytest tests/test_skills_category_collapse.py tests/test_skill_detail_error_guard.py tests/test_regressions.py -v
```
