# Hermes Fork Patches — Settings UI

Scoped fork patches on top of upstream `nesquena/hermes-webui` for the
HermesOS Cloud platform. Designed to minimize merge-conflict surface so daily
upstream rebases stay clean.

## Goals

1. **First-class custom providers** — Venice, CroFAI, Bankr, CometAPI, Xiaomi MiMo appear as native Settings → Providers cards with API-key edit form (not the `custom:`-prefixed read-only entries upstream creates from `custom_providers:` config).
2. **Provider logos** — every provider card renders an icon (clearbit-fetched or local fallback).
3. **STT settings panel** — Settings → Voice section exposing `stt.provider` / `stt.model` / API key for groq/openai/local, writing to `config.yaml` via the existing `/api/claude-config` PATCH endpoint.
4. **OAuth Authenticate button** — for OAuth providers (Codex, Nous, Copilot, Qwen-OAuth), add an inline button that opens the composer terminal pre-typed with `hermes auth add <provider>`.

## Files touched

| File | Change | Conflict risk |
|---|---|---|
| `api/config.py` | Add 5 providers to `_PROVIDER_DISPLAY`, `_OPENAI_COMPAT_PROVIDER_BASE_URLS`, `_PROVIDER_MODELS` | Low — additive dict entries |
| `api/providers.py` | Add to `_PROVIDER_ENV_VAR`; add new `_PROVIDER_LOGO_URL` dict; surface `logo_url` in `get_providers()` payload | Low — additive |
| `static/index.html` | Add `<section id="settingsPaneVoice">` for STT panel; mark anchors for inline injection | Low — only adds a new pane |
| `static/panels.js` | Patch `_buildProviderCard` to render `<img src="logo_url">` + `[Authenticate]` button for OAuth providers; add `loadVoicePanel()` | Medium — touches a hot file |
| `static/i18n.js` | Add string keys: `voice_section_title`, `voice_provider_label`, `provider_authenticate_btn`, etc. | Low — additive |
| `tests/test_first_class_providers.py` | Membership assertions for the 5 new providers in dicts | Low — new test file |

## Provider data (source of truth for this fork)

```python
# api/config.py — add to _PROVIDER_DISPLAY
"venice": "Venice",
"crof": "CrofAI",
"bankr": "Bankr",
"cometapi": "CometAPI",
"xiaomi": "Xiaomi MiMo",

# api/config.py — add to _OPENAI_COMPAT_PROVIDER_BASE_URLS
"venice": "https://api.venice.ai/api/v1",
"crof": "https://crof.ai/v1",            # already present
"bankr": "https://gateway.bankr.bot/v1",
"cometapi": "https://api.cometapi.com/v1",
"xiaomi": "https://api.xiaomi.com/v1",

# api/providers.py — add to _PROVIDER_ENV_VAR
"venice": "VENICE_API_KEY",
"crof": "CROF_API_KEY",
"bankr": "BANKR_API_KEY",
"cometapi": "COMETAPI_API_KEY",
"xiaomi": "XIAOMI_API_KEY",

# api/providers.py — new _PROVIDER_LOGO_URL
# Falls back to clearbit; cards render a generic icon if missing.
_PROVIDER_LOGO_URL = {
  "venice": "https://logo.clearbit.com/venice.ai",
  "crof":   "https://logo.clearbit.com/crof.ai",
  "bankr":  "https://logo.clearbit.com/bankr.bot",
  "cometapi": "https://logo.clearbit.com/cometapi.com",
  "xiaomi": "https://logo.clearbit.com/mi.com",
  "anthropic": "https://logo.clearbit.com/anthropic.com",
  "openai": "https://logo.clearbit.com/openai.com",
  "openrouter": "https://logo.clearbit.com/openrouter.ai",
  "deepseek": "https://logo.clearbit.com/deepseek.com",
  "google": "https://logo.clearbit.com/google.com",
  "gemini": "https://logo.clearbit.com/google.com",
  "x-ai": "https://logo.clearbit.com/x.ai",
  "mistralai": "https://logo.clearbit.com/mistral.ai",
  "nvidia": "https://logo.clearbit.com/nvidia.com",
  "alibaba": "https://logo.clearbit.com/alibabagroup.com",
  "huggingface": "https://logo.clearbit.com/huggingface.co",
  "ollama": "https://logo.clearbit.com/ollama.com",
  "ollama-cloud": "https://logo.clearbit.com/ollama.com",
  "groq": "https://logo.clearbit.com/groq.com",
  "minimax": "https://logo.clearbit.com/minimax.chat",
  "kimi-coding": "https://logo.clearbit.com/moonshot.cn",
  "zai": "https://logo.clearbit.com/z.ai",
  "qwen": "https://logo.clearbit.com/qwen.ai",
  "lmstudio": "https://logo.clearbit.com/lmstudio.ai",
  "copilot": "https://logo.clearbit.com/github.com",
  "openai-codex": "https://logo.clearbit.com/openai.com",
  "nous": "https://logo.clearbit.com/nousresearch.com",
}
```

## Marker convention for re-merge resilience

Each insertion in upstream files is wrapped in a stable comment marker so
`git rebase` can locate and re-apply automatically:

```python
# >>> hermes-fork: first-class providers
"venice": "Venice",
...
# <<< hermes-fork
```

When upstream conflicts, look only inside `>>> hermes-fork` blocks.

## Build pipeline

1. Commit changes on `ashneil12/master`.
2. CI auto-rebuilds `:stable` (~4 min).
3. Fleet picks up via `docker pull` on next provision; existing instances
   refresh via the dashboard "Update" button (or manual `docker compose pull`).

## Out of scope for this fork patch

- Recommended/All/Custom tabs in Settings → Providers (keeps simpler flat list)
- Server-side OAuth handoff (Option C) — Authenticate button uses Option A (terminal pretype) only
- Per-provider quota/usage panels for new providers (upstream supports this for known ones; new providers fall back to "no quota info")
- Re-skin (separate session)
