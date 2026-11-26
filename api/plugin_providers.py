"""Helpers for model-provider plugins (``plugins/model-providers/<name>/``).

The Hermes agent discovers these via ``providers.list_providers()`` and exposes
them in the CLI model picker.  WebUI must mirror that registry instead of
relying only on the static ``_PROVIDER_DISPLAY`` / ``_PROVIDER_MODELS`` tables.

Bundled agent profiles (gemini, nous, custom, …) also live in
``list_providers()``.  WebUI already handles those via static tables and
dedicated code paths — only *plugin-only* slugs (e.g. user-installed yandex)
should take the plugin discovery path.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

_PROFILES_LOCK = threading.Lock()
_PROFILES_BY_NAME: dict[str, Any] | None = None
_WEBUI_STATIC_PROVIDER_IDS: frozenset[str] | None = None


def _webui_static_provider_ids() -> frozenset[str]:
    """Provider slugs already owned by WebUI static tables / special cases."""
    global _WEBUI_STATIC_PROVIDER_IDS
    if _WEBUI_STATIC_PROVIDER_IDS is not None:
        return _WEBUI_STATIC_PROVIDER_IDS
    try:
        from api.config import _PROVIDER_DISPLAY, _PROVIDER_MODELS

        static = (
            frozenset(_PROVIDER_DISPLAY.keys())
            | frozenset(_PROVIDER_MODELS.keys())
            | frozenset({"custom"})
        )
    except Exception:
        static = frozenset({"custom"})
    _WEBUI_STATIC_PROVIDER_IDS = static
    return static


def _load_profiles_by_name() -> dict[str, Any]:
    try:
        from providers import list_providers
    except Exception:
        logger.debug("providers package unavailable for plugin discovery", exc_info=True)
        return {}

    result: dict[str, Any] = {}
    try:
        for profile in list_providers():
            name = str(getattr(profile, "name", "") or "").strip().lower()
            if name:
                result[name] = profile
    except Exception:
        logger.debug("Failed to enumerate model-provider plugins", exc_info=True)
        return {}
    return result


def plugin_model_provider_profiles() -> dict[str, Any]:
    """Return registered model-provider profiles keyed by canonical slug."""
    global _PROFILES_BY_NAME
    cached = _PROFILES_BY_NAME
    if cached is not None:
        return cached
    with _PROFILES_LOCK:
        if _PROFILES_BY_NAME is None:
            _PROFILES_BY_NAME = _load_profiles_by_name()
        return _PROFILES_BY_NAME


def invalidate_plugin_model_provider_cache() -> None:
    """Clear cached plugin discovery (e.g. after config reload)."""
    global _PROFILES_BY_NAME
    with _PROFILES_LOCK:
        _PROFILES_BY_NAME = None


def plugin_model_provider_ids() -> frozenset[str]:
    """Slugs from ``list_providers()`` that are not already WebUI-static."""
    static = _webui_static_provider_ids()
    return frozenset(
        pid for pid in plugin_model_provider_profiles().keys() if pid not in static
    )


def plugin_model_provider_display_name(provider_id: str) -> str | None:
    profile = plugin_model_provider_profiles().get((provider_id or "").strip().lower())
    if profile is None:
        return None
    return str(getattr(profile, "display_name", "") or getattr(profile, "name", "") or "").strip() or None


def plugin_model_provider_api_key_env_var(provider_id: str) -> str | None:
    """Return the primary API-key env var for a plugin provider, if any."""
    profile = plugin_model_provider_profiles().get((provider_id or "").strip().lower())
    if profile is None:
        return None
    env_vars = getattr(profile, "env_vars", ()) or ()
    for var in env_vars:
        upper = str(var).upper()
        if upper.endswith("_BASE_URL") or upper.endswith("_URL"):
            continue
        if upper.endswith("_FOLDER_ID"):
            continue
        return str(var)
    return None


# Exact OAuth auth types, matching the Agent (hermes_cli/auth_plugin_providers.py).
_OAUTH_PLUGIN_AUTH_TYPES = frozenset({"oauth_device_code", "oauth_external"})


def _effective_registry_auth_type(provider_id: str) -> str | None:
    """Return the auth type of the current home's effective registry row.

    This reads ``hermes_cli.auth.PROVIDER_REGISTRY.get()``, the same home-aware
    view that the Agent's ``get_auth_status()`` dispatcher consults through
    ``_registry_lookup`` before it picks a status builder. It is the most stable
    public surface for that row (WebUI already imports the name elsewhere), and
    it is read on every call, so a profile switch is never masked by a cached
    decision. Returns ``None`` when the registry is unavailable or has no row.
    """
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY

        row = PROVIDER_REGISTRY.get(provider_id)
    except Exception:
        logger.debug("Auth registry unavailable for %s", provider_id, exc_info=True)
        return None
    if row is None:
        return None
    return str(getattr(row, "auth_type", "") or "").strip().lower()


def plugin_model_provider_is_oauth(provider_id: str) -> bool:
    """True when the Agent would build this plugin provider's status as OAuth.

    The Agent's ``get_auth_status()`` dispatches on the ``auth_type`` of the
    effective auth registry row for the current home, not on the cached provider
    profile. A home-level profile override keeps the existing row's auth type, so
    the two can disagree; the row wins. The row is therefore consulted first and
    must be exactly ``oauth_device_code`` or ``oauth_external`` (no prefix
    matching). When the registry is unavailable or has no row for the provider,
    the type is read from the cached profile (``registry_auth_type`` if set,
    otherwise ``auth_type``), as before.

    For OAuth providers the Agent's status reports ``configured=True`` as soon as
    the profile is registered, regardless of sign-in, so callers must look at
    ``logged_in`` instead (#9105).
    """
    pid = (provider_id or "").strip().lower()
    auth_type = _effective_registry_auth_type(pid)
    if auth_type is None:
        profile = plugin_model_provider_profiles().get(pid)
        if profile is None:
            return False
        auth_type = str(
            getattr(profile, "registry_auth_type", None) or getattr(profile, "auth_type", "") or ""
        ).strip().lower()
    return auth_type in _OAUTH_PLUGIN_AUTH_TYPES


def effective_provider_env_var(provider_id: str, static_map: dict[str, str]) -> str | None:
    pid = (provider_id or "").strip().lower()
    if not pid:
        return None
    if pid in static_map:
        return static_map[pid]
    if not is_plugin_model_provider(pid):
        return None
    return plugin_model_provider_api_key_env_var(pid)


def effective_provider_display_name(provider_id: str, static_map: dict[str, str]) -> str:
    pid = (provider_id or "").strip().lower()
    if pid in static_map:
        return static_map[pid]
    if is_plugin_model_provider(pid):
        plugin_name = plugin_model_provider_display_name(pid)
        if plugin_name:
            return plugin_name
    return pid.replace("-", " ").title()


def is_plugin_model_provider(provider_id: str) -> bool:
    """True for plugin-only providers (not already in WebUI static tables)."""
    pid = (provider_id or "").strip().lower()
    if not pid or pid in _webui_static_provider_ids():
        return False
    return pid in plugin_model_provider_profiles()
