"""Regression tests for #9209 (round 3): canonical ownership before legacy aliases.

The #9209 repair mints the Agent's identity for a mixed-script name and round 2
kept a LEGACY fold as a fallback so ids already written to ``model.provider``
keep resolving. Round 2 applied that fallback inside each store, so a legacy
alias in ``custom_providers`` short-circuited the ladder before the canonical
``providers:`` record was even inspected:

* ``custom:relay`` returned the mixed-name ``晨光 Relay`` entry's endpoint, key,
  ``api_mode`` and transport while a canonical ``relay`` record owned that id;
* an explicitly DISABLED canonical record was bypassed the same way, so a
  switched-off provider kept serving a live route;
* an alias claimed by one store and another alias claimed by the other store was
  resolved to whichever came first instead of being refused.

The contract pinned here: every CANONICAL owner (including its disabled and
ambiguous verdicts) is resolved across the whole ladder before any legacy alias
is considered; a legacy alias is then adopted only when it is the single safe
claim, and a claim split across stores is refused rather than paired.

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
MIXED_URL = "https://mixed.invalid/v1"
MIXED_KEY = "mixed-secret"
PLAIN_URL = "https://plain.invalid/v1"
PLAIN_KEY = "plain-secret"
OMEGA_URL = "https://omega.invalid/v1"
OMEGA_KEY = "omega-secret"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Stub only the external catalog boundary, never the slug producers."""
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
    """The id the WebUI SAVED before #9209 -- the legacy alias this repair keeps."""
    raw = str(name or "").strip().lower()
    if not raw:
        return ""
    if raw.startswith("custom:"):
        return raw
    slug = re.sub(r"[^a-z0-9._-]+", "-", raw).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return ("custom:" + slug) if slug else ""


def connection_or_refusal(provider_id):
    """``resolve_custom_provider_connection``'s answer, with a refusal read as empty."""
    try:
        return config.resolve_custom_provider_connection(provider_id, return_provenance=True)
    except config.AmbiguousCustomProviderError:
        return (None, None, False)


def api_key_or_refusal(provider_id):
    try:
        return providers._get_provider_api_key(provider_id)
    except config.AmbiguousCustomProviderError:
        return None


def cfg_with(custom_entries, providers_cfg, provider=LEGACY_ID):
    return {
        "model": {"provider": provider, "default": "m1"},
        "custom_providers": list(custom_entries),
        "providers": dict(providers_cfg),
    }


def test_canonical_raw_record_outranks_the_legacy_alias(monkeypatch):
    """``providers: {relay: ...}`` owns ``custom:relay``; the mixed entry cannot take it."""
    mixed = entry(MIXED_NAME, MIXED_URL, MIXED_KEY)
    data = cfg_with([mixed], {"relay": {"api": PLAIN_URL, "api_key": PLAIN_KEY, "name": "relay"}})
    bind(monkeypatch, data)

    assert saved_ascii_fold(MIXED_NAME) == LEGACY_ID  # the alias really is in play
    assert config._custom_provider_slug_from_name(MIXED_NAME) == MIXED_ID

    record, source, _exact, _status = config._select_custom_provider_record(
        LEGACY_ID, "relay", data
    )
    assert record is not None
    assert source == "providers"
    assert config._custom_record_base_url(record) == PLAIN_URL
    assert connection_or_refusal(LEGACY_ID) == (PLAIN_KEY, PLAIN_URL, False)
    assert api_key_or_refusal(LEGACY_ID) == PLAIN_KEY  # never the mixed entry's key
    # The mixed entry keeps its own, Agent-resolvable identity.
    assert connection_or_refusal(MIXED_ID) == (MIXED_KEY, MIXED_URL, True)


def test_keyed_canonical_record_outranks_the_legacy_alias(monkeypatch):
    """The ``providers: {custom:relay: ...}`` spelling of the same authority."""
    mixed = entry(MIXED_NAME, MIXED_URL, MIXED_KEY)
    data = cfg_with([mixed], {"custom:relay": {"api": PLAIN_URL, "api_key": PLAIN_KEY}})
    bind(monkeypatch, data)

    assert connection_or_refusal(LEGACY_ID) == (PLAIN_KEY, PLAIN_URL, False)
    assert api_key_or_refusal(LEGACY_ID) == PLAIN_KEY
    assert connection_or_refusal(MIXED_ID) == (MIXED_KEY, MIXED_URL, True)


