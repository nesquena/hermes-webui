"""#8822: the context-length lookup sends a key only where its own source sends it.

The lookup hands (URL, key, provider) to the Agent's ``get_model_context_length``. The Agent contacts the URL's
host, plus fixed metadata hosts for some providers (``_CONTEXT_LOOKUP_FIXED_HOSTS``). A key may go only to hosts
its own config source owns: every URL that source declares (``api``, ``url`` or ``base_url``), plus the provider's
fixed hosts when the source is that provider's own (a built-in record, the model block configured for it, or a
caller key that came with no URL). Otherwise the lookup sends no key. It never changes the URL, and a key that isn't
a string (a ``key_cmd`` token source) passes through untouched.

The two streaming sites ask the lookup for the config key first, as v0.52.544 did, and offer the Agent's own key
only as its fallback, under the same rule; nothing is added after the lookup.

Most of these tests need no Agent. The ``requires_agent`` group probes the installed Agent's own metadata lookup
under a no-network request recorder.
"""
import base64
import json
import pathlib
import re
import textwrap
import types
import urllib.parse

import pytest

from api import routes
from tests.conftest import requires_agent

REPO = pathlib.Path(__file__).resolve().parent.parent
STREAMING_PY = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")

A = "https://a.model-url.test/v1"
B = "https://b.record-url.test/v1"
L = "https://l.legacy-url.test/v1"
C = "https://c.caller-url.test/v1"


def _lookup(cfg, provider, **kw):
    cfg.setdefault("providers", {})
    cfg.setdefault("custom_providers", [])
    return routes._context_length_lookup_inputs_for_model("m-x", provider, cfg=cfg, **kw)


# ── The rule at the lookup ─────────────────────────────────────────────────────


def test_a_native_record_keeps_its_own_key_on_its_own_url():
    got = _lookup({"model": {"provider": "custom:my-proxy"},
                   "providers": {"custom:my-proxy": {"base_url": B, "api_key": "KB"}}}, "custom:my-proxy")
    assert (got.base_url, got.api_key) == (B, "KB")


def test_the_model_url_never_carries_a_native_record_key():
    got = _lookup({"model": {"provider": "custom:my-proxy", "base_url": A},
                   "providers": {"custom:my-proxy": {"api_key": "KB"}}}, "custom:my-proxy")
    assert got.base_url == A
    assert got.api_key == ""


def test_the_model_block_keeps_its_own_pair():
    got = _lookup({"model": {"provider": "custom:my-proxy", "base_url": A, "api_key": "KA"}}, "custom:my-proxy")
    assert (got.base_url, got.api_key) == (A, "KA")


def test_a_native_record_url_never_carries_a_legacy_key():
    got = _lookup({"model": {"provider": "custom:my-proxy"},
                   "providers": {"custom:my-proxy": {"base_url": B}},
                   "custom_providers": [{"name": "my-proxy", "api_key": "KL"}]}, "custom:my-proxy")
    assert got.base_url == B
    assert got.api_key == ""


def test_a_legacy_entry_keeps_its_own_pair():
    got = _lookup({"model": {"provider": "custom:my-proxy"},
                   "custom_providers": [{"name": "my-proxy", "base_url": L, "api_key": "KL"}]}, "custom:my-proxy")
    assert (got.base_url, got.api_key) == (L, "KL")


@pytest.mark.parametrize("field", ["api", "url", "base_url"])
def test_a_native_record_owns_its_url_in_every_spelling(field):
    """WebUI and the Agent read a record's URL from ``api``, ``url`` or ``base_url``; all are the record's own."""
    got = _lookup({"model": {"provider": "custom:my-proxy", "base_url": B},
                   "providers": {"custom:my-proxy": {field: B, "api_key": "KB"}}}, "custom:my-proxy")
    assert (got.base_url, got.api_key) == (B, "KB")


@pytest.mark.parametrize("field", ["api", "url"])
def test_a_legacy_entry_owns_its_url_in_every_spelling(field):
    got = _lookup({"model": {"provider": "custom:my-proxy", "base_url": L},
                   "custom_providers": [{"name": "my-proxy", field: L, "api_key": "KL"}]}, "custom:my-proxy")
    assert (got.base_url, got.api_key) == (L, "KL")


def test_a_record_spelling_owns_only_its_own_url():
    got = _lookup({"model": {"provider": "custom:my-proxy", "base_url": A},
                   "providers": {"custom:my-proxy": {"api": B, "api_key": "KB"}}}, "custom:my-proxy")
    assert got.base_url == A
    assert got.api_key == ""


def test_a_caller_key_owns_the_callers_url():
    got = _lookup({"model": {"provider": "custom:my-proxy", "base_url": A}}, "custom:my-proxy",
                  base_url=C, api_key="KC")
    assert (got.base_url, got.api_key) == (C, "KC")


@pytest.mark.parametrize("follows_url", [False, True])
def test_a_caller_key_with_a_url_owns_only_that_url(follows_url, monkeypatch):
    """A pair with a URL can carry another source's key under the provider's label (a model alias's own endpoint,
    or the Agent's pair after a fallback switched its endpoint and key), so it owns its URL, not the provider's
    fixed hosts. An Agent whose Codex catalog ignores the URL would also send the key to chatgpt.com."""
    monkeypatch.setattr(routes, "_agent_codex_catalog_follows_url", lambda: follows_url)
    got = _lookup({"model": {"provider": "openai-codex"}}, "openai-codex", base_url=C, api_key="K-ALIAS")
    assert got.base_url == C
    assert got.api_key == ("K-ALIAS" if follows_url else "")


