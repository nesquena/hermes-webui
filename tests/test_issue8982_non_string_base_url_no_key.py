"""Regression coverage for #8982 — a non-string ``base_url`` carries no key.

``base_url`` comes from YAML, where a bare ``123`` parses as an ``int``.
``api/config.py``'s ``_custom_record_base_url()`` used to coerce whatever it
found with ``str()``, so a malformed setting became the endpoint ``"123"``: the
record still counted as endpoint-owning, and its ``api_key`` travelled with that
non-URL into ``agent.model_metadata.get_model_context_length()`` through
``api/routes.py``'s ``_session_context_length_lookup_state()`` (the GET
``/api/session`` context-length lookup).

A non-string value is not an endpoint. It is now reported exactly like an absent
one, so the existing merge verdict (``custom_provider_endpoint_unresolved``)
refuses the route before any credential is paired with it — one rule at the
record boundary, inherited by every consumer.

Both origins of a named custom route are covered: a legacy ``custom_providers[]``
row and a raw ``providers:`` record (the standard v12 shape).
"""

import sys
import types

import pytest

import api.config as config
import api.routes as routes

_MODEL = "issue8982-model"
_API_KEY = "sk-issue8982-secret"
_GOOD_URL = "https://omni.example/v1"


def _install_fake_context_resolver(monkeypatch, value=128000):
    """Install a recording stand-in for the agent's context-length resolver."""
    calls = []
    mod = types.ModuleType("agent.model_metadata")

    def _fake(
        model,
        base_url="",
        *args,
        api_key=None,
        config_context_length=None,
        provider="",
        custom_providers=None,
        **kwargs,
    ):
        calls.append(
            {
                "model": model,
                "base_url": base_url,
                "api_key": api_key,
                "config_context_length": config_context_length,
                "provider": provider,
                "custom_providers": custom_providers,
            }
        )
        return value

    mod.get_model_context_length = _fake
    if "agent" not in sys.modules:
        agent_pkg = types.ModuleType("agent")
        agent_pkg.__path__ = []
        monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.model_metadata", mod)
    return calls


def _apply_config(monkeypatch, cfg):
    """Point both views of the config at ``cfg`` for this test."""
    monkeypatch.setattr(config, "cfg", cfg)
    monkeypatch.setattr(config, "get_config", lambda *a, **k: cfg)


def _legacy_row(base_url):
    """A legacy ``custom_providers[]`` entry that owns ``_MODEL``."""
    return {
        "name": "omni",
        "base_url": base_url,
        "api_key": _API_KEY,
        "model": _MODEL,
    }


def _legacy_cfg(base_url):
    return {
        "model": {"default": _MODEL, "provider": "custom:omni"},
        "providers": {},
        "custom_providers": [_legacy_row(base_url)],
    }


def _providers_cfg(base_url):
    """The v12 shape: the route is a raw ``providers:<key>`` record."""
    return {
        "model": {"default": _MODEL, "provider": "custom:relay"},
        "providers": {
            "relay": {"base_url": base_url, "api_key": _API_KEY, "models": [_MODEL]},
        },
        "custom_providers": [],
    }


@pytest.mark.parametrize("malformed", [123, True, {"host": "omni"}, ["https://x"]])
def test_legacy_custom_providers_entry_non_string_base_url_enters_no_lookup(
    monkeypatch, malformed
):
    """(a) A ``custom_providers[]`` row with a non-str base_url yields no URL/key."""
    _apply_config(monkeypatch, _legacy_cfg(malformed))
    calls = _install_fake_context_resolver(monkeypatch)

    model, provider, base_url, api_key = routes._session_context_length_lookup_state(
        _MODEL, "custom:omni"
    )

    assert (provider, base_url, api_key) == ("custom:omni", "", "")
    # The record still owns the slug; it just declares no endpoint. That is the
    # same bundle an empty ``base_url`` produces, and the merge refuses it.
    bundle = config.resolve_custom_provider_bundle("custom:omni", lookup_only=True)
    assert bundle["base_url"] is None
    assert bundle["endpoint_owned"] is False

    routes._resolve_context_length_for_session_model(
        model, provider, base_url=base_url, api_key=api_key, caller_key_only=True
    )
    assert [c for c in calls if c["base_url"] or c["api_key"]] == []


def test_providers_record_non_string_base_url_enters_no_lookup(monkeypatch):
    """(b) A raw ``providers:`` record with an int base_url yields no URL/key.

    This is the shape that used to reach ``get_model_context_length()`` with
    ``base_url="123"`` and the record's api_key beside it.
    """
    _apply_config(monkeypatch, _providers_cfg(123))
    calls = _install_fake_context_resolver(monkeypatch)

    model, provider, base_url, api_key = routes._session_context_length_lookup_state(
        _MODEL, "custom:relay"
    )

    assert (provider, base_url, api_key) == ("custom:relay", "", "")
    bundle = config.resolve_custom_provider_bundle("custom:relay", lookup_only=True)
    assert bundle["base_url"] is None
    assert bundle["endpoint_owned"] is False

    routes._resolve_context_length_for_session_model(
        model, provider, base_url=base_url, api_key=api_key, caller_key_only=True
    )
    assert [c for c in calls if c["base_url"] or c["api_key"]] == []


@pytest.mark.parametrize(
    "cfg_builder, provider",
    [(_legacy_cfg, "custom:omni"), (_providers_cfg, "custom:relay")],
    ids=["custom_providers", "providers"],
)
def test_string_base_url_still_resolves_url_and_key(monkeypatch, cfg_builder, provider):
    """Control: a valid string endpoint keeps working, key included."""
    _apply_config(monkeypatch, cfg_builder(_GOOD_URL))
    calls = _install_fake_context_resolver(monkeypatch)

    model, resolved_provider, base_url, api_key = (
        routes._session_context_length_lookup_state(_MODEL, provider)
    )

    assert (resolved_provider, base_url, api_key) == (provider, _GOOD_URL, _API_KEY)

    routes._resolve_context_length_for_session_model(
        model,
        resolved_provider,
        base_url=base_url,
        api_key=api_key,
        caller_key_only=True,
    )
    assert calls[-1]["base_url"] == _GOOD_URL
    assert calls[-1]["api_key"] == _API_KEY


def test_empty_string_base_url_behaviour_is_unchanged(monkeypatch):
    """The already-empty case keeps its current contract: no URL, no key."""
    _apply_config(monkeypatch, _legacy_cfg(""))

    _, provider, base_url, api_key = routes._session_context_length_lookup_state(
        _MODEL, "custom:omni"
    )

    assert (provider, base_url, api_key) == ("custom:omni", "", "")
