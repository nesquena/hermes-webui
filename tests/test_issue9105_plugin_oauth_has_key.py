"""Regression tests for #9105: an OAuth-shaped plugin provider that is only
*registered* (``configured=True``) but not signed in (``logged_in=False``,
``accounts=0``) must not be reported as ``has_key=True`` by ``get_providers()``.

For OAuth plugins the Agent's ``configured`` flag means "the provider profile is
registered", not "an account is authenticated".  ``configured`` must still count
for non-OAuth plugin providers (API-key / external-process), and
``logged_in`` must not become a universal runtime-readiness rule.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

import api.config as config
import api.profiles as profiles
from api.plugin_providers import invalidate_plugin_model_provider_cache

# Exact ``get_auth_status("solstice")`` payload quoted in the issue.
ISSUE_9105_LOGGED_OUT_STATUS = {
    "configured": True,
    "provider": "solstice",
    "logged_in": False,
    "needs_refresh": False,
    "accounts": 0,
    "base_url": "",
    "hint": "Run `hermes auth add solstice` to sign in.",
}


def _providers_for(
    monkeypatch, tmp_path, *, auth_type, status, env_vars=(), registry_auth_type=None
):
    """Return the ``solstice`` card from ``get_providers()`` for one fake plugin."""
    profile = SimpleNamespace(
        name="solstice",
        display_name="Solstice",
        env_vars=env_vars,
        auth_type=auth_type,
        aliases=(),
    )
    if registry_auth_type is not None:
        profile.registry_auth_type = registry_auth_type
    fake_providers = types.ModuleType("providers")
    fake_providers.list_providers = lambda: [profile]
    monkeypatch.setitem(sys.modules, "providers", fake_providers)
    invalidate_plugin_model_provider_cache()

    fake_pkg = types.ModuleType("hermes_cli")
    fake_pkg.__path__ = []
    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.list_available_providers = lambda: []
    fake_models.provider_model_ids = lambda pid: []
    fake_auth = types.ModuleType("hermes_cli.auth")
    fake_auth.get_auth_status = lambda pid: dict(status) if pid == "solstice" else {}
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.cfg.clear()
    config.cfg["model"] = {}
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0
    config.invalidate_models_cache()

    from api.providers import get_providers

    try:
        result = get_providers()
        return next((p for p in result["providers"] if p["id"] == "solstice"), None)
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime
        config.invalidate_models_cache()
        invalidate_plugin_model_provider_cache()


@pytest.mark.parametrize("auth_type", ["oauth_external", "oauth_device_code"])
def test_oauth_plugin_registered_but_logged_out_is_not_has_key(monkeypatch, tmp_path, auth_type):
    card = _providers_for(
        monkeypatch, tmp_path, auth_type=auth_type, status=ISSUE_9105_LOGGED_OUT_STATUS
    )
    assert card is not None, "plugin provider must still appear in Settings -> Providers"
    assert card["has_key"] is False


@pytest.mark.parametrize("auth_type", ["oauth_external", "oauth_device_code"])
def test_oauth_plugin_logged_in_is_has_key(monkeypatch, tmp_path, auth_type):
    status = {
        **ISSUE_9105_LOGGED_OUT_STATUS,
        "logged_in": True,
        "accounts": 1,
        "hint": "",
    }
    card = _providers_for(monkeypatch, tmp_path, auth_type=auth_type, status=status)
    assert card is not None
    assert card["has_key"] is True


def test_api_key_plugin_configured_is_has_key(monkeypatch, tmp_path):
    """Non-OAuth plugin: ``configured`` alone is still enough (not a universal logged_in rule)."""
    status = {"configured": True, "provider": "solstice", "logged_in": False, "key_source": "config_yaml"}
    card = _providers_for(monkeypatch, tmp_path, auth_type="api_key", status=status)
    assert card is not None
    assert card["has_key"] is True


@pytest.mark.parametrize("auth_type", ["external_process", "none"])
def test_non_oauth_auth_types_keep_configured(monkeypatch, tmp_path, auth_type):
    """Synthetic status (``configured: True, logged_in: False``): ``logged_in`` is not required outside OAuth."""
    status = {"configured": True, "provider": "solstice", "logged_in": False}
    card = _providers_for(monkeypatch, tmp_path, auth_type=auth_type, status=status)
    assert card is not None
    assert card["has_key"] is True


def test_oauth_registry_auth_type_wins_over_auth_type(monkeypatch, tmp_path):
    """The Agent picks the status builder by ``registry_auth_type or auth_type``."""
    card = _providers_for(
        monkeypatch,
        tmp_path,
        auth_type="api_key",
        registry_auth_type="oauth_external",
        status=ISSUE_9105_LOGGED_OUT_STATUS,
    )
    assert card is not None
    assert card["has_key"] is False


def test_oauth_prefixed_unknown_auth_type_is_not_oauth(monkeypatch, tmp_path):
    """Only the exact OAuth types count; a plugin-defined ``oauth*`` value does not."""
    card = _providers_for(
        monkeypatch, tmp_path, auth_type="oauth_custom", status=ISSUE_9105_LOGGED_OUT_STATUS
    )
    assert card is not None
    assert card["has_key"] is True


def test_oauth_plugin_with_separately_configured_api_key_keeps_has_key(monkeypatch, tmp_path):
    """A key configured out-of-band (env file) is kept even when the OAuth status is logged out."""
    (tmp_path / ".env").write_text("SOLSTICE_API_KEY=test-solstice-key-12345\n", encoding="utf-8")
    card = _providers_for(
        monkeypatch,
        tmp_path,
        auth_type="oauth_external",
        status=ISSUE_9105_LOGGED_OUT_STATUS,
        env_vars=("SOLSTICE_API_KEY",),
    )
    assert card is not None
    assert card["has_key"] is True
