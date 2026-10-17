"""Tests for issue #8017: a non-ASCII-only custom_providers name must not vanish.

``_custom_provider_slug_from_name()`` slugifies with an ASCII-only character
class, so a name made only of non-ASCII characters (e.g. a pure-CJK name)
slugifies to ``""``. The catalog builder treated that as "no provider" and
skipped the whole entry: its models never reached the WebUI model picker
while the CLI kept working — no error, no diagnostic.

The fix derives a deterministic ASCII identity for such records (explicit
``provider_key`` → endpoint-derived slug → short stable hash) so the entry
always has a stable identity that can round-trip as ``custom:<slug>``.
"""

from __future__ import annotations

import api.config as config


# ── the slugify primitive is unchanged ─────────────────────────────────────


def test_slug_from_name_still_empty_for_non_ascii_only_name():
    """The primitive keeps its documented behaviour; the fallback is separate."""
    assert config._custom_provider_slug_from_name("晨光鑫遇专用") == ""
    assert config._custom_provider_slug_from_name("") == ""


def test_slug_from_name_ascii_unchanged():
    assert config._custom_provider_slug_from_name("cgxy-cpa") == "custom:cgxy-cpa"
    assert config._custom_provider_slug_from_name("Local (127.0.0.1:15721)") == "custom:local-127.0.0.1-15721"


# ── the fallback ───────────────────────────────────────────────────────────


def test_fallback_prefers_explicit_provider_key():
    slug = config._named_custom_slug_fallback(
        {"provider_key": "cgxy-cpa", "base_url": "http://127.0.0.1:8317/v1"},
        "晨光鑫遇专用",
    )
    assert slug == "custom:cgxy-cpa"


def test_fallback_hashes_a_non_ascii_provider_key_deterministically():
    record = {"provider_key": "晨光鑫遇"}
    first = config._named_custom_slug_fallback(record, "晨光鑫遇专用")
    second = config._named_custom_slug_fallback(dict(record), "晨光鑫遇专用")
    assert first == second
    assert first.startswith("custom:")
    # Stable across processes: a content hash, not id()/repr()/random.
    assert first == config._named_custom_slug_fallback(record, "晨光鑫遇专用")


def test_fallback_uses_endpoint_slug_when_no_provider_key():
    slug = config._named_custom_slug_fallback(
        {"base_url": "http://127.0.0.1:8317/v1"},
        "晨光鑫遇专用",
    )
    # Prefer the hyphenated endpoint form: it stays a single safe identifier.
    assert slug == "custom:127.0.0.1-8317"


def test_fallback_hashes_the_name_when_nothing_else_is_available():
    slug = config._named_custom_slug_fallback({}, "晨光鑫遇专用")
    assert slug.startswith("custom:")
    assert len(slug) == len("custom:") + 8
    # Same name -> same slug; different name -> different slug.
    assert slug == config._named_custom_slug_fallback({}, "晨光鑫遇专用")
    assert slug != config._named_custom_slug_fallback({}, "别的名字")


def test_fallback_ignores_a_blank_provider_key():
    slug = config._named_custom_slug_fallback(
        {"provider_key": "   ", "base_url": "http://10.0.0.5:9000/v1"},
        "晨光鑫遇专用",
    )
    assert slug == "custom:10.0.0.5-9000"


# ── catalog builder wiring ─────────────────────────────────────────────────


def test_catalog_builder_falls_back_for_non_ascii_names():
    """The builder must call the fallback rather than dropping the entry."""
    src = open(config.__file__, encoding="utf-8").read()
    assert "_named_custom_slug_fallback(_cp, _cp_name)" in src
    # The fallback is only for entries whose name produced no slug at all.
    assert "if not _slug and _cp_name:" in src


# ── advertised selection → resolver → runtime bundle round-trip ──────────────
#
# The catalog fix mints a slug for a non-ASCII-only name (e.g. ``custom:cgxy-cpa``
# from the explicit ``provider_key``) and advertises options as
# ``@custom:cgxy-cpa:<model>``. The resolver and the runtime bundle must
# recognise that SAME identity back to the record, or the picker selection
# resolves to ``(model, custom:cgxy-cpa, None)`` and the bundle reports
# ``status: missing`` with no record/endpoint/key (#8017 review round 2).