def test_a_caller_key_without_a_url_owns_its_providers_fixed_host():
    got = _lookup({"model": {"provider": "anthropic"}}, "anthropic", api_key="KC")
    assert (got.base_url, got.api_key) == ("", "KC")


class _TokenSource:
    """Shaped like the Agent's ``CommandTokenSource``: a credential minted on use, never a string."""

    def __call__(self):
        return "minted-token"

    def __repr__(self):
        return "<TokenSource object at 0x0>"


def test_a_non_string_caller_key_passes_through_unchanged():
    source = _TokenSource()
    got = _lookup({"model": {"provider": "custom:my-proxy"}}, "custom:my-proxy", base_url=C, api_key=source)
    assert got.api_key is source


def test_a_non_string_fallback_key_passes_through_unchanged():
    source = _TokenSource()
    got = _lookup({"model": {"provider": "custom:my-proxy"}}, "custom:my-proxy", base_url=C,
                  fallback_api_key=source, fallback_base_url=C)
    assert got.api_key is source


def test_the_config_key_that_owns_the_url_wins_over_the_fallback():
    got = _lookup({"model": {"provider": "custom:foo"}, "providers": {"custom:foo": {"base_url": C, "api_key": "KA"}}},
                  "custom:foo", base_url=C, fallback_api_key="AGENT-KEY", fallback_base_url=C)
    assert (got.base_url, got.api_key) == (C, "KA")


def test_a_fallback_key_never_lands_on_a_url_it_did_not_come_with():
    got = _lookup({"model": {"provider": "custom:my-proxy", "base_url": A}}, "custom:my-proxy",
                  fallback_api_key="AGENT-KEY", fallback_base_url=C)
    assert got.base_url == A
    assert got.api_key == ""


@pytest.mark.parametrize("agent_provider, sent", [("anthropic", "AGENT-KEY"), ("deepseek", ""), ("", "")])
def test_a_fallback_key_without_a_url_owns_its_own_providers_fixed_hosts(agent_provider, sent):
    """Not the session label's: after a fallback the Agent's key belongs to the Agent's provider."""
    got = _lookup({"model": {"provider": "anthropic"}}, "anthropic",
                  fallback_api_key="AGENT-KEY", fallback_base_url="", fallback_provider=agent_provider)
    assert (got.base_url, got.api_key) == ("", sent)


def test_a_fallback_key_under_another_label_does_not_reach_that_labels_fixed_host():
    got = _lookup({"model": {"provider": "openai-codex"}}, "openai-codex",
                  fallback_api_key=_jwt("AGENT-KEY"), fallback_base_url="", fallback_provider="anthropic")
    assert got.api_key == ""


def test_the_agent_fallback_is_one_source():
    agent = types.SimpleNamespace(api_key="AGENT-KEY", base_url="", provider="anthropic")
    assert routes._context_lookup_agent_fallback(agent, "K-SESSION", C, "openai-codex") == dict(
        fallback_api_key="AGENT-KEY", fallback_base_url="", fallback_provider="anthropic")
    keyless = types.SimpleNamespace(api_key="", base_url=A, provider="anthropic")
    assert routes._context_lookup_agent_fallback(keyless, "K-SESSION", C, "openai-codex") == dict(
        fallback_api_key="K-SESSION", fallback_base_url=C, fallback_provider="openai-codex")


@pytest.mark.parametrize("filled", ["model", "record"])
def test_a_caller_key_without_a_url_is_never_paired_with_a_filled_url(filled):
    """The GET /api/session entry passes a key with no URL; the lookup then fills one from another source."""
    cfg = {"model": {"provider": "custom:my-proxy"}}
    if filled == "model":
        cfg["model"]["base_url"] = A
    else:
        cfg["providers"] = {"custom:my-proxy": {"base_url": B}}
    got = _lookup(cfg, "custom:my-proxy", api_key="KL")
    assert got.base_url == (A if filled == "model" else B)
    assert got.api_key == ""


def test_the_url_is_never_changed():
    for cfg, provider, kw in [
        ({"model": {"provider": "custom:my-proxy", "base_url": A},
          "providers": {"custom:my-proxy": {"api_key": "KB"}}}, "custom:my-proxy", {}),
        ({"model": {"provider": "custom:my-proxy"}}, "custom:my-proxy", {"base_url": C, "api_key": "KC"}),
        ({"model": {"provider": "anthropic"},
          "custom_providers": [{"name": "anthropic", "api_key": "KL"}]}, "anthropic", {}),
    ]:
        got = _lookup(cfg, provider, **kw)
        assert got.base_url in (A, B, C, L, "")


# ── GET /api/session assembles the caller pair under the same rule ─────────────


def _session_state(cfg, provider, monkeypatch):
    """Bind ``cfg`` for the real resolvers and return (base_url, api_key) as GET /api/session hands them on."""
    import api.config as config

    cfg.setdefault("providers", {})
    cfg.setdefault("custom_providers", [])
    monkeypatch.setattr(config, "cfg", cfg)
    monkeypatch.setattr(config, "get_config", lambda *a, **k: cfg)
    _model, _provider, base_url, api_key = routes._session_context_length_lookup_state("m-x", provider)
    return base_url, api_key


@pytest.mark.parametrize("cfg", [
    {"model": {"provider": "custom:my-proxy", "base_url": A}, "providers": {"custom:my-proxy": {"api_key": "KB"}}},
    {"model": {"provider": "custom:my-proxy", "base_url": A},
     "providers": {"custom:my-proxy": {"base_url": B, "api_key": "KB"}}},
    {"model": {"provider": "custom:my-proxy", "base_url": A},
     "custom_providers": [{"name": "my-proxy", "base_url": L, "api_key": "KL"}]},
], ids=["record-key-only", "record-url", "legacy-entry"])
def test_a_session_reload_never_pairs_a_records_key_with_the_model_url(cfg, monkeypatch):
    assert _session_state(cfg, "custom:my-proxy", monkeypatch) == (A, "")


