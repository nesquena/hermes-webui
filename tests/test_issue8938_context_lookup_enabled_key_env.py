"""#8938: the context-length lookup applies the ``enabled`` rule and reads a record's ``api_key_env``, as the chat does.

Three follow-ups from the review of #8935, measured against the chat's own route on every supported Agent:

1. **A record the user switched off** (``providers.<id>.enabled: false``, or ``off`` / ``no``) is refused by the Agent
   for a session on it, custom or built-in. So it is no key source for its own session, and no same-value holder
   for another session (#8918). A legacy ``custom_providers`` entry is different: the Agent's legacy view still
   routes one that's switched off, and the chat still sends its key, so it stays a source and a holder.
2. **A record's key reference spelled ``api_key_env``** is read where the chat reads it: on the records WebUI's own
   merge serves for a named ``custom:<name>`` route, which respells it to ``key_env`` (a ``custom:`` key, the generic
   ``custom`` record, a keyed record whose key is no built-in provider). Never on a built-in provider's record: the
   chat gets an AuthError there and sends no key. On a legacy entry or the model block the lookup reads ``key_env``
   alone, as the merge does (#8959).
3. **Pins** for three rules that mutants showed no test held: a legacy holder's ``api`` / ``url`` spellings, a
   model-block ``key_env`` holder, and no same-value grant on the Agent-fallback branch.
"""
import pytest

from api import routes

A = "https://a.model-url.test/v1"
B = "https://b.record-url.test/v1"
L = "https://l.legacy-url.test/v1"
U = "https://u.shared-url.test/v1"
VAR = "WEBUI_TEST_8938_KEY"
UNSET = "WEBUI_TEST_8938_UNSET"


# The chat resolves the session's provider against the live config (api.config.cfg), so the lookup's requested id
# does too: each test's config is installed as the live one.
LIVE = {}


@pytest.fixture(autouse=True)
def live_config(monkeypatch):
    from api import config as api_config

    LIVE.clear()
    monkeypatch.setattr(api_config, "cfg", LIVE)
    monkeypatch.setattr(api_config, "get_config", lambda *a, **k: LIVE)


def _lookup(cfg, provider="custom:my-proxy", **kw):
    cfg.setdefault("providers", {})
    cfg.setdefault("custom_providers", [])
    LIVE.clear()
    LIVE.update(cfg)
    return routes._context_length_lookup_inputs_for_model("m-x", provider, cfg=cfg, **kw)


@pytest.fixture
def key_in_env(monkeypatch):
    monkeypatch.setenv(VAR, "K1")
    monkeypatch.delenv(UNSET, raising=False)


# The installed Agent's provider aliases, pinned: an agent-free run (CI) resolves none, so ``ollama`` would stay
# ``ollama`` and the requested-vs-resolved cases below would test nothing.
ALIASES = {"ollama": "custom", "vllm": "custom", "claude": "anthropic", "grok": "xai", "glm": "zai",
           "moonshot": "kimi-coding"}


@pytest.fixture
def aliases(monkeypatch):
    from api import config as api_config

    real = getattr(api_config, "_resolve_provider_alias", None)

    def resolve(value):
        key = str(value or "").strip().lower()
        if key in ALIASES:
            return ALIASES[key]
        return real(value) if real else value

    monkeypatch.setattr(api_config, "_resolve_provider_alias", resolve)


# ── 1. A record the user switched off is no key source for its own session ────


@pytest.mark.parametrize("flag", [False, "off", "no", "false", 0])
def test_a_disabled_record_for_the_session_provider_gives_no_key(flag):
    got = _lookup({"model": {"provider": "custom:my-proxy"},
                   "providers": {"custom:my-proxy": {"base_url": B, "api_key": "K1", "enabled": flag}}})
    assert (got.base_url, got.api_key) == (B, "")