@pytest.mark.parametrize(
    "providers_cfg",
    [
        {"relay": {"api": PLAIN_URL, "api_key": PLAIN_KEY, "name": "relay", "enabled": False}},
        {"custom:relay": {"api": PLAIN_URL, "api_key": PLAIN_KEY, "enabled": False}},
    ],
    ids=["raw-key", "custom-keyed"],
)
def test_disabled_canonical_record_is_terminal_over_the_legacy_alias(monkeypatch, providers_cfg):
    """A switched-off canonical owner is not resurrected by another store's alias."""
    mixed = entry(MIXED_NAME, MIXED_URL, MIXED_KEY)
    data = cfg_with([mixed], providers_cfg)
    bind(monkeypatch, data)

    assert config._custom_provider_identity_disabled(LEGACY_ID, data) is True
    assert connection_or_refusal(LEGACY_ID) == (None, None, False)
    assert api_key_or_refusal(LEGACY_ID) is None
    assert providers._custom_provider_name_matches(LEGACY_ID, MIXED_NAME) is True
    # The mixed entry itself is still reachable under its canonical id.
    assert connection_or_refusal(MIXED_ID) == (MIXED_KEY, MIXED_URL, True)


def test_legacy_alias_split_across_stores_is_refused(monkeypatch):
    """One alias in ``custom_providers`` plus one in ``providers`` is ambiguous."""
    mixed = entry(MIXED_NAME, MIXED_URL, MIXED_KEY)
    omega = {"api": OMEGA_URL, "api_key": OMEGA_KEY, "name": "Ω Relay"}
    data = cfg_with([mixed], {"omega": omega})
    bind(monkeypatch, data)

    # Both really do fold to the legacy id -- otherwise this case measures nothing.
    assert saved_ascii_fold(MIXED_NAME) == LEGACY_ID
    assert saved_ascii_fold("Ω Relay") == LEGACY_ID
    # ...and neither names it canonically.
    assert config._custom_provider_slug_from_name(MIXED_NAME) == MIXED_ID
    assert config._custom_provider_slug_from_name("Ω Relay") == "custom:ω-relay"

    with pytest.raises(config.AmbiguousCustomProviderError):
        config._select_custom_provider_record(LEGACY_ID, "relay", data)
    assert connection_or_refusal(LEGACY_ID) == (None, None, False)
    assert api_key_or_refusal(LEGACY_ID) is None


def test_canonical_custom_providers_entry_outranks_a_legacy_native_alias(monkeypatch):
    """Reversed stores: canonical row in ``custom_providers``, alias only in ``providers``."""
    plain = entry("Relay", PLAIN_URL, PLAIN_KEY)
    omega = {"api": OMEGA_URL, "api_key": OMEGA_KEY, "name": "Ω Relay"}
    data = cfg_with([plain], {"omega": omega})
    bind(monkeypatch, data)

    record, source, exact, _status = config._select_custom_provider_record(
        LEGACY_ID, "relay", data
    )
    assert record is plain
    assert source == "custom_providers"
    assert exact is True
    assert connection_or_refusal(LEGACY_ID) == (PLAIN_KEY, PLAIN_URL, True)
    assert api_key_or_refusal(LEGACY_ID) == PLAIN_KEY


def test_ambiguous_canonical_owners_are_still_refused(monkeypatch):
    """Canonical ambiguity keeps its own verdict -- aliases do not soften it."""
    first = entry("Relay", PLAIN_URL, PLAIN_KEY)
    second = entry("relay", MIXED_URL, MIXED_KEY)
    data = cfg_with([first, second], {})
    bind(monkeypatch, data)

    with pytest.raises(config.AmbiguousCustomProviderError):
        config._unique_custom_provider_entry(config._custom_provider_entries(data), "relay")
    assert connection_or_refusal(LEGACY_ID) == (None, None, False)


