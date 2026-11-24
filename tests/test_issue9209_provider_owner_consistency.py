"""Regression tests for #9209 (round 3): every owner surface agrees on ONE record.

The #9209 repair mints the Agent's identity for a mixed-script custom provider
name (``晨光 Relay`` -> ``custom:晨光-relay``) and keeps the id the WebUI used to
save (``custom:relay``) as a fallback alias. Minting is not the problem these
tests pin: the canonical/legacy split must never make two surfaces disagree about
which record owns a request, and a fallback alias must never outrank or revive an
owner the user already configured or switched off.

Each test drives a PRODUCTION entry point that exists both on master and on this
branch (``resolve_model_provider``, ``resolve_custom_provider_connection``,
``merge_custom_provider_runtime_bundle``, ``_get_provider_api_key``, the
credential-env derivation), never the private helpers this branch added, so the
same file is a matched control: it passes on frozen master ``d49955dc`` and fails
on the integration head that introduced the alias.

Covered, one test per gate finding:

* ``test_saved_selection_keeps_its_own_provider`` -- a previously saved provider
  selection still routes to the provider it names, not to a different configured
  one that happens to list the same model earlier in config order;
* ``test_disabled_owner_is_terminal_before_fallbacks`` -- an identity the user
  switched off stays off: the ``model:`` block the WebUI writes is a fallback,
  not a resurrecting authority;
* ``test_endpoint_and_connection_name_the_same_record`` -- the endpoint the route
  resolves and the connection the send uses come from one record (or from none);
* ``test_credential_namespace_does_not_collide_across_identities`` -- two
  distinct non-ASCII identities never share one credential-env namespace, while
  ASCII names keep the exact env name they always had;
* ``test_same_provider_in_both_stores_is_one_owner`` -- one provider described by
  both stores is one owner, not an ambiguity (neighbour guard: this file's
  controls must keep passing once the alias is repaired).

No Agent install, live endpoint or real profile is required.
"""
from __future__ import annotations

import sys
import types

import pytest

from api import config, providers


MIXED_NAME = "晨光 Relay"
MIXED_ID = "custom:晨光-relay"
OTHER_NAME = "光芒 Relay"
OTHER_ID = "custom:光芒-relay"
LEGACY_ID = "custom:relay"
OMEGA_NAME = "Omega Relay"
PLAIN_NAME = "Relay"

MIXED_URL = "https://relay.invalid/v1"
MIXED_KEY = "mixed-secret"
OTHER_URL = "https://other.invalid/v1"
OMEGA_URL = "https://omega.invalid/v1"
OMEGA_KEY = "omega-secret"
PLAIN_URL = "https://plain.invalid/v1"
PLAIN_KEY = "plain-secret"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Stub only the external catalog boundary, never a production resolver."""
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


def entry(name, url, key, model="m1"):
    return {"name": name, "base_url": url, "api_key": key, "model": model}


def connection_or_refusal(provider_id):
    """``resolve_custom_provider_connection``'s answer, with a refusal read as empty."""
    try:
        return config.resolve_custom_provider_connection(
            provider_id, return_provenance=True
        )
    except config.AmbiguousCustomProviderError:
        return (None, None, False)


def bundle_for(provider_id, *, base_url=None, api_key=None):
    """The bundle every AIAgent-constructing consumer applies for one route."""
    runtime = {"provider": provider_id, "base_url": base_url, "api_key": api_key}
    return config.merge_custom_provider_runtime_bundle(
        provider_id, api_key, base_url, runtime
    )


# ---------------------------------------------------------------------------
# 1. A saved selection keeps pointing at the provider it was saved for.
# ---------------------------------------------------------------------------