@pytest.mark.parametrize("flag", [True, "on", None])
def test_an_enabled_record_for_the_session_provider_keeps_its_key(flag):
    record = {"base_url": B, "api_key": "K1"}
    if flag is not None:
        record["enabled"] = flag
    got = _lookup({"model": {"provider": "custom:my-proxy"}, "providers": {"custom:my-proxy": record}})
    assert (got.base_url, got.api_key) == (B, "K1")


def test_a_disabled_record_does_not_fall_through_to_the_model_block_key():
    # The Agent refuses the session, so the chat sends neither key. The model block's key at B would be a pair no
    # baseline sends.
    got = _lookup({"model": {"provider": "custom:my-proxy", "base_url": B, "api_key": "KM"},
                   "providers": {"custom:my-proxy": {"base_url": B, "api_key": "K1", "enabled": False}}})
    assert (got.base_url, got.api_key) == (B, "")


@pytest.mark.parametrize("disabled_first", [True, False])
def test_a_disabled_record_does_not_fall_through_to_another_record_for_the_provider(disabled_first):
    # Two spellings of one provider's record. The disabled one is the exact key the Agent checks, so it refuses the
    # provider whichever comes first in the file.
    off = ("custom:my-proxy", {"base_url": B, "enabled": False})
    on = ("Custom:My-Proxy", {"base_url": B, "api_key": "K2"})
    records = dict([off, on] if disabled_first else [on, off])
    got = _lookup({"model": {"provider": "custom:my-proxy"}, "providers": records})
    assert got.api_key == ""


@pytest.mark.parametrize("canonical_first", [True, False])
def test_a_disabled_alias_beside_the_enabled_record_keeps_the_records_key(canonical_first):
    # The Agent's exact key finds the enabled record and its matcher skips the switched-off spelling, so the chat
    # keeps KR. Only the authoritative record's verdict switches the provider off.
    on = ("custom:my-proxy", {"base_url": B, "api_key": "KR"})
    off = ("Custom:My-Proxy", {"base_url": B, "api_key": "K2", "enabled": False})
    got = _lookup({"model": {"provider": "custom:my-proxy"},
                   "providers": dict([on, off] if canonical_first else [off, on])})
    assert (got.base_url, got.api_key) == (B, "KR")


def test_a_disabled_record_named_for_the_identity_under_another_key_switches_it_off():
    # WebUI's route finds the record by its name and has no authority left; the chat refuses. Exp-v0.52.659 sent the
    # model block's KM to A here.
    got = _lookup({"model": {"provider": "custom:my-proxy", "base_url": A, "api_key": "KM"},
                   "providers": {"one": {"name": "my-proxy", "base_url": B, "api_key": "K1", "enabled": False}}})
    assert (got.base_url, got.api_key) == (A, "")


def test_a_disabled_alias_alone_switches_the_provider_off():
    # No enabled record names the identity, so WebUI's route has no authority and the Agent finds no source.
    got = _lookup({"model": {"provider": "custom:my-proxy", "base_url": A, "api_key": "KM"},
                   "providers": {"Custom:My-Proxy": {"base_url": B, "api_key": "K2", "enabled": False}}})
    assert got.api_key == ""


@pytest.mark.parametrize("canonical_first", [True, False])
def test_a_disabled_builtin_alias_beside_the_enabled_record_keeps_the_records_key(canonical_first):
    on = ("openai", {"api_key": "KB"})
    off = ("OpenAI", {"api_key": "K2", "enabled": False})
    got = _lookup({"model": {"provider": "openai"}, "providers": dict([on, off] if canonical_first else [off, on])},
                  provider="openai")
    assert got.api_key == "KB"


@pytest.mark.parametrize("flag", [False, "off", "no"])
@pytest.mark.parametrize("canonical_first", [True, False])
def test_a_disabled_builtin_record_refuses_even_beside_an_enabled_alias(canonical_first, flag):
    off = ("openai", {"api_key": "KB", "enabled": flag})
    on = ("OpenAI", {"api_key": "K2"})
    got = _lookup({"model": {"provider": "openai"}, "providers": dict([off, on] if canonical_first else [on, off])},
                  provider="openai")
    assert got.api_key == ""


