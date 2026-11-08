"""#8918: a lookup key may also go where a config source holding the SAME key value sends it.

#8822's rule sends a key only to hosts its own source owns. A caller key that arrives with no URL owns only its
provider's fixed hosts, and a key the config selection takes from a source without a URL owns nothing, so the lookup
dropped a key even when another source holding that exact credential declares the URL. That source already sends
this credential there, so the lookup exposes nothing new by sending it too. v0.52.544 sent these pairs.

The match is exact, on non-empty strings only. A blank value, or a key that isn't a string (a ``key_cmd`` token
source), never matches. A key whose holders declare no URL keeps #8822's behaviour: the model block's URL never
carries a record's or legacy entry's key just because they sit side by side.
"""
import pytest

from api import routes

A = "https://a.model-url.test/v1"
B = "https://b.record-url.test/v1"
L = "https://l.legacy-url.test/v1"


def _lookup(cfg, provider="custom:my-proxy", **kw):
    cfg.setdefault("providers", {})
    cfg.setdefault("custom_providers", [])
    return routes._context_length_lookup_inputs_for_model("m-x", provider, cfg=cfg, **kw)


def _session_lookup(cfg, monkeypatch, provider="custom:my-proxy"):
    """GET /api/session's path: the entry assembles the caller pair, then the lookup applies the rule."""
    import api.config as config

    cfg.setdefault("providers", {})
    cfg.setdefault("custom_providers", [])
    monkeypatch.setattr(config, "cfg", cfg)
    monkeypatch.setattr(config, "get_config", lambda *a, **k: cfg)
    model, prov, base_url, api_key = routes._session_context_length_lookup_state("m-x", provider)
    got = routes._context_length_lookup_inputs_for_model(model, prov, base_url=base_url, api_key=api_key, cfg=cfg)
    return got.base_url, got.api_key


# ── A caller key with no URL ───────────────────────────────────────────────────


def test_a_caller_key_without_a_url_goes_where_a_record_holding_the_same_value_sends_it():
    got = _lookup({"model": {"provider": "custom:my-proxy", "base_url": B},
                   "providers": {"custom:my-proxy": {"base_url": B, "api_key": "K1"}}}, api_key="K1")
    assert (got.base_url, got.api_key) == (B, "K1")


def test_a_caller_key_without_a_url_goes_where_a_legacy_entry_holding_the_same_value_sends_it():
    got = _lookup({"model": {"provider": "custom:my-proxy", "base_url": L},
                   "custom_providers": [{"name": "my-proxy", "base_url": L, "api_key": "K1"}]}, api_key="K1")
    assert (got.base_url, got.api_key) == (L, "K1")


def test_a_caller_key_with_a_different_value_still_owns_no_url():
    got = _lookup({"model": {"provider": "custom:my-proxy", "base_url": B},
                   "providers": {"custom:my-proxy": {"base_url": B, "api_key": "K2"}}}, api_key="K1")
    assert (got.base_url, got.api_key) == (B, "")


def test_a_same_value_holder_lends_only_the_urls_it_declares():
    # The record holds K1 for B; the lookup's URL is the model block's A, which no holder of K1 declares.
    got = _lookup({"model": {"provider": "custom:my-proxy", "base_url": A},
                   "providers": {"custom:my-proxy": {"base_url": B, "api_key": "K1"}}}, api_key="K1")
    assert (got.base_url, got.api_key) == (A, "")


def test_a_same_value_holder_on_another_port_lends_nothing():
    other_port = "https://b.record-url.test:8443/v1"
    got = _lookup({"model": {"provider": "custom:my-proxy", "base_url": B},
                   "providers": {"custom:my-proxy": {"base_url": other_port, "api_key": "K1"}}}, api_key="K1")
    assert (got.base_url, got.api_key) == (B, "")


