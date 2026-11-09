"""#8963 (model-switch half): a named custom provider's context lookup takes the chat route's pair.

On a model switch, the context-length lookup used to pick the key with its own config selection
(``_context_length_lookup_inputs_for_model``), which predicts the record the chat routes through and drifts from it:
- a raw-named ``providers.h`` record serves a ``custom:h`` session in WebUI's merge, but the selection never matched
  it, so the model block's key went to the URL instead (Codex round 12 on #8956);
- a record's key spelled ``api_key_env`` (#8938 item 2) is read by the merge for the chat, never by the selection.

For a ``custom:<name>`` session the switch now asks GET's helper (``_session_context_length_lookup_state``, #8928):
WebUI's own half of the chat's routing chain, read-only (``lookup_only``), with ``caller_key_only`` so the selection
can't add a key. The rest of the lookup (a per-model ``context_length`` under the stored spelling) still reads the
session's stored model and provider. The lookup adds no switched-off verdict of its own on this route: the chat
routes through the same merge even when the Agent's resolver raises (api/streaming.py prints and drops that error,
the switched-off ValueError included), so the merge's verdict is the chat's. Every expected pair below is the chat's
own, measured on the real streaming chain with the installed Agent. Other providers keep the previous path.

Streaming needs no change: its five lookup sites pass the chat's merged bundle (``resolved_*``, and the Agent built
from it), and the lookup returns that pair. The stream rows below pin it.
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


def _fake_metadata(calls, seen=None):
    def fake_get_model_context_length(model, base_url="", api_key="", **kwargs):
        calls.append((base_url, api_key))
        if seen is not None:
            seen.append(dict(kwargs, model=model))
        return 123_456

    mod = types.ModuleType("agent.model_metadata")
    mod.get_model_context_length = fake_get_model_context_length
    return mod


def _use(cfg, monkeypatch):
    cfg = copy.deepcopy(cfg)
    monkeypatch.setattr(config, "cfg", cfg)
    monkeypatch.setattr(config, "get_config", lambda *a, **k: cfg)
    return cfg


def _switch(cfg, monkeypatch, provider="custom:h", seen=None):
    """Run the real model-switch site and return every (URL, key) the lookup sent."""
    _use(cfg, monkeypatch)
    calls = []
    session = types.SimpleNamespace(model="m-old", model_provider=provider, last_used_model=None, gateway_routing=None,
                                    context_length=0, threshold_tokens=0, last_prompt_tokens=0)
    with patch.dict(sys.modules, {"agent.model_metadata": _fake_metadata(calls, seen)}):
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


# ── Switched off: the merge (the chat's own route) decides ───────────────────────────────────────────────────
# api/streaming.py prints and drops the Agent's resolve_runtime_provider error, its switched-off ValueError included,
# and routes the turn through WebUI's merge with an empty runtime (no key, the configured URL). So the chat refuses
# exactly when the merge does, and the lookup adds no verdict of its own (Codex round 2 on #8963).


REFUSED_SHAPES = [
    ("exact-record-disabled-beside-raw-h", _cfg({**_alias("K3", False, spelling="custom:h"), **RAW_H},
                                                 model=_model_k1())),
    ("exact-record-disabled-alone", _cfg({"custom:h": {"base_url": A, "api_key": "KX", "enabled": False}},
                                         model=_model_k1())),
    ("case-variant-disabled-alone", _cfg(_alias("K3", False), model={"provider": "custom:h", "default": "m-x",
                                                                       "base_url": A})),
]


@pytest.mark.parametrize("cfg", [s[1] for s in REFUSED_SHAPES], ids=[s[0] for s in REFUSED_SHAPES])
def test_a_route_the_merge_refuses_looks_up_without_a_key(monkeypatch, cfg):
    _use(cfg, monkeypatch)
    with pytest.raises(config.CustomProviderRouteError):
        _streaming_bundle(cfg, monkeypatch, "custom:h")
    switch, get = _switch(cfg, monkeypatch), _get(cfg, monkeypatch)
    assert [key for _, key in switch] == [""], switch
    assert [key for _, key in get] == [""], get


EXACT_OFF_BESIDE_LEGACY = [
    ("legacy-entry", [{"name": "h", "base_url": L, "api_key": "KL"}]),
    ("legacy-entry-itself-switched-off", [{"name": "h", "base_url": L, "api_key": "KL", "enabled": False}]),
]


@pytest.mark.parametrize("legacy", [s[1] for s in EXACT_OFF_BESIDE_LEGACY], ids=[s[0] for s in EXACT_OFF_BESIDE_LEGACY])
def test_an_exact_switched_off_record_beside_a_legacy_entry_keeps_the_chats_legacy_pair(monkeypatch, legacy):
    """The Agent raises (``providers.custom:h`` is off), streaming drops the error, and the merge routes through the
    legacy entry: the chat sends KL to L. exp-v0.52.660 and #8956 send that pair on GET; senior round 9's "refused"
    reading of this shape on #8956 came from a probe that counted the Agent's raise as the chat's refusal."""
    cfg = _cfg({"custom:h": {"base_url": A, "api_key": "KX", "enabled": False}}, legacy=legacy)
    route = _streaming_bundle(cfg, monkeypatch, "custom:h")
    assert (route["base_url"], route["api_key"]) == (L, "KL")
    assert _switch(cfg, monkeypatch) == _get(cfg, monkeypatch) == [(L, "KL")]


