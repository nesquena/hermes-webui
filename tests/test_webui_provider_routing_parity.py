"""The WebUI in-process runtime forwards the profile's provider_routing block
to AIAgent — parity with the gateway's TurnRunner._build_fresh_agent.

Before the fix, the streaming chat path (POST /api/chat/start, browser turns)
assembled ``_agent_kwargs`` in api/streaming.py without any of the six routing
kwargs (provider_sort / providers_allowed / providers_ignored /
providers_order / provider_require_parameters / provider_data_collection), so
an OpenRouter ``provider_routing`` block in config.yaml (sort: price, etc.)
was silently dropped for browser chat turns while Discord/Telegram/TUI turns
respected it. The fallback sync endpoint (POST /api/chat,
routes._handle_chat_sync) had the same gap.

These tests:
  * unit-test the pure mapping helper ``_provider_routing_kwargs_for_agent``
    (full block, per-param filtering, absent/malformed block == constructor
    defaults, i.e. treated as empty, ``require_parameters`` default);
  * structurally assert (AST) that the streaming construction site feeds the
    helper's result into ``_agent_kwargs`` before the
    ``_AIAgent(**_agent_kwargs)`` construction;
  * structurally assert (AST) that ``_handle_chat_sync`` builds its routing
    kwargs through the same signature-gated helper and passes them via a
    ``**`` expansion (NOT unconditionally as explicit kwargs, which would
    TypeError an older hermes-agent build);
  * prove the sync path's ``_sync_routing_kwargs`` resolves ``${VAR}``
    references in ``provider_routing`` (the raw profile config it reads is
    NOT env-expanded; only the main get_config() loader is), with the
    profile-scoped env taking precedence over process env, matching the
    streaming path's expanded config;
  * prove a stub ``AIAgent`` whose ``__init__`` lacks the routing params is
    still constructible through the sync gating (no TypeError);
  * prove the per-session agent-cache signature CHANGES when
    ``provider_routing`` is edited, so an already-open session mints a fresh
    agent instead of reusing one built on the old routing.

Pre-fix state: ``api.streaming`` has no ``_provider_routing_kwargs_for_agent``
and no wiring, the sync site passes the six kwargs unconditionally, and
``_compute_agent_cache_signature`` has no ``provider_routing_kwargs`` param —
so the helper import raises AttributeError, the AST assertions fail, and the
signature call raises TypeError. Post-fix everything passes.
"""
from __future__ import annotations

import ast
import inspect

ROUTING_KWARGS = {
    "provider_sort",
    "providers_allowed",
    "providers_ignored",
    "providers_order",
    "provider_require_parameters",
    "provider_data_collection",
}


def _helper():
    from api import streaming as streaming_mod
    return streaming_mod._provider_routing_kwargs_for_agent


def _defaults() -> dict:
    """The AIAgent constructor defaults the helper forwards for an empty block
    (gateway parity: TurnRunner._build_fresh_agent)."""
    return {
        "provider_sort": None,
        "providers_allowed": None,
        "providers_ignored": None,
        "providers_order": None,
        "provider_require_parameters": False,
        "provider_data_collection": None,
    }


# ── Helper unit tests ──────────────────────────────────────────────────────

def test_helper_forwards_full_block():
    h = _helper()
    cfg = {"provider_routing": {
        "sort": "price",
        "ignore": ["open-inference"],
        "only": ["openrouter"],
        "order": ["a", "b"],
        "require_parameters": True,
        "data_collection": "extra",
    }}
    out = h(cfg, ROUTING_KWARGS)
    assert out == {
        "provider_sort": "price",
        "providers_ignored": ["open-inference"],
        "providers_allowed": ["openrouter"],
        "providers_order": ["a", "b"],
        "provider_require_parameters": True,
        "provider_data_collection": "extra",
    }


def test_helper_filters_to_supported_params():
    h = _helper()
    out = h({"provider_routing": {"sort": "price"}}, {"provider_sort"})
    assert out == {"provider_sort": "price"}
    # Empty param set (older agent build) -> nothing forwarded.
    assert h({"provider_routing": {"sort": "price"}}, set()) == {}


def test_helper_absent_block_forwards_constructor_defaults():
    h = _helper()
    # Gateway parity (TurnRunner._build_fresh_agent): an absent block forwards
    # the AIAgent constructor defaults (None / False), which are no-ops.
    assert h({}, ROUTING_KWARGS) == _defaults()
    assert h(None, ROUTING_KWARGS) == _defaults()
    assert h({"provider_routing": None}, ROUTING_KWARGS) == _defaults()


def test_helper_malformed_block_equals_absent_block():
    """A malformed ``provider_routing`` (a bare string, or a YAML list) must be
    treated as empty — the SAME result as no block at all — on BOTH the
    streaming and sync paths (both route through this helper). In particular it
    must NOT raise AttributeError from ``.get`` on a non-dict."""
    h = _helper()
    absent = h({}, ROUTING_KWARGS)
    assert h({"provider_routing": "price"}, ROUTING_KWARGS) == absent
    assert h({"provider_routing": ["a", "b"]}, ROUTING_KWARGS) == absent