def test_a_same_value_holder_with_another_scheme_lends_nothing():
    plain = "http://b.record-url.test/v1"
    got = _lookup({"model": {"provider": "custom:my-proxy", "base_url": B},
                   "providers": {"custom:my-proxy": {"base_url": plain, "api_key": "K1"}}}, api_key="K1")
    assert (got.base_url, got.api_key) == (B, "")


def test_a_holder_that_declares_no_url_lends_nothing():
    # #8822's class stays closed: the record holds K1 but declares no URL, and A is the model block's.
    got = _lookup({"model": {"provider": "custom:my-proxy", "base_url": A},
                   "providers": {"custom:my-proxy": {"api_key": "K1"}}}, api_key="K1")
    assert (got.base_url, got.api_key) == (A, "")


def test_a_builtin_record_holding_the_value_lends_only_its_own_fixed_hosts():
    got = _lookup({"model": {"provider": "custom:my-proxy", "base_url": A},
                   "providers": {"openai": {"api_key": "K1"}}}, api_key="K1")
    assert (got.base_url, got.api_key) == (A, "")


def test_an_env_reference_resolving_to_the_same_value_matches(monkeypatch):
    monkeypatch.setenv("WEBUI_TEST_8918_KEY", "K1")
    got = _lookup({"model": {"provider": "custom:my-proxy", "base_url": B},
                   "providers": {"custom:my-proxy": {"base_url": B, "api_key": "${WEBUI_TEST_8918_KEY}"}}},
                  api_key="K1")
    assert (got.base_url, got.api_key) == (B, "K1")


def test_a_key_env_holder_matches(monkeypatch):
    monkeypatch.setenv("WEBUI_TEST_8918_KEY", "K1")
    got = _lookup({"model": {"provider": "custom:my-proxy", "base_url": L},
                   "custom_providers": [{"name": "my-proxy", "base_url": L, "key_env": "WEBUI_TEST_8918_KEY"}]},
                  api_key="K1")
    assert (got.base_url, got.api_key) == (L, "K1")


def test_a_record_key_env_holder_matches(monkeypatch):
    monkeypatch.setenv("WEBUI_TEST_8918_KEY", "K1")
    got = _lookup({"model": {"provider": "custom:my-proxy", "base_url": B},
                   "providers": {"custom:my-proxy": {"base_url": B, "key_env": "WEBUI_TEST_8918_KEY"}}},
                  api_key="K1")
    assert (got.base_url, got.api_key) == (B, "K1")


@pytest.mark.parametrize("caller, held", [("K1", "  K1  "), ("  K1\t", "K1"), (" K1 ", "\nK1 ")])
def test_surrounding_whitespace_is_not_part_of_the_value(caller, held):
    # WebUI strips every key it reads, on both sides, so a value padded with whitespace is the same credential: the
    # stripped key goes to the holder's URL.
    got = _lookup({"model": {"provider": "custom:my-proxy", "base_url": B},
                   "providers": {"custom:my-proxy": {"base_url": B, "api_key": held}}}, api_key=caller)
    assert (got.base_url, got.api_key) == (B, "K1")


def test_an_unresolved_reference_grants_nothing(monkeypatch):
    monkeypatch.delenv("WEBUI_TEST_8918_UNSET", raising=False)
    ref = "${WEBUI_TEST_8918_UNSET}"
    cfg = {"model": {"provider": "custom:my-proxy", "base_url": B, "api_key": ref},
           "providers": {"custom:my-proxy": {"base_url": B, "api_key": ref}},
           "custom_providers": [{"name": "my-proxy", "base_url": L, "api_key": ref}]}
    assert routes._context_lookup_same_value_origins(ref, cfg) == frozenset()
    got = _lookup(cfg, api_key=ref)
    assert (got.base_url, got.api_key) == (B, "")


