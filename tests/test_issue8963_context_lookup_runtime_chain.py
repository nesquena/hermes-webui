"""#8963 (model-switch half): a named custom provider's context lookup takes the chat route's pair.

On a model switch, the context-length lookup used to pick the key with its own config selection
(``_context_length_lookup_inputs_for_model``), which predicts the record the chat routes through and drifts from it:
- a raw-named ``providers.h`` record serves a ``custom:h`` session in WebUI's merge, but the selection never matched
  it, so the model block's key went to the URL instead (Codex round 12 on #8956);
- a record's key spelled ``api_key_env`` (#8938 item 2) is read by the merge for the chat, never by the selection.

For a ``custom:<name>`` session the switch now asks GET's helper (``_session_context_length_lookup_state``, #8928):
WebUI's own half of the chat's routing chain, read-only (``lookup_only``), with ``caller_key_only`` so the selection
can't add a key. The same helper sends no key when the chat refuses the provider as switched off (``enabled: false``
on its authoritative record, the verdict #8938 item 1 gave the config selection). Every expected pair below is the
chat's own, measured on the real chain with the installed Agent. Other providers keep the previous path.
"""
import copy
import sys
import types
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

import api.config as config
from api import routes

A = "https://a.chat-url.test/v1"
B = "https://b.other-url.test/v1"
L = "https://l.legacy-url.test/v1"
VAR = "T8963_RECORD_KEY"


def _cfg(providers, *, model=None, legacy=None):
    return {
        "model": dict(model or {"provider": "custom:h", "default": "m-x"}),
        "providers": providers,
        "custom_providers": list(legacy or []),
    }


def _model_k1():
    return {"provider": "custom:h", "default": "m-x", "base_url": A, "api_key": "K1"}


def _fake_metadata(calls):
    def fake_get_model_context_length(model, base_url="", api_key="", **kwargs):
        calls.append((base_url, api_key))
        return 123_456

    mod = types.ModuleType("agent.model_metadata")
    mod.get_model_context_length = fake_get_model_context_length
    return mod


def _use(cfg, monkeypatch):
    cfg = copy.deepcopy(cfg)
    monkeypatch.setattr(config, "cfg", cfg)
    monkeypatch.setattr(config, "get_config", lambda *a, **k: cfg)
    return cfg


def _switch(cfg, monkeypatch, provider="custom:h"):
    """Run the real model-switch site and return every (URL, key) the lookup sent."""
    _use(cfg, monkeypatch)
    calls = []
    session = types.SimpleNamespace(model="m-old", model_provider=provider, last_used_model=None, gateway_routing=None,
                                    context_length=0, threshold_tokens=0, last_prompt_tokens=0)
    with patch.dict(sys.modules, {"agent.model_metadata": _fake_metadata(calls)}):
        changed = routes._invalidate_session_route_state_if_changed(session, model="m-x", provider=provider)
    assert changed
    assert session.context_length == 123_456
    return calls


def _stub_session(provider):
    s = MagicMock()
    for attr in ("context_length", "threshold_tokens", "last_prompt_tokens", "input_tokens", "output_tokens"):
        setattr(s, attr, 0)
    s.read_only = False
    s._loaded_metadata_only = False
    s.profile = None
    s.model = "m-x"
    s.model_provider = provider
    s.title = s.session_id = "t8963"
    s.messages, s.tool_calls, s.pending_attachments = [], [], []
    s.active_stream_id = s.pending_user_message = s.pending_started_at = None
    s.compact.return_value = {"session_id": "t8963", "title": "t8963", "model": "m-x", "input_tokens": 0,
                              "output_tokens": 0, "context_length": 0, "threshold_tokens": 0, "last_prompt_tokens": 0}
    return s


def _get(cfg, monkeypatch, provider="custom:h"):
    """Run GET /api/session (resolve_model=1, no persisted window) and return every (URL, key) the lookup sent."""
    _use(cfg, monkeypatch)
    calls, captured = [], {}
    with patch("api.routes.get_session", return_value=_stub_session(provider)), \
         patch("api.routes.j", side_effect=lambda h, data, status=200: captured.update(status=status)), \
         patch.dict(sys.modules, {"agent.model_metadata": _fake_metadata(calls)}):
        routes.handle_get(MagicMock(), urlparse("/api/session?session_id=t8963&messages=0&resolve_model=1"))
    assert captured.get("status", 200) == 200, captured
    return calls


