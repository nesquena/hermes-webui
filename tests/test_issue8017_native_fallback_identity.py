"""#8017: a native row whose identity comes from the non-ASCII fallback.

``providers:`` keys master could not slug (a pure-CJK key such as ``晨光``) were
never read as custom-provider rows. The fallback identity that keeps such a name
visible (#8017) is NEW: no configuration could select it before, so the row must
be visible in every read surface WITHOUT answering for a model id that is
resolved by name. Master's answer for that config is the configured provider's
endpoint, and it stays the answer here.

The controls: the explicitly qualified pick (``@custom:<name>:<model>``) still
routes to the record's own endpoint, and the row is present in the cards, both
catalogs and live discovery under its own identity.
"""

from __future__ import annotations

import socket
import sys
import types
from urllib.parse import urlparse

import pytest

from api import config, providers, routes

NATIVE_KEY = "晨光"
PID = "custom:晨光"
MODEL = "chat-model"
DEFAULT_URL = "https://model.invalid/v1"
NATIVE_URL = "https://native.invalid/v1"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Stub the external catalog/auth boundary. Everything else is the real code."""
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
    monkeypatch.setattr(config._thread_ctx, "block_process_env_fallback", True, raising=False)
    monkeypatch.setattr(routes, "j", lambda _handler, body, *a, **k: body)
    monkeypatch.setattr(routes, "_get_cached_live_models", lambda _key: None)
    monkeypatch.setattr(routes, "_set_cached_live_models", lambda *_args: None)

    def offline(*_args, **_kwargs):
        raise OSError("synthetic offline host")

    monkeypatch.setattr(socket, "getaddrinfo", offline)
    monkeypatch.setattr("urllib.request.urlopen", offline)
    config.invalidate_models_cache()
    yield
    config.invalidate_models_cache()


def _oracle_config() -> dict:
    """The maintainer's reproduction config, verbatim."""
    return {
        "model": {
            "provider": "custom",
            "default": MODEL,
            "base_url": DEFAULT_URL,
        },
        "providers": {
            NATIVE_KEY: {
                "base_url": NATIVE_URL,
                "api_key": "synthetic-native",
                "model": MODEL,
            },
        },
    }


def _bind(monkeypatch, data):
    monkeypatch.setattr(config, "cfg", data)
    monkeypatch.setattr(config, "get_config", lambda: data)
    monkeypatch.setattr(providers, "get_config", lambda: data)
    providers.invalidate_providers_cache()


def _cards(pid=PID):
    return [p for p in providers.get_providers()["providers"] if p["id"] == pid]


def _live(pid=PID):
    return routes._handle_live_models(
        None, urlparse("/api/models/live?provider=" + pid)
    )


def _groups(catalog, pid=PID):
    return [g for g in catalog["groups"] if g["provider_id"] == pid]


def test_a_normalized_native_row_owns_the_identity_its_key_names(monkeypatch):
    """The row mints its own identity instead of being shadowed by itself.

    Red on the round-12 head: the row was named with the bare key, the fallback
    rule read that as a claim on a slug its own ``providers:`` record already
    owned, and the row minted ``""`` while the router still answered from its
    endpoint.
    """
    data = _oracle_config()
    _bind(monkeypatch, data)
    entries = config._custom_provider_entries(data)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["name"] == PID
    assert entry["display_name"] == NATIVE_KEY
    assert config._custom_provider_entry_identity(
        entry, entries, data.get("providers"), data.get("model")
    ) == PID


def test_the_configured_default_keeps_its_route(monkeypatch):
    """A bare model id resolves to the configured default, exactly as on master.

    The whole triple is asserted: a fix that keeps the endpoint but invents a
    provider (or the reverse) would satisfy a URL-only check.
    """
    _bind(monkeypatch, _oracle_config())
    _assert_default_keeps_its_own_connection(_oracle_config())


def test_the_row_stays_visible_on_every_read_surface(monkeypatch):
    """Cards, both catalogs and live discovery name the row by its own identity."""
    _bind(monkeypatch, _oracle_config())

    cards = _cards()
    assert len(cards) == 1, f"got cards {[p['id'] for p in providers.get_providers()['providers']]}"
    assert cards[0]["display_name"] == NATIVE_KEY
    assert [m["id"] for m in cards[0]["models"]] == [MODEL]

    live = _live()
    assert live["provider"] == PID
    assert [m["id"] for m in live["models"]] == [MODEL]

    for catalog in (config._static_models_catalog_without_live_probes(),
                    config.get_available_models(force_refresh=True)):
        found = _groups(catalog)
        assert len(found) == 1, f"got groups {[g['provider_id'] for g in catalog['groups']]}"
        # The catalog hands the picker the QUALIFIED id, which is why the bare
        # model id can stay with the configured default: a pick from this group
        # resolves through the @provider arm, not through the by-name scan.
        assert [m["id"] for m in found[0]["models"]] == [f"@{PID}:{MODEL}"]


