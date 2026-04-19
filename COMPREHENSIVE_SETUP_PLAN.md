# Comprehensive WebUI Setup —对齐 Hermes CLI Setup

> **Goal:** Every `hermes setup` section is accessible from the WebUI.
> Reference: `hermes_cli/config.py` DEFAULT_CONFIG dict (~lines 347–780).

---

## Config File Layout

| File | Purpose | Managed by |
|------|---------|------------|
| `~/.hermes/config.yaml` | Agent config — model, provider, terminal, TTS, display, compression, delegation, browser, checkpoints, logging, per-platform settings | WebUI + CLI (shared) |
| `~/.hermes/.env` | Secrets — API keys, bot tokens, SMTP credentials, etc. | WebUI + CLI (shared) |
| `~/.hermes-webui/state/settings.json` | WebUI-only: bot_name, default_workspace, onboarding_completed, password_hash, language | WebUI only |

**Principle:** `config.yaml` is the single source of truth for everything the agent needs at runtime. The WebUI writes to it; the CLI reads it.

---

## Config Fields Reference

Exact field paths from `hermes_cli/config.py` DEFAULT_CONFIG:

```
agent:
  max_turns: 90              # iteration cap
  gateway_timeout: 1800       # seconds; 0 = unlimited
  restart_drain_timeout: 60
  tool_use_enforcement: "auto"
  gateway_timeout_warning: 900
  gateway_notify_interval: 600

terminal:
  backend: "local"           # local | docker | modal | ssh | daytona | singularity
  cwd: "."
  timeout: 180
  docker_image: "nikolaik/python-nodejs:python3.11-nodejs20"
  docker_volumes: []
  docker_mount_cwd_to_workspace: False
  persistent_shell: True
  container_cpu: 1
  container_memory: 5120      # MB
  container_disk: 51200       # MB
  container_persistent: True
  env_passthrough: []
  docker_env: {}
  sandbox_dir: ""            # working dir inside container/singularity
  modal_mode: "auto"         # auto | native | emulator (Modal only)
  singularity_image: "docker://nikolaik/python-nodejs:python3.11-nodejs20"
  modal_image: "nikolaik/python-nodejs:python3.11-nodejs20"
  daytona_image: "nikolaik/python-nodejs:python3.11-nodejs20"
  # SSH backend — host/user/key/port stored as env vars, NOT in config.yaml
  # TERMINAL_SSH_HOST, TERMINAL_SSH_USER, TERMINAL_SSH_KEY, TERMINAL_SSH_PORT

display:
  compact: False
  personality: "kawaii"     # kawaii | neutral
  resume_display: "full"
  busy_input_mode: "interrupt"
  bell_on_complete: False
  show_reasoning: False
  streaming: False
  inline_diffs: True
  show_cost: False
  skin: "default"
  interim_assistant_messages: True
  tool_progress_command: False
  tool_preview_length: 0
  platforms: {}              # per-platform overrides

compression:
  enabled: True
  threshold: 0.50
  target_ratio: 0.20
  protect_last_n: 20

delegation:
  model: ""                  # e.g. "google/gemini-3-flash-preview"
  provider: ""
  base_url: ""
  api_key: ""
  max_iterations: 50
  reasoning_effort: ""        # xhigh | high | medium | low | minimal | none

browser:
  inactivity_timeout: 120
  command_timeout: 30
  record_sessions: False
  allow_private_urls: False

checkpoints:
  enabled: True
  max_snapshots: 50

tts:
  provider: "edge"           # edge | elevenlabs | openai | minimax | mistral | gemini | neutts | xai
  elevenlabs:
    voice_id: ""
    model_id: "eleven_v3"
  openai:
    voice: "alloy"           # alloy | echo | shimmer | ...
    model: "gpt-4o-mini-tts"
  minimax:
    voice_id: ""
    model: "speech-02-hd"
  mistral:
    voice: "sapphire"        # sapphire | mistral | echo | ...
    model: "voxtral-mini-tts-2603"
  gemini:
    model: "gemini-2.5-flash-preview-05-20"
  neutts:
    model: "neuphonic/neutts-air-q4-gguf"

voice:
  record_key: "ctrl+b"
  max_recording_seconds: 120
  auto_tts: False
  silence_threshold: 200
  silence_duration: 3.0

logging:
  level: "INFO"              # DEBUG | INFO | WARNING
  max_size_mb: 5
  backup_count: 3

network:
  force_ipv4: False

cron:
  wrap_response: True

messaging (Discord example):
  require_mention: True
  free_response_channels: ""
  allowed_channels: ""
  auto_thread: True
  reactions: True
  channel_prompts: {}
```

---

## What the WebUI currently has

| WebUI | Equivalent in CLI |
|-------|-------------------|
| ✅ Provider + API key + model (onboarding) | ✅ (partial — only 4 providers) |
| ✅ Default model + workspace (onboarding) | ❌ |
| ✅ Password (onboarding) | ❌ (WebUI-only) |
| ❌ Agent settings | ❌ (present in CLI, missing in WebUI) |
| ❌ Terminal backend | ❌ |
| ❌ TTS | ❌ |
| ❌ Voice recording | ❌ |
| ❌ Display / skin | ❌ |
| ❌ Compression | ❌ |
| ❌ Delegation / subagents | ❌ |
| ❌ Browser settings | ❌ |
| ❌ Checkpoints | ❌ |
| ❌ Logging | ❌ |
| ❌ Network | ❌ |
| ❌ Messaging platforms | ❌ |
| ❌ Tools configuration | ❌ |

---

## Tasks

### Phase 1: All-Provider Onboarding (pre-requisite)

**Do first:** All tasks in `ONBOARDING_PROVIDERS_PLAN.md`. This fixes the provider dropdown bottleneck. Without it, no other setup section can select non-4-providers.

---

### Phase 2: Settings Page Shell

Before adding individual sections, create a unified Settings page accessible from the nav bar.

#### 2a. Add nav item

**File:** `static/app.js`

Add a "Settings" gear icon/link in the top nav bar.

#### 2b. Settings HTML page

**File:** `static/settings.html` (new)

Single-page settings UI with tabbed or accordion sections:

```
┌─────────────────────────────────────────────┐
│  Settings                            [Save] │
├─────────────────────────────────────────────┤
│  [Agent] [TTS] [Terminal] [Display] [Tools] │
│  [Messaging] [Logging]                      │
├─────────────────────────────────────────────┤
│  (content of selected section)             │
│                                             │
└─────────────────────────────────────────────┘
```

Each section is a `<div id="settings-section-{name}">` shown/hidden by tab. All sections load their initial state via GET on mount.

#### 2c. Settings GET endpoint

**File:** `api/routes.py`

Add a single `GET /api/settings/all` endpoint that returns all configurable state in one response — the frontend can use whichever sections it needs:

```python
@route("/api/settings/all")
def handle_settings_all(handler, parsed, body):
    cfg = get_config()
    env = _load_env_file(_get_active_hermes_home() / ".env")

    return json({
        "agent": {
            "max_turns": cfg.get("agent", {}).get("max_turns", 90),
            "gateway_timeout": cfg.get("agent", {}).get("gateway_timeout", 1800),
            "tool_use_enforcement": cfg.get("agent", {}).get("tool_use_enforcement", "auto"),
            "gateway_timeout_warning": cfg.get("agent", {}).get("gateway_timeout_warning", 900),
            "gateway_notify_interval": cfg.get("agent", {}).get("gateway_notify_interval", 600),
        },
        "delegation": dict(cfg.get("delegation", {})),
        "compression": dict(cfg.get("compression", {})),
        "terminal": _terminal_settings(cfg),
        "tts": dict(cfg.get("tts", {})),
        "voice": dict(cfg.get("voice", {})),
        "display": dict(cfg.get("display", {})),
        "browser": dict(cfg.get("browser", {})),
        "checkpoints": dict(cfg.get("checkpoints", {})),
        "logging": dict(cfg.get("logging", {})),
        "network": dict(cfg.get("network", {})),
        "messaging": _messaging_status(cfg, env),
        "tools": _tools_status(cfg, env),
    })
```

---

### Phase 3: Agent Settings

#### 3a. Fields

`config.yaml` path: `agent.*`

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `max_turns` | int | 90 | Per-conversation iteration cap |
| `gateway_timeout` | int | 1800 | Seconds; 0 = unlimited |
| `restart_drain_timeout` | int | 60 | Graceful drain on restart |
| `tool_use_enforcement` | str | "auto" | "auto" \| true \| false |
| `gateway_timeout_warning` | int | 900 | Seconds; 0 = disable |
| `gateway_notify_interval` | int | 600 | Seconds; 0 = disable |

#### 3b. UI

```
Agent
├── Max turns per conversation: [90________]
├── Gateway timeout (seconds): [1800_______]  0 = unlimited
├── Restart drain timeout (seconds): [60_____]
├── Tool use enforcement: [Auto ▾]
├── Timeout warning (seconds): [900_______]  0 = disable
├── Notify interval (seconds): [600_______]  0 = disable
```

---

### Phase 4: Delegation / Subagent Settings

#### 4a. Fields

`config.yaml` path: `delegation.*`

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `model` | str | "" | Model for subagents (e.g. `google/gemini-3-flash-preview`); empty = inherit |
| `provider` | str | "" | Provider for subagents; empty = inherit |
| `base_url` | str | "" | OpenAI-compatible endpoint for subagents |
| `api_key` | str | "" | API key for base_url; falls back to OPENAI_API_KEY |
| `max_iterations` | int | 50 | Per-subagent iteration cap |
| `reasoning_effort` | str | "" | xhigh \| high \| medium \| low \| minimal \| none; empty = inherit |

#### 4b. UI

```
Subagent Delegation
├── Subagent model: [google/gemini-3-flash-preview____]
│   (leave empty to inherit parent model)
├── Subagent provider: [openrouter___________________]
│   (leave empty to inherit parent provider)
├── Base URL: [https://...________________________]
│   (for custom OpenAI-compatible endpoints)
├── API Key: [____________________________________]
│   (for custom base_url; falls back to OPENAI_API_KEY)
├── Max iterations: [50_______]
└── Reasoning effort: [Inherit ▾]  (xhigh | high | medium | low | minimal | none)
```

---

### Phase 5: Compression

#### 5a. Fields

`config.yaml` path: `compression.*`

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `enabled` | bool | True | |
| `threshold` | float | 0.50 | Compress when context exceeds this ratio |
| `target_ratio` | float | 0.20 | Fraction of threshold to preserve as recent tail |
| `protect_last_n` | int | 20 | Minimum recent messages to keep uncompressed |

#### 5b. UI

```
Context Compression
├── [✓] Enable automatic compression
├── Compression threshold: [0.50____]
│   (0.0–1.0; compress when context usage exceeds this ratio)
├── Target ratio: [0.20____]
│   (fraction of threshold to preserve as recent tail)
└── Protect last N messages: [20_____]
```

---

### Phase 6: TTS

#### 6a. TTS API Key Auto-Detection

TTS providers often share API keys with the main model provider (e.g. MiniMax uses the same `MINIMAX_API_KEY` for both chat and TTS; `OPENAI_API_KEY` covers both model and OpenAI TTS; `GEMINI_API_KEY` / `GOOGLE_API_KEY` covers both). The TTS settings page must pre-fill API key fields by reading existing env vars, so users don't enter the same key twice.

**Pre-existing key detection priority per provider:**

| TTS Provider | Env var(s) to check | Notes |
|---|---|---|
| edge | — | No key needed |
| elevenlabs | `ELEVENLABS_API_KEY` | |
| openai | `VOICE_TOOLS_OPENAI_KEY` → `OPENAI_API_KEY` | `VOICE_TOOLS_OPENAI_KEY` takes precedence; falls back to `OPENAI_API_KEY` |
| xai | `XAI_API_KEY` | |
| minimax | `MINIMAX_API_KEY` | **Same key used for model provider** |
| mistral | `MISTRAL_API_KEY` | |
| gemini | `GEMINI_API_KEY` → `GOOGLE_API_KEY` | `GEMINI_API_KEY` takes precedence |
| neutts | — | No key needed; local model |