def _alias(key, enabled, spelling="Custom:H"):
    return {spelling: {"base_url": A, "api_key": key, "enabled": enabled}}


RAW_H = {"h": {"base_url": A, "api_key": "K2"}}

# (id, config, the chat's pair). The chat routes a custom:h session through WebUI's merge, which serves the raw-named
# providers.h record; a case variant of the key is an alias the Agent's matcher skips when switched off.
CHAT_ROUTE_SHAPES = [
    ("r12-codex-disabled-alias-same-key", _cfg({**_alias("K2", False), **RAW_H}, model=_model_k1()), (A, "K2")),
    ("r12-disabled-alias-other-key", _cfg({**_alias("K3", False), **RAW_H}, model=_model_k1()), (A, "K2")),
    ("r12-enabled-alias", _cfg({**_alias("K2", True), **RAW_H}, model=_model_k1()), (A, "K2")),
    ("r12-raw-h-only", _cfg(dict(RAW_H), model=_model_k1()), (A, "K2")),
    ("r11-s1-case-variant-beside-exact",
     _cfg({"Custom:H": {"base_url": B, "api_key_env": VAR}, "custom:h": {"base_url": A, "api_key": "KR"}},
          model={"provider": "custom:h", "default": "m-x", "base_url": A}), (A, "KR")),
    ("r11-s1-space-variant-beside-exact",
     _cfg({" custom:h ": {"base_url": B, "api_key_env": VAR}, "custom:h": {"base_url": A, "api_key": "KR"}},
          model={"provider": "custom:h", "default": "m-x", "base_url": A}), (A, "KR")),
    ("item2-own-source-api-key-env", _cfg({"custom:h": {"base_url": A, "api_key_env": VAR}}), (A, "KV")),
    ("item2-own-source-beside-model-key",
     _cfg({"custom:h": {"base_url": A, "api_key_env": VAR}},
          model={"provider": "custom:h", "default": "m-x", "base_url": A, "api_key": "KM"}), (A, "KV")),
]


@pytest.mark.parametrize("cfg,chat_pair", [s[1:] for s in CHAT_ROUTE_SHAPES], ids=[s[0] for s in CHAT_ROUTE_SHAPES])
def test_model_switch_sends_the_chat_routes_pair(monkeypatch, cfg, chat_pair):
    monkeypatch.setenv(VAR, "KV")
    assert _switch(cfg, monkeypatch) == [chat_pair]


@pytest.mark.parametrize("cfg,chat_pair", [s[1:] for s in CHAT_ROUTE_SHAPES], ids=[s[0] for s in CHAT_ROUTE_SHAPES])
def test_model_switch_and_get_agree(monkeypatch, cfg, chat_pair):
    monkeypatch.setenv(VAR, "KV")
    assert _switch(cfg, monkeypatch) == _get(cfg, monkeypatch) == [chat_pair]


def test_the_expected_pairs_are_the_merges(monkeypatch):
    """The expectation is WebUI's merge (the chat's route), not a fixture choice: check it for every shape."""
    monkeypatch.setenv(VAR, "KV")
    for name, cfg, chat_pair in CHAT_ROUTE_SHAPES:
        _use(cfg, monkeypatch)
        route = config.merge_custom_provider_runtime_bundle(
            "custom:h", None, None, {}, lookup_provider="custom:h",
            connection_resolver=config.resolve_custom_provider_connection,
        )
        config.raise_for_custom_provider_route(route)
        assert (route["base_url"], route["api_key"]) == chat_pair, name


def test_a_disabled_alias_key_never_reaches_the_lookup(monkeypatch):
    calls = _switch(_cfg({**_alias("K3", False), **RAW_H}, model=_model_k1()), monkeypatch)
    assert all(key != "K3" for _, key in calls), calls