def test_a_reference_shaped_literal_grants_nothing():
    # ``${}`` is too short to resolve, so a holder keeps it as a literal; a value shaped like a reference still never
    # grants, whatever a holder resolved it to.
    cfg = {"model": {"provider": "custom:my-proxy", "base_url": B},
           "providers": {"custom:my-proxy": {"base_url": B, "api_key": "${}"}}}
    assert routes._context_lookup_same_value_origins("${}", cfg) == frozenset()


def test_an_ambient_env_fallback_does_not_make_a_holder(monkeypatch):
    # The legacy entry declares no key; the per-provider env variable the runtime may fall back to isn't in config,
    # so the entry holds nothing and grants nothing.
    import api.config as api_config

    monkeypatch.setenv(api_config._api_key_env_name("custom:my-proxy"), "K1")
    monkeypatch.setenv(api_config._api_key_env_name("my-proxy"), "K1")
    cfg = {"model": {"provider": "custom:my-proxy", "base_url": L},
           "custom_providers": [{"name": "my-proxy", "base_url": L}]}
    assert routes._context_lookup_same_value_origins("K1", cfg) == frozenset()


@pytest.mark.parametrize("placeholder", ["ollama", "none", "EMPTY"])
def test_a_placeholder_value_is_matched_like_any_other(placeholder):
    # Local servers often take a placeholder key. It is treated as a value like any other: it reaches the URL its holder
    # declares, and nowhere else.
    own = _lookup({"model": {"provider": "custom:my-proxy", "base_url": B},
                   "providers": {"custom:my-proxy": {"base_url": B, "api_key": placeholder}}}, api_key=placeholder)
    assert (own.base_url, own.api_key) == (B, placeholder)
    other = _lookup({"model": {"provider": "custom:my-proxy", "base_url": A},
                     "providers": {"custom:my-proxy": {"base_url": B, "api_key": placeholder}}}, api_key=placeholder)
    assert (other.base_url, other.api_key) == (A, "")


# ── Fixed hosts: only the holder's own provider ────────────────────────────────

ANTHROPIC = "https://api.anthropic.com/v1"


def test_a_builtin_record_holding_the_value_grants_its_own_fixed_host():
    got = _lookup({"model": {"provider": "custom:my-proxy", "base_url": ANTHROPIC},
                   "providers": {"anthropic": {"api_key": "K1"}}}, api_key="K1")
    assert (got.base_url, got.api_key) == (ANTHROPIC, "K1")


def test_another_builtin_holding_the_value_grants_no_other_providers_fixed_host():
    got = _lookup({"model": {"provider": "custom:my-proxy", "base_url": ANTHROPIC},
                   "providers": {"novita": {"api_key": "K1"}}}, api_key="K1")
    assert (got.base_url, got.api_key) == (ANTHROPIC, "")


def test_a_legacy_entry_named_like_a_builtin_grants_no_fixed_host():
    got = _lookup({"model": {"provider": "custom:my-proxy", "base_url": ANTHROPIC},
                   "custom_providers": [{"name": "anthropic", "api_key": "K1"}]}, api_key="K1")
    assert (got.base_url, got.api_key) == (ANTHROPIC, "")


def test_a_custom_model_block_holding_the_value_grants_no_fixed_host():
    cfg = {"model": {"provider": "custom:anthropic", "api_key": "K1"}}
    assert routes._context_lookup_origin(ANTHROPIC) not in routes._context_lookup_same_value_origins("K1", cfg)


def test_an_alias_record_grants_its_canonical_builtins_fixed_host():
    # ``providers.claude`` is the anthropic record under an alias: the owner rule and the Agent treat it as anthropic.
    got = _lookup({"model": {"provider": "custom:my-proxy", "base_url": ANTHROPIC},
                   "providers": {"claude": {"api_key": "K1"}}}, api_key="K1")
    assert (got.base_url, got.api_key) == (ANTHROPIC, "K1")


X = "https://x.caller-url.test/v1"
COPILOT_HOST = "api.githubcopilot.com:443"


