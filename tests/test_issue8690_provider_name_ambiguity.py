"""#8690: a provider name two records claim fails closed instead of mixing them.

Config: ``providers.openai`` (the built-in record) plus ``providers['custom:openai']``
(another endpoint and key) and ``model.provider: openai``. Hermes Agent answers the
bare ``openai`` request with the first record whose alias set contains ``openai``
and keys its credential pool by name, so depending on dict order it returned the
NAMED record's endpoint with the BUILT-IN record's key. Both WebUI connection-bundle
helpers passed that pair through.

The fix is name-based (no URL comparison): when a bare built-in route ``X`` and any
other record claiming ``X`` coexist (any spelling, enabled or disabled), or the
explicit ``custom:X`` selector is used while ``providers.X`` exists, the route
returns a terminal verdict naming the records. The runtime dicts below are the ones
the installed Agent returns for these configs (captured with synthetic keys and
outbound I/O blocked). No Agent, live endpoint or real profile is used.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

from api import config, routes, streaming

BUILTIN_URL, BUILTIN_KEY = "https://builtin.invalid/v1", "synthetic-builtin-key"
NAMED_URL, NAMED_KEY = "https://named.invalid/v1", "synthetic-named-key"
OPENAI_DEFAULT_URL = "https://api.openai.com/v1"
AMBIENT_KEY = "synthetic-ambient-env-key"
SECRETS = (BUILTIN_KEY, NAMED_KEY, AMBIENT_KEY, BUILTIN_URL, NAMED_URL, "https://", "http://")
POOL = object()  # stands in for the Agent's CredentialPool object
# getattr: on a checkout without the fix the refusal tests must fail on their
# assertions, not on an import error that would also fail the unchanged cases.
REASON = getattr(config, "PROVIDER_NAME_AMBIGUOUS", "provider_name_ambiguous")


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    # The raw-identity source (Codex r3) reads the active profile's config.yaml;
    # point it at a per-test file that only the raw-config tests create.
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(tmp_path / "profile" / "config.yaml"))
    # No Agent: record pool / host-gated lookups report nothing, exactly as in CI.
    pkg = types.ModuleType("hermes_cli")
    pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", types.ModuleType("hermes_cli.runtime_provider"))
    monkeypatch.setattr(config._thread_ctx, "env", {}, raising=False)
    monkeypatch.setattr(config._thread_ctx, "block_process_env_fallback", True, raising=False)

    def no_network(*_args, **_kwargs):
        raise AssertionError("unexpected network access")

    monkeypatch.setattr("socket.getaddrinfo", no_network)
    monkeypatch.setattr("socket.create_connection", no_network)


def _bind(monkeypatch, data):
    monkeypatch.setattr(config, "cfg", data)
    monkeypatch.setattr(config, "get_config", lambda: data)


def _records(pid, order, named_url=NAMED_URL):
    builtin = (pid, {"base_url": BUILTIN_URL, "api_key": BUILTIN_KEY})
    named = ("custom:" + pid, {"base_url": named_url, "api_key": NAMED_KEY, "models": ["named-model"]})
    return dict([named, builtin] if order == "named-first" else [builtin, named])


def _config(pid, providers, **model_extra):
    return {"model": {"provider": pid, "default": "gpt-synthetic", **model_extra}, "providers": providers}


def _agent_runtime(base_url, api_key, **extra):
    """The dict ``resolve_runtime_provider(requested=<provider>)`` returns here."""
    return {"provider": "custom", "api_mode": "chat_completions", "base_url": base_url,
            "api_key": api_key, "credential_pool": POOL, **extra}


def _streaming_bundle(provider, runtime, configured_base_url=None):
    """api/streaming.py's sequence: preferred URL, then the connection bundle."""
    base_url = streaming._runtime_preferred_base_url(runtime, provider, configured_base_url)
    return streaming._resolve_runtime_connection_bundle(
        provider, runtime.get("api_key"), base_url, runtime, custom_provider_lookup=provider,
    )


