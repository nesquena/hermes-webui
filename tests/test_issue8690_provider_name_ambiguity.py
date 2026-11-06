"""#8690: a provider name two records claim fails closed instead of mixing them.

Config: ``providers.openai`` (the built-in record) plus ``providers['custom:openai']``
(another endpoint and key) and ``model.provider: openai``. Hermes Agent answers the
bare ``openai`` request with the first record whose alias set contains ``openai``
and keys its credential pool by name, so depending on dict order it returned the
NAMED record's endpoint with the BUILT-IN record's key. Both WebUI connection-bundle
helpers passed that pair through.

The fix is name-based (no URL comparison): when a bare built-in route ``X`` and any
other record claiming ``X`` coexist (any spelling; a keyed record the Agent treats as
disabled claims nothing, a legacy row counts either way), or the
explicit ``custom:X`` selector is used while ``providers.X`` exists, the route
returns a terminal verdict naming the records. The runtime dicts below are the ones
the installed Agent returns for these configs (captured with synthetic keys and
outbound I/O blocked). No Agent, live endpoint or real profile is used.
"""

from __future__ import annotations

import copy
import json
import os
import pathlib
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
    # Also when a real Agent was imported earlier in the run: its ``hermes_cli.config``
    # (env expander, managed scope) is installed only by the tests that need a stand-in.
    monkeypatch.setitem(sys.modules, "hermes_cli.config", types.ModuleType("hermes_cli.config"))
    monkeypatch.setitem(sys.modules, "agent.secret_scope", types.ModuleType("agent.secret_scope"))
    monkeypatch.setattr(config._thread_ctx, "env", {}, raising=False)
    monkeypatch.setattr(config._thread_ctx, "block_process_env_fallback", True, raising=False)

    def no_network(*_args, **_kwargs):
        raise AssertionError("unexpected network access")

    monkeypatch.setattr("socket.getaddrinfo", no_network)
    monkeypatch.setattr("socket.create_connection", no_network)


