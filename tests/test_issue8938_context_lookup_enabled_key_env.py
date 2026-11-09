"""#8938: the context-length lookup applies the ``enabled`` rule and reads a record's ``api_key_env``, as the chat does.

Three follow-ups from the review of #8935, measured against the chat's own route on every supported Agent:

1. **A record the user switched off** (``providers.<id>.enabled: false``, or ``off`` / ``no``) is refused by the Agent
   for a session on it, custom or built-in. So it is no key source for its own session, and no same-value holder
   for another session (#8918). A legacy ``custom_providers`` entry is different: the Agent's legacy view still
   routes one that's switched off, and the chat still sends its key, so it stays a source and a holder.
2. **A record's key reference spelled ``api_key_env``** is read, as WebUI's own ladder respells it to ``key_env`` for
   a record and the Agent reads both. The chat doesn't read ``api_key_env`` on a legacy entry or the model block, so
   neither does the lookup.
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


def _lookup(cfg, provider="custom:my-proxy", **kw):
    cfg.setdefault("providers", {})
    cfg.setdefault("custom_providers", [])
    return routes._context_length_lookup_inputs_for_model("m-x", provider, cfg=cfg, **kw)


@pytest.fixture
def key_in_env(monkeypatch):
    monkeypatch.setenv(VAR, "K1")
    monkeypatch.delenv(UNSET, raising=False)


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


def test_only_disabled_spellings_of_a_builtin_give_no_config_key():
    # The Agent's exact key misses, so it doesn't refuse; but the switched-off record isn't its source and the chat
    # doesn't send the model block's key here (openai chats with its own environment key or the placeholder). No
    # config key lets the streaming sites' Agent fallback supply the chat's own.
    got = _lookup({"model": {"provider": "openai", "api_key": "KM"},
                   "providers": {"OpenAI": {"api_key": "K2", "enabled": False}}}, provider="openai")
    assert got.api_key == ""


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
    # Two records name my-proxy. The chat routes this shape through one of them; the selection here reads on as on
    # exp-v0.52.659 (the model block's key at its own URL). Ambiguity isn't a switch-off.
    got = _lookup({"model": {"provider": "custom:my-proxy", "base_url": A, "api_key": "KM"},
                   "providers": {"one": {"name": "my-proxy", "base_url": B, "api_key": "K1"},
                                 "two": {"name": "my-proxy", "base_url": L, "api_key": "K2"}}})
    assert (got.base_url, got.api_key) == (A, "KM")


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


def test_a_builtin_record_key_spelled_api_key_env_is_its_own_key(key_in_env):
    # The Agent's own model listing reads ``key_env or api_key_env`` on a provider block (hermes_cli.models).
    got = _lookup({"model": {"provider": "openai"}, "providers": {"openai": {"api_key_env": VAR}}}, provider="openai")
    assert got.api_key == "K1"


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