**GET /api/tts response** should include detected pre-existing keys (masked):

```python
{
    "provider": "minimax",
    "voices": [...],
    "config": {
        "minimax": {
            "voice_id": "speech-02-hd",
            "model": "speech-02-hd",
        },
    },
    # Per-provider key detection — frontend uses these to pre-fill/lock fields
    "detected_keys": {
        "elevenlabs":   {"var": "ELEVENLABS_API_KEY",   "has": False},
        "openai":       {"var": "VOICE_TOOLS_OPENAI_KEY","has": False},  # or OPENAI_API_KEY
        "xai":          {"var": "XAI_API_KEY",          "has": False},
        "minimax":      {"var": "MINIMAX_API_KEY",       "has": True},   # auto-detected!
        "mistral":      {"var": "MISTRAL_API_KEY",       "has": False},
        "gemini":       {"var": "GEMINI_API_KEY",        "has": False},  # or GOOGLE_API_KEY
    },
    # If a key is already set, include a locked indicator so the UI shows:
    # "MiniMax API Key: ●●●●●●●●●●●●●●● (already configured for model provider)"
    "key_status": {
        "minimax": "in_use",    # in_use | separate | none
        # If key came from model provider, label it as "shared with model provider"
        # If user explicitly set a separate TTS key, label as "separate"
    }
}
```

**Key sharing UX pattern:**

When `MINIMAX_API_KEY` is detected and MiniMax TTS is selected:
- Show the masked key field as `[●●●●●●●●●●●●●●●●●●●●●●] (shared with model provider — already configured)`
- Disable editing (grey out) with a lock icon
- Below it: small note "This key is also used for chat. To use a different key for TTS, remove it from the Model Provider settings first."
- "Test Voice" button still works with the shared key

When a key is detected but the user wants to override it for TTS only:
- No override allowed at the TTS level — the key is shared
- Direct user to model provider settings to use a separate key, or use a different TTS provider

#### 6b. Fields

`config.yaml` path: `tts.*`

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `provider` | str | "edge" | edge \| elevenlabs \| openai \| minimax \| mistral \| gemini \| neutts \| xai |
| `elevenlabs.voice_id` | str | "" | |
| `elevenlabs.model_id` | str | "eleven_v3" | |
| `openai.voice` | str | "alloy" | alloy \| echo \| shimmer \| ... |
| `openai.model` | str | "gpt-4o-mini-tts" | |
| `minimax.voice_id` | str | "" | |
| `minimax.model` | str | "speech-02-hd" | |
| `mistral.voice` | str | "sapphire" | sapphire \| mistral \| echo \| ... |
| `mistral.model` | str | "voxtral-mini-tts-2603" | |
| `gemini.model` | str | "gemini-2.5-flash-preview-05-20" | |
| `xai.model` | str | "grok" | Model for xAI TTS |
| `neutts.model` | str | "neuphonic/neutts-air-q4-gguf" | HuggingFace model repo |

#### 6c. TTS Provider Selection UI

```
Text-to-Speech
Provider: [MiniMax TTS ▾]  (Edge | ElevenLabs | OpenAI | xAI | MiniMax | Mistral | Gemini | NeuTTS)

[If ElevenLabs:]
  Voice ID: [____________________________]  (e.g. Rachel, ARIA)
  Model:    [eleven_v3 ▾]  (eleven_v3 | eleven_v2 | ...)  [✓] Has API key: ●●●●●●●●●●●●●●●●

[If OpenAI:]
  Voice:   [Alloy ▾]  (Alloy | Echo | Shimmer | Nova | Fable | Onyx)
  Model:   [gpt-4o-mini-tts ▾]
           [✓] Has API key: ●●●●●●●●●●●●●●●● (VOICE_TOOLS_OPENAI_KEY)

[If xAI:]
  Model:   [grok ▾]
           [✓] Has API key: ●●●●●●●●●●●●●●●● (XAI_API_KEY)

[If MiniMax:]
  Voice:   [Speech 02 HD ▾]  (Speech 02 HD | Speech 02 | Speech 01)
  Model:   [speech-02-hd ▾]
           🔒 API key: ●●●●●●●●●●●●●●●● (MINIMAX_API_KEY — shared with model provider)
              This key is also used for chat. To use a different key for TTS,
              remove it from the Model Provider settings first.

[If Mistral:]
  Voice:   [Sapphire ▾]  (Sapphire | Mistral | Echo | Alloy)
  Model:   [voxtral-mini-tts-2603 ▾]
           [✓] Has API key: ●●●●●●●●●●●●●●●● (MISTRAL_API_KEY)

[If Gemini:]
  Model:   [gemini-2.5-flash-preview-05-20 ▾]
           [✓] Has API key: ●●●●●●●●●●●●●●●● (GEMINI_API_KEY)

[If NeuTTS:]
  Model:   [neuphonic/neutts-air-q4-gguf ▾]  (HuggingFace model repo)
  (no API key needed — runs locally)

[If Edge:]
  Voice:   [Aria (US English) ▾]  (Aria | Sonia | Katja | Denise | Nanami | Xiaoxiao)
  (no API key needed)

[ ] Save
[Test Voice]  ← plays a short sample using current settings
```

The provider dropdown drives which provider-specific fields are shown. The "Has API key" indicator is driven by `detected_keys` from GET /api/tts. When `key_status` is `"shared"`, the API key field is replaced with the locked indicator and editing is disabled.

#### 6d. Voice catalog

**File:** `api/config.py`