def _bind(monkeypatch, data):
    monkeypatch.setattr(config, "cfg", data)
    monkeypatch.setattr(config, "get_config", lambda: data)
    # The identity check reads a detached snapshot; bind it too so these tests never refresh
    # the real config cache (that leaked into later files' in-place cfg patches).
    monkeypatch.setattr(config, "get_config_snapshot", lambda: copy.deepcopy(data))


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
    {"providers": {"custom:OpenAI": {"base_url": NAMED_URL, "api_key": NAMED_KEY}}},
    {"providers": {" custom:openai ": {"base_url": NAMED_URL, "api_key": NAMED_KEY}}},
    {"providers": {"my-proxy": {"name": "openai", "base_url": NAMED_URL, "api_key": NAMED_KEY}}},
    {"providers": {"my-proxy": {"name": "Custom:OpenAI", "base_url": NAMED_URL, "api_key": NAMED_KEY}}},
    {"providers": {"custom": {"name": "openai", "base_url": NAMED_URL, "api_key": NAMED_KEY}}},
    {"providers": {"OpenAI": {"base_url": NAMED_URL, "api_key": NAMED_KEY}}},
], ids=[
    "legacy-base_url", "legacy-url", "legacy-api", "legacy-baseUrl", "legacy-disabled",
    "legacy-case-whitespace", "keyed-case",
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


@pytest.mark.parametrize("flag", [False, "false", "off", "no", 0, "0"])
def test_a_disabled_keyed_record_claims_nothing(monkeypatch, flag):
    """Release-lane gate r1103 (senior): the Agent skips a keyed ``providers`` record whose
    ``enabled`` is false in both of its views, so the built-in route stays consistent and must
    not be refused. A disabled legacy ``custom_providers`` row still counts (the Agent's legacy
    view selects it), covered by ``legacy-disabled`` above."""
    data = _config("openai", {
        "custom:openai": {"base_url": NAMED_URL, "api_key": NAMED_KEY, "enabled": flag},
        "openai": {"base_url": BUILTIN_URL, "api_key": BUILTIN_KEY},
    })
    _bind(monkeypatch, data)
    assert config.provider_name_ambiguity_verdict("openai") is None
    _both_route("openai", _agent_runtime(BUILTIN_URL, BUILTIN_KEY), ("openai", BUILTIN_URL, BUILTIN_KEY))


def _install_agent_expander(monkeypatch):
    """A stand-in for the installed Agent's ``hermes_cli.config._expand_env_vars``.

    Same contract as the Agent (``_env_expand_match``): ``${VAR}`` / ``${env:VAR}`` read
    the environment, an unset or non-env (``${file:...}``) reference stays verbatim.
    """
    import re as _re

    def expand(obj):
        def one(match):
            inner = match.group(1).strip()
            if inner.startswith("env:"):
                name = inner[len("env:"):].strip()
            elif _re.match(r"^[a-z][a-z0-9_-]*:", inner):
                name = None
            else:
                name = inner
            value = os.environ.get(name) if name else None
            return match.group(0) if value is None else value
        return _re.sub(r"\$\{([^}]+)\}", one, obj) if isinstance(obj, str) else obj

    module = sys.modules.get("hermes_cli.config") or types.ModuleType("hermes_cli.config")
    monkeypatch.setattr(module, "_expand_env_vars", expand, raising=False)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", module)
    _install_secret_scope(monkeypatch, {})


def _install_secret_scope(monkeypatch, mapping):
    """``agent.secret_scope.current_secret_scope``: a bound profile scope, or None (unbound)."""
    scope = types.ModuleType("agent.secret_scope")
    scope.current_secret_scope = lambda: mapping
    monkeypatch.setitem(sys.modules, "agent.secret_scope", scope)


def _templated_flag_config():
    return _config("openai", {
        "custom:openai": {"base_url": NAMED_URL, "api_key": NAMED_KEY, "enabled": "${PROXY_ENABLED}"},
        "openai": {"base_url": BUILTIN_URL, "api_key": BUILTIN_KEY},
    })


@pytest.mark.parametrize("value", ["false", "off", "0", "no", " FALSE "])
def test_a_keyed_record_whose_templated_flag_expands_false_claims_nothing(monkeypatch, tmp_path, value):
    """Gate r1103b/c (Codex CORE): ``enabled: ${PROXY_ENABLED}`` with PROXY_ENABLED=false.

    The Agent expands the flag before ``is_provider_enabled`` and skips the record, so the
    built-in pair is consistent. Both views hold the unexpanded flag (the raw file always
    does; a cache that could not expand it does too) and must judge it the Agent's way.
    """
    _install_agent_expander(monkeypatch)
    monkeypatch.setenv("PROXY_ENABLED", value)
    _write_profile_config(tmp_path / "profile" / "config.yaml", _templated_flag_config())
    _bind(monkeypatch, _templated_flag_config())
    assert config.provider_name_ambiguity_verdict("openai") is None
    _both_route("openai", _agent_runtime(BUILTIN_URL, BUILTIN_KEY), ("openai", BUILTIN_URL, BUILTIN_KEY))


@pytest.mark.parametrize("value", ["true", "yes", "1", "maybe", "", None], ids=[
    "true", "yes", "1", "garbage", "empty", "unset"])
def test_a_keyed_record_whose_templated_flag_the_agent_keeps_enabled_still_claims(monkeypatch, tmp_path, value):
    """The Agent's ``is_provider_enabled`` treats anything but a false word as enabled,
    including an unset reference it keeps verbatim: the sibling claims and is refused."""
    _install_agent_expander(monkeypatch)
    if value is None:
        monkeypatch.delenv("PROXY_ENABLED", raising=False)
    else:
        monkeypatch.setenv("PROXY_ENABLED", value)
    _write_profile_config(tmp_path / "profile" / "config.yaml", _templated_flag_config())
    _bind(monkeypatch, _templated_flag_config())
    _both_refused("openai", _agent_runtime(NAMED_URL, BUILTIN_KEY), "'custom:openai'")


def test_a_templated_flag_without_the_agents_expander_keeps_the_record(monkeypatch, tmp_path):
    """No Agent expander (older Agent, or none installed): the flag's value is unknown, so
    the record keeps claiming (fail closed), whatever WebUI's own environment says."""
    monkeypatch.setenv("PROXY_ENABLED", "false")
    _write_profile_config(tmp_path / "profile" / "config.yaml", _templated_flag_config())
    _bind(monkeypatch, _templated_flag_config())
    _both_refused("openai", _agent_runtime(NAMED_URL, BUILTIN_KEY), "'custom:openai'")


def test_a_disabled_legacy_row_still_claims_with_the_agents_expander(monkeypatch, tmp_path):
    """Legacy rows are untouched by the flag rule: the Agent's legacy view selects them."""
    _install_agent_expander(monkeypatch)
    monkeypatch.setenv("PROXY_ENABLED", "false")
    data = _config("openai", {"openai": {"base_url": BUILTIN_URL, "api_key": BUILTIN_KEY}})
    data["custom_providers"] = [{"name": "openai", "base_url": NAMED_URL, "api_key": NAMED_KEY,
                                 "enabled": "${PROXY_ENABLED}"}]
    _write_profile_config(tmp_path / "profile" / "config.yaml", data)
    _bind(monkeypatch, data)
    _both_refused("openai", _agent_runtime(NAMED_URL, BUILTIN_KEY), "custom_providers entry 'openai'")


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


def test_a_url_alias_without_its_own_key_takes_the_same_refusal(monkeypatch):
    """``model_aliases: {fast: {provider: openai, base_url: <named URL>}}`` with no key of its own.

    The Agent resolves the alias host's credential from the provider's shared pool, which with
    a sibling claiming ``openai`` is the built-in record's key (#8690): the alias takes the
    provider's verdict. An alias that declares its own key owns both halves (next test).
    """
    _bind(monkeypatch, _config("openai", _records("openai", "named-first")))
    alias = {"alias": "fast", "model": "gpt-synthetic", "provider": "openai", "base_url": NAMED_URL,
             "base_url_explicit": True, "credential_explicit": False}
    runtime = {"base_url": NAMED_URL, "api_key": BUILTIN_KEY}  # _resolve_model_alias_endpoint_runtime's shape
    _assert_refused(config.merge_model_alias_runtime_bundle(alias, runtime), "'custom:openai'")
    with pytest.raises(config.CustomProviderRouteError):
        config.raise_for_custom_provider_route(streaming._resolve_model_alias_connection_bundle(alias, runtime))


def test_a_url_alias_without_its_own_key_routes_when_nothing_else_claims_the_name(monkeypatch):
    _bind(monkeypatch, _config("openai", {"openai": {"base_url": BUILTIN_URL, "api_key": BUILTIN_KEY}}))
    alias = {"alias": "fast", "model": "gpt-synthetic", "provider": "openai", "base_url": NAMED_URL,
             "base_url_explicit": True, "credential_explicit": False}
    bundle = config.raise_for_custom_provider_route(
        config.merge_model_alias_runtime_bundle(alias, {"base_url": NAMED_URL, "api_key": BUILTIN_KEY}))
    assert (bundle["provider"], bundle["base_url"], bundle["api_key"]) == ("openai", NAMED_URL, BUILTIN_KEY)


@pytest.mark.parametrize("record", [
    {"name": "openai", "enabled": "${PROXY_ENABLED}"},
    {"name": "${PROXY_NAME}"},
], ids=["templated-flag", "templated-name"])
def test_templated_values_are_not_expanded_without_a_bound_profile_secret_scope(monkeypatch, tmp_path, record):
    """Codex f1 CORE #1: with no profile secret scope bound, the Agent's expander reads the
    shared process env, which a concurrent worker for another profile can hold. The value
    stays unknown and the record keeps claiming (the PR-head, fail-closed reading)."""
    monkeypatch.setenv("PROXY_ENABLED", "false")
    monkeypatch.setenv("PROXY_NAME", "myproxy")
    data = _config("openai", {"custom:proxy": {"base_url": NAMED_URL, "api_key": NAMED_KEY, **record},
                              "openai": {"base_url": BUILTIN_URL, "api_key": BUILTIN_KEY}})
    _bind(monkeypatch, data)
    _install_agent_expander(monkeypatch)
    assert config.provider_name_ambiguity_verdict("openai", data) is None
    _install_secret_scope(monkeypatch, None)
    assert config.provider_name_ambiguity_verdict("openai", data)["reason"] == REASON


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


BUILTIN_IDS = ("openai", "anthropic", "openrouter", "x-ai", "google")


@pytest.mark.parametrize("name", ["${PROXY_NAME}", "${env:PROXY_NAME}", " ${PROXY_NAME} "])
@pytest.mark.parametrize("value", ["myproxy", None], ids=["set-to-another-name", "unset"])
def test_a_templated_name_is_judged_by_the_agents_expansion(monkeypatch, tmp_path, name, value):
    """Gate r1103c (Codex CORE #2): ``name: ${PROXY_NAME}`` with PROXY_NAME=myproxy refused
    every built-in route. The Agent expands the name (an unset reference stays verbatim, which
    names no built-in), so only the expanded value can claim."""
    _install_agent_expander(monkeypatch)
    if value is None:
        monkeypatch.delenv("PROXY_NAME", raising=False)
    else:
        monkeypatch.setenv("PROXY_NAME", value)
    _write_profile_config(tmp_path / "profile" / "config.yaml", _sibling_named(name))
    _bind(monkeypatch, _sibling_named(name))
    for pid in BUILTIN_IDS:
        assert config.provider_name_ambiguity_verdict(pid) is None, pid
    _both_route("openai", _agent_runtime(BUILTIN_URL, BUILTIN_KEY), ("openai", BUILTIN_URL, BUILTIN_KEY),
                configured_base_url=BUILTIN_URL)


@pytest.mark.parametrize("name", ["${PROXY_NAME}", "${env:PROXY_NAME}"])
@pytest.mark.parametrize("value", ["openai", "OpenAI", "custom:openai"])
def test_a_templated_name_that_expands_to_the_builtin_is_still_refused(monkeypatch, tmp_path, name, value):
    """A genuine same-name conflict after expansion: refused, and the message keeps the raw
    spelling (never the expanded value)."""
    _install_agent_expander(monkeypatch)
    monkeypatch.setenv("PROXY_NAME", value)
    _write_profile_config(tmp_path / "profile" / "config.yaml", _sibling_named(name))
    _bind(monkeypatch, _sibling_named(name))
    text = _assert_refused(_streaming_bundle("openai", _agent_runtime(NAMED_URL, NAMED_KEY), BUILTIN_URL),
                           f"'myproxy' (named '{name}')")
    _assert_refused(lambda: _routes_bundle("openai", _agent_runtime(NAMED_URL, NAMED_KEY), BUILTIN_URL), "'myproxy'")
    assert config.provider_name_ambiguity_verdict("anthropic") is None
    assert f"(named '{value}')" not in text


def test_a_stale_cache_cannot_hide_a_name_the_agent_expands_to_the_builtin(monkeypatch, tmp_path):
    """Codex r3 with the Agent's expander: the cache still says 'unrelated', the Agent now
    expands ``${SIBLING_NAME}`` to 'openai'. The raw view, judged the Agent's way, refuses."""
    _install_agent_expander(monkeypatch)
    monkeypatch.setenv("SIBLING_NAME", "openai")
    _write_profile_config(tmp_path / "profile" / "config.yaml", _sibling_named("${SIBLING_NAME}"))
    _bind(monkeypatch, _sibling_named("unrelated"))
    _both_refused("openai", _agent_runtime(BUILTIN_URL, NAMED_KEY), "'myproxy'", configured_base_url=BUILTIN_URL)


def test_a_templated_name_without_the_agents_expander_is_still_a_wildcard(monkeypatch, tmp_path):
    """No Agent expander: the Agent's value is unknown, so the name claims (fail closed)."""
    monkeypatch.setenv("PROXY_NAME", "myproxy")
    _write_profile_config(tmp_path / "profile" / "config.yaml", _sibling_named("${PROXY_NAME}"))
    _bind(monkeypatch, _sibling_named("${PROXY_NAME}"))
    _both_refused("openai", _agent_runtime(BUILTIN_URL, BUILTIN_KEY), "'myproxy'", configured_base_url=BUILTIN_URL)


# ── #8775 review: the chat route's secret scope decides how templated records are judged ──
#
# _run_agent_streaming calls no set_secret_scope itself; its connection-bundle helper
# (streaming._resolve_runtime_connection_bundle, called with profile_name=<session profile>)
# enters profiles.profile_scope_for_detached_worker, which binds a scope for a named
# (non-root) profile and binds none for the root profile or an empty name, leaving any
# outer scope in place. These drive that helper with the chat route's own arguments and
# no outer scope bound, so the default profile's resolution sees current_secret_scope() None.


def _contextvar_secret_scope(monkeypatch):
    """The Agent's ``agent.secret_scope`` contract: a context-local mapping that
    ``set_secret_scope`` binds and ``reset_secret_scope`` restores; None when unbound."""
    import contextvars

    var = contextvars.ContextVar("issue8690_secret_scope", default=None)
    module = types.ModuleType("agent.secret_scope")
    module.set_secret_scope = lambda mapping: var.set(dict(mapping))
    module.reset_secret_scope = var.reset
    module.current_secret_scope = var.get
    monkeypatch.setitem(sys.modules, "agent.secret_scope", module)
    return module


@pytest.fixture
def chat_profiles(monkeypatch, tmp_path):
    """A root home and one named profile ``writer`` whose .env holds the templated values."""
    from api import profiles

    root = tmp_path / ".hermes"
    writer = root / "profiles" / "writer"
    writer.mkdir(parents=True)
    (writer / ".env").write_text("PROXY_NAME=myproxy\nPROXY_ENABLED=false\n", encoding="utf-8")
    monkeypatch.delenv("HERMES_WEBUI_ISOLATED_PROFILE", raising=False)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    monkeypatch.setattr(profiles, "_loaded_profile_env_keys", set())
    monkeypatch.setattr(profiles, "list_profiles_api",
                        lambda: [{"name": "default", "is_default": True}, {"name": "writer"}])
    profiles._invalidate_root_profile_cache()
    monkeypatch.setattr(profiles._tls, "profile", None, raising=False)
    # The chat route mirrors the session profile's env into the process env for the turn.
    monkeypatch.setenv("PROXY_NAME", "myproxy")
    monkeypatch.setenv("PROXY_ENABLED", "false")
    _install_agent_expander(monkeypatch)
    scope = _contextvar_secret_scope(monkeypatch)
    yield scope
    profiles._invalidate_root_profile_cache()


def _chat_route_bundle(provider, runtime, profile_name, configured_base_url=None):
    """_run_agent_streaming's call: preferred URL, then the bundle with the session profile."""
    base_url = streaming._runtime_preferred_base_url(runtime, provider, configured_base_url)
    return streaming._resolve_runtime_connection_bundle(
        provider, runtime.get("api_key"), base_url, runtime,
        profile_name=profile_name, custom_provider_lookup=provider,
    )


_CHAT_TEMPLATED = {
    "templated-name": (lambda: _sibling_named("${PROXY_NAME}"), "'myproxy'"),
    "templated-flag": (_templated_flag_config, "'custom:openai'"),
}


def _bind_chat_config(monkeypatch, tmp_path, kind):
    build, claimant = _CHAT_TEMPLATED[kind]
    _write_profile_config(tmp_path / "profile" / "config.yaml", build())
    _bind(monkeypatch, build())
    return claimant


def test_the_chat_route_passes_the_session_profile_to_every_bundle_resolution():
    """The premise of the tests below: each of the chat route's bundle resolutions hands the
    helper the session profile, which decides whether the helper binds the secret scope the
    templated-record judgement depends on (one for a named profile, none for the root
    profile or an empty name)."""
    import inspect
    import re

    source = inspect.getsource(streaming._run_agent_streaming)
    calls = re.findall(r"_resolve_runtime_connection_bundle\((.*?)\n\s*\)", source, re.S)
    assert len(calls) == 3
    assert all("profile_name=_resolved_profile_name" in call for call in calls)
    assert "set_secret_scope" not in source


@pytest.mark.parametrize("profile_name", ["default", None], ids=["default-profile", "no-profile"])
@pytest.mark.parametrize("kind", sorted(_CHAT_TEMPLATED))
def test_the_chat_route_without_a_secret_scope_judges_templated_records_fail_closed(
        monkeypatch, tmp_path, chat_profiles, kind, profile_name):
    """#8775 review: with no outer scope bound, the chat route's bundle resolution for the
    default profile (or with no profile name) runs with ``current_secret_scope()`` None, so a
    templated ``name: ${PROXY_NAME}`` (PROXY_NAME=myproxy) still claims every name and a templated
    ``enabled: ${PROXY_ENABLED}`` (false) keeps its record claiming: the bare built-in route
    is refused although the Agent's resolver would expand both from the process env."""
    claimant = _bind_chat_config(monkeypatch, tmp_path, kind)
    seen = []
    real = config._agent_expanded_text
    monkeypatch.setattr(config, "_agent_expanded_text",
                        lambda value: seen.append(chat_profiles.current_secret_scope()) or real(value))
    bundle = _chat_route_bundle("openai", _agent_runtime(BUILTIN_URL, BUILTIN_KEY), profile_name,
                                configured_base_url=BUILTIN_URL)
    _assert_refused(bundle, claimant)
    assert seen and all(scope is None for scope in seen)


@pytest.mark.parametrize("kind", sorted(_CHAT_TEMPLATED))
def test_a_named_profile_chat_route_judges_templated_records_under_its_secret_scope(
        monkeypatch, tmp_path, chat_profiles, kind):
    """Counterpart: a named profile's bundle resolution runs under the scope
    profile_scope_for_detached_worker binds from that profile's .env, so the templated
    name or flag is judged by the Agent's expansion and the built-in route goes through."""
    _bind_chat_config(monkeypatch, tmp_path, kind)
    seen = []
    real = config._agent_expanded_text
    monkeypatch.setattr(config, "_agent_expanded_text",
                        lambda value: seen.append(chat_profiles.current_secret_scope()) or real(value))
    bundle = _chat_route_bundle("openai", _agent_runtime(BUILTIN_URL, BUILTIN_KEY), "writer",
                                configured_base_url=BUILTIN_URL)
    bundle = config.raise_for_custom_provider_route(bundle)
    assert (bundle["provider"], bundle["base_url"], bundle["api_key"]) == ("openai", BUILTIN_URL, BUILTIN_KEY)
    assert seen and all(scope is not None and scope.get("PROXY_NAME") == "myproxy" for scope in seen)
    assert chat_profiles.current_secret_scope() is None  # restored after the turn's resolution


# Codex optA-gate CORE (reproduced with the installed Agent's real resolver, synthetic keys,
# network blocked): a guard that re-reads the shared process env after the Agent's resolver
# can be raced. A concurrent named-profile worker mirrors its .env into os.environ while the
# default profile's resolver expands the record, then restores it before the guard runs.
# The writer's values make the templated record claim ``openai``; the root's do not.
_RACE_WRITER_ENV = {"PROXY_NAME": "openai", "PROXY_ENABLED": "true"}
_RACE_TEMPLATE = {"templated-name": "${PROXY_NAME}", "templated-flag": "${PROXY_ENABLED}"}


def _resolver_expansion(template):
    """What the Agent's resolver reads for the record: its own expander, no scope bound."""
    return sys.modules["hermes_cli.config"]._expand_env_vars(template)


def _resolver_runtime(kind, expansion):
    """The runtime the installed Agent's ``resolve_runtime_provider(requested='openai')``
    returned in that reproduction: the templated record answers the bare name when it
    expands to ``openai`` (name) or enabled (flag), and its URL and key come back."""
    claims = expansion == "openai" if kind == "templated-name" else expansion == "true"
    return _agent_runtime(NAMED_URL, NAMED_KEY) if claims else _agent_runtime(BUILTIN_URL, BUILTIN_KEY)


@pytest.mark.parametrize("kind", sorted(_RACE_TEMPLATE))
def test_a_concurrent_profile_env_between_the_resolver_and_the_guard_cannot_unlock_a_templated_record(
        monkeypatch, tmp_path, chat_profiles, kind):
    """The default profile's bundle resolution here runs with no secret scope bound, so the
    guard must not judge a templated name or flag by re-reading os.environ: here the resolver reads the writer worker's
    values (the record claims ``openai`` and its key comes back), the worker restores the
    root values (``myproxy`` / ``false``) and only then does the guard run. Re-reading
    would approve and pair the named key with the built-in URL; the route fails closed."""
    import threading

    from api import profiles

    (tmp_path / ".hermes" / "profiles" / "writer" / ".env").write_text(
        "".join(f"{key}={value}\n" for key, value in _RACE_WRITER_ENV.items()), encoding="utf-8")
    claimant = _bind_chat_config(monkeypatch, tmp_path, kind)
    root_env = dict(os.environ)
    entered, release, errors = threading.Event(), threading.Event(), []

    def writer_worker():
        try:
            with profiles.profile_env_for_background_worker("writer", scope_skill_modules=False):
                entered.set()
                if not release.wait(10):
                    errors.append("release barrier timed out")
        except BaseException as exc:  # surfaced by the assertions below
            errors.append(repr(exc))
            entered.set()

    worker = threading.Thread(target=writer_worker, name="issue8690-writer-worker")
    worker.start()
    try:
        assert entered.wait(10) and not errors, errors
        # 1. The Agent's resolver, in the default profile's turn: no scope, the writer's env.
        assert chat_profiles.current_secret_scope() is None
        resolver_read = _resolver_expansion(_RACE_TEMPLATE[kind])
        assert resolver_read == _RACE_WRITER_ENV["PROXY_NAME" if kind == "templated-name" else "PROXY_ENABLED"]
        runtime = _resolver_runtime(kind, resolver_read)
        assert (runtime["base_url"], runtime["api_key"]) == (NAMED_URL, NAMED_KEY)
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive() and not errors, errors
    # 2. The worker restored the root env: a re-read now sees a value that claims nothing.
    assert dict(os.environ) == root_env
    assert _resolver_expansion(_RACE_TEMPLATE[kind]) != resolver_read
    # 3. The guard: the chat route's bundle resolution for the default profile.
    bundle = _chat_route_bundle("openai", runtime, "default", configured_base_url=BUILTIN_URL)
    assert NAMED_KEY not in bundle.values() and BUILTIN_URL not in bundle.values(), (
        bundle["base_url"], bundle["api_key"])
    _assert_refused(bundle, claimant)
    assert chat_profiles.current_secret_scope() is None


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
    """WebUI's view and, until :func:`_agent_scope` overrides it, the Agent's
    config file (``hermes_cli.config.get_config_path()``) hold the same config."""
    monkeypatch.setattr(streaming, "_get_aux_title_config", lambda: dict(aux))
    data = _config(main_provider, providers, base_url=BUILTIN_URL)
    _bind(monkeypatch, data)
    agent_cfg = _write_profile_config(
        pathlib.Path(os.environ["HERMES_CONFIG_PATH"]).parent.parent / "agent-same" / "config.yaml", data)
    fake_cfg = types.ModuleType("hermes_cli.config")
    fake_cfg.get_config_path = lambda: agent_cfg
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake_cfg)


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


