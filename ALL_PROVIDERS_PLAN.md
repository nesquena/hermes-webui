# All Hermes Providers Support — Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Add comprehensive support for all Hermes providers in the WebUI dropdown and provider detection, ensuring every provider Hermes agent supports can be selected and used from the WebUI.

**Architecture:** The provider system works as follows:
1. `api/config.py` — `_PROVIDER_MODELS` dict maps provider IDs to their model lists; `_PROVIDER_DISPLAY` maps IDs to display names; `get_available_models()` detects available providers from API keys and returns grouped model lists to the frontend.
2. Frontend (`static/`) — receives the grouped model list from `/api/models` and renders a dropdown partitioned by provider.
3. At chat time, `resolve_model_provider()` in `api/config.py` and `resolve_runtime_provider()` from `hermes_cli` route the request to the correct provider with the right API key.

**Tech Stack:** Python (api/config.py, api/routes.py), JavaScript (static/ boot.js, panels.js, ui.js), Hermes hermes_cli for runtime provider resolution.

---

## Audit Results

### Providers with `_PROVIDER_MODELS` entries (partially complete)
| Provider ID | Status |
|-------------|--------|
| `anthropic` | ✅ Complete (4 models) |
| `openai` | ✅ Complete (2 models) |
| `openai-codex` | ✅ Complete (8 models) |
| `google` / `gemini` | ✅ Complete (5 models each) |
| `deepseek` | ✅ Complete (2 models) |
| `nous` | ⚠️ Incomplete (4 models, portal-specific) |
| `zai` | ✅ Complete (6 models) |
| `kimi-coding` | ✅ Complete (4 models) |
| `minimax` | ✅ Complete (5 models) |
| `opencode-zen` | ✅ Complete (32 models) |
| `opencode-go` | ✅ Complete (7 models) |
| `mistralai` | ✅ Complete (2 models) |
| `qwen` | ✅ Complete (2 models) |
| `x-ai` | ✅ Complete (1 model) |

### Providers MISSING from `_PROVIDER_MODELS`
| Provider ID | Status |
|-------------|--------|
| `openrouter` | ⚠️ Uses `_FALLBACK_MODELS` hardcoded list, no dynamic fetching |
| `huggingface` | ❌ No entry |
| `alibaba` / `dashscope` | ❌ No entry |
| `meta-llama` | ❌ No entry |
| `ollama` | ❌ No entry (local, no API key needed) |
| `lmstudio` | ❌ No entry (local, no API key needed) |
| `xiaomi` / `mimo` | ❌ No entry |
| `kilocode` | ❌ No entry |

### `_PROVIDER_DISPLAY` gaps
Missing: `nous` (shown as "Nous Portal" already in config but not in dict?), `openrouter`, `kimi-coding`, `deepseek`, `minimax`, `google`, `meta-llama`, `huggingface`, `alibaba`, `ollama`, `lmstudio`, `mistralai`, `qwen`, `x-ai`, `xiaomi`, `kilocode`, `openai-codex`

### Provider detection env vars missing
| Provider | Missing env var |
|----------|-----------------|
| `x-ai` | `XAI_API_KEY` |
| `huggingface` | `HF_TOKEN` |
| `alibaba` | `DASHSCOPE_API_KEY` |
| `meta-llama` | `METALLAMA_API_KEY` (if they have one; may use OAuth) |
| `xiaomi` | `XIAOMI_API_KEY` |
| `kilocode` | `KILOCODE_API_KEY` |
| `openai-codex` | `OPENAI_CODEX_API_KEY` (if separate from OPENAI_API_KEY) |

---

## Tasks

### Task 1: Add missing providers to `_PROVIDER_DISPLAY`

**Objective:** Ensure every Hermes provider has a human-readable display name in the dropdown.

**Files:**
- Modify: `api/config.py:460-483`

**Step 1: Read current `_PROVIDER_DISPLAY` dict**

