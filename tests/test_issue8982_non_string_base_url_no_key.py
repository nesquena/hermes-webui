"""#8982: a non-string ``base_url`` is no URL, so the lookup carries no key.

A config record or legacy ``custom_providers[]`` entry can declare a non-string
``base_url`` (the YAML integer ``base_url: 123``, a boolean, a mapping). Blind
``str()`` coercion turned that into the URL-shaped value ``"123"``: the route
looked owned, that value became GET /api/session's lookup URL, and the route's
credential travelled beside it to ``get_model_context_length``.

The guard lives where the route bundle's endpoint is merged
(:func:`api.config._custom_record_base_url`), so every consumer inherits it: the
chat route, the ``(api_key, base_url)`` resolver view and the session lookup.
A record that declares no usable endpoint takes the path an endpointless record
already takes — the terminal ``custom_provider_endpoint_unresolved`` verdict —
which is why the lookup then has no URL to hand a key to.

Impact stays "none on the wire" (``"123"`` has no scheme and no host, so the
resolver fails before any request). This is a correctness gap, not a leak.

The model switch (#8963) reads the same helper as GET, so it inherits the fix;
its own routing is covered by ``tests/test_issue8963_context_lookup_runtime_chain.py``.
"""
import sys
import types
from pathlib import Path

import pytest

from api import config as config_mod
from api import routes

REPO = Path(__file__).resolve().parent.parent
URL = "https://ok.proxy.test/v1"

# Every non-string shape a hand-edited config.yaml can produce for a URL field.
NON_STRING_URLS = [
    pytest.param(123, id="int"),
    pytest.param(True, id="bool"),
    pytest.param({"url": URL}, id="dict"),
    pytest.param([URL], id="list"),
    pytest.param(123.5, id="float"),
]


def _providers_cfg(base_url):
    """Shape (a): the ``providers`` record the route bundle is merged from."""
    return {
        "model": {"provider": "custom:my-proxy"},
        "providers": {"custom:my-proxy": {"base_url": base_url, "api_key": "KB"}},
        "custom_providers": [],
    }


def _legacy_cfg(base_url):
    """Shape (b): a legacy ``custom_providers[]`` entry."""
    return {
        "model": {"provider": "custom:my-proxy"},
        "providers": {},
        "custom_providers": [
            {"name": "my-proxy", "base_url": base_url, "api_key": "KL"}
        ],
    }


ORIGINS = [
    pytest.param(_providers_cfg, "KB", id="providers-record"),
    pytest.param(_legacy_cfg, "KL", id="legacy-custom-entry"),
]


def _install_config(monkeypatch, cfg):
    monkeypatch.setattr(config_mod, "cfg", cfg)
    monkeypatch.setattr(config_mod, "get_config", lambda *a, **k: cfg)


def _install_fake_resolver(monkeypatch):
    """Record every resolver call so a test can see exactly what the Agent gets."""
    calls = []
    fake_agent = types.ModuleType("agent")
    fake_agent.__path__ = []
    metadata = types.ModuleType("agent.model_metadata")

    def _fake(model, base_url="", *args, **kwargs):
        calls.append({"model": model, "base_url": base_url, "api_key": kwargs.get("api_key")})
        return 0

    metadata.get_model_context_length = _fake
    fake_agent.model_metadata = metadata  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent", fake_agent)
    monkeypatch.setitem(sys.modules, "agent.model_metadata", metadata)
    return calls


# ── Shape (a) and shape (b): the lookup state itself ─────────────────────────


@pytest.mark.parametrize("make_cfg,key", ORIGINS)
@pytest.mark.parametrize("base_url", NON_STRING_URLS)
def test_a_non_string_route_url_hands_the_lookup_no_url_and_no_key(
    make_cfg, key, base_url, monkeypatch
):
    cfg = make_cfg(base_url)
    _install_config(monkeypatch, cfg)

    model, provider, lookup_url, lookup_key = routes._session_context_length_lookup_state(
        "m-x", "custom:my-proxy"
    )

    assert provider == "custom:my-proxy"
    assert lookup_url == "", f"a non-string base_url became the URL {lookup_url!r}"
    assert lookup_key == "", f"the route key {key} rode along with no URL"


# ── End to end: what the Agent's resolver is actually handed ─────────────────