# ── Codex r4: the title guard reads the profile the Agent's call_llm will load ──


def _agent_scope(monkeypatch, aux_calls, tmp_path, agent_providers, main_provider=None):
    """The Agent's view in a background title worker: its own config path (the
    session profile) and, for auto routes, its current main provider."""
    agent_cfg = _write_profile_config(tmp_path / "agent-profile" / "config.yaml",
                                      _config("openai", agent_providers, base_url=BUILTIN_URL))
    fake_cfg = types.ModuleType("hermes_cli.config")
    fake_cfg.get_config_path = lambda: agent_cfg
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake_cfg)
    if main_provider is not None:
        sys.modules["agent.auxiliary_client"]._read_main_provider = lambda: main_provider


AMBIGUOUS = _records("openai", "named-first")
PLAIN = {"openai": {"base_url": BUILTIN_URL, "api_key": BUILTIN_KEY}}


def test_title_guard_refuses_when_the_agents_profile_is_ambiguous(monkeypatch, aux_calls, tmp_path):
    """Converse of Codex r4 #1: the worker binds the session profile's env for the
    Agent while WebUI's own view is the default profile (unambiguous here)."""
    _title_route(monkeypatch, {"provider": "openai"}, PLAIN)
    _agent_scope(monkeypatch, aux_calls, tmp_path, AMBIGUOUS)
    assert streaming.generate_title_raw_via_aux("hello there", "hi") == (None, TITLE_REFUSED)
    assert aux_calls == []


