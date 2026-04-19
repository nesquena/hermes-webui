# Onboarding All-Providers Support — Revised Plan

> **Covers the actual bottleneck:** `_SUPPORTED_PROVIDER_SETUPS` in `api/onboarding.py`

## The Problem

`_SUPPORTED_PROVIDER_SETUPS` (lines 31-63 in `api/onboarding.py`) is a static dict with only **4 entries**: `openrouter`, `anthropic`, `openai`, `custom`.

Two places gate keeping all other providers (deepseek, minimax, google, xai, etc.) out of the wizard:

**1. `_build_setup_catalog()`** — only iterates `_SUPPORTED_PROVIDER_SETUPS` (line 364), so the frontend **never even receives** the other providers in its dropdown.

**2. `apply_onboarding_setup()`** — at line 468, any provider not in `_SUPPORTED_PROVIDER_SETUPS` silently returns without writing config:
```python
if provider not in _SUPPORTED_PROVIDER_SETUPS:
    save_settings({"onboarding_completed": True})
    return get_onboarding_status()  # SILENT NO-OP
```

Meanwhile `api/config.py` already has:
- `_PROVIDER_DISPLAY` — human-readable names for all providers ✅
- `_PROVIDER_MODELS` — model lists for most providers ✅
- `get_available_models()` — env-var-based provider detection ✅

So the model dropdown at chat-runtime already works for all providers. The onboarding wizard is the only broken part.

---

## Approach

Make `_SUPPORTED_PROVIDER_SETUPS` **dynamically generated** at import time from `_PROVIDER_MODELS` + `_PROVIDER_DISPLAY` in `config.py`, with a per-provider metadata table for the fields that `_PROVIDER_MODELS` doesn't cover:
- `env_var` — which env var holds the API key
- `requires_base_url` — whether the provider needs a custom endpoint
- `default_base_url` — if known (only for openai and custom)

This eliminates the dual-maintenance problem: we no longer have two separate hardcoded lists to keep in sync.

---

## Tasks

### Task 1: Define `_PROVIDER_SETUP_METADATA` — the per-provider defaults table

**File:** `api/onboarding.py`

**Purpose:** A single source of truth for the fields that `_PROVIDER_MODELS` doesn't cover (env var name, base_url requirement, default base_url). Only providers that differ from the OpenAI-compatible API-key-per-provider pattern need an entry here.

**Add after the imports, before `_SUPPORTED_PROVIDER_SETUPS`:**

