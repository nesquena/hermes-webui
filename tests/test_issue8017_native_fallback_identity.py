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


DISPLAY_LABEL_URL = NATIVE_URL


def _display_label_config(provider_url=DISPLAY_LABEL_URL) -> dict:
    """The round-17 reproduction: a bare key whose LABEL spells the identity.

    Master's native-view producer mints an identity from the mapping KEY alone, so
    a bare ``晨光`` key produced no identity at all however its ``name`` was
    spelled. The label is editable metadata, not provenance.
    """
    data = _oracle_config()
    data["model"]["api_key"] = "model-key"
    data["providers"] = {
        NATIVE_KEY: {
            "name": PID,
            "base_url": provider_url,
            "api_key": "native-key",
            "model": MODEL,
        },
    }
    return data


def test_a_display_label_on_a_bare_key_does_not_take_the_default(monkeypatch):
    """A label alone is not a route master had, so it must not own the default.

    Red on the round-16 head: ``_native_fallback_identities`` subtracted the
    record's qualified display name from the fallback set, so ``custom:晨光`` read
    as an identity the KEY minted before the change. It did not, so the bare
    ``chat-model`` was promoted to the row and the constructor bundle replaced
    ``model-key`` with the row's own credential.
    """
    data = _display_label_config()
    _bind(monkeypatch, data)
    _assert_default_keeps_its_own_connection(_display_label_config())


def test_a_display_label_on_a_shared_endpoint_does_not_take_the_default(monkeypatch):
    """The equal-endpoint shape, where the URL promotion cannot tell them apart."""
    data = _display_label_config(DEFAULT_URL)
    _bind(monkeypatch, data)
    _assert_default_keeps_its_own_connection(_display_label_config(DEFAULT_URL))


def test_an_explicit_pick_of_a_display_labelled_row_still_reaches_it(monkeypatch):
    """Control: the row stays in the picker and its qualified form still serves it."""
    _bind(monkeypatch, _display_label_config())
    assert [card["id"] for card in _cards()] == [PID]
    assert config.resolve_model_provider(
        f"@{PID}:{MODEL}", explicitly_picked=True
    ) == (MODEL, PID, NATIVE_URL)
    bundle = config.merge_custom_provider_runtime_bundle(
        PID, "model-key", NATIVE_URL, None
    )
    assert bundle["api_key"] == "native-key"


def test_a_configured_native_identity_still_routes_to_its_own_record(monkeypatch):
    """Control: naming the native record in ``model.provider`` is an explicit claim."""
    data = _oracle_config()
    data["model"]["provider"] = NATIVE_KEY
    _bind(monkeypatch, data)
    assert config.resolve_model_provider(MODEL) == (MODEL, PID, NATIVE_URL)


DISABLED_SIBLING = {"enabled": False, "base_url": "https://disabled.invalid/v1",
                    "api_key": "disabled-key"}
COMPLETE_SIBLING = {"base_url": NATIVE_URL, "api_key": "sibling-key", "model": MODEL}


def _sibling_config(*, explicit_url=NATIVE_URL, sibling=DISABLED_SIBLING,
                    sibling_first=False):
    """The round-15 reproduction: an INERT bare sibling beside an explicit owner.

    The sibling mints the same fallback identity but can never serve it (disabled
    or empty), so it must not be read as proof that the identity is fallback-new.
    ``sibling=None`` builds the no-sibling control.
    """
    explicit = {
        "base_url": explicit_url,
        "api_key": "explicit-key",
        "model": MODEL,
    }
    providers = {}
    pairs = [(PID, explicit)]
    if sibling is not None:
        pairs.append((NATIVE_KEY, sibling))
    if sibling_first:
        pairs.reverse()
    for key, record in pairs:
        providers[key] = record
    return {
        "model": {
            "provider": "custom",
            "default": MODEL,
            "base_url": DEFAULT_URL,
            "api_key": "model-key",
        },
        "providers": providers,
    }


def _assert_explicit_owner_keeps_the_model(data, explicit_url=NATIVE_URL) -> None:
    """The resolver triple AND the constructor bundle, exactly as pinned master.

    Master's own answer for this configuration, measured on 041a32514: the
    resolver names the explicit record at its endpoint, and the constructor
    bundle keeps that endpoint with the record's credential. The bundle's
    provider field reads ``custom`` on master too, so asserting it as the
    qualified id would pin a shape master never produced.
    """
    assert config.resolve_model_provider(MODEL) == (MODEL, PID, explicit_url)
    bundle = config.merge_custom_provider_runtime_bundle(
        PID, "model-key", explicit_url, None
    )
    assert (bundle["provider"], bundle["base_url"], bundle["api_key"]) == (
        "custom", explicit_url, "explicit-key",
    )


