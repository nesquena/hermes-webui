"""Regression tests for #9209: a name mixing non-ASCII and ASCII keeps its identity.

A ``custom_providers[]`` entry named ``晨光 Relay`` used to be listed by the WebUI
under the ASCII-only fold of its name (``custom:relay``), while the Agent mints
``custom:晨光-relay`` (``hermes_cli.providers.custom_provider_slug``: lowercase,
spaces to dashes, non-ASCII kept). Saving the picker's value therefore wrote an
identity the Agent answered "Unknown provider" to, breaking CLI, cron and
compression on that profile.

The slug is derived from the name in ONE place,
``api.config._custom_provider_slug_from_name()``, and every caller inherits it.
These tests pin three things against that single producer:

* mixed names mint the Agent's identity, so the saved value resolves;
* names the Agent's formula maps cleanly (lowercase words separated by single
  spaces) are identical to master's ASCII fold, byte for byte -- ASCII-only
  names do not change at all;
* the sanitization that exists for the ``@provider:model`` colon grammar is
  kept: no derived slug ever carries a ``':'`` beyond its own ``custom:`` prefix.

No Agent installation, live endpoint or real profile is required.
"""
from __future__ import annotations

import copy
import re
import sys
import types

import pytest

from api import config, providers


MIXED_NAME = "晨光 Relay"
MIXED_ID = "custom:晨光-relay"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Stub only the external catalog boundary, never the slug producer."""
    pkg = types.ModuleType("hermes_cli")
    pkg.__path__ = []
    models = types.ModuleType("hermes_cli.models")
    models.list_available_providers = lambda: []
    models.provider_model_ids = lambda _pid: []
    auth = types.ModuleType("hermes_cli.auth")
    auth.get_auth_status = lambda _pid: {}
    auth.read_credential_pool = lambda _pid: []
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", auth)
    monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models_cache.json")
    monkeypatch.setattr(config, "_get_auth_store_path", lambda: tmp_path / "auth.json")
    monkeypatch.setattr(config._thread_ctx, "env", {}, raising=False)
    monkeypatch.setattr(
        config._thread_ctx, "block_process_env_fallback", True, raising=False
    )

    def no_network(*_args, **_kwargs):
        raise AssertionError("unexpected network access")

    monkeypatch.setattr("socket.getaddrinfo", no_network)
    monkeypatch.setattr("urllib.request.urlopen", no_network)
    config.invalidate_models_cache()
    providers.invalidate_providers_cache()
    yield
    config.invalidate_models_cache()
    providers.invalidate_providers_cache()


def master_ascii_fold(name):
    """The pre-#9209 producer, kept here as the reference for "unchanged" names."""
    raw = str(name or "").strip().lower()
    if not raw:
        return ""
    if raw.startswith("custom:"):
        return raw
    slug = re.sub(r"[^a-z0-9._-]+", "-", raw).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return ("custom:" + slug) if slug else ""


def bind(monkeypatch, data):
    monkeypatch.setattr(config, "cfg", data)
    monkeypatch.setattr(config, "get_config", lambda: data)
    monkeypatch.setattr(providers, "get_config", lambda: data)


def test_mixed_name_mints_the_agent_identity():
    """The load-bearing case: the WebUI saves what the Agent resolves."""
    assert config._custom_provider_slug_from_name(MIXED_NAME) == MIXED_ID
    assert config._custom_provider_slug_from_name(MIXED_NAME) == (
        config._agent_custom_provider_slug(MIXED_NAME)
    )
    # And it is not master's ASCII-only fold, which the Agent rejects.
    assert config._custom_provider_slug_from_name(MIXED_NAME) != master_ascii_fold(
        MIXED_NAME
    )
    assert master_ascii_fold(MIXED_NAME) == "custom:relay"


@pytest.mark.parametrize(
    "name",
    [
        "Local Gateway",
        "Local (127.0.0.1:15721)",
        "  My  Provider!! ",
        "omni",
        "foo-bar",
        "Foo (Bar)",
        "custom:omni",
        "",
        "   ",
        "---",
    ],
)
def test_ascii_names_are_byte_identical_to_master(name):
    """No behaviour change for names without non-ASCII characters."""
    assert config._custom_provider_slug_from_name(name) == master_ascii_fold(name)