```python
_PROVIDER_DISPLAY = {
    "nous": "Nous Portal",
    "openrouter": "OpenRouter",
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "openai-codex": "OpenAI Codex",
    "copilot": "GitHub Copilot",
    "zai": "Z.AI / GLM",
    "kimi-coding": "Kimi / Moonshot",
    "deepseek": "DeepSeek",
    "minimax": "MiniMax",
    "google": "Google",
    "meta-llama": "Meta Llama",
    "huggingface": "HuggingFace",
    "alibaba": "Alibaba",
    "ollama": "Ollama",
    "opencode-zen": "OpenCode Zen",
    "opencode-go": "OpenCode Go",
    "lmstudio": "LM Studio",
    "mistralai": "Mistral",
    "qwen": "Qwen",
    "x-ai": "xAI",
}
```

**Step 2: Add missing entries** (the dict above already shows all entries that should be present — verify against actual file)

**Step 3: Commit**

```bash
git add api/config.py
git commit -m "feat: add all Hermes providers to _PROVIDER_DISPLAY"
```

---

### Task 2: Add missing provider model lists to `_PROVIDER_MODELS`

**Objective:** Every provider must have at least one model entry so the dropdown can show it.

**Files:**
- Modify: `api/config.py:486-625`

**Step 1: Add entries for missing providers**

Add after the existing `"x-ai"` entry (around line 624):

```python
# HuggingFace — token-based, models from huggingface.co
"huggingface": [
    {"id": "meta-llama/llama-4-scout", "label": "Llama 4 Scout"},
    {"id": "meta-llama/llama-4-maverick", "label": "Llama 4 Maverick"},
    {"id": "mistralai/mistral-small", "label": "Mistral Small"},
    {"id": "google/gemma-4-26b-a4b", "label": "Gemma 4 26B"},
    {"id": "deepseek-ai/deepseek-v3", "label": "DeepSeek V3"},
],

# Alibaba / DashScope — qwen prefix in OpenRouter
"alibaba": [
    {"id": "qwen/qwen3-coder", "label": "Qwen3 Coder"},
    {"id": "qwen/qwen3.6-plus", "label": "Qwen3.6 Plus"},
    {"id": "qwen/qwen2.5-coder", "label": "Qwen2.5 Coder"},
],

# Meta Llama — direct Meta API (when available)
"meta-llama": [
    {"id": "llama-4-scout", "label": "Llama 4 Scout"},
    {"id": "llama-4-maverick", "label": "Llama 4 Maverick"},
    {"id": "llama-3.3-70b-instruct", "label": "Llama 3.3 70B"},
],

# Ollama — local provider, models fetched from /v1/models or use defaults
"ollama": [
    {"id": "llama3.3", "label": "Llama 3.3"},
    {"id": "mistral", "label": "Mistral"},
    {"id": "codellama", "label": "Code Llama"},
    {"id": "mixtral", "label": "Mixtral"},
],

# LM Studio — local provider
"lmstudio": [
    {"id": "llama-3.3-70b", "label": "Llama 3.3 70B"},
    {"id": "mistral-7b", "label": "Mistral 7B"},
    {"id": "mixtral-8x7b", "label": "Mixtral 8x7B"},
],

# Xiaomi MiMo
"xiaomi": [
    {"id": "MiMo-7B", "label": "MiMo 7B"},
    {"id": "MiMo-8B", "label": "MiMo 8B"},
    {"id": "MiMo-14B", "label": "MiMo 14B"},
],

# Kilo Code
"kilocode": [
    {"id": "k2.5-coder", "label": "K2.5 Coder"},
    {"id": "k2-coder", "label": "K2 Coder"},
],
```

**Step 2: Expand existing incomplete entries**

Update `"nous"` to include all major portal models:

```python
"nous": [
    {"id": "claude-opus-4.6", "label": "Claude Opus 4.6 (via Nous)"},
    {"id": "claude-sonnet-4.6", "label": "Claude Sonnet 4.6 (via Nous)"},
    {"id": "claude-sonnet-4-5", "label": "Claude Sonnet 4.5 (via Nous)"},
    {"id": "gpt-5.4-mini", "label": "GPT-5.4 Mini (via Nous)"},
    {"id": "gpt-5.4", "label": "GPT-5.4 (via Nous)"},
    {"id": "gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro (via Nous)"},
    {"id": "deepseek-chat-v3-0324", "label": "DeepSeek V3 (via Nous)"},
],
```

