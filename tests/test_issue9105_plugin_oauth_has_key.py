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


class _FakeRegistry:
    """Stand-in for ``hermes_cli.auth.PROVIDER_REGISTRY`` (a home-aware ``.get``)."""

    def __init__(self, rows):
        self.rows = dict(rows)

    def get(self, key, default=None):
        row_auth_type = self.rows.get(key)
        if row_auth_type is None:
            return default
        return SimpleNamespace(id=key, auth_type=row_auth_type)


def _providers_for(
    monkeypatch,
    tmp_path,
    *,
    auth_type,
    status,
    env_vars=(),
    registry_auth_type=None,
    registry_row_auth_type=None,
):
    """Return the ``solstice`` card from ``get_providers()`` for one fake plugin.

    ``registry_row_auth_type`` sets the effective auth registry row the Agent
    would dispatch on; when ``None`` the fake ``hermes_cli.auth`` has no registry
    at all (older Agent / unavailable), so classification falls back to the profile.
    """
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
    if registry_row_auth_type is not None:
        fake_auth.PROVIDER_REGISTRY = _FakeRegistry({"solstice": registry_row_auth_type})
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


def test_registry_row_oauth_beats_api_key_profile_with_empty_pool(monkeypatch, tmp_path):
    """Row ``oauth_external`` + profile ``api_key`` + empty pool: the Agent builds an
    OAuth status (``configured=True, logged_in=False``), so ``has_key`` is False."""
    card = _providers_for(
        monkeypatch,
        tmp_path,
        auth_type="api_key",
        registry_row_auth_type="oauth_external",
        status=ISSUE_9105_LOGGED_OUT_STATUS,
    )
    assert card is not None
    assert card["has_key"] is False


def test_registry_row_oauth_logged_in_with_api_key_profile_is_has_key(monkeypatch, tmp_path):
    status = {**ISSUE_9105_LOGGED_OUT_STATUS, "logged_in": True, "accounts": 1, "hint": ""}
    card = _providers_for(
        monkeypatch,
        tmp_path,
        auth_type="api_key",
        registry_row_auth_type="oauth_device_code",
        status=status,
    )
    assert card is not None
    assert card["has_key"] is True


@pytest.mark.parametrize("profile_kwargs", [
    {"auth_type": "oauth_external"},
    {"auth_type": "api_key", "registry_auth_type": "oauth_device_code"},
])
def test_registry_row_api_key_beats_oauth_profile_keeps_configured(
    monkeypatch, tmp_path, profile_kwargs
):
    """Row ``api_key`` + OAuth-looking profile: the Agent builds an API-key status,
    where ``configured`` alone counts, so the configured path is preserved.

    The status is synthetic (``logged_in: False``): the Agent's API-key builder sets
    ``logged_in`` equal to ``configured``; this only pins the ``configured`` rule.
    """
    status = {"configured": True, "provider": "solstice", "logged_in": False, "key_source": "config_yaml"}
    card = _providers_for(
        monkeypatch,
        tmp_path,
        registry_row_auth_type="api_key",
        status=status,
        **profile_kwargs,
    )
    assert card is not None
    assert card["has_key"] is True


def test_registry_without_solstice_row_falls_back_to_profile(monkeypatch, tmp_path):
    """A registry that has no row for the provider defers to the cached profile."""
    from api.plugin_providers import plugin_model_provider_is_oauth

    profile = SimpleNamespace(name="solstice", env_vars=(), auth_type="oauth_external", aliases=())
    fake_providers = types.ModuleType("providers")
    fake_providers.list_providers = lambda: [profile]
    monkeypatch.setitem(sys.modules, "providers", fake_providers)
    fake_auth = types.ModuleType("hermes_cli.auth")
    fake_auth.PROVIDER_REGISTRY = _FakeRegistry({})
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)
    invalidate_plugin_model_provider_cache()
    try:
        assert plugin_model_provider_is_oauth("solstice") is True
    finally:
        invalidate_plugin_model_provider_cache()


def test_registry_unavailable_falls_back_to_profile(monkeypatch, tmp_path):
    """Registry import/attribute/lookup failures never raise; the profile decides."""
    from api.plugin_providers import plugin_model_provider_is_oauth

    class _BrokenRegistry:
        def get(self, key, default=None):
            raise RuntimeError("registry exploded")

    profile = SimpleNamespace(name="solstice", env_vars=(), auth_type="oauth_external", aliases=())
    fake_providers = types.ModuleType("providers")
    fake_providers.list_providers = lambda: [profile]
    monkeypatch.setitem(sys.modules, "providers", fake_providers)
    invalidate_plugin_model_provider_cache()
    try:
        # (1) hermes_cli.auth cannot be imported at all
        monkeypatch.setitem(sys.modules, "hermes_cli.auth", None)
        assert plugin_model_provider_is_oauth("solstice") is True
        # (2) the registry lookup itself raises
        fake_auth = types.ModuleType("hermes_cli.auth")
        fake_auth.PROVIDER_REGISTRY = _BrokenRegistry()
        monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)
        assert plugin_model_provider_is_oauth("solstice") is True
    finally:
        invalidate_plugin_model_provider_cache()