@pytest.mark.parametrize("make_cfg,key", ORIGINS)
def test_the_resolver_is_handed_no_url_and_no_key_for_a_malformed_route(
    make_cfg, key, monkeypatch
):
    cfg = make_cfg(123)
    _install_config(monkeypatch, cfg)
    calls = _install_fake_resolver(monkeypatch)

    got = routes._resolve_context_length_for_session_model(
        "m-x", "custom:my-proxy", caller_key_only=True
    )

    assert got == 0
    assert calls, "the lookup must still answer, just without a route pair"
    for call in calls:
        assert call["api_key"] == "", f"key {key} reached the lookup"
        assert "123" not in str(call["base_url"]), (
            f"the integer base_url reached the resolver as {call['base_url']!r}"
        )


# ── Control: the good path is untouched ──────────────────────────────────────


@pytest.mark.parametrize("make_cfg,key", ORIGINS)
def test_a_string_route_url_still_returns_the_url_and_the_route_key(
    make_cfg, key, monkeypatch
):
    cfg = make_cfg(URL)
    _install_config(monkeypatch, cfg)
    calls = _install_fake_resolver(monkeypatch)

    model, provider, lookup_url, lookup_key = routes._session_context_length_lookup_state(
        "m-x", "custom:my-proxy"
    )
    assert (lookup_url, lookup_key) == (URL, key)

    routes._resolve_context_length_for_session_model(
        "m-x", "custom:my-proxy", base_url=lookup_url, api_key=lookup_key,
        caller_key_only=True,
    )
    assert calls[-1]["base_url"] == URL
    assert calls[-1]["api_key"] == key


def test_an_empty_string_route_url_keeps_its_current_treatment(monkeypatch):
    """``base_url: ""`` already meant "no URL, no key" — the fix must not change it."""
    _install_config(monkeypatch, _providers_cfg(""))
    _model, _provider, lookup_url, lookup_key = routes._session_context_length_lookup_state(
        "m-x", "custom:my-proxy"
    )
    assert (lookup_url, lookup_key) == ("", "")


# ── The merge-layer rule every consumer inherits ──────────────────────────────


@pytest.mark.parametrize("base_url", NON_STRING_URLS)
@pytest.mark.parametrize("make_cfg,key", ORIGINS)
def test_the_merge_layer_reads_a_non_string_url_as_no_endpoint(
    make_cfg, key, base_url, monkeypatch
):
    _install_config(monkeypatch, make_cfg(base_url))
    assert config_mod._custom_record_base_url({"base_url": base_url}) is None
    # The resolver's url/key VIEW of the record, used by the streaming sites.
    api_key, resolved_url = config_mod.resolve_custom_provider_connection("custom:my-proxy")
    assert api_key == key
    assert resolved_url is None


@pytest.mark.parametrize("base_url", NON_STRING_URLS)
def test_a_malformed_record_is_no_longer_a_routable_chat_route(base_url, monkeypatch):
    """It fails closed with the endpoint verdict an endpointless record already gets."""
    _install_config(monkeypatch, _providers_cfg(base_url))
    bundle, _custom = config_mod._custom_provider_runtime_bundle_with_provenance(
        "custom:my-proxy",
        None,
        None,
        {},
        lookup_provider="custom:my-proxy",
        connection_resolver=config_mod.resolve_custom_provider_connection,
        lookup_only=True,
    )

    verdict = config_mod.custom_provider_route_error(bundle)
    assert verdict is not None, "a route with no usable URL must not look routable"
    assert verdict["reason"] == config_mod.CUSTOM_ROUTE_NO_ENDPOINT
    assert bundle["base_url"] is None
    assert bundle["api_key"] is None


def test_a_string_url_still_merges_as_the_route_endpoint(monkeypatch):
    _install_config(monkeypatch, _providers_cfg(URL))
    api_key, resolved_url = config_mod.resolve_custom_provider_connection("custom:my-proxy")
    assert (api_key, resolved_url) == ("KB", URL)


# ── The switch reads the same helper (#8963) ─────────────────────────────────


def test_get_and_the_model_switch_read_the_same_helper():
    """GET /api/session and the model switch share this helper, so the guard reaches both."""
    source = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
    assert source.count("_session_context_length_lookup_state(") >= 3, (
        "expected the helper definition plus the GET and switch callsites"
    )