def _contacts_caller_url_and_copilot(monkeypatch):
    # Pin what the Agent contacts for (copilot, X): the caller's URL and Copilot's fixed host. Whether the installed
    # Agent's Copilot step adds the fixed host varies by Agent version; these tests are about the gate's arithmetic.
    contacted = frozenset({routes._context_lookup_origin(X), COPILOT_HOST})
    monkeypatch.setattr(routes, "_context_lookup_contacted_origins", lambda *a, **k: contacted)


def test_the_grant_adds_to_the_keys_own_hosts_rather_than_replacing_them(monkeypatch):
    # The caller key owns X; the copilot record holding the same value owns Copilot's fixed host. Only together do
    # they cover what the Agent contacts.
    _contacts_caller_url_and_copilot(monkeypatch)
    got = _lookup({"providers": {"copilot": {"api_key": "K1"}}}, provider="copilot", base_url=X, api_key="K1")
    assert (got.base_url, got.api_key) == (X, "K1")


def test_a_model_block_grants_its_own_providers_fixed_hosts_not_the_sessions(monkeypatch):
    # The model block holds K1 for novita. The session is copilot: Copilot's fixed host must not be granted through
    # the model block just because the lookup is for copilot.
    _contacts_caller_url_and_copilot(monkeypatch)
    got = _lookup({"model": {"provider": "novita", "api_key": "K1"}}, provider="copilot", base_url=X, api_key="K1")
    assert (got.base_url, got.api_key) == (X, "")


# ── A key the config selection took from a source without a URL ─────────────


def test_a_legacy_key_without_a_url_goes_where_the_model_block_holding_it_sends_it():
    got = _lookup({"model": {"provider": "custom:my-proxy", "base_url": A, "api_key": "K1"},
                   "custom_providers": [{"name": "my-proxy", "api_key": "K1"}]})
    assert (got.base_url, got.api_key) == (A, "K1")


def test_a_legacy_key_without_a_url_still_drops_beside_a_model_block_with_another_key():
    got = _lookup({"model": {"provider": "custom:my-proxy", "base_url": A, "api_key": "K2"},
                   "custom_providers": [{"name": "my-proxy", "api_key": "K1"}]})
    assert (got.base_url, got.api_key) == (A, "")


# ── What never matches ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["", "   ", None])
def test_a_blank_value_owns_nothing(value):
    cfg = {"model": {"provider": "custom:my-proxy", "base_url": A, "api_key": "   "},
           "providers": {"custom:my-proxy": {"base_url": B, "api_key": ""}},
           "custom_providers": [{"name": "my-proxy", "base_url": L, "api_key": " "}]}
    assert routes._context_lookup_same_value_origins(value, cfg) == frozenset()


def test_blank_holders_never_lend_their_urls():
    cfg = {"model": {"provider": "custom:my-proxy", "base_url": A, "api_key": "   "},
           "providers": {"custom:my-proxy": {"base_url": B, "api_key": ""}},
           "custom_providers": [{"name": "my-proxy", "base_url": L, "api_key": " "}]}
    assert routes._context_lookup_same_value_origins("K1", cfg) == frozenset()


class _TokenSource:
    """A ``key_cmd``-style token source that claims to equal every string."""

    def __eq__(self, other):
        return True

    __hash__ = object.__hash__

    def __str__(self):
        return "K1"


def test_a_token_source_never_matches_by_value():
    cfg = {"model": {"provider": "custom:my-proxy", "base_url": B},
           "providers": {"custom:my-proxy": {"base_url": B, "api_key": "K1"}}}
    assert routes._context_lookup_same_value_origins(_TokenSource(), cfg) == frozenset()
    got = _lookup(cfg, api_key=_TokenSource())
    assert got.base_url == B
    assert got.api_key == ""


