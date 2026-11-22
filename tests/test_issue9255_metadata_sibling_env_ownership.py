"""#9255 round 3: an ambient profile credential must not make a metadata-only
sibling a competing claimant.

The #8841 collision audit calls :func:`_custom_record_owns_connection` for every
sibling that NAMES the requested slug. That predicate ends with
``_resolve_custom_record_key(api_key, key_env, pid)``, whose third argument is the
REQUESTED provider id — so it falls back to the ``CUSTOM_<SLUG>_API_KEY``
convention. A sibling that declares ONLY a display name and model metadata
therefore "owns a connection" as soon as that ambient variable is populated,
and the audit refuses an otherwise valid exact-key provider.

The discriminator is the sibling's OWN declared connection fields. Ambient
credential fallback must never manufacture a claimant.

Drives the real production entry points — :func:`_select_custom_provider_record`
and :func:`resolve_custom_provider_connection` — with synthetic values only. No
network, no Agent runtime, no real credential.
"""

from __future__ import annotations

import copy
import sys
import types

import pytest

from api import config

URL_EXACT = "https://exact-key.invalid/v1"
URL_SIBLING = "https://metadata-sibling.invalid/v1"
KEY_EXACT = "synthetic-exact-key"
ENV_NAME = "CUSTOM_X_AI_API_KEY"
ENV_VALUE = "ambient-profile-key"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(tmp_path / "profile" / "config.yaml"))
    pkg = types.ModuleType("hermes_cli")
    pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", types.ModuleType("hermes_cli.runtime_provider"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", types.ModuleType("hermes_cli.config"))
    monkeypatch.setitem(sys.modules, "agent.secret_scope", types.ModuleType("agent.secret_scope"))
    monkeypatch.setattr(config._thread_ctx, "env", {}, raising=False)
    monkeypatch.setattr(config._thread_ctx, "block_process_env_fallback", True, raising=False)


def _bind(monkeypatch, data):
    monkeypatch.setattr(config, "cfg", data)
    monkeypatch.setattr(config, "get_config", lambda: data)
    monkeypatch.setattr(config, "get_config_snapshot", lambda: copy.deepcopy(data))


def _exact_and_metadata_sibling():
    """The exact canonical key owns the route; the sibling only NAMES the slug."""
    return {
        "model": {"provider": "custom:x_ai", "default": "synthetic-model"},
        "providers": {
            "custom:x_ai": {"base_url": URL_EXACT, "api_key": KEY_EXACT},
            "myproxy": {"name": "x_ai", "models": ["synthetic-model"]},
        },
    }


def _populated_profile_env(monkeypatch):
    """A conventional profile credential for the REQUESTED provider is present."""
    monkeypatch.setattr(config._thread_ctx, "env", {ENV_NAME: ENV_VALUE}, raising=False)


def _select(pid, cfg_data):
    return config._select_custom_provider_record(pid, config._custom_provider_slug_key(pid), cfg_data)


# ── The gate's discriminator: the exact record survives a populated environment ──


def test_metadata_sibling_with_populated_profile_env_is_not_a_claimant(monkeypatch):
    """RED on the broken head: the metadata-only sibling becomes a claimant."""
    cfg = _exact_and_metadata_sibling()
    _bind(monkeypatch, cfg)
    _populated_profile_env(monkeypatch)

    record, source, is_exact, status = _select("custom:x_ai", cfg)

    assert status == config.CUSTOM_SELECTION_KEYED
    assert status == config.CUSTOM_SELECTION_KEYED
    assert record is not None, "the exact-key provider must still own the route"
    assert record.get("base_url") == URL_EXACT
    assert record.get("api_key") == KEY_EXACT


def test_metadata_sibling_with_populated_profile_env_keeps_the_connection(monkeypatch):
    """The connection boundary keeps the same endpoint/credential pair."""
    cfg = _exact_and_metadata_sibling()
    _bind(monkeypatch, cfg)
    _populated_profile_env(monkeypatch)

    api_key, base_url = config.resolve_custom_provider_connection("custom:x_ai")

    assert base_url == URL_EXACT
    assert api_key == KEY_EXACT