def test_title_guard_allows_an_unambiguous_agent_profile(monkeypatch, aux_calls, tmp_path):
    """Codex r4 #1: an ambiguous default profile must not block another profile's titles."""
    _title_route(monkeypatch, {"provider": "openai"}, AMBIGUOUS)
    _agent_scope(monkeypatch, aux_calls, tmp_path, PLAIN)
    streaming.generate_title_raw_via_aux("hello there", "hi")
    assert aux_calls and aux_calls[0]["provider"] == "openai"


@pytest.mark.parametrize("value, refused", [("myproxy", False), (None, False), ("openai", True)])
def test_the_title_guard_judges_a_templated_name_by_the_agents_expansion(monkeypatch, aux_calls, tmp_path, value, refused):
    """Gate r1103c (Codex CORE #2) on the title path: the Agent profile's raw file is judged
    with the same expander, so an unrelated templated name no longer blocks LLM titles."""
    _title_route(monkeypatch, {"provider": "openai"}, PLAIN)
    _agent_scope(monkeypatch, aux_calls, tmp_path, {
        "myproxy": {"name": "${PROXY_NAME}", "base_url": NAMED_URL, "api_key": NAMED_KEY}, **PLAIN})
    _install_agent_expander(monkeypatch)
    if value is None:
        monkeypatch.delenv("PROXY_NAME", raising=False)
    else:
        monkeypatch.setenv("PROXY_NAME", value)
    raw, status = streaming.generate_title_raw_via_aux("hello there", "hi")
    assert (status == TITLE_REFUSED) is refused
    assert bool(aux_calls) is not refused