def test_the_model_block_key_no_longer_stands_in_for_the_record(monkeypatch):
    """Codex round 12 on #8956: the selection sent the model block's K1 where the chat sends the raw record's K2."""
    calls = _switch(_cfg({**_alias("K2", False), **RAW_H}, model=_model_k1()), monkeypatch)
    assert calls == [(A, "K2")]
    assert all(key != "K1" for _, key in calls)


# ── Switched off: the chat refuses, the lookup sends no key ───────────────────────────────────────────────────


REFUSED_SHAPES = [
    ("exact-record-disabled-beside-raw-h", _cfg({**_alias("K3", False, spelling="custom:h"), **RAW_H},
                                                 model=_model_k1())),
    ("exact-record-disabled-beside-legacy-entry",
     _cfg({"custom:h": {"base_url": A, "api_key": "KX", "enabled": False}},
          legacy=[{"name": "h", "base_url": L, "api_key": "KL"}])),
    ("exact-record-disabled-alone", _cfg({"custom:h": {"base_url": A, "api_key": "KX", "enabled": False}},
                                         model=_model_k1())),
]


@pytest.mark.parametrize("cfg", [s[1] for s in REFUSED_SHAPES], ids=[s[0] for s in REFUSED_SHAPES])
def test_a_switched_off_provider_looks_up_without_a_key_on_switch_and_get(monkeypatch, cfg):
    switch, get = _switch(cfg, monkeypatch), _get(cfg, monkeypatch)
    assert [key for _, key in switch] == [""], switch
    assert [key for _, key in get] == [""], get


def test_get_sent_the_legacy_key_for_a_refused_session_before(monkeypatch):
    """Senior round 9 on #8956: GET sent the legacy entry's key though the chat refuses custom:h. Its enabled twin
    still sends it, so the verdict is the switch, not the legacy entry."""
    legacy = [{"name": "h", "base_url": L, "api_key": "KL"}]
    on = _cfg({"custom:h": {"base_url": A, "api_key": "KX", "enabled": True}}, legacy=legacy)
    off = _cfg({"custom:h": {"base_url": A, "api_key": "KX", "enabled": False}}, legacy=legacy)
    assert [key for _, key in _get(off, monkeypatch)] == [""]
    assert [key for _, key in _get(on, monkeypatch)] != [""]


def test_the_disabled_verdict_is_the_config_selections(monkeypatch):
    """GET and the switch use #8938 item 1's verdict, so the three paths can't disagree on what is switched off."""
    seen = []
    real = routes._context_lookup_provider_disabled
    monkeypatch.setattr(routes, "_context_lookup_provider_disabled",
                        lambda *a, **k: seen.append(a[0]) or real(*a, **k))
    _get(REFUSED_SHAPES[1][1], monkeypatch)
    assert "custom:h" in seen


# ── The switch takes GET's pair only for a named custom provider ──────────────────────────────────────────────


def test_a_custom_switch_passes_the_route_key_as_final(monkeypatch):
    seen = {}
    real = routes._resolve_context_length_for_session_model

    def spy(*a, **k):
        seen.update(k)
        return real(*a, **k)

    monkeypatch.setattr(routes, "_resolve_context_length_for_session_model", spy)
    _switch(_cfg(dict(RAW_H), model=_model_k1()), monkeypatch)
    assert seen.get("caller_key_only") is True
    assert (seen.get("base_url"), seen.get("api_key")) == (A, "K2")


def test_a_keyless_route_adds_no_key_from_the_selection(monkeypatch):
    """A record the merge judges keyless gets the no-auth placeholder in the chat; caller_key_only keeps the model
    block's key at the same URL out of the lookup."""
    calls = _switch(_cfg({"custom:h": {"base_url": A}}, model=_model_k1()), monkeypatch)
    assert [key for _, key in calls] == [""], calls