def test_a_disabled_builtin_alias_alone_is_skipped_like_a_missing_record():
    # The Agent's exact key misses, so the provider isn't switched off; the alias is invisible, and the selection
    # reads on as with no record: the model block configured for openai.
    got = _lookup({"model": {"provider": "openai", "api_key": "KM"},
                   "providers": {"OpenAI": {"api_key": "K2", "enabled": False}}}, provider="openai")
    assert got.api_key == "KM"


# Codex round 3: a disabled spelling that isn't the exact key never drops the model block's working pair. The chat
# sends it (measured over authenticated HTTP), with or without the switched-off record.
@pytest.mark.parametrize("provider,spelling", [("custom", "Custom"), ("openai", "OpenAI"), ("ollama", "Ollama"),
                                               ("openrouter", "OpenRouter")])
@pytest.mark.parametrize("key_field", ["api_key", "key_env"])
def test_a_disabled_spelling_never_drops_the_model_blocks_pair(provider, spelling, key_field, key_in_env):
    model = {"provider": provider, "base_url": A, key_field: "KM" if key_field == "api_key" else VAR}
    want = "KM" if key_field == "api_key" else "K1"
    alone = _lookup({"model": dict(model)}, provider=provider)
    beside = _lookup({"model": dict(model), "providers": {spelling: {"base_url": B, "api_key": "K2", "enabled": False}}},
                     provider=provider)
    assert (alone.base_url, alone.api_key) == (A, want)
    assert (beside.base_url, beside.api_key) == (A, want)


# The Agent checks providers.<the id it was asked for>, before alias resolution: an ollama session is refused by
# providers.ollama, not providers.custom; a claude session by providers.claude, not providers.anthropic.
@pytest.mark.parametrize("provider", ["ollama", "vllm", "claude", "grok", "glm", "moonshot"])
def test_the_requested_ids_disabled_record_switches_an_aliased_provider_off(provider, aliases):
    assert routes._canonical_context_provider(provider) == ALIASES[provider]
    got = _lookup({"model": {"provider": provider, "base_url": A, "api_key": "KM"},
                   "providers": {provider: {"enabled": False}}}, provider=provider)
    assert (got.base_url, got.api_key) == (A, "")


@pytest.mark.parametrize("provider", ["ollama", "vllm"])
def test_a_disabled_record_under_the_resolved_id_leaves_an_aliased_provider_on(provider, aliases):
    # providers.custom switched off doesn't refuse an ollama session; the chat sends the model block's KM to A.
    assert routes._canonical_context_provider(provider) == "custom"
    got = _lookup({"model": {"provider": provider, "base_url": A, "api_key": "KM"},
                   "providers": {"custom": {"enabled": False}}}, provider=provider)
    assert (got.base_url, got.api_key) == (A, "KM")


def test_the_requested_id_comes_from_the_model_block_when_the_session_names_none(aliases):
    got = _lookup({"model": {"provider": "ollama", "base_url": A, "api_key": "KM"},
                   "providers": {"ollama": {"enabled": False}}}, provider=None)
    assert got.api_key == ""


# Codex round 4: the chat rewrites a legacy configured ``local`` to ``custom`` before asking the Agent, so for a model
# change on a ``local`` session (the stored provider is passed raw) the Agent checks providers.custom, not
# providers.local. Measured: providers.local off -> the chat sends K@U; providers.custom off -> it refuses.
@pytest.mark.parametrize("off,want", [("local", "KM"), ("custom", "")])
def test_a_legacy_local_session_is_checked_under_custom(off, want):
    model = {"provider": "local", "base_url": A, "api_key": "KM"}
    cfg = {"model": dict(model), "providers": {off: {"enabled": False}}}
    # Model switch: the stored provider is raw, so the switch passes the chat's resolution of it.
    LIVE.clear()
    LIVE.update(cfg)
    requested = routes._context_lookup_requested_provider("m-x", "local")
    assert requested == "custom"
    got = _lookup(cfg, provider="local", requested_provider=requested)
    assert (got.base_url, got.api_key) == (A, want)
    # GET and the streaming sites pass the resolved provider, which is the chat's id as is.
    got = _lookup({"model": dict(model), "providers": {off: {"enabled": False}}}, provider="custom")
    assert (got.base_url, got.api_key) == (A, want)