@pytest.mark.parametrize("agent_main, refused", [("anthropic", False), ("openai", True)])
def test_auto_title_routes_use_the_agents_current_main_provider(monkeypatch, aux_calls, tmp_path, agent_main, refused):
    """Codex r4 #2: model.provider: ${MAIN_PROVIDER}; the cache holds a stale expansion."""
    cached_main = "anthropic" if agent_main == "openai" else "openai"
    _title_route(monkeypatch, {"provider": "auto"}, AMBIGUOUS, main_provider=cached_main)
    _agent_scope(monkeypatch, aux_calls, tmp_path, AMBIGUOUS, main_provider=agent_main)
    raw, status = streaming.generate_title_raw_via_aux("hello there", "hi")
    assert (status == TITLE_REFUSED) is refused
    assert bool(aux_calls) is not refused


# ── Codex r5: the Agent merges its managed scope over the profile's providers ──


def _agent_deep_merge(base, override):
    """Stand-in for ``hermes_cli.config._deep_merge`` (verbatim): the merge the
    Agent's ``load_config()`` and ``apply_managed_overlay`` put managed values on with."""
    result = base.copy()
    for key, value in override.items():
        over_dict = isinstance(result.get(key), dict)
        if over_dict and isinstance(value, dict):
            result[key] = _agent_deep_merge(result[key], value)
        elif not (over_dict and value is None):
            result[key] = value
    return result


