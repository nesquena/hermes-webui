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