@pytest.mark.parametrize("off,want", [("local", "KM"), ("custom", "")])
def test_the_model_switch_passes_the_chats_requested_id(monkeypatch, off, want):
    from types import SimpleNamespace

    LIVE.update({"model": {"provider": "local", "base_url": A, "api_key": "KM"}, "providers": {off: {"enabled": False}},
                 "custom_providers": []})
    seen = {}

    def fake(model, provider=None, **kw):
        seen.update(kw, model=model, provider=provider)
        return routes._context_length_lookup_inputs_for_model(model, provider, cfg=LIVE, **kw).api_key == "KM" and 1 or 0

    monkeypatch.setattr(routes, "_resolve_context_length_for_session_model", fake)
    session = SimpleNamespace(model="old-m", model_provider="local", last_used_model=None, gateway_routing=None,
                              context_length=0, threshold_tokens=0, last_prompt_tokens=0)
    assert routes._invalidate_session_route_state_if_changed(session, model="m-x", provider="local", already_resolved=True)
    assert seen["requested_provider"] == "custom"
    assert session.context_length == (1 if want == "KM" else 0)


def test_the_session_resolver_forwards_the_requested_id(monkeypatch):
    import sys
    from types import ModuleType

    meta = ModuleType("agent.model_metadata")
    meta.get_model_context_length = lambda *a, **k: 4242
    monkeypatch.setitem(sys.modules, "agent.model_metadata", meta)
    seen = {}
    real = routes._context_length_lookup_inputs_for_model

    def spy(*a, **kw):
        seen.update(kw)
        return real(*a, **kw)

    monkeypatch.setattr(routes, "_context_length_lookup_inputs_for_model", spy)
    assert routes._resolve_context_length_for_session_model("m-x", "local", requested_provider="custom") == 4242
    assert seen["requested_provider"] == "custom"


# Codex round 5: resolution is not idempotent, so a caller that already resolved the session must not be resolved again.
# GET resolves model.default "openai/m-x" to ("m-x", "openai"); resolving the bare "m-x" again would match the disabled
# anthropic record's model list and drop OpenAI's working key (authenticated /api/session: 65,536 -> 256,000).
def test_a_resolved_caller_is_not_resolved_again(key_in_env):
    cfg = {"model": {"provider": "openai", "default": "openai/m-x", "base_url": A, "key_env": VAR},
           "providers": {"anthropic": {"models": ["m-x"], "enabled": False}}}
    got = _lookup(cfg, provider="openai")
    assert (got.base_url, got.api_key) == (A, "K1")


def test_the_requested_id_is_the_chats():
    LIVE.update({"model": {"provider": "openrouter"}, "providers": {}, "custom_providers": []})
    for raw, want in (("ollama", "ollama"), ("claude", "claude"), ("custom", "custom"), ("custom:my-proxy", "custom:my-proxy"),
                      ("OpenAI", "openai")):
        assert routes._context_lookup_requested_provider("m-x", raw) == want, raw
    assert routes._context_lookup_requested_provider("@ollama:m-x", None) == "ollama"
    # A configured provider keeps its spelling through the chat's resolver; the Agent lowercases it.
    LIVE["model"] = {"provider": "OpenAI"}
    assert routes._context_lookup_requested_provider("m-x", "OpenAI") == "openai"


def test_the_config_selection_alone_reads_the_requested_id(aliases):
    cfg = {"model": {"provider": "ollama", "base_url": A, "api_key": "KM"}, "providers": {"ollama": {"enabled": False}}}
    assert routes._context_length_config_api_key_and_owner("ollama", cfg)[0] == ""
    assert routes._context_length_config_api_key_and_owner("custom", cfg, requested_provider="ollama")[0] == ""
    cfg["providers"] = {"custom": {"enabled": False}}
    assert routes._context_length_config_api_key_and_owner("custom", cfg, requested_provider="ollama")[0] == "KM"