**Step 3: Commit**

```bash
git add api/config.py
git commit -m "feat: add _PROVIDER_MODELS entries for all Hermes providers"
```

---

### Task 3: Add missing provider API key detection env vars

**Objective:** The WebUI must detect which providers are available by scanning env vars.

**Files:**
- Modify: `api/config.py:829-867`

**Step 1: Add missing env var scanning**

In the `if not _hermes_auth_used:` block, add these checks alongside the existing ones:

```python
# xAI / Grok
if all_env.get("XAI_API_KEY"):
    detected_providers.add("x-ai")

# HuggingFace
if all_env.get("HF_TOKEN") or all_env.get("HUGGINGFACE_TOKEN"):
    detected_providers.add("huggingface")

# Alibaba / DashScope
if all_env.get("DASHSCOPE_API_KEY"):
    detected_providers.add("alibaba")

# Xiaomi
if all_env.get("XIAOMI_API_KEY"):
    detected_providers.add("xiaomi")

# Kilo Code
if all_env.get("KILOCODE_API_KEY"):
    detected_providers.add("kilocode")
```

**Step 2: Also scan os.getenv directly for these new vars (in case they're not in .env)**

The existing code already does `val = os.getenv(k)` for all listed vars. Make sure all new vars are added to the tuple at line ~829:

```python
for k in (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "GLM_API_KEY",
    "KIMI_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENCODE_ZEN_API_KEY",
    "OPENCODE_GO_API_KEY",
    "MINIMAX_API_KEY",
    "MINIMAX_CN_API_KEY",
    "XAI_API_KEY",           # ADD
    "HF_TOKEN",              # ADD
    "HUGGINGFACE_TOKEN",     # ADD (alias)
    "DASHSCOPE_API_KEY",     # ADD
    "XIAOMI_API_KEY",        # ADD
    "KILOCODE_API_KEY",      # ADD
):
```

**Step 3: Commit**

```bash
git add api/config.py
git commit -m "feat: detect all Hermes provider API keys from env vars"
```

---

### Task 4: Add provider-specific API key resolution for new providers in streaming

**Objective:** When a user selects a model from a non-default provider, the WebUI needs to resolve the correct API key. Currently `resolve_runtime_provider` handles this, but we need to ensure the env var mapping is complete.

**Files:**
- Modify: `api/streaming.py` (if needed)

**Step 1: Verify resolve_runtime_provider handles all providers**

The `resolve_runtime_provider` from `hermes_cli.runtime_provider` is the authoritative source. Check that it maps all provider IDs correctly by reviewing `hermes_cli/auth.py` and `hermes_cli/providers.py` in the hermes-agent source. This step is primarily for verification — most providers are already handled by the hermes_cli layer.

If any new provider needs explicit key-to-envvar mapping in the webui (not handled by hermes_cli), add it to the resolution logic around line 1021 in streaming.py where `resolve_runtime_provider` is called.

**Step 2: Commit**

```bash
git add api/streaming.py  # only if modified
git commit -m "chore: verify provider API key resolution for all providers"
```

---

### Task 5: Add provider-specific model prefix routing for new providers

**Objective:** Non-default providers must use the `@provider:model` format so `resolve_model_provider()` can route them correctly.

**Files:**
- Modify: `api/config.py:1090-1123`

**Step 1: Verify the existing routing logic handles new providers**

The existing code at lines 1090-1123 already handles this:
- If `active_provider != pid`, models are prefixed `@pid:model`
- This works for any provider in `_PROVIDER_MODELS`

Since we added all providers to `_PROVIDER_MODELS` in Task 2, this should work automatically. Verify that new providers like `huggingface`, `alibaba`, `meta-llama`, `xiaomi`, `kilocode`, `ollama`, `lmstudio` follow the same pattern.

**Step 2: Commit**

```bash
git add api/config.py  # only if modified
git commit -m "feat: enable @provider:model routing for all new providers"
```

---

### Task 6: Add provider icon/emoji support to frontend

**Objective:** Each provider group in the dropdown should have a recognizable icon or emoji.

**Files:**
- Modify: `static/boot.js` (find model dropdown rendering)
- Modify: `static/panels.js` (model selector in composer)

**Step 1: Find the model dropdown rendering code**

Search in boot.js and panels.js for where provider group headers are rendered.

**Step 2: Add a provider icon/emoji map**

```javascript
const PROVIDER_ICONS = {
  'OpenRouter': '🔀',
  'Anthropic': '🧠',
  'OpenAI': '🤖',
  'OpenAI Codex': '⚡',
  'Google': '🔵',
  'DeepSeek': '🔮',
  'Nous Portal': '🌟',
  'Z.AI / GLM': '📊',
  'Kimi / Moonshot': '🌙',
  'MiniMax': '⚡',
  'xAI': '❌',
  'Mistral': '🌬️',
  'Qwen': '🍡',
  'HuggingFace': '🤗',
  'Alibaba': '🏢',
  'Meta Llama': '🦙',
  'Ollama': '🦕',
  'LM Studio': '💻',
  'OpenCode Zen': '🧘',
  'OpenCode Go': '🚀',
  'Xiaomi': '📱',
  'Kilo Code': '⚡',
  'Copilot': '🔷',
};
```

**Step 3: Use icons in dropdown rendering** (exact code depends on current rendering approach — inspect the actual template code first)

**Step 4: Commit**

```bash
git add static/boot.js static/panels.js
git commit -m "feat: add provider icons to model dropdown"
```

---

### Task 7: Run tests to verify no regressions

**Files:**
- Test: `tests/test_sprint46.py`, `tests/test_model_resolver.py`

**Step 1: Run existing provider/model tests**

```bash
cd ~/hermes-webui
python -m pytest tests/test_model_resolver.py -v
python -m pytest tests/test_sprint46.py -v
```

**Step 2: Run full test suite**

```bash
python -m pytest tests/ -v --tb=short 2>&1 | tail -50
```

**Step 3: Commit test results if any updates needed**

---

### Task 8: Update ROADMAP.md

**Files:**
- Modify: `ROADMAP.md`

**Step 1: Add feature entry**

In the Feature Parity Checklist under "Chat and Agent":

```
- [x] All Hermes providers supported: OpenRouter, Anthropic, OpenAI, Google, DeepSeek, Nous Portal, Z.AI/GLM, Kimi/Moonshot, MiniMax, xAI, Mistral, Qwen, HuggingFace, Alibaba, Meta Llama, Ollama, LM Studio, OpenCode Zen, OpenCode Go, Xiaomi MiMo, Kilo Code (Sprint 41)
```

**Step 2: Commit**

```bash
git add ROADMAP.md
git commit -m "docs: document all-providers feature in ROADMAP.md"
```

---

## Verification Steps

After all tasks:

1. **Fresh browser load** — Clear localStorage, reload the page, open the model dropdown. Every provider should appear as a separate group with at least one model.
2. **Select a non-default provider model** — e.g. select a HuggingFace model when OpenAI is the active provider. Send a test message. Verify the chat works (provider routing is correct).
3. **Provider detection** — Set only `HF_TOKEN` in `.env`. Restart the server. Verify HuggingFace appears in the dropdown but other providers without keys don't.
4. **Local providers** — With Ollama running locally and `base_url=http://localhost:11434` in config.yaml, verify Ollama appears in the dropdown.
5. **Run full test suite** — `pytest tests/ -q` should pass with no new failures.

## Files Summary

| File | Changes |
|------|---------|
| `api/config.py` | `_PROVIDER_DISPLAY` (Task 1), `_PROVIDER_MODELS` (Task 2), env var detection (Task 3), routing logic (Task 5) |
| `static/boot.js` | Provider icons (Task 6) |
| `static/panels.js` | Provider icons in composer model picker (Task 6) |
| `ROADMAP.md` | Feature documentation (Task 8) |

## Dependencies

- hermes-agent must be checked out (already part of the webui discovery)
- API keys set in `~/.hermes/.env` or environment
- hermes_cli runtime_provider must support all listed providers