def test_a_session_reload_keeps_a_records_key_on_its_own_url(monkeypatch):
    cfg = {"model": {"provider": "custom:my-proxy"}, "providers": {"custom:my-proxy": {"base_url": B, "api_key": "KB"}}}
    assert _session_state(cfg, "custom:my-proxy", monkeypatch) == (B, "KB")


def test_a_session_reload_keeps_a_records_key_when_the_model_url_is_the_records_own(monkeypatch):
    cfg = {"model": {"provider": "custom:my-proxy", "base_url": B},
           "providers": {"custom:my-proxy": {"base_url": B.upper(), "api_key": "KB"}}}
    base_url, api_key = _session_state(cfg, "custom:my-proxy", monkeypatch)
    assert (routes._context_lookup_origin(base_url), api_key) == (routes._context_lookup_origin(B), "KB")


def test_a_session_reload_still_resolves_a_records_env_key(monkeypatch):
    """The record's key is resolved here, not at the lookup: a ``${VAR}`` key still travels with the record's URL."""
    monkeypatch.setenv("MY_PROXY_TEST_KEY", "KB-env")
    cfg = {"model": {"provider": "custom:my-proxy"},
           "providers": {"custom:my-proxy": {"base_url": B, "api_key": "${MY_PROXY_TEST_KEY}"}}}
    assert _session_state(cfg, "custom:my-proxy", monkeypatch) == (B, "KB-env")


# ── Fixed metadata hosts ────────────────────────────────────────────────────────


def test_a_builtin_record_key_may_reach_its_providers_fixed_host():
    got = _lookup({"model": {"provider": "anthropic"}, "providers": {"anthropic": {"api_key": "KB"}}}, "anthropic")
    assert (got.base_url, got.api_key) == ("", "KB")


def test_a_legacy_key_never_reaches_a_providers_fixed_host():
    got = _lookup({"model": {"provider": "anthropic"},
                   "custom_providers": [{"name": "anthropic", "api_key": "KL"}]}, "anthropic")
    assert got.base_url == ""
    assert got.api_key == ""


@pytest.mark.parametrize("follows_url", [False, True])
def test_a_key_with_its_own_url_still_owns_codex_s_fixed_host_only_if_it_is_codex_s_own(follows_url, monkeypatch):
    monkeypatch.setattr(routes, "_agent_codex_catalog_follows_url", lambda: follows_url)
    own = _lookup({"model": {"provider": "openai-codex"},
                   "providers": {"openai-codex": {"base_url": B, "api_key": "KB"}}}, "openai-codex")
    assert (own.base_url, own.api_key) == (B, "KB")
    legacy = _lookup({"model": {"provider": "openai-codex"},
                      "custom_providers": [{"name": "openai-codex", "base_url": L, "api_key": "KL"}]}, "openai-codex")
    assert legacy.base_url == L
    # An Agent whose Codex catalog ignores the URL also sends the key to chatgpt.com, which the legacy entry
    # doesn't own; one that follows the URL sends it only to the entry's own URL.
    assert legacy.api_key == ("KL" if follows_url else "")


def test_the_fixed_host_table_names_every_measured_host():
    table = routes._CONTEXT_LOOKUP_FIXED_HOSTS
    assert table["anthropic"]["no_url"] == ("api.anthropic.com",)
    assert table["novita"]["no_url"] == ("api.novita.ai",)
    assert table["copilot"]["no_url"] == table["copilot-acp"]["no_url"] == ("api.githubcopilot.com",)
    assert table["copilot"]["with_url"] == table["copilot-acp"]["with_url"] == ("api.githubcopilot.com",)
    assert table["copilot"]["with_url_unless"] == table["copilot-acp"]["with_url_unless"] == (
        "_agent_copilot_lookup_follows_url")
    assert table["openai-codex"]["no_url"] == table["openai-codex"]["with_url"] == ("chatgpt.com",)


# ── The Copilot row with a URL (#8822 d-i) ─────────────────────────────────────
#
# Older Agents call their Copilot live-models step with the key alone once the lookup reaches the
# provider-specific step, so a Copilot id's key (or one for an id the Agent infers as Copilot from the URL)
# reaches api.githubcopilot.com even with a URL set. The row applies only on such Agents and only when the
# Agent's own early-return check lets the lookup reach that step. Fake Agent modules stand in below, so these
# tests need no Agent; the installed-Agent tests further down check the same helpers against real Agents.

K = "https://api.known-provider.test/v1"   # a URL the fake Agent treats as a known provider's: the step is reached
G = "https://models.github-inferred.test/v1"   # a URL the fake Agent infers as Copilot