def _routes_bundle(provider, runtime, configured_base_url=None):
    """api/routes.py's sequence (``_llm_update_summary`` and its siblings)."""
    return routes._resolve_agent_connection_bundle(
        provider, runtime.get("api_key"), configured_base_url or runtime.get("base_url"), runtime,
    )


def _assert_refused(bundle_or_call, *claimants):
    """The final constructor bundle carries no endpoint/credential and a named verdict."""
    if callable(bundle_or_call):
        with pytest.raises(config.CustomProviderRouteError) as exc:
            bundle_or_call()
        err = exc.value
        assert err.reason == REASON
        text = f"{err.message} {err.hint}"
    else:
        bundle = bundle_or_call
        verdict = config.custom_provider_route_error(bundle)
        assert verdict is not None and verdict["reason"] == REASON
        # Defence in depth: nothing routable is left in the bundle either.
        assert bundle["base_url"] is None and bundle["api_key"] is None
        assert bundle["credential_pool"] is None and bundle["api_mode"] is None
        assert bundle["acp_command"] is None and bundle["acp_args"] is None
        with pytest.raises(config.CustomProviderRouteError):
            config.raise_for_custom_provider_route(bundle)
        text = f"{verdict['message']} {verdict['hint']}"
    for claimant in claimants:
        assert claimant in text
    for secret in SECRETS:
        assert secret not in text
    return text


def _both_refused(provider, runtime, *claimants, configured_base_url=None):
    _assert_refused(_streaming_bundle(provider, runtime, configured_base_url), *claimants)
    _assert_refused(lambda: _routes_bundle(provider, runtime, configured_base_url), *claimants)


def _both_route(provider, runtime, expected, configured_base_url=None):
    for build in (_streaming_bundle, _routes_bundle):
        bundle = config.raise_for_custom_provider_route(build(provider, runtime, configured_base_url))
        assert (bundle["provider"], bundle["base_url"], bundle["api_key"]) == expected


# ── The reported case: both orders x both helpers, final bundle ─────────────────


@pytest.mark.parametrize("order", ["named-first", "builtin-first"])
@pytest.mark.parametrize("pid", ["openai", "x-ai", "google"])
def test_bare_builtin_route_with_a_custom_sibling_is_refused(monkeypatch, pid, order):
    _bind(monkeypatch, _config(pid, _records(pid, order)))
    # What the Agent returns: the first record's URL, the built-in pool's key.
    runtime = _agent_runtime(NAMED_URL if order == "named-first" else BUILTIN_URL, BUILTIN_KEY)
    _both_refused(pid, runtime, f"'{pid}'", f"'custom:{pid}'")
    # A configured model.base_url does not launder the pair either.
    _both_refused(pid, runtime, f"'custom:{pid}'", configured_base_url=BUILTIN_URL)


def test_the_refusal_message_names_both_records_and_the_fix(monkeypatch):
    _bind(monkeypatch, _config("openai", _records("openai", "named-first")))
    text = _assert_refused(_streaming_bundle("openai", _agent_runtime(NAMED_URL, BUILTIN_KEY)))
    assert text.startswith(
        "Two providers claim 'openai': the built-in 'openai' and 'custom:openai'. Rename the "
        "custom one (for example custom:my-proxy) or remove one, then retry."
    )
    label = streaming._custom_provider_route_classification(
        config.custom_provider_route_error(_streaming_bundle("openai", _agent_runtime(NAMED_URL, BUILTIN_KEY)))
    )
    assert label["label"] == "Provider name conflict" and label["reason"] == REASON


def test_every_claimant_is_named(monkeypatch):
    data = _config("openai", _records("openai", "named-first"))
    data["custom_providers"] = [{"name": "OpenAI", "base_url": NAMED_URL, "api_key": NAMED_KEY}]
    _bind(monkeypatch, data)
    text = _assert_refused(_streaming_bundle("openai", _agent_runtime(NAMED_URL, BUILTIN_KEY)))
    assert text.startswith(
        "Three providers claim 'openai': the built-in 'openai', 'custom:openai' and "
        "custom_providers entry 'OpenAI'."
    )