def test_saved_selection_keeps_its_own_provider(monkeypatch):
    """``model.provider`` saved as the legacy id still routes to ITS entry.

    ``Omega Relay`` is listed first and lists the same model, so a resolution that
    forgets the saved selection's own owner hands the turn to Omega: a different
    endpoint and a different credential, chosen by config write order.
    """
    data = {
        "model": {"provider": LEGACY_ID, "default": "m1"},
        "custom_providers": [
            entry(OMEGA_NAME, OMEGA_URL, OMEGA_KEY),
            entry(MIXED_NAME, MIXED_URL, MIXED_KEY),
        ],
    }
    bind(monkeypatch, data)

    _model, provider, base_url = config.resolve_model_provider("m1")

    # The route is the saved provider's own endpoint, whether the resolver keeps
    # the saved id or hands back the Agent's canonical one -- both name this row.
    assert base_url == MIXED_URL, f"routed to {provider!r} at {base_url!r}"
    key, url, _exact = connection_or_refusal(provider)
    assert (key, url) == (MIXED_KEY, MIXED_URL)

    # ...and the bundle a send applies agrees with it, end to end.
    bundle = bundle_for(provider)
    assert config.custom_provider_route_error(bundle) is None
    assert (bundle["api_key"], bundle["base_url"]) == (MIXED_KEY, MIXED_URL)


# ---------------------------------------------------------------------------
# 2. A disabled owner stays terminal before any fallback is admitted.
# ---------------------------------------------------------------------------


def test_disabled_owner_is_terminal_before_fallbacks(monkeypatch):
    """A switched-off identity cannot be brought back by the ``model:`` block.

    ``providers.custom:晨光-relay`` is the record that owns this identity, and the
    user set ``enabled: false`` on it. The ``model:`` block still carries the
    endpoint and key the WebUI wrote while that provider was active; treating it
    as an authority here re-enables a provider the user took offline.
    """
    data = {
        "model": {
            "provider": LEGACY_ID,
            "default": "m1",
            "base_url": MIXED_URL,
            "api_key": MIXED_KEY,
        },
        "providers": {
            MIXED_ID: {
                "name": MIXED_NAME,
                "base_url": MIXED_URL,
                "api_key": MIXED_KEY,
                "enabled": False,
            }
        },
    }
    bind(monkeypatch, data)

    assert connection_or_refusal(LEGACY_ID) == (None, None, False)

    bundle = bundle_for(LEGACY_ID, base_url=MIXED_URL, api_key=MIXED_KEY)
    verdict = config.custom_provider_route_error(bundle)
    assert verdict is not None, "a disabled provider was accepted as routable"
    assert verdict["reason"] == config.CUSTOM_ROUTE_UNOWNED
    assert bundle["base_url"] is None and bundle["api_key"] is None

    assert providers._get_provider_api_key(LEGACY_ID) is None


# ---------------------------------------------------------------------------
# 3. Endpoint lookup and connection selection name the same record.
# ---------------------------------------------------------------------------


def test_endpoint_and_connection_name_the_same_record(monkeypatch):
    """The resolved endpoint and the connection cannot come from two records.

    The DSL here is the one that splits ownership: a ``custom_providers[]`` row
    for ``晨光 Relay`` owns the model route, while ``providers.relay`` -- the
    canonical owner of the saved id ``custom:relay`` -- is switched off. The
    endpoint lookup must report the same owner the connection resolves, so a
    caller that persists or displays that endpoint cannot advertise a route the
    connection refuses.
    """
    data = {
        "model": {"provider": LEGACY_ID, "default": "m1"},
        "custom_providers": [entry(MIXED_NAME, MIXED_URL, MIXED_KEY)],
        "providers": {
            "relay": {
                "base_url": PLAIN_URL,
                "api_key": PLAIN_KEY,
                "enabled": False,
            }
        },
    }
    bind(monkeypatch, data)

    _model, _provider, route_base_url = config.resolve_model_provider("m1")
    _key, connection_base_url, _exact = connection_or_refusal(LEGACY_ID)

    assert (route_base_url or None) == (connection_base_url or None), (
        "endpoint lookup and connection selection disagree: "
        f"{route_base_url!r} vs {connection_base_url!r}"
    )