def _fake_agent(monkeypatch, *, copilot_step=True, predicates=True, raising=False, infer=None):
    """Install stand-in ``agent.model_metadata`` and ``hermes_cli.models`` modules for one test.

    ``predicates=False`` leaves out the Agent's early-return check; ``raising=True`` makes it raise.
    ``copilot_step`` decides whether ``get_copilot_model_context`` exists. ``infer`` maps URL -> inferred id.
    """
    import sys

    mm = types.ModuleType("agent.model_metadata")
    if predicates:
        def _boom(url):
            raise RuntimeError("predicate failed")

        mm._is_custom_endpoint = _boom if raising else (lambda url: url not in (K, G))
        mm._is_known_provider_base_url = _boom if raising else (lambda url: url in (K, G))
    mm._infer_provider_from_url = lambda url: (infer or {}).get(url, "")
    agent_pkg = types.ModuleType("agent")
    agent_pkg.model_metadata = mm
    hm = types.ModuleType("hermes_cli.models")
    if copilot_step:
        hm.get_copilot_model_context = lambda *a, **k: None
    cli_pkg = types.ModuleType("hermes_cli")
    cli_pkg.models = hm
    for name, module in (("agent", agent_pkg), ("agent.model_metadata", mm),
                         ("hermes_cli", cli_pkg), ("hermes_cli.models", hm)):
        monkeypatch.setitem(sys.modules, name, module)


def _no_agent(monkeypatch):
    import sys

    for name in ("agent", "agent.model_metadata", "hermes_cli", "hermes_cli.models"):
        monkeypatch.setitem(sys.modules, name, None)   # importing it now raises ImportError


def _copilot_caller_key(url):
    return _lookup({"model": {"provider": "copilot"}}, "copilot", base_url=url, api_key="K-CALLER")


def test_an_older_agent_drops_a_copilot_key_whose_url_reaches_the_copilot_step(monkeypatch):
    _fake_agent(monkeypatch)
    got = _copilot_caller_key(K)
    assert got.base_url == K
    assert got.api_key == ""


def test_an_older_agent_keeps_a_copilot_key_on_a_custom_endpoint_it_returns_early_for(monkeypatch):
    """The narrowing: at a truly custom endpoint the Agent returns before the Copilot step, so the key goes
    only to that URL, which it owns. Also the control that keeps the fail-closed tests below from passing
    vacuously."""
    _fake_agent(monkeypatch)
    got = _copilot_caller_key(C)
    assert (got.base_url, got.api_key) == (C, "K-CALLER")


def test_a_newer_agent_keeps_a_copilot_key_on_its_own_url(monkeypatch):
    _fake_agent(monkeypatch, copilot_step=False)
    for url in (C, K):
        got = _copilot_caller_key(url)
        assert (got.base_url, got.api_key) == (url, "K-CALLER")


@pytest.mark.parametrize("broken", ["no-agent", "no-predicates", "predicate-raises"])
def test_the_copilot_row_fails_closed_when_the_agents_checks_cant_be_read(broken, monkeypatch):
    """Condition 1 (auditor, STB8822DIACK): when the row's inputs can't be read (no importable Agent, the
    early-return predicates missing, or one raising), the row applies and the key is dropped, even at a URL the
    real check would have let it keep (C, as in the control above)."""
    if broken == "no-agent":
        _no_agent(monkeypatch)
        assert routes._agent_copilot_lookup_follows_url() is False
    else:
        _fake_agent(monkeypatch, predicates=(broken != "no-predicates"), raising=(broken == "predicate-raises"))
    assert routes._agent_reaches_provider_step_with_url("copilot", C) is True
    got = _copilot_caller_key(C)
    assert got.base_url == C
    assert got.api_key == ""


def test_the_copilot_detector_reads_the_agents_copilot_step(monkeypatch):
    """The detector says "follows the URL" only when ``hermes_cli.models`` imports and lacks the Copilot step.
    If it can't be imported, it fails closed (False). An Agent that keeps the step under a new name is the
    absence-detector risk (#8822 note §5); the installed-Agent tests below catch it."""
    _fake_agent(monkeypatch, copilot_step=True)
    assert routes._agent_copilot_lookup_follows_url() is False
    _fake_agent(monkeypatch, copilot_step=False)
    assert routes._agent_copilot_lookup_follows_url() is True
    _no_agent(monkeypatch)
    assert routes._agent_copilot_lookup_follows_url() is False


@pytest.mark.parametrize("provider", ["", "custom", "openrouter"])
def test_an_id_the_agent_infers_as_copilot_gets_the_copilot_row(provider, monkeypatch):
    """For the ids the Agent re-infers from the URL, a URL it infers as Copilot reaches the Copilot step on an
    older Agent; one it infers as nothing keeps the key on its own URL."""
    _fake_agent(monkeypatch, infer={G: "copilot"})
    got = _lookup({"model": {"provider": provider or "custom"}}, provider, base_url=G, api_key="K-CALLER")
    assert got.base_url == G
    assert got.api_key == ""
    _fake_agent(monkeypatch, infer={})
    got = _lookup({"model": {"provider": provider or "custom"}}, provider, base_url=G, api_key="K-CALLER")
    assert (got.base_url, got.api_key) == (G, "K-CALLER")


# ── The two streaming call sites that used to fall back to the Agent's key ─────


def _site_blocks():
    lines = STREAMING_PY.splitlines()
    starts = [i for i, line in enumerate(lines)
              if line.strip() == "from api.routes import _context_length_lookup_inputs_for_model"]
    blocks = []
    for i in starts:
        j = next(k for k in range(i, len(lines)) if lines[k].strip().startswith("_cfg_provider ="))
        blocks.append(textwrap.dedent("\n".join(lines[i:j + 1])))
    return blocks


def test_both_sites_are_found():
    assert len(_site_blocks()) == 2