import copy

import pytest

_MAINTAINER_CFG = {
    "model": {"provider": "custom", "default": "shared:model"},
    "custom_providers": [
        {
            "name": "晨光鑫遇专用",
            "provider_key": "cgxy-cpa",
            "base_url": "http://127.0.0.1:8317/v1",
            "api_key": "sk-cgxy",
            "models": ["shared:model"],
        },
    ],
}


def _with_direct_config(monkeypatch, cfg_dict):
    """Point both ``config.cfg`` and ``get_config()`` at ``cfg_dict``."""
    monkeypatch.setattr(config, "get_config", lambda: copy.deepcopy(cfg_dict))
    monkeypatch.setitem(config.cfg, "custom_providers", cfg_dict.get("custom_providers", []))
    monkeypatch.setitem(config.cfg, "model", cfg_dict.get("model", {}))
    monkeypatch.setitem(config.cfg, "providers", cfg_dict.get("providers", {}))


def test_advertised_option_resolves_to_the_records_own_endpoint(monkeypatch):
    """The picker option ``@custom:cgxy-cpa:shared:model`` must route back to the entry."""
    _with_direct_config(monkeypatch, _MAINTAINER_CFG)

    model, provider, base_url = config.resolve_model_provider("@custom:cgxy-cpa:shared:model")

    assert model == "shared:model"
    assert provider == "custom:cgxy-cpa"
    # The provider_key-derived identity belongs to the record, so the record's
    # own endpoint must come back with it (was None: unresolved).
    assert base_url == "http://127.0.0.1:8317/v1"


def test_advertised_slug_selects_the_record_for_the_runtime_bundle(monkeypatch):
    """``resolve_custom_provider_bundle`` must find the record behind the advertised slug."""
    _with_direct_config(monkeypatch, _MAINTAINER_CFG)

    bundle = config.resolve_custom_provider_bundle("custom:cgxy-cpa")

    assert bundle is not None, "a named custom route must report its selection outcome"
    assert bundle["status"] == config.CUSTOM_SELECTION_EXACT, bundle["status"]
    assert bundle["is_exact"] is True
    assert bundle["record"] is not None
    assert bundle["record"]["name"] == "晨光鑫遇专用"
    assert bundle["base_url"] == "http://127.0.0.1:8317/v1"
    assert bundle["api_key"] == "sk-cgxy"


def test_connection_ladder_resolves_for_the_advertised_slug(monkeypatch):
    """The URL/key view must resolve from the same identity the picker advertises."""
    _with_direct_config(monkeypatch, _MAINTAINER_CFG)

    assert config.resolve_custom_provider_connection("custom:cgxy-cpa") == (
        "sk-cgxy",
        "http://127.0.0.1:8317/v1",
    )


def test_merge_produces_a_constructor_ready_bundle(monkeypatch):
    """The production merge must pair the record's endpoint and key, not fail closed."""
    _with_direct_config(monkeypatch, _MAINTAINER_CFG)

    bundle = config.merge_custom_provider_runtime_bundle(
        "custom:cgxy-cpa",
        None,
        None,
        None,
        lookup_provider="custom:cgxy-cpa",
    )
    assert bundle[config.CUSTOM_ROUTE_ERROR_FIELD] is None, bundle
    assert bundle["base_url"] == "http://127.0.0.1:8317/v1"
    assert bundle["api_key"] == "sk-cgxy"
    # The named provider supplied the concrete endpoint, so the route goes
    # through the generic custom client (config.py's merge contract).
    assert bundle["provider"] == "custom"
    # Constructor-ready: the single chokepoint must not raise for this route.
    assert config.raise_for_custom_provider_route(bundle) is bundle


def test_bare_model_matches_through_the_fallback_identity(monkeypatch):
    """A bare model owned by the entry must resolve to its advertised identity."""
    _with_direct_config(monkeypatch, _MAINTAINER_CFG)

    model, provider, base_url = config.resolve_model_provider("shared:model")

    assert model == "shared:model"
    assert provider == "custom:cgxy-cpa"
    assert base_url == "http://127.0.0.1:8317/v1"


