"""#8822: the context-length lookup sends a key only where its own source sends it.

The lookup hands (URL, key, provider) to the Agent's ``get_model_context_length``. The Agent contacts the URL's
host, plus fixed metadata hosts for some providers (``_CONTEXT_LOOKUP_FIXED_HOSTS``). A key may go only to hosts
its own config source owns: that source's ``base_url``, plus the provider's fixed hosts when the source is that
provider's own. Otherwise the lookup sends no key. It never changes the URL.

None of these tests needs an Agent, except the last two groups (``requires_agent``), which probe the installed
Agent's own metadata lookup under a no-network request recorder.
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
    state = {"mode": "miss", "seen": [], "home": agent_home}
    payload = {"data": [{"id": "m-x", "context_length": 8192,
                         "capabilities": {"limits": {"max_prompt_tokens": 8192}}}],
               "models": [{"slug": "m-x", "context_window": 8192}]}

    def note(url, headers):
        creds = [str(v).removeprefix("Bearer ") for k, v in dict(headers or {}).items()
                 if str(k).lower() in ("authorization", "x-api-key", "api-key")]
        state["seen"].append((urllib.parse.urlsplit(str(url)).hostname or "", creds))

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
                if "cache" in name.lower() and not name.isupper() and isinstance(value, (dict, set, list)):
                    value.clear()
        shutil.rmtree(home, ignore_errors=True)
        home.mkdir(parents=True)
        state["mode"] = mode
        state["seen"].clear()
        try:
            mm.get_model_context_length("m-x", url, api_key=key, provider=provider)
        except Exception:
            pass
        hosts |= {host for host, creds in state["seen"] if key and key in creds}
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


PROBED_PROVIDERS = ("anthropic", "novita", "copilot", "copilot-acp", "github-copilot", "openai-codex", "openai",
                    "nous", "bedrock", "fireworks", "deepseek", "openrouter", "vllm", "local", "custom:my-proxy")


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


@requires_agent
@pytest.mark.parametrize("shape", ["synthetic", "jwt"])
@pytest.mark.parametrize("raw_provider", PROBED_PROVIDERS)
def test_the_fixed_host_table_covers_the_installed_agent(raw_provider, shape, recorded_agent_requests):
    """Every host the installed Agent sends a key to is the URL's own host or listed in the table.

    The Agent is given the id the lookup emits (``_canonical_context_provider``), not the raw spelling: the
    lookup never hands the Agent ``github-copilot``. If alias folding ever stops, the emitted id has no row,
    the fix drops the key (no leak), and this test fails loudly on the unlisted host.
    """
    provider = routes._canonical_context_provider(raw_provider)
    key = "K-synthetic" if shape == "synthetic" else _jwt("K-synthetic")
    for url in ("", C):
        sent = _agent_keyed_hosts(recorded_agent_requests, url, key, provider)
        known = {o.rsplit(":", 1)[0] for o in routes._context_lookup_contacted_origins(provider, url)}
        assert sent <= known, (
            f"{raw_provider} (emitted {provider}) url={url or '-'}: the Agent sent the key to {sorted(sent - known)}"
        )


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
    """Why the caller's key may go to its provider's fixed host (source identity, #8822 note §3).

    The caller passes the Agent's own runtime pair. Wherever the installed Agent contacts a fixed host for this
    id, a ``custom_providers`` / ``providers`` entry merely NAMED like the id must not capture the runtime route;
    otherwise the caller's key could be that entry's key and the fixed host would not be its own.
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
