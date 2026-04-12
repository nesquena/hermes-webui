"""Hermes Web UI -- first-run onboarding helpers."""

from __future__ import annotations

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
    get_available_models,
    get_config,
    load_settings,
    reload_config,
    save_settings,
    verify_hermes_imports,
)
from api.workspace import get_last_workspace, load_workspaces


_SUPPORTED_PROVIDER_SETUPS = {
    "openrouter": {
        "label": "OpenRouter",
        "env_var": "OPENROUTER_API_KEY",
        "default_model": "anthropic/claude-sonnet-4.6",
        "requires_base_url": False,
        "models": [
            {"id": model["id"], "label": model["label"]} for model in _FALLBACK_MODELS
        ],
    },
    "anthropic": {
        "label": "Anthropic",
        "env_var": "ANTHROPIC_API_KEY",
        "default_model": "claude-sonnet-4.6",
        "requires_base_url": False,
        "models": list(_PROVIDER_MODELS.get("anthropic", [])),
    },
    "openai": {
        "label": "OpenAI",
        "env_var": "OPENAI_API_KEY",
        "default_model": "gpt-4o",
        "default_base_url": "https://api.openai.com/v1",
        "requires_base_url": False,
        "models": list(_PROVIDER_MODELS.get("openai", [])),
    },
    "custom": {
        "label": "Custom OpenAI-compatible",
        "env_var": "OPENAI_API_KEY",
        "default_model": "gpt-4o-mini",
        "requires_base_url": True,
        "models": [],
    },
}

_UNSUPPORTED_PROVIDER_NOTE = (
    "OAuth and advanced provider flows such as Nous Portal, OpenAI Codex, and GitHub "
    "Copilot are still terminal-first. Use `hermes model` for those flows."
)


def _get_active_hermes_home() -> Path:
    try:
        from api.profiles import get_active_hermes_home

        return get_active_hermes_home()
    except ImportError:
        return Path.home() / ".hermes"


def _load_env_file(env_path: Path) -> dict[str, str]:
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


def _write_env_file(env_path: Path, updates: dict[str, str]) -> None:
    current = _load_env_file(env_path)
    for key, value in updates.items():
        if value is None:
            current.pop(key, None)
            os.environ.pop(key, None)
            continue
        clean = str(value).strip()
        if not clean:
            continue
        # Reject embedded newlines/carriage returns to prevent .env injection
        if "\n" in clean or "\r" in clean:
            raise ValueError("API key must not contain newline characters.")
        current[key] = clean
        os.environ[key] = clean

    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{key}={current[key]}" for key in sorted(current)]
    env_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


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
    clean = (model or "").strip()
    if not clean:
        return ""
    if provider in {"anthropic", "openai"} and clean.startswith(provider + "/"):
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

    # Fast path: ask hermes_cli directly — the authoritative source
    try:
        from hermes_cli.auth import get_auth_status as _gas

        status = _gas(provider)
        if isinstance(status, dict) and status.get("logged_in"):
            return True
    except Exception:
        pass

    # Fallback: parse auth.json ourselves for known OAuth provider IDs.
    # Covers deployments where hermes_cli is installed but the import above
    # fails for an unexpected reason (version mismatch, import cycle, etc.).
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
    env_values = _load_env_file(_get_active_hermes_home() / ".env")

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
            # Unknown / OAuth provider (e.g. openai-codex, copilot, qwen-oauth).
            # These do not use a plain API key; auth lives in auth.json or a
            # credential pool managed by hermes_cli.
            provider_ready = _provider_oauth_authenticated(
                provider, _get_active_hermes_home()
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
        "env_path": str(_get_active_hermes_home() / ".env"),
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
                "quick": provider_id == "openrouter",
            }
        )

    return {
        "providers": providers,
        "unsupported_note": _UNSUPPORTED_PROVIDER_NOTE,
        "current": {
            "provider": current_provider,
            "model": current_model
            or _SUPPORTED_PROVIDER_SETUPS[current_provider]["default_model"],
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

    return {
        "completed": bool(settings.get("onboarding_completed")),
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
    provider = str(body.get("provider") or "").strip().lower()
    model = str(body.get("model") or "").strip()
    api_key = str(body.get("api_key") or "").strip()
    base_url = _normalize_base_url(str(body.get("base_url") or ""))

    if provider not in _SUPPORTED_PROVIDER_SETUPS:
        raise ValueError("Unsupported provider for WebUI onboarding.")
    if not model:
        raise ValueError("model is required")

    provider_meta = _SUPPORTED_PROVIDER_SETUPS[provider]
    if provider_meta.get("requires_base_url"):
        if not base_url:
            raise ValueError("base_url is required for custom endpoints")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("base_url must start with http:// or https://")

    cfg = _load_yaml_config(_get_config_path())
    env_path = _get_active_hermes_home() / ".env"
    env_values = _load_env_file(env_path)

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
    _save_yaml_config(_get_config_path(), cfg)

    if api_key:
        _write_env_file(env_path, {provider_meta["env_var"]: api_key})

    try:
        from api.profiles import _reload_dotenv

        _reload_dotenv(_get_active_hermes_home())
    except Exception:
        pass

    reload_config()
    return get_onboarding_status()


def complete_onboarding() -> dict:
    save_settings({"onboarding_completed": True})
    return get_onboarding_status()