```python
_TTS_VOICES = {
    "edge": [
        {"id": "en-US-AriaNeural", "label": "Aria (US English)"},
        {"id": "en-GB-SoniaNeural", "label": "Sonia (UK English)"},
        {"id": "de-DE-KatjaNeural", "label": "Katja (German)"},
        {"id": "fr-FR-DeniseNeural", "label": "Denise (French)"},
        {"id": "ja-JP-NanamiNeural", "label": "Nanami (Japanese)"},
        {"id": "zh-CN-XiaoxiaoNeural", "label": "Xiaoxiao (Mandarin)"},
    ],
    "elevenlabs": [],  # Fetch dynamically or use common defaults
    "openai": [
        {"id": "alloy", "label": "Alloy"},
        {"id": "echo", "label": "Echo"},
        {"id": "shimmer", "label": "Shimmer"},
        {"id": "nova", "label": "Nova"},
        {"id": "fable", "label": "Fable"},
        {"id": "onyx", "label": "Onyx"},
    ],
    "minimax": [
        {"id": "speech-02-hd", "label": "Speech 02 HD"},
        {"id": "speech-02", "label": "Speech 02"},
        {"id": "speech-01", "label": "Speech 01"},
    ],
    "mistral": [
        {"id": "sapphire", "label": "Sapphire"},
        {"id": "mistral", "label": "Mistral"},
        {"id": "echo", "label": "Echo"},
        {"id": "alloy", "label": "Alloy"},
    ],
    "xai": [
        {"id": "grok", "label": "Grok"},
    ],
    "gemini": [
        {"id": "gemini-2.5-flash-preview-05-20", "label": "Gemini 2.5 Flash (default)"},
    ],
    "neutts": [
        {"id": "neuphonic/neutts-air-q4-gguf", "label": "Neutts Air (local GGUF)"},
    ],
}

_TTS_PROVIDERS = ["edge", "elevenlabs", "openai", "xai", "minimax", "mistral", "gemini", "neutts"]
```

#### 6e. GET / POST endpoints

**File:** `api/routes.py`

```python
@route("/api/tts")
def handle_tts(handler, parsed, body):
    cfg = get_config()
    tts = cfg.get("tts", {})
    provider = tts.get("provider", "edge")
    voices = _TTS_VOICES.get(provider, [])

    if handler.command == "GET":
        env = _load_env_file(_get_active_hermes_home() / ".env")

        def _key_has(env, var):
            return var in env and bool(env[var])

        def _mask_key(key):
            if not key:
                return ""
            if len(key) <= 8:
                return key[:3] + "****"
            return key[:4] + "****" + key[-4:]

        # Determine key sharing status per provider
        # A TTS key is "in_use" when the same var is also set at the model provider level
        # (i.e., same key used for both chat and TTS)
        model_provider = cfg.get("provider", "").lower()
        model_api_key_var = ""
        if model_provider == "minimax":
            model_api_key_var = "MINIMAX_API_KEY"
        elif model_provider in ("openai", "openrouter"):
            model_api_key_var = "OPENAI_API_KEY"
        elif model_provider == "google":
            model_api_key_var = "GOOGLE_API_KEY"
        elif model_provider == "xai":
            model_api_key_var = "XAI_API_KEY"
        elif model_provider == "mistral":
            model_api_key_var = "MISTRAL_API_KEY"
        elif model_provider == "deepseek":
            model_api_key_var = "DEEPSEEK_API_KEY"

        detected_keys = {
            "elevenlabs": {
                "var": "ELEVENLABS_API_KEY",
                "has": _key_has(env, "ELEVENLABS_API_KEY"),
                "masked": _mask_key(env.get("ELEVENLABS_API_KEY", "")),
            },
            "openai": {
                "var": "VOICE_TOOLS_OPENAI_KEY",  # primary for TTS
                "secondary_var": "OPENAI_API_KEY",  # fallback
                "has": _key_has(env, "VOICE_TOOLS_OPENAI_KEY") or _key_has(env, "OPENAI_API_KEY"),
                "masked": _mask_key(env.get("VOICE_TOOLS_OPENAI_KEY") or env.get("OPENAI_API_KEY", "")),
                "is_separate_tts_key": _key_has(env, "VOICE_TOOLS_OPENAI_KEY"),
            },
            "xai": {
                "var": "XAI_API_KEY",
                "has": _key_has(env, "XAI_API_KEY"),
                "masked": _mask_key(env.get("XAI_API_KEY", "")),
            },
            "minimax": {
                "var": "MINIMAX_API_KEY",
                "has": _key_has(env, "MINIMAX_API_KEY"),
                "masked": _mask_key(env.get("MINIMAX_API_KEY", "")),
                "shared_with_model": model_api_key_var == "MINIMAX_API_KEY",
            },
            "mistral": {
                "var": "MISTRAL_API_KEY",
                "has": _key_has(env, "MISTRAL_API_KEY"),
                "masked": _mask_key(env.get("MISTRAL_API_KEY", "")),
                "shared_with_model": model_api_key_var == "MISTRAL_API_KEY",
            },
            "gemini": {
                "var": "GEMINI_API_KEY",
                "secondary_var": "GOOGLE_API_KEY",
                "has": _key_has(env, "GEMINI_API_KEY") or _key_has(env, "GOOGLE_API_KEY"),
                "masked": _mask_key(env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY", "")),
                "shared_with_model": model_api_key_var in ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
            },
        }

        # key_status: how the current TTS provider's key relates to the model provider key
        key_status = {}
        if provider in detected_keys:
            dk = detected_keys[provider]
            if dk.get("has"):
                if dk.get("shared_with_model"):
                    key_status[provider] = "shared"  # same key used for model + TTS
                elif dk.get("is_separate_tts_key"):
                    key_status[provider] = "separate"  # VOICE_TOOLS_OPENAI_KEY vs OPENAI_API_KEY
                else:
                    key_status[provider] = "standalone"
            else:
                key_status[provider] = "none"

        return json({
            "provider": provider,
            "voices": voices,
            "config": {
                "elevenlabs": tts.get("elevenlabs", {}),
                "openai": tts.get("openai", {}),
                "minimax": tts.get("minimax", {}),
                "mistral": tts.get("mistral", {}),
                "gemini": tts.get("gemini", {}),
                "xai": tts.get("xai", {}),
                "neutts": tts.get("neutts", {}),
            },
            "detected_keys": detected_keys,
            "key_status": key_status,
        })

    if handler.command == "POST":
        provider = body.get("provider", "edge")
        if provider not in _TTS_PROVIDERS:
            return json({"error": f"provider must be one of: {', '.join(_TTS_PROVIDERS)}"}, status=400)

        # Separate API keys (go to .env) from config values (go to config.yaml)
        _TTS_API_KEYS = {
            "elevenlabs": "ELEVENLABS_API_KEY",
            "openai": "VOICE_TOOLS_OPENAI_KEY",  # separate from OPENAI_API_KEY used for model
            "xai": "XAI_API_KEY",
            "minimax": "MINIMAX_API_KEY",
            "mistral": "MISTRAL_API_KEY",
            "gemini": "GEMINI_API_KEY",
        }

        env = _load_env_file(_get_active_hermes_home() / ".env")
        tts_nested = {}  # non-secret config for config.yaml

        for p, env_var in _TTS_API_KEYS.items():
            if p in body and body[p].get("api_key"):
                env[env_var] = body[p].pop("api_key")  # move api_key → .env, keep rest

        for p in _TTS_PROVIDERS:
            if p in body:
                tts_nested[p] = body[p]

        # Write config.yaml: tts.provider + per-provider non-secret config
        cfg["tts"] = {"provider": provider}
        cfg["tts"].update(tts_nested)
        _save_yaml_config(_get_config_path(), cfg)

        # Write .env: API keys only
        _write_env_file(_get_active_hermes_home() / ".env", env)

        reload_config()
        return json({"ok": True})
```

