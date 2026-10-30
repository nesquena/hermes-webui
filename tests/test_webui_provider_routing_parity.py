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
  * structurally assert (AST) that the sync seam call runs inside
    ``with profile_env_for_thread_readonly(s, ...)`` and NOT inside
    ``profile_env_for_background_worker``: a named profile's
    ``provider_routing`` must resolve against that profile's own .env on
    the HTTP handler thread, not the server process env, and the pure
    in-memory expansion must bind the env to the handler thread ONLY (the
    background worker mirrors it into os.environ under a lock that covers
    setup/restore only, so interleaved sync and streaming scopes restore
    over each other's process env); and behaviorally prove the binding:
    with the session profile env bound the profile .env value wins, unbound
    the process env value is what the expansion picks up, and os.environ is
    never mutated by the thread-only scope;
  * prove a ``${VAR}`` that only the server process env defines (or that
    is defined nowhere) is DROPPED from the sync routing kwargs with a
    warning, never forwarded as a literal (which would make OpenRouter
    target a nonexistent provider and fail a turn master completes);
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
import types

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


def test_helper_drops_unresolved_env_refs_for_both_paths(caplog):
    """The shared mapper (used by streaming AND sync) never forwards ``${...}``.

    A routing value whose ``${VAR}`` stayed unresolved after expansion must be
    dropped with a warning, keeping resolved list items; forwarding the literal
    makes OpenRouter target a provider that does not exist (#7812 round 6).
    """
    import logging

    params = {
        "providers_allowed",
        "providers_ignored",
        "provider_sort",
        "provider_require_parameters",
    }
    cfg = {
        "provider_routing": {
            "only": ["${MISSING_ROUTE}", "anthropic"],
            "ignore": ["${ALSO_MISSING}"],
            "sort": "${SORT_UNSET}",
        }
    }
    with caplog.at_level(logging.WARNING):
        kwargs = _helper()(cfg, params)
    assert kwargs["providers_allowed"] == ["anthropic"]
    assert kwargs["providers_ignored"] is None
    assert kwargs["provider_sort"] is None
    assert "${" not in repr(kwargs)
    assert cfg["provider_routing"]["only"] == ["${MISSING_ROUTE}", "anthropic"], "input must not be mutated"
    assert "dropping" in caplog.text


def test_helper_require_parameters_defaults_false():
    h = _helper()
    out = h({"provider_routing": {}}, {"provider_require_parameters"})
    assert out == {"provider_require_parameters": False}


# ── Streaming construction-site wiring (AST) ───────────────────────────────

def _is_streaming_construction(call: ast.Call) -> bool:
    """True for ``_AIAgent(**_agent_kwargs)`` and for its #8517 profile-scoped
    form ``construct_profile_scoped_agent(_AIAgent, _agent_kwargs, ...)``."""
    if (
        isinstance(call.func, ast.Name)
        and call.func.id == "construct_profile_scoped_agent"
        and len(call.args) >= 2
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == "_AIAgent"
        and isinstance(call.args[1], ast.Name)
        and call.args[1].id == "_agent_kwargs"
    ):
        return True
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
    # #8517: the sync agent is built through the profile-scoped constructor,
    # ``construct_profile_scoped_agent(AIAgent, dict(<kwargs>), ...)``; its
    # ``dict(...)`` carries the constructor kwargs.
    agent_calls += [
        n.args[1] for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "construct_profile_scoped_agent"
        and len(n.args) >= 2
        and isinstance(n.args[0], ast.Name)
        and n.args[0].id == "AIAgent"
        and isinstance(n.args[1], ast.Call)
        and isinstance(n.args[1].func, ast.Name)
        and n.args[1].func.id == "dict"
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


# ── Sync-path profile env binding ──────────────────────────────────────────

def _call_name(call: ast.Call):
    """The callable's name for a bare or module-qualified call."""
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def test_chat_sync_routing_expansion_binds_session_profile_env():
    """The sync seam's ``${VAR}`` expansion must run with the session's
    profile env bound to the handler thread ONLY.

    ``_expand_env_vars()`` resolves ``${VAR}`` thread-local-first, then
    process env. The streaming worker binds the session profile's ``.env``
    into the thread-local before reading its config, but the sync HTTP
    handler thread (``POST /api/chat``) binds nothing. Unbound, a named
    profile's ``provider_routing`` would resolve against the server process
    env (the process-wide profile's ``.env``): an unset variable leaks
    through as a literal, and a different process value applies the wrong
    provider routing.

    The binding must be ``profile_env_for_thread_readonly`` (thread-local
    channel only), NOT ``profile_env_for_background_worker``: the seam does
    a pure in-memory expansion and reads nothing from ``os.environ``, but
    the background worker mirrors the profile env into ``os.environ`` and
    its ``_ENV_LOCK`` covers setup/restore only, not the body, so a sync
    scope and a concurrent streaming worker (``CHAT_LOCK`` does not
    protect streaming workers) restore over each other's mirrored env.
    The thread-only scope is a no-op for default-profile sessions."""
    from api import routes as routes_mod
    src_mod = inspect.getsource(routes_mod)
    tree = ast.parse(src_mod)
    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "_handle_chat_sync"),
        None,
    )
    assert fn is not None, "_handle_chat_sync not found in api/routes.py"

    seam_calls = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_sync_routing_kwargs"
    ]
    assert seam_calls, (
        "_handle_chat_sync must build routing kwargs via _sync_routing_kwargs"
    )

    def _inside_with(call: ast.Call, manager_name: str) -> bool:
        return any(
            isinstance(w, ast.With)
            and any(
                isinstance(item.context_expr, ast.Call)
                and _call_name(item.context_expr) == manager_name
                for item in w.items
            )
            and any(c is call for c in ast.walk(w))
            for w in ast.walk(fn)
        )

    for call in seam_calls:
        assert _inside_with(call, "profile_env_for_thread_readonly"), (
            "the _sync_routing_kwargs seam call must run inside "
            "`with profile_env_for_thread_readonly(s, ...)` so ${VAR} in a "
            "named profile's provider_routing resolves against that "
            "profile's own .env instead of the server process env, bound to "
            "the handler thread only"
        )
        assert not _inside_with(call, "profile_env_for_background_worker"), (
            "the sync routing seam must NOT use "
            "profile_env_for_background_worker: it mirrors the profile env "
            "into os.environ under a lock that covers setup/restore only, "
            "so a sync scope and a concurrent streaming turn clobber each "
            "other's process env"
        )