@pytest.fixture
def managed(monkeypatch, tmp_path):
    """Install an Agent managed scope: its config.yaml in ``$HERMES_MANAGED_DIR``
    (how the Agent's own tests point it away from /etc/hermes), read unexpanded
    by ``load_managed_config`` like ``hermes_cli.managed_scope``'s reader."""
    def install(data):
        managed_dir = tmp_path / "managed"
        managed_dir.mkdir(exist_ok=True)
        (managed_dir / "config.yaml").write_text(json.dumps(data), encoding="utf-8")
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_dir))
        scope = types.ModuleType("hermes_cli.managed_scope")
        scope.load_managed_config = lambda: json.loads(
            (pathlib.Path(os.environ["HERMES_MANAGED_DIR"]) / "config.yaml").read_text(encoding="utf-8"))
        monkeypatch.setitem(sys.modules, "hermes_cli.managed_scope", scope)
        agent_cfg = sys.modules.get("hermes_cli.config")
        if agent_cfg is None:
            agent_cfg = types.ModuleType("hermes_cli.config")
            monkeypatch.setitem(sys.modules, "hermes_cli.config", agent_cfg)
        monkeypatch.setattr(agent_cfg, "_deep_merge", _agent_deep_merge, raising=False)
    return install


def _local(monkeypatch, tmp_path, data):
    """The profile's config.yaml (raw view) and WebUI's cache hold ``data``."""
    _write_profile_config(tmp_path / "profile" / "config.yaml", data)
    _bind(monkeypatch, data)