# ── Which records claim the name: spellings, legacy shapes, disabled rows ───────


@pytest.mark.parametrize("extra", [
    {"custom_providers": [{"name": "openai", "base_url": NAMED_URL, "api_key": NAMED_KEY}]},
    {"custom_providers": [{"name": "openai", "url": NAMED_URL, "api_key": NAMED_KEY}]},
    {"custom_providers": [{"name": "openai", "api": NAMED_URL, "api_key": NAMED_KEY}]},
    {"custom_providers": [{"name": "openai", "baseUrl": NAMED_URL, "api_key": NAMED_KEY}]},
    {"custom_providers": [{"name": "openai", "base_url": NAMED_URL, "api_key": NAMED_KEY, "enabled": False}]},
    {"custom_providers": [{"name": "  OpenAI ", "base_url": NAMED_URL, "api_key": NAMED_KEY}]},
    {"providers": {"custom:openai": {"base_url": NAMED_URL, "api_key": NAMED_KEY, "enabled": "false"}}},
    {"providers": {"custom:OpenAI": {"base_url": NAMED_URL, "api_key": NAMED_KEY}}},
    {"providers": {" custom:openai ": {"base_url": NAMED_URL, "api_key": NAMED_KEY}}},
    {"providers": {"my-proxy": {"name": "openai", "base_url": NAMED_URL, "api_key": NAMED_KEY}}},
    {"providers": {"my-proxy": {"name": "Custom:OpenAI", "base_url": NAMED_URL, "api_key": NAMED_KEY}}},
    {"providers": {"custom": {"name": "openai", "base_url": NAMED_URL, "api_key": NAMED_KEY}}},
    {"providers": {"OpenAI": {"base_url": NAMED_URL, "api_key": NAMED_KEY}}},
], ids=[
    "legacy-base_url", "legacy-url", "legacy-api", "legacy-baseUrl", "legacy-disabled",
    "legacy-case-whitespace", "keyed-disabled", "keyed-case",
    "keyed-whitespace", "display-name", "display-name-custom-prefix", "generic-custom-named",
    "second-builtin-spelling",
])
def test_any_record_claiming_the_builtin_name_is_a_sibling(monkeypatch, extra):
    data = _config("openai", {"openai": {"base_url": BUILTIN_URL, "api_key": BUILTIN_KEY}}, base_url=BUILTIN_URL)
    data["providers"].update(extra.get("providers", {}))
    if "custom_providers" in extra:
        data["custom_providers"] = extra["custom_providers"]
    _bind(monkeypatch, data)
    runtime = _agent_runtime(NAMED_URL, NAMED_KEY)
    _both_refused("openai", runtime, "the built-in 'openai'", configured_base_url=BUILTIN_URL)


def test_an_env_templated_sibling_url_is_refused_without_reading_it(monkeypatch):
    """``${env:VAR}`` URLs are never expanded or compared: the name alone decides."""
    monkeypatch.setenv("NAMED_URL", NAMED_URL)
    _bind(monkeypatch, _config("openai", _records("openai", "named-first", "${env:NAMED_URL}"),
                               base_url=BUILTIN_URL))
    _both_refused("openai", _agent_runtime(NAMED_URL, BUILTIN_KEY), "'custom:openai'",
                  configured_base_url=BUILTIN_URL)