def test_a_disabled_builtin_spelling_beside_the_enabled_record_with_a_url_keeps_its_pair():
    # The chat sends KX to A here (the enabled record's own URL and key). Only this dict order: the lookup's URL comes
    # from the FIRST matching record, enabled or not, so with the switched-off spelling first it asks the provider's
    # default host and the key, owned only at A, is dropped there. That URL rule predates this PR (exp-v0.52.659 gives
    # the same) and this PR changes keys only, never URLs.
    got = _lookup({"model": {"provider": "openai"},
                   "providers": {"openai": {"base_url": A, "api_key": "KX"}, "OpenAI": {"enabled": False}}},
                  provider="openai")
    assert (got.base_url, got.api_key) == (A, "KX")


@pytest.mark.parametrize("flag", [False, "off", "no", 0])
def test_a_disabled_builtin_record_gives_no_key(flag):
    # The Agent refuses the provider, so not even the model block's key goes.
    got = _lookup({"model": {"provider": "openai", "api_key": "KM"},
                   "providers": {"openai": {"api_key": "K1", "enabled": flag}}}, provider="openai")
    assert got.api_key == ""


def test_an_ambiguous_identity_is_left_as_before():
    # Two records name my-proxy. The chat refuses this shape: the real resolve_model_provider raises
    # AmbiguousCustomProviderError. The selection here reads on as on exp-v0.52.659 (the model block's key at its own
    # URL); ambiguity isn't a switch-off, so behaviour is unchanged.
    from api import config as api_config

    got = _lookup({"model": {"provider": "custom:my-proxy", "base_url": A, "api_key": "KM"},
                   "providers": {"one": {"name": "my-proxy", "base_url": B, "api_key": "K1"},
                                 "two": {"name": "my-proxy", "base_url": L, "api_key": "K2"}}})
    assert (got.base_url, got.api_key) == (A, "KM")
    with pytest.raises(api_config.AmbiguousCustomProviderError):
        api_config.resolve_model_provider(api_config.model_with_provider_context("m-x", "custom:my-proxy"))


def test_an_enabled_builtin_record_keeps_its_key():
    got = _lookup({"model": {"provider": "openai"}, "providers": {"openai": {"api_key": "K1"}}}, provider="openai")
    assert got.api_key == "K1"


def test_a_disabled_record_for_another_provider_does_not_touch_this_session():
    got = _lookup({"model": {"provider": "custom:my-proxy"},
                   "providers": {"custom:my-proxy": {"base_url": B, "api_key": "K1"},
                                 "custom:other": {"base_url": B, "api_key": "K2", "enabled": False}}})
    assert (got.base_url, got.api_key) == (B, "K1")


def test_a_disabled_legacy_entry_is_still_a_key_source():
    # The Agent's legacy view ignores ``enabled`` on a custom_providers entry, and the chat sends KL to L.
    got = _lookup({"model": {"provider": "custom:my-proxy"},
                   "custom_providers": [{"name": "my-proxy", "base_url": L, "api_key": "KL", "enabled": False}]})
    assert (got.base_url, got.api_key) == (L, "KL")


# ── 1b. ... and no same-value holder for another session ─────────────────────


@pytest.mark.parametrize("flag", [False, "off", "no"])
def test_a_disabled_record_holding_the_value_lends_nothing(flag):
    cfg = {"model": {"provider": "custom:s", "base_url": B},
           "providers": {"custom:old": {"base_url": B, "api_key": "K1", "enabled": flag}}}
    assert routes._context_lookup_origin(B) not in routes._context_lookup_same_value_origins("K1", cfg)
    got = _lookup(cfg, provider="custom:s", api_key="K1")
    assert (got.base_url, got.api_key) == (B, "")