def test_helper_require_parameters_defaults_false():
    h = _helper()
    out = h({"provider_routing": {}}, {"provider_require_parameters"})
    assert out == {"provider_require_parameters": False}


# ── Streaming construction-site wiring (AST) ───────────────────────────────

def _is_streaming_construction(call: ast.Call) -> bool:
    """True for ``_AIAgent(**_agent_kwargs)``."""
    if not (isinstance(call.func, ast.Name) and call.func.id == "_AIAgent"):
        return False
    return (
        len(call.args) == 0
        and len(call.keywords) == 1
        and call.keywords[0].arg is None
        and isinstance(call.keywords[0].value, ast.Name)
        and call.keywords[0].value.id == "_agent_kwargs"
    )


def _forwards_routing_into_agent_kwargs(fn: ast.AST) -> bool:
    """True if ``fn`` feeds the routing helper's result into ``_agent_kwargs``.

    Accepts both the direct form ``_agent_kwargs.update(
    _provider_routing_kwargs_for_agent(...))`` and the captured form
    ``x = _provider_routing_kwargs_for_agent(...)`` then
    ``_agent_kwargs.update(x)`` (the latter keeps the effective routing in a
    local so the agent-cache signature can include it)."""
    helper_vars = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if (isinstance(t, ast.Name)
                        and isinstance(node.value, ast.Call)
                        and isinstance(node.value.func, ast.Name)
                        and node.value.func.id == "_provider_routing_kwargs_for_agent"):
                    helper_vars.add(t.id)
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "update"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "_agent_kwargs"):
            continue
        if any(isinstance(a, ast.Call)
               and isinstance(a.func, ast.Name)
               and a.func.id == "_provider_routing_kwargs_for_agent"
               for a in node.args):
            return True
        if any(isinstance(a, ast.Name) and a.id in helper_vars for a in node.args):
            return True
    return False


def _functions_with(tree: ast.AST, predicate):
    """Yield (func_node, [matching sub-calls]) for every function whose subtree
    contains at least one call matching ``predicate``."""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        matches = [
            sub for sub in ast.walk(node)
            if isinstance(sub, ast.Call) and predicate(sub)
        ]
        if matches:
            yield node, matches


def test_streaming_construction_sites_forward_provider_routing():
    from api import streaming as streaming_mod
    tree = ast.parse(inspect.getsource(streaming_mod))

    construction_sites = list(_functions_with(tree, _is_streaming_construction))
    assert construction_sites, (
        "expected at least one _AIAgent(**_agent_kwargs) construction in "
        "api/streaming.py — did the chat path move?"
    )

    for fn, constructions in construction_sites:
        assert _forwards_routing_into_agent_kwargs(fn), (
            f"{fn.name} constructs _AIAgent(**_agent_kwargs) but never forwards "
            "provider_routing into _agent_kwargs — OpenRouter sort/ignore/only "
            "would be silently dropped for browser chat turns"
        )
        helper_calls = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "_provider_routing_kwargs_for_agent"
        ]
        assert helper_calls, f"{fn.name} never calls the routing helper"
        # The routing must be resolved before the first construction.
        assert min(c.lineno for c in helper_calls) < min(c.lineno for c in constructions), (
            f"provider_routing resolution in {fn.name} must run before the "
            "_AIAgent(**_agent_kwargs) construction"
        )


# ── Fallback sync endpoint wiring (AST) ────────────────────────────────────

def test_chat_sync_agent_routing_is_signature_gated():
    """The sync POST /api/chat construction must build its routing kwargs via
    the shared signature-gated helper and pass them with ``**``, NOT pass the
    six unconditionally as explicit kwargs (which TypeErrors an older
    hermes-agent build that lacks any of them)."""
    from api import routes as routes_mod
    src_mod = inspect.getsource(routes_mod)
    tree = ast.parse(src_mod)
    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "_handle_chat_sync"),
        None,
    )
    assert fn is not None, "_handle_chat_sync not found in api/routes.py"

    agent_calls = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "AIAgent"
    ]
    assert agent_calls, "expected an AIAgent(...) construction in _handle_chat_sync"

    for call in agent_calls:
        explicit = {k.arg for k in call.keywords if k.arg is not None}
        # The six routing kwargs must NOT be explicit (unconditional) kwargs.
        assert not (explicit & ROUTING_KWARGS), (
            f"_handle_chat_sync passes routing kwargs unconditionally: "
            f"{sorted(explicit & ROUTING_KWARGS)} — an older hermes-agent build "
            "lacking any of them would TypeError every POST /api/chat call. "
            "Gate them on the constructor signature instead."
        )
        # They must be supplied via the **_routing_kwargs star expansion.
        has_routing_star = any(
            k.arg is None
            and isinstance(k.value, ast.Name)
            and k.value.id == "_routing_kwargs"
            for k in call.keywords
        )
        assert has_routing_star, (
            "expected **_routing_kwargs on the _handle_chat_sync AIAgent call"
        )

    seam_calls = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_sync_routing_kwargs"
    ]
    assert seam_calls, (
        "_handle_chat_sync must build routing kwargs via _sync_routing_kwargs "
        "(env-expanded profile config through the shared signature-gated "
        "helper)"
    )
    fn_src = ast.get_source_segment(src_mod, fn)
    assert ("signature(AIAgent" in fn_src) or ("inspect.signature" in fn_src), (
        "_handle_chat_sync must gate routing kwargs on the AIAgent constructor "
        "signature"
    )


