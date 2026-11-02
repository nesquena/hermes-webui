"""#8685: provider-routing regressions from #8448 (exp-v0.52.597), fixed to v0.52.544 behaviour.

A. ``providers['custom:<builtin>']`` must not take over the bare built-in selector. With
   ``model.provider: openai`` plus ``providers.openai`` and ``providers['custom:openai']``, a bare
   model turn on .597 resolved to ``custom:openai`` (custom endpoint and key) while Settings showed
   ``openai`` active. v0.52.544 keeps the built-in route in either dict order; only the explicit
   ``custom:openai`` selector reaches the named record. A legacy ``custom_providers`` entry named
   ``openai`` still claims the bare name, as on v0.52.544.
B. An empty or malformed ``custom_providers`` must not hide native providers. On .597 their models
   resolved to built-in ``openai`` with no endpoint; v0.52.544 routed them to their own endpoint.
"""

from __future__ import annotations

import copy

import pytest

from api import config

BUILTIN_URL, BUILTIN_KEY = "https://builtin.invalid/v1", "synthetic-builtin"
NAMED_URL, NAMED_KEY = "https://named.invalid/v1", "synthetic-named"
NATIVE_URL = "https://myprov.invalid/v1"


def _bind(monkeypatch, data):
    monkeypatch.setattr(config, "cfg", data)
    monkeypatch.setattr(config, "get_config", lambda: data)


def _collision_config(pid, reverse):
    records = [
        (pid, {"base_url": BUILTIN_URL, "api_key": BUILTIN_KEY}),
        ("custom:" + pid, {"base_url": NAMED_URL, "api_key": NAMED_KEY, "models": ["stable-model"]}),
    ]
    return {"model": {"provider": pid, "default": "stable-model"},
            "providers": dict(reversed(records) if reverse else records)}


@pytest.mark.parametrize("reverse", [False, True], ids=["builtin-first", "named-first"])
@pytest.mark.parametrize("pid", ["openai", "x-ai", "google"])
def test_a_native_custom_builtin_record_does_not_take_over_the_bare_builtin(monkeypatch, pid, reverse):
    data = _collision_config(pid, reverse)
    named = "custom:" + pid
    without_named = {**data, "providers": {k: v for k, v in data["providers"].items() if k != named}}
    _bind(monkeypatch, data)
    before = copy.deepcopy(data)

    bare = config._resolve_configured_provider_id(pid, data)
    assert bare != named
    assert bare == config._resolve_configured_provider_id(pid, without_named)
    model, provider, base_url = config.resolve_model_provider(config.model_with_provider_context("stable-model", None))
    assert provider == pid and base_url is None  # the built-in route, not the named endpoint
    # The explicit selector still reaches the named record, with its own endpoint and key.
    assert config._resolve_configured_provider_id(named, data) == named
    bundle = config.resolve_custom_provider_bundle(named)
    assert (bundle["base_url"], bundle["api_key"]) == (NAMED_URL, NAMED_KEY)
    assert config.resolve_model_provider(f"@{named}:stable-model") == ("stable-model", named, NAMED_URL)
    assert data == before


def test_a_legacy_entry_named_like_a_builtin_still_claims_the_bare_name(monkeypatch):
    """v0.52.544 parity: the legacy list's named preference is unchanged."""
    data = {"model": {"provider": "openai", "default": "stable-model"},
            "custom_providers": [{"name": "openai", "base_url": NAMED_URL, "models": ["stable-model"]}],
            "providers": {"openai": {"base_url": BUILTIN_URL, "api_key": BUILTIN_KEY}}}
    _bind(monkeypatch, data)
    assert config._resolve_configured_provider_id("openai", data) == "custom:openai"
    model, provider, base_url = config.resolve_model_provider(config.model_with_provider_context("stable-model", None))
    assert (provider, base_url) == ("custom:openai", NAMED_URL)


@pytest.mark.parametrize("legacy", [[], None, {}, "malformed", [None, 3]],
                         ids=["empty", "none", "dict", "string", "junk-items"])