@pytest.mark.parametrize("flag", [True, "on", None])
def test_an_enabled_record_holding_the_value_still_lends_its_url(flag):
    record = {"base_url": B, "api_key": "K1"}
    if flag is not None:
        record["enabled"] = flag
    got = _lookup({"model": {"provider": "custom:s", "base_url": B}, "providers": {"custom:old": record}},
                  provider="custom:s", api_key="K1")
    assert (got.base_url, got.api_key) == (B, "K1")


@pytest.mark.parametrize("flag", [False, "off"])
def test_a_disabled_legacy_entry_holding_the_value_still_lends_its_url(flag):
    # A session on custom:old still routes through this entry and sends K1 to L, so the value is already there.
    cfg = {"model": {"provider": "custom:s", "base_url": L},
           "custom_providers": [{"name": "old", "base_url": L, "api_key": "K1", "enabled": flag}]}
    assert routes._context_lookup_origin(L) in routes._context_lookup_same_value_origins("K1", cfg)
    got = _lookup(cfg, provider="custom:s", api_key="K1")
    assert (got.base_url, got.api_key) == (L, "K1")


# ── 2. A record's key reference spelled ``api_key_env`` ──────────────────────


def test_a_record_key_spelled_api_key_env_is_its_own_key(key_in_env):
    got = _lookup({"model": {"provider": "custom:h"},
                   "providers": {"custom:h": {"base_url": B, "api_key_env": VAR}}}, provider="custom:h")
    assert (got.base_url, got.api_key) == (B, "K1")


@pytest.mark.parametrize("pid", ["openai", "anthropic", "deepseek", "openrouter"])
@pytest.mark.parametrize("with_url", [False, True], ids=["no-url", "record-url"])
def test_a_builtin_record_key_spelled_api_key_env_is_not_sent(key_in_env, pid, with_url):
    # The chat gets an AuthError and sends no key: the Agent reads a built-in record's ``api_key_env`` only for its
    # model listing and URL blocks, never for the chat. So the lookup sends none either, as on exp-v0.52.659.
    record = {"api_key_env": VAR, **({"base_url": B} if with_url else {})}
    got = _lookup({"model": {"provider": pid}, "providers": {pid: record}}, provider=pid)
    assert got.api_key == ""


@pytest.mark.parametrize("pid", ["openai", "anthropic"])
def test_a_builtin_record_holding_the_value_under_api_key_env_lends_nothing(key_in_env, pid):
    cfg = {"model": {"provider": "custom:s", "base_url": U},
           "providers": {"custom:s": {"base_url": U}, pid: {"base_url": U, "api_key_env": VAR}}}
    assert routes._context_lookup_origin(U) not in routes._context_lookup_same_value_origins("K1", cfg)
    got = _lookup(cfg, provider="custom:s", api_key="K1")
    assert (got.base_url, got.api_key) == (U, "")


def test_the_generic_custom_record_key_spelled_api_key_env_is_its_own_key(key_in_env):
    got = _lookup({"model": {"provider": "custom"}, "providers": {"custom": {"base_url": B, "api_key_env": VAR}}},
                  provider="custom")
    assert got.api_key == "K1"


def test_a_keyed_custom_record_holding_the_value_under_api_key_env_lends_its_url(key_in_env):
    # ``providers.myproxy`` is no built-in: the merge serves it for custom:myproxy and respells its api_key_env.
    cfg = {"model": {"provider": "custom:s", "base_url": U},
           "providers": {"custom:s": {"base_url": U}, "myproxy": {"base_url": U, "api_key_env": VAR}}}
    assert routes._context_lookup_origin(U) in routes._context_lookup_same_value_origins("K1", cfg)
    got = _lookup(cfg, provider="custom:s", api_key="K1")
    assert (got.base_url, got.api_key) == (U, "K1")


@pytest.mark.parametrize("key,reads", [
    ("custom:h", True), ("Custom:H", True), ("custom", True), ("myproxy", True), ("my_proxy", True),
    ("openai", False), ("anthropic", False), ("deepseek", False), ("openrouter", False), ("OpenAI", False), ("", False),
    (None, False),
])
def test_which_records_the_chat_reads_api_key_env_on(key, reads):
    assert routes._context_lookup_record_reads_api_key_env(key) is reads