def test_sync_chat_survives_old_agent_build_without_routing_params():
    """The sync path must not TypeError on an older hermes-agent build whose
    AIAgent.__init__ lacks the six routing params.

    This exercises the exact gating the sync site relies on: filter the routing
    kwargs against the constructor's real signature, then expand them into the
    AIAgent(...) call. A stub AIAgent that omits the routing params must be
    constructible with the (empty) filtered kwargs."""
    class StubAIAgent:
        def __init__(self, model=None, provider=None, base_url=None,
                     api_key = None, platform=None, quiet_mode=None,
                     enabled_toolsets=None, session_id=None):
            self.model = model

    cfg = {"provider_routing": {"sort": "price", "ignore": ["x"]}}
    params = set(inspect.signature(StubAIAgent.__init__).parameters)
    routing = _helper()(cfg, params)
    assert routing == {}, "no routing kwarg may reach a build that lacks the params"
    # The construction the sync site performs:
    agent = StubAIAgent(
        model="m", provider="openrouter", base_url="u",
        platform="webui", quiet_mode=True, enabled_toolsets=[],
        session_id="s", **routing,
    )
    assert agent.model == "m"  # constructed fine, no TypeError


# ── Agent-cache invalidation on routing edits ──────────────────────────────

def test_agent_cache_signature_changes_when_routing_edited():
    """Editing ``provider_routing`` between two turns in the same session must
    change the agent-cache signature, so the already-open session mints a fresh
    agent (with the new routing) instead of reusing one built on the old.

    ``only``/``ignore`` are allow/deny lists, so this is the difference between
    'a config edit is silently ignored until restart' and actually taking
    effect on the next turn."""
    from api import streaming as streaming_mod

    base = dict(
        resolved_model="m",
        resolved_api_key="k",
        resolved_base_url="u",
        resolved_provider="openrouter",
        runtime_bundle={},
        prefill_context={},
    )
    sig_price = streaming_mod._compute_agent_cache_signature(
        **base, provider_routing_kwargs={"provider_sort": "price"})
    sig_latency = streaming_mod._compute_agent_cache_signature(
        **base, provider_routing_kwargs={"provider_sort": "latency"})
    sig_none = streaming_mod._compute_agent_cache_signature(
        **base, provider_routing_kwargs=None)

    assert sig_price != sig_latency, (
        "editing sort between turns must change the agent-cache signature "
        "(so a new agent is built)"
    )
    assert sig_price != sig_none, (
        "the presence of routing must change the signature"
    )
    assert sig_price == streaming_mod._compute_agent_cache_signature(
        **base, provider_routing_kwargs={"provider_sort": "price"}
    ), "same routing must keep the signature stable within a turn"


# ── Sync-path env expansion ─────────────────────────────────────────────────

def test_sync_routing_kwargs_expand_env_vars(monkeypatch):
    """The sync path must resolve ``${VAR}`` references inside
    ``provider_routing``.

    ``_handle_chat_sync`` reads the session profile config through
    ``_read_profile_model_config``: a RAW ``yaml.safe_load`` with no env
    expansion (only the main ``get_config()`` loader expands). Without
    expansion, ``provider_routing: {only: [${ROUTE_PROVIDER}]}`` reaches the
    Agent as the literal string ``${ROUTE_PROVIDER}`` and is sent straight
    into OpenRouter's request body. ``_sync_routing_kwargs`` must expand with
    the same ``_expand_env_vars()`` the loader uses, matching the streaming
    path (which reads an already-expanded config)."""
    from api import routes as routes_mod
    from api import config as config_mod

    cfg = {"provider_routing": {"only": ["${ROUTE_PROVIDER}"], "sort": "price"}}
    params = ROUTING_KWARGS

    # Process env fallback: no thread-local profile env set.
    monkeypatch.setenv("ROUTE_PROVIDER", "openrouter")
    config_mod._clear_thread_env()
    out = routes_mod._sync_routing_kwargs(cfg, params)
    assert out["providers_allowed"] == ["openrouter"], (
        f"${{ROUTE_PROVIDER}} must resolve against the environment, got "
        f"{out['providers_allowed']!r}"
    )
    assert out["provider_sort"] == "price"
    assert "${" not in str(out), (
        "a literal ${...} reference must never reach the Agent"
    )

    # Profile-scoped env (thread-local, active when the request runs under a
    # profile env scope) takes precedence over process env, exactly like the
    # streaming path's env resolution.
    config_mod._set_thread_env(ROUTE_PROVIDER="profileval")
    try:
        out2 = routes_mod._sync_routing_kwargs(cfg, params)
    finally:
        config_mod._clear_thread_env()
    assert out2["providers_allowed"] == ["profileval"], (
        "the profile-scoped env value must win over the process env"
    )
    assert "${" not in str(out2)
