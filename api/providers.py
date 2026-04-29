"""Hermes Web UI -- provider management endpoints.

Provides CRUD operations for configuring provider API keys post-onboarding.
Closes #586 (allow provider key update) and part of #604 (model picker
multi-provider support).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from api.config import (
    _PROVIDER_DISPLAY,
    _PROVIDER_MODELS,
    _get_config_path,
    _save_yaml_config_file,
    get_config,
    invalidate_models_cache,
    reload_config,
)

logger = logging.getLogger(__name__)

# SECTION: Provider ↔ env var mapping

# Maps canonical provider slug → env var name for API key.
# Providers not listed here (OAuth/token-flow providers like copilot, nous,
# openai-codex) cannot have their keys managed from the WebUI.
_PROVIDER_ENV_VAR: dict[str, str] = {
    "openrouter": "OPENROUTER_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "zai": "GLM_API_KEY",
    "kimi-coding": "KIMI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "mistralai": "MISTRAL_API_KEY",
    "x-ai": "XAI_API_KEY",
    "opencode-zen": "OPENCODE_ZEN_API_KEY",
    "opencode-go": "OPENCODE_GO_API_KEY",
    "ollama": "OLLAMA_API_KEY",
    "ollama-cloud": "OLLAMA_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
}

# Providers that use OAuth or token flows — their credentials are managed
# through the Hermes CLI, not via API keys.  The WebUI cannot set these.
_OAUTH_PROVIDERS = frozenset({
    "copilot",
    "copilot-acp",
    "nous",
    "openai-codex",
    "qwen-oauth",
})

# SECTION: Helper functions


def _get_hermes_home() -> Path:
    """Return the active Hermes home directory."""
    try:
        from api.profiles import get_active_hermes_home
        return get_active_hermes_home()
    except ImportError:
        return Path.home() / ".hermes"


def _load_env_file(env_path: Path) -> dict[str, str]:
    """Read key=value pairs from a .env file."""
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


def _write_env_file(env_path: Path, updates: dict[str, str | None]) -> None:
    """Write key=value pairs to the .env file.

    Values of ``None`` cause the key to be removed.

    Preserves comments, blank lines, and original key order (#1164).
    New keys are appended at the end of the file with a blank-line separator.

    Holds ``_ENV_LOCK`` from ``api.streaming`` for the entire load → modify →
    write cycle to prevent TOCTOU races between concurrent POST /api/providers
    calls (each reading the same file baseline and overwriting the other's key).
    Also serialises os.environ mutations with streaming sessions.
    """
    from api.streaming import _ENV_LOCK
    import stat as _stat

    with _ENV_LOCK:
        # ── Read existing lines (preserving comments and blank lines) ──
        existing_lines: list[str] = []
        if env_path.exists():
            try:
                existing_lines = env_path.read_text(encoding="utf-8").splitlines()
            except Exception:
                existing_lines = []

        # Map each existing key to its line index so we can update in-place.
        existing_key_indices: dict[str, int] = {}
        for _i, _raw in enumerate(existing_lines):
            _stripped = _raw.strip()
            if _stripped and not _stripped.startswith("#") and "=" in _stripped:
                _existing_key_indices_key = _stripped.split("=", 1)[0].strip()
                existing_key_indices[_existing_key_indices_key] = _i

        output_lines = list(existing_lines)
        new_keys: list[str] = []

        for key, value in updates.items():
            if value is None:
                # Mark the line for removal (None sentinel) and clear env.
                os.environ.pop(key, None)
                if key in existing_key_indices:
                    output_lines[existing_key_indices[key]] = None  # type: ignore[assignment]
                continue
            clean = str(value).strip()
            if not clean:
                continue
            # Reject embedded newlines/carriage returns to prevent .env injection
            if "\n" in clean or "\r" in clean:
                raise ValueError("API key must not contain newline characters.")
            os.environ[key] = clean

            if key in existing_key_indices:
                output_lines[existing_key_indices[key]] = f"{key}={clean}"
            else:
                new_keys.append(f"{key}={clean}")

        # Remove deleted lines (None sentinels)
        output_lines = [l for l in output_lines if l is not None]

        # Append new keys after a blank-line separator
        if new_keys:
            if output_lines and output_lines[-1].strip() != "":
                output_lines.append("")
            output_lines.extend(new_keys)

        env_path.parent.mkdir(parents=True, exist_ok=True)
        content = "\n".join(output_lines)
        if content:
            content += "\n"
        # Atomic write via tempfile + os.replace so cross-process readers
        # (Telegram bot, CLI) never see a half-truncated file.  The shared
        # ``~/.hermes/.env`` is also written by ``hermes_cli.config.save_env_value``
        # using the same atomic pattern; matching it here closes the
        # cross-process leg of #1164 (within-process is covered by _ENV_LOCK).
        _mode = _stat.S_IRUSR | _stat.S_IWUSR  # 0o600
        import tempfile as _tempfile
        _tmp_fd, _tmp_path = _tempfile.mkstemp(
            dir=str(env_path.parent), prefix=".env_", suffix=".tmp"
        )
        try:
            with os.fdopen(_tmp_fd, "w", encoding="utf-8") as _f:
                _f.write(content)
                _f.flush()
                os.fsync(_f.fileno())
            os.chmod(_tmp_path, _mode)  # tighten before rename so readers see 0600
            os.replace(_tmp_path, env_path)
        except BaseException:
            try:
                os.unlink(_tmp_path)
            except OSError:
                pass
            raise
        try:
            env_path.chmod(_mode)
        except OSError:
            pass


def _provider_has_key(provider_id: str) -> bool:
    """Check whether a provider has a configured API key.

    Checks (in order):
    1. ``~/.hermes/.env`` for the known env var
    2. ``os.environ`` for the known env var
    3. ``config.yaml → model.api_key`` (only if provider is the active one)
    4. ``config.yaml → providers.<id>.api_key``
    5. ``config.yaml → custom_providers[].api_key`` (for custom providers)
    """
    env_var = _PROVIDER_ENV_VAR.get(provider_id)
    if env_var:
        env_path = _get_hermes_home() / ".env"
        env_values = _load_env_file(env_path)
        if env_values.get(env_var):
            return True
        if os.getenv(env_var):
            return True

    cfg = get_config()
    # Check model.api_key — only match if this provider is the active one.
    # Previously this checked globally, causing all providers to show
    # "configured" when the active provider had a top-level api_key.
    model_cfg = cfg.get("model", {})
    if isinstance(model_cfg, dict) and str(model_cfg.get("api_key") or "").strip():
        active_provider = model_cfg.get("provider")
        if active_provider and str(active_provider).strip().lower() == provider_id.lower():
            return True
    # Check providers.<id>.api_key
    providers_cfg = cfg.get("providers", {})
    if isinstance(providers_cfg, dict):
        provider_cfg = providers_cfg.get(provider_id, {})
        if isinstance(provider_cfg, dict) and str(provider_cfg.get("api_key") or "").strip():
            return True
    # Check custom_providers
    custom_providers = cfg.get("custom_providers", [])
    if isinstance(custom_providers, list):
        for cp in custom_providers:
            if isinstance(cp, dict):
                cp_name = (cp.get("name") or "").strip().lower().replace(" ", "-")
                if f"custom:{cp_name}" == provider_id or cp.get("name", "").strip().lower() == provider_id:
                    if str(cp.get("api_key") or "").strip():
                        return True
    return False


def _provider_is_oauth(provider_id: str) -> bool:
    """Check whether a provider uses OAuth/token flows (managed by CLI)."""
    return provider_id in _OAUTH_PROVIDERS


# SECTION: Public API


def get_providers() -> dict[str, Any]:
    """Return a list of all known providers with their configuration status.

    Each entry contains:
    - ``id``: canonical provider slug
    - ``display_name``: human-readable name
    - ``has_key``: whether an API key is configured
    - ``configurable``: whether the key can be set from the WebUI
    - ``key_source``: where the key was found (``env_file``, ``env_var``,
      ``config_yaml``, ``oauth``, ``none``)
    - ``models``: list of known model IDs for this provider
    """
    providers = []

    # Collect all known provider IDs from multiple sources
    known_ids = set(_PROVIDER_DISPLAY.keys()) | set(_PROVIDER_MODELS.keys())

    # Also detect providers from config.yaml providers section
    cfg = get_config()
    providers_cfg = cfg.get("providers", {})
    if isinstance(providers_cfg, dict):
        known_ids.update(providers_cfg.keys())

    # Add OAuth providers even if not in _PROVIDER_DISPLAY
    known_ids.update(_OAUTH_PROVIDERS)

    for pid in sorted(known_ids):
        display_name = _PROVIDER_DISPLAY.get(pid, pid.replace("-", " ").title())
        is_oauth = _provider_is_oauth(pid)
        has_key = _provider_has_key(pid)

        # Determine key source
        key_source = "none"
        auth_error = None
        if is_oauth:
            key_source = "oauth"
            # Check if actually authenticated via hermes_cli.
            # IMPORTANT: do not unconditionally overwrite has_key from _provider_has_key().
            # A token in config.yaml is a valid credential even when get_auth_status()
            # returns logged_in=False (e.g. token not in the hermes credential pool,
            # or refresh token consumed by native Codex CLI / VS Code extension).
            try:
                from hermes_cli.auth import get_auth_status as _gas
                status = _gas(pid)
                if isinstance(status, dict) and status.get("logged_in"):
                    has_key = True
                    key_source = status.get("key_source", "oauth")
                elif has_key:
                    # _provider_has_key() found a token in config.yaml — respect it
                    # rather than hiding a working credential from the Settings UI.
                    key_source = "config_yaml"
                    auth_error = status.get("error") if isinstance(status, dict) else None
                else:
                    has_key = False
                    auth_error = status.get("error") if isinstance(status, dict) else None
            except Exception:
                # Import failed or auth check errored — don't override a known-good
                # key just because the hermes_cli auth module is unavailable.
                logger.debug("hermes_cli auth check failed for %s", pid, exc_info=True)
                # keep has_key from _provider_has_key()
        elif has_key:
            env_var = _PROVIDER_ENV_VAR.get(pid)
            if env_var:
                env_path = _get_hermes_home() / ".env"
                env_values = _load_env_file(env_path)
                if env_values.get(env_var):
                    key_source = "env_file"
                elif os.getenv(env_var):
                    key_source = "env_var"
                else:
                    key_source = "config_yaml"
            else:
                key_source = "config_yaml"
        elif pid not in _PROVIDER_ENV_VAR:
            # Fallback: provider is not a known API-key provider and not in
            # the hardcoded _OAUTH_PROVIDERS set.  It may be a custom or
            # newly-added OAuth provider (e.g. Anthropic connected via OAuth).
            # Check live auth status so the Providers tab agrees with the
            # model picker (#1212).
            #
            # IMPORTANT: we skip providers in _PROVIDER_ENV_VAR because they
            # are pure API-key providers — calling get_auth_status() for every
            # unconfigured API-key provider would add unnecessary latency
            # (network round-trip per provider) on the Settings page.
            # Validate pid looks like a real provider before probing
            import re as _re
            if _re.match(r'^[a-z][a-z0-9_-]{0,63}$', pid):
                try:
                    from hermes_cli.auth import get_auth_status as _gas
                    status = _gas(pid)
                    if isinstance(status, dict) and status.get("logged_in"):
                        has_key = True
                        # Constrain key_source to a known-safe closed set
                        _raw_ks = status.get("key_source", "")
                        key_source = _raw_ks if _raw_ks in {"oauth", "env", "config", "token"} else "oauth"
                        is_oauth = True
                except Exception:
                    pass

        models = _PROVIDER_MODELS.get(pid, [])
        # Also include models from config.yaml providers section
        if isinstance(providers_cfg, dict):
            provider_cfg = providers_cfg.get(pid, {})
            if isinstance(provider_cfg, dict) and "models" in provider_cfg:
                cfg_models = provider_cfg["models"]
                if isinstance(cfg_models, dict):
                    models = models + [{"id": k, "label": k} for k in cfg_models.keys()]
                elif isinstance(cfg_models, list):
                    models = models + [{"id": k, "label": k} for k in cfg_models]

        providers.append({
            "id": pid,
            "display_name": display_name,
            "has_key": has_key,
            "configurable": not is_oauth and pid in _PROVIDER_ENV_VAR,
            "is_oauth": is_oauth,
            "key_source": key_source,
            "auth_error": auth_error,
            "models": models,
        })

    # Scan custom_providers from config.yaml (e.g. glmcode, timicc)
    custom_providers_cfg = cfg.get("custom_providers", [])
    if isinstance(custom_providers_cfg, list):
        for cp in custom_providers_cfg:
            if not isinstance(cp, dict) or not cp.get("name"):
                continue
            cp_name = str(cp["name"]).strip()
            cp_id = f"custom:{cp_name}"
            # Collect models from `models` list or `model` single
            cp_models = []
            if isinstance(cp.get("models"), list):
                cp_models = [{"id": str(m), "label": str(m)} for m in cp["models"]]
            elif cp.get("model"):
                cp_models = [{"id": cp["model"], "label": cp["model"]}]
            # Check for env var reference (${VAR_NAME} pattern)
            cp_api_key = str(cp.get("api_key") or "")
            cp_has_key = bool(cp_api_key.strip())
            # Replace env var reference to check actual value
            if cp_api_key.startswith("${") and cp_api_key.endswith("}"):
                env_var = cp_api_key[2:-1]
                cp_has_key = bool(os.getenv(env_var, "").strip())
            providers.append({
                "id": cp_id,
                "display_name": cp_name,
                "has_key": cp_has_key,
                "configurable": False,  # custom providers managed via config.yaml
                "key_source": "config_yaml" if cp_has_key else "none",
                "models": cp_models,
            })

    # Determine active provider
    active_provider = None
    model_cfg = cfg.get("model", {})
    if isinstance(model_cfg, dict):
        active_provider = model_cfg.get("provider")

    return {
        "providers": providers,
        "active_provider": active_provider,
    }


def set_provider_key(provider_id: str, api_key: str | None) -> dict[str, Any]:
    """Set or update the API key for a provider.

    Writes the key to ``~/.hermes/.env`` using the standard env var name.
    If ``api_key`` is None or empty, the key is removed.

    Returns a status dict with the operation result.
    """
    provider_id = provider_id.strip().lower()

    if not provider_id:
        return {"ok": False, "error": "Provider ID is required."}

    if _provider_is_oauth(provider_id):
        return {
            "ok": False,
            "error": f"'{_PROVIDER_DISPLAY.get(provider_id, provider_id)}' uses OAuth authentication. "
                     f"Use `hermes model` in the terminal to configure it.",
        }

    env_var = _PROVIDER_ENV_VAR.get(provider_id)
    if not env_var:
        return {
            "ok": False,
            "error": f"Cannot configure API key for '{_PROVIDER_DISPLAY.get(provider_id, provider_id)}'. "
                     f"This provider does not have a known env var mapping.",
        }

    # Validate API key format (basic sanity check)
    if api_key:
        api_key = api_key.strip()
        if "\n" in api_key or "\r" in api_key:
            return {"ok": False, "error": "API key must not contain newline characters."}
        if len(api_key) < 8:
            return {"ok": False, "error": "API key appears too short."}

    env_path = _get_hermes_home() / ".env"
    try:
        _write_env_file(env_path, {env_var: api_key})
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.exception("Failed to write env file for provider %s", provider_id)
        return {"ok": False, "error": f"Failed to save API key: {exc}"}

    # Invalidate the model cache so the dropdown refreshes on next request.
    # Using invalidate_models_cache() instead of reload_config() to avoid
    # disrupting active streaming sessions that may be reading config.cfg.
    invalidate_models_cache()

    return {
        "ok": True,
        "provider": provider_id,
        "display_name": _PROVIDER_DISPLAY.get(provider_id, provider_id),
        "action": "updated" if api_key else "removed",
    }


def remove_provider_key(provider_id: str) -> dict[str, Any]:
    """Remove the API key for a provider.

    Removes the key from ``~/.hermes/.env`` (via ``set_provider_key``)
    and also cleans up ``config.yaml`` if the key is stored there
    (``providers.<id>.api_key`` or top-level ``model.api_key`` when this
    provider is the active one).

    Returns a status dict with the operation result.
    """
    result = set_provider_key(provider_id, None)

    # Even if the .env removal succeeded, the key might also live in
    # config.yaml (e.g. providers.<id>.api_key or model.api_key).
    # Clean those up so _provider_has_key() returns False after removal.
    if result.get("ok"):
        _clean_provider_key_from_config(provider_id)

    return result


def _clean_provider_key_from_config(provider_id: str) -> None:
    """Remove provider API key entries from config.yaml.

    Handles three storage locations:
    1. ``providers.<id>.api_key`` — per-provider key
    2. ``model.api_key`` — top-level key (only if provider is active)
    3. ``custom_providers[].api_key`` — custom provider entries

    Writes back to config.yaml only if something was actually removed.
    Uses ``_cfg_lock`` to prevent TOCTOU races.
    """
    from api.config import _cfg_lock

    try:
        config_path = _get_config_path()
    except Exception:
        return

    if not config_path.exists():
        return

    try:
        import yaml as _yaml

        changed = False

        with _cfg_lock:
            raw = config_path.read_text(encoding="utf-8")
            cfg = _yaml.safe_load(raw)
            if not isinstance(cfg, dict):
                return

            # 1. Clean providers.<id>.api_key
            providers_cfg = cfg.get("providers", {})
            if isinstance(providers_cfg, dict):
                provider_cfg = providers_cfg.get(provider_id, {})
                if isinstance(provider_cfg, dict) and provider_cfg.get("api_key"):
                    del provider_cfg["api_key"]
                    changed = True

            # 2. Clean model.api_key — only if this provider is the active one
            model_cfg = cfg.get("model", {})
            if isinstance(model_cfg, dict) and model_cfg.get("api_key"):
                active_provider = model_cfg.get("provider")
                if active_provider and str(active_provider).strip().lower() == provider_id.lower():
                    del model_cfg["api_key"]
                    changed = True

            # 3. Clean custom_providers[].api_key
            custom_providers = cfg.get("custom_providers", [])
            if isinstance(custom_providers, list):
                for cp in custom_providers:
                    if isinstance(cp, dict):
                        cp_name = (cp.get("name") or "").strip().lower().replace(" ", "-")
                        if f"custom:{cp_name}" == provider_id or cp.get("name", "").strip().lower() == provider_id:
                            if cp.get("api_key"):
                                del cp["api_key"]
                                changed = True

            if changed:
                _save_yaml_config_file(config_path, cfg)
        # Sync in-memory cache and bust model TTL cache
        # MUST be called outside _cfg_lock to avoid deadlock:
        # _cfg_lock is a threading.Lock (non-reentrant) and
        # reload_config() also acquires _cfg_lock internally.
        if changed:
            reload_config()
    except Exception:
        logger.exception("Failed to clean provider key from config.yaml for %s", provider_id)