@pytest.mark.parametrize("name", ["${env:SIBLING_NAME}", "${SIBLING_NAME}"])
def test_an_env_templated_sibling_name_is_refused(monkeypatch, name):
    """Codex r1: the Agent expands ``${env:VAR}`` names in its own secret scope."""
    monkeypatch.setenv("SIBLING_NAME", "openai")
    providers = {"myproxy": {"name": name, "base_url": NAMED_URL, "api_key": NAMED_KEY},
                 "openai": {"base_url": BUILTIN_URL, "api_key": BUILTIN_KEY}}
    _bind(monkeypatch, _config("openai", providers, base_url=BUILTIN_URL))
    _both_refused("openai", _agent_runtime(NAMED_URL, NAMED_KEY), "'myproxy'", configured_base_url=BUILTIN_URL)


@pytest.mark.parametrize("providers", [
    {"myproxy": {"name": "Proxy ${", "base_url": NAMED_URL, "api_key": NAMED_KEY}},
    {"myproxy": {"name": "${file:PROXY_ID}", "base_url": NAMED_URL, "api_key": NAMED_KEY}},
    {"myproxy": {"name": "${env:}", "base_url": NAMED_URL, "api_key": NAMED_KEY}},
    {"${env:SIBLING_NAME}": {"base_url": NAMED_URL, "api_key": NAMED_KEY}},
], ids=["incomplete-ref", "non-env-source", "empty-env-ref", "templated-mapping-key"])
def test_references_the_agent_leaves_verbatim_do_not_claim_the_name(monkeypatch, providers):
    """Codex r2: only ``${VAR}`` / ``${env:VAR}`` in a string value is expanded by the Agent."""
    monkeypatch.setenv("SIBLING_NAME", "openai")
    _bind(monkeypatch, _config("openai", {**providers, "openai": {"base_url": BUILTIN_URL, "api_key": BUILTIN_KEY}}))
    _both_route("openai", _agent_runtime(BUILTIN_URL, BUILTIN_KEY), ("openai", BUILTIN_URL, BUILTIN_KEY))


@pytest.mark.parametrize("extra", [
    {"custom_providers": {"openai": {"name": "openai", "base_url": NAMED_URL, "api_key": NAMED_KEY}}},
    {"providers": {"myproxy": {"provider_key": "openai", "base_url": NAMED_URL, "api_key": NAMED_KEY}}},
    {"custom_providers": [{"name": "My Proxy", "provider_key": "openai", "base_url": NAMED_URL}]},
], ids=["dict-shaped-legacy-section", "keyed-provider_key-field", "legacy-provider_key-field"])
def test_identities_the_agent_ignores_do_not_claim_the_name(monkeypatch, extra):
    """Codex r1: the Agent skips a non-list ``custom_providers`` and raw ``provider_key``."""
    data = _config("openai", {"openai": {"base_url": BUILTIN_URL, "api_key": BUILTIN_KEY}})
    data["providers"].update(extra.get("providers", {}))
    if "custom_providers" in extra:
        data["custom_providers"] = extra["custom_providers"]
    _bind(monkeypatch, data)
    _both_route("openai", _agent_runtime(BUILTIN_URL, BUILTIN_KEY), ("openai", BUILTIN_URL, BUILTIN_KEY))


@pytest.mark.parametrize("model_base_url", [None, OPENAI_DEFAULT_URL], ids=["no-model-base_url", "model-base_url"])
@pytest.mark.parametrize("ambient", [False, True], ids=["no-env-key", "ambient-env-key"])
def test_bare_route_with_only_the_named_record_is_refused(monkeypatch, ambient, model_base_url):
    """No ``providers.openai``: the Agent answers bare ``openai`` with the named record.

    WebUI routes bare ``openai`` as the built-in (#8685) and prefers a configured
    ``model.base_url``, so the named key would go to the built-in's endpoint; the
    built-in identity (default endpoint, ambient key) and the named record are two
    claimants of one name.
    """
    extra = {"base_url": model_base_url} if model_base_url else {}
    _bind(monkeypatch, _config("openai", {"custom:openai": {"base_url": NAMED_URL, "api_key": NAMED_KEY}}, **extra))
    if ambient:
        monkeypatch.setenv("OPENAI_API_KEY", AMBIENT_KEY)
    _both_refused("openai", _agent_runtime(NAMED_URL, NAMED_KEY), "'custom:openai'",
                  configured_base_url=model_base_url)