def _single_holder(source, value):
    """Config where only ``source`` holds ``value`` and declares the lookup's URL (its own)."""
    if source == "record":
        return {"model": {"provider": "custom:my-proxy", "base_url": B},
                "providers": {"custom:my-proxy": {"base_url": B, "api_key": value}}}, B
    if source == "legacy":
        return {"model": {"provider": "custom:my-proxy", "base_url": L},
                "custom_providers": [{"name": "my-proxy", "base_url": L, "api_key": value}]}, L
    return {"model": {"provider": "custom:my-proxy", "base_url": A, "api_key": value}}, A


@pytest.mark.parametrize("source", ["record", "legacy", "model"])
@pytest.mark.parametrize("held", ["k1", "K", "K1-suffix", "xK1", "K1 K1"])
def test_a_value_matches_exactly_not_by_case_prefix_or_substring(source, held):
    cfg, url = _single_holder(source, held)
    assert routes._context_lookup_same_value_origins("K1", cfg) == frozenset()
    got = _lookup(cfg, api_key="K1")
    assert (got.base_url, got.api_key) == (url, "")


@pytest.mark.parametrize("source", ["record", "legacy", "model"])
def test_each_source_holding_the_exact_value_lends_its_own_url(source):
    cfg, url = _single_holder(source, "K1")
    assert routes._context_lookup_origin(url) in routes._context_lookup_same_value_origins("K1", cfg)
    got = _lookup(cfg, api_key="K1")
    assert (got.base_url, got.api_key) == (url, "K1")


def test_the_helper_collects_every_same_value_holder():
    cfg = {"model": {"provider": "custom:my-proxy", "base_url": A, "api_key": "K1"},
           "providers": {"custom:my-proxy": {"base_url": B, "api_key": "K1"}},
           "custom_providers": [{"name": "my-proxy", "base_url": L, "api_key": "K1"},
                                {"name": "other", "base_url": "https://o.other.test/v1", "api_key": "K2"}]}
    origins = routes._context_lookup_same_value_origins("K1", cfg)
    assert {routes._context_lookup_origin(u) for u in (A, B, L)} <= origins
    assert routes._context_lookup_origin("https://o.other.test/v1") not in origins


# ── GET /api/session: the measured shapes ──────────────────────────────────────


@pytest.mark.parametrize("cfg, expected", [
    # The record declares B and holds K1; the legacy entry holds K1 with no URL (the caller key arrives without one).
    ({"model": {"provider": "custom:my-proxy"},
      "providers": {"custom:my-proxy": {"base_url": B, "api_key": "K1", "models": {"m-x": {}}}},
      "custom_providers": [{"name": "my-proxy", "api_key": "K1", "models": {"m-x": {}}}]}, (B, "K1")),
    # The model block declares A and holds K1; the legacy entry holds K1 with no URL.
    ({"model": {"provider": "custom:my-proxy", "base_url": A, "api_key": "K1"},
      "custom_providers": [{"name": "my-proxy", "api_key": "K1", "models": {"m-x": {}}}]}, (A, "K1")),
    # The record holds K1 with no URL and the model block declares B with no key: no holder of K1 declares B.
    ({"model": {"provider": "custom:my-proxy", "base_url": B},
      "providers": {"custom:my-proxy": {"api_key": "K1", "models": {"m-x": {}}}}}, (B, "")),
    # Duplicated config, unchanged since #8822: the same URL and key in the model block and the legacy entry.
    ({"model": {"provider": "custom:my-proxy", "base_url": L, "api_key": "K1"},
      "custom_providers": [{"name": "my-proxy", "base_url": L, "api_key": "K1", "models": {"m-x": {}}}]}, (L, "K1")),
], ids=["record-url-legacy-key", "model-pair-legacy-key", "no-holder-declares-the-url", "duplicated-config"])
def test_a_session_reload_sends_a_key_where_a_same_value_holder_sends_it(cfg, expected, monkeypatch):
    assert _session_lookup(cfg, monkeypatch) == expected