def test_the_lookup_adds_no_switched_off_verdict_on_the_chat_route(monkeypatch):
    """#8938 item 1's ``_context_lookup_provider_disabled`` is the config selection's own. The chat-route helper
    consults no switched-off verdict at all: the merge it runs is the chat's."""
    seen = []
    monkeypatch.setattr(routes, "_context_lookup_provider_disabled", lambda *a, **k: seen.append(a) or True)
    cfg = _cfg({"custom:h": {"base_url": A, "api_key": "KX", "enabled": False}}, legacy=EXACT_OFF_BESIDE_LEGACY[0][1])
    assert _get(cfg, monkeypatch) == [(L, "KL")]
    assert seen == []


def test_streaming_drops_the_agents_resolution_error_and_routes_through_the_merge():
    """Source pin for the premise above. If api/streaming.py starts stopping the turn when the Agent's
    resolve_runtime_provider raises, the chat refuses these shapes and this file's expectations must change."""
    import pathlib

    src = (pathlib.Path(routes.__file__).parent / "streaming.py").read_text()
    # The turn's own resolution (the other call is the credential self-heal, which re-resolves on a 401).
    warn = src.index('print(f"[webui] WARNING: resolve_runtime_provider failed: {_e}", flush=True)')
    head = src.rindex("_rt = resolve_runtime_provider_with_anthropic_env_lock(", 0, warn)
    assert "_rt = {}" in src[head - 1500:head]
    assert "except Exception as _e:" in src[head:warn]
    tail = src[warn:warn + 4000]
    assert "_runtime_bundle = _resolve_runtime_connection_bundle(" in tail
    assert "raise_for_custom_provider_route(_runtime_bundle)" in tail
    assert "raise" not in src[warn:warn + 200].split("\n", 1)[1].split("\n", 1)[0]


def _streaming_bundle(cfg, monkeypatch, provider):
    """What api/streaming.py builds when the Agent's resolution raised: the merge with an empty runtime, no key and
    the configured URL, then the route verdict."""
    _use(cfg, monkeypatch)
    _m, pid, configured = config.resolve_model_provider(config.model_with_provider_context("m-x", provider))
    from api import streaming

    route = streaming._resolve_runtime_connection_bundle(pid, None, configured, {}, custom_provider_lookup=pid)
    config.raise_for_custom_provider_route(route)
    return route


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


# ── Codex round 1 on #8963 ────────────────────────────────────────────────────────────────────────────────────
# F2: a switched-off alias or named record with no exact record, beside a legacy custom_providers entry. The merge
# routes through the legacy entry and the chat sends its key; exp-v0.52.660 and #8956 send that pair on GET (and on
# the switch for the entries at A). The first build of this fix refused here (WebUI's native verdict).

LEGACY_L = [{"name": "h", "base_url": L, "api_key": "KL"}]
LEGACY_A = [{"name": "h", "base_url": A, "api_key": "KL"}]