def test_unique_legacy_alias_is_adopted_as_the_complete_record(monkeypatch):
    """A single safe legacy claim still resolves -- as its OWN record, not a field mix."""
    mixed = entry(MIXED_NAME, MIXED_URL, MIXED_KEY)
    mixed["api_mode"] = "anthropic_messages"
    data = cfg_with([mixed], {})
    bind(monkeypatch, data)

    record, source, _exact, _status = config._select_custom_provider_record(
        LEGACY_ID, "relay", data
    )
    assert record is mixed
    assert isinstance(record, dict)
    assert source == "custom_providers"
    assert config._custom_record_api_mode(record) == "anthropic_messages"
    assert connection_or_refusal(LEGACY_ID) == (MIXED_KEY, MIXED_URL, True)
    assert api_key_or_refusal(LEGACY_ID) == MIXED_KEY


def test_canonical_record_without_any_alias_is_unchanged(monkeypatch):
    """Control: the ordinary shape keeps resolving exactly as master did."""
    data = cfg_with([], {"relay": {"api": PLAIN_URL, "api_key": PLAIN_KEY, "name": "relay"}})
    bind(monkeypatch, data)

    assert connection_or_refusal(LEGACY_ID) == (PLAIN_KEY, PLAIN_URL, False)
    assert api_key_or_refusal(LEGACY_ID) == PLAIN_KEY
    assert connection_or_refusal("custom:ghost") == (None, None, False)


def test_generic_active_provider_record_does_not_make_the_alias_ambiguous(monkeypatch):
    """Control: the active-provider shape the WebUI writes names the SAME provider.

    ``providers['custom']`` is a catch-all, not a second owner: it must not be
    counted as a competing legacy claim, or the WebUI's own config would start
    refusing to resolve itself.
    """
    mixed = entry(MIXED_NAME, MIXED_URL, MIXED_KEY)
    data = cfg_with(
        [mixed], {"custom": {"name": MIXED_NAME, "api": MIXED_URL, "api_key": MIXED_KEY}}
    )
    bind(monkeypatch, data)

    assert connection_or_refusal(LEGACY_ID) == (MIXED_KEY, MIXED_URL, True)
    assert connection_or_refusal(MIXED_ID) == (MIXED_KEY, MIXED_URL, True)
    assert api_key_or_refusal(LEGACY_ID) == MIXED_KEY


def test_generic_active_provider_record_still_serves_the_legacy_alias(monkeypatch):
    """Round 2 retained: a saved legacy id backed ONLY by the catch-all keeps resolving.

    Here ``model.provider`` already points somewhere else, so the generic record
    is the only place the mixed entry survives. The catch-all is consulted last
    and never counted as a competing claim, but it must not disappear.
    """
    data = {
        "model": {"provider": "openai", "default": "gpt"},
        "custom_providers": [],
        "providers": {"custom": {"name": MIXED_NAME, "base_url": MIXED_URL, "api_key": MIXED_KEY}},
    }
    bind(monkeypatch, data)

    record, source, exact, _status = config._select_custom_provider_record(
        LEGACY_ID, "relay", data
    )
    assert record is not None
    assert source == "providers"
    assert exact is False
    assert connection_or_refusal(LEGACY_ID) == (MIXED_KEY, MIXED_URL, False)


def test_model_block_provider_key_alias_is_retained(monkeypatch):
    """The ``model:`` block spelling of the same legacy-only authority."""
    data = {
        "model": {
            "provider": "custom",
            "provider_key": MIXED_NAME,
            "base_url": MIXED_URL,
            "api_key": MIXED_KEY,
        },
        "custom_providers": [],
        "providers": {},
    }
    bind(monkeypatch, data)

    record, source, _exact, _status = config._select_custom_provider_record(
        LEGACY_ID, "relay", data
    )
    assert record is not None
    assert source == "model"
    assert connection_or_refusal(LEGACY_ID) == (MIXED_KEY, MIXED_URL, False)