def test_sync_routing_kwargs_resolve_named_profile_env_not_process_env(
    tmp_path, monkeypatch
):
    """A named profile's ``provider_routing`` ``${VAR}`` must resolve
    against that profile's ``.env`` on the sync path, not the server
    process env, without mutating the process env.

    This is the review finding's repro: with nothing bound on the handler
    thread, the unbound expansion of
    ``provider_routing.only = [${ROUTE_PROVIDER}]`` either leaked the
    literal (variable unset in the process env) or picked up an unrelated
    process value (wrong provider routing). ``profile_env_for_thread_readonly``
    binds the profile's ``.env`` into the thread-local channel and blocks the process-env fallback, exactly what the sync seam needs
    for a pure in-memory read; unlike
    ``profile_env_for_background_worker`` it must leave ``os.environ``
    untouched."""
    import os

    from api import config as config_mod
    from api import profiles as profiles_mod
    from api import routes as routes_mod

    profile_home = tmp_path / "route-profile-home"
    profile_home.mkdir()
    (profile_home / ".env").write_text(
        "ROUTE_PROVIDER=profile-openrouter\n", encoding="utf-8"
    )
    # The manager resolves the profile home through this function.
    monkeypatch.setattr(
        profiles_mod, "get_hermes_home_for_profile",
        lambda name: profile_home,
    )
    # A conflicting process-env value: exactly the wrong value the unbound
    # (pre-fix) sync expansion would pick up.
    monkeypatch.setenv("ROUTE_PROVIDER", "process-openrouter")
    config_mod._clear_thread_env()
    config_mod._thread_ctx.block_process_env_fallback = False
    old_hermes_home = os.environ.get("HERMES_HOME")

    cfg = {"provider_routing": {"only": ["${ROUTE_PROVIDER}"], "sort": "price"}}
    session = types.SimpleNamespace(profile="route-profile", session_id="s1")

    # Unbound (the pre-fix handler state): the process env value is what the
    # expansion resolves to.
    unbound = routes_mod._sync_routing_kwargs(cfg, ROUTING_KWARGS)
    assert unbound["providers_allowed"] == ["process-openrouter"], (
        "baseline: without the profile env bound, the process env value is "
        "what the unbound sync expansion resolves to"
    )

    # Bound, the way the handler now wraps the seam call (thread-only):
    with profiles_mod.profile_env_for_thread_readonly(
        session, "sync provider routing"
    ):
        bound = routes_mod._sync_routing_kwargs(cfg, ROUTING_KWARGS)
        # The scope must not mirror the profile env into the process env.
        assert os.environ.get("ROUTE_PROVIDER") == "process-openrouter"
        assert os.environ.get("HERMES_HOME") == old_hermes_home
    assert bound["providers_allowed"] == ["profile-openrouter"], (
        "with the session profile env bound, the profile's .env value must "
        "win over the process env"
    )
    assert bound["provider_sort"] == "price"
    assert "${" not in str(bound), (
        "a literal ${...} reference must never reach the Agent"
    )

    # The scope restores the prior thread state on exit AND leaves
    # os.environ exactly as it found it.
    after = routes_mod._sync_routing_kwargs(cfg, ROUTING_KWARGS)
    assert after["providers_allowed"] == ["process-openrouter"]
    assert config_mod._thread_ctx.block_process_env_fallback is False
    assert os.environ.get("ROUTE_PROVIDER") == "process-openrouter"
    assert os.environ.get("HERMES_HOME") == old_hermes_home