#### 6f. Test TTS button

**File:** `api/routes.py`

```python
@route("/api/tts/test")
def handle_tts_test(handler, parsed, body):
    # POST: { "text": "Hello, this is a test." }
    # Uses hermes_cli TTS engine or direct API call
    # Returns audio/ogg stream or mp3
    # Falls back to 501 if provider not implemented
```

The test endpoint calls the actual TTS engine (edge tts is free, others need keys). Edge TTS can be called via `edge_tts` Python library. For other providers, check if hermes_cli exposes a TTS test function.

#### 6g. Voice recording settings

`config.yaml` path: `voice.*`

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `record_key` | str | "ctrl+b" | Keyboard shortcut for voice recording |
| `max_recording_seconds` | int | 120 | |
| `auto_tts` | bool | False | Auto-read responses aloud |
| `silence_threshold` | int | 200 | RMS threshold (0–32767) |
| `silence_duration` | float | 3.0 | Seconds of silence before auto-stop |

```
Voice Recording
├── Voice recording hotkey: [ctrl+b_________]
├── Max recording duration (seconds): [120_____]
├── [ ] Auto-play TTS for agent responses
├── Silence threshold: [200_______]  0–32767
└── Silence duration before stop (seconds): [3.0____]
```

---

### Phase 7: Terminal Backend

#### 7a. Fields

`config.yaml` path: `terminal.*`

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `backend` | str | "local" | local \| docker \| modal \| ssh \| daytona \| singularity |
| `cwd` | str | "." | Working directory |
| `timeout` | int | 180 | Command timeout (seconds) |
| `persistent_shell` | bool | True | Keep shell across calls |
| `docker_image` | str | "nikolaik/..." | Docker only |
| `docker_volumes` | list | [] | Docker only: host:container mount pairs |
| `docker_mount_cwd_to_workspace` | bool | False | Docker only |
| `docker_env` | dict | {} | Docker only: exact key-value pairs injected into container |
| `container_cpu` | int | 1 | Docker/modal/daytona/singularity |
| `container_memory` | int | 5120 | MB — Docker/modal/daytona/singularity |
| `container_disk` | int | 51200 | MB — Docker/modal/daytona/singularity |
| `container_persistent` | bool | True | Keep container alive between calls |
| `env_passthrough` | list | [] | Env vars to pass through from host |
| `sandbox_dir` | str | "" | Working dir inside container/singularity |
| `modal_mode` | str | "auto" | auto \| native \| emulator — Modal only |
| `singularity_image` | str | "docker://..." | Singularity only |
| `modal_image` | str | "nikolaik/..." | Modal only |
| `daytona_image` | str | "nikolaik/..." | Daytona only |

SSH credentials — stored in `.env` (not config.yaml):

| Field | Env var | Notes |
|-------|---------|-------|
| SSH host | `TERMINAL_SSH_HOST` | |
| SSH user | `TERMINAL_SSH_USER` | |
| SSH key path | `TERMINAL_SSH_KEY` | Path to private key file |
| SSH port | `TERMINAL_SSH_PORT` | Optional; defaults to 22 |

#### 7b. UI

```
Terminal Backend
├── Backend: [Local ▾]  (Local | Docker | Modal | SSH | Daytona | Singularity)
├── Working directory: [./____________________________]
└── Timeout (seconds): [180_______]

[If Docker:]
├── Docker image: [nikolaik/python-nodejs:python3.11-nodejs20____]
├── [ ] Mount current working directory to /workspace
├── Docker volumes (one per line, host:container):
│   [________________________________________________]
├── Docker env vars (KEY=value, one per line):
│   [________________________________________________]
├── Container CPU: [1___] cores
├── Container memory: [5120___] MB
├── Container disk: [51200___] MB
├── [✓] Persistent shell
└── Env passthrough (comma-separated var names):
    [________________________________________________]

[If Modal:]
├── Modal image: [nikolaik/python-nodejs:python3.11-nodejs20____]
├── Modal mode: [Auto ▾]  (Auto | Native | Emulator)
├── Container CPU: [1___] cores
├── Container memory: [5120___] MB
└── Container disk: [51200___] MB

[If Singularity:]
├── Singularity image: [docker://nikolaik/python-nodejs:python3.11-nodejs20____________]
├── Working directory inside container: [________________________]
├── Container CPU: [1___] cores
├── Container memory: [5120___] MB
└── Container disk: [51200___] MB

[If Daytona:]
├── Daytona image: [nikolaik/python-nodejs:python3.11-nodejs20____]
├── Container CPU: [1___] cores
├── Container memory: [5120___] MB
└── Container disk: [51200___] MB

[If SSH:]
├── Host: [my-server.com___________________________]
├── User: [ubuntu_________________________________]
├── SSH key path: [~/.ssh/id_rsa___________________]
├── Port: [22____]  (leave empty for default)
└── Timeout (seconds): [180_______]

[If Local:]
(no backend-specific options)
```