# ── The explicit custom:<builtin> selector ──────────────────────────────────────


@pytest.mark.parametrize("order", ["named-first", "builtin-first"])
def test_explicit_custom_builtin_selector_with_the_builtin_record_is_refused(monkeypatch, order):
    """The Agent's alias set for ``providers.openai`` contains ``custom:openai`` too."""
    _bind(monkeypatch, _config("custom:openai", _records("openai", order)))
    runtime = _agent_runtime(NAMED_URL, BUILTIN_KEY)
    _both_refused("custom:openai", runtime, "the built-in 'openai'", "'custom:openai'")


def test_explicit_custom_builtin_selector_without_the_builtin_record_is_unchanged(monkeypatch):
    _bind(monkeypatch, _config("custom:openai", {"custom:openai": {"base_url": NAMED_URL, "api_key": NAMED_KEY}}))
    monkeypatch.setenv("OPENAI_API_KEY", AMBIENT_KEY)
    _both_route("custom:openai", _agent_runtime(NAMED_URL, NAMED_KEY), ("custom", NAMED_URL, NAMED_KEY))


# ── Configs with no ambiguity are unchanged ─────────────────────────────────────


def test_a_genuine_named_custom_provider_route_is_unchanged(monkeypatch):
    providers = {"custom:myprov": {"base_url": NAMED_URL, "api_key": NAMED_KEY},
                 "openai": {"base_url": BUILTIN_URL, "api_key": BUILTIN_KEY}}
    _bind(monkeypatch, _config("custom:myprov", providers))
    _both_route("custom:myprov", _agent_runtime(NAMED_URL, NAMED_KEY), ("custom", NAMED_URL, NAMED_KEY))


@pytest.mark.parametrize("pid", ["openai", "x-ai", "google"])
def test_the_builtin_record_alone_is_unchanged(monkeypatch, pid):
    providers = {pid: {"base_url": BUILTIN_URL, "api_key": BUILTIN_KEY},
                 "custom:myprov": {"base_url": NAMED_URL, "api_key": NAMED_KEY}}
    _bind(monkeypatch, _config(pid, providers))
    _both_route(pid, _agent_runtime(BUILTIN_URL, BUILTIN_KEY), (pid, BUILTIN_URL, BUILTIN_KEY))


@pytest.mark.parametrize("providers", [
    {},
    {"openai": {"api_key": BUILTIN_KEY}},
    {"custom:myprov": {"base_url": NAMED_URL, "api_key": NAMED_KEY}},
    {"openai-codex": {"api_key": NAMED_KEY}, "custom:openai-proxy": {"base_url": NAMED_URL}},
], ids=["no-providers", "builtin-key-only", "unrelated-custom", "near-miss-names"])
def test_ambient_env_key_route_is_unchanged(monkeypatch, providers):
    """No other record claims ``openai``: the Agent's direct alias stands."""
    _bind(monkeypatch, _config("openai", providers))
    monkeypatch.setenv("OPENAI_API_KEY", AMBIENT_KEY)
    runtime = _agent_runtime(OPENAI_DEFAULT_URL, AMBIENT_KEY, api_mode="codex_responses",
                             credential_pool=None, source="direct-alias")
    _both_route("openai", runtime, ("openai", OPENAI_DEFAULT_URL, AMBIENT_KEY))


@pytest.mark.parametrize("provider", ["custom", "auto", "", "custom:my-proxy", "my-proxy"])
def test_non_builtin_names_are_out_of_scope(monkeypatch, provider):
    data = _config("openai", _records("openai", "named-first"))
    data["providers"].update({"my-proxy": {"base_url": NAMED_URL}, "custom:my-proxy": {"base_url": NAMED_URL}})
    _bind(monkeypatch, data)
    assert config.provider_name_ambiguity_verdict(provider) is None