LEGACY_SHAPES = [
    ("case-variant-off-beside-legacy", _cfg(_alias("K3", False), legacy=LEGACY_L), (L, "KL")),
    ("space-variant-off-beside-legacy", _cfg(_alias("K3", False, spelling=" custom:h "), legacy=LEGACY_L), (L, "KL")),
    ("case-variant-off-beside-legacy-and-model-key", _cfg(_alias("K3", False), model=_model_k1(), legacy=LEGACY_L),
     (L, "KL")),
    ("case-variant-off-at-another-host-beside-legacy-at-the-model-url",
     _cfg({"Custom:H": {"base_url": B, "api_key": "K3", "enabled": False}},
          model={"provider": "custom:h", "default": "m-x", "base_url": A}, legacy=LEGACY_A), (A, "KL")),
    ("case-variant-off-beside-legacy-same-url", _cfg(_alias("K3", False), legacy=LEGACY_A), (A, "KL")),
    ("case-variant-off-beside-legacy-same-url-and-model-key", _cfg(_alias("K3", False), model=_model_k1(),
                                                                    legacy=LEGACY_A), (A, "KL")),
    ("named-record-off-beside-legacy", _cfg({"one": {"name": "h", "base_url": A, "api_key": "KO", "enabled": False}},
                                            legacy=LEGACY_A), (A, "KL")),
    # Senior round 1's variant: the raw-named record switched off.
    ("raw-named-off-beside-legacy", _cfg({"h": {"base_url": B, "api_key": "K2", "enabled": False}}, legacy=LEGACY_L),
     (L, "KL")),
]


@pytest.mark.parametrize("cfg,chat_pair", [s[1:] for s in LEGACY_SHAPES], ids=[s[0] for s in LEGACY_SHAPES])
def test_a_switched_off_spelling_beside_a_legacy_entry_keeps_the_chats_pair(monkeypatch, cfg, chat_pair):
    assert _switch(cfg, monkeypatch) == _get(cfg, monkeypatch) == [chat_pair]


@pytest.mark.parametrize("cfg,chat_pair", [s[1:] for s in LEGACY_SHAPES], ids=[s[0] for s in LEGACY_SHAPES])
def test_the_legacy_shapes_expected_pair_is_the_merges(monkeypatch, cfg, chat_pair):
    _use(cfg, monkeypatch)
    route = config.merge_custom_provider_runtime_bundle(
        "custom:h", None, None, {}, lookup_provider="custom:h",
        connection_resolver=config.resolve_custom_provider_connection,
    )
    config.raise_for_custom_provider_route(route)
    assert (route["base_url"], route["api_key"]) == chat_pair


def test_a_switched_off_spelling_alone_is_still_refused(monkeypatch):
    """Without the legacy entry the merge has no source and refuses, and so does the chat (AuthError)."""
    cfg = _cfg(_alias("K3", False), model={"provider": "custom:h", "default": "m-x", "base_url": A})
    assert [key for _, key in _switch(cfg, monkeypatch)] == [""]
    assert [key for _, key in _get(cfg, monkeypatch)] == [""]


# F1: a raw-named record's per-model context_length. The switch still reads the stored spelling (providers.mylab),
# as #8956 did; only the URL and key come from the chat route. (GET and streaming ask under the resolved
# custom:mylab and miss it, as on exp-v0.52.660: a known residual, not changed here.)


@pytest.mark.parametrize("record,chat_pair", [
    ({"base_url": A, "api_key": "KR"}, (A, "KR")),
    ({"base_url": A, "api_key_env": VAR}, (A, "KV")),
], ids=["literal", "api-key-env"])
def test_a_raw_named_records_model_window_still_reaches_the_switch(monkeypatch, record, chat_pair):
    monkeypatch.setenv(VAR, "KV")
    cfg = _cfg({"mylab": {**record, "models": {"m-x": {"context_length": 16384}}}},
               model={"provider": "mylab", "default": "m-x"})
    seen = []
    assert _switch(cfg, monkeypatch, provider="mylab", seen=seen) == [chat_pair]
    assert [s.get("config_context_length") for s in seen] == [16384]
    assert [s.get("provider") for s in seen] == ["mylab"]


def test_the_switch_passes_the_stored_pair_and_the_requested_id(monkeypatch):
    seen = {}
    real = routes._resolve_context_length_for_session_model

    def spy(*a, **k):
        seen.update(k, args=a)
        return real(*a, **k)

    monkeypatch.setattr(routes, "_resolve_context_length_for_session_model", spy)
    monkeypatch.setenv(VAR, "KV")
    _switch(_cfg({"mylab": {"base_url": A, "api_key_env": VAR}}, model={"provider": "mylab", "default": "m-x"}),
            monkeypatch, provider="mylab")
    assert seen["args"] == ("m-x", "mylab")
    assert seen["requested_provider"] == "custom:mylab"
    assert (seen["base_url"], seen["api_key"], seen["caller_key_only"]) == (A, "KV", True)