**SSH credentials:** `TERMINAL_SSH_HOST`, `TERMINAL_SSH_USER`, `TERMINAL_SSH_KEY`, `TERMINAL_SSH_PORT` are written to `~/.hermes/.env` (not `config.yaml`) using `_write_env_file()`. This mirrors how `hermes setup terminal` stores them.

---

### Phase 8: Display / Skin

#### 8a. Fields

`config.yaml` path: `display.*`

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `compact` | bool | False | |
| `personality` | str | "kawaii" | kawaii \| neutral |
| `resume_display` | str | "full" | full \| compact |
| `busy_input_mode` | str | "interrupt" | interrupt \| queue |
| `bell_on_complete` | bool | False | Terminal bell on completion |
| `show_reasoning` | bool | False | Show reasoning content |
| `streaming` | bool | False | Stream responses |
| `inline_diffs` | bool | True | Show inline diff previews for file operations |
| `show_cost` | bool | False | Show $ cost estimate |
| `skin` | str | "default" | Skin name |
| `interim_assistant_messages` | bool | True | Show mid-turn status messages |
| `tool_progress_command` | bool | False | Enable /verbose command |
| `tool_preview_length` | int | 0 | Max chars for tool call previews; 0 = unlimited |

#### 8b. Skins catalog

Check `hermes_cli/skin_engine.py` for available skins. At minimum: `default`, `minimal`, `monochrome`.

```
Display
├── Skin: [Default ▾]
├── Personality: [Kawaii ▾]  (Kawaii | Neutral)
├── [ ] Compact mode
├── Resume display: [Full ▾]  (Full | Compact)
├── Busy input mode: [Interrupt ▾]  (Interrupt | Queue)
├── [ ] Bell on complete
├── [ ] Show reasoning content
├── [ ] Streaming mode
├── [✓] Show inline diff previews
├── [ ] Show cost estimate
├── [✓] Show mid-turn assistant messages
├── [ ] Enable /verbose command
└── Tool preview max length: [0_______]  0 = unlimited
```

---

### Phase 9: Browser Settings

#### 9a. Fields

`config.yaml` path: `browser.*`

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `inactivity_timeout` | int | 120 | Seconds |
| `command_timeout` | int | 30 | Seconds |
| `record_sessions` | bool | False | Auto-record as WebM |
| `allow_private_urls` | bool | False | Allow localhost/internal IPs |

```
Browser
├── Inactivity timeout (seconds): [120_______]
├── Command timeout (seconds): [30_________]
├── [ ] Auto-record browser sessions as WebM
├── [ ] Allow navigation to localhost/private URLs
```

---

### Phase 10: Checkpoints (File Rollback)

#### 10a. Fields

`config.yaml` path: `checkpoints.*`

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `enabled` | bool | True | |
| `max_snapshots` | int | 50 | Max snapshots per directory |

```
File Checkpoints
├── [✓] Enable automatic snapshots before destructive file ops
└── Max snapshots per directory: [50_______]
```

---

### Phase 11: Logging

#### 11a. Fields

`config.yaml` path: `logging.*`

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `level` | str | "INFO" | DEBUG \| INFO \| WARNING |
| `max_size_mb` | int | 5 | Per-file size before rotation |
| `backup_count` | int | 3 | Rotated backups to keep |

```
Logging
├── Log level: [Info ▾]  (Debug | Info | Warning)
├── Max log file size (MB): [5_______]
└── Backup count: [3_______]
```

#### 11b. Network settings

`config.yaml` path: `network.*`

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `force_ipv4` | bool | False | Skip IPv6, use IPv4 only |

```
Network
└── [ ] Force IPv4 only  (workaround for broken IPv6)
```

---

### Phase 12: Messaging Platforms

All configured via `.env` env vars — no `config.yaml` changes needed.

#### 12a. Platforms and env vars