LEGACY_SIBLING = dict(_config("openai", PLAIN, base_url=BUILTIN_URL),
                      custom_providers=[{"name": "openai", "base_url": NAMED_URL, "api_key": NAMED_KEY}])


@pytest.mark.parametrize("local, overlay", [
    (_sibling_named("openai"), {"providers": {"myproxy": {"name": "private-proxy"}}}),
    (LEGACY_SIBLING, {"custom_providers": []}),  # a managed list replaces the profile's
], ids=["managed-rename", "managed-list-replaces"])
def test_a_managed_overlay_that_removes_the_sibling_claim_is_not_refused(monkeypatch, tmp_path, managed, local, overlay):
    """Codex r5: the Agent sees one 'openai' claimant, so WebUI must not refuse."""
    _local(monkeypatch, tmp_path, local)
    assert config.provider_name_ambiguity_verdict("openai")["reason"] == REASON
    managed(overlay)
    assert config.provider_name_ambiguity_verdict("openai") is None
    _both_route("openai", _agent_runtime(BUILTIN_URL, BUILTIN_KEY), ("openai", BUILTIN_URL, BUILTIN_KEY),
                configured_base_url=BUILTIN_URL)


@pytest.mark.parametrize("name", ["openai", "${PROXY_NAME}"])
def test_a_managed_overlay_that_adds_a_sibling_claim_is_refused(monkeypatch, tmp_path, managed, name):
    """Converse: the profile's names are distinct, the managed one is 'openai' (or a
    ``${VAR}`` the Agent expands, whatever WebUI's environment says it is)."""
    monkeypatch.setenv("PROXY_NAME", "unrelated")
    _local(monkeypatch, tmp_path, _sibling_named("private-proxy"))
    managed({"providers": {"myproxy": {"name": name}}})
    assert config.provider_name_ambiguity_verdict("openai")["reason"] == REASON
    _both_refused("openai", _agent_runtime(BUILTIN_URL, NAMED_KEY), "'myproxy'", configured_base_url=BUILTIN_URL)