# ── Model alias paths ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("alias_provider", ["openai", "custom:openai"])
def test_a_provider_only_model_alias_takes_the_same_refusal(monkeypatch, alias_provider):
    """``model_aliases: {fast: {provider: openai}}`` builds its bundle from the same runtime."""
    _bind(monkeypatch, _config("openai", _records("openai", "named-first")))
    runtime = _agent_runtime(NAMED_URL, BUILTIN_KEY)
    alias = {"alias": "fast", "model": "gpt-synthetic", "provider": alias_provider,
             "base_url_explicit": False, "credential_explicit": False}
    _assert_refused(config.merge_model_alias_runtime_bundle(alias, runtime), "'custom:openai'")
    # A credential-only alias must neither complete nor relabel the refused pair.
    for key in (AMBIENT_KEY, None):
        keyed = {**alias, "credential_explicit": True, "api_key": key}
        _assert_refused(config.merge_model_alias_runtime_bundle(keyed, runtime), "'custom:openai'")
    with pytest.raises(config.CustomProviderRouteError):
        config.raise_for_custom_provider_route(streaming._resolve_model_alias_connection_bundle(alias, runtime))


def test_an_alias_with_its_own_endpoint_and_key_is_unchanged(monkeypatch):
    """A URL+key alias owns both halves and is resolved by URL (``requested=custom``)."""
    _bind(monkeypatch, _config("openai", _records("openai", "named-first")))
    alias = {"alias": "fast", "model": "gpt-synthetic", "provider": "openai", "base_url": NAMED_URL,
             "base_url_explicit": True, "credential_explicit": True, "api_key": NAMED_KEY}
    bundle = config.raise_for_custom_provider_route(config.merge_model_alias_runtime_bundle(alias, {}))
    assert (bundle["base_url"], bundle["api_key"]) == (NAMED_URL, NAMED_KEY)


# ── Codex r3: identities are read unexpanded from the profile's config.yaml ────


def _write_profile_config(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")  # JSON is YAML
    return path


def _sibling_named(name):
    return _config("openai", {
        "myproxy": {"name": name, "base_url": NAMED_URL, "api_key": NAMED_KEY},
        "openai": {"base_url": BUILTIN_URL, "api_key": BUILTIN_KEY},
    }, base_url=BUILTIN_URL)


def test_a_reference_the_cache_expanded_to_another_name_is_still_refused(monkeypatch, tmp_path):
    """Codex r3 repro: the cache expanded ``${SIBLING_NAME}`` to 'unrelated', then the
    profile .env changed it to 'openai' without touching config.yaml. The Agent reads
    'openai'; the stale cache must not let WebUI accept the built-in URL with myproxy's key."""
    _write_profile_config(tmp_path / "profile" / "config.yaml", _sibling_named("${SIBLING_NAME}"))
    _bind(monkeypatch, _sibling_named("unrelated"))  # the stale expanded cache
    runtime = _agent_runtime(BUILTIN_URL, NAMED_KEY)
    _both_refused("openai", runtime, "'myproxy'", configured_base_url=BUILTIN_URL)
    verdict = config.provider_name_ambiguity_verdict("openai")
    assert verdict and verdict["reason"] == REASON


def test_literal_names_in_the_profile_config_keep_their_verdicts(monkeypatch, tmp_path):
    _write_profile_config(tmp_path / "profile" / "config.yaml", _sibling_named("unrelated"))
    _bind(monkeypatch, _sibling_named("unrelated"))
    assert config.provider_name_ambiguity_verdict("openai") is None
    _write_profile_config(tmp_path / "profile" / "config.yaml", _sibling_named("openai"))
    _bind(monkeypatch, _sibling_named("openai"))
    assert config.provider_name_ambiguity_verdict("openai")["reason"] == REASON


def test_the_raw_identities_follow_the_active_profile_config(monkeypatch, tmp_path):
    """The raw file is the one get_config() serves: switching profiles switches it."""
    templated = _write_profile_config(tmp_path / "b" / "config.yaml", _sibling_named("${SIBLING_NAME}"))
    literal = _write_profile_config(tmp_path / "a" / "config.yaml", _sibling_named("unrelated"))
    _bind(monkeypatch, _sibling_named("unrelated"))
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(literal))
    assert config.provider_name_ambiguity_verdict("openai") is None
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(templated))
    assert config.provider_name_ambiguity_verdict("openai")["reason"] == REASON