| Platform | Primary env var | Other env vars |
|----------|----------------|---------------|
| Telegram | `TELEGRAM_BOT_TOKEN` | `TELEGRAM_ALLOWED_USERS`, `TELEGRAM_HOME_CHANNEL` |
| Discord | `DISCORD_BOT_TOKEN` | `DISCORD_ALLOWED_USERS`, `DISCORD_HOME_CHANNEL` |
| Slack | `SLACK_BOT_TOKEN` | `SLACK_APP_TOKEN`, `SLACK_ALLOWED_USERS` |
| Signal | `SIGNAL_CLI_PATH` | `SIGNAL_ALLOWED_USERS` |
| Matrix | `MATRIX_ACCESS_TOKEN` | `MATRIX_HOMESERVER`, `MATRIX_USER_ID`, `MATRIX_PASSWORD`, `MATRIX_ENCRYPTION` |
| Email | `SMTP_HOST` | `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `IMAP_HOST`, `IMAP_PORT`, `IMAP_USER`, `IMAP_PASSWORD` |
| WhatsApp | `TWILIO_ACCOUNT_SID` | `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` |
| Home Assistant | `HOME_ASSISTANT_URL` | `HOME_ASSISTANT_TOKEN` |
| DingTalk | `DINGTALK_APP_KEY` | `DINGTALK_APP_SECRET` |
| Feishu/Lark | `FEISHU_APP_ID` | `FEISHU_APP_SECRET` |
| WeCom | `WECOM_CORP_ID` | `WECOM_AGENT_ID`, `WECOM_AGENT_SECRET` |
| QQ Bot | `QQ_APP_ID` | `QQ_CLIENT_SECRET`, `QQ_ALLOWED_USERS`, `QQ_HOME_CHANNEL` |

#### 12b. GET / POST

**File:** `api/routes.py`

GET returns `{platforms: {telegram: {configured: bool, label: str, instructions: str, fields: [...]}, ...}}`

POST accepts `{platform: str, ...fields}` and writes to `~/.hermes/.env` via `_write_env_file()`.

#### 12c. Messaging platform field definitions

Full per-platform field definitions:

```python
_MESSAGING_PLATFORMS = {
    "telegram": {
        "label": "Telegram",
        "instructions": "Create a bot via @BotFather on Telegram. The bot token looks like: 123456789:ABCdefGHI-jklMNOpqrSTU",
        "fields": [
            {"name": "bot_token",    "label": "Bot Token",          "required": True,  "password": True},
            {"name": "allowed_users","label": "Allowed User IDs",    "required": False, "help": "Comma-separated. Find your ID by messaging @userinfobot"},
            {"name": "home_channel","label": "Home Channel / Chat ID","required": False,"help": "For DMs this is your user ID. Leave empty to set later with /set-home"},
        ],
    },
    "discord": {
        "label": "Discord",
        "instructions": "Create a bot at https://discord.com/developers/applications",
        "fields": [
            {"name": "bot_token",    "label": "Bot Token",          "required": True,  "password": True},
            {"name": "allowed_users","label": "Allowed User IDs",    "required": False, "help": "Enable Developer Mode → right-click name → Copy ID"},
            {"name": "home_channel","label": "Home Channel ID",     "required": False, "help": "Right-click channel → Copy Channel ID"},
        ],
    },
    "slack": {
        "label": "Slack",
        "instructions": "1. Create app at api.slack.com/apps → Create New App\n2. Enable Socket Mode: Settings → Socket Mode → Enable → create App-Level Token with connections:write\n3. Add Bot Token Scopes: chat:write, app_mentions:read, channels:history, channels:read, im:history, im:read, im:write, users:read, files:read, files:write\n4. Subscribe to Events: message.im, message.channels, app_mention\n5. Install to workspace → invite bot to channels with /invite @YourBot",
        "fields": [
            {"name": "bot_token",    "label": "Bot Token (xoxb-...)", "required": True,  "password": True},
            {"name": "app_token",    "label": "App Token (xapp-...)", "required": True,  "password": True},
            {"name": "allowed_users","label": "Allowed User IDs",       "required": False},
        ],
    },
    "signal": {
        "label": "Signal",
        "instructions": "Requires signal-cli installed and on PATH. Get the path with `which signal-cli`.",
        "fields": [
            {"name": "signal_cli_path", "label": "signal-cli path",            "required": True},
            {"name": "allowed_users",  "label": "Allowed phone numbers",       "required": False, "help": "Comma-separated E.164 numbers"},
        ],
    },
    "matrix": {
        "label": "Matrix",
        "instructions": "Works with any Matrix homeserver (Synapse, Conduit, matrix.org). Get an access token from Element web client: Settings → Help & About → Advanced → Access Token.",
        "fields": [
            {"name": "homeserver",   "label": "Homeserver URL",              "required": True},
            {"name": "access_token", "label": "Access Token",                 "required": False, "password": True},
            {"name": "user_id",       "label": "User ID (@user:homeserver)",  "required": False},
            {"name": "password",     "label": "Password (if no access token)","required": False, "password": True},
            {"name": "encryption",    "label": "Enable E2EE",                 "required": False, "type": "checkbox"},
        ],
    },
    "email": {
        "label": "Email (SMTP/IMAP)",
        "instructions": "Configure an SMTP account for sending. IMAP is optional for receiving.",
        "fields": [
            {"name": "smtp_host",     "label": "SMTP Host",                   "required": True},
            {"name": "smtp_port",     "label": "SMTP Port",                   "required": False, "default": "587"},
            {"name": "smtp_user",     "label": "SMTP Username",               "required": True},
            {"name": "smtp_password", "label": "SMTP Password",               "required": True,  "password": True},
            {"name": "imap_host",     "label": "IMAP Host",                   "required": False},
            {"name": "imap_port",     "label": "IMAP Port",                   "required": False, "default": "993"},
            {"name": "imap_user",     "label": "IMAP Username",               "required": False},
            {"name": "imap_password", "label": "IMAP Password",               "required": False, "password": True},
        ],
    },
    "whatsapp": {
        "label": "WhatsApp (Twilio)",
        "instructions": "Requires a Twilio account with WhatsApp sandbox configured at twilio.com/console.",
        "fields": [
            {"name": "account_sid",   "label": "Account SID",                 "required": True},
            {"name": "auth_token",   "label": "Auth Token",                  "required": True,  "password": True},
            {"name": "from_number",   "label": "From (WhatsApp number)",     "required": True,  "help": "E.164 format, e.g. +1234567890"},
        ],
    },
    "homeassistant": {
        "label": "Home Assistant",
        "instructions": "Requires a Home Assistant instance with a Long-Lived Access Token: Profile → Long-Lived Access Tokens → Create.",
        "fields": [
            {"name": "url",          "label": "Home Assistant URL",          "required": True,  "help": "e.g. http://homeassistant.local:8123"},
            {"name": "access_token", "label": "Access Token",                 "required": True,  "password": True},
        ],
    },
    "dingtalk": {
        "label": "DingTalk",
        "instructions": "Create an application at open.dingtalk.com. Requires AppKey and AppSecret from the dev portal.",
        "fields": [
            {"name": "app_key",     "label": "App Key",                     "required": True},
            {"name": "app_secret",  "label": "App Secret",                  "required": True,  "password": True},
        ],
    },
    "feishu": {
        "label": "Feishu / Lark",
        "instructions": "Create an app at open.feishu.cn/app. Enable bot capability and message permissions.",
        "fields": [
            {"name": "app_id",      "label": "App ID",                      "required": True},
            {"name": "app_secret",  "label": "App Secret",                  "required": True,  "password": True},
        ],
    },
    "wecom": {
        "label": "WeCom (Enterprise WeChat)",
        "instructions": "Create an internal application in WeCom admin console at work.weixin.qq.com.",
        "fields": [
            {"name": "corp_id",     "label": "Corp ID",                     "required": True},
            {"name": "agent_id",    "label": "Agent ID",                    "required": True},
            {"name": "agent_secret","label": "Agent Secret",                "required": True,  "password": True},
        ],
    },
    "qqbot": {
        "label": "QQ Bot",
        "instructions": "Create a bot application at q.qq.com. Requires App ID and App Secret from the dev portal.",
        "fields": [
            {"name": "app_id",       "label": "App ID",                     "required": True},
            {"name": "client_secret","label": "App Secret",                 "required": True,  "password": True},
            {"name": "allowed_users","label": "Allowed OpenIDs",             "required": False, "help": "Comma-separated; find in event payloads"},
            {"name": "home_channel","label": "Home Channel OpenID",         "required": False},
        ],
    },
}
```

#### 12d. Per-platform config.yaml settings

For Discord, the gateway also reads per-platform settings from `config.yaml` (not env vars). These should be editable in the platform's form:

Discord `config.yaml` path: `discord.*`

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `require_mention` | bool | True | Require @mention in server channels |
| `free_response_channels` | str | "" | Comma-separated channel IDs; respond without mention |
| `allowed_channels` | str | "" | Whitelist — only respond in these channels |
| `auto_thread` | bool | True | Auto-create threads on @mention |
| `reactions` | bool | True | Add 👀/✅/❌ reactions during processing |

```
Discord Settings
├── [✓] Require @mention to respond
├── Free-response channels: [channel-id-1, channel-id-2____]
│   (comma-separated; bot responds without @mention here)
├── Allowed channels: [___________________________________]
│   (leave empty to allow all; comma-separated channel IDs)
├── [✓] Auto-create threads on @mention
└── [✓] Show reactions during processing
```

Similar per-platform settings exist for `telegram`, `slack`, `whatsapp`, `mattermost` — stored in `config.yaml` under their respective keys. Each platform form should include both env-var fields and these config fields.

#### 12e. Messaging UI

Platform cards listing each platform with configure/remove buttons. Clicking Configure opens a modal with:
1. Instructions text
2. Per-platform settings fields (from 12d)
3. Env-var credential fields (from 12c)

---

### Phase 13: Tools Configuration

#### 13a. Web search

From `hermes_cli/config.py` env var scanning:

| Provider | Env var |
|----------|---------|
| DuckDuckGo | free (no key) |
| Google | `GOOGLE_SEARCH_API_KEY` |
| SerpAPI | `SERPAPI_API_KEY` |
| Bing | `BING_SEARCH_API_KEY` |

#### 13b. Browser automation

| Provider | Env var |
|----------|---------|
| Playwright | free |
| Selenium | free |
| Puppeteer | free |
| Browserbase | `BROWSERBASE_API_KEY`, `BROWSERBASE_PROJECT_ID` |

#### 13c. Image generation

| Provider | Env var |
|----------|---------|
| OpenAI (DALL-E) | `OPENAI_API_KEY` |
| Stability AI | `STABILITY_API_KEY` |
| Fireworks | `FIREWORKS_API_KEY` |

#### 13d. Tools UI

```
Tools
Web Search
├── Provider: [DuckDuckGo (free) ▾]
├── API Key: [________________________]  (if needed)