def test_metadata_sibling_with_empty_profile_env_is_not_a_claimant(monkeypatch):
    """The empty-environment control the gate pins stays green."""
    cfg = _exact_and_metadata_sibling()
    _bind(monkeypatch, cfg)

    record, source, is_exact, status = _select("custom:x_ai", cfg)

    assert status == config.CUSTOM_SELECTION_KEYED
    assert record is not None
    assert record.get("base_url") == URL_EXACT


def test_no_metadata_sibling_with_populated_profile_env_is_not_a_claimant(monkeypatch):
    """The no-sibling control keeps working with a populated environment."""
    cfg = {
        "model": {"provider": "custom:x_ai", "default": "synthetic-model"},
        "providers": {"custom:x_ai": {"base_url": URL_EXACT, "api_key": KEY_EXACT}},
    }
    _bind(monkeypatch, cfg)
    _populated_profile_env(monkeypatch)

    record, source, is_exact, status = _select("custom:x_ai", cfg)

    assert status == config.CUSTOM_SELECTION_KEYED
    assert status == config.CUSTOM_SELECTION_KEYED
    assert record is not None
    assert record.get("base_url") == URL_EXACT


# ── A sibling that DOES declare its own connection still collides ──


def test_sibling_declaring_its_own_connection_still_collides(monkeypatch):
    """The #8841 goal survives: a real second claimant is still refused."""
    cfg = {
        "model": {"provider": "custom:x_ai", "default": "synthetic-model"},
        "providers": {
            "custom:x_ai": {"base_url": URL_EXACT, "api_key": KEY_EXACT},
            "myproxy": {"name": "x_ai", "base_url": URL_SIBLING, "api_key": "synthetic-sibling-key"},
        },
    }
    _bind(monkeypatch, cfg)
    _populated_profile_env(monkeypatch)

    with pytest.raises(config.AmbiguousCustomProviderError) as exc:
        _select("custom:x_ai", cfg)

    assert "'custom:x_ai'" in exc.value.message and "'myproxy'" in exc.value.message


def test_sibling_declaring_its_own_key_env_still_collides(monkeypatch):
    """A sibling declaring ``key_env`` owns a connection and still collides."""
    cfg = {
        "model": {"provider": "custom:x_ai", "default": "synthetic-model"},
        "providers": {
            "custom:x_ai": {"base_url": URL_EXACT, "api_key": KEY_EXACT},
            "myproxy": {"name": "x_ai", "key_env": "SIBLING_OWN_KEY"},
        },
    }
    _bind(monkeypatch, cfg)

    with pytest.raises(config.AmbiguousCustomProviderError) as exc:
        _select("custom:x_ai", cfg)

    assert "'custom:x_ai'" in exc.value.message and "'myproxy'" in exc.value.message


def test_disabled_sibling_with_populated_profile_env_does_not_collide(monkeypatch):
    """A disabled sibling claims nothing, even with a populated environment."""
    cfg = {
        "model": {"provider": "custom:x_ai", "default": "synthetic-model"},
        "providers": {
            "custom:x_ai": {"base_url": URL_EXACT, "api_key": KEY_EXACT},
            "myproxy": {"name": "x_ai", "enabled": False, "models": ["synthetic-model"]},
        },
    }
    _bind(monkeypatch, cfg)
    _populated_profile_env(monkeypatch)

    record, source, is_exact, status = _select("custom:x_ai", cfg)

    assert status == config.CUSTOM_SELECTION_KEYED
    assert status == config.CUSTOM_SELECTION_KEYED
    assert record is not None


def test_unrelated_sibling_with_populated_profile_env_does_not_collide(monkeypatch):
    """A sibling naming a DIFFERENT slug never becomes a claimant."""
    cfg = {
        "model": {"provider": "custom:x_ai", "default": "synthetic-model"},
        "providers": {
            "custom:x_ai": {"base_url": URL_EXACT, "api_key": KEY_EXACT},
            "otherproxy": {"name": "other_slug", "models": ["synthetic-model"]},
        },
    }
    _bind(monkeypatch, cfg)
    _populated_profile_env(monkeypatch)

    record, source, is_exact, status = _select("custom:x_ai", cfg)

    assert status == config.CUSTOM_SELECTION_KEYED
    assert status == config.CUSTOM_SELECTION_KEYED
    assert record is not None