def test_an_empty_or_malformed_legacy_list_keeps_native_providers_routed(monkeypatch, legacy):
    native = {"myprov": {"api_key": "synthetic-myprov", "base_url": NATIVE_URL, "models": ["my-model"]}}
    without = {"model": {"provider": "openai", "default": "gpt-x"}, "providers": copy.deepcopy(native)}
    data = {**copy.deepcopy(without), "custom_providers": legacy}

    _bind(monkeypatch, without)
    expected_route = config.resolve_model_provider(config.model_with_provider_context("my-model", None))
    expected_entries = config._custom_provider_entries(without)
    assert expected_route[2] == NATIVE_URL

    _bind(monkeypatch, data)
    assert config._custom_provider_entries(data) == expected_entries
    assert config.resolve_model_provider(config.model_with_provider_context("my-model", None)) == expected_route


def test_a_real_legacy_entry_still_wins_its_identity_over_a_native_one(monkeypatch):
    """Mixed stores are unchanged: a non-empty legacy list keeps its precedence by identity."""
    data = {"model": {"provider": "openai", "default": "gpt-x"},
            "custom_providers": [{"name": "myprov", "base_url": "https://legacy-myprov.invalid/v1", "models": ["my-model"]}],
            "providers": {"myprov": {"api_key": "synthetic-myprov", "base_url": NATIVE_URL, "models": ["my-model"]}}}
    _bind(monkeypatch, data)
    entries = config._custom_provider_entries(data)
    assert [e["base_url"] for e in entries] == ["https://legacy-myprov.invalid/v1"]


@pytest.mark.parametrize("field,value", [("model", 42), ("model", ["x"]), ("default_model", 42), ("model", "   ")])
@pytest.mark.parametrize("legacy", ["absent", [], {}, [{"name": "other", "base_url": "https://other.invalid/v1"}]],
                         ids=["no-key", "empty", "dict", "unrelated-entry"])
def test_a_non_string_native_model_does_not_break_unrelated_routes(monkeypatch, field, value, legacy):
    """Gate finding on b95e28fcc: ``providers.myprov.model: 42`` crashed resolution of an
    unrelated ``@openai:...`` selector (``.strip()`` on an int) on exp-v0.52.597+ whenever the
    native view was read. v0.52.544 and .596 resolve it; the legacy-shaped view now carries only
    string model names."""
    data = {"model": {"provider": "openai", "default": "other"},
            "providers": {"myprov": {"base_url": NATIVE_URL, field: value}}}
    if legacy != "absent":
        data["custom_providers"] = legacy
    _bind(monkeypatch, data)
    assert config.resolve_model_provider("@openai:other") == ("other", "openai", None)
    assert config.resolve_model_provider("other") == ("other", "openai", None)
    native = [e for e in config._custom_provider_entries(data) if e.get("base_url") == NATIVE_URL]
    assert [e["model"] for e in native] == [""]


def test_a_string_native_default_model_is_still_the_view_model(monkeypatch):
    data = {"model": {"provider": "openai", "default": "other"},
            "providers": {"myprov": {"base_url": NATIVE_URL, "model": 42, "default_model": "my-default"}}}
    _bind(monkeypatch, data)
    assert [e["model"] for e in config._custom_provider_entries(data)] == ["my-default"]


def test_a_whitespace_native_model_falls_back_to_a_string_default_model(monkeypatch):
    """Round-2 gate: a whitespace-only ``model`` is no model name, so a string ``default_model``
    names the default (.600 kept the blank and emitted a ``@custom:myprov:   `` catalog row). The
    Agent itself reads only ``default_model`` for keyed records."""
    data = {"model": {"provider": "openai", "default": "other"},
            "providers": {"myprov": {"base_url": NATIVE_URL, "model": "   ", "default_model": "d1"}}}
    _bind(monkeypatch, data)
    assert [e["model"] for e in config._custom_provider_entries(data)] == ["d1"]
    assert config.resolve_model_provider("@openai:other") == ("other", "openai", None)