@pytest.mark.parametrize("site", [0, 1])
@pytest.mark.parametrize("config_key", [False, True])
def test_a_site_never_sends_the_agents_key_to_a_url_the_agent_doesnt_own(site, config_key, monkeypatch):
    """The Agent has no URL; the lookup fills the model URL and drops the key. The site must send no key."""
    cfg = {"model": {"provider": "custom:my-proxy", "base_url": A}, "providers": {}, "custom_providers": []}
    if config_key:
        cfg["providers"]["custom:my-proxy"] = {"api_key": "KB"}
    import api.config as api_config
    monkeypatch.setattr(api_config, "get_config", lambda *a, **k: cfg, raising=False)
    ns = dict(agent=types.SimpleNamespace(model="m-x", base_url="", api_key="AGENT-KEY"),
              resolved_model="m-x", resolved_provider="custom:my-proxy", resolved_base_url="",
              resolved_api_key="", _cfg=cfg)
    exec(_site_blocks()[site], ns)
    assert ns["_cfg_base_url"] == A
    assert ns["_cfg_api_key"] == ""


@pytest.mark.parametrize("site", [0, 1])
def test_a_site_still_sends_the_agents_own_pair(site, monkeypatch):
    cfg = {"model": {"provider": "custom:my-proxy", "base_url": A}, "providers": {}, "custom_providers": []}
    import api.config as api_config
    monkeypatch.setattr(api_config, "get_config", lambda *a, **k: cfg, raising=False)
    ns = dict(agent=types.SimpleNamespace(model="m-x", base_url=C, api_key="AGENT-KEY"),
              resolved_model="m-x", resolved_provider="custom:my-proxy", resolved_base_url="",
              resolved_api_key="", _cfg=cfg)
    exec(_site_blocks()[site], ns)
    assert (ns["_cfg_base_url"], ns["_cfg_api_key"]) == (C, "AGENT-KEY")


@pytest.mark.parametrize("site", [0, 1])
def test_a_site_prefers_the_config_key_that_owns_the_url(site, monkeypatch):
    """#8816 round 7: the runtime bundle can pair record A's URL with record B's key (a shared-name credential
    pool). The config key that owns A's URL wins, as on v0.52.544; the Agent's key is only the fallback."""
    cfg = {"model": {"provider": "custom:foo"}, "custom_providers": [],
           "providers": {"myproxy": {"name": "foo", "base_url": B, "api_key": "KB"},
                         "custom:foo": {"name": "foo", "base_url": A, "api_key": "KA"}}}
    import api.config as api_config
    monkeypatch.setattr(api_config, "get_config", lambda *a, **k: cfg, raising=False)
    ns = dict(agent=types.SimpleNamespace(model="m-x", base_url=A, api_key="KB"),
              resolved_model="m-x", resolved_provider="custom:foo", resolved_base_url=A,
              resolved_api_key="KB", _cfg=cfg)
    exec(_site_blocks()[site], ns)
    assert (ns["_cfg_base_url"], ns["_cfg_api_key"]) == (A, "KA")


@pytest.mark.parametrize("site", [0, 1])
@pytest.mark.parametrize("follows_url", [False, True])
def test_a_site_under_another_label_sends_the_agents_key_only_where_it_owns_every_host(site, follows_url,
                                                                                       monkeypatch):
    """The Agent's pair after a fallback (or a model alias's own endpoint) keeps the session's provider label.
    Under ``openai-codex`` an Agent whose Codex catalog ignores the URL also contacts chatgpt.com, which that key
    doesn't own, so it goes nowhere; an Agent that follows the URL gets it at its own URL only."""
    monkeypatch.setattr(routes, "_agent_codex_catalog_follows_url", lambda: follows_url)
    cfg = {"model": {"provider": "openai-codex"}, "providers": {}, "custom_providers": []}
    import api.config as api_config
    monkeypatch.setattr(api_config, "get_config", lambda *a, **k: cfg, raising=False)
    ns = dict(agent=types.SimpleNamespace(model="m-x", base_url=C, api_key="K-OTHER", provider="deepseek"),
              resolved_model="m-x", resolved_provider="openai-codex", resolved_base_url=C,
              resolved_api_key="K-OTHER", _cfg=cfg)
    exec(_site_blocks()[site], ns)
    assert ns["_cfg_base_url"] == C
    assert ns["_cfg_api_key"] == ("K-OTHER" if follows_url else "")


@pytest.mark.parametrize("site", [0, 1])
def test_a_site_never_pairs_the_agents_key_with_the_sessions_url(site, monkeypatch):
    """After a fallback to an Agent with no URL, the lookup URL is still the session's; the Agent's key isn't its."""
    cfg = {"model": {"provider": "custom:my-proxy"}, "providers": {}, "custom_providers": []}
    import api.config as api_config
    monkeypatch.setattr(api_config, "get_config", lambda *a, **k: cfg, raising=False)
    ns = dict(agent=types.SimpleNamespace(model="m-x", base_url="", api_key="AGENT-KEY", provider="deepseek"),
              resolved_model="m-x", resolved_provider="custom:my-proxy", resolved_base_url=A,
              resolved_api_key="K-SESSION", _cfg=cfg)
    exec(_site_blocks()[site], ns)
    assert ns["_cfg_base_url"] == A
    assert ns["_cfg_api_key"] == ""


def _site_calls():
    """Each site's block from its Agent import through its ``get_model_context_length`` call."""
    blocks = []
    for anchor, first, end, indent in (
        ("Fallback: if the compressor didn't report a context_length",
         "from agent.model_metadata import get_model_context_length\n", "\n                        if _resolved_cl:", 24),
        ("Fallback: when the compressor is absent or reports context_length=0",
         "from agent.model_metadata import get_model_context_length as _get_cl", "\n                    if _fb_cl:", 20),
    ):
        a = STREAMING_PY.index(anchor)
        b = STREAMING_PY.index(first, a)
        e = STREAMING_PY.index(end, b)
        blocks.append(textwrap.dedent(" " * indent + STREAMING_PY[b:e]))
    return blocks