@pytest.mark.parametrize("provider", ["openai", "ollama", "custom", "anthropic"])
def test_other_providers_keep_the_config_selection(monkeypatch, provider):
    state = []
    monkeypatch.setattr(routes, "_session_context_length_lookup_state",
                        lambda *a, **k: state.append(a) or ("m-x", provider, "", ""))
    seen = {}
    real = routes._resolve_context_length_for_session_model

    def spy(*a, **k):
        seen.update(k)
        return real(*a, **k)

    monkeypatch.setattr(routes, "_resolve_context_length_for_session_model", spy)
    _switch(_cfg({}, model={"provider": provider, "default": "m-x", "base_url": A, "api_key": "KM"}), monkeypatch,
            provider=provider)
    assert state == []
    assert "caller_key_only" not in seen
    assert seen.get("requested_provider")


def test_the_route_helper_gets_the_stored_model_and_provider(monkeypatch):
    """Resolution isn't idempotent (#8938): the helper resolves the stored pair itself, never a resolved one."""
    got = []
    real = routes._session_context_length_lookup_state
    monkeypatch.setattr(routes, "_session_context_length_lookup_state", lambda *a, **k: got.append(a) or real(*a, **k))
    _switch(_cfg(dict(RAW_H), model=_model_k1()), monkeypatch)
    assert got == [("m-x", "custom:h")]


# ── A raw-named provider resolves to custom:<name> and takes the same route ──────────────────────────────────

RAW_NAMED = [
    ("raw-named-api-key-env", {"mylab": {"base_url": A, "api_key_env": VAR}}, None, (A, "KV")),
    ("raw-named-api-key-env-beside-model-key", {"mylab": {"base_url": A, "api_key_env": VAR}},
     {"provider": "mylab", "default": "m-x", "base_url": A, "api_key": "KM"}, (A, "KV")),
    ("raw-named-literal-beside-model-key", {"mylab": {"base_url": A, "api_key": "KR"}},
     {"provider": "mylab", "default": "m-x", "base_url": A, "api_key": "KM"}, (A, "KR")),
]


@pytest.mark.parametrize("providers,model,chat_pair", [s[1:] for s in RAW_NAMED], ids=[s[0] for s in RAW_NAMED])
def test_a_raw_named_provider_takes_the_chat_route(monkeypatch, providers, model, chat_pair):
    """#8963's raw-named records: the session's stored provider is ``mylab``; WebUI resolves it to
    ``custom:mylab``, whose merge reads the record's ``api_key_env`` for the chat."""
    monkeypatch.setenv(VAR, "KV")
    cfg = _cfg(providers, model=model or {"provider": "mylab", "default": "m-x"})
    assert _switch(cfg, monkeypatch, provider="mylab") == [chat_pair]
    assert _get(cfg, monkeypatch, provider="mylab") == [chat_pair]
    route = config.merge_custom_provider_runtime_bundle(
        "custom:mylab", None, None, {}, lookup_provider="custom:mylab",
        connection_resolver=config.resolve_custom_provider_connection,
    )
    config.raise_for_custom_provider_route(route)
    assert (route["base_url"], route["api_key"]) == chat_pair


def test_the_route_helper_gets_a_raw_named_sessions_stored_provider(monkeypatch):
    got = []
    real = routes._session_context_length_lookup_state
    monkeypatch.setattr(routes, "_session_context_length_lookup_state", lambda *a, **k: got.append(a) or real(*a, **k))
    monkeypatch.setenv(VAR, "KV")
    _switch(_cfg({"mylab": {"base_url": A, "api_key_env": VAR}}, model={"provider": "mylab", "default": "m-x"}),
            monkeypatch, provider="mylab")
    assert got == [("m-x", "mylab")]


def test_a_generic_custom_session_keeps_the_config_selection(monkeypatch):
    """WebUI's read-only merge has no key for the generic ``custom`` provider (the Agent supplies the chat's), so
    taking its route would drop the model block's key the selection sends today. The switch keeps the selection."""
    calls = _switch(_cfg({"custom": {"base_url": A, "api_key_env": VAR}},
                         model={"provider": "custom", "default": "m-x", "base_url": A, "api_key": "KM"}),
                    monkeypatch, provider="custom")
    assert calls == [(A, "KM")]