```python
# Metadata for providers that need non-default setup values.
# For most providers the default assumptions apply:
#   env_var = f"{PROVIDER_ID.upper()}_API_KEY"  (e.g. DEEPSEEK_API_KEY)
#   requires_base_url = False
#   default_base_url = ""
# Only override the ones that differ.
_PROVIDER_SETUP_METADATA: dict[str, dict] = {
    "openrouter": {
        "label": "OpenRouter",
        "env_var": "OPENROUTER_API_KEY",
        "requires_base_url": False,
        "default_base_url": "",
    },
    "anthropic": {
        "label": "Anthropic",
        "env_var": "ANTHROPIC_API_KEY",
        "requires_base_url": False,
        "default_base_url": "",
    },
    "openai": {
        "label": "OpenAI",
        "env_var": "OPENAI_API_KEY",
        "requires_base_url": False,
        "default_base_url": "https://api.openai.com/v1",
    },
    "google": {
        "label": "Google",
        "env_var": "GOOGLE_API_KEY",
        "requires_base_url": False,
        "default_base_url": "",
    },
    "gemini": {
        "label": "Gemini",
        "env_var": "GEMINI_API_KEY",
        "requires_base_url": False,
        "default_base_url": "",
    },
    "deepseek": {
        "label": "DeepSeek",
        "env_var": "DEEPSEEK_API_KEY",
        "requires_base_url": False,
        "default_base_url": "",
    },
    "minimax": {
        "label": "MiniMax",
        "env_var": "MINIMAX_API_KEY",
        "requires_base_url": False,
        "default_base_url": "",
    },
    "x-ai": {
        "label": "xAI",
        "env_var": "XAI_API_KEY",
        "requires_base_url": False,
        "default_base_url": "",
    },
    "zai": {
        "label": "Z.AI / GLM",
        "env_var": "GLM_API_KEY",
        "requires_base_url": False,
        "default_base_url": "",
    },
    "kimi-coding": {
        "label": "Kimi / Moonshot",
        "env_var": "KIMI_API_KEY",
        "requires_base_url": False,
        "default_base_url": "",
    },
    "huggingface": {
        "label": "HuggingFace",
        "env_var": "HF_TOKEN",
        "requires_base_url": False,
        "default_base_url": "",
    },
    "alibaba": {
        "label": "Alibaba / DashScope",
        "env_var": "DASHSCOPE_API_KEY",
        "requires_base_url": False,
        "default_base_url": "",
    },
    "meta-llama": {
        "label": "Meta Llama",
        "env_var": "METALLAMA_API_KEY",
        "requires_base_url": False,
        "default_base_url": "",
    },
    "ollama": {
        "label": "Ollama",
        "env_var": "OLLAMA_API_KEY",  # optional; local, may not need a key
        "requires_base_url": True,     # always needs explicit base_url
        "default_base_url": "http://localhost:11434",
    },
    "lmstudio": {
        "label": "LM Studio",
        "env_var": "LMSTUDIO_API_KEY",  # optional; local
        "requires_base_url": True,
        "default_base_url": "http://localhost:1234",
    },
    "opencode-zen": {
        "label": "OpenCode Zen",
        "env_var": "OPENCODE_ZEN_API_KEY",
        "requires_base_url": False,
        "default_base_url": "",
    },
    "opencode-go": {
        "label": "OpenCode Go",
        "env_var": "OPENCODE_GO_API_KEY",
        "requires_base_url": False,
        "default_base_url": "",
    },
    "mistralai": {
        "label": "Mistral",
        "env_var": "MISTRAL_API_KEY",
        "requires_base_url": False,
        "default_base_url": "",
    },
    "qwen": {
        "label": "Qwen",
        "env_var": "QWEN_API_KEY",
        "requires_base_url": False,
        "default_base_url": "",
    },
    "xiaomi": {
        "label": "Xiaomi MiMo",
        "env_var": "XIAOMI_API_KEY",
        "requires_base_url": False,
        "default_base_url": "",
    },
    "kilocode": {
        "label": "Kilo Code",
        "env_var": "KILOCODE_API_KEY",
        "requires_base_url": False,
        "default_base_url": "",
    },
    # OAuth/token-flow providers — not handled by the wizard's API-key flow.
    # _provider_oauth_authenticated() handles them separately via auth.json.
    "openai-codex": None,
    "copilot": None,
    "copilot-acp": None,
    "qwen-oauth": None,
    "nous": None,
    # Custom is always last and always requires a base_url.
    "custom": {
        "label": "Custom OpenAI-compatible",
        "env_var": "OPENAI_API_KEY",
        "requires_base_url": True,
        "default_base_url": "",
    },
}
```

**Key design decisions:**
- Providers whose env var is simply `{PROVIDER_ID.upper()}_API_KEY` don't need an entry — we compute it as the default.
- `None` entries explicitly mark OAuth/token-flow providers that the wizard cannot handle (they must use `hermes auth`).
- `ollama` and `lmstudio` set `requires_base_url=True` since they're local and need an explicit endpoint.
- `custom` remains as a manual endpoint fallback with `requires_base_url=True`.

---

### Task 2: Replace static `_SUPPORTED_PROVIDER_SETUPS` with a dynamic builder

**File:** `api/onboarding.py`

**Replace the static `_SUPPORTED_PROVIDER_SETUPS` dict (lines 31-63) with:**

```python
def _build_supported_provider_setups() -> dict:
    """
    Dynamically build the provider setup catalog from:
    - _PROVIDER_MODELS  (model lists, already in config.py)
    - _PROVIDER_DISPLAY (display names, already in config.py)
    - _PROVIDER_SETUP_METADATA (env_var, requires_base_url, default_base_url — local to this file)
    """
    # Import from config.py — safe at module level since config.py itself
    # doesn't import from onboarding.py (no circular dependency)
    from api.config import _PROVIDER_MODELS, _PROVIDER_DISPLAY

    setups = {}

    # First: any provider with an explicit metadata entry (includes the 4 original entries)
    for pid, meta in _PROVIDER_SETUP_METADATA.items():
        if meta is None:
            continue  # OAuth providers — skip

        env_var = meta.get("env_var") or f"{pid.upper()}_API_KEY"
        label = meta.get("label") or _PROVIDER_DISPLAY.get(pid, pid.title())

        # Pull models from _PROVIDER_MODELS if available, else use metadata models
        models = list(_PROVIDER_MODELS.get(pid, []))
        if not models:
            # Try to get from metadata (only openrouter has this pattern)
            models = meta.get("models", [])

        # Default model: first model in the list, or provider-specific fallback
        default_model = ""
        if models:
            default_model = models[0].get("id", "")

        setups[pid] = {
            "label": label,
            "env_var": env_var,
            "default_model": default_model,
            "requires_base_url": bool(meta.get("requires_base_url")),
            "default_base_url": meta.get("default_base_url", ""),
            "models": models,
        }

    # Second: any provider in _PROVIDER_MODELS that isn't in _PROVIDER_SETUP_METADATA.
    # Use defaults: env_var = f"{pid.upper()}_API_KEY", requires_base_url = False.
    for pid in _PROVIDER_MODELS:
        if pid in setups:
            continue
        label = _PROVIDER_DISPLAY.get(pid, pid.title())
        models = list(_PROVIDER_MODELS[pid])
        setups[pid] = {
            "label": label,
            "env_var": f"{pid.upper()}_API_KEY",
            "default_model": models[0].get("id", "") if models else "",
            "requires_base_url": False,
            "default_base_url": "",
            "models": models,
        }

    # Third: openrouter's special case — uses _FALLBACK_MODELS, not _PROVIDER_MODELS.
    # It may already be in _PROVIDER_MODELS (if added), but if not, add it with
    # _FALLBACK_MODELS as its model list.
    if "openrouter" not in setups:
        from api.config import _FALLBACK_MODELS
        setups["openrouter"] = {
            "label": "OpenRouter",
            "env_var": "OPENROUTER_API_KEY",
            "default_model": "anthropic/claude-sonnet-4.6",
            "requires_base_url": False,
            "default_base_url": "",
            "models": [{"id": m["id"], "label": m["label"]} for m in _FALLBACK_MODELS],
        }

    return setups


# Build once at import time
_SUPPORTED_PROVIDER_SETUPS = _build_supported_provider_setups()
```

