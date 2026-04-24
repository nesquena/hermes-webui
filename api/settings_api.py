"""
Hermes WebUI -- Config Settings API.

Reads and writes config.yaml sections and .env secrets.
Each section has its own namespace in both GET (full read) and POST (partial update).

Secrets (API keys, tokens, passwords) → ~/.hermes/.env  via _write_env_file()
Config options → ~/.hermes/config.yaml  via _save_yaml_config()

This module does NOT depend on the webui-specific settings (settings.json).
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

# Re-use the same YAML loader and config path logic from config.py
from api.config import _get_env_path, _get_config_path, get_config, reload_config

_yaml_lock = threading.Lock()


def _load_yaml_config() -> dict:
    """Load config.yaml, returning an empty dict if it doesn't exist."""
    cfg_path = _get_config_path()
    if not cfg_path.exists():
        return {}
    try:
        import yaml as _yaml

        return _yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _save_yaml_config(cfg: dict) -> None:
    """Atomically write config.yaml, creating parents as needed."""
    cfg_path = _get_config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import yaml as _yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to write Hermes config.yaml") from exc
    with _yaml_lock:
        cfg_path.write_text(
            _yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    reload_config()


# ---------------------------------------------------------------------------
# .env helpers (shared with onboarding.py — must stay in sync)
# ---------------------------------------------------------------------------

_HERMES_HOME = Path.home() / ".hermes"


def _load_env_file() -> dict[str, str]:
    """Read .env from the active profile path as a flat key→value dict."""
    env_path = _get_env_path()
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    except Exception:
        return {}
    return values


def _write_env_file(updates: dict[str, str | None]) -> None:
    """Merge updates into the active profile's .env, removing keys where value is None."""
    env_path = _get_env_path()
    current = _load_env_file()
    for key, value in updates.items():
        if value is None:
            current.pop(key, None)
            os.environ.pop(key, None)
        else:
            clean = str(value).strip()
            if "\n" in clean or "\r" in clean:
                raise ValueError("Value must not contain newline characters.")
            if clean:
                current[key] = clean
                os.environ[key] = clean
    lines = [f"{key}={current[key]}" for key in sorted(current)]
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


# ---------------------------------------------------------------------------
# Section definitions
# Each section: (config_key, env_vars_to_read, env_vars_to_write, defaults)
# ---------------------------------------------------------------------------

_AGENT_DEFAULTS = {
    "max_turns": 90,
    "gateway_timeout": 1800,
    "restart_drain_timeout": 60,
    "tool_use_enforcement": "auto",
    "gateway_timeout_warning": 900,
    "gateway_notify_interval": 600,
}

_DELEGATION_DEFAULTS = {
    "model": "",
    "provider": "",
    "base_url": "",
    "api_key": "",
    "max_iterations": 50,
    "reasoning_effort": "",
}

_COMPRESSION_DEFAULTS = {
    "enabled": True,
    "threshold": 0.5,
    "target_ratio": 0.2,
    "protect_last_n": 20,
}

_TERMINAL_DEFAULTS = {
    "backend": "local",
    "cwd": ".",
    "timeout": 180,
    "docker_image": "nikolaik/python-nodejs:python3.11-nodejs20",
    "docker_volumes": [],
    "docker_mount_cwd_to_workspace": False,
    "persistent_shell": True,
    "container_cpu": 1,
    "container_memory": 5120,
    "container_disk": 51200,
    "container_persistent": True,
    "env_passthrough": [],
    "docker_env": {},
    "sandbox_dir": "",
    "modal_mode": "auto",
    "singularity_image": "docker://nikolaik/python-nodejs:python3.11-nodejs20",
    "modal_image": "nikolaik/python-nodejs:python3.11-nodejs20",
    "daytona_image": "nikolaik/python-nodejs:python3.11-nodejs20",
}

_DISPLAY_DEFAULTS = {
    "compact": False,
    "personality": "kawaii",
    "resume_display": "full",
    "busy_input_mode": "interrupt",
    "bell_on_complete": False,
    "show_reasoning": False,
    "streaming": False,
    "inline_diffs": True,
    "show_cost": False,
    "skin": "default",
    "interim_assistant_messages": True,
    "tool_progress_command": False,
    "tool_preview_length": 0,
    "platforms": {},
}

_BROWSER_DEFAULTS = {
    "inactivity_timeout": 120,
    "command_timeout": 30,
    "record_sessions": False,
    "allow_private_urls": False,
}

_CHECKPOINTS_DEFAULTS = {
    "enabled": True,
    "max_snapshots": 50,
}

_LOGGING_DEFAULTS = {
    "level": "INFO",
    "max_size_mb": 5,
    "backup_count": 3,
}

_NETWORK_DEFAULTS = {
    "force_ipv4": False,
}

_CRON_DEFAULTS = {
    "wrap_response": True,
}

_TTS_DEFAULTS = {
    "provider": "edge",
    "elevenlabs": {"voice_id": "", "model_id": "eleven_v3"},
    "openai": {"voice": "alloy", "model": "gpt-4o-mini-tts"},
    "minimax": {"voice_id": "", "model": "speech-02-hd"},
    "mistral": {"voice": "sapphire", "model": "voxtral-mini-tts-2603"},
    "gemini": {"model": "gemini-2.5-flash-preview-05-20"},
    "neutts": {"model": "neuphonic/neutts-air-q4-gguf"},
    "xai": {"model": "grok"},
}

# Voice catalog per TTS provider
_TTS_VOICES = {
    "edge": [
        {"id": "en-US-AriaNeural", "label": "Aria (US English)"},
        {"id": "en-GB-SoniaNeural", "label": "Sonia (UK English)"},
        {"id": "de-DE-KatjaNeural", "label": "Katja (German)"},
        {"id": "fr-FR-DeniseNeural", "label": "Denise (French)"},
        {"id": "ja-JP-NanamiNeural", "label": "Nanami (Japanese)"},
        {"id": "zh-CN-XiaoxiaoNeural", "label": "Xiaoxiao (Mandarin)"},
    ],
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
    "elevenlabs": [
        # ElevenLabs uses dynamic voice list — show common voices as placeholders
        {"id": "pNInz6obpgDQGcFmaJgB", "label": "Rachel (Multilingual v2)"},
        {"id": "EXAVITQu4vr4xnSDxMaL", "label": "Bella (Multilingual v2)"},
    ],
}

_VOICE_DEFAULTS = {
    "record_key": "ctrl+b",
    "max_recording_seconds": 120,
    "auto_tts": False,
    "silence_threshold": 200,
    "silence_duration": 3.0,
}

# SSH credentials — stored as env vars, NOT in config.yaml
_SSH_ENV_VARS = {
    "TERMINAL_SSH_HOST",
    "TERMINAL_SSH_USER",
    "TERMINAL_SSH_KEY",
    "TERMINAL_SSH_PORT",
}


# ---------------------------------------------------------------------------
# GET — read all sections
# ---------------------------------------------------------------------------

def get_all_settings() -> dict:
    """Return all configurable sections for the settings UI."""
    cfg = get_config()
    env = _load_env_file()

    return {
        "agent": {k: cfg.get("agent", {}).get(k, v) for k, v in _AGENT_DEFAULTS.items()},
        "delegation": {k: cfg.get("delegation", {}).get(k, v) for k, v in _DELEGATION_DEFAULTS.items()},
        "compression": {k: cfg.get("compression", {}).get(k, v) for k, v in _COMPRESSION_DEFAULTS.items()},
        "terminal": {k: cfg.get("terminal", {}).get(k, v) for k, v in _TERMINAL_DEFAULTS.items()},
        "display": {k: cfg.get("display", {}).get(k, v) for k, v in _DISPLAY_DEFAULTS.items()},
        "browser": {k: cfg.get("browser", {}).get(k, v) for k, v in _BROWSER_DEFAULTS.items()},
        "checkpoints": {k: cfg.get("checkpoints", {}).get(k, v) for k, v in _CHECKPOINTS_DEFAULTS.items()},
        "logging": {k: cfg.get("logging", {}).get(k, v) for k, v in _LOGGING_DEFAULTS.items()},
        "network": {k: cfg.get("network", {}).get(k, v) for k, v in _NETWORK_DEFAULTS.items()},
        "cron": {k: cfg.get("cron", {}).get(k, v) for k, v in _CRON_DEFAULTS.items()},
        "tts": _get_tts_settings(cfg, env),
        "voice": {k: cfg.get("voice", {}).get(k, v) for k, v in _VOICE_DEFAULTS.items()},
        "messaging": _get_messaging_settings(cfg),
        "ssh": _get_ssh_settings(env),
        "tools": _get_tools_settings(cfg),
    }


def _get_tts_settings(cfg: dict, env: dict) -> dict:
    """Build TTS section with voice catalog and per-provider key detection."""
    tts_cfg = cfg.get("tts", {})
    provider = tts_cfg.get("provider", "edge")
    model_provider = cfg.get("model", {}).get("provider", "")

    # Per-provider env var → TTS provider mapping
    _KEY_MAP = [
        ("ELEVENLABS_API_KEY", "elevenlabs"),
        ("VOICE_TOOLS_OPENAI_KEY", "openai"),
        ("OPENAI_API_KEY", "openai"),
        ("MINIMAX_API_KEY", "minimax"),
        ("MISTRAL_API_KEY", "mistral"),
        ("GEMINI_API_KEY", "gemini"),
        ("XAI_API_KEY", "xai"),
    ]

    # detected_keys — keyed by TTS provider for easy frontend access
    detected_keys: dict[str, dict] = {}
    for var, tts_p in _KEY_MAP:
        if env.get(var):
            # Determine if this key is also the model provider's key
            model_key_vars = {
                "minimax": "MINIMAX_API_KEY",
                "openai": "OPENAI_API_KEY",
                "openrouter": "OPENAI_API_KEY",
                "google": "GOOGLE_API_KEY",
                "xai": "XAI_API_KEY",
                "mistral": "MISTRAL_API_KEY",
                "deepseek": "DEEPSEEK_API_KEY",
            }
            is_shared = (
                model_provider == tts_p
                and model_key_vars.get(model_provider) == var
            )
            source = "shared_with_model" if is_shared else "standalone"
            # Mask the key for display
            raw = env.get(var, "")
            masked = raw[:4] + "****" + raw[-4:] if len(raw) > 8 else raw[:3] + "****"
            detected_keys[tts_p] = {
                "env_var": var,
                "set": True,
                "source": source,
                "masked": masked,
            }

    # key_status — relationship between selected TTS provider and its key
    key_status = "none"
    if provider in detected_keys:
        dk = detected_keys[provider]
        if dk["source"] == "shared_with_model":
            key_status = "shared"
        elif provider == "openai" and "VOICE_TOOLS_OPENAI_KEY" in [d["env_var"] for d in detected_keys.values()]:
            key_status = "separate"
        else:
            key_status = "standalone"

    return {
        "provider": provider,
        "voices": _TTS_VOICES.get(provider, []),
        "config": {k: tts_cfg.get(k, v) if not isinstance(v, dict) else tts_cfg.get(k, v) for k, v in _TTS_DEFAULTS.items()},
        "detected_keys": detected_keys,
        "key_status": key_status,
    }


def _get_messaging_settings(cfg: dict) -> dict:
    """Return messaging platform status (no secrets — those are in .env)."""
    return {}


def _get_ssh_settings(env: dict) -> dict:
    """SSH credentials come from .env, not config.yaml."""
    return {
        k.lower().replace("terminal_ssh_", ""): env.get(k, "")
        for k in _SSH_ENV_VARS
    }


def _get_tools_settings(cfg: dict) -> dict:
    """Return tools configuration."""
    return {}


# ---------------------------------------------------------------------------
# POST — update one section at a time
# ---------------------------------------------------------------------------

def update_section(section: str, data: dict) -> dict:
    """
    Update a config.yaml section (and .env for secrets).

    Returns the updated section data (read back from config).

    Raises ValueError for bad field values.
    """
    if section == "agent":
        return _update_agent(data)
    elif section == "delegation":
        return _update_delegation(data)
    elif section == "compression":
        return _update_compression(data)
    elif section == "terminal":
        return _update_terminal(data)
    elif section == "display":
        return _update_display(data)
    elif section == "browser":
        return _update_browser(data)
    elif section == "checkpoints":
        return _update_checkpoints(data)
    elif section == "logging":
        return _update_logging(data)
    elif section == "network":
        return _update_network(data)
    elif section == "cron":
        return _update_cron(data)
    elif section == "tts":
        return _update_tts(data)
    elif section == "voice":
        return _update_voice(data)
    elif section == "ssh":
        return _update_ssh(data)
    elif section == "messaging":
        return _update_messaging(data)
    else:
        raise ValueError(f"Unknown settings section: {section}")


def _update_agent(data: dict) -> dict:
    cfg = _load_yaml_config()
    agent = cfg.setdefault("agent", {})
    for key in list(data.keys()):
        if key not in _AGENT_DEFAULTS:
            raise ValueError(f"Unknown agent field: {key}")
    for k, v in _AGENT_DEFAULTS.items():
        agent[k] = data.get(k, v)
    _save_yaml_config(cfg)
    return {k: agent.get(k) for k in _AGENT_DEFAULTS}


def _update_delegation(data: dict) -> dict:
    cfg = _load_yaml_config()
    delegation = cfg.setdefault("delegation", {})
    for key in list(data.keys()):
        if key not in _DELEGATION_DEFAULTS:
            raise ValueError(f"Unknown delegation field: {key}")
    for k, v in _DELEGATION_DEFAULTS.items():
        delegation[k] = data.get(k, v)
    _save_yaml_config(cfg)
    return {k: delegation.get(k) for k in _DELEGATION_DEFAULTS}


def _update_compression(data: dict) -> dict:
    cfg = _load_yaml_config()
    comp = cfg.setdefault("compression", {})
    for key in list(data.keys()):
        if key not in _COMPRESSION_DEFAULTS:
            raise ValueError(f"Unknown compression field: {key}")
    for k, v in _COMPRESSION_DEFAULTS.items():
        comp[k] = data.get(k, v)
    _save_yaml_config(cfg)
    return {k: comp.get(k) for k in _COMPRESSION_DEFAULTS}


def _update_terminal(data: dict) -> dict:
    cfg = _load_yaml_config()
    term = cfg.setdefault("terminal", {})
    for key in list(data.keys()):
        if key not in _TERMINAL_DEFAULTS:
            raise ValueError(f"Unknown terminal field: {key}")
    for k, v in _TERMINAL_DEFAULTS.items():
        term[k] = data.get(k, v)
    _save_yaml_config(cfg)
    return {k: term.get(k) for k in _TERMINAL_DEFAULTS}


def _update_display(data: dict) -> dict:
    cfg = _load_yaml_config()
    disp = cfg.setdefault("display", {})
    for key in list(data.keys()):
        if key not in _DISPLAY_DEFAULTS:
            raise ValueError(f"Unknown display field: {key}")
    for k, v in _DISPLAY_DEFAULTS.items():
        disp[k] = data.get(k, v)
    _save_yaml_config(cfg)
    return {k: disp.get(k) for k in _DISPLAY_DEFAULTS}


def _update_browser(data: dict) -> dict:
    cfg = _load_yaml_config()
    browser = cfg.setdefault("browser", {})
    for key in list(data.keys()):
        if key not in _BROWSER_DEFAULTS:
            raise ValueError(f"Unknown browser field: {key}")
    for k, v in _BROWSER_DEFAULTS.items():
        browser[k] = data.get(k, v)
    _save_yaml_config(cfg)
    return {k: browser.get(k) for k in _BROWSER_DEFAULTS}


def _update_checkpoints(data: dict) -> dict:
    cfg = _load_yaml_config()
    ckpt = cfg.setdefault("checkpoints", {})
    for key in list(data.keys()):
        if key not in _CHECKPOINTS_DEFAULTS:
            raise ValueError(f"Unknown checkpoints field: {key}")
    for k, v in _CHECKPOINTS_DEFAULTS.items():
        ckpt[k] = data.get(k, v)
    _save_yaml_config(cfg)
    return {k: ckpt.get(k) for k in _CHECKPOINTS_DEFAULTS}


def _update_logging(data: dict) -> dict:
    cfg = _load_yaml_config()
    log = cfg.setdefault("logging", {})
    for key in list(data.keys()):
        if key not in _LOGGING_DEFAULTS:
            raise ValueError(f"Unknown logging field: {key}")
    for k, v in _LOGGING_DEFAULTS.items():
        log[k] = data.get(k, v)
    _save_yaml_config(cfg)
    return {k: log.get(k) for k in _LOGGING_DEFAULTS}


def _update_network(data: dict) -> dict:
    cfg = _load_yaml_config()
    net = cfg.setdefault("network", {})
    for key in list(data.keys()):
        if key not in _NETWORK_DEFAULTS:
            raise ValueError(f"Unknown network field: {key}")
    for k, v in _NETWORK_DEFAULTS.items():
        net[k] = data.get(k, v)
    _save_yaml_config(cfg)
    return {k: net.get(k) for k in _NETWORK_DEFAULTS}


def _update_cron(data: dict) -> dict:
    cfg = _load_yaml_config()
    cron = cfg.setdefault("cron", {})
    for key in list(data.keys()):
        if key not in _CRON_DEFAULTS:
            raise ValueError(f"Unknown cron field: {key}")
    for k, v in _CRON_DEFAULTS.items():
        cron[k] = data.get(k, v)
    _save_yaml_config(cfg)
    return {k: cron.get(k) for k in _CRON_DEFAULTS}


def _update_tts(data: dict) -> dict:
    """
    TTS secrets (API keys) → .env
    TTS config options → config.yaml tts: section
    """
    secrets: dict[str, str | None] = {}

    # Map TTS provider → env var
    tts_key_map = {
        "elevenlabs": "ELEVENLABS_API_KEY",
        "openai": "VOICE_TOOLS_OPENAI_KEY",
        "minimax": "MINIMAX_API_KEY",
        "mistral": "MISTRAL_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "xai": "XAI_API_KEY",
    }

    # Write TTS API keys to .env
    for tts_provider, env_var in tts_key_map.items():
        key_name = f"{tts_provider}_api_key"
        if key_name in data:
            val = data.get(key_name)
            secrets[env_var] = val if val else None

    if secrets:
        _write_env_file(secrets)

    # Write TTS config to config.yaml
    cfg = _load_yaml_config()
    tts_cfg = cfg.setdefault("tts", {})

    # Top-level fields
    if "provider" in data:
        tts_cfg["provider"] = data["provider"]

    # Per-provider config
    for provider_key, defaults in [
        ("elevenlabs", _TTS_DEFAULTS["elevenlabs"]),
        ("openai", _TTS_DEFAULTS["openai"]),
        ("minimax", _TTS_DEFAULTS["minimax"]),
        ("mistral", _TTS_DEFAULTS["mistral"]),
        ("gemini", _TTS_DEFAULTS["gemini"]),
        ("neutts", _TTS_DEFAULTS["neutts"]),
    ]:
        if provider_key in data:
            provider_data = data[provider_key]
            if isinstance(provider_data, dict):
                current = dict(tts_cfg.get(provider_key, {}))
                current.update({k: v for k, v in provider_data.items() if k in defaults})
                tts_cfg[provider_key] = current

    _save_yaml_config(cfg)

    # Return updated TTS state
    env = _load_env_file()
    return _get_tts_settings(cfg, env)


def _update_voice(data: dict) -> dict:
    cfg = _load_yaml_config()
    voice = cfg.setdefault("voice", {})
    for key in list(data.keys()):
        if key not in _VOICE_DEFAULTS:
            raise ValueError(f"Unknown voice field: {key}")
    for k, v in _VOICE_DEFAULTS.items():
        voice[k] = data.get(k, v)
    _save_yaml_config(cfg)
    return {k: voice.get(k) for k in _VOICE_DEFAULTS}


def _update_ssh(data: dict) -> dict:
    """
    SSH credentials → .env (TERMINAL_SSH_HOST, TERMINAL_SSH_USER, etc.)
    These are NOT stored in config.yaml.
    """
    env_vars = {
        "host": "TERMINAL_SSH_HOST",
        "user": "TERMINAL_SSH_USER",
        "key": "TERMINAL_SSH_KEY",
        "port": "TERMINAL_SSH_PORT",
    }
    updates = {}
    for field, env_var in env_vars.items():
        if field in data:
            val = data[field]
            updates[env_var] = val if val else None
    if updates:
        _write_env_file(updates)
    env = _load_env_file()
    return _get_ssh_settings(env)


def _update_messaging(data: dict) -> dict:
    """Messaging platforms — tokens go to .env, config to config.yaml."""
    # TODO: per-platform update logic (Discord, Slack, etc.)
    return {}