def test_an_explicitly_qualified_pick_still_routes_to_the_record(monkeypatch):
    """Control: the picker's ``@provider:model`` form is unaffected."""
    _bind(monkeypatch, _oracle_config())
    model, provider, base_url = config.resolve_model_provider(
        f"@{PID}:{MODEL}", explicitly_picked=True
    )
    assert (model, provider, base_url) == (MODEL, PID, NATIVE_URL)


def test_a_legacy_empty_hint_entry_keeps_its_own_endpoint(monkeypatch):
    """Control: the historic legacy empty-hint route is untouched (#8026).

    A legacy entry whose name mints nothing has no native record to tie itself
    to, so it still answers for a model it declares with its own endpoint.
    """
    data = {
        "model": {"provider": "custom", "default": MODEL, "base_url": DEFAULT_URL},
        "custom_providers": [
            {"name": "-", "base_url": "http://dash.example/v2",
             "models": {MODEL: {}}},
        ],
    }
    _bind(monkeypatch, data)
    assert config.resolve_model_provider(MODEL) == (MODEL, "", "http://dash.example/v2")


def _same_url_config() -> dict:
    """The round-14 reproduction: the native row shares the default's endpoint."""
    data = _oracle_config()
    data["model"]["api_key"] = "model-key"
    data["providers"][NATIVE_KEY]["base_url"] = DEFAULT_URL
    return data


def _assert_default_keeps_its_own_connection(data) -> None:
    """The resolver AND the constructor bundle stay on the configured default.

    Both halves matter: a route can keep the provider and still hand the send a
    different record's key, which is the account substitution the review named.
    """
    _model, provider, base_url = config.resolve_model_provider(MODEL)
    assert (provider, base_url) == ("custom", DEFAULT_URL)
    bundle = config.merge_custom_provider_runtime_bundle(
        provider, "model-key", base_url, None
    )
    assert (bundle["provider"], bundle["base_url"], bundle["api_key"]) == (
        "custom", DEFAULT_URL, "model-key",
    )


def test_a_same_url_native_row_does_not_take_the_configured_default(monkeypatch):
    """An equal endpoint is not evidence that two records are one account.

    Red on the previous head: the bare ``custom`` route was promoted by
    ``_named_custom_provider_slug_for_base_url`` to ``custom:晨光`` and the
    runtime bundle replaced ``model-key`` with the row's own key.
    """
    _bind(monkeypatch, _same_url_config())
    _assert_default_keeps_its_own_connection(_same_url_config())


def test_an_explicit_pick_of_the_same_url_row_still_carries_its_own_key(monkeypatch):
    """Control: the picker's qualified form reaches the row even at a shared URL."""
    _bind(monkeypatch, _same_url_config())
    assert config.resolve_model_provider(
        f"@{PID}:{MODEL}", explicitly_picked=True
    ) == (MODEL, PID, DEFAULT_URL)
    bundle = config.merge_custom_provider_runtime_bundle(
        PID, "model-key", DEFAULT_URL, None
    )
    assert bundle["api_key"] == "synthetic-native"


def test_a_configured_native_identity_still_routes_to_its_own_record(monkeypatch):
    """Control: naming the native record in ``model.provider`` is an explicit claim."""
    data = _oracle_config()
    data["model"]["provider"] = NATIVE_KEY
    _bind(monkeypatch, data)
    assert config.resolve_model_provider(MODEL) == (MODEL, PID, NATIVE_URL)


def test_a_url_match_on_an_ascii_entry_still_promotes(monkeypatch):
    """Control: only fallback-derived native identities are excluded.

    The historical ASCII ``custom_providers[]`` inference by ``model.base_url``
    is what a gateway config relies on, and it is unchanged.
    """
    data = {
        "model": {"provider": "custom", "default": MODEL, "base_url": DEFAULT_URL},
        "custom_providers": [
            {"name": "Gateway", "base_url": DEFAULT_URL, "api_key": "entry-key",
             "model": MODEL},
        ],
    }
    _bind(monkeypatch, data)
    assert config.resolve_model_provider(MODEL) == (MODEL, "custom:gateway", DEFAULT_URL)