**Verification:** After this change, `len(_SUPPORTED_PROVIDER_SETUPS)` should be ≥ 20 (all API-key providers), not 4.

---

### Task 3: Update `_build_setup_catalog()` to include all providers

**File:** `api/onboarding.py`

**Current code** (line 364):
```python
for provider_id, meta in _SUPPORTED_PROVIDER_SETUPS.items():
```

This already iterates all entries — no code change needed. But now `_SUPPORTED_PROVIDER_SETUPS` is dynamically built, so the catalog will include all providers automatically.

**However**, add a `quick` field to only the top 3 most common providers (openrouter, anthropic, openai) so the frontend can show a "Quick setup" badge without hardcoding:
```python
_quick_setups = {"openrouter", "anthropic", "openai"}

for provider_id, meta in _SUPPORTED_PROVIDER_SETUPS.items():
    providers.append({
        "id": provider_id,
        "label": meta["label"],
        "env_var": meta["env_var"],
        "default_model": meta["default_model"],
        "default_base_url": meta.get("default_base_url") or "",
        "requires_base_url": bool(meta.get("requires_base_url")),
        "models": list(meta.get("models", [])),
        "quick": provider_id in _quick_setups,
    })
```

---

### Task 4: Fix `apply_onboarding_setup()` to remove the silent no-op

**File:** `api/onboarding.py`

**Current code** (line 468):
```python
if provider not in _SUPPORTED_PROVIDER_SETUPS:
    save_settings({"onboarding_completed": True})
    return get_onboarding_status()
```

**Problem:** This is why users with deepseek/minimax/etc. configured via env vars see the wizard on every page load — `apply_onboarding_setup()` silently accepts any provider not in the hardcoded 4, but `apply_onboarding_setup()` is never called for those users (they skip past the wizard), and even if it were, it wouldn't write their config.

**Fix — replace the guard with:**
```python
if provider not in _SUPPORTED_PROVIDER_SETUPS:
    # Unknown provider — may be OAuth-based (openai-codex, copilot, nous, etc.)
    # or a provider we simply don't have setup metadata for.
    # These must have been configured via CLI already or they can't work.
    # Just mark onboarding complete and let them through.
    save_settings({"onboarding_completed": True})
    return get_onboarding_status()
```

**Wait — this is already what it says.** The real issue is that `_SUPPORTED_PROVIDER_SETUPS` is too small. With Task 2, the guard will pass for all API-key providers.

**But add this safeguard** right after the guard, to handle providers that are in `_SUPPORTED_PROVIDER_SETUPS` but don't have models yet (edge case):
```python
provider_meta = _SUPPORTED_PROVIDER_SETUPS.get(provider)
if not provider_meta:
    save_settings({"onboarding_completed": True})
    return get_onboarding_status()
```

**Also update the model normalization logic** (currently only handles `anthropic` and `openai`):
```python
# Current (line 145-151):
def _normalize_model_for_provider(provider: str, model: str) -> str:
    clean = (model or "").strip()
    if not clean:
        return ""
    if provider in {"anthropic", "openai"} and clean.startswith(provider + "/"):
        return clean.split("/", 1)[1]
    return clean
```