def test_alternating_effective_rows_are_not_cached(monkeypatch):
    """Switching the effective registry row (profile home change) flips the answer on
    the very next call, even though the discovery profile stays cached and unchanged."""
    from api.plugin_providers import plugin_model_provider_is_oauth

    profile = SimpleNamespace(name="solstice", env_vars=(), auth_type="api_key", aliases=())
    fake_providers = types.ModuleType("providers")
    fake_providers.list_providers = lambda: [profile]
    monkeypatch.setitem(sys.modules, "providers", fake_providers)
    registry = _FakeRegistry({"solstice": "oauth_external"})
    fake_auth = types.ModuleType("hermes_cli.auth")
    fake_auth.PROVIDER_REGISTRY = registry
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)
    invalidate_plugin_model_provider_cache()
    try:
        answers = []
        for row_auth_type in ("oauth_external", "api_key", "oauth_device_code", "api_key"):
            registry.rows["solstice"] = row_auth_type
            answers.append(plugin_model_provider_is_oauth("solstice"))
        assert answers == [True, False, True, False]
    finally:
        invalidate_plugin_model_provider_cache()


@pytest.mark.parametrize("row_type", ["external_process", "oauth_custom"])
def test_registry_row_non_oauth_types_keep_configured(monkeypatch, tmp_path, row_type):
    """Non-OAuth effective rows keep the ``configured`` rule, including a row type
    that merely starts with ``oauth``.

    The status is synthetic (``configured: True, logged_in: False``): the Agent's
    external-process builder sets ``logged_in`` equal to ``configured``; this only
    pins that WebUI does not require ``logged_in`` outside OAuth.
    """
    status = {"configured": True, "provider": "solstice", "logged_in": False}
    card = _providers_for(
        monkeypatch,
        tmp_path,
        auth_type="oauth_external",
        registry_row_auth_type=row_type,
        status=status,
    )
    assert card is not None
    assert card["has_key"] is True


def test_registry_oauth_row_with_separately_configured_api_key_keeps_has_key(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("SOLSTICE_API_KEY=test-solstice-key-12345\n", encoding="utf-8")
    card = _providers_for(
        monkeypatch,
        tmp_path,
        auth_type="api_key",
        registry_row_auth_type="oauth_external",
        status=ISSUE_9105_LOGGED_OUT_STATUS,
        env_vars=("SOLSTICE_API_KEY",),
    )
    assert card is not None
    assert card["has_key"] is True


# The Agent reports ``needs_refresh=True`` when every pooled token has expired but
# refresh material and the plugin's refresh hook exist: the account is still usable
# (the Agent refreshes it on first use). The WebUI's own pool view can be empty at
# that moment, for example while its pool cache still holds the state from before
# a CLI sign-in, so the Agent's status is the only signal that an account exists.
EXPIRED_BUT_REFRESHABLE_STATUS = {
    **ISSUE_9105_LOGGED_OUT_STATUS,
    "needs_refresh": True,
    "accounts": 1,
    "hint": "Run `hermes auth add solstice` to sign in.",
}


@pytest.mark.parametrize("auth_type", ["oauth_external", "oauth_device_code"])
def test_oauth_plugin_expired_but_refreshable_is_has_key(monkeypatch, tmp_path, auth_type):
    card = _providers_for(
        monkeypatch, tmp_path, auth_type=auth_type, status=EXPIRED_BUT_REFRESHABLE_STATUS
    )
    assert card is not None
    assert card["has_key"] is True


def test_registry_row_oauth_expired_but_refreshable_is_has_key(monkeypatch, tmp_path):
    """Same through the effective registry row (row OAuth, cached profile api_key)."""
    card = _providers_for(
        monkeypatch,
        tmp_path,
        auth_type="api_key",
        registry_row_auth_type="oauth_external",
        status=EXPIRED_BUT_REFRESHABLE_STATUS,
    )
    assert card is not None
    assert card["has_key"] is True


@pytest.mark.parametrize("status_extra", [
    {"needs_refresh": False, "accounts": 1},   # expired, nothing to refresh with
    {"needs_refresh": None},                    # odd value from a producer
])
def test_oauth_plugin_without_a_refreshable_account_stays_not_has_key(monkeypatch, tmp_path, status_extra):
    status = {**ISSUE_9105_LOGGED_OUT_STATUS, **status_extra}
    card = _providers_for(monkeypatch, tmp_path, auth_type="oauth_external", status=status)
    assert card is not None
    assert card["has_key"] is False


def test_oauth_plugin_status_without_needs_refresh_key_stays_not_has_key(monkeypatch, tmp_path):
    """A producer that omits ``needs_refresh`` keeps the signed-out answer."""
    status = {k: v for k, v in ISSUE_9105_LOGGED_OUT_STATUS.items() if k != "needs_refresh"}
    card = _providers_for(monkeypatch, tmp_path, auth_type="oauth_device_code", status=status)
    assert card is not None
    assert card["has_key"] is False