@pytest.mark.parametrize("site", [0, 1])
def test_the_agent_receives_a_token_source_itself_not_its_repr(site, monkeypatch):
    import sys

    seen = {}

    def get_model_context_length(model, base_url="", *args, api_key=None, **kwargs):
        seen.update(base_url=base_url, api_key=api_key)
        return 8192

    stub = types.ModuleType("agent.model_metadata")
    stub.get_model_context_length = get_model_context_length
    monkeypatch.setitem(sys.modules, "agent.model_metadata", stub)
    cfg = {"model": {"provider": "custom", "base_url": C}, "providers": {}, "custom_providers": []}
    import api.config as api_config
    monkeypatch.setattr(api_config, "get_config", lambda *a, **k: cfg, raising=False)
    source = _TokenSource()
    ns = dict(agent=types.SimpleNamespace(model="m-x", base_url=C, api_key=source, provider="custom"),
              resolved_model="m-x", resolved_provider="custom", resolved_base_url=C, resolved_api_key=source,
              _cfg=cfg)
    exec(_site_calls()[site], ns)
    assert seen["base_url"] == C
    assert seen["api_key"] is source


def test_no_site_falls_back_to_the_agents_key_after_the_lookup():
    assert not re.search(r"_ctx_lookup\.api_key\s+or\s+getattr\(agent,\s*'api_key'", STREAMING_PY)


# ── Against the installed Agent: every host it contacts must be one the table knows ─────────