# ── Sync-path drop of process-env-only references ───────────────────────────

def test_sync_routing_kwargs_drop_process_env_only_refs(tmp_path, monkeypatch):
    """A ``${VAR}`` that only the server process env defines must be DROPPED
    from the sync routing kwargs, not forwarded as the literal ``${VAR}``.

    The fail-closed profile scope (block_process_env_fallback) refuses to
    expand a variable the profile's ``.env`` does not define. Forwarding the
    surviving literal would make OpenRouter restrict the request to a
    provider that does not exist and fail the turn, where master (which
    forwards no routing on the sync path at all) succeeds. Dropping the
    affected field gives master's outcome (no preference sent for it) with
    a warning, without widening what a profile may read from the server
    environment. This is the review's requested repro: a named profile's
    routing referencing a process-env-only variable on the sync endpoint."""
    from api import config as config_mod
    from api import profiles as profiles_mod
    from api import routes as routes_mod

    profile_home = tmp_path / "proc-only-home"
    profile_home.mkdir()
    # The profile defines an unrelated variable; NOT PROC_ONLY.
    (profile_home / ".env").write_text("UNRELATED=keep\n", encoding="utf-8")
    monkeypatch.setattr(
        profiles_mod, "get_hermes_home_for_profile",
        lambda name: profile_home,
    )
    # Defined ONLY in the server process env: exactly the SHOULD-FIX case.
    monkeypatch.setenv("PROC_ONLY", "proc-value")
    config_mod._clear_thread_env()

    cfg = {"provider_routing": {"only": ["${PROC_ONLY}"], "sort": "price"}}
    session = types.SimpleNamespace(profile="proc-only", session_id="s2")

    # Bound, the way the handler wraps the seam call:
    with profiles_mod.profile_env_for_thread_readonly(session, "test"):
        out = routes_mod._sync_routing_kwargs(cfg, ROUTING_KWARGS)
    assert out["providers_allowed"] is None, (
        f"a process-env-only ${{...}} reference must be dropped (no "
        f"preference forwarded), got {out.get('providers_allowed')!r}"
    )
    assert "${" not in str(out), (
        "a literal ${...} reference must never reach the Agent"
    )
    assert out == {**_defaults(), "provider_sort": "price"}, (
        "unaffected fields keep forwarding; the dropped field equals the "
        f"constructor default (master's outcome): {out!r}"
    )

    # A partially-resolvable list keeps its resolvable items and drops the
    # unresolved one.
    cfg_partial = {
        "provider_routing": {"only": ["anthropic", "${PROC_ONLY}"]}
    }
    with profiles_mod.profile_env_for_thread_readonly(session, "test"):
        out_partial = routes_mod._sync_routing_kwargs(cfg_partial, ROUTING_KWARGS)
    assert out_partial["providers_allowed"] == ["anthropic"], (
        "resolvable list items must be kept; only the unresolved one drops"
    )
    assert "${" not in str(out_partial)

    # Default profile (no scope bound, process-env fallback allowed): a
    # variable defined NOWHERE still survives expansion as a literal and is
    # dropped the same way, so a broken reference never reaches OpenRouter
    # on the sync path regardless of profile.
    config_mod._clear_thread_env()
    config_mod._thread_ctx.block_process_env_fallback = False
    monkeypatch.delenv("NEVER_DEFINED_ANYWHERE", raising=False)
    cfg_missing = {"provider_routing": {"ignore": ["${NEVER_DEFINED_ANYWHERE}"]}}
    out_missing = routes_mod._sync_routing_kwargs(cfg_missing, ROUTING_KWARGS)
    assert out_missing["providers_ignored"] is None
    assert "${" not in str(out_missing)