# ── Streaming: no change needed. Its lookups pass the chat's merged bundle and send that pair ────────────────
# api/streaming.py sets resolved_* from the merged bundle (the chat's pair, provider "custom" for a named custom
# provider) and builds the Agent from it. Two lookup forms read it: the Agent-fallback sites
# (_context_lookup_agent_fallback) and the three caller sites that pass the Agent's own URL and key.

STREAM_SHAPES = [s for s in CHAT_ROUTE_SHAPES if not s[0].startswith(("r12-enabled", "item2"))] + [
    ("raw-named-literal", _cfg({"mylab": {"base_url": A, "api_key": "KR"}},
                               model={"provider": "mylab", "default": "m-x", "base_url": A, "api_key": "KM"}),
     (A, "KR")),
]


def _bundle(cfg, monkeypatch, provider):
    _use(cfg, monkeypatch)
    route = config.merge_custom_provider_runtime_bundle(
        provider, None, None, {}, lookup_provider=provider,
        connection_resolver=config.resolve_custom_provider_connection,
    )
    config.raise_for_custom_provider_route(route)
    return route


def _stream_lookups(cfg, bundle_provider, url, key, model="m-x"):
    agent = types.SimpleNamespace(api_key=key, base_url=url, provider=bundle_provider, model=model)
    fallback = routes._context_length_lookup_inputs_for_model(
        model, bundle_provider, base_url=getattr(agent, "base_url", "") or url or "", cfg=cfg,
        **routes._context_lookup_agent_fallback(agent, key, url, bundle_provider))
    caller = routes._context_length_lookup_inputs_for_model(
        model, bundle_provider, base_url=getattr(agent, "base_url", "") or url or "",
        api_key=getattr(agent, "api_key", "") or key or "", cfg=cfg)
    return [(fallback.base_url, fallback.api_key), (caller.base_url, caller.api_key)]


@pytest.mark.parametrize("cfg,chat_pair", [s[1:] for s in STREAM_SHAPES], ids=[s[0] for s in STREAM_SHAPES])
def test_streaming_sends_the_chats_pair(monkeypatch, cfg, chat_pair):
    provider = "custom:mylab" if "mylab" in cfg["providers"] else "custom:h"
    route = _bundle(cfg, monkeypatch, provider)
    assert route["provider"] == "custom"
    assert (route["base_url"], route["api_key"]) == chat_pair
    assert _stream_lookups(cfg, route["provider"], route["base_url"], route["api_key"]) == [chat_pair, chat_pair]


def test_streaming_an_ollama_session_sends_the_chats_pair(monkeypatch):
    """r10 on #8956: the chat's bundle (measured with the Agent) is provider ollama, the model block's KM at A; a
    generic providers.custom record's api_key_env key never replaces it."""
    monkeypatch.setenv(VAR, "KV")
    cfg = _cfg({"custom": {"base_url": B, "api_key_env": VAR}},
               model={"provider": "ollama", "default": "m-x", "base_url": A, "api_key": "KM"})
    _use(cfg, monkeypatch)
    assert _stream_lookups(cfg, "ollama", A, "KM") == [(A, "KM"), (A, "KM")]


def test_the_streaming_sites_read_the_merged_bundle():
    """Source pin: the rows above model the five lookup sites as they are. If a site starts passing something else
    (the Agent's raw runtime pair, a re-derived record), these tests no longer prove streaming needs no change."""
    import pathlib
    import re

    src = (pathlib.Path(routes.__file__).parent / "streaming.py").read_text()
    assert len(re.findall(r"resolved_api_key = _runtime_bundle\['api_key'\]", src)) == 3
    assert len(re.findall(r"resolved_base_url = _runtime_bundle\['base_url'\]", src)) == 3
    assert len(re.findall(r"_context_lookup_agent_fallback\(\s*agent, resolved_api_key, resolved_base_url, "
                          r"resolved_provider", src)) == 2
    callers = []
    for match in re.finditer(r"= _cli_(?:u|cc|sse)\(\n", src):
        call = src[match.end():src.index("cfg=", match.end())]
        found = re.search(r"api_key=([^\n]+)\n", call)
        assert found, call
        callers.append(found.group(1).strip())
    assert sorted(callers) == sorted([
        "_key_u,",
        "getattr(agent, 'api_key', '') or resolved_api_key or '',",
        "getattr(agent, 'api_key', '') or resolved_api_key or '',",
    ])
    # _cli_u's _key_u is the live Agent's own key (getattr(_agent, 'api_key')), built from the same bundle.
    assert "_key_u = getattr(_agent, 'api_key', '') or ''" in src