@pytest.mark.parametrize("local, overlay, refused", [
    (_sibling_named("openai"), {"providers": {"myproxy": {"name": "private-proxy"}}}, False),
    (_sibling_named("private-proxy"), {"providers": {"myproxy": {"name": "openai"}}}, True),
], ids=["managed-rename", "managed-sibling"])
def test_the_title_guard_reads_the_agents_profile_with_its_managed_scope(
        monkeypatch, aux_calls, tmp_path, managed, local, overlay, refused):
    _title_route(monkeypatch, {"provider": "openai"}, PLAIN)
    _agent_scope(monkeypatch, aux_calls, tmp_path, local["providers"])
    managed(overlay)
    raw, status = streaming.generate_title_raw_via_aux("hello there", "hi")
    assert (status == TITLE_REFUSED) is refused
    assert bool(aux_calls) is not refused


@pytest.mark.parametrize("local, refused", [(_sibling_named("openai"), True), (_sibling_named("private-proxy"), False)])
def test_an_agent_without_managed_scope_keeps_the_profile_verdict(monkeypatch, tmp_path, managed, local, refused):
    """Older Agents have no ``hermes_cli.managed_scope``: no overlay, verdicts as before."""
    _local(monkeypatch, tmp_path, local)
    managed({"providers": {"myproxy": {"name": "openai" if not refused else "private-proxy"}}})
    monkeypatch.setitem(sys.modules, "hermes_cli.managed_scope", None)  # import -> ImportError
    assert (config.provider_name_ambiguity_verdict("openai") is not None) is refused


def test_the_managed_overlay_mutates_neither_the_cache_nor_the_raw_config(monkeypatch, tmp_path, managed):
    import copy

    local = _sibling_named("openai")
    _local(monkeypatch, tmp_path, local)
    before = copy.deepcopy(local)
    raw_before = copy.deepcopy(config._load_yaml_config_file_raw(tmp_path / "profile" / "config.yaml"))
    managed({"providers": {"myproxy": {"name": "private-proxy"}, "extra": {"name": "x"}}})
    assert config.provider_name_ambiguity_verdict("openai") is None
    assert config.provider_name_ambiguity_verdict("openai", config=local) is None
    assert local == before
    raw_cached = config._load_yaml_config_file_raw(tmp_path / "profile" / "config.yaml", _copy=False)
    assert raw_cached == raw_before


def test_identity_sources_are_detached_from_the_live_config_cache(monkeypatch):
    """Release-lane tag-delta audit (r1103): the cached view must be a snapshot.

    ``get_config()`` returns the shared cache dict, which another profile's reload refills in
    place while this check runs. A retained reference then judged this profile's provider name
    against the other profile's records and refused a valid send (reproduced over loopback HTTP
    with two profiles). ``get_config_snapshot()`` copies under the config lock.
    """
    data = _config("openai", {"custom:openai": {"base_url": NAMED_URL, "api_key": NAMED_KEY}})
    _bind(monkeypatch, data)
    cached = config._provider_identity_sources(None)[-1]
    other = _config("openai", {"openai": {"base_url": BUILTIN_URL, "api_key": BUILTIN_KEY}})
    data.clear()
    data.update(other)
    providers = (cached or {}).get("providers") or {}
    assert "custom:openai" in providers, cached
    assert "openai" not in providers, cached