# ---------------------------------------------------------------------------
# 4. Credential namespace derivation is not lossy across identities.
# ---------------------------------------------------------------------------


def test_credential_namespace_does_not_collide_across_identities(monkeypatch):
    """Two distinct identities never read ONE credential-env namespace.

    The env name for a custom id is derived by folding every non-alphanumeric
    character to ``_`` and upper-casing. Non-ASCII providers therefore collapse to
    runs of underscores: ``晨光 Relay`` and ``光芒 Relay`` used to share
    ``CUSTOM____RELAY_API_KEY``, i.e. one secret for two providers. The name is
    taken from the production derivation, so this control measures whatever that
    function currently mints.
    """
    env_name = config._api_key_env_name(MIXED_ID)
    monkeypatch.setattr(
        config._thread_ctx, "env", {env_name: "shared-secret"}, raising=False
    )
    data = {
        "model": {"provider": MIXED_ID, "default": "m1"},
        "custom_providers": [
            {"name": MIXED_NAME, "base_url": MIXED_URL, "model": "m1"},
            {"name": OTHER_NAME, "base_url": OTHER_URL, "model": "m1"},
        ],
    }
    bind(monkeypatch, data)

    mixed_key, _mixed_url, _exact = connection_or_refusal(MIXED_ID)
    other_key, _other_url, _exact = connection_or_refusal(OTHER_ID)

    assert other_key != "shared-secret", (
        f"{OTHER_NAME!r} took {MIXED_NAME!r}'s credential from {env_name}"
    )
    assert not (mixed_key and mixed_key == other_key), (
        "two distinct provider identities shared one credential: "
        f"{MIXED_ID} and {OTHER_ID} both read {env_name}"
    )


# ---------------------------------------------------------------------------
# Neighbour guard: the same provider described by BOTH stores is ONE owner.
# ---------------------------------------------------------------------------


def test_same_provider_in_both_stores_is_one_owner(monkeypatch):
    """A provider saved twice is not "two providers competing" for its old id.

    The WebUI writes ``custom_providers[]``; the Agent's own config shape lives
    under ``providers:``. When a profile carries both descriptions of ONE provider
    they fold to the same saved id, and refusing it as ambiguous breaks a route
    that master resolves.
    """
    data = {
        "model": {"provider": LEGACY_ID, "default": "m1"},
        "custom_providers": [entry(MIXED_NAME, MIXED_URL, MIXED_KEY)],
        "providers": {
            MIXED_ID: {
                "name": MIXED_NAME,
                "base_url": MIXED_URL,
                "api_key": MIXED_KEY,
            }
        },
    }
    bind(monkeypatch, data)

    assert connection_or_refusal(LEGACY_ID) == (MIXED_KEY, MIXED_URL, True)


def test_ascii_credential_namespace_is_unchanged(monkeypatch):
    """Existing ASCII names keep exactly the env name they always derived."""
    assert config._api_key_env_name("custom:gpu.local-8000") == "CUSTOM_GPU_LOCAL_8000_API_KEY"
    assert config._api_key_env_name("custom:10.8.71.41:8080") == "CUSTOM_10_8_71_41_8080_API_KEY"
    assert config._api_key_env_name("custom:relay") == "CUSTOM_RELAY_API_KEY"

    monkeypatch.setattr(
        config._thread_ctx, "env", {"CUSTOM_RELAY_API_KEY": "ascii-secret"}, raising=False
    )
    data = {
        "model": {"provider": LEGACY_ID, "default": "m1"},
        "custom_providers": [{"name": PLAIN_NAME, "base_url": PLAIN_URL, "model": "m1"}],
    }
    bind(monkeypatch, data)

    key, url, _exact = connection_or_refusal(LEGACY_ID)
    assert (key, url) == ("ascii-secret", PLAIN_URL)