@pytest.mark.parametrize("explicit_url", [NATIVE_URL, DEFAULT_URL],
                         ids=["different-url", "equal-url"])
@pytest.mark.parametrize("sibling", [DISABLED_SIBLING, {}],
                         ids=["disabled-sibling", "empty-sibling"])
@pytest.mark.parametrize("sibling_first", [False, True],
                         ids=["owner-first", "sibling-first"])
def test_an_inert_bare_sibling_does_not_suppress_the_explicit_owner(
    monkeypatch, explicit_url, sibling, sibling_first
):
    """An inert sibling is not evidence that the identity is fallback-new.

    Red on the round-14 head: the sibling's bare key put ``custom:晨光`` in the
    fallback identity set, both exclusions skipped the explicit record, and the
    model fell to the generic ``model:`` route (``model-key`` at DEFAULT_URL)
    where master kept the record's endpoint and credential.
    """
    data = _sibling_config(
        explicit_url=explicit_url, sibling=sibling, sibling_first=sibling_first
    )
    _bind(monkeypatch, data)
    _assert_explicit_owner_keeps_the_model(data, explicit_url)


def test_the_explicit_owner_alone_still_owns_the_model(monkeypatch):
    """Control: with no sibling at all the explicit record answers, as before."""
    data = _sibling_config(sibling=None)
    _bind(monkeypatch, data)
    _assert_explicit_owner_keeps_the_model(data)


def test_an_empty_explicit_key_still_reaches_the_bare_records_alias(monkeypatch):
    """Control: an inert explicit key does not hide a complete bare record.

    Master answered this shape from the bare record through the keyed alias, so
    the identity is not fallback-only here either. The empty explicit key keeps
    master's verdict: the record that can serve is the bare one.
    """
    data = _sibling_config(sibling=dict(COMPLETE_SIBLING))
    data["providers"][PID] = {}
    _bind(monkeypatch, data)
    assert config.resolve_model_provider(MODEL) == (MODEL, PID, NATIVE_URL)
    bundle = config.merge_custom_provider_runtime_bundle(PID, "model-key", NATIVE_URL, None)
    assert (bundle["base_url"], bundle["api_key"]) == (NATIVE_URL, "sibling-key")


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


def test_a_removed_model_is_not_repaired_from_a_non_owner_entry(monkeypatch):
    """A same-slug entry that is not the owner cannot rewrite a saved chat.

    The ``model:`` block owns ``custom:晨光`` at DEFAULT_URL; the list entry only
    reaches that identity through the non-ASCII fallback, so its pinned list says
    nothing about this provider. Reading it anyway replaced a restored
    ``@custom:晨光:chat-model`` with the entry's ``other-model`` while the chat
    still used the block's endpoint and credential.
    """
    data = {
        "model": {"provider": PID, "base_url": DEFAULT_URL, "api_key": "model-key"},
        "custom_providers": [
            {"name": NATIVE_KEY, "base_url": NATIVE_URL, "api_key": "entry-key",
             "models": ["other-model"]},
        ],
    }
    _bind(monkeypatch, data)
    assert routes._repair_removed_qualified_custom_provider_model(
        "@" + PID + ":" + MODEL, PID, config_obj=data
    ) is None


def test_a_removed_model_is_repaired_from_its_sole_owner(monkeypatch):
    """Control: with no competing owner the entry's pinned list still repairs."""
    data = {
        "custom_providers": [
            {"name": NATIVE_KEY, "base_url": NATIVE_URL, "api_key": "entry-key",
             "models": ["other-model"]},
        ],
    }
    _bind(monkeypatch, data)
    assert routes._repair_removed_qualified_custom_provider_model(
        "@" + PID + ":" + MODEL, PID, config_obj=data
    ) == "@" + PID + ":other-model"


def test_a_disabled_native_identity_is_not_masked_by_a_non_owner_entry(monkeypatch):
    """A switched-off keyed route still forbids the persisted-pool probe.

    ``providers['custom:晨光']`` reserves its slug whether or not it is enabled,
    because switching that record off answers the route with a terminal miss.
    The list entry beside it is fallback-derived and not the owner, so it no
    longer stands in as a presentation row and the probe is refused.
    """
    data = {
        "providers": {PID: {"enabled": False}},
        "custom_providers": [
            {"name": NATIVE_KEY, "base_url": DEFAULT_URL, "api_key": "entry-key"},
        ],
    }
    _bind(monkeypatch, data)
    assert config._custom_provider_identity_disabled(PID, data) is True


