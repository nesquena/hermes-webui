"""Regression tests for #9209 (round 2): a previously saved WebUI id keeps resolving.

The #9209 repair mints the Agent's identity (``custom:晨光-relay``) for a name that
mixes scripts. Before that repair the WebUI minted -- and SAVED -- its own
ASCII-only fold (``custom:relay``) for the same entry, so ``model.provider`` on
disk can still carry that legacy id. Changing only the producer made those saved
ids stop resolving:

* ``resolve_custom_provider_connection('custom:relay', return_provenance=True)``
  returns ``(None, None, False)`` instead of the entry's key, endpoint and
  ``True``;
* ``providers._custom_provider_name_matches('custom:relay', '晨光 Relay')`` is
  ``False``, so the picker no longer recognises the entry.

The contract these tests pin, starting from the OLD saved id:

* both ids name the same entry -- the Agent's canonical one and the legacy WebUI
  fold -- so a profile saved before the repair keeps its route;
* a canonical owner outranks a legacy alias (an entry literally named ``Relay``
  owns ``custom:relay``, not ``晨光 Relay``);
* a legacy alias claimed by TWO entries is refused instead of resolved to
  whichever record comes first, because that pairs one endpoint with another
  entry's key;
* an id nobody claims still owns nothing.

No Agent install, live endpoint or real profile is required.
"""
from __future__ import annotations

import re
import sys
import types

import pytest

from api import config, providers


MIXED_NAME = "晨光 Relay"
MIXED_ID = "custom:晨光-relay"
LEGACY_ID = "custom:relay"
MIXED_URL = "https://relay.invalid/v1"
MIXED_KEY = "legacy-mixed-secret"
ASCII_URL = "https://plain.invalid/v1"
ASCII_KEY = "legacy-plain-secret"
OMEGA_URL = "https://omega.invalid/v1"
OMEGA_KEY = "legacy-omega-secret"


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


def bind(monkeypatch, data):
    monkeypatch.setattr(config, "cfg", data)
    monkeypatch.setattr(config, "get_config", lambda: data)
    monkeypatch.setattr(providers, "get_config", lambda: data)


def entry(name, url, key):
    return {"name": name, "base_url": url, "api_key": key, "model": "m1"}


def saved_ascii_fold(name):
    """The id the WebUI SAVED before #9209 (master's producer), kept as the reference."""
    raw = str(name or "").strip().lower()
    if not raw:
        return ""
    if raw.startswith("custom:"):
        return raw
    slug = re.sub(r"[^a-z0-9._-]+", "-", raw).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return ("custom:" + slug) if slug else ""


def cfg_with(*entries, provider=LEGACY_ID):
    return {"model": {"provider": provider, "default": "m1"}, "custom_providers": list(entries)}


def connection_or_refusal(provider_id):
    """``resolve_custom_provider_connection``'s answer, with a refusal read as empty."""
    try:
        return config.resolve_custom_provider_connection(
            provider_id, return_provenance=True
        )
    except config.AmbiguousCustomProviderError:
        return (None, None, False)


def test_legacy_saved_id_still_resolves_the_connection(monkeypatch):
    """The id already written to ``model.provider`` must keep naming its entry."""
    bind(monkeypatch, cfg_with(entry(MIXED_NAME, MIXED_URL, MIXED_KEY)))
    assert connection_or_refusal(LEGACY_ID) == (MIXED_KEY, MIXED_URL, True)
    # ...and the picker recognises it, which is the same identity test.
    assert providers._custom_provider_name_matches(LEGACY_ID, MIXED_NAME) is True


def test_new_agent_identity_still_resolves_the_connection(monkeypatch):
    """The repair is retained: the Agent's own id keeps working."""
    bind(monkeypatch, cfg_with(entry(MIXED_NAME, MIXED_URL, MIXED_KEY), provider=MIXED_ID))
    assert connection_or_refusal(MIXED_ID) == (MIXED_KEY, MIXED_URL, True)
    assert providers._custom_provider_name_matches(MIXED_ID, MIXED_NAME) is True


def test_canonical_owner_outranks_a_legacy_alias(monkeypatch):
    """An entry literally named ``Relay`` owns ``custom:relay``; the alias cannot take it."""
    plain = entry("Relay", ASCII_URL, ASCII_KEY)
    mixed = entry(MIXED_NAME, MIXED_URL, MIXED_KEY)
    data = cfg_with(plain, mixed)
    bind(monkeypatch, data)

    entries = config._custom_provider_entries(data)
    assert config._unique_custom_provider_entry(entries, "relay") is plain
    assert connection_or_refusal(LEGACY_ID) == (ASCII_KEY, ASCII_URL, True)
    # The mixed entry keeps its own, Agent-resolvable identity.
    assert connection_or_refusal(MIXED_ID) == (MIXED_KEY, MIXED_URL, True)


def test_ambiguous_legacy_alias_is_refused(monkeypatch):
    """Two entries folding to ``relay`` cannot lend each other's key or endpoint."""
    mixed = entry(MIXED_NAME, MIXED_URL, MIXED_KEY)
    omega = entry("Ω Relay", OMEGA_URL, OMEGA_KEY)
    data = cfg_with(mixed, omega)
    bind(monkeypatch, data)

    entries = config._custom_provider_entries(data)
    assert [e["name"] for e in entries] == [MIXED_NAME, "Ω Relay"]
    # Both really do fold to the same legacy id -- otherwise this case measures nothing.
    assert saved_ascii_fold(MIXED_NAME) == LEGACY_ID
    assert saved_ascii_fold("Ω Relay") == LEGACY_ID

    with pytest.raises(config.AmbiguousCustomProviderError):
        config._unique_custom_provider_entry(entries, "relay")

    key, url, _exact = connection_or_refusal(LEGACY_ID)
    assert key is None and url is None
    try:
        leaked = providers._get_provider_api_key(LEGACY_ID)
    except config.AmbiguousCustomProviderError:
        leaked = None
    assert leaked is None
    # Each entry is still individually reachable by its own canonical id.
    assert connection_or_refusal(MIXED_ID) == (MIXED_KEY, MIXED_URL, True)


def test_unknown_slug_still_owns_nothing(monkeypatch):
    """No legacy rung may resurrect the sole-row / first-row fallback."""
    bind(monkeypatch, cfg_with(entry(MIXED_NAME, MIXED_URL, MIXED_KEY)))
    assert connection_or_refusal("custom:ghost") == (None, None, False)
    assert providers._custom_provider_name_matches("custom:ghost", MIXED_NAME) is False


def test_ascii_entry_keeps_master_behaviour(monkeypatch):
    """Pure-ASCII names are unchanged: the legacy fold IS the canonical id."""
    data = cfg_with(entry("Local Gateway", ASCII_URL, ASCII_KEY), provider="custom:local-gateway")
    bind(monkeypatch, data)
    assert saved_ascii_fold("Local Gateway") == "custom:local-gateway"
    assert config._custom_provider_slug_from_name("Local Gateway") == "custom:local-gateway"
    assert connection_or_refusal("custom:local-gateway") == (ASCII_KEY, ASCII_URL, True)