**Replace with:**
```python
def _normalize_model_for_provider(provider: str, model: str) -> str:
    """Strip provider prefix from model IDs for providers that don't use it."""
    clean = (model or "").strip()
    if not clean:
        return ""
    # Known providers that use provider/model format in their API
    _uses_provider_prefix = {
        "openrouter", "openai-codex", "google", "gemini",
        "deepseek", "huggingface", "kimi-coding", "x-ai",
        "zai", "meta-llama", "alibaba", "qwen", "mistralai",
    }
    if provider in _uses_provider_prefix and "/" in clean:
        return clean.split("/", 1)[1]
    return clean
```

This prevents `deepseek/deepseek-chat-v3-0324` from being sent as-is to a provider that expects just `deepseek-chat-v3-0324`.

---

### Task 5: Update `_provider_api_key_present()` for completeness

**File:** `api/onboarding.py`

The function at line 183 already has a fallback (lines 220-227) that calls `hermes_cli.auth.get_auth_status()` for providers not in `_SUPPORTED_PROVIDER_SETUPS`. With Tasks 1-2, most providers will be in `_SUPPORTED_PROVIDER_SETUPS` and take the fast path.

**No code change needed**, but verify that `hermes_cli.auth.get_auth_status()` covers all providers we added. If it does not, the fallback handles it. This is safe.

---

### Task 6: Update `_UNSUPPORTED_PROVIDER_NOTE`

**File:** `api/onboarding.py`

The note at lines 65-68 is misleading once all providers are in the wizard:

**Current:**
```python
_UNSUPPORTED_PROVIDER_NOTE = (
    "OAuth and advanced provider flows such as Nous Portal, OpenAI Codex, and GitHub "
    "Copilot are still terminal-first. Use `hermes model` for those flows."
)
```

**Replace with:**
```python
_UNSUPPORTED_PROVIDER_NOTE = (
    "OAuth-based providers (Nous Portal, OpenAI Codex, GitHub Copilot, Qwen OAuth) "
    "must be configured via `hermes auth` or `hermes model` in a terminal first."
)
```

---

### Task 7: Update `static/onboarding.js` — No changes needed

The frontend already reads from `_build_setup_catalog()` which now returns all providers. The dropdown will automatically show all entries. The `syncOnboardingProvider()` function handles switching between providers including `custom` (base_url field). No JS changes required.

**Exception:** If `ollama` or `lmstudio` are selected and `requires_base_url=True`, the frontend shows the base_url field automatically via `showBaseUrl` (line 113). This already works — no change needed.

---

## File Changes Summary

| File | Changes |
|------|---------|
| `api/onboarding.py` | Task 1: `_PROVIDER_SETUP_METADATA` table · Task 2: `_build_supported_provider_setups()` builder · Task 3: add `quick` field in catalog · Task 4: fix `_normalize_model_for_provider()` + safeguard · Task 6: update `_UNSUPPORTED_PROVIDER_NOTE` |
| `static/onboarding.js` | None needed |

---

## Verification Steps

1. **Start the webui with only `DEEPSEEK_API_KEY` set** (no config.yaml). Open the onboarding wizard. DeepSeek should appear as a selectable provider with its models. Complete the wizard. Verify `config.yaml` has `provider: deepseek`.

2. **Start with `MINIMAX_API_KEY` + `OLLAMA_BASE_URL`** (ollama running locally). Verify MiniMax AND Ollama appear in the wizard. Complete setup for Ollama. Verify `config.yaml` has `provider: ollama` with `base_url: http://localhost:11434`.

3. **With `HERMES_WEBUI_SKIP_ONBOARDING=1`**, verify the wizard is skipped even when no provider is configured.

4. **With an OAuth provider already authenticated** (e.g. copilot via `hermes auth`), verify the wizard shows the OAuth confirmation card and does NOT ask for an API key.

5. **Run the test suite:**
   ```bash
   python -m pytest tests/test_onboarding.py -v
   python -m pytest tests/test_model_resolver.py -v
   python -m pytest tests/ -q --tb=short
   ```

---

## Interaction with the Original Plan (`ALL_PROVIDERS_PLAN.md`)

The original plan focused on `api/config.py` (Tasks 1-3) and frontend icon rendering (Task 6). This revised plan **supersedes the onboarding-related portions** but is orthogonal to the config.py changes:

- **Do Tasks 1-3 of `ALL_PROVIDERS_PLAN.md`** (config.py provider display, model lists, env var detection) — they are correct and needed.
- **Skip Task 6** (provider icons) unless you want icons too.
- **Do Tasks 1-4, 6 of THIS document** (onboarding.py).
- The combined result: all providers appear in both the runtime model dropdown AND the onboarding wizard.