# ── Codex r1 (deferred): title generation calls the Agent with a provider name ──

TITLE_REFUSED = "provider_name_ambiguous_aux"


@pytest.fixture
def aux_calls(monkeypatch):
    """A stand-in ``agent.auxiliary_client`` that records every ``call_llm``."""
    calls = []
    fake = types.ModuleType("agent.auxiliary_client")
    fake.call_llm = lambda **kwargs: calls.append(kwargs)
    pkg = sys.modules.get("agent")
    if pkg is None:
        pkg = types.ModuleType("agent")
        pkg.__path__ = []
        monkeypatch.setitem(sys.modules, "agent", pkg)
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", fake)
    return calls


def _title_route(monkeypatch, aux, providers, main_provider="openai"):
    monkeypatch.setattr(streaming, "_get_aux_title_config", lambda: dict(aux))
    _bind(monkeypatch, _config(main_provider, providers, base_url=BUILTIN_URL))


@pytest.mark.parametrize("aux, caller", [
    ({"provider": "openai", "model": "gpt-synthetic"}, {}),           # auxiliary.title_generation
    ({"provider": "custom:openai"}, {}),                              # explicit selector
    ({}, {"provider": "openai", "model": "gpt-synthetic"}),           # use_agent_model pass-through
    ({"provider": "auto"}, {}),                                       # auto -> the main provider
    ({}, {}),                                                         # unset -> the main provider
])
def test_title_generation_with_an_ambiguous_name_never_calls_the_agent(monkeypatch, aux_calls, caplog, aux, caller):
    _title_route(monkeypatch, aux, _records("openai", "named-first"))
    with caplog.at_level("WARNING"):
        raw, status = streaming.generate_title_raw_via_aux("hello there", "hi, how can I help", **caller)
    assert aux_calls == [] and (raw, status) == (None, TITLE_REFUSED)
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "title" in logged.lower() and "'custom:openai'" in logged
    for secret in SECRETS:
        assert secret not in logged


def test_the_refused_title_falls_back_to_the_local_title(monkeypatch, aux_calls):
    _title_route(monkeypatch, {"provider": "openai"}, _records("openai", "builtin-first"))
    title, status, preview = streaming._generate_llm_session_title_via_aux("hello there", "hi")
    assert (title, status, preview) == (None, TITLE_REFUSED, "") and aux_calls == []
    # Not an LLM error/invalid answer: no retry through another route, local title next.
    assert status not in {"llm_error_aux", "llm_invalid_aux", "llm_language_mismatch_aux"}
    assert "provider" in streaming._human_title_failure_reason(status)


@pytest.mark.parametrize("aux", [
    {"provider": "openai", "model": "gpt-synthetic"},
    {"provider": "auto"},
    {"base_url": NAMED_URL, "model": "named-model"},  # a URL route carries no name
])
def test_title_generation_with_an_unambiguous_route_is_unchanged(monkeypatch, aux_calls, aux):
    _title_route(monkeypatch, aux, {"openai": {"base_url": BUILTIN_URL, "api_key": BUILTIN_KEY}})
    streaming.generate_title_raw_via_aux("hello there", "hi, how can I help")
    assert aux_calls and aux_calls[0]["task"] == "title_generation"
    assert aux_calls[0]["provider"] == (aux.get("provider") if aux.get("provider") != "auto" else None)