def test_endpoint_derived_identity_round_trips(monkeypatch):
    """A non-ASCII name without provider_key falls back to the endpoint slug; both
    the hyphenated and the colon forms the WebUI historically used must resolve."""
    cfg_dict = {
        "model": {"provider": "custom", "default": "m1"},
        "custom_providers": [
            {
                "name": "晨光鑫遇专用",
                "base_url": "http://127.0.0.1:8317/v1",
                "api_key": "sk-2",
                "models": ["m1"],
            },
        ],
    }
    _with_direct_config(monkeypatch, cfg_dict)
    entry = cfg_dict["custom_providers"][0]
    assert config._named_custom_slug_fallback(entry, "晨光鑫遇专用") == "custom:127.0.0.1-8317"

    model, provider, base_url = config.resolve_model_provider("@custom:127.0.0.1-8317:m1")
    assert (provider, base_url) == ("custom:127.0.0.1-8317", "http://127.0.0.1:8317/v1")

    bundle = config.resolve_custom_provider_bundle("custom:127.0.0.1-8317")
    assert bundle["status"] == config.CUSTOM_SELECTION_EXACT
    assert bundle["base_url"] == "http://127.0.0.1:8317/v1"
    assert bundle["api_key"] == "sk-2"

    # The colon spelling (custom:<host>:<port>) is the legacy WebUI shape for
    # endpoint-derived slugs and must select the same record.
    colon = config.resolve_custom_provider_bundle("custom:127.0.0.1:8317")
    assert colon["status"] == config.CUSTOM_SELECTION_EXACT
    assert colon["record"] == bundle["record"]


def test_name_hash_identity_round_trips(monkeypatch):
    """With no provider_key and no base_url, the short name hash is the identity;
    the bundle must still select the record and resolve its credential."""
    cfg_dict = {
        "model": {"provider": "custom", "default": "m1"},
        "custom_providers": [
            {
                "name": "晨光鑫遇专用",
                "api_key": "sk-3",
                "models": ["m1"],
            },
        ],
    }
    _with_direct_config(monkeypatch, cfg_dict)
    entry = cfg_dict["custom_providers"][0]
    slug = config._named_custom_slug_fallback(entry, "晨光鑫遇专用")
    assert slug.startswith("custom:")

    model, provider, _base_url = config.resolve_model_provider(f"@{slug}:m1")
    assert provider == slug

    bundle = config.resolve_custom_provider_bundle(slug)
    assert bundle["status"] == config.CUSTOM_SELECTION_EXACT
    assert bundle["record"] is not None
    assert bundle["api_key"] == "sk-3"


def test_ascii_name_round_trip_is_unchanged(monkeypatch):
    """An ASCII name keeps its historical name-derived identity end to end."""
    _with_direct_config(monkeypatch, {
        "model": {"provider": "custom", "default": "m1"},
        "custom_providers": [
            {
                "name": "cgxy-cpa",
                "base_url": "http://127.0.0.1:8317/v1",
                "api_key": "sk-cgxy",
                "models": ["m1"],
            },
        ],
    })

    model, provider, base_url = config.resolve_model_provider("@custom:cgxy-cpa:m1")
    assert (provider, base_url) == ("custom:cgxy-cpa", "http://127.0.0.1:8317/v1")

    bundle = config.resolve_custom_provider_bundle("custom:cgxy-cpa")
    assert bundle["status"] == config.CUSTOM_SELECTION_EXACT
    assert bundle["base_url"] == "http://127.0.0.1:8317/v1"


def test_fallback_identity_collides_fail_closed(monkeypatch):
    """A name-derived slug and a provider_key-derived slug that collide must
    raise the same ambiguity error the name-only collision path already did."""
    _with_direct_config(monkeypatch, {
        "model": {"provider": "custom"},
        "custom_providers": [
            {
                "name": "cgxy-cpa",
                "base_url": "http://a.example/v1",
                "api_key": "k1",
                "models": ["m1"],
            },
            {
                "name": "晨光鑫遇专用",
                "provider_key": "cgxy-cpa",
                "base_url": "http://b.example/v1",
                "api_key": "k2",
                "models": ["m2"],
            },
        ],
    })

    with pytest.raises(config.AmbiguousCustomProviderError):
        config.resolve_custom_provider_bundle("custom:cgxy-cpa")