def _jwt(sub):
    def b64(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{b64({'alg': 'none'})}.{b64({'sub': sub})}.sig"


@pytest.fixture
def recorded_agent_requests(monkeypatch, tmp_path):
    """Record every request the Agent's metadata lookup makes; nothing leaves the process.

    ``state["mode"]``: "miss" answers 404, "hit" answers a models list (so every probe path runs).
    The Agent gets its own throwaway ``HERMES_HOME`` so each probe can start from an empty disk cache.
    """
    import email.message
    import io
    import socket
    import urllib.error
    import urllib.request

    import httpx
    import requests

    agent_home = (tmp_path / "agent-home").resolve()
    agent_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(agent_home))
    state = {"mode": "miss", "seen": [], "home": agent_home, "blocked": 0}
    payload = {"data": [{"id": "m-x", "context_length": 8192,
                         "capabilities": {"limits": {"max_prompt_tokens": 8192}}}],
               "models": [{"slug": "m-x", "context_window": 8192}]}

    def note(url, headers):
        # Every header value and the URL itself: a key can travel under any header name or in a query string.
        values = [str(v) for v in dict(headers or {}).values()]
        state["seen"].append((urllib.parse.urlsplit(str(url)).hostname or "", values, str(url)))

    def req(self, method, url, *a, **kw):
        headers = dict(self.headers)
        headers.update(kw.get("headers") or {})
        note(url, headers)
        r = requests.Response()
        r.status_code = 200 if state["mode"] == "hit" else 404
        r._content = json.dumps(payload if state["mode"] == "hit" else {}).encode()
        r.headers["content-type"] = "application/json"
        r.url = str(url)
        return r

    def hx(self, request, *a, **kw):
        note(request.url, request.headers)
        return httpx.Response(200 if state["mode"] == "hit" else 404,
                              json=payload if state["mode"] == "hit" else {}, request=request)

    async def ahx(self, request, *a, **kw):
        return hx(self, request)

    def ul(self, fullurl, *a, **kw):
        if isinstance(fullurl, urllib.request.Request):
            url, headers = fullurl.full_url, dict(fullurl.header_items())
        else:
            url, headers = str(fullurl), {}
        note(url, headers)
        if state["mode"] == "miss":
            raise urllib.error.HTTPError(url, 404, "probe miss", email.message.Message(), None)
        return _Answer(json.dumps(payload).encode())

    class _Answer(io.BytesIO):
        status = code = 200
        headers: dict = {}

        def getcode(self):
            return 200

    def no_connect(self, address):
        # A request that reaches a socket bypassed the recorded clients, so its key and host went unseen.
        state["blocked"] += 1
        raise OSError("network disabled by test")

    def fake_dns(host, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(requests.sessions.Session, "request", req)
    monkeypatch.setattr(httpx.Client, "send", hx)
    monkeypatch.setattr(httpx.AsyncClient, "send", ahx)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", ul)
    monkeypatch.setattr(socket, "getaddrinfo", fake_dns)
    monkeypatch.setattr(socket.socket, "connect", no_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", no_connect)
    return state


def _agent_keyed_hosts(state, url, key, provider):
    """Hosts the installed Agent sends ``key`` to for (url, provider), over both endpoint modes.

    Each probe starts cold, like the recorder harness: in-memory caches cleared and the Agent's own
    ``HERMES_HOME`` (a per-test temp dir, see the fixture) emptied, since the Agent keeps context lengths and
    endpoint metadata on disk there. Without that, a second probe answers from cache and contacts nothing.
    """
    import shutil

    import agent.model_metadata as mm
    import hermes_cli.models as hm
    from hermes_constants import get_hermes_home

    home = pathlib.Path(get_hermes_home()).resolve()
    assert state["home"] in (home, *home.parents), f"the Agent's HERMES_HOME {home} is not the test's temp dir"
    hosts = set()
    for mode in ("miss", "hit"):
        for module in (mm, hm):
            for name, value in list(vars(module).items()):
                if "cache" not in name.lower() or name.isupper():
                    continue
                if isinstance(value, (dict, set, list)):
                    value.clear()
                elif isinstance(value, (int, float)) and not isinstance(value, bool):
                    # A cache's timestamp: older Agents keep the GitHub model catalog behind a TTL checked against
                    # ``time.monotonic()``, and an emptied catalog with a fresh timestamp reads as "cached empty",
                    # so a second probe with the same key would contact nothing. -inf makes every TTL expired,
                    # whatever the host's uptime (a fresh CI runner can be younger than the TTL).
                    setattr(module, name, float("-inf") if "time" in name.lower() else 0)
        shutil.rmtree(home, ignore_errors=True)
        home.mkdir(parents=True)
        state["mode"] = mode
        state["seen"].clear()
        state["blocked"] = 0
        try:
            mm.get_model_context_length("m-x", url, api_key=key, provider=provider)
        except Exception:
            pass
        assert state["blocked"] == 0, (
            f"{provider} url={url or '-'}: a request bypassed the recorded clients, so where the key went is unknown"
        )
        hosts |= {host for host, values, full in state["seen"]
                  if key and (any(key in value for value in values) or key in full)}
    return hosts


@requires_agent
@pytest.mark.parametrize("shape", ["synthetic", "jwt"])
def test_a_legacy_codex_entry_s_kept_key_reaches_only_its_own_url(shape, recorded_agent_requests):
    """The lookup keeps a legacy ``openai-codex`` entry's key on its own URL only when the installed Agent's
    Codex probe follows that URL; whatever it keeps, the Agent must send nowhere else (e.g. chatgpt.com)."""
    key = "KL-synthetic" if shape == "synthetic" else _jwt("KL-synthetic")
    got = _lookup({"model": {"provider": "openai-codex"},
                   "custom_providers": [{"name": "openai-codex", "base_url": L, "api_key": key}]}, "openai-codex")
    assert got.base_url == L
    if got.api_key:
        assert _agent_keyed_hosts(recorded_agent_requests, got.base_url, got.api_key, got.provider) <= {
            urllib.parse.urlsplit(L).hostname}


@requires_agent
def test_the_recorder_sees_the_agents_requests(recorded_agent_requests):
    """Control: without it, an empty recording would make the coverage tests pass vacuously.

    The repeated ``novita`` probe proves each probe starts cold: a warm disk cache answers the second probe
    without contacting anything, which would let a coverage assertion pass on an empty set.
    """
    host = urllib.parse.urlsplit(C).hostname
    assert _agent_keyed_hosts(recorded_agent_requests, C, "K-synthetic", "custom:my-proxy") == {host}
    assert "api.anthropic.com" in _agent_keyed_hosts(recorded_agent_requests, "", "K-synthetic", "anthropic")
    for _ in range(2):
        assert "api.novita.ai" in _agent_keyed_hosts(recorded_agent_requests, "", "K-synthetic", "novita")


def _agent_provider_ids():
    """Every provider id the installed Agent knows, as the lookup would emit it, plus the URL-inferred ones."""
    ids = set(routes._CONTEXT_LOOKUP_FIXED_HOSTS) | {"", "custom", "openrouter", "custom:my-proxy"}
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY
        ids |= {str(name) for name in PROVIDER_REGISTRY}
    except Exception:
        pass
    try:
        import providers as agent_providers
        ids |= {str(getattr(p, "name", "") or "") for p in agent_providers.list_providers()}
    except Exception:
        pass
    return sorted({routes._canonical_context_provider(i) if i else "" for i in ids})


def _agent_known_hosts():
    """Hosts the installed Agent treats as known provider endpoints (not custom endpoints)."""
    import agent.model_metadata as mm

    hosts = {h.lstrip(".") for h in getattr(mm, "_URL_TO_PROVIDER", {})}
    try:
        import providers as agent_providers
        for p in agent_providers.list_providers():
            hosts |= {str(h).lstrip(".*") for h in (*getattr(p, "hostnames", ()), *getattr(p, "metadata_hostnames", ()))}
    except Exception:
        pass
    return sorted(h if h.count(".") >= 2 else f"api.{h}" for h in hosts if h)


def _is_copilot_with_url_row(provider, url, unlisted):
    """#8822 d-i: Copilot's live-models step reached with a URL set (older Agents only)."""
    return bool(url) and unlisted == {"api.githubcopilot.com"}


def _coverage_gaps(state, key):
    """(provider, url, unlisted hosts) for every probe where the Agent sent ``key`` to a host the table lacks."""
    known_hosts = _agent_known_hosts()
    inferred = {"", "custom", "openrouter"}
    gaps = []
    for provider in _agent_provider_ids():
        urls = ["", C, "https://api.openai.com/v1"]
        if provider in inferred:
            urls += [f"https://{h}/v1" for h in known_hosts]
        for url in urls:
            sent = _agent_keyed_hosts(state, url, key, provider)
            known = {o.rsplit(":", 1)[0] for o in routes._context_lookup_contacted_origins(provider, url)}
            if sent - known:
                gaps.append((provider, url, frozenset(sent - known)))
    return gaps


@requires_agent
@pytest.mark.parametrize("shape", ["synthetic", "jwt"])
def test_the_fixed_host_table_covers_the_installed_agent(shape, recorded_agent_requests):
    """Every host the installed Agent sends a key to is the URL's own host or listed in the table.

    Probed over the Agent's whole provider set (as the lookup emits each id) plus ``''``, ``custom`` and
    ``openrouter``, with no URL, a custom endpoint and a known provider host, and for the ids the Agent infers from
    a URL, every known host. The Copilot-with-a-URL rows are reported separately below.
    """
    key = "K-synthetic" if shape == "synthetic" else _jwt("K-synthetic")
    gaps = [g for g in _coverage_gaps(recorded_agent_requests, key) if not _is_copilot_with_url_row(*g)]
    assert not gaps, f"the Agent sent the key to hosts the table lacks: {sorted((p, u, sorted(h)) for p, u, h in gaps)}"


@requires_agent
@pytest.mark.parametrize("shape", ["synthetic", "jwt"])
def test_copilot_live_models_with_a_url_is_in_the_table(shape, recorded_agent_requests):
    """Older Agents (with Copilot's live-models step) send a Copilot id's key, or that of an id inferred from a
    GitHub URL, to api.githubcopilot.com even with a URL set. The d-i row must list every such host. This is also
    the backstop for the detector: an Agent that renames ``get_copilot_model_context`` but keeps the step makes
    the detector say "follows the URL", and fails here."""
    key = "K-synthetic" if shape == "synthetic" else _jwt("K-synthetic")
    rows = [g for g in _coverage_gaps(recorded_agent_requests, key) if _is_copilot_with_url_row(*g)]
    assert not rows, f"{len(rows)} Copilot-with-a-URL rows, e.g. {sorted((p, u) for p, u, _ in rows)[:4]}"


@requires_agent
def test_the_copilot_detector_matches_the_installed_agent(recorded_agent_requests):
    """``_agent_copilot_lookup_follows_url`` must say "follows" exactly when the Agent sends a Copilot key with a
    known provider's URL nowhere but that URL, so a renamed Agent function fails here instead of silently
    letting the key reach api.githubcopilot.com."""
    known = "https://api.openai.com/v1"
    sent = _agent_keyed_hosts(recorded_agent_requests, known, "K-synthetic", "copilot")
    assert ("api.githubcopilot.com" in sent) == (not routes._agent_copilot_lookup_follows_url())


@requires_agent
@pytest.mark.parametrize("url", [C, "https://api.openai.com/v1"])
def test_the_early_return_gate_matches_the_installed_agent(url, recorded_agent_requests):
    """On an Agent with the Copilot step, ``_agent_reaches_provider_step_with_url`` must be True exactly when the
    Agent's lookup reaches that step for ``url``: the key then also goes to api.githubcopilot.com. A custom
    endpoint returns early (key stays on the URL); a known provider's URL doesn't."""
    if routes._agent_copilot_lookup_follows_url():
        pytest.skip("the installed Agent has no Copilot live-models step")
    sent = _agent_keyed_hosts(recorded_agent_requests, url, "K-synthetic", "copilot")
    assert ("api.githubcopilot.com" in sent) == routes._agent_reaches_provider_step_with_url("copilot", url)


@requires_agent
def test_the_codex_detector_matches_the_installed_agent(recorded_agent_requests):
    """``_agent_codex_catalog_follows_url`` must say "follows" exactly when the Agent sends nothing to chatgpt.com
    with a URL set, so a renamed Agent attribute fails here instead of silently dropping every Codex proxy key."""
    sent = _agent_keyed_hosts(recorded_agent_requests, C, _jwt("K-synthetic"), "openai-codex")
    assert ("chatgpt.com" in sent) == (not routes._agent_codex_catalog_follows_url())


@pytest.mark.parametrize("raw_provider", ["github", "github-copilot", "GitHub-Copilot"])
def test_copilot_aliases_still_fold_to_copilot(raw_provider):
    """Named directly, so a change to the alias folding shows up here, not only as a coverage failure."""
    assert routes._canonical_context_provider(raw_provider) == "copilot"


def _agent_named_route(provider_id, cfg, monkeypatch):
    """The named-custom entry the installed Agent's runtime would route ``provider_id`` through, for ``cfg``."""
    from hermes_cli import runtime_provider_custom as rpc

    monkeypatch.setattr(rpc._rp(), "load_config", lambda *a, **k: cfg)
    try:
        return rpc._get_named_custom_provider(provider_id, config=cfg, resolve_credentials=False)
    except TypeError:  # older Agents: (requested_provider) only, reads load_config()
        return rpc._get_named_custom_provider(provider_id)


@requires_agent
@pytest.mark.parametrize("provider_id", sorted(routes._CONTEXT_LOOKUP_FIXED_HOSTS))
def test_a_caller_key_for_a_fixed_host_id_is_that_providers_own(provider_id, recorded_agent_requests, monkeypatch):
    """Why a caller key that came with no URL may go to its provider's fixed host (source identity, #8822 note §3b).

    A caller key with a URL owns only that URL. One with no URL owns its provider's fixed hosts, on the premise
    that it's the Agent's own credential for that provider. Wherever the installed Agent contacts a fixed host for
    this id, a ``custom_providers`` / ``providers`` entry merely NAMED like the id must not capture the runtime route;
    otherwise that key could be the entry's and the fixed host would not be its own.
    """
    contacts_fixed_host = bool(_agent_keyed_hosts(recorded_agent_requests, "", "K-synthetic", provider_id)
                               | _agent_keyed_hosts(recorded_agent_requests, "", _jwt("K-synthetic"), provider_id))
    if not contacts_fixed_host:
        pytest.skip(f"the installed Agent contacts no fixed host for {provider_id}")
    for cfg in ({"custom_providers": [{"name": provider_id, "base_url": L, "api_key": "KL-synthetic"}]},
                {"providers": {provider_id: {"base_url": L, "api_key": "KL-synthetic"}}}):
        assert not _agent_named_route(provider_id, cfg, monkeypatch), (
            f"a custom entry named {provider_id!r} captures the runtime route on an Agent that sends that "
            "id's key to its fixed host"
        )