def test_profile_env_for_thread_readonly_touches_no_process_env(
    tmp_path, monkeypatch
):
    """The sync seam's profile binding must not mutate process-wide
    ``os.environ`` (the review's P1).

    ``profile_env_for_background_worker`` mirrors the profile env into
    ``os.environ`` under an ``_ENV_LOCK`` that covers setup/restore only,
    not the body, so if a streaming turn starts while a sync scope is open,
    the sync cleanup restores the earlier env over the running turn's
    settings, and the streaming cleanup can leave the sync profile's values
    behind. ``CHAT_LOCK`` does not protect streaming workers. The thread-only
    manager binds ``_thread_ctx`` on the current thread and leaves
    ``os.environ`` untouched, so concurrent scopes cannot clobber each
    other."""
    import os

    from api import config as config_mod
    from api import profiles as profiles_mod

    profile_home = tmp_path / "no-process-mirror"
    profile_home.mkdir()
    (profile_home / ".env").write_text(
        "ROUTE_PROVIDER=profile-openrouter\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        profiles_mod, "get_hermes_home_for_profile",
        lambda name: profile_home,
    )
    monkeypatch.delenv("ROUTE_PROVIDER", raising=False)
    old_hermes_home = os.environ.get("HERMES_HOME")
    config_mod._clear_thread_env()

    session = types.SimpleNamespace(profile="no-process-mirror", session_id="s3")
    with profiles_mod.profile_env_for_thread_readonly(session, "test"):
        # Thread scope is bound: the expansion resolves the profile value...
        assert config_mod._expand_env_vars(
            {"k": "${ROUTE_PROVIDER}"}
        )["k"] == "profile-openrouter"
        # ...while the process env is untouched:
        assert "ROUTE_PROVIDER" not in os.environ, (
            "the thread-only scope must not mirror the profile .env into "
            "os.environ"
        )
        assert os.environ.get("HERMES_HOME") == old_hermes_home

    # Thread state is restored on exit.
    assert not getattr(config_mod._thread_ctx, "env", {})
    assert config_mod._thread_ctx.block_process_env_fallback is False

    # Default profile: no-op, the thread's existing env state is preserved.
    config_mod._set_thread_env(SENTINEL="pre")
    default_session = types.SimpleNamespace(profile="default", session_id="s4")
    with profiles_mod.profile_env_for_thread_readonly(default_session, "test"):
        assert config_mod._thread_ctx.env.get("SENTINEL") == "pre"
        assert config_mod._thread_ctx.block_process_env_fallback is False
    config_mod._clear_thread_env()
