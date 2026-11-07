"""#8822: the context-length lookup sends a key only where its own source sends it.

The lookup hands (URL, key, provider) to the Agent's ``get_model_context_length``. The Agent contacts the URL's
host, plus fixed metadata hosts for some providers (``_CONTEXT_LOOKUP_FIXED_HOSTS``). A key may go only to hosts
its own config source owns: that source's ``base_url``, plus the provider's fixed hosts when the source is that
provider's own. Otherwise the lookup sends no key. It never changes the URL.

None of these tests needs an Agent.
"""
import pathlib
import re
import textwrap
import types

import pytest

from api import routes

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


def test_a_caller_key_owns_the_callers_url():
    got = _lookup({"model": {"provider": "custom:my-proxy", "base_url": A}}, "custom:my-proxy",
                  base_url=C, api_key="KC")
    assert (got.base_url, got.api_key) == (C, "KC")


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
    assert table["openai-codex"]["no_url"] == table["openai-codex"]["with_url"] == ("chatgpt.com",)


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


def test_no_site_falls_back_to_the_agents_key_after_the_lookup():
    assert not re.search(r"_ctx_lookup\.api_key\s+or\s+getattr\(agent,\s*'api_key'", STREAMING_PY)