def test_a_record_holding_the_value_under_api_key_env_lends_its_url(key_in_env):
    # The issue's repro: a caller K1 with no URL, the lookup URL U, and a record at U whose api_key_env names K1.
    cfg = {"model": {"provider": "custom:s", "base_url": U},
           "providers": {"custom:s": {"base_url": U}, "custom:h": {"base_url": U, "api_key_env": VAR}}}
    assert routes._context_lookup_origin(U) in routes._context_lookup_same_value_origins("K1", cfg)
    got = _lookup(cfg, provider="custom:s", api_key="K1")
    assert (got.base_url, got.api_key) == (U, "K1")


def test_a_blank_key_env_falls_back_to_api_key_env(key_in_env):
    got = _lookup({"model": {"provider": "custom:h"},
                   "providers": {"custom:h": {"base_url": B, "key_env": "  ", "api_key_env": VAR}}}, provider="custom:h")
    assert got.api_key == "K1"


def test_key_env_outranks_api_key_env(key_in_env):
    # WebUI's ladder keeps a non-blank key_env; one naming an unset variable gives no key.
    got = _lookup({"model": {"provider": "custom:h"},
                   "providers": {"custom:h": {"base_url": B, "key_env": UNSET, "api_key_env": VAR}}}, provider="custom:h")
    assert got.api_key == ""


def test_a_literal_api_key_outranks_api_key_env(key_in_env):
    got = _lookup({"model": {"provider": "custom:h"},
                   "providers": {"custom:h": {"base_url": B, "api_key": "KLIT", "api_key_env": VAR}}}, provider="custom:h")
    assert got.api_key == "KLIT"


def test_a_legacy_entry_api_key_env_is_not_read(key_in_env):
    # The chat sends the keyless placeholder for this entry: the spelling isn't read on a legacy entry.
    cfg = {"model": {"provider": "custom:h"}, "custom_providers": [{"name": "h", "base_url": L, "api_key_env": VAR}]}
    assert routes._context_lookup_origin(L) not in routes._context_lookup_same_value_origins("K1", cfg)
    got = _lookup(cfg, provider="custom:h")
    assert (got.base_url, got.api_key) == (L, "")


def test_a_model_block_api_key_env_is_not_read(key_in_env):
    cfg = {"model": {"provider": "custom:my-proxy", "base_url": A, "api_key_env": VAR}}
    assert routes._context_lookup_origin(A) not in routes._context_lookup_same_value_origins("K1", cfg)
    got = _lookup(cfg)
    assert (got.base_url, got.api_key) == (A, "")


# ── 3. Rules no test held ────────────────────────────────────────────────────


@pytest.mark.parametrize("spelling", ["api", "url"])
def test_a_legacy_holder_lends_its_url_in_every_spelling_the_agent_reads(spelling):
    got = _lookup({"model": {"provider": "custom:s", "base_url": L},
                   "custom_providers": [{"name": "other", spelling: L, "api_key": "K1"}]},
                  provider="custom:s", api_key="K1")
    assert (got.base_url, got.api_key) == (L, "K1")


def test_a_model_block_key_env_holder_matches(key_in_env):
    got = _lookup({"model": {"provider": "custom:my-proxy", "base_url": A, "key_env": VAR}}, api_key="K1")
    assert (got.base_url, got.api_key) == (A, "K1")


def test_the_same_value_grant_does_not_extend_the_fallback_key():
    # The Agent's fallback key came with C, so it owns C only. A record holding the same value declares B, the lookup's
    # URL, but the fallback branch keeps the caller-key rule: no key at B.
    C = "https://c.agent-url.test/v1"
    got = _lookup({"model": {"provider": "custom:s", "base_url": B},
                   "providers": {"custom:h": {"base_url": B, "api_key": "AGENT-KEY"}}},
                  provider="custom:s", fallback_api_key="AGENT-KEY", fallback_base_url=C)
    assert (got.base_url, got.api_key) == (B, "")