Browser Automation
├── Provider: [Playwright ▾]
└── [No API key required]

Image Generation
├── Provider: [OpenAI ▾]
└── API Key: [________________________]
```

POST writes to `.env` for API keys, and sets platform_toolsets in config.yaml.

---

## Implementation Order

| Order | Phase | Complexity |
|-------|-------|------------|
| 1 | ONBOARDING_PROVIDERS_PLAN.md | Fix the blocker first |
| 2 | Phase 2: Settings Shell | Low — nav + HTML page + GET /api/settings/all |
| 3 | Phase 3: Agent Settings | Low — int inputs, one POST |
| 4 | Phase 4: Delegation | Low — str inputs, one POST |
| 5 | Phase 5: Compression | Low — bool + floats, one POST |
| 6 | Phase 7: Terminal Backend | Medium — conditional fields per backend type |
| 7 | Phase 8: Display / Skin | Low — dropdowns + bools, one POST |
| 8 | Phase 9: Browser | Low — int/bool, one POST |
| 9 | Phase 10: Checkpoints | Low — bool + int, one POST |
| 10 | Phase 11: Logging + Network | Low — dropdown + int, one POST |
| 11 | Phase 6: TTS | Medium — provider select, voice catalog, test button |
| 12 | Phase 12: Messaging | High — 12 platforms, each with multi-field forms + per-platform config.yaml settings |
| 13 | Phase 13: Tools | Low-medium — API key forms + provider selects |

---

## File Changes Summary

| File | Changes |
|------|---------|
| `api/onboarding.py` | ONBOARDING_PROVIDERS_PLAN.md — dynamic `_SUPPORTED_PROVIDER_SETUPS` builder |
| `api/config.py` | `_TTS_VOICES` catalog, `_TTS_PROVIDERS` list |
| `api/routes.py` | `GET /api/settings/all`, `GET/POST /api/tts`, `POST /api/tts/test`, `GET/POST /api/messaging`, `_MESSAGING_PLATFORMS` dict, `_is_messaging_configured()`, `_terminal_settings()`, `_messaging_status()`, `_tools_status()` |
| `static/settings.html` | (new) Unified settings page |
| `static/settings.js` | (new) Settings page logic — tab switching, form rendering, API calls |
| `static/app.js` | Add Settings gear icon to nav |

---

## Config Write Pattern

All config writes follow the same pattern:

```python
def _save_config_section(path: str, data: dict):
    """Merge data into config.yaml at the given dot-path key."""
    cfg = _load_yaml_config(_get_config_path())
    # e.g. path = "agent", data = {"max_turns": 90} → cfg["agent"].update(data)
    section = cfg
    parts = path.split(".")
    for p in parts[:-1]:
        section = section.setdefault(p, {})
    section.update(data)
    _save_yaml_config(_get_config_path(), cfg)
    reload_config()
```

All env-var writes use the existing `_write_env_file()` from `onboarding.py` (move to a shared utility module if needed).

---

## Verification

1. **Agent settings:** Change `max_turns` to 50 in WebUI → verify `config.yaml` has `agent.max_turns: 50` → run `hermes chat` → verify agent respects the limit.
2. **TTS:** Select ElevenLabs, enter voice_id, click Test Voice → audio plays.
3. **Terminal backend:** Switch to Docker, enter image → verify `config.yaml` has `terminal.backend: docker` and `terminal.docker_image: ...`.
4. **Messaging:** Configure Telegram with bot token → verify `.env` has `TELEGRAM_BOT_TOKEN=...` → start `hermes gateway` → verify bot responds.
5. **Full test:** `pytest tests/ -q --tb=short` passes.
