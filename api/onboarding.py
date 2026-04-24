"""Hermes Web UI -- first-run onboarding helpers."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import urlparse

from api.auth import is_auth_enabled
from api.config import (
    DEFAULT_MODEL,
    DEFAULT_WORKSPACE,
    _FALLBACK_MODELS,
    _HERMES_FOUND,
    _PROVIDER_DISPLAY,
    _PROVIDER_MODELS,
    _get_config_path,
    _get_env_path,
    _load_env_into_os,
    get_available_models,
    get_config,
    load_settings,
    reload_config,
    save_settings,
    verify_hermes_imports,
)
from api.settings_api import _load_env_file, _write_env_file
from api.workspace import get_last_workspace, load_workspaces

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider setup metadata -- single source of truth for onboarding.
# Only providers that differ from the default assumptions need an entry.
# Default: env_var=f"{PID.upper()}_API_KEY", requires_base_url=False.
# None = OAuth/CLI-only provider, excluded from the wizard.
# ---------------------------------------------------------------------------
_PROVIDER_SETUP_METADATA: dict[str, dict | None] = {
    "openrouter": {
        "label": "OpenRouter",
        "env_var": "OPENROUTER_API_KEY",
        "requires_base_url": False,
        "default_base_url": "",
        "models_key": "_fallback",
    },
    "anthropic": {
        "label": "Anthropic",
        "env_var": "ANTHROPIC_API_KEY",
        "requires_base_url": False,
        "default_base_url": "",
        "models_key": "anthropic",
    },
    "openai": {
        "label": "OpenAI",
        "env_var": "OPENAI_API_KEY",
        "requires_base_url": False,
        "default_base_url": "https://api.openai.com/v1",
        "models_key": "openai",
    },
    "google": {
        "label": "Google",
        "env_var": "GOOGLE_API_KEY",
        "requires_base_url": False,
        "default_base_url": "",
        "models_key": "google",
    },
    "gemini": {
        "label": "Gemini",
        "env_var": "GEMINI_API_KEY",
        "requires_base_url": False,
        "default_base_url": "",
        "models_key": "gemini",
    },
    "deepseek": {
        "label": "DeepSeek",
        "env_var": "DEEPSEEK_API_KEY",
        "requires_base_url": False,
        "default_base_url": "https://api.deepseek.com/v1",
        "models_key": "deepseek",
    },
    "minimax": {
        "label": "MiniMax",
        "env_var": "MINIMAX_API_KEY",
        "requires_base_url": False,
        "default_base_url": "https://api.minimax.chat/v1",
        "models_key": "minimax",
    },
    "x-ai": {
        "label": "xAI",
        "env_var": "XAI_API_KEY",
        "requires_base_url": False,
        "default_base_url": "",
        "models_key": "x-ai",
    },
    "zai": {
        "label": "Z.AI / GLM",
        "env_var": "GLM_API_KEY",
        "requires_base_url": False,
        "default_base_url": "",
        "models_key": "zai",
    },
    "kimi-coding": {
        "label": "Kimi / Moonshot",
        "env_var": "KIMI_API_KEY",
        "requires_base_url": False,
        "default_base_url": "",
        "models_key": "kimi-coding",
    },
    "huggingface": {
        "label": "HuggingFace",
        "env_var": "HF_TOKEN",
        "requires_base_url": False,
        "default_base_url": "",
        "models_key": "huggingface",
    },
    "alibaba": {
        "label": "Alibaba / DashScope",
        "env_var": "DASHSCOPE_API_KEY",
        "requires_base_url": False,
        "default_base_url": "",
        "models_key": "alibaba",
    },
    "ollama": {
        "label": "Ollama",
        "env_var": "",  # local provider; no key needed
        "requires_base_url": True,
        "default_base_url": "http://localhost:11434",
        "models_key": "ollama",
    },
    "lmstudio": {
        "label": "LM Studio",
        "env_var": "",  # local provider; no key needed
        "requires_base_url": True,
        "default_base_url": "http://localhost:1234",
        "models_key": "lmstudio",
    },
    "opencode-zen": {
        "label": "OpenCode Zen",
        "env_var": "OPENCODE_ZEN_API_KEY",
        "requires_base_url": False,
        "default_base_url": "",
        "models_key": "opencode-zen",
    },
    "opencode-go": {
        "label": "OpenCode Go",
        "env_var": "OPENCODE_GO_API_KEY",
        "requires_base_url": False,
        "default_base_url": "",
        "models_key": "opencode-go",
    },
    "mistralai": {
        "label": "Mistral",
        "env_var": "MISTRAL_API_KEY",
        "requires_base_url": False,
        "default_base_url": "",
        "models_key": "mistralai",
    },
    "qwen": {
        "label": "Qwen",
        "env_var": "QWEN_API_KEY",
        "requires_base_url": False,
        "default_base_url": "",
        "models_key": "qwen",
    },
    "xiaomi": {
        "label": "Xiaomi MiMo",
        "env_var": "XIAOMI_API_KEY",
        "requires_base_url": False,
        "default_base_url": "",
        "models_key": "xiaomi",
    },
    "kilocode": {
        "label": "Kilo Code",
        "env_var": "KILOCODE_API_KEY",
        "requires_base_url": False,
        "default_base_url": "",
        "models_key": "kilocode",
    },
    # OAuth / CLI-only providers — excluded from wizard
    "openai-codex": None,
    "copilot": None,
    "copilot-acp": None,
    "qwen-oauth": None,
    "nous": None,
    # Custom always last — manual endpoint fallback
    "custom": {
        "label": "Custom OpenAI-compatible",
        "env_var": "OPENAI_API_KEY",
        "requires_base_url": True,
        "default_base_url": "",
        "models_key": None,
    },
}


def _build_supported_provider_setups() -> dict:
    """
    Dynamically build the provider setup catalog from:
    - _PROVIDER_SETUP_METADATA  (local overrides — env_var, base_url, models_key)
    - _PROVIDER_MODELS         (model lists from config.py)
    - _PROVIDER_DISPLAY        (display names from config.py)
    - _FALLBACK_MODELS         (OpenRouter's model list from config.py)
    """
    setups = {}

    # 1. Providers with explicit metadata entries (includes all original 4)
    for pid, meta in _PROVIDER_SETUP_METADATA.items():
        if meta is None:
            continue  # OAuth providers

        models_key = meta.get("models_key")
        if models_key == "_fallback":
            models = [{"id": m["id"], "label": m["label"]} for m in _FALLBACK_MODELS]
        elif models_key:
            models = list(_PROVIDER_MODELS.get(models_key, []))
        else:
            models = []

        # Resolve default_model: prefer metadata override, else first non-prefixed model
        # ID.  Provider-prefixed IDs like "ollama/llama-5-pro" shouldn't be used as
        # defaults since the API form only wants the short ID (e.g. "llama-5-pro").
        default_model = meta.get("default_model") or ""
        if not default_model:
            for m in models:
                mid = m.get("id", "")
                # Good default = no "/" or the part before "/" is the provider itself
                # (i.e. the second part is what the API actually uses)
                if mid and "/" in mid:
                    default_model = mid.split("/", 1)[1]
                    break
            if not default_model and models:
                default_model = models[0].get("id", "")

        setups[pid] = {
            "label": meta["label"],
            "env_var": meta.get("env_var") or f"{pid.upper()}_API_KEY",
            "default_model": default_model,
            "requires_base_url": bool(meta.get("requires_base_url")),
            "default_base_url": meta.get("default_base_url", ""),
            "models": models,
        }

    # 2. Any provider in _PROVIDER_MODELS not yet covered — use defaults.
    # Skip any provider explicitly marked as None (OAuth / CLI-only) in metadata.
    for pid, models in _PROVIDER_MODELS.items():
        if pid in setups:
            continue
        meta = _PROVIDER_SETUP_METADATA.get(pid)
        if meta is None:
            continue  # OAuth / CLI-only provider
        label = _PROVIDER_DISPLAY.get(pid, pid.title())
        model_list = list(models)
        # Default to first model; don't use provider-prefixed IDs as defaults
        # (ollama/lmstudio store e.g. "ollama/llama-5-pro" but the API just wants "llama-5-pro")
        default_model = ""
        for m in model_list:
            mid = m.get("id", "")
            if mid and "/" not in mid.split("/")[0]:
                default_model = mid
                break
        if not default_model and model_list:
            default_model = model_list[0].get("id", "")
        setups[pid] = {
            "label": label,
            "env_var": f"{pid.upper()}_API_KEY",
            "default_model": model_list[0]["id"] if model_list else "",
            "requires_base_url": False,
            "default_base_url": "",
            "models": model_list,
        }

    # 3. OpenRouter uses _FALLBACK_MODELS (may not be in _PROVIDER_MODELS)
    if "openrouter" not in setups:
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
_SUPPORTED_PROVIDER_SETUPS: dict = {**_build_supported_provider_setups()}

_UNSUPPORTED_PROVIDER_NOTE = (
    "OAuth-based providers (Nous Portal, OpenAI Codex, GitHub Copilot, Qwen OAuth) "
    "must be configured via `hermes auth` in a terminal first."
)


def _get_active_hermes_home() -> Path:
    """Return the active profile's HERMES_HOME using the centralized path resolver."""
    return _get_env_path().parent


def _load_yaml_config(config_path: Path) -> dict:
    try:
        import yaml as _yaml
    except ImportError:
        return {}

    if not config_path.exists():
        return {}
    try:
        loaded = _yaml.safe_load(config_path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _save_yaml_config(config_path: Path, config: dict) -> None:
    try:
        import yaml as _yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to write Hermes config.yaml") from exc

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        _yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _normalize_model_for_provider(provider: str, model: str) -> str:
    """Strip provider prefix from model IDs for providers that don't use it in their API."""
    clean = (model or "").strip()
    if not clean:
        return ""
    # These providers embed provider/name format in their API
    _uses_provider_prefix = {
        "openrouter", "openai-codex", "google", "gemini",
        "deepseek", "huggingface", "kimi-coding", "x-ai",
        "zai", "alibaba", "qwen", "mistralai",
    }
    if provider in _uses_provider_prefix and "/" in clean:
        return clean.split("/", 1)[1]
    return clean


def _normalize_base_url(base_url: str) -> str:
    return (base_url or "").strip().rstrip("/")


def _extract_current_provider(cfg: dict) -> str:
    model_cfg = cfg.get("model", {})
    if isinstance(model_cfg, dict):
        provider = str(model_cfg.get("provider") or "").strip().lower()
        if provider:
            return provider
    return ""


def _extract_current_model(cfg: dict) -> str:
    model_cfg = cfg.get("model", {})
    if isinstance(model_cfg, str):
        return model_cfg.strip()
    if isinstance(model_cfg, dict):
        return str(model_cfg.get("default") or "").strip()
    return ""


def _extract_current_base_url(cfg: dict) -> str:
    model_cfg = cfg.get("model", {})
    if isinstance(model_cfg, dict):
        return _normalize_base_url(str(model_cfg.get("base_url") or ""))
    return ""


def _provider_api_key_present(
    provider: str, cfg: dict, env_values: dict[str, str]
) -> bool:
    provider = (provider or "").strip().lower()
    if not provider:
        return False

    env_var = _SUPPORTED_PROVIDER_SETUPS.get(provider, {}).get("env_var")
    if env_var and env_values.get(env_var):
        return True

    model_cfg = cfg.get("model", {})
    if isinstance(model_cfg, dict) and str(model_cfg.get("api_key") or "").strip():
        return True

    providers_cfg = cfg.get("providers", {})
    if isinstance(providers_cfg, dict):
        provider_cfg = providers_cfg.get(provider, {})
        if (
            isinstance(provider_cfg, dict)
            and str(provider_cfg.get("api_key") or "").strip()
        ):
            return True
        if provider == "custom":
            custom_cfg = providers_cfg.get("custom", {})
            if (
                isinstance(custom_cfg, dict)
                and str(custom_cfg.get("api_key") or "").strip()
            ):
                return True

    # For providers not in _SUPPORTED_PROVIDER_SETUPS (e.g. minimax-cn, deepseek,
    # xai, etc.), ask the hermes_cli auth registry — it knows every provider's env
    # var names and can check os.environ for a valid key.
    # Exclude known OAuth/token-flow providers — those are handled separately by
    # _provider_oauth_authenticated() and should not be short-circuited here.
    _known_oauth = {"openai-codex", "copilot", "copilot-acp", "qwen-oauth", "nous"}
    if provider not in _SUPPORTED_PROVIDER_SETUPS and provider not in _known_oauth:
        try:
            from hermes_cli.auth import get_auth_status as _gas
            status = _gas(provider)
            if isinstance(status, dict) and status.get("logged_in"):
                return True
        except Exception:
            pass

    return False



def _provider_oauth_authenticated(provider: str, hermes_home: "Path") -> bool:
    """Return True if the provider has valid OAuth credentials.

    Checks via hermes_cli.auth.get_auth_status() when available, then falls
    back to reading auth.json directly for the known OAuth provider IDs
    (openai-codex, copilot, copilot-acp, qwen-oauth, nous).

    This covers users who authenticated via 'hermes auth' or 'hermes model'
    but whose provider is not in _SUPPORTED_PROVIDER_SETUPS because it does
    not use a plain API key.
    """
    provider = (provider or "").strip().lower()
    if not provider:
        return False

    # Check auth.json for known OAuth provider IDs.
    # hermes_home scopes the check — callers must pass the correct home directory.
    # (A prior CLI fast path via hermes_cli.auth.get_auth_status() was removed
    # because it ignored hermes_home and read from the real system home, breaking
    # both test isolation and deployments with multiple profiles.)
    _known_oauth_providers = {"openai-codex", "copilot", "copilot-acp", "qwen-oauth", "nous"}
    if provider not in _known_oauth_providers:
        return False

    try:
        import json as _j

        auth_path = hermes_home / "auth.json"
        if not auth_path.exists():
            return False
        store = _j.loads(auth_path.read_text(encoding="utf-8"))
        providers_store = store.get("providers")
        if not isinstance(providers_store, dict):
            return False
        state = providers_store.get(provider)
        if not isinstance(state, dict):
            return False
        # Any non-empty token is enough to confirm the user has credentials.
        # Token refresh happens at runtime inside the agent.
        has_token = bool(
            str(state.get("access_token") or "").strip()
            or str(state.get("api_key") or "").strip()
            or str(state.get("refresh_token") or "").strip()
        )
        return has_token
    except Exception:
        return False


def _status_from_runtime(cfg: dict, imports_ok: bool) -> dict:
    provider = _extract_current_provider(cfg)
    model = _extract_current_model(cfg)
    base_url = _extract_current_base_url(cfg)
    env_values = _load_env_file()

    provider_configured = bool(provider and model)
    provider_ready = False

    if provider_configured:
        if provider == "custom":
            provider_ready = bool(
                base_url and _provider_api_key_present(provider, cfg, env_values)
            )
        elif provider in _SUPPORTED_PROVIDER_SETUPS:
            provider_ready = _provider_api_key_present(provider, cfg, env_values)
        else:
            # Unknown provider — may be an OAuth flow (openai-codex, copilot, etc.)
            # OR an API-key provider not in the quick-setup list (minimax-cn, deepseek,
            # xai, etc.).  Check both: api key presence first (covers the majority of
            # third-party providers), then OAuth auth.json.
            provider_ready = (
                _provider_api_key_present(provider, cfg, env_values)
                or _provider_oauth_authenticated(provider, _get_active_hermes_home())
            )

    chat_ready = bool(_HERMES_FOUND and imports_ok and provider_ready)

    if not _HERMES_FOUND or not imports_ok:
        state = "agent_unavailable"
        note = (
            "Hermes is not fully importable from the Web UI yet. Finish bootstrap or fix the "
            "agent install before provider setup will work."
        )
    elif chat_ready:
        state = "ready"
        provider_name = _PROVIDER_DISPLAY.get(
            provider, provider.title() if provider else "Hermes"
        )
        note = f"Hermes is minimally configured and ready to chat via {provider_name}."
    elif provider_configured:
        state = "provider_incomplete"
        if provider == "custom" and not base_url:
            note = (
                "Hermes has a saved provider/model selection but still needs the "
                "base URL and API key required to chat."
            )
        elif provider not in _SUPPORTED_PROVIDER_SETUPS:
            # OAuth / unsupported provider: avoid misleading "API key" wording.
            note = (
                f"Provider '{provider}' is configured but not yet authenticated. "
                "Run 'hermes auth' or 'hermes model' in a terminal to complete "
                "setup, then reload the Web UI."
            )
        else:
            note = (
                "Hermes has a saved provider/model selection but still needs the "
                "API key required to chat."
            )
    else:
        state = "needs_provider"
        note = "Hermes is installed, but you still need to choose a provider and save working credentials."

    return {
        "provider_configured": provider_configured,
        "provider_ready": provider_ready,
        "chat_ready": chat_ready,
        "setup_state": state,
        "provider_note": note,
        "current_provider": provider or None,
        "current_model": model or None,
        "current_base_url": base_url or None,
        "env_path": str(_get_env_path()),
    }


def _build_setup_catalog(cfg: dict) -> dict:
    current_provider = _extract_current_provider(cfg) or "openrouter"
    current_model = _extract_current_model(cfg)
    current_base_url = _extract_current_base_url(cfg)

    providers = []
    for provider_id, meta in _SUPPORTED_PROVIDER_SETUPS.items():
        providers.append(
            {
                "id": provider_id,
                "label": meta["label"],
                "env_var": meta["env_var"],
                "default_model": meta["default_model"],
                "default_base_url": meta.get("default_base_url") or "",
                "requires_base_url": bool(meta.get("requires_base_url")),
                "models": list(meta.get("models", [])),
                "quick": provider_id in {"openrouter", "anthropic", "openai"},
            }
        )

    # Flag whether the currently-configured provider is OAuth-based (not in the
    # API-key flow).  The frontend uses this to show a confirmation card instead
    # of a key input when the user has already authenticated via 'hermes auth'.
    current_is_oauth = current_provider not in _SUPPORTED_PROVIDER_SETUPS and bool(
        current_provider
    )

    return {
        "providers": providers,
        "unsupported_note": _UNSUPPORTED_PROVIDER_NOTE,
        "current_is_oauth": current_is_oauth,
        "current": {
            "provider": current_provider,
            "model": current_model
            or _SUPPORTED_PROVIDER_SETUPS.get(current_provider, {}).get(
                "default_model", ""
            ),
            "base_url": current_base_url,
        },
    }


def get_onboarding_status() -> dict:
    settings = load_settings()
    cfg = get_config()
    imports_ok, missing, errors = verify_hermes_imports()
    runtime = _status_from_runtime(cfg, imports_ok)
    workspaces = load_workspaces()
    last_workspace = get_last_workspace()
    available_models = get_available_models()

    # HERMES_WEBUI_SKIP_ONBOARDING=1 lets hosting providers (e.g. Agent37) ship
    # a pre-configured instance without the wizard blocking the first load.
    # This is an operator-level override and is honoured unconditionally —
    # the operator knows their deployment is configured; we must not second-guess
    # it by requiring chat_ready to also be true.
    skip_env = os.environ.get("HERMES_WEBUI_SKIP_ONBOARDING", "").strip()
    skip_requested = skip_env in {"1", "true", "yes"}
    auto_completed = skip_requested  # unconditional: operator says skip, we skip

    # Auto-complete for existing Hermes users on two paths:
    # 1. BYO (bring your own): operator placed config.yaml AND .env files where
    #    Hermes can find them — skip regardless of agent availability.  This lets
    #    users who pre-configured Hermes drop in a config without running setup.
    # 2. CLI-configured: config.yaml exists + chat_ready.  These users configured
    #    Hermes via the CLI before the Web UI existed; they must never be shown
    #    the first-run wizard — it would silently overwrite their config.
    config_exists = Path(_get_config_path()).exists()
    env_exists = _get_env_path().exists()
    config_auto_completed = (config_exists and env_exists) or (config_exists and bool(runtime.get("chat_ready")))

    return {
        "completed": bool(settings.get("onboarding_completed")) or auto_completed or config_auto_completed,
        "settings": {
            "default_model": settings.get("default_model") or DEFAULT_MODEL,
            "default_workspace": settings.get("default_workspace")
            or str(DEFAULT_WORKSPACE),
            "password_enabled": is_auth_enabled(),
            "bot_name": settings.get("bot_name") or "Hermes",
        },
        "system": {
            "hermes_found": bool(_HERMES_FOUND),
            "imports_ok": bool(imports_ok),
            "missing_modules": missing,
            "import_errors": errors,
            "config_path": str(_get_config_path()),
            "config_exists": Path(_get_config_path()).exists(),
            **runtime,
        },
        "setup": _build_setup_catalog(cfg),
        "workspaces": {
            "items": workspaces,
            "last": last_workspace,
        },
        "models": available_models,
    }


def apply_onboarding_setup(body: dict) -> dict:
    # Hard guard: if the operator set SKIP_ONBOARDING, the wizard should never
    # have appeared.  Even if the frontend somehow calls this endpoint anyway
    # (e.g. a stale JS bundle or a curious user), we must not overwrite the
    # operator's config.yaml or .env files.  Just mark onboarding complete and
    # return the current status — no file writes.
    skip_env = os.environ.get("HERMES_WEBUI_SKIP_ONBOARDING", "").strip()
    if skip_env in {"1", "true", "yes"}:
        save_settings({"onboarding_completed": True})
        return get_onboarding_status()

    provider = str(body.get("provider") or "").strip().lower()
    model = str(body.get("model") or "").strip()
    api_key = str(body.get("api_key") or "").strip()
    base_url = _normalize_base_url(str(body.get("base_url") or ""))

    if provider not in _SUPPORTED_PROVIDER_SETUPS:
        # Unsupported providers (openai-codex, copilot, nous, etc.) are already
        # configured via the CLI. Just mark onboarding as complete and let the
        # user through — the agent is already set up, no further setup needed.
        save_settings({"onboarding_completed": True})
        return get_onboarding_status()
    if not model:
        raise ValueError("model is required")

    provider_meta = _SUPPORTED_PROVIDER_SETUPS[provider]
    if provider_meta.get("requires_base_url"):
        if not base_url:
            raise ValueError("base_url is required for custom endpoints")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("base_url must start with http:// or https://")

    config_path = _get_config_path()
    # Guard: if config.yaml already exists and the caller did not explicitly
    # acknowledge the overwrite, refuse to proceed.  The frontend must pass
    # confirm_overwrite=True after showing the user a confirmation step.
    if Path(config_path).exists() and not body.get("confirm_overwrite"):
        return {
            "error": "config_exists",
            "message": (
                "Hermes is already configured (config.yaml exists). "
                "Pass confirm_overwrite=true to overwrite it."
            ),
            "requires_confirm": True,
        }

    cfg = _load_yaml_config(config_path)
    env_values = _load_env_file()

    if not api_key and not _provider_api_key_present(provider, cfg, env_values):
        raise ValueError(f"{provider_meta['env_var']} is required")

    model_cfg = cfg.get("model", {})
    if not isinstance(model_cfg, dict):
        model_cfg = {}

    model_cfg["provider"] = provider
    model_cfg["default"] = _normalize_model_for_provider(provider, model)

    if provider == "custom":
        model_cfg["base_url"] = base_url
    elif provider == "openai":
        model_cfg["base_url"] = (
            provider_meta.get("default_base_url") or "https://api.openai.com/v1"
        )
    else:
        model_cfg.pop("base_url", None)

    cfg["model"] = model_cfg
    _save_yaml_config(config_path, cfg)

    if api_key:
        _write_env_file({provider_meta["env_var"]: api_key})

    # Reload the hermes_cli provider/config cache so the next streaming call
    # picks up the new key without requiring a server restart.
    try:
        from api.profiles import _reload_dotenv
        _reload_dotenv(_get_active_hermes_home())
    except Exception:
        logger.debug("Failed to reload dotenv")

    # Belt-and-braces: set directly on os.environ AFTER _reload_dotenv so the
    # value survives even if _reload_dotenv cleared it (e.g. when _write_env_file
    # wrote to disk but the profile isolation tracking hasn't seen it yet).
    if api_key:
        os.environ[provider_meta["env_var"]] = api_key

    try:
        # hermes_cli may cache config at import time; ask it to reload if possible.
        from hermes_cli.config import reload as _cli_reload
        _cli_reload()
    except Exception:
        logger.debug("Failed to reload hermes_cli config")

    reload_config()
    return get_onboarding_status()


def complete_onboarding() -> dict:
    save_settings({"onboarding_completed": True})
    return get_onboarding_status()


def import_config_file(config_content: str | None, env_content: str | None) -> dict:
    """
    Import config.yaml and/or .env from pasted/file content.

    - config_content: raw YAML text for config.yaml
    - env_content: raw .env text

    Merges into the active profile's config.yaml and .env (existing keys are
    not removed, new keys are added).  After writing, fires the full reload
    chain so the next streaming request uses the new credentials without
    requiring a server restart.

    Returns the updated onboarding status.
    """
    hermes_home = _get_active_hermes_home()
    config_path = _get_config_path()

    if config_content:
        try:
            import yaml as _yaml
            parsed = _yaml.safe_load(config_content)
            if not isinstance(parsed, dict):
                raise ValueError("config.yaml must be a YAML object (key: value pairs)")
        except Exception as exc:
            raise ValueError(f"Invalid config.yaml: {exc}") from exc

        # Read existing config and merge
        existing = _load_yaml_config(config_path)
        # Shallow merge: top-level keys from imported config win
        for k, v in parsed.items():
            if isinstance(v, dict) and k in existing and isinstance(existing.get(k), dict):
                existing[k] = {**existing[k], **v}
            else:
                existing[k] = v
        _save_yaml_config(config_path, existing)

    if env_content:
        try:
            # Parse as .env format: KEY=value lines
            updates: dict[str, str] = {}
            for line in env_content.strip().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                key = k.strip()
                val = v.strip().strip('"').strip("'")
                if key:
                    updates[key] = val
            if updates:
                _write_env_file(updates)
        except Exception as exc:
            raise ValueError(f"Invalid .env content: {exc}") from exc

    # ── Full reload chain (same as apply_onboarding_setup) ────────────────────
    try:
        from api.profiles import _reload_dotenv
        _reload_dotenv(hermes_home)
    except Exception:
        logger.debug("Failed to reload dotenv after import")

    try:
        from hermes_cli.config import reload as _cli_reload
        _cli_reload()
    except Exception:
        logger.debug("Failed to reload hermes_cli config after import")

    reload_config()

    # Mark onboarding as complete so the wizard closes after restart
    try:
        from api.config import save_settings
        save_settings({"onboarding_completed": True})
    except Exception:
        logger.debug("Failed to save onboarding_completed after import")

    return get_onboarding_status()