def test_a_disabled_native_identity_is_not_answered_by_a_non_owner_entry(monkeypatch):
    """The key lookup agrees: a switched-off route reads no key from the entry.

    Same shape as the probe above, one layer down. Before the ownership context
    reached this caller, a disabled ``custom:晨光`` route took the fallback
    entry's ``entry-key`` and handed it to an endpoint the user had switched off.
    """
    data = {
        "providers": {PID: {"enabled": False}},
        "custom_providers": [
            {"name": NATIVE_KEY, "base_url": DEFAULT_URL, "api_key": "entry-key"},
        ],
    }
    _bind(monkeypatch, data)
    assert providers._get_provider_api_key(PID) is None


def test_an_enabled_keyed_route_still_reads_its_own_key(monkeypatch):
    """Control: the owner's own credential is untouched by the exclusion."""
    data = {
        "providers": {
            PID: {"base_url": NATIVE_URL, "api_key": "native-key"},
        },
        "custom_providers": [
            {"name": NATIVE_KEY, "base_url": DEFAULT_URL, "api_key": "entry-key"},
        ],
    }
    _bind(monkeypatch, data)
    assert config._custom_provider_identity_disabled(PID, data) is False
    assert providers._get_provider_api_key(PID) == "native-key"


def test_an_entry_that_owns_the_identity_keeps_the_pool_probe(monkeypatch):
    """Control: a list entry that IS the owner still stands for the identity."""
    data = {
        "custom_providers": [
            {"name": NATIVE_KEY, "base_url": DEFAULT_URL, "api_key": "entry-key"},
        ],
    }
    _bind(monkeypatch, data)
    assert config._custom_provider_identity_disabled(PID, data) is False

import yaml


def test_options_save_keeps_expanded_native_owner(monkeypatch, tmp_path):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(config, "_get_config_path", lambda: path)
    monkeypatch.setenv("DUMMY_LABEL", "custom:晨光")
    monkeypatch.setattr(config._thread_ctx, "env", {}, raising=False)
    monkeypatch.setattr(config._thread_ctx, "block_process_env_fallback", False, raising=False)
    data = {
        "model": {"provider": "openai", "default": "gpt-4o"},
        "providers": {
            "anchor": {
                "name": "${DUMMY_LABEL}",
                "base_url": "https://native.invalid/v1",
                "api_key": "DUMMY_NATIVE",
                "model": "vision-model",
            },
        },
        "custom_providers": [
            {"name": "晨光", "base_url": "https://legacy.invalid/v1", "api_key": "DUMMY_LEGACY", "model": "other-model"},
        ],
        "auxiliary": {
            "vision": {"provider": "custom:晨光", "model": "vision-model", "base_url": "https://native.invalid/v1"},
        },
    }
    path.write_text(yaml.safe_dump(data, allow_unicode=True))
    config.reload_config()
    assert config.resolve_custom_provider_connection("custom:晨光") == ("DUMMY_NATIVE", "https://native.invalid/v1")
    config.set_auxiliary_model("vision", "custom:晨光", "vision-model", advanced={"timeout": 60})
    saved = yaml.safe_load(path.read_text())
    assert saved["auxiliary"]["vision"]["base_url"] == "https://native.invalid/v1"
    assert saved["providers"]["anchor"]["name"] == "${DUMMY_LABEL}"


def test_generic_agent_alias_keeps_new_fallback_from_minting_its_id(monkeypatch):
    entry = {"name": "晨光", "base_url": "https://entry.invalid/v1", "api_key": "DUMMY_ENTRY", "model": "chat-a"}
    data = {
        "model": {"provider": "custom", "default": "old-model", "base_url": "https://generic.invalid/v1", "api_key": "DUMMY_GENERIC"},
        "providers": {
            "custom": {"name": "晨光", "base_url": "https://generic.invalid/v1", "api_key": "DUMMY_GENERIC"},
        },
        "custom_providers": [entry],
    }
    # The installed Agent's native lookup accepts custom:晨光 for providers.custom.
    # A new legacy identity must not select a different record for that same id.
    assert (
        config._custom_provider_entry_identity(entry, data["custom_providers"], data["providers"], data["model"]) == ""
    )