def test_ascii_name_examples_are_pinned_explicitly():
    assert config._custom_provider_slug_from_name("Local Gateway") == (
        "custom:local-gateway"
    )
    assert config._custom_provider_slug_from_name("Local (127.0.0.1:15721)") == (
        "custom:local-127.0.0.1-15721"
    )


def test_colon_never_reaches_the_slug_body():
    """The @provider:model grammar must never receive a second separator."""
    for name in [
        "Local (127.0.0.1:15721)",
        "a:b c",
        "custom provider: x",
        MIXED_NAME,
        "晨光：Relay",
        "Gateway: 8080",
    ]:
        slug = config._custom_provider_slug_from_name(name)
        body = slug.split(":", 1)[1] if slug.startswith("custom:") else slug
        assert ":" not in body, (name, slug)
        # Nothing else the grammar cannot carry either.
        assert not re.search(r"\s", body), (name, slug)


def test_non_ascii_only_name_does_not_regress_to_empty():
    """#8017/#8164 span: a pure non-ASCII name keeps its own characters."""
    assert config._custom_provider_slug_from_name("晨光") == "custom:晨光"
    assert config._custom_provider_slug_from_name("晨光") == (
        config._agent_custom_provider_slug("晨光")
    )
    assert config._custom_provider_slug_key("晨光") == "晨光"


def test_decomposed_accent_keeps_its_mark():
    """A name typed on a normalising-away keyboard must not lose its accent."""
    name = "cafe\u0301 Latte"
    assert config._custom_provider_slug_from_name(name) == "custom:cafe\u0301-latte"
    assert config._custom_provider_slug_from_name(name) == (
        config._agent_custom_provider_slug(name)
    )


@pytest.mark.parametrize(
    "name",
    ["晨光 Relay", "晨光", "Café Relay", "ＡＢＣ Gateway", "Local Gateway", "omni"],
)
def test_slug_and_agent_identity_agree(name):
    """Round trip: the value the picker saves is the value the Agent mints."""
    slug = config._custom_provider_slug_from_name(name)
    assert slug
    assert slug == config._agent_custom_provider_slug(name)
    # Re-running the producer over its own output is a no-op.
    assert config._custom_provider_slug_from_name(slug) == slug
    assert config._custom_provider_slug_key(name) == slug.split(":", 1)[1]


def test_saved_mixed_name_resolves_to_its_own_entry(monkeypatch):
    """The minted id names the entry, so a Save produces a resolvable route."""
    data = {
        "model": {"provider": MIXED_ID, "default": "m1"},
        "custom_providers": [
            {
                "name": MIXED_NAME,
                "base_url": "https://relay.invalid/v1",
                "api_key": "sk-test",
                "model": "m1",
            }
        ],
    }
    before = copy.deepcopy(data)
    bind(monkeypatch, data)

    entries = config._custom_provider_entries(data)
    assert [entry["name"] for entry in entries] == [MIXED_NAME]

    # The picker's id for the entry is the one the Agent accepts...
    assert providers._custom_provider_name_matches(MIXED_ID, MIXED_NAME) is True

    # ...and the record's alias set carries the Agent's identity too.
    aliases = config._custom_provider_alias_set(MIXED_NAME, MIXED_NAME)
    assert MIXED_ID in aliases
    assert config._agent_custom_provider_slug(MIXED_NAME) in aliases

    # Reading the catalog does not rewrite the config.
    assert data == before


def test_ascii_saved_id_keeps_matching(monkeypatch):
    """ASCII routes still match the id they were saved with."""
    data = {
        "model": {"provider": "custom:local-gateway", "default": "m1"},
        "custom_providers": [
            {
                "name": "Local Gateway",
                "base_url": "https://gw.invalid/v1",
                "api_key": "sk-test",
                "model": "m1",
            }
        ],
    }
    bind(monkeypatch, data)
    assert providers._custom_provider_name_matches(
        "custom:local-gateway", "Local Gateway"
    ) is True
    assert config._custom_provider_slug_from_name("Local Gateway") == (
        "custom:local-gateway"
    )
